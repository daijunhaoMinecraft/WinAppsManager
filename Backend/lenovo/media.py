"""联想图片代理缓存（图标 / 截图 / 封面）。

联想图片 CDN 会拒绝 ``curl/8.x`` 这类 UA（直接 403），也不能让客户端直连，因此图片全部由
后端代理：

- 拉图必须带浏览器 UA，且 URL 的 host 必须在白名单内（``lsw-fast.lenovo.com.cn``、
  ``imgs.lenovomm.cn``、``pc1.gtimg.com``、``pc-store-video.lenovomm.cn``，以及
  ``*.lenovomm.cn`` / ``*.lenovo.com.cn`` 子域），否则抛 :class:`LenovoProtocolError`，避免
  SSRF 与把非联想域名写进缓存；
- 用 Content-Type / URL 后缀识别 ``png/jpg/jpeg/gif/ico/bmp``，单图上限 10 MB；
- 磁盘 ``<cache>/lenovo/media/<sha256(url)>.<ext>`` + sidecar ``<sha256(url)>.json``
  （``url`` / ``content_type`` / ``fetched_at``），原子写，TTL 默认 30 天，总容量超出时按最旧
  淘汰（默认 256 MB）；内存 LRU 64 项 / 32 MB；
- ``ETag = sha256(url)[:32]``，由路由比较 ``If-None-Match`` 决定是否 304。
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
import json
import logging
import math
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any
from urllib.parse import urlsplit

import httpx2

from .errors import (
    LenovoError,
    LenovoProtocolError,
    LenovoTimeoutError,
    LenovoUnavailableError,
)

logger = logging.getLogger("winappsmanager.lenovo")

MEDIA_SCHEMA_VERSION = 1
DEFAULT_IMAGE_CACHE_TTL_SECONDS = 2592000.0
DEFAULT_IMAGE_CACHE_MAX_BYTES = 268435456
DEFAULT_MAX_IMAGE_BYTES = 10 * 1024 * 1024
DEFAULT_MEMORY_MAX_ENTRIES = 64
DEFAULT_MEMORY_MAX_BYTES = 32 * 1024 * 1024
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
ALLOWED_IMAGE_HOSTS = frozenset(
    {
        "lsw-fast.lenovo.com.cn",
        "imgs.lenovomm.cn",
        "pc1.gtimg.com",
        "pc-store-video.lenovomm.cn",
    }
)
ALLOWED_IMAGE_DOMAIN_SUFFIXES = (".lenovomm.cn", ".lenovo.com.cn")

_IMAGE_CONTENT_TYPES: dict[str, tuple[str, str]] = {
    "image/png": ("image/png", "png"),
    "image/jpeg": ("image/jpeg", "jpg"),
    "image/jpg": ("image/jpeg", "jpg"),
    "image/gif": ("image/gif", "gif"),
    "image/x-icon": ("image/x-icon", "ico"),
    "image/vnd.microsoft.icon": ("image/x-icon", "ico"),
    "image/ico": ("image/x-icon", "ico"),
    "image/icon": ("image/x-icon", "ico"),
    "image/bmp": ("image/bmp", "bmp"),
    "image/x-ms-bmp": ("image/bmp", "bmp"),
    "image/x-bmp": ("image/bmp", "bmp"),
}
_IMAGE_SUFFIXES: dict[str, tuple[str, str]] = {
    ".png": ("image/png", "png"),
    ".jpg": ("image/jpeg", "jpg"),
    ".jpeg": ("image/jpeg", "jpg"),
    ".gif": ("image/gif", "gif"),
    ".ico": ("image/x-icon", "ico"),
    ".bmp": ("image/bmp", "bmp"),
}
_GENERIC_CONTENT_TYPES = frozenset({"", "application/octet-stream", "binary/octet-stream"})
_IMAGE_EXTENSIONS = frozenset(extension for _, extension in _IMAGE_SUFFIXES.values()) | {"jpg"}
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ImagePayload:
    """一次图片响应：字节、实际 Content-Type 与稳定 ETag。"""

    content: bytes
    content_type: str
    etag: str


@dataclass(frozen=True)
class _MemoryEntry:
    payload: ImagePayload
    expires_at: float


@dataclass(frozen=True)
class _DiskEntry:
    media_path: Path
    sidecar_path: Path
    url: str
    content_type: str
    fetched_at: float
    size: int


class LenovoMediaCache:
    """图片拉取 + 内存/磁盘缓存。

    ``directory`` 是联想缓存根目录（``<cache_dir>/lenovo``），图片写在其中的 ``media/`` 下。
    """

    def __init__(
        self,
        client: httpx2.AsyncClient,
        directory: Path | str | None = None,
        *,
        ttl_seconds: float = DEFAULT_IMAGE_CACHE_TTL_SECONDS,
        max_bytes: int = DEFAULT_IMAGE_CACHE_MAX_BYTES,
        max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
        memory_max_entries: int = DEFAULT_MEMORY_MAX_ENTRIES,
        memory_max_bytes: int = DEFAULT_MEMORY_MAX_BYTES,
        cache_enabled: bool = True,
        cache_persist: bool = True,
        timeout_seconds: float = 20.0,
        connect_timeout_seconds: float = 5.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(client, httpx2.AsyncClient):
            raise ValueError("client must be an httpx2.AsyncClient")
        for name, value in (
            ("ttl_seconds", ttl_seconds),
            ("timeout_seconds", timeout_seconds),
            ("connect_timeout_seconds", connect_timeout_seconds),
        ):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a finite positive value")
        for name, value in (
            ("max_bytes", max_bytes),
            ("max_image_bytes", max_image_bytes),
            ("memory_max_entries", memory_max_entries),
            ("memory_max_bytes", memory_max_bytes),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if type(cache_enabled) is not bool or type(cache_persist) is not bool:
            raise ValueError("cache_enabled / cache_persist must be booleans")
        if not callable(clock):
            raise ValueError("clock must be callable")
        self._client = client
        self._ttl_seconds = float(ttl_seconds)
        self._max_bytes = max_bytes
        self._max_image_bytes = max_image_bytes
        self._memory_max_entries = memory_max_entries
        self._memory_max_bytes = memory_max_bytes
        self._cache_enabled = cache_enabled
        self._cache_persist = cache_persist
        self._timeout_seconds = float(timeout_seconds)
        self._connect_timeout_seconds = float(connect_timeout_seconds)
        self._clock = clock
        self._directory = _prepare_directory(directory)
        self._media_directory = self._directory / "media" if self._directory is not None else None
        self._memory: OrderedDict[str, _MemoryEntry] = OrderedDict()
        self._memory_bytes = 0
        self._disk_entries: dict[str, _DiskEntry] = {}
        self._disk_bytes = 0
        self._hits = 0
        self._disk_hits = 0
        self._misses = 0
        self._downloads = 0
        self._evicted = 0
        if self._cache_enabled and self._cache_persist:
            self._scan_disk()

    # -- 公开接口 ---------------------------------------------------------

    @property
    def counters(self) -> dict[str, int]:
        return {
            "hits": self._hits,
            "disk_hits": self._disk_hits,
            "misses": self._misses,
            "downloads": self._downloads,
            "evicted": self._evicted,
            "disk_entries": len(self._disk_entries),
            "disk_bytes": self._disk_bytes,
            "memory_entries": len(self._memory),
            "memory_bytes": self._memory_bytes,
        }

    @staticmethod
    def etag_for(url: str) -> str:
        return sha256(url.encode("utf-8")).hexdigest()[:32]

    async def fetch(self, url: str) -> ImagePayload:
        normalized = validate_media_url(url)
        key = sha256(normalized.encode("utf-8")).hexdigest()
        etag = key[:32]
        now = self._clock()

        if self._cache_enabled:
            memory_entry = self._memory.get(key)
            if memory_entry is not None and now < memory_entry.expires_at:
                self._hits += 1
                self._memory.move_to_end(key)
                return memory_entry.payload
            if self._cache_persist:
                payload = await self._read_disk(key, normalized, etag)
                if payload is not None:
                    self._disk_hits += 1
                    self._remember_memory(key, payload)
                    return payload
        self._misses += 1
        payload = await self._download(normalized, etag)
        if self._cache_enabled:
            self._remember_memory(key, payload)
            if self._cache_persist:
                extension = _extension_for_content_type(payload.content_type)
                if extension is not None and self._media_directory is not None:
                    await self._save_disk(key, normalized, payload, extension)
        return payload

    async def clear(self) -> int:
        """清空内存与磁盘图片缓存，返回删除的磁盘文件数。"""

        self._memory.clear()
        self._memory_bytes = 0
        self._disk_entries.clear()
        self._disk_bytes = 0
        if not self._cache_persist or self._media_directory is None:
            return 0
        try:
            return await asyncio.to_thread(self._delete_files_sync)
        except OSError:
            return 0

    # -- 上游下载 ---------------------------------------------------------

    async def _download(self, url: str, etag: str) -> ImagePayload:
        headers = {
            "User-Agent": BROWSER_USER_AGENT,
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        }
        try:
            response = await self._client.get(
                url,
                headers=headers,
                timeout=self._timeout(),
                follow_redirects=True,
            )
        except httpx2.TimeoutException:
            raise LenovoTimeoutError("联想图片请求超时。") from None
        except httpx2.HTTPError:
            raise LenovoUnavailableError("联想图片服务暂时无法连接。") from None

        # 跟随重定向后仍要保证最终地址在图片白名单内（防 SSRF 绕过）。
        final_url = str(response.url)
        if final_url != url:
            validate_media_url(final_url)
        if response.status_code != 200:
            raise LenovoUnavailableError(
                f"联想图片请求失败（HTTP {response.status_code}）。"
            )
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                declared = None
            if declared is not None and declared > self._max_image_bytes:
                raise LenovoProtocolError("联想图片超过单图大小上限。")
        content = response.content
        if not content:
            raise LenovoProtocolError("联想图片响应为空。")
        if len(content) > self._max_image_bytes:
            raise LenovoProtocolError("联想图片超过单图大小上限。")
        detected = detect_image_type(response.headers.get("content-type"), url)
        if detected is None:
            raise LenovoProtocolError("联想图片格式不受支持。")
        content_type, _extension = detected
        self._downloads += 1
        return ImagePayload(content=content, content_type=content_type, etag=etag)

    def _timeout(self) -> httpx2.Timeout:
        return httpx2.Timeout(self._timeout_seconds, connect=self._connect_timeout_seconds)

    # -- 内存缓存 ---------------------------------------------------------

    def _remember_memory(self, key: str, payload: ImagePayload) -> None:
        existing = self._memory.pop(key, None)
        if existing is not None:
            self._memory_bytes -= len(existing.payload.content)
        self._memory[key] = _MemoryEntry(
            payload=payload, expires_at=self._clock() + self._ttl_seconds
        )
        self._memory_bytes += len(payload.content)
        while (
            len(self._memory) > self._memory_max_entries
            or self._memory_bytes > self._memory_max_bytes
        ):
            if not self._memory:
                break
            _, evicted = self._memory.popitem(last=False)
            self._memory_bytes -= len(evicted.payload.content)
        self._memory_bytes = max(0, self._memory_bytes)

    # -- 磁盘缓存 ---------------------------------------------------------

    def _scan_disk(self) -> None:
        if self._media_directory is None:
            return
        try:
            entries = _scan_media_directory(self._media_directory)
        except OSError as error:
            logger.warning("无法读取联想图片缓存目录 %s: %s", self._media_directory, error)
            return
        self._disk_entries = entries
        self._disk_bytes = sum(entry.size for entry in entries.values())

    async def _read_disk(self, key: str, url: str, etag: str) -> ImagePayload | None:
        entry = self._disk_entries.get(key)
        if entry is None:
            return None
        if entry.url != url:
            self._disk_entries.pop(key, None)
            self._disk_bytes -= entry.size
            return None
        if self._clock() - entry.fetched_at > self._ttl_seconds:
            return None
        try:
            content = await asyncio.to_thread(entry.media_path.read_bytes)
        except OSError:
            self._disk_entries.pop(key, None)
            self._disk_bytes -= entry.size
            return None
        if not content:
            return None
        return ImagePayload(content=content, content_type=entry.content_type, etag=etag)

    async def _save_disk(
        self, key: str, url: str, payload: ImagePayload, extension: str
    ) -> None:
        if self._media_directory is None:
            return
        fetched_at = float(self._clock())
        try:
            size = await asyncio.to_thread(
                _write_media_entry_sync,
                self._media_directory,
                key,
                extension,
                url,
                payload,
                fetched_at,
            )
        except (OSError, TypeError, ValueError):
            logger.warning("写入联想图片缓存失败: %s", self._media_directory, exc_info=True)
            return
        previous = self._disk_entries.get(key)
        if previous is not None:
            self._disk_bytes -= previous.size
        self._disk_entries[key] = _DiskEntry(
            media_path=self._media_directory / f"{key}.{extension}",
            sidecar_path=self._media_directory / f"{key}.json",
            url=url,
            content_type=payload.content_type,
            fetched_at=fetched_at,
            size=size,
        )
        self._disk_bytes += size
        await self._evict_if_needed()

    async def _evict_if_needed(self) -> None:
        while self._disk_bytes > self._max_bytes and self._disk_entries:
            oldest_key = min(
                self._disk_entries,
                key=lambda item: (self._disk_entries[item].fetched_at, item),
            )
            entry = self._disk_entries.pop(oldest_key)
            self._disk_bytes -= entry.size
            try:
                await asyncio.to_thread(_delete_entry_sync, entry.media_path, entry.sidecar_path)
            except OSError:
                pass
            self._evicted += 1
        self._disk_bytes = max(0, self._disk_bytes)

    def _delete_files_sync(self) -> int:
        if self._media_directory is None or not self._media_directory.is_dir():
            return 0
        deleted = 0
        for path in list(self._media_directory.iterdir()):
            if path.is_file():
                try:
                    path.unlink()
                except FileNotFoundError:
                    continue
                except OSError:
                    continue
                deleted += 1
        return deleted


# ---------------------------------------------------------------------------
# 图片格式与 URL 校验辅助函数
# ---------------------------------------------------------------------------


def validate_media_url(url: Any) -> str:
    """校验图片 URL：http(s)、host 在白名单内、没有用户信息。"""

    if not isinstance(url, str):
        raise LenovoProtocolError("联想图片地址类型无效。")
    text = url.strip()
    if not text or any(ord(character) < 32 for character in text):
        raise LenovoProtocolError("联想图片地址无效。")
    try:
        parsed = urlsplit(text)
        parsed.port
    except ValueError:
        raise LenovoProtocolError("联想图片地址无效。") from None
    if parsed.scheme.casefold() not in ("http", "https") or not parsed.hostname:
        raise LenovoProtocolError("联想图片地址必须是 http(s) URL。")
    if parsed.username is not None or parsed.password is not None:
        raise LenovoProtocolError("联想图片地址不允许包含用户信息。")
    host = parsed.hostname.casefold()
    if not _host_allowed(host):
        raise LenovoProtocolError(f"联想图片地址不在允许的域名内: {host}")
    return text


def _host_allowed(host: str) -> bool:
    if host in ALLOWED_IMAGE_HOSTS:
        return True
    return any(host.endswith(suffix) and len(host) > len(suffix) for suffix in ALLOWED_IMAGE_DOMAIN_SUFFIXES)


def detect_image_type(content_type: Any, url: str) -> tuple[str, str] | None:
    """返回 ``(规范 Content-Type, 扩展名)``；无法识别时返回 ``None``。"""

    normalized = ""
    if isinstance(content_type, str):
        normalized = content_type.split(";", 1)[0].strip().casefold()
    if normalized and normalized not in _GENERIC_CONTENT_TYPES:
        return _IMAGE_CONTENT_TYPES.get(normalized)
    try:
        path = urlsplit(url).path
    except ValueError:
        return None
    suffix = Path(path).suffix.casefold()
    return _IMAGE_SUFFIXES.get(suffix)


def _extension_for_content_type(content_type: str) -> str | None:
    for candidate, extension in _IMAGE_CONTENT_TYPES.values():
        if candidate == content_type:
            return extension
    return None


def _scan_media_directory(directory: Path) -> dict[str, _DiskEntry]:
    if not directory.is_dir():
        return {}
    entries: dict[str, _DiskEntry] = {}
    for sidecar in list(directory.glob("*.json")):
        key = sidecar.stem
        if _SHA256_HEX.fullmatch(key) is None:
            _unlink_quietly(sidecar)
            continue
        entry = _read_sidecar(sidecar, key, directory)
        if entry is None:
            _unlink_quietly(sidecar)
            continue
        entries[key] = entry
    known_media = {entry.media_path.name for entry in entries.values()}
    for path in list(directory.iterdir()):
        if not path.is_file():
            continue
        name = path.name
        if name in known_media or name.endswith(".json"):
            continue
        if name.startswith(".") and name.endswith(".tmp"):
            _unlink_quietly(path)
            continue
        suffix = path.suffix.casefold().lstrip(".")
        if suffix in _IMAGE_EXTENSIONS:
            # sidecar 丢失的孤儿图片文件：无法判断 TTL，直接清理。
            _unlink_quietly(path)
    return entries


def _read_sidecar(sidecar: Path, key: str, directory: Path) -> _DiskEntry | None:
    try:
        raw = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or raw.get("schema_version") != MEDIA_SCHEMA_VERSION:
        return None
    url = raw.get("url")
    content_type = raw.get("content_type")
    fetched_at = raw.get("fetched_at")
    if not isinstance(url, str) or not url:
        return None
    if not isinstance(content_type, str) or not content_type:
        return None
    if type(fetched_at) not in (int, float) or not math.isfinite(float(fetched_at)):
        return None
    detected = detect_image_type(content_type, url)
    if detected is None:
        return None
    _normalized, extension = detected
    media_path = directory / f"{key}.{extension}"
    try:
        size = media_path.stat().st_size
    except OSError:
        return None
    if size <= 0:
        return None
    return _DiskEntry(
        media_path=media_path,
        sidecar_path=sidecar,
        url=url,
        content_type=content_type,
        fetched_at=float(fetched_at),
        size=size,
    )


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _prepare_directory(directory: Path | str | None) -> Path | None:
    if directory is None:
        return None
    try:
        text = os.fspath(directory)
    except TypeError as error:
        raise ValueError("directory must be a filesystem path") from error
    if not str(text).strip():
        raise ValueError("directory must be a non-empty path")
    return Path(text).expanduser().resolve()


def _write_media_entry_sync(
    directory: Path,
    key: str,
    extension: str,
    url: str,
    payload: ImagePayload,
    fetched_at: float,
) -> int:
    directory.mkdir(parents=True, exist_ok=True)
    media_path = directory / f"{key}.{extension}"
    _atomic_write_bytes(media_path, payload.content)
    sidecar_path = directory / f"{key}.json"
    document = {
        "schema_version": MEDIA_SCHEMA_VERSION,
        "url": url,
        "content_type": payload.content_type,
        "fetched_at": fetched_at,
        "size": len(payload.content),
    }
    _atomic_write_text(sidecar_path, json.dumps(document, ensure_ascii=False, separators=(",", ":")))
    return len(payload.content)


def _delete_entry_sync(media_path: Path, sidecar_path: Path) -> None:
    _unlink_quietly(media_path)
    _unlink_quietly(sidecar_path)


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
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


def _atomic_write_text(path: Path, text: str) -> None:
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
    "ALLOWED_IMAGE_DOMAIN_SUFFIXES",
    "ALLOWED_IMAGE_HOSTS",
    "BROWSER_USER_AGENT",
    "DEFAULT_IMAGE_CACHE_MAX_BYTES",
    "DEFAULT_IMAGE_CACHE_TTL_SECONDS",
    "ImagePayload",
    "LenovoMediaCache",
    "detect_image_type",
    "validate_media_url",
]
