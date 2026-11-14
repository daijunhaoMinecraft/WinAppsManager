import re
from urllib.parse import unquote

import httpx2
from pydantic import ValidationError

from ._utility import UtilityReleaseProvider, official_executable_url
from .base import ProviderProtocolError, ProviderTimeoutError, ProviderUnavailableError

if "." in __package__:
    from ..models import Release
else:
    from models import Release


DOWNLOAD_URL = "https://download.deepseek.com/desktop/dsh-latest-windows-x64.exe"
DOWNLOAD_HOSTS = {"download.deepseek.com"}
DOWNLOAD_PATH = "/desktop/dsh-latest-windows-x64.exe"

#: Official file name carried by ``Content-Disposition``; the version lives only here.
_FILE_NAME = re.compile(
    r"^deepseek-harness-(?P<version>\d+(?:\.\d+){1,3}(?:-[0-9a-z]+(?:\.[0-9a-z]+)*)?)-win-x64\.exe$"
)
_EXTENDED_VALUE = re.compile(r"^(?P<charset>[a-z0-9-]+)'(?P<language>[^']*)'(?P<value>.+)$", re.IGNORECASE)
_CONTENT_RANGE = re.compile(r"^bytes\s+(?P<start>\d+)-(?P<end>\d+)/(?P<total>\d+|\*)$", re.IGNORECASE)
_RANGE_FALLBACK_STATUS = (405, 501)


def _disposition_file_name(value) -> str | None:
    """Extract ``filename`` from a Content-Disposition header, preferring ``filename*``."""

    if not isinstance(value, str) or not value.strip():
        return None
    plain: str | None = None
    extended: str | None = None
    for part in value.split(";"):
        name, separator, raw = part.partition("=")
        if not separator:
            continue
        key = name.strip().lower()
        candidate = raw.strip()
        if len(candidate) >= 2 and candidate[0] == candidate[-1] == '"':
            candidate = candidate[1:-1]
        if key == "filename*":
            extended = candidate
        elif key == "filename":
            plain = candidate
    if extended:
        match = _EXTENDED_VALUE.fullmatch(extended)
        if match is not None:
            try:
                return unquote(match.group("value"), encoding="utf-8", errors="strict")
            except UnicodeError:
                return None
    if plain:
        return plain
    return None


def _total_size(headers) -> int | None:
    """Return the full installer size, using the total of a 206 ``Content-Range`` reply."""

    value = headers.get("content-range")
    if value is not None:
        match = _CONTENT_RANGE.fullmatch(value.strip())
        if match is None or match.group("total") == "*":
            raise ProviderProtocolError("DeepSeek Harness 官方安装包大小格式无效。")
        value = match.group("total")
    else:
        value = headers.get("content-length")
    if value is None:
        return None
    try:
        size = int(value.strip())
    except (AttributeError, ValueError):
        raise ProviderProtocolError("DeepSeek Harness 官方安装包大小格式无效。") from None
    if size <= 0:
        raise ProviderProtocolError("DeepSeek Harness 官方安装包大小无效。")
    return size


class DeepSeekHarnessReleaseProvider(UtilityReleaseProvider):
    """DeepSeek Harness (dsh) Windows x64 desktop client.

    ``download.deepseek.com`` only serves a stable ``dsh-latest-windows-x64.exe`` alias for the
    Windows 64-bit desktop build: there is no version JSON and no SHA256. The version is read
    from the ``Content-Disposition`` file name of the alias, which the official product page
    (https://www.deepseek.com/harness/) links to. A server that refuses HEAD (405/501) is read
    through a one byte ranged GET, taking the size from ``Content-Range``.
    """

    id = "deepseekharness"
    display_name = "DeepSeek Harness"

    async def _load_releases(self) -> list[Release]:
        download_url, file_name = official_executable_url(
            DOWNLOAD_URL,
            display_name=self.display_name,
            hosts=DOWNLOAD_HOSTS,
            path_prefixes=(DOWNLOAD_PATH,),
        )
        disposition, size_bytes = await self._download_metadata()
        disposition_name = _disposition_file_name(disposition)
        if disposition_name is None:
            raise ProviderProtocolError(
                "DeepSeek Harness 官方安装包响应缺少 Content-Disposition 文件名，无法确定版本号。"
            )
        match = _FILE_NAME.fullmatch(disposition_name)
        if match is None:
            raise ProviderProtocolError("DeepSeek Harness 官方安装包文件名不符合约定，无法确定版本号。")
        version = match.group("version")
        try:
            release = Release(
                id=f"{version}-windows-x64",
                version=version,
                platform="windows",
                architecture="x64",
                download_url=download_url,
                file_name=file_name,
                size_bytes=size_bytes,
            )
        except ValidationError:
            raise ProviderProtocolError("DeepSeek Harness 官方安装包信息无效。") from None
        return [release]

    async def _download_metadata(self) -> tuple[str | None, int | None]:
        try:
            response = await self._client.head(DOWNLOAD_URL, follow_redirects=True)
        except httpx2.TimeoutException:
            raise ProviderTimeoutError(f"{self.display_name} 官方安装包信息请求超时。") from None
        except httpx2.HTTPError:
            raise ProviderUnavailableError(f"{self.display_name} 官方安装包地址暂时无法连接。") from None
        if response.status_code in _RANGE_FALLBACK_STATUS:
            return await self._download_metadata_by_range()
        if response.status_code >= 400:
            raise ProviderUnavailableError(
                f"{self.display_name} 官方安装包信息请求失败（HTTP {response.status_code}）。"
            )
        return response.headers.get("content-disposition"), _total_size(response.headers)

    async def _download_metadata_by_range(self) -> tuple[str | None, int | None]:
        try:
            async with self._client.stream(
                "GET", DOWNLOAD_URL, headers={"Range": "bytes=0-0"}, follow_redirects=True
            ) as response:
                if response.status_code >= 400:
                    raise ProviderUnavailableError(
                        f"{self.display_name} 官方安装包信息请求失败（HTTP {response.status_code}）。"
                    )
                # Only the headers are inspected; the (possibly large) body is never read.
                return response.headers.get("content-disposition"), _total_size(response.headers)
        except httpx2.TimeoutException:
            raise ProviderTimeoutError(f"{self.display_name} 官方安装包信息请求超时。") from None
        except httpx2.HTTPError:
            raise ProviderUnavailableError(f"{self.display_name} 官方安装包地址暂时无法连接。") from None
