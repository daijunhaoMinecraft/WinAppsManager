import asyncio
import math
import re
from time import monotonic
from urllib.parse import unquote, urlencode, urlsplit

import httpx2
from pydantic import HttpUrl, ValidationError

from .base import ProviderProtocolError, ProviderReleaseChangedError, ProviderTimeoutError, ProviderUnavailableError, ReleaseProvider

if "." in __package__:
    from ..models import Release
else:
    from models import Release


VERSIONS_URL = "https://product-details.mozilla.org/1.0/firefox_versions.json"
DOWNLOAD_ENDPOINT = "https://download.mozilla.org/"
CHECKSUM_URL = "https://archive.mozilla.org/pub/firefox/releases/{version}/SHA256SUMS"
WINDOWS_PLATFORMS = {"x64": ("win64", "win64"), "x86": ("win", "win32"), "arm64": ("win64-aarch64", "win64-aarch64")}
LOCALE = "zh-CN"
_VERSION_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
_DOWNLOAD_HOSTS = {"download-installer.cdn.mozilla.net", "archive.mozilla.org"}


class FirefoxReleaseProvider(ReleaseProvider):
    """Mozilla stable Windows installers, with official zh-CN packages and checksums."""

    id = "firefox"

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
            response = await self._request(VERSIONS_URL, operation="版本")
            try:
                payload = response.json()
            except (ValueError, UnicodeError):
                raise ProviderProtocolError("Firefox 官方版本响应不是有效的 JSON。") from None
            if not isinstance(payload, dict):
                raise ProviderProtocolError("Firefox 官方版本响应结构无效。")
            version = payload.get("LATEST_FIREFOX_VERSION")
            if not isinstance(version, str) or len(version) > 50 or not _VERSION_PATTERN.fullmatch(version):
                raise ProviderProtocolError("Firefox 官方响应缺少有效的稳定版版本号。")
            checksum_response = await self._request(CHECKSUM_URL.format(version=version), operation="校验", optional=True)
            checksums = self._parse_checksums(checksum_response.text, version) if checksum_response is not None else None
            results = await asyncio.gather(*(self._resolve_platform(version, architecture, platform, directory, checksums)
                                           for architecture, (platform, directory) in WINDOWS_PLATFORMS.items()))
            releases = [release for release in results if release is not None]
            if not releases:
                raise ProviderProtocolError("Firefox 官方服务没有提供可用的 Windows 安装包。")
            self._cache = tuple(releases)
            self._cache_expires_at = monotonic() + self._cache_ttl
            return self._copy_releases()

    async def resolve_download(self, release: Release) -> Release:
        if not isinstance(release, Release):
            raise ProviderProtocolError("Firefox 版本资源类型无效。")
        known = next((item for item in await self.get_releases() if item.id == release.id), None)
        if known is None:
            raise ProviderReleaseChangedError("Firefox 官方版本已更新，请刷新版本资料。")
        if release.model_dump(mode="json") != known.model_dump(mode="json"):
            raise ProviderProtocolError("Firefox 版本资源与官方元数据不一致。")
        return known.model_copy(deep=True)

    def _copy_releases(self) -> list[Release]:
        return [release.model_copy(deep=True) for release in self._cache]

    async def _request(self, url: str, *, operation: str, optional=False, redirect=False):
        try:
            response = await self._client.get(url, timeout=15.0, follow_redirects=False)
            if optional and response.status_code in {204, 404}:
                return None
            if redirect and response.status_code in {301, 302, 303, 307, 308}:
                return response
            response.raise_for_status()
        except httpx2.TimeoutException:
            raise ProviderTimeoutError(f"Firefox 官方{operation}请求超时。") from None
        except httpx2.HTTPStatusError as error:
            raise ProviderUnavailableError(f"Firefox 官方{operation}请求失败（HTTP {error.response.status_code}）。") from None
        except httpx2.HTTPError:
            raise ProviderUnavailableError(f"Firefox 官方{operation}服务暂时无法连接。") from None
        return response

    @staticmethod
    def _parse_checksums(text: str, version: str) -> dict[str, str]:
        targets = {f"{directory}/{LOCALE}/Firefox Setup {version}.exe" for _, directory in WINDOWS_PLATFORMS.values()}
        hashes = {}
        for line in text.splitlines():
            fields = line.split(maxsplit=1)
            if len(fields) != 2:
                continue
            digest, path = fields
            path = path.lstrip("*").strip()
            if path not in targets:
                continue
            if not _SHA256_PATTERN.fullmatch(digest) or path in hashes:
                raise ProviderProtocolError("Firefox 官方 SHA256 校验清单无效。")
            hashes[path] = digest
        if not hashes:
            raise ProviderProtocolError("Firefox 官方 SHA256 清单缺少对应版本的 Windows 安装包。")
        return hashes

    async def _resolve_platform(self, version, architecture, platform, directory, checksums):
        query = urlencode({"product": f"firefox-{version}-ssl", "os": platform, "lang": LOCALE})
        response = await self._request(f"{DOWNLOAD_ENDPOINT}?{query}", operation="安装包", optional=True, redirect=True)
        if response is None:
            return None
        location = response.headers.get("location")
        if response.status_code not in {301, 302, 303, 307, 308} or not isinstance(location, str) or not location:
            raise ProviderProtocolError("Firefox 官方下载服务没有返回有效的安装包重定向。")
        relative_path = f"{directory}/{LOCALE}/Firefox Setup {version}.exe"
        if checksums is not None and relative_path not in checksums:
            raise ProviderProtocolError("Firefox 官方 SHA256 清单缺少对应架构的安装包。")
        expected_path = f"/pub/firefox/releases/{version}/{relative_path}"
        try:
            parsed = urlsplit(location)
            if (parsed.scheme.lower() != "https" or parsed.hostname not in _DOWNLOAD_HOSTS
                    or parsed.username is not None or parsed.password is not None or parsed.fragment
                    or any(ord(character) < 32 for character in location)
                    or re.search(r"%(?![0-9a-fA-F]{2})", location)):
                raise ValueError("invalid installer URL")
            parsed.port
            if unquote(parsed.path, encoding="utf-8", errors="strict") != expected_path:
                raise ValueError("installer does not match version, architecture and locale")
            url = HttpUrl(location)
        except (ValueError, UnicodeError, ValidationError):
            raise ProviderProtocolError("Firefox 官方安装包地址与版本、架构或语言不一致。") from None
        try:
            return Release(
                id=f"{version}-windows-{architecture}", version=version, platform="windows",
                architecture=architecture, download_url=url, file_name=f"Firefox Setup {version}.exe",
                size_bytes=None, sha256=checksums.get(relative_path) if checksums is not None else None,
            )
        except ValidationError:
            raise ProviderProtocolError("Firefox 官方响应包含无效的版本资源。") from None
