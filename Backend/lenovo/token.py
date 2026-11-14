"""联想应用商店的设备指纹与 token 注册 / 缓存。

联想 PC 商店除“下载地址列表”外都需要请求头 ``token: <JWT>``。token 由设备指纹向
``POST /appbase/token/register`` 换取，JWT 的 ``exp`` 实测是注册时间 + 15 天。本模块：

- 随机生成并持久化一份设备指纹（``mac`` 12 位小写 hex、``sn`` 64 位 hex、``uuid`` 大写
  GUID），首次生成后写进 ``<cache_dir>/lenovo/token.json``，重启复用同一身份；
- token 缓存在内存；``exp - 3600`` 秒之前直接复用，之后重新注册；
- 可选磁盘缓存 ``{schema_version, fingerprint, token, expires_at}``（原子写，损坏忽略）；
- 接口返回 ``token invalid`` 时由 :meth:`LenovoTokenManager.refresh_token` 强制重新注册
  一次（多个并发请求只注册一次）。
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
import logging
import math
import os
from pathlib import Path
import secrets
import tempfile
import time
from typing import Any
import uuid as uuid_module

import httpx2

from .errors import LenovoProtocolError, LenovoTimeoutError, LenovoUnavailableError

logger = logging.getLogger("winappsmanager.lenovo")

BASE_URL = "https://pc-store.lenovomm.cn"
TOKEN_REGISTER_PATH = "/appbase/token/register"
TOKEN_SCHEMA_VERSION = 1
# token 在过期前 1 小时就不再复用，避免请求刚好卡在过期边界。
TOKEN_EXPIRY_MARGIN_SECONDS = 3600

# 设备指纹里的固定示例值（联想的接口不校验这些字段，随机 MAC 也能拿到 token）。
_DEFAULT_FINGERPRINT: dict[str, str] = {
    "ch": "9.0.3400.1106",
    "cpuIdentifier": "Intel64 Family 6 Model 165 Stepping 3",
    "cpuVendor": "GenuineIntel",
    "mt": "None",
    "os": "win10",
    "osBit": "64",
    "osVersion": "10.0.19045",
}

_FINGERPRINT_FIELDS = (
    "ch",
    "cpuIdentifier",
    "cpuVendor",
    "mac",
    "mt",
    "os",
    "osBit",
    "osVersion",
    "sn",
    "uuid",
)


class DeviceFingerprint:
    """一次注册所用的设备指纹；字段集合与联想真实请求体一致。"""

    __slots__ = ("ch", "cpu_identifier", "cpu_vendor", "mac", "mt", "os", "os_bit", "os_version", "sn", "uuid")

    def __init__(
        self,
        *,
        ch: str,
        cpu_identifier: str,
        cpu_vendor: str,
        mac: str,
        mt: str,
        os: str,
        os_bit: str,
        os_version: str,
        sn: str,
        uuid: str,
    ) -> None:
        self.ch = ch
        self.cpu_identifier = cpu_identifier
        self.cpu_vendor = cpu_vendor
        self.mac = mac
        self.mt = mt
        self.os = os
        self.os_bit = os_bit
        self.os_version = os_version
        self.sn = sn
        self.uuid = uuid

    @classmethod
    def random(cls) -> "DeviceFingerprint":
        """生成随机但形状合法的设备指纹。"""

        return cls(
            ch=_DEFAULT_FINGERPRINT["ch"],
            cpu_identifier=_DEFAULT_FINGERPRINT["cpuIdentifier"],
            cpu_vendor=_DEFAULT_FINGERPRINT["cpuVendor"],
            mac=secrets.token_hex(6),
            mt=_DEFAULT_FINGERPRINT["mt"],
            os=_DEFAULT_FINGERPRINT["os"],
            os_bit=_DEFAULT_FINGERPRINT["osBit"],
            os_version=_DEFAULT_FINGERPRINT["osVersion"],
            sn=secrets.token_hex(32),
            uuid=str(uuid_module.uuid4()).upper(),
        )

    def to_payload(self) -> dict[str, str]:
        return {
            "ch": self.ch,
            "cpuIdentifier": self.cpu_identifier,
            "cpuVendor": self.cpu_vendor,
            "mac": self.mac,
            "mt": self.mt,
            "os": self.os,
            "osBit": self.os_bit,
            "osVersion": self.os_version,
            "sn": self.sn,
            "uuid": self.uuid,
        }

    def to_json(self) -> dict[str, str]:
        return self.to_payload()

    @classmethod
    def from_document(cls, document: Any) -> "DeviceFingerprint | None":
        if not isinstance(document, Mapping):
            return None
        values: dict[str, str] = {}
        for field in _FINGERPRINT_FIELDS:
            value = document.get(field)
            if not isinstance(value, str) or not value:
                return None
            values[field] = value
        return cls(
            ch=values["ch"],
            cpu_identifier=values["cpuIdentifier"],
            cpu_vendor=values["cpuVendor"],
            mac=values["mac"],
            mt=values["mt"],
            os=values["os"],
            os_bit=values["osBit"],
            os_version=values["osVersion"],
            sn=values["sn"],
            uuid=values["uuid"],
        )


@dataclass(frozen=True)
class CachedToken:
    token: str
    # 绝对时间戳（epoch 秒）；到达该时间后重新注册。
    expires_at: float


def parse_jwt_exp(token: str) -> int | None:
    """解析 JWT payload 的 ``exp``（base64url，不验签）。

    解析失败（不是三段式 JWT、payload 不是 base64url JSON、``exp`` 不是正整数）返回
    ``None``；调用方决定这是协议错误还是可降级情况。
    """

    if not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) < 2 or not parts[1]:
        return None
    payload = parts[1]
    try:
        padded = payload + "=" * (-len(payload) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        document = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        return None
    if not isinstance(document, Mapping):
        return None
    exp = document.get("exp")
    # 注意 bool 是 int 的子类，这里必须排除。
    if type(exp) is not int or exp <= 0:
        return None
    return exp


def coerce_status(value: Any) -> int | None:
    """把联想响应包络里的 ``status`` 统一成数字；无法解析时返回 ``None``。

    正常响应是数字 ``0``，失败时可能是字符串 ``"502"``，必须同时兼容。
    """

    if type(value) is int:
        return value
    if type(value) is str:
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            return None
    return None


class LenovoTokenManager:
    """注册 / 缓存联想 token；磁盘保存设备指纹与当前 token。"""

    def __init__(
        self,
        client: httpx2.AsyncClient,
        directory: Path | str | None = None,
        *,
        cache_enabled: bool = True,
        cache_persist: bool = True,
        timeout_seconds: float = 20.0,
        connect_timeout_seconds: float = 5.0,
        clock: Callable[[], float] = time.time,
        base_url: str = BASE_URL,
    ) -> None:
        if type(cache_enabled) is not bool or type(cache_persist) is not bool:
            raise ValueError("cache_enabled / cache_persist must be booleans")
        for name, value in (("timeout_seconds", timeout_seconds), ("connect_timeout_seconds", connect_timeout_seconds)):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a finite positive value")
        if not callable(clock):
            raise ValueError("clock must be callable")
        self._client = client
        self._cache_enabled = cache_enabled
        self._cache_persist = cache_persist
        self._timeout_seconds = float(timeout_seconds)
        self._connect_timeout_seconds = float(connect_timeout_seconds)
        self._clock = clock
        self._base_url = base_url.rstrip("/")
        self._path = _prepare_token_path(directory) if cache_persist and cache_enabled else None
        self._fingerprint: DeviceFingerprint | None = None
        self._cached: CachedToken | None = None
        self._disk_loaded = False
        self._lock = asyncio.Lock()
        self._registrations = 0

    # -- 公开接口 ---------------------------------------------------------

    @property
    def registrations(self) -> int:
        """已经向联想注册 token 的次数（诊断用）。"""

        return self._registrations

    async def get_token(self) -> str:
        """返回未过期的 token；没有可用 token 时注册一个新的。"""

        async with self._lock:
            now = self._clock()
            if self._cached is not None and now < self._cached.expires_at:
                return self._cached.token
            self._cached = None
            self._load_disk_locked()
            if self._cached is not None and self._clock() < self._cached.expires_at:
                return self._cached.token
            self._cached = None
            return (await self._register_locked()).token

    async def refresh_token(self, invalid_token: str) -> str:
        """``invalid_token`` 被上游判定失效后重新注册。

        并发场景下多个请求可能同时发现同一个 token 失效：只有第一个真正注册，其余请求
        直接复用新 token。
        """

        async with self._lock:
            now = self._clock()
            current = self._cached
            if current is not None and current.token != invalid_token and now < current.expires_at:
                return current.token
            self._cached = None
            return (await self._register_locked()).token

    # -- 内部实现 ---------------------------------------------------------

    async def _register_locked(self) -> CachedToken:
        fingerprint = self._fingerprint
        if fingerprint is None:
            self._load_disk_locked()
            fingerprint = self._fingerprint
        if fingerprint is None:
            fingerprint = DeviceFingerprint.random()
            self._fingerprint = fingerprint
            # 首先生成并持久化设备指纹，避免注册失败后每次重启都换机器身份。
            self._save_disk_locked(fingerprint, None)

        payload = fingerprint.to_payload()
        try:
            response = await self._client.post(
                f"{self._base_url}{TOKEN_REGISTER_PATH}",
                json=payload,
                headers={"Accept": "application/json"},
                timeout=self._timeout(),
            )
            response.raise_for_status()
        except httpx2.TimeoutException:
            raise LenovoTimeoutError("联想 token 注册请求超时。") from None
        except httpx2.HTTPStatusError as error:
            raise LenovoUnavailableError(
                f"联想 token 注册请求失败（HTTP {error.response.status_code}）。"
            ) from None
        except httpx2.HTTPError:
            raise LenovoUnavailableError("联想 token 注册服务暂时无法连接。") from None

        try:
            document = response.json()
        except (ValueError, UnicodeError):
            raise LenovoProtocolError("联想 token 注册响应不是有效的 JSON。") from None
        if not isinstance(document, dict):
            raise LenovoProtocolError("联想 token 注册响应结构无效。")
        status = coerce_status(document.get("status"))
        if status is None:
            raise LenovoProtocolError("联想 token 注册响应缺少有效的 status。")
        if status != 0:
            message = _text(document.get("message"))
            raise LenovoUnavailableError(
                f"联想 token 注册被上游拒绝{f'：{message}' if message else '。'}"
            )
        token = document.get("data")
        if not isinstance(token, str) or not token:
            raise LenovoProtocolError("联想 token 注册响应缺少 token。")
        exp = parse_jwt_exp(token)
        if exp is None:
            raise LenovoProtocolError("联想 token 不是包含 exp 的合法 JWT。")
        expires_at = float(exp) - TOKEN_EXPIRY_MARGIN_SECONDS
        if expires_at <= self._clock():
            raise LenovoProtocolError("联想 token 的有效期短于提前刷新余量。")

        self._registrations += 1
        self._fingerprint = fingerprint
        cached = CachedToken(token=token, expires_at=expires_at)
        self._cached = cached
        self._save_disk_locked(fingerprint, cached)
        return cached

    def _timeout(self) -> httpx2.Timeout:
        return httpx2.Timeout(self._timeout_seconds, connect=self._connect_timeout_seconds)

    def _load_disk_locked(self) -> None:
        if self._disk_loaded or self._path is None:
            return
        self._disk_loaded = True
        document = _read_json_file(self._path)
        if document is None:
            return
        if document.get("schema_version") != TOKEN_SCHEMA_VERSION:
            return
        fingerprint = DeviceFingerprint.from_document(document.get("fingerprint"))
        if fingerprint is None:
            return
        self._fingerprint = fingerprint
        token = document.get("token")
        expires_at = document.get("expires_at")
        if (
            isinstance(token, str)
            and token
            and type(expires_at) in (int, float)
            and math.isfinite(float(expires_at))
        ):
            self._cached = CachedToken(token=token, expires_at=float(expires_at))

    def _save_disk_locked(self, fingerprint: DeviceFingerprint, cached: CachedToken | None) -> None:
        if self._path is None:
            return
        document = {
            "schema_version": TOKEN_SCHEMA_VERSION,
            "fingerprint": fingerprint.to_json(),
            "token": cached.token if cached is not None else "",
            "expires_at": cached.expires_at if cached is not None else 0.0,
        }
        try:
            _write_json_file(self._path, document)
        except OSError:
            logger.warning("写入联想 token 缓存失败: %s", self._path, exc_info=True)


def _prepare_token_path(directory: Path | str | None) -> Path | None:
    if directory is None:
        return None
    try:
        text = os.fspath(directory)
    except TypeError as error:
        raise ValueError("directory must be a filesystem path") from error
    if not str(text).strip():
        raise ValueError("directory must be a non-empty path")
    return Path(text).expanduser() / "token.json"


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _write_json_file(path: Path, document: Mapping[str, Any]) -> None:
    """原子写 JSON（临时文件 + ``os.replace``），供 token 缓存使用。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        except OSError:
            pass


__all__ = [
    "BASE_URL",
    "CachedToken",
    "DeviceFingerprint",
    "LenovoTokenManager",
    "TOKEN_EXPIRY_MARGIN_SECONDS",
    "TOKEN_REGISTER_PATH",
    "coerce_status",
    "parse_jwt_exp",
]
