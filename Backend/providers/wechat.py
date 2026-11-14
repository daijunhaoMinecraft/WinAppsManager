import asyncio
import math
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import PureWindowsPath
from time import monotonic
from urllib.parse import unquote, urlsplit

import httpx2
from pydantic import ValidationError

from .base import ProviderProtocolError, ProviderReleaseChangedError, ProviderTimeoutError, ProviderUnavailableError, ReleaseProvider

if "." in __package__:
    from ..models import Release
else:
    from models import Release


DOWNLOAD_PAGE = "https://pc.weixin.qq.com/"
_VERSION = re.compile(r"\b([0-9]+(?:\.[0-9]+){1,3})\b")
_VOID_ELEMENTS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_RESERVED_STEMS = {"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)), *(f"LPT{n}" for n in range(1, 10))}


@dataclass
class _DownloadAnchor:
    href: str
    context: tuple[str, ...]
    text: str = ""


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._stack: list[tuple[str, str]] = []
        self._anchor: _DownloadAnchor | None = None
        self.downloads: list[_DownloadAnchor] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "a" and "download-button" in (attributes.get("class") or "").split():
            self._anchor = _DownloadAnchor(attributes.get("href") or "", tuple(item[1] for item in self._stack))
        if tag not in _VOID_ELEMENTS:
            self._stack.append((tag, attributes.get("id") or ""))

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._anchor is not None:
            self.downloads.append(self._anchor)
            self._anchor = None
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                del self._stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if self._anchor is not None:
            self._anchor.text += " " + data


class WeChatReleaseProvider(ReleaseProvider):
    id = "wechat"

    def __init__(self, client: httpx2.AsyncClient, *, cache_ttl: float = 60.0) -> None:
        if not math.isfinite(cache_ttl) or cache_ttl < 0:
            raise ValueError("cache_ttl must be finite and non-negative")
        self._client = client
        self._cache_ttl = cache_ttl
        self._cache: tuple[Release, ...] = ()
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def get_releases(self, *, force_refresh: bool = False) -> list[Release]:
        async with self._lock:
            if not force_refresh and self._cache and monotonic() < self._expires_at:
                return self._copies()
            try:
                response = await self._client.get(DOWNLOAD_PAGE, params={"lang": "zh_CN"}, follow_redirects=True)
                response.raise_for_status()
            except httpx2.TimeoutException:
                raise ProviderTimeoutError("获取微信下载资料超时。") from None
            except httpx2.HTTPStatusError as error:
                raise ProviderUnavailableError(f"微信官网返回异常（HTTP {error.response.status_code}）。") from None
            except httpx2.HTTPError:
                raise ProviderUnavailableError("暂时无法连接微信官网。") from None
            releases = self._parse_page(response.text)
            self._cache = tuple(releases)
            self._expires_at = monotonic() + self._cache_ttl
            return self._copies()

    def _copies(self) -> list[Release]:
        return [release.model_copy(deep=True) for release in self._cache]

    async def resolve_download(self, release: Release) -> Release:
        if not isinstance(release, Release):
            raise ProviderProtocolError("微信安装包资料无效。")
        available = await self.get_releases()
        current = next((item for item in available if item.id == release.id), None)
        if current is None:
            raise ProviderReleaseChangedError("微信官方版本已更新，请刷新版本资料。")
        if current.model_dump(mode="json") != release.model_dump(mode="json"):
            raise ProviderProtocolError("微信安装包与官方资料不一致。")
        return current.model_copy(deep=True)

    @classmethod
    def _parse_page(cls, text: str) -> list[Release]:
        parser = _PageParser()
        parser.feed(text)
        releases: dict[str, Release] = {}
        for anchor in parser.downloads:
            if not {"newVersion", "oldVersion"}.intersection(anchor.context):
                continue
            file_name = cls._file_name(anchor.href)
            visible_text = " ".join(anchor.text.split())
            version_match = _VERSION.search(visible_text)
            if version_match is None:
                raise ProviderProtocolError("微信官网下载按钮缺少版本号。")
            version = version_match.group(1)
            combined = (file_name + " " + visible_text).lower()
            if "arm64" in combined or "aarch64" in combined:
                architecture = "arm64"
            elif "x86" in combined or "32位" in combined or "32-bit" in combined:
                architecture = "x86"
            elif "x64" in combined or "64位" in combined or "64-bit" in combined:
                architecture = "x64"
            else:
                raise ProviderProtocolError("微信官网下载按钮缺少明确的系统架构。")
            if architecture in releases:
                raise ProviderProtocolError("微信官网包含重复的安装包架构。")
            # The oldVersion section is the official 32-bit compatibility download, with its own version.
            if "oldVersion" in anchor.context and architecture != "x86":
                raise ProviderProtocolError("微信兼容版本的架构信息不一致。")
            try:
                releases[architecture] = Release(
                    id=f"{version}-windows-{architecture}", version=version, platform="windows",
                    architecture=architecture, download_url=anchor.href, file_name=file_name,
                )
            except ValidationError:
                raise ProviderProtocolError("微信官网的安装包资料格式无效。") from None
        if not releases:
            raise ProviderProtocolError("微信官网没有可用的 Windows 安装包资料。")
        return [releases[architecture] for architecture in ("x64", "x86", "arm64") if architecture in releases]

    @staticmethod
    def _file_name(value: str) -> str:
        try:
            parsed = urlsplit(value)
            host = parsed.hostname or ""
            if (parsed.scheme != "https" or not (host == "qq.com" or host.endswith(".qq.com"))
                or parsed.username is not None or parsed.password is not None or parsed.fragment
                or parsed.port not in (None, 443) or re.search(r"%(?![0-9a-fA-F]{2})", value)
                or any(ord(character) < 32 for character in value)):
                raise ValueError("invalid installer address")
            name = unquote(parsed.path.rsplit("/", 1)[-1], encoding="utf-8", errors="strict")
        except (ValueError, UnicodeError):
            raise ProviderProtocolError("微信官网的安装包地址无效。") from None
        if (not name.lower().endswith(".exe") or name != name.strip()
            or PureWindowsPath(name).name != name or name.split(".", 1)[0].upper() in _RESERVED_STEMS
            or any(character in '<>:"/\\|?*' or ord(character) < 32 for character in name)):
            raise ProviderProtocolError("微信官网的安装包文件名无效。")
        return name
