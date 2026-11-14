from html.parser import HTMLParser
import re

from pydantic import ValidationError

from ._utility import UtilityReleaseProvider, official_executable_url
from .base import ProviderProtocolError

if "." in __package__:
    from ..models import Release
else:
    from models import Release


DOWNLOADS_INDEX_URL = "https://notepad-plus-plus.org/downloads/"
RELEASE_API_PREFIX = "https://api.github.com/repos/notepad-plus-plus/notepad-plus-plus/releases/tags/v"
_CURRENT_VERSION = re.compile(r"\bCurrent\s+Version\s*:?\s*v?(\d+(?:\.\d+){1,3})\b", re.IGNORECASE)
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


class _VersionPage(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._ignored_depth += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data):
        if not self._ignored_depth:
            self.text.append(data)


class NotepadPlusPlusReleaseProvider(UtilityReleaseProvider):
    id = "notepadplusplus"
    display_name = "Notepad++"

    async def _load_releases(self) -> list[Release]:
        index = await self._get(DOWNLOADS_INDEX_URL)
        parser = _VersionPage()
        parser.feed(index.text)
        parser.close()
        match = _CURRENT_VERSION.search(" ".join(parser.text))
        if match is None or len(match.group(1)) > 50:
            raise ProviderProtocolError("Notepad++ 官方下载页缺少当前稳定版本号。")
        version = match.group(1)
        response = await self._get(RELEASE_API_PREFIX + version, headers={"Accept": "application/vnd.github+json"})
        try:
            payload = response.json()
        except (ValueError, UnicodeError):
            raise ProviderProtocolError("Notepad++ 官方版本资料不是有效的 JSON。") from None
        return self._parse_release(payload, version)

    @classmethod
    def _parse_release(cls, payload, version: str) -> list[Release]:
        if (
            not isinstance(payload, dict)
            or payload.get("tag_name") != f"v{version}"
            or payload.get("draft") is not False
            or payload.get("prerelease") is not False
            or not isinstance(payload.get("assets"), list)
        ):
            raise ProviderProtocolError("Notepad++ 官方版本资料与当前稳定版不一致。")

        names = {
            f"npp.{version}.Installer.x64.exe": "x64",
            f"npp.{version}.Installer.exe": "x86",
            f"npp.{version}.Installer.x86.exe": "x86",
            f"npp.{version}.Installer.arm64.exe": "arm64",
        }
        releases: dict[str, Release] = {}
        for asset in payload["assets"]:
            if not isinstance(asset, dict):
                raise ProviderProtocolError("Notepad++ 安装包资产列表格式无效。")
            name = asset.get("name")
            architecture = names.get(name) if isinstance(name, str) else None
            if architecture is None or asset.get("state", "uploaded") != "uploaded":
                continue
            if architecture in releases:
                raise ProviderProtocolError("Notepad++ 官方资料包含重复架构的安装包。")
            url, file_name = official_executable_url(
                asset.get("browser_download_url"), display_name=cls.display_name,
                hosts={"github.com"},
                path_prefixes=(f"/notepad-plus-plus/notepad-plus-plus/releases/download/v{version}/",),
            )
            if file_name != name:
                raise ProviderProtocolError("Notepad++ 安装包名称与下载地址不一致。")
            size = asset.get("size")
            if size is not None and (type(size) is not int or size <= 0):
                raise ProviderProtocolError("Notepad++ 安装包大小无效。")
            digest = asset.get("digest")
            sha256 = None
            if digest is not None:
                if not isinstance(digest, str):
                    raise ProviderProtocolError("Notepad++ 安装包校验值格式无效。")
                if digest.lower().startswith("sha256:"):
                    sha256 = digest[7:]
                    if _SHA256.fullmatch(sha256) is None:
                        raise ProviderProtocolError("Notepad++ 安装包 SHA256 校验值无效。")
                    sha256 = sha256.lower()
            try:
                releases[architecture] = Release(
                    id=f"{version}-windows-{architecture}", version=version, platform="windows",
                    architecture=architecture, download_url=url, file_name=file_name,
                    size_bytes=size, sha256=sha256,
                )
            except ValidationError:
                raise ProviderProtocolError("Notepad++ 官方安装包信息无效。") from None
        if not releases:
            raise ProviderProtocolError("Notepad++ 当前稳定版没有可用的 Windows EXE 安装包。")
        return [releases[architecture] for architecture in ("x64", "x86", "arm64") if architecture in releases]
