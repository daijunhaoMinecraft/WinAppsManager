from html.parser import HTMLParser
import re
from urllib.parse import urljoin, urlsplit

from pydantic import ValidationError

from ._utility import UtilityReleaseProvider, official_executable_url
from .base import ProviderProtocolError

if "." in __package__:
    from ..models import Release
else:
    from models import Release


DOWNLOAD_PAGE_URL = "https://www.7-zip.org/download.html"
_VERSION_HEADING = re.compile(r"\bDownload\s+7-Zip\s+(\d+(?:\.\d+){1,3})\b", re.IGNORECASE)
_INSTALLER_NAME = re.compile(r"^7z(?P<version>\d+)(?P<architecture>-(?:x64|arm64))?\.exe$", re.IGNORECASE)


class _DownloadPage(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self.links: list[str] = []
        self._block_tag: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("p", "h1", "h2", "h3", "h4", "h5", "h6"):
            self._block_tag = tag
            self._text = []
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)

    def handle_data(self, data):
        if self._block_tag is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == self._block_tag:
            self.blocks.append(" ".join(" ".join(self._text).split()))
            self._block_tag = None
            self._text = []


class SevenZipReleaseProvider(UtilityReleaseProvider):
    id = "7zip"
    display_name = "7-Zip"

    async def _load_releases(self) -> list[Release]:
        response = await self._get(DOWNLOAD_PAGE_URL)
        return self._parse_download_page(response.text)

    @classmethod
    def _parse_download_page(cls, content: str) -> list[Release]:
        parser = _DownloadPage()
        parser.feed(content)
        parser.close()
        versions = []
        for block in parser.blocks:
            match = _VERSION_HEADING.search(block)
            if match and not re.search(r"\b(?:alpha|beta|preview|rc)\b", block, re.IGNORECASE):
                versions.append(match.group(1))
        if not versions:
            raise ProviderProtocolError("7-Zip 官方下载页缺少最新稳定版本号。")
        version = max(versions, key=lambda value: tuple(int(part) for part in value.split(".")))
        compact_version = version.replace(".", "")
        releases: dict[str, Release] = {}
        for href in parser.links:
            try:
                absolute_url = urljoin(DOWNLOAD_PAGE_URL, href)
                parsed = urlsplit(absolute_url)
            except ValueError:
                raise ProviderProtocolError("7-Zip 官方下载页包含无效的链接。") from None
            match = _INSTALLER_NAME.fullmatch(parsed.path.rsplit("/", 1)[-1])
            if match is None or match.group("version") != compact_version:
                continue
            prefixes = (
                (f"/ip7z/7zip/releases/download/{version}/", f"/ip7z/7zip/releases/download/v{version}/")
                if parsed.hostname == "github.com" else ("/a/",)
            )
            url, file_name = official_executable_url(
                absolute_url,
                display_name=cls.display_name,
                hosts={"www.7-zip.org", "7-zip.org", "github.com"},
                path_prefixes=prefixes,
            )
            suffix = match.group("architecture")
            architecture = suffix[1:].lower() if suffix else "x86"
            # A page can list a second official mirror for the same installer.
            if architecture in releases:
                continue
            try:
                releases[architecture] = Release(
                    id=f"{version}-windows-{architecture}", version=version, platform="windows",
                    architecture=architecture, download_url=url, file_name=file_name,
                    size_bytes=None, sha256=None,
                )
            except ValidationError:
                raise ProviderProtocolError("7-Zip 官方下载页包含无效的安装包信息。") from None
        if not releases:
            raise ProviderProtocolError("7-Zip 最新稳定版本没有可用的 Windows EXE 安装包。")
        return [releases[architecture] for architecture in ("x64", "x86", "arm64") if architecture in releases]
