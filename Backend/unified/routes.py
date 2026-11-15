"""统一软件源 HTTP 路由：``/api/v1/sources`` 与 ``/api/v1/sources/{source_id}/...``。

契约见 ``docs/source-api.md``。路由本身不含任何来源 ID 分支：来源描述、``capabilities``、
目录/详情/搜索/下载/媒体全部通过 ``request.app.state.source_registry`` 中的适配器完成，
新增来源只需注册适配器（见 :mod:`Backend.unified.factory`）。

缓存约定：

- 目录/分类/详情/搜索/下载/刷新/视频响应都是 ``Cache-Control: no-store``（与旧
  ``/store/lenovo/*``、``/store/360/*`` 一致；下载地址绝不进入任何中间缓存）；
- 图片（图标/截图/封面）是 ``public, max-age=86400`` + ``ETag``，``If-None-Match`` 命中返回
  304，与旧图片代理路由完全一致；
- 未启用的来源与未知来源都返回 404 ``{"detail":"软件源不存在或未启用"}``；不支持的能力
  返回 404（客户端本应根据 ``capabilities`` 不请求）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response

from .adapters import SourceAdapter, SourceRegistry
from .errors import SourceNotFoundError, SourceUnavailableError, SourceUnsupportedError
from .media import NO_STORE, video_head_response, video_response
from .models import (
    SourceAppDetails,
    SourceCatalog,
    SourceCategoriesResponse,
    SourceSearchResponse,
    SourcesResponse,
)

if "." in (__package__ or ""):
    from ..models import Architecture, Release
else:
    from models import Architecture, Release

T = TypeVar("T")

SOURCE_DISABLED_DETAIL = "软件源不存在或未启用"
UNSUPPORTED_DETAIL = "该软件源不支持此能力。"
SOURCE_ID_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}$"
APP_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"
SEARCH_MIN_LIMIT = 1
SEARCH_MAX_LIMIT = 50
SEARCH_DEFAULT_LIMIT = 30

SourceIdPath = Annotated[str, Path(min_length=1, max_length=64, pattern=SOURCE_ID_PATTERN)]
AppIdPath = Annotated[str, Path(min_length=1, max_length=100, pattern=APP_ID_PATTERN)]
ScreenshotIndexPath = Annotated[int, Path(ge=0)]


def create_unified_router() -> APIRouter:
    """创建统一软件源路由（``/api/v1/sources...``，无额外 prefix 由 main.py include）。"""

    router = APIRouter(prefix="/api/v1", tags=["Sources"])

    def registry(request: Request) -> SourceRegistry:
        value = getattr(request.app.state, "source_registry", None)
        if value is None:
            raise HTTPException(status_code=503, detail="软件源注册表尚未初始化。")
        return value

    def adapter_for(request: Request, source_id: str) -> SourceAdapter:
        adapter = registry(request).get(source_id)
        if adapter is None or not adapter.enabled:
            raise HTTPException(status_code=404, detail=SOURCE_DISABLED_DETAIL)
        return adapter

    def require(adapter: SourceAdapter, capability: str) -> None:
        """来源声明不具备该能力时返回 404（客户端应根据 ``capabilities`` 不请求）。"""

        if not getattr(adapter.capabilities, capability):
            raise HTTPException(status_code=404, detail=UNSUPPORTED_DETAIL)

    async def invoke(operation: Callable[[], Awaitable[T]]) -> T:
        """把适配器的跨来源异常映射成统一的 HTTP 错误。"""

        try:
            return await operation()
        except SourceNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from None
        except SourceUnsupportedError as error:
            raise HTTPException(status_code=404, detail=str(error)) from None
        except SourceUnavailableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from None

    # -- 来源列表 ---------------------------------------------------------

    @router.get("/sources", response_model=SourcesResponse)
    async def list_sources(request: Request, response: Response) -> SourcesResponse:
        """软件源列表：来源描述、启用状态、搜索方式与 ``capabilities``。"""

        response.headers["Cache-Control"] = NO_STORE
        return registry(request).sources_response()

    # -- 分类 / 目录 / 搜索 -----------------------------------------------

    @router.get("/sources/{source_id}/categories", response_model=SourceCategoriesResponse)
    async def get_categories(
        request: Request, response: Response, source_id: SourceIdPath
    ) -> SourceCategoriesResponse:
        adapter = adapter_for(request, source_id)
        response.headers["Cache-Control"] = NO_STORE
        return await invoke(adapter.list_categories)

    @router.get("/sources/{source_id}/apps", response_model=SourceCatalog)
    async def list_apps(request: Request, response: Response, source_id: SourceIdPath) -> SourceCatalog:
        adapter = adapter_for(request, source_id)
        response.headers["Cache-Control"] = NO_STORE
        # 只读来源自己的目录快照/缓存；统一路由不接受 refresh 查询参数（刷新走 POST /refresh）。
        return await invoke(adapter.list_apps)

    @router.get("/sources/{source_id}/search", response_model=SourceSearchResponse)
    async def search_apps(
        request: Request,
        response: Response,
        source_id: SourceIdPath,
        q: Annotated[str, Query(min_length=1, max_length=100)],
        limit: Annotated[int, Query(ge=SEARCH_MIN_LIMIT, le=SEARCH_MAX_LIMIT)] = SEARCH_DEFAULT_LIMIT,
    ) -> SourceSearchResponse:
        adapter = adapter_for(request, source_id)
        require(adapter, "search")
        response.headers["Cache-Control"] = NO_STORE
        return await invoke(lambda: adapter.search(q, limit))

    # -- 详情 / 下载 -------------------------------------------------------

    @router.get("/sources/{source_id}/apps/{app_id}", response_model=SourceAppDetails)
    async def get_app_details(
        request: Request, response: Response, source_id: SourceIdPath, app_id: AppIdPath
    ) -> SourceAppDetails:
        adapter = adapter_for(request, source_id)
        require(adapter, "details")
        response.headers["Cache-Control"] = NO_STORE
        return await invoke(lambda: adapter.get_app(app_id))

    @router.get("/sources/{source_id}/apps/{app_id}/download", response_model=Release)
    async def get_app_download(
        request: Request,
        response: Response,
        source_id: SourceIdPath,
        app_id: AppIdPath,
        architecture: Architecture = "x64",
    ) -> Release:
        adapter = adapter_for(request, source_id)
        require(adapter, "download")
        response.headers["Cache-Control"] = NO_STORE
        return await invoke(lambda: adapter.get_download(app_id, architecture))

    @router.post("/sources/{source_id}/refresh")
    async def refresh_source(
        request: Request, response: Response, source_id: SourceIdPath
    ) -> dict[str, Any]:
        adapter = adapter_for(request, source_id)
        require(adapter, "refresh")
        response.headers["Cache-Control"] = NO_STORE
        # 刷新结果是来源服务自己的统计字段（refreshed/total/updated_at/duration_seconds...），
        # 原样返回，不强行套统一模型。
        return await invoke(adapter.refresh)

    # -- 媒体（图标 / 截图 / 封面 / 视频）------------------------------------

    @router.get("/sources/{source_id}/apps/{app_id}/icon")
    async def get_app_icon(request: Request, source_id: SourceIdPath, app_id: AppIdPath) -> Response:
        adapter = adapter_for(request, source_id)
        return await invoke(lambda: adapter.icon_response(request, app_id))

    @router.get("/sources/{source_id}/apps/{app_id}/screenshots/{index}")
    async def get_app_screenshot(
        request: Request, source_id: SourceIdPath, app_id: AppIdPath, index: ScreenshotIndexPath
    ) -> Response:
        adapter = adapter_for(request, source_id)
        require(adapter, "screenshots")
        return await invoke(lambda: adapter.screenshot_response(request, app_id, index))

    @router.get("/sources/{source_id}/apps/{app_id}/cover")
    async def get_app_cover(request: Request, source_id: SourceIdPath, app_id: AppIdPath) -> Response:
        adapter = adapter_for(request, source_id)
        require(adapter, "cover")
        return await invoke(lambda: adapter.cover_response(request, app_id))

    # HEAD 必须注册在 GET 之前：Starlette 会给 GET 路由自动加 HEAD 方法，顺序反了 HEAD 会落到
    # GET 处理器并白白读完整段视频。HEAD 只解析地址、取上游响应头，读完 0 字节。
    @router.head(
        "/sources/{source_id}/apps/{app_id}/screenshots/{index}/video",
        include_in_schema=False,
    )
    async def head_app_screenshot_video(
        request: Request, source_id: SourceIdPath, app_id: AppIdPath, index: ScreenshotIndexPath
    ) -> Response:
        adapter = adapter_for(request, source_id)
        require(adapter, "video")
        stream = await invoke(
            lambda: adapter.open_video(app_id, index, range_header=request.headers.get("range"))
        )
        try:
            return video_head_response(stream)
        finally:
            await stream.aclose()

    @router.get("/sources/{source_id}/apps/{app_id}/screenshots/{index}/video")
    async def get_app_screenshot_video(
        request: Request, source_id: SourceIdPath, app_id: AppIdPath, index: ScreenshotIndexPath
    ) -> Response:
        """视频流代理：透传 Range，分块转发上游字节，不缓存签名 URL、不落盘、不整体读入内存。"""

        adapter = adapter_for(request, source_id)
        require(adapter, "video")
        stream = await invoke(
            lambda: adapter.open_video(app_id, index, range_header=request.headers.get("range"))
        )
        return video_response(stream)

    return router


__all__ = [
    "APP_ID_PATTERN",
    "SEARCH_DEFAULT_LIMIT",
    "SEARCH_MAX_LIMIT",
    "SEARCH_MIN_LIMIT",
    "SOURCE_DISABLED_DETAIL",
    "SOURCE_ID_PATTERN",
    "UNSUPPORTED_DETAIL",
    "create_unified_router",
]
