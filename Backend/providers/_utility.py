import asyncio
from abc import abstractmethod
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


class UtilityReleaseProvider(ReleaseProvider):
    """Shared transport/cache for the utility providers in this module group."""

    display_name: str

    def __init__(self, client: httpx2.AsyncClient, *, cache_ttl: float = 60.0) -> None:
        if not math.isfinite(cache_ttl) or cache_ttl < 0:
            raise ValueError("cache_ttl must be a finite non-negative value")
        self._client = client
        self._cache_ttl = cache_ttl
        self._cache: tuple[Release, ...] = ()
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def get_releases(self, *, force_refresh: bool = False) -> list[Release]:
        async with self._lock:
            if not force_refresh and self._cache and monotonic() < self._expires_at:
                return [release.model_copy(deep=True) for release in self._cache]
            releases = await self._load_releases()
            if not releases or len({release.id for release in releases}) != len(releases):
                raise ProviderProtocolError(f"{self.display_name} 没有有效且唯一的 Windows 安装包。")
            self._cache = tuple(releases)
            self._expires_at = monotonic() + self._cache_ttl
            return [release.model_copy(deep=True) for release in self._cache]

    async def resolve_download(self, release: Release) -> Release:
        if not isinstance(release, Release):
            raise ProviderProtocolError(f"{self.display_name} 版本资源类型无效。")
        releases = await self.get_releases()
        current = next((item for item in releases if item.id == release.id), None)
        if current is None:
            raise ProviderReleaseChangedError(f"{self.display_name} 官方版本已更新，请刷新版本资料。")
        if current.model_dump(mode="json") != release.model_dump(mode="json"):
            raise ProviderProtocolError(f"{self.display_name} 版本资源与官方资料不一致。")
        return current.model_copy(deep=True)

    async def _get(self, url: str, **kwargs) -> httpx2.Response:
        try:
            response = await self._client.get(url, follow_redirects=True, **kwargs)
            response.raise_for_status()
            return response
        except httpx2.TimeoutException:
            raise ProviderTimeoutError(f"{self.display_name} 官方资料请求超时。") from None
        except httpx2.HTTPStatusError as error:
            raise ProviderUnavailableError(
                f"{self.display_name} 官方资料请求失败（HTTP {error.response.status_code}）。"
            ) from None
        except httpx2.HTTPError:
            raise ProviderUnavailableError(f"{self.display_name} 官方资料暂时无法连接。") from None

    @abstractmethod
    async def _load_releases(self) -> list[Release]:
        pass


def official_executable_url(
    value,
    *,
    display_name: str,
    hosts: set[str],
    path_prefixes: tuple[str, ...] = (),
    suffixes: tuple[str, ...] = (".exe",),
    case_insensitive_path: bool = False,
) -> tuple[HttpUrl, str]:
    """Validate an official installer URL and return ``(url, file_name)``.

    ``case_insensitive_path`` exists for GitHub: the release page URL keeps the repository's
    canonical casing (``VSCodium/vscodium``) while a catalog entry may be written with any
    casing, and GitHub download paths are effectively case-insensitive.
    """

    def path_matches(path: str) -> bool:
        if not path_prefixes:
            return True
        if case_insensitive_path:
            lowered = path.lower()
            return any(lowered.startswith(prefix.lower()) for prefix in path_prefixes)
        return any(path.startswith(prefix) for prefix in path_prefixes)

    if not isinstance(value, str) or not value or value != value.strip() or any(ord(char) < 32 for char in value):
        raise ProviderProtocolError(f"{display_name} 返回了无效的安装包地址。")
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in hosts
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or re.search(r"%(?![0-9a-fA-F]{2})", value)
            or not path_matches(parsed.path)
        ):
            raise ValueError("invalid official URL")
        url = HttpUrl(value)
        canonical = urlsplit(str(url))
        if not path_matches(canonical.path):
            raise ValueError("normalized URL left the official release path")
        file_name = unquote(parsed.path.rsplit("/", 1)[-1], encoding="utf-8", errors="strict")
        invalid = '<>:"/\\|?*'
        reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
        if (
            not file_name.lower().endswith(tuple(suffixes))
            or file_name != file_name.strip()
            or PureWindowsPath(file_name).name != file_name
            or any(char in invalid or ord(char) < 32 for char in file_name)
            or file_name.split(".", 1)[0].upper() in reserved
        ):
            raise ValueError("invalid executable file name")
        return url, file_name
    except (ValueError, UnicodeError, ValidationError):
        raise ProviderProtocolError(f"{display_name} 返回了无效的官方安装包地址或文件名。") from None
