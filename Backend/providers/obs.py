import re

from ._media import CachedMediaProvider
from .base import ProviderProtocolError

if "." in __package__:
    from ..models import Release
else:
    from models import Release


LATEST_URL = "https://api.github.com/repos/obsproject/obs-studio/releases/latest"
_VERSION = re.compile(r"v?([0-9]+(?:\.[0-9]+){1,3})")
_INSTALLER = re.compile(
    r"OBS-Studio-([0-9]+(?:\.[0-9]+){1,3})-Windows-(x64|x86|arm64)-Installer\.exe", re.IGNORECASE
)
_SHA256 = re.compile(r"sha256:([0-9a-fA-F]{64})")


class OBSReleaseProvider(CachedMediaProvider):
    id = "obs"
    display_name = "OBS Studio"

    async def _load_releases(self) -> list[Release]:
        payload = await self._json(LATEST_URL)
        if not isinstance(payload, dict) or payload.get("draft") is not False or payload.get("prerelease") is not False:
            raise ProviderProtocolError("OBS 官方版本资料不是已发布的稳定版本。")
        tag = payload.get("tag_name")
        version_match = _VERSION.fullmatch(tag) if isinstance(tag, str) and len(tag) <= 50 else None
        if version_match is None:
            raise ProviderProtocolError("OBS 官方版本号无效。")
        version = version_match.group(1)
        assets = payload.get("assets")
        if not isinstance(assets, list):
            raise ProviderProtocolError("OBS 官方版本资料缺少安装包集合。")

        releases = []
        for asset in assets:
            if not isinstance(asset, dict):
                raise ProviderProtocolError("OBS 官方安装包资料结构无效。")
            name = asset.get("name")
            match = _INSTALLER.fullmatch(name) if isinstance(name, str) else None
            if match is None or match.group(1) != version:
                continue
            architecture = match.group(2).lower()
            url, filename = self._installer_url(
                asset.get("browser_download_url"), allowed_hosts={"github.com"},
                path_prefix=f"/obsproject/obs-studio/releases/download/{tag}/",
            )
            if filename != name:
                raise ProviderProtocolError("OBS 安装包地址与文件名不一致。")
            digest = asset.get("digest")
            sha256 = None
            if digest is not None:
                digest_match = _SHA256.fullmatch(digest) if isinstance(digest, str) else None
                if digest_match is None:
                    raise ProviderProtocolError("OBS 安装包 SHA256 校验资料无效。")
                sha256 = digest_match.group(1).lower()
            releases.append(self._release(
                version=version, architecture=architecture, url=url, filename=filename,
                size_bytes=asset.get("size"), sha256=sha256,
            ))
        if not releases:
            raise ProviderProtocolError("OBS 最新稳定版本没有 Windows EXE 安装包。")
        order = {"x64": 0, "x86": 1, "arm64": 2}
        return sorted(releases, key=lambda release: order[release.architecture])
