import json
import re
from urllib.parse import quote

import httpx2
from pydantic import ValidationError

from ._utility import UtilityReleaseProvider, official_executable_url
from .base import ProviderProtocolError, ProviderTimeoutError, ProviderUnavailableError

if "." in __package__:
    from ..models import Release
else:
    from models import Release


DOWNLOAD_PAGE_URL = "https://www.doubao.com/download/desktop"
LANDING_CLICK_URL = "https://99uri.com/ug/pc/landpage_click"
DOWNLOAD_HOSTS = {"lf9-apk.ugapk.cn"}
DOWNLOAD_PATH_PREFIX = "/package/pc/"

#: doubao.com's desktop download page hard codes one landing token per platform/channel in
#: its front-end bundle (``{win_x64: "L58eQ", win_arm: "LmxKF", ...}``) and posts it to the
#: landing click endpoint; there is no public version API. The token can change when the page
#: is redeployed, in which case the endpoint answers ``ret != 0`` and the provider reports the
#: software as temporarily unavailable instead of inventing a release.
WIN_X64_SURL_TOKEN = "L58eQ"

_REQUEST_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://www.doubao.com",
    "Referer": "https://www.doubao.com/",
}

#: The returned installer is the online (web-installed) Windows x64 package; its file name
#: carries the build number (``..._v2035012_59db_1794554757.exe``) which is the only version
#: string doubao publishes for the desktop client.
_FILE_NAME = re.compile(
    r"^doubao_client_[0-9a-z_]*online_installer_win_v(?P<build>\d{4,12})_[0-9a-z]+_\d{6,12}\.exe$"
)


def _request_body() -> str:
    content = json.dumps(
        {"web_url": quote(DOWNLOAD_PAGE_URL, safe=""), "surl_token": WIN_X64_SURL_TOKEN},
        separators=(",", ":"),
    )
    return json.dumps({"type": 1, "content": content, "os": "windows"}, separators=(",", ":"))


def _readable(value, limit: int = 80) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = "".join(character for character in value if character.isprintable()).strip()
    return cleaned[:limit]


class DoubaoReleaseProvider(UtilityReleaseProvider):
    """Doubao (ByteDance) Windows x64 online installer.

    The official site resolves the installer on the server: the download page posts the
    hard-coded landing token to ``https://99uri.com/ug/pc/landpage_click`` and receives a
    ``download_url`` on ByteDance's ``lf9-apk.ugapk.cn`` CDN. That URL points at the small
    online installer, and its build number is the only published version, so it is used as
    the release version. No SHA256 is published. The installer size is optional metadata from
    a HEAD request on the returned URL.
    """

    id = "doubao"
    display_name = "豆包"

    async def _load_releases(self) -> list[Release]:
        response = await self._post(LANDING_CLICK_URL, content=_request_body())
        try:
            payload = response.json()
        except (ValueError, UnicodeError):
            raise ProviderProtocolError("豆包官方下载接口返回的不是有效的 JSON。") from None
        if not isinstance(payload, dict):
            raise ProviderProtocolError("豆包官方下载接口返回格式无效。")
        result = payload.get("ret")
        if type(result) is not int or result != 0:
            reason = _readable(payload.get("errmsg"))
            detail = f"（{reason}）" if reason else ""
            raise ProviderUnavailableError(
                f"豆包官方下载接口没有返回安装包{detail}，官网下载渠道参数可能已更新。"
            )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ProviderProtocolError("豆包官方下载接口缺少安装包资料。")

        url, file_name = official_executable_url(
            data.get("download_url"),
            display_name=self.display_name,
            hosts=DOWNLOAD_HOSTS,
            path_prefixes=(DOWNLOAD_PATH_PREFIX,),
        )
        match = _FILE_NAME.fullmatch(file_name)
        if match is None:
            raise ProviderProtocolError("豆包在线安装包文件名不符合约定，无法确定构建版本号。")
        version = match.group("build")

        try:
            release = Release(
                id=f"{version}-windows-x64",
                version=version,
                platform="windows",
                architecture="x64",
                download_url=url,
                file_name=file_name,
                size_bytes=await self._installer_size(str(url)),
            )
        except ValidationError:
            raise ProviderProtocolError("豆包官方安装包信息无效。") from None
        return [release]

    async def _post(self, url: str, *, content: str) -> httpx2.Response:
        try:
            response = await self._client.post(url, content=content, headers=_REQUEST_HEADERS)
            response.raise_for_status()
            return response
        except httpx2.TimeoutException:
            raise ProviderTimeoutError(f"{self.display_name} 官方下载接口请求超时。") from None
        except httpx2.HTTPStatusError as error:
            raise ProviderUnavailableError(
                f"{self.display_name} 官方下载接口请求失败（HTTP {error.response.status_code}）。"
            ) from None
        except httpx2.HTTPError:
            raise ProviderUnavailableError(f"{self.display_name} 官方下载接口暂时无法连接。") from None

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
        except (AttributeError, ValueError):
            raise ProviderProtocolError("豆包安装包大小格式无效。") from None
        if size <= 0:
            raise ProviderProtocolError("豆包安装包大小无效。")
        return size
