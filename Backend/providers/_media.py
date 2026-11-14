import asyncio
import math
from pathlib import PureWindowsPath
import re
from time import monotonic
from urllib.parse import unquote, urlsplit

import httpx2
from pydantic import HttpUrl, ValidationError

from .base import (
    ProviderProtocolError,
    ProviderReleaseChangedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    ReleaseProvider,
)

if "." in __package__:
    from ..models import Release
else:
    from models import Release


_RESERVED_FILE_STEMS = {"CON", "PRN", "AUX", "NUL", *(f"COM{index}" for index in range(1, 10)),
                        *(f"LPT{index}" for index in range(1, 10))}


class CachedMediaProvider(ReleaseProvider):
    display_name: str

    def __init__(self, client: httpx2.AsyncClient, *, cache_ttl: float = 60.0) -> None:
        if not math.isfinite(cache_ttl) or cache_ttl < 0:
            raise ValueError("cache_ttl must be finite and non-negative")
        self._client = client
        self._cache_ttl = cache_ttl
        self._cache: tuple[Release, ...] = ()
        self._cache_expires_at = 0.0
        self._refresh_lock = asyncio.Lock()

    async def get_releases(self, *, force_refresh: bool = False) -> list[Release]:
        async with self._refresh_lock:
            if not force_refresh and self._cache and monotonic() < self._cache_expires_at:
                return [release.model_copy(deep=True) for release in self._cache]
            releases = await self._load_releases()
            if not releases or len({release.architecture for release in releases}) != len(releases):
                raise ProviderProtocolError(f"{self.display_name} 没有有效且唯一的 Windows 安装包资料。")
            self._cache = tuple(release.model_copy(deep=True) for release in releases)
            self._cache_expires_at = monotonic() + self._cache_ttl
            return [release.model_copy(deep=True) for release in self._cache]

    async def resolve_download(self, release: Release) -> Release:
        if not isinstance(release, Release):
            raise ProviderProtocolError(f"{self.display_name} 版本资料类型无效。")
        known = next((item for item in await self.get_releases() if item.id == release.id), None)
        if known is None:
            raise ProviderReleaseChangedError(f"{self.display_name} 官方版本已更新，请刷新版本资料。")
        if known.model_dump(mode="json") != release.model_dump(mode="json"):
            raise ProviderProtocolError(f"{self.display_name} 版本资料与官方来源不一致。")
        return known.model_copy(deep=True)

    async def _load_releases(self) -> list[Release]:
        raise NotImplementedError

    async def _response(self, url: str) -> httpx2.Response:
        try:
            response = await self._client.get(url, timeout=5.0, follow_redirects=True)
            response.raise_for_status()
            return response
        except httpx2.TimeoutException:
            raise ProviderTimeoutError(f"{self.display_name} 官方下载资料请求超时。") from None
        except httpx2.HTTPStatusError as error:
            raise ProviderUnavailableError(
                f"{self.display_name} 官方下载资料请求失败（HTTP {error.response.status_code}）。"
            ) from None
        except httpx2.HTTPError:
            raise ProviderUnavailableError(f"{self.display_name} 官方下载服务暂时无法连接。") from None

    async def _json(self, url: str):
        response = await self._response(url)
        try:
            return response.json()
        except (ValueError, UnicodeError):
            raise ProviderProtocolError(f"{self.display_name} 官方下载资料不是有效的 JSON。") from None

    def _installer_url(self, value, *, allowed_hosts: set[str], path_prefix: str | None = None) -> tuple[HttpUrl, str]:
        if (not isinstance(value, str) or not value or value != value.strip()
                or any(ord(char) < 32 for char in value) or re.search(r"%(?![0-9a-fA-F]{2})", value)):
            raise ProviderProtocolError(f"{self.display_name} 安装包地址无效。")
        try:
            parsed = urlsplit(value)
            if (
                parsed.scheme != "https"
                or parsed.hostname not in allowed_hosts
                or parsed.port not in {None, 443}
                or parsed.username is not None
                or parsed.password is not None
                or parsed.fragment
                or (path_prefix is not None and not parsed.path.startswith(path_prefix))
            ):
                raise ValueError
            url = HttpUrl(value)
            normalized_path = urlsplit(str(url)).path
            if path_prefix is not None and not normalized_path.startswith(path_prefix):
                raise ValueError
            filename = unquote(parsed.path.rsplit("/", 1)[-1], encoding="utf-8", errors="strict")
            if (
                not filename
                or filename != filename.strip()
                or PureWindowsPath(filename).name != filename
                or any(char in '<>:"/\\|?*' or ord(char) < 32 for char in filename)
                or not filename.lower().endswith(".exe")
                or filename.endswith((".", " "))
                or filename.split(".", 1)[0].upper() in _RESERVED_FILE_STEMS
            ):
                raise ValueError
        except (ValueError, UnicodeError, ValidationError):
            raise ProviderProtocolError(f"{self.display_name} 安装包地址或文件名无效。") from None
        return url, filename

    def _release(self, *, version: str, architecture: str, url, filename: str,
                 size_bytes: int | None = None, sha256: str | None = None) -> Release:
        try:
            return Release(
                id=f"{version}-windows-{architecture}",
                version=version,
                platform="windows",
                architecture=architecture,
                download_url=url,
                file_name=filename,
                size_bytes=size_bytes,
                sha256=sha256,
            )
        except ValidationError:
            raise ProviderProtocolError(f"{self.display_name} 官方版本或安装包资料无效。") from None
