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


UPDATE_API = "https://update.code.visualstudio.com/api/update/{platform}/stable/latest"
WINDOWS_PLATFORMS = {"x64": "win32-x64-user", "arm64": "win32-arm64-user"}
_VERSION_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
_DOWNLOAD_HOSTS = {"vscode.download.prss.microsoft.com", "az764295.vo.msecnd.net"}


class VSCodeReleaseProvider(ReleaseProvider):
    """Windows User Installer metadata from Microsoft's stable update API."""

    id = "vscode"

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
            results = await asyncio.gather(*(self._fetch_platform(architecture, platform)
                                           for architecture, platform in WINDOWS_PLATFORMS.items()))
            releases = [release for release in results if release is not None]
            if not releases:
                raise ProviderProtocolError("VS Code 官方服务没有提供可用的 Windows 安装包。")
            self._cache = tuple(releases)
            self._cache_expires_at = monotonic() + self._cache_ttl
            return self._copy_releases()

    async def resolve_download(self, release: Release) -> Release:
        if not isinstance(release, Release):
            raise ProviderProtocolError("VS Code 版本资源类型无效。")
        known = next((item for item in await self.get_releases() if item.id == release.id), None)
        if known is None:
            raise ProviderReleaseChangedError("VS Code 官方版本已更新，请刷新版本资料。")
        if release.model_dump(mode="json") != known.model_dump(mode="json"):
            raise ProviderProtocolError("VS Code 版本资源与官方元数据不一致。")
        return known.model_copy(deep=True)

    def _copy_releases(self) -> list[Release]:
        return [release.model_copy(deep=True) for release in self._cache]

    async def _fetch_platform(self, architecture: str, platform: str) -> Release | None:
        try:
            response = await self._client.get(UPDATE_API.format(platform=platform), timeout=15.0, follow_redirects=False)
            if response.status_code in {204, 404}:
                return None
            response.raise_for_status()
        except httpx2.TimeoutException:
            raise ProviderTimeoutError("VS Code 官方版本请求超时。") from None
        except httpx2.HTTPStatusError as error:
            raise ProviderUnavailableError(f"VS Code 官方版本请求失败（HTTP {error.response.status_code}）。") from None
        except httpx2.HTTPError:
            raise ProviderUnavailableError("VS Code 官方版本服务暂时无法连接。") from None
        try:
            payload = response.json()
        except (ValueError, UnicodeError):
            raise ProviderProtocolError("VS Code 官方版本响应不是有效的 JSON。") from None
        return self._parse_platform(payload, architecture)

    @staticmethod
    def _parse_platform(payload, architecture: str) -> Release:
        if not isinstance(payload, dict):
            raise ProviderProtocolError("VS Code 官方版本响应结构无效。")
        # The update API's `version` is a Git commit; productVersion is the public release version.
        version = payload.get("productVersion")
        if not isinstance(version, str) or len(version) > 50 or not _VERSION_PATTERN.fullmatch(version):
            raise ProviderProtocolError("VS Code 官方响应缺少有效的产品版本号。")
        value = payload.get("url")
        if (not isinstance(value, str) or not value or value != value.strip()
                or any(ord(character) < 32 for character in value)
                or re.search(r"%(?![0-9a-fA-F]{2})", value)):
            raise ProviderProtocolError("VS Code 官方安装包地址无效。")
        try:
            parsed = urlsplit(value)
            if (parsed.scheme.lower() != "https" or parsed.hostname not in _DOWNLOAD_HOSTS
                    or parsed.username is not None or parsed.password is not None or parsed.fragment):
                raise ValueError("invalid installer URL")
            parsed.port
            url = HttpUrl(value)
            file_name = unquote(parsed.path.rsplit("/", 1)[-1], encoding="utf-8", errors="strict")
        except (ValueError, UnicodeError, ValidationError):
            raise ProviderProtocolError("VS Code 官方安装包地址无效。") from None
        expected_name = f"VSCodeUserSetup-{architecture}-{version}.exe"
        if file_name != expected_name or PureWindowsPath(file_name).name != file_name:
            raise ProviderProtocolError("VS Code 安装包文件名与产品版本或架构不一致。")

        sha256 = payload.get("sha256hash")
        if sha256 is not None and (not isinstance(sha256, str) or not _SHA256_PATTERN.fullmatch(sha256)):
            raise ProviderProtocolError("VS Code 官方 SHA256 校验值无效。")
        size = payload.get("size")
        if size is not None and (type(size) is not int or size <= 0):
            raise ProviderProtocolError("VS Code 官方安装包大小无效。")
        try:
            return Release(
                id=f"{version}-windows-{architecture}", version=version, platform="windows",
                architecture=architecture, download_url=url, file_name=file_name,
                size_bytes=size, sha256=sha256,
            )
        except ValidationError:
            raise ProviderProtocolError("VS Code 官方响应包含无效的版本资源。") from None
