"""360 软件管家源的统一 API 适配器（``/api/v1/sources/soft360/...``）。

与联想源一样只做模型转换 + 错误翻译，抓取/缓存/代理逻辑全部复用
:class:`Backend.soft360.service.Soft360StoreService`，所以 ``/store/360/*`` 旧路由与统一路由
共享同一份目录/详情/图片缓存。

与联想的差异（均体现在契约里）：

- ``id`` = 360 ``soft_id``（数字字符串）；
- 目录/搜索条目没有版本信息：``version`` 为 ``null``，详情接口返回真实版本；
- 没有封面与视频能力，``capabilities.cover`` / ``capabilities.video`` 为 ``false``；
- 搜索能力为 ``true``（``search_mode=api``，目录只是各分类热门子集）。
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
    SourceScreenshot,
    SourceSearchResponse,
)

if "." in (__package__ or ""):
    from ..models import Architecture, Release
    from ..soft360.errors import Soft360NotFoundError
    from ..soft360.models import (
        Soft360AppDetails,
        Soft360AppSummary,
        Soft360Catalog,
        Soft360CategoriesResponse,
        Soft360SearchResponse,
    )
    from ..soft360.service import SOURCE_DESCRIPTION, SOURCE_ID, SOURCE_NAME
else:
    from models import Architecture, Release
    from soft360.errors import Soft360NotFoundError
    from soft360.models import (
        Soft360AppDetails,
        Soft360AppSummary,
        Soft360Catalog,
        Soft360CategoriesResponse,
        Soft360SearchResponse,
    )
    from soft360.service import SOURCE_DESCRIPTION, SOURCE_ID, SOURCE_NAME

T = TypeVar("T")
UNIFIED_PREFIX = f"/api/v1/sources/{SOURCE_ID}"
SOURCE_DISABLED_MESSAGE = "360 软件源未启用。"


async def _translate(operation: Callable[[], Awaitable[T]]) -> T:
    """执行 360 服务调用，把“找不到”翻译成统一 404 异常，其余异常原样抛出。"""

    try:
        return await operation()
    except Soft360NotFoundError as error:
        raise SourceNotFoundError(str(error)) from None


def _app_path(soft_id: str) -> str:
    return f"{UNIFIED_PREFIX}/apps/{quote(soft_id, safe='')}"


def _is_remote(url: str | None) -> bool:
    return isinstance(url, str) and url.startswith(("http://", "https://"))


def _summary_fields(item: Soft360AppSummary) -> dict[str, Any]:
    return {
        "id": item.soft_id,
        "name": item.name,
        "description": item.description,
        "category": item.category,
        "category_id": item.category_id,
        # 360 目录/搜索没有软件分组（software/game）概念。
        "group": None,
        # 目录/搜索接口不返回版本：统一模型允许 null，详情接口再返回真实版本。
        "version": None,
        "size_display": None,
        "size_bytes": item.size_bytes,
        "publisher": None,
        "platforms": None,
        "architectures": list(item.architectures),
        "minimum_windows": item.minimum_windows,
        "icon_url": item.icon_url,
        "icon_proxy_url": f"{_app_path(item.soft_id)}/icon",
        "icon_text": None,
        "icon_background": None,
        "tags": list(item.tags),
        "score": item.score,
        "download_count": None,
        "license": item.license,
        "is_free": item.is_free,
        # 360 目录/搜索没有推荐/测试版标记，明确给 false（严格客户端不能接受 null 布尔值）。
        "is_recommended": False,
        "is_beta": False,
        "sort_order": item.sort_order,
        "installation_notes": item.installation_notes,
        "website_url": None,
        "download_page_url": None,
    }


def _summary(item: Soft360AppSummary) -> SourceAppSummary:
    return SourceAppSummary(**_summary_fields(item))


def _details(item: Soft360AppDetails) -> SourceAppDetails:
    app_path = _app_path(item.soft_id)
    fields = _summary_fields(item)
    fields.update(
        version=item.version,
        publisher=item.publisher,
        long_description=item.long_description,
    )
    return SourceAppDetails(
        **fields,
        release_notes="",
        screenshots=[
            SourceScreenshot(
                index=screenshot.index,
                kind="image",
                url=screenshot.url if _is_remote(screenshot.url) else None,
                proxy_url=f"{app_path}/screenshots/{screenshot.index}",
            )
            for screenshot in item.screenshots
        ],
        cover_url=None,
        cover_proxy_url=None,
        labels=[],
        ranking=None,
        detail_tags=[],
    )


def _catalog(item: Soft360Catalog) -> SourceCatalog:
    items = [_summary(app) for app in item.items]
    return SourceCatalog(
        source=SOURCE_ID,
        source_name=SOURCE_NAME,
        updated_at=item.updated_at,
        stale=item.stale,
        total=len(items),
        items=items,
    )


def _categories(item: Soft360CategoriesResponse) -> SourceCategoriesResponse:
    categories = [
        SourceCategory(id=category.id, name=category.name, count=category.count, group=None)
        for category in item.categories
    ]
    return SourceCategoriesResponse(source=SOURCE_ID, updated_at=item.updated_at, categories=categories)


def _search(item: Soft360SearchResponse) -> SourceSearchResponse:
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


class Soft360SourceAdapter(SourceAdapter):
    """360 软件管家源的统一接口适配器（无封面/视频能力）。"""

    source_id = SOURCE_ID
    source_name = SOURCE_NAME
    source_description = SOURCE_DESCRIPTION
    search_mode = "api"
    online_search = True
    is_default = False
    capabilities = SourceCapabilities(
        categories=True,
        catalog=True,
        search=True,
        details=True,
        download=True,
        screenshots=True,
        cover=False,
        video=False,
        # 手动刷新接口已禁用，目录只由后台定时线程刷新。
        refresh=False,
    )

    def __init__(self, service: Any | None, *, enabled: bool = True) -> None:
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
            raise SourceUnavailableError(SOURCE_DISABLED_MESSAGE)
        return self._service

    # -- 目录 / 详情 / 搜索 / 下载 ---------------------------------------

    async def list_categories(self) -> SourceCategoriesResponse:
        service = self._require()
        return _categories(await _translate(service.get_categories))

    async def list_apps(self) -> SourceCatalog:
        service = self._require()
        # 请求路径只读 360 目录快照；任何查询参数都不会触发抓取。
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

    # 手动刷新接口已禁用（目录只由后台线程刷新）。
    # async def refresh(self) -> dict:
    #     service = self._require()
    #     return await _translate(service.refresh_catalog)

    # -- 媒体（图标 / 截图；没有封面与视频）--------------------------------

    async def icon_response(self, request, app_id: str):
        service = self._require()
        payload = await _translate(lambda: service.get_icon(app_id))
        return image_response(request, payload)

    async def screenshot_response(self, request, app_id: str, index: int):
        service = self._require()
        payload = await _translate(lambda: service.get_screenshot(app_id, index))
        return image_response(request, payload)


__all__ = ["Soft360SourceAdapter"]
