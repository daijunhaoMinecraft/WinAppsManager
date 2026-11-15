"""联想应用商店源的统一 API 适配器（``/api/v1/sources/lenovo/...``）。

适配器只做**模型转换 + 错误翻译**，所有抓取/缓存/代理逻辑仍由
:class:`Backend.lenovo.service.LenovoStoreService` 负责，因此 ``/store/lenovo/*`` 旧路由与
统一路由共享同一份缓存、同一套下载地址解析与图片代理，不会重复请求联想接口。

映射关系（字段契约见 ``docs/source-api.md``）：

- ``id`` = 联想 ``softID``（保留原始大小写），不再使用带来源前缀的客户端主键；
- 目录/搜索条目、详情、截图、封面的字段几乎 1:1 映射，图片直连地址只保留安全的公网
  http(s) 绝对地址，代理路径改写为统一前缀 ``/api/v1/sources/lenovo/...``；
- 视频截图额外返回 ``video_proxy_url``（统一视频代理），客户端直接播放，不再自己拼接来源
  路径；
- 联想“找不到”异常翻译成 :class:`SourceNotFoundError`（404），其余上游异常保持
  ``ProviderError``（502/504 由全局处理器映射）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar
from urllib.parse import quote

from .adapters import SourceAdapter
from .errors import SourceNotFoundError, SourceUnavailableError
from .media import image_response
from .models import (
    SourceAppDetails,
    SourceAppSummary,
    SourceCapabilities,
    SourceCatalog,
    SourceCategoriesResponse,
    SourceCategory,
    SourceDetailTag,
    SourceScreenshot,
    SourceSearchResponse,
)

if "." in (__package__ or ""):
    from ..lenovo.errors import LenovoNotFoundError
    from ..lenovo.models import (
        StoreAppDetails,
        StoreAppSummary,
        StoreCatalog,
        StoreCategoriesResponse,
        StoreSearchResponse,
    )
    from ..lenovo.service import SOURCE_DESCRIPTION, SOURCE_ID, SOURCE_NAME
    from ..models import Architecture, Release
else:
    from lenovo.errors import LenovoNotFoundError
    from lenovo.models import (
        StoreAppDetails,
        StoreAppSummary,
        StoreCatalog,
        StoreCategoriesResponse,
        StoreSearchResponse,
    )
    from lenovo.service import SOURCE_DESCRIPTION, SOURCE_ID, SOURCE_NAME
    from models import Architecture, Release

T = TypeVar("T")
UNIFIED_PREFIX = f"/api/v1/sources/{SOURCE_ID}"
NO_STORE_MESSAGE = "联想应用商店源未启用。"


async def _translate(operation: Callable[[], Awaitable[T]]) -> T:
    """执行联想服务调用，把“找不到”翻译成统一 404 异常，其余异常原样抛出。"""

    try:
        return await operation()
    except LenovoNotFoundError as error:
        raise SourceNotFoundError(str(error)) from None


def _app_path(soft_id: str) -> str:
    return f"{UNIFIED_PREFIX}/apps/{quote(soft_id, safe='')}"


def _is_remote(url: str | None) -> bool:
    """统一契约的 ``url`` 只放 CDN 绝对地址；联想缓存里的相对兜底路径改由 proxy 字段表达。"""

    return isinstance(url, str) and url.startswith(("http://", "https://"))


def _summary_fields(item: StoreAppSummary) -> dict[str, Any]:
    return {
        "id": item.soft_id,
        "name": item.name,
        "description": item.description,
        "category": item.category,
        "category_id": item.category_id,
        "group": item.group,
        "version": item.version,
        "size_display": item.size_display,
        "size_bytes": item.size_bytes,
        "publisher": item.publisher,
        "platforms": item.platforms,
        "architectures": list(item.architectures),
        "minimum_windows": item.minimum_windows,
        "icon_url": item.icon_url,
        "icon_proxy_url": f"{_app_path(item.soft_id)}/icon",
        "icon_text": item.icon_text,
        "icon_background": item.icon_background,
        "tags": list(item.tags),
        "score": item.score,
        "download_count": item.download_count,
        "license": item.license,
        "is_free": item.is_free,
        "is_recommended": item.is_recommended,
        "is_beta": item.is_beta,
        "sort_order": item.sort_order,
        "installation_notes": item.installation_notes,
        "website_url": None,
        "download_page_url": None,
    }


def _summary(item: StoreAppSummary) -> SourceAppSummary:
    return SourceAppSummary(**_summary_fields(item))


def _screenshot(soft_id: str, item: Any) -> SourceScreenshot:
    app_path = _app_path(soft_id)
    is_video = item.kind == "video"
    return SourceScreenshot(
        index=item.index,
        kind=item.kind,
        url=item.url if _is_remote(item.url) else None,
        proxy_url=f"{app_path}/screenshots/{item.index}",
        video_proxy_url=f"{app_path}/screenshots/{item.index}/video" if is_video else None,
    )


def _details(item: StoreAppDetails) -> SourceAppDetails:
    app_path = _app_path(item.soft_id)
    cover_url = item.cover_url if _is_remote(item.cover_url) else None
    cover_proxy_url = f"{app_path}/cover" if item.cover_proxy_url is not None else None
    return SourceAppDetails(
        **_summary_fields(item),
        long_description=item.long_description,
        release_notes=item.release_notes,
        screenshots=[_screenshot(item.soft_id, shot) for shot in item.screenshots],
        cover_url=cover_url,
        cover_proxy_url=cover_proxy_url,
        labels=list(item.labels),
        ranking=item.ranking,
        detail_tags=[SourceDetailTag(name=tag.name, tips=tag.tips) for tag in item.detail_tags],
    )


def _catalog(item: StoreCatalog) -> SourceCatalog:
    items = [_summary(app) for app in item.items]
    return SourceCatalog(
        source=SOURCE_ID,
        source_name=SOURCE_NAME,
        updated_at=item.updated_at,
        stale=item.stale,
        total=len(items),
        items=items,
    )


def _categories(item: StoreCategoriesResponse) -> SourceCategoriesResponse:
    categories = [
        SourceCategory(id=category.id, name=category.name, count=category.count, group=group.code)
        for group in item.groups
        for category in group.categories
    ]
    return SourceCategoriesResponse(source=SOURCE_ID, updated_at=item.updated_at, categories=categories)


def _search(item: StoreSearchResponse) -> SourceSearchResponse:
    items = [_summary(app) for app in item.items]
    return SourceSearchResponse(
        source=SOURCE_ID,
        source_name=SOURCE_NAME,
        updated_at=None,
        stale=False,
        query=item.query,
        total=len(items),
        items=items,
    )


class LenovoSourceAdapter(SourceAdapter):
    """联想应用商店源的统一接口适配器（目录/详情/搜索/下载/图片/视频/刷新全能力）。"""

    source_id = SOURCE_ID
    source_name = SOURCE_NAME
    source_description = SOURCE_DESCRIPTION
    search_mode = "catalog"
    online_search = False
    is_default = True
    capabilities = SourceCapabilities(
        categories=True,
        catalog=True,
        search=True,
        details=True,
        download=True,
        screenshots=True,
        cover=True,
        video=True,
        refresh=True,
    )

    def __init__(self, service: Any | None, *, enabled: bool = True) -> None:
        # 服务不存在（``lenovo.enabled=false``）时来源一律视为未启用。
        super().__init__(enabled=enabled and service is not None)
        self._service = service

    # -- 来源描述 ---------------------------------------------------------

    def app_count(self) -> int | None:
        if self._service is None:
            return None
        return self._service.cached_app_count()

    def updated_at(self):
        if self._service is None:
            return None
        return self._service.cached_updated_at()

    def _require(self) -> Any:
        if self._service is None or not self.enabled:
            raise SourceUnavailableError(NO_STORE_MESSAGE)
        return self._service

    # -- 目录 / 详情 / 搜索 / 下载 ---------------------------------------

    async def list_categories(self) -> SourceCategoriesResponse:
        service = self._require()
        return _categories(await _translate(service.get_categories))

    async def list_apps(self) -> SourceCatalog:
        service = self._require()
        # 请求路径只读联想目录快照（内存/磁盘），不触发抓取；目录由后台线程与手动刷新维护。
        return _catalog(await _translate(service.get_catalog))

    async def search(self, query: str, limit: int) -> SourceSearchResponse:
        service = self._require()
        return _search(await _translate(lambda: service.search(query, limit=limit)))

    async def get_app(self, app_id: str) -> SourceAppDetails:
        service = self._require()
        return _details(await _translate(lambda: service.get_details(app_id)))

    async def get_download(self, app_id: str, architecture: Architecture) -> Release:
        service = self._require()
        return await _translate(lambda: service.resolve_download(app_id, architecture=architecture))

    async def refresh(self) -> dict:
        service = self._require()
        return await _translate(service.refresh_catalog)

    # -- 媒体 -------------------------------------------------------------

    async def icon_response(self, request, app_id: str):
        service = self._require()
        payload = await _translate(lambda: service.get_icon(app_id))
        return image_response(request, payload)

    async def screenshot_response(self, request, app_id: str, index: int):
        service = self._require()
        payload = await _translate(lambda: service.get_screenshot(app_id, index))
        return image_response(request, payload)

    async def cover_response(self, request, app_id: str):
        service = self._require()
        payload = await _translate(lambda: service.get_cover(app_id))
        return image_response(request, payload)

    async def open_video(self, app_id: str, index: int, *, range_header: str | None = None):
        service = self._require()
        # 视频代理每次实时解析带 auth_key 的临时地址并返回尚未读取的流；统一路由按 Range 转发。
        return await _translate(lambda: service.open_screenshot_video(app_id, index, range_header=range_header))


__all__ = ["LenovoSourceAdapter"]
