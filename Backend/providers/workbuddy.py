import re

import httpx2
from pydantic import ValidationError

from ._utility import UtilityReleaseProvider, official_executable_url
from .base import ProviderProtocolError, ProviderTimeoutError

if "." in __package__:
    from ..models import Release
else:
    from models import Release


UPDATE_URL = "https://www.workbuddy.cn/v2/update?platform=workbuddy-win32-x64-user"
DOWNLOAD_HOSTS = {"download.codebuddy.cn"}
DOWNLOAD_PATH_PREFIX = "/workbuddy/saas/win32-x64-user/"
_VERSION = re.compile(r"^\d+(?:\.\d+){1,3}$")
_FILE_NAME = re.compile(
    r"^WorkBuddy-win32-x64-user-(?P<version>\d+(?:\.\d+){1,3})-(?P<revision>[0-9a-f]{6,64})\.exe$"
)
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


class WorkBuddyReleaseProvider(UtilityReleaseProvider):
    """WorkBuddy (Tencent Cloud CodeBuddy) Windows x64 installer.

    The desktop client publishes a single update document for
    ``platform=workbuddy-win32-x64-user``; ``win32-ia32`` and ``arm64`` are answered with
    HTTP 400, so only x64 is ever produced. The version, installer URL and SHA256 all come
    from that JSON document. Installer size is optional metadata taken from a HEAD request
    on the same URL, so a CDN that refuses HEAD simply leaves ``size_bytes`` empty.
    """

    id = "workbuddy"
    display_name = "WorkBuddy"

    async def _load_releases(self) -> list[Release]:
        response = await self._get(UPDATE_URL, headers={"Accept": "application/json"})
        try:
            payload = response.json()
        except (ValueError, UnicodeError):
            raise ProviderProtocolError("WorkBuddy 官方更新资料不是有效的 JSON。") from None
        if not isinstance(payload, dict):
            raise ProviderProtocolError("WorkBuddy 官方更新资料格式无效。")

        version = payload.get("version")
        if not isinstance(version, str) or _VERSION.fullmatch(version) is None or len(version) > 50:
            raise ProviderProtocolError("WorkBuddy 官方更新资料缺少有效的版本号。")

        url, file_name = official_executable_url(
            payload.get("url"),
            display_name=self.display_name,
            hosts=DOWNLOAD_HOSTS,
            path_prefixes=(DOWNLOAD_PATH_PREFIX,),
        )
        match = _FILE_NAME.fullmatch(file_name)
        if match is None or match.group("version") != version:
            raise ProviderProtocolError("WorkBuddy 安装包文件名与官方版本号不一致。")

        sha256 = payload.get("sha256hash")
        if not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None:
            raise ProviderProtocolError("WorkBuddy 官方更新资料缺少有效的 SHA256 校验值。")

        try:
            release = Release(
                id=f"{version}-windows-x64",
                version=version,
                platform="windows",
                architecture="x64",
                download_url=url,
                file_name=file_name,
                size_bytes=await self._installer_size(str(url)),
                sha256=sha256.lower(),
            )
        except ValidationError:
            raise ProviderProtocolError("WorkBuddy 官方安装包信息无效。") from None
        return [release]

    async def _installer_size(self, url: str) -> int | None:
        """Return the installer size; it is optional metadata, so only timeouts propagate."""

        try:
            response = await self._client.head(url, follow_redirects=True)
            response.raise_for_status()
        except httpx2.TimeoutException:
            raise ProviderTimeoutError(f"{self.display_name} 安装包大小请求超时。") from None
        except httpx2.HTTPError:
            return None
        value = response.headers.get("content-length")
        if value is None:
            return None
        try:
            size = int(value.strip())
        except AttributeError:
            raise ProviderProtocolError("WorkBuddy 安装包大小格式无效。") from None
        except ValueError:
            raise ProviderProtocolError("WorkBuddy 安装包大小格式无效。") from None
        if size <= 0:
            raise ProviderProtocolError("WorkBuddy 安装包大小无效。")
        return size
