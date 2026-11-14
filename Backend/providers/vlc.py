import json
import re
from urllib.parse import urljoin, urlsplit

from ._media import CachedMediaProvider
from .base import ProviderProtocolError

if "." in __package__:
    from ..models import Release
else:
    from models import Release


PAGE_URL = "https://www.videolan.org/vlc/"
_PLATFORMS_MARKER = re.compile(r"\b(?:var|let|const)\s+PLATFORMS\s*=\s*")
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){1,3}")
_WINDOWS_PLATFORMS = (
    ("windows64", "x64", "win64"),
    ("windows", "x86", "win32"),
    ("windowsarm64", "arm64", "winarm64"),
)


class VLCReleaseProvider(CachedMediaProvider):
    id = "vlc"
    display_name = "VLC"

    async def _load_releases(self) -> list[Release]:
        response = await self._response(PAGE_URL)
        marker = _PLATFORMS_MARKER.search(response.text)
        if marker is None:
            raise ProviderProtocolError("VLC 官方网页缺少平台下载资料。")
        try:
            platforms, _ = json.JSONDecoder().raw_decode(response.text[marker.end():])
        except (ValueError, UnicodeError):
            raise ProviderProtocolError("VLC 官方平台下载资料不是有效的 JSON。") from None
        if not isinstance(platforms, dict):
            raise ProviderProtocolError("VLC 官方平台下载资料结构无效。")

        releases = []
        for key, architecture, directory in _WINDOWS_PLATFORMS:
            record = platforms.get(key)
            if record is None:
                continue
            if not isinstance(record, dict):
                raise ProviderProtocolError("VLC Windows 平台下载资料结构无效。")
            version = record.get("latestVersion")
            if not isinstance(version, str) or len(version) > 50 or not _VERSION.fullmatch(version):
                raise ProviderProtocolError("VLC Windows 版本号无效。")
            location = record.get("location")
            if not isinstance(location, str) or not location:
                raise ProviderProtocolError("VLC Windows 安装包地址缺失。")
            url, filename = self._installer_url(
                urljoin(PAGE_URL, location), allowed_hosts={"get.videolan.org"},
                path_prefix=f"/vlc/{version}/{directory}/",
            )
            expected_filename = f"vlc-{version}-{directory}.exe"
            if filename != expected_filename or urlsplit(str(url)).path != f"/vlc/{version}/{directory}/{expected_filename}":
                raise ProviderProtocolError("VLC 安装包与当前版本或架构不一致。")
            releases.append(self._release(version=version, architecture=architecture, url=url, filename=filename))

        if not releases:
            raise ProviderProtocolError("VLC 官方网页没有 Windows 安装包。")
        latest_version = max(releases, key=lambda release: tuple(int(part) for part in release.version.split("."))).version
        return [release for release in releases if release.version == latest_version]
