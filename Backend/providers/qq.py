import asyncio
import math
from pathlib import PureWindowsPath
import re
from time import monotonic
from urllib.parse import unquote, urlsplit

import httpx2
from pydantic import HttpUrl, ValidationError

from .base import ProviderProtocolError, ProviderReleaseChangedError, ProviderTimeoutError, ProviderUnavailableError, ReleaseProvider

if "." in __package__:
    from ..models import Release
else:
    from models import Release


CONFIG_URL = "https://qq-web.cdn-go.cn/im.qq.com_new/latest/rainbow/pcConfig.json"
SIGN_URL = "https://im.qq.com/http2rpc/gotrpc/noauth/trpc.qqntv2.urlsign.UrlSign/GetSign"
OIDB_HEADER = '{"uint32_command":"0x9b8e","uint32_service_type":1}'
NT_DOWNLOAD_FIELDS = {
    "x64": "ntDownloadX64Url",
    "x86": "ntDownloadUrl",
    "arm64": "ntDownloadARMUrl",
}
_VERSION_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}$")
_RESERVED_FILE_STEMS = {"CON", "PRN", "AUX", "NUL", *(f"COM{index}" for index in range(1, 10)), *(f"LPT{index}" for index in range(1, 10))}


class QQReleaseProvider(ReleaseProvider):
    id = "qq"

    def __init__(self, client: httpx2.AsyncClient, *, cache_ttl: float = 60.0) -> None:
        if not math.isfinite(cache_ttl) or cache_ttl < 0:
            raise ValueError("cache_ttl must be a finite non-negative value")
        self._client = client
        self._cache_ttl = cache_ttl
        self._cache: tuple[Release, ...] = ()
        self._cache_expires_at = 0.0
        self._refresh_lock = asyncio.Lock()

    async def get_releases(self, *, force_refresh: bool = False) -> list[Release]:
        async with self._refresh_lock:
            if not force_refresh and self._cache and monotonic() < self._cache_expires_at:
                return self._copy_releases()

            payload = await self._request_json("GET", CONFIG_URL, operation="配置")
            releases = self._parse_config(payload)
            self._cache = tuple(releases)
            self._cache_expires_at = monotonic() + self._cache_ttl
            return self._copy_releases()

    async def resolve_download(self, release: Release) -> Release:
        if not isinstance(release, Release):
            raise ProviderProtocolError("QQ 版本资源类型无效。")

        known_releases = await self.get_releases()
        known_release = next((item for item in known_releases if item.id == release.id), None)
        if known_release is None:
            raise ProviderReleaseChangedError("QQ 官方版本已更新，请刷新版本资料。")
        if release.model_dump(mode="json") != known_release.model_dump(mode="json"):
            raise ProviderProtocolError("QQ 版本资源与当前官方配置不一致，请刷新软件详情。")

        payload = await self._request_json(
            "POST",
            SIGN_URL,
            operation="下载签名",
            json={"url": str(known_release.download_url)},
            headers={"x-oidb": OIDB_HEADER},
        )
        self._validate_sign_result(payload)
        signed_url, file_name = self._validate_download_url(payload["data"]["url"], operation="下载签名")
        unsigned = urlsplit(str(known_release.download_url))
        signed = urlsplit(str(signed_url))
        if (
            signed.hostname != unsigned.hostname
            or signed.port != unsigned.port
            or signed.path != unsigned.path
            or file_name != known_release.file_name
        ):
            raise ProviderProtocolError("QQ 下载签名指向了不同的安装包。")

        sign_values = []
        for parameter in signed.query.split("&"):
            name, _, value = parameter.partition("=")
            if unquote(name) == "sign":
                sign_values.append(value)
        if len(sign_values) != 1 or not sign_values[0]:
            raise ProviderProtocolError("QQ 下载签名响应缺少唯一且非空的 sign 参数。")

        try:
            return Release.model_validate({**known_release.model_dump(), "download_url": signed_url})
        except ValidationError:
            raise ProviderProtocolError("QQ 下载签名返回了无效的版本资源。") from None

    def _copy_releases(self) -> list[Release]:
        return [release.model_copy(deep=True) for release in self._cache]

    async def _request_json(self, method: str, url: str, *, operation: str, **kwargs) -> dict:
        try:
            response = await self._client.request(method, url, timeout=15.0, follow_redirects=False, **kwargs)
            response.raise_for_status()
        except httpx2.TimeoutException:
            raise ProviderTimeoutError(f"QQ {operation}请求超时。") from None
        except httpx2.HTTPStatusError as exception:
            raise ProviderUnavailableError(f"QQ {operation}请求失败（HTTP {exception.response.status_code}）。") from None
        except httpx2.HTTPError:
            raise ProviderUnavailableError(f"QQ {operation}服务暂时无法连接。") from None

        try:
            payload = response.json()
        except (ValueError, UnicodeError):
            raise ProviderProtocolError(f"QQ {operation}响应不是有效的 JSON。") from None
        if not isinstance(payload, dict):
            raise ProviderProtocolError(f"QQ {operation}响应结构无效。")
        return payload

    @classmethod
    def _parse_config(cls, payload: dict) -> list[Release]:
        windows = payload.get("Windows")
        if not isinstance(windows, dict):
            raise ProviderProtocolError("QQ 配置中缺少 Windows 软件资料。")

        version = windows.get("version")
        if not isinstance(version, str) or len(version.strip()) > 50 or not _VERSION_PATTERN.fullmatch(version.strip()):
            raise ProviderProtocolError("QQ 配置中的 Windows 版本号无效。")
        version = version.strip()

        releases = []
        for architecture, field in NT_DOWNLOAD_FIELDS.items():
            value = windows.get(field)
            if value is None or value == "":
                continue
            url, file_name = cls._validate_download_url(value, operation="配置")
            try:
                releases.append(Release(
                    id=f"{version}-windows-{architecture}",
                    version=version,
                    platform="windows",
                    architecture=architecture,
                    download_url=url,
                    file_name=file_name,
                    size_bytes=None,
                    sha256=None,
                ))
            except ValidationError:
                raise ProviderProtocolError("QQ 配置包含无效的版本资源。") from None

        if not releases:
            raise ProviderProtocolError("QQ 配置中没有可用的 Windows NT 安装包。")
        return releases

    @staticmethod
    def _validate_download_url(value, *, operation: str) -> tuple[HttpUrl, str]:
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or any(ord(character) < 32 for character in value)
            or re.search(r"%(?![0-9a-fA-F]{2})", value)
        ):
            raise ProviderProtocolError(f"QQ {operation}返回了无效的安装包地址。")
        try:
            parsed = urlsplit(value)
            if (
                parsed.scheme.lower() != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.fragment
            ):
                raise ValueError("invalid download URL")
            # Validate ports and the HTTP URL before decoding its file name.
            parsed.port
            url = HttpUrl(value)
            file_name = unquote(parsed.path.rsplit("/", 1)[-1], encoding="utf-8", errors="strict")
        except (ValueError, UnicodeError, ValidationError):
            raise ProviderProtocolError(f"QQ {operation}返回了无效的安装包地址。") from None

        invalid_characters = '<>:"/\\|?*'
        if (
            not file_name
            or file_name != file_name.strip()
            or not file_name.lower().endswith(".exe")
            or PureWindowsPath(file_name).name != file_name
            or any(character in invalid_characters or ord(character) < 32 for character in file_name)
            or file_name.endswith((".", " "))
            or file_name.split(".", 1)[0].upper() in _RESERVED_FILE_STEMS
        ):
            raise ProviderProtocolError(f"QQ {operation}返回了无效的安装包文件名。")
        return url, file_name

    @staticmethod
    def _validate_sign_result(payload: dict) -> None:
        has_success_code = False
        if "retcode" in payload:
            if type(payload["retcode"]) is not int or payload["retcode"] != 0:
                raise ProviderProtocolError("QQ 下载签名被上游拒绝。")
            has_success_code = True
        if "error" in payload:
            error = payload["error"]
            if not isinstance(error, dict) or type(error.get("code")) is not int or error["code"] != 0:
                raise ProviderProtocolError("QQ 下载签名被上游拒绝。")
            has_success_code = True
        if not has_success_code:
            raise ProviderProtocolError("QQ 下载签名响应缺少明确的成功状态。")
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("url"), str) or not data["url"]:
            raise ProviderProtocolError("QQ 下载签名响应缺少安装包地址。")
