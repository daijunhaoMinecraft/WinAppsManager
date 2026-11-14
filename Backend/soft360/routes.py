"""360 软件源（soft360）的 HTTP 路由。

``Backend/main.py`` 通过 ``api.include_router(create_soft360_router())`` 注册本模块自带的
``/api/v1/store/360`` 前缀；所有路由都从 ``request.app.state.soft360`` 取
:class:`~Backend.soft360.service.Soft360StoreService`，为 ``None``（源未启用）时返回 503。

缓存约定：

- ``GET /categories`` 与 ``GET /apps`` **只读内存/磁盘快照，绝不访问 360 网络**，也不接受任何
  触发抓取。360 目录由后台刷新线程（默认每 1800 秒）与 ``POST /store/360/refresh`` 更新；
  查询参数（例如 ``?refresh=true``）会被 FastAPI 忽略，不会触发抓取；
- 目录、分类、详情、下载和刷新响应都带 ``Cache-Control: no-store``；下载地址每轮实时解析，
  绝不进入任何中间缓存；
- 图片（图标 / 截图）响应带 ``Cache-Control: public, max-age=86400`` 与 ``ETag``，
  请求头 ``If-None-Match`` 与 ETag 相同（兼容带/不带引号、``W/`` 前缀和逗号列表）时返回 304；
- ``Soft360NotFoundError`` 在这里转成 404；其余 ``Soft360Error`` 交给 ``main.py`` 的全局
  ``ProviderError`` 异常处理器映射成 502/504，本模块不重复包装成 500。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response

from .errors import Soft360NotFoundError
from .media import ImagePayload
from .models import (
    Soft360AppDetails,
    Soft360Catalog,
    Soft360CategoriesResponse,
    Soft360SearchResponse,
)

if "." in (__package__ or ""):
    from ..models import Architecture, Release
else:
    from models import Architecture, Release


# 本模块自带前缀，main.py 直接 include 不带额外 prefix。
STORE_PREFIX = "/api/v1/store/360"
# 360 soft_id 是数字字符串；同时接受客户端主键 soft360-{soft_id}（服务层会去掉前缀）。
SOFT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$"
SOURCE_DISABLED_DETAIL = "360 软件源未启用。"
NO_STORE = "no-store"
IMAGE_CACHE_CONTROL = "public, max-age=86400"
SEARCH_MIN_LIMIT = 1
SEARCH_MAX_LIMIT = 50
SEARCH_DEFAULT_LIMIT = 30

SoftIdPath = Annotated[str, Path(min_length=1, max_length=40, pattern=SOFT_ID_PATTERN)]
ScreenshotIndexPath = Annotated[int, Path(ge=0)]


def _service(request: Request) -> Any:
    """取 360 服务；源未启用（``api.state.soft360 is None``）时返回 503。"""

    service = getattr(request.app.state, "soft360", None)
    if service is None:
        raise HTTPException(status_code=503, detail=SOURCE_DISABLED_DETAIL)
    return service


def _not_found(error: Soft360NotFoundError) -> HTTPException:
    return HTTPException(status_code=404, detail=str(error))


def _etag_matches(header: str | None, etag: str) -> bool:
    """判断 ``If-None-Match`` 是否命中图片 ETag（兼容引号、``W/`` 与逗号列表）。"""

    if not header:
        return False
    expected = etag.strip().casefold()
    for candidate in header.split(","):
        value = candidate.strip()
        if not value:
            continue
        if value == "*":
            return True
        if value[:2].casefold() == "w/":
            value = value[2:].strip()
        if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        if value.casefold() == expected:
            return True
    return False


def _image_response(request: Request, payload: ImagePayload) -> Response:
    """构造图片响应：长 TTL + 带引号 ETag，``If-None-Match`` 命中时返回 304。"""

    headers = {
        "Cache-Control": IMAGE_CACHE_CONTROL,
        "ETag": f'"{payload.etag}"',
    }
    if _etag_matches(request.headers.get("if-none-match"), payload.etag):
        return Response(status_code=304, headers=headers)
    return Response(content=payload.content, media_type=payload.content_type, headers=headers)


async def _image_endpoint(
    request: Request, operation: Callable[[Any], Awaitable[ImagePayload]]
) -> Response:
    service = _service(request)
    try:
        payload = await operation(service)
    except Soft360NotFoundError as error:
        raise _not_found(error) from None
    return _image_response(request, payload)


def create_soft360_router() -> APIRouter:
    """创建 360 软件源路由（前缀 ``/api/v1/store/360``）。"""

    router = APIRouter(prefix=STORE_PREFIX, tags=["360 Store"])

    @router.get("/categories", response_model=Soft360CategoriesResponse)
    async def get_categories(request: Request, response: Response) -> Soft360CategoriesResponse:
        response.headers["Cache-Control"] = NO_STORE
        try:
            return await _service(request).get_categories()
        except Soft360NotFoundError as error:
            raise _not_found(error) from None

    @router.get("/apps", response_model=Soft360Catalog)
    async def list_apps(request: Request, response: Response) -> Soft360Catalog:
        response.headers["Cache-Control"] = NO_STORE
        # GET 只读内存/磁盘快照（catalog.json），绝不访问 360；目录由后台定时刷新维护。
        # 未知查询参数（例如 ?refresh=true）会被 FastAPI 忽略，不会触发抓取；
        # 手动立即更新只走 POST /store/360/refresh。
        return await _service(request).get_catalog()

    @router.get("/search", response_model=Soft360SearchResponse)
    async def search_apps(
        request: Request,
        response: Response,
        q: Annotated[str, Query(min_length=1, max_length=100)],
        limit: Annotated[int, Query(ge=SEARCH_MIN_LIMIT, le=SEARCH_MAX_LIMIT)] = SEARCH_DEFAULT_LIMIT,
    ) -> Soft360SearchResponse:
        response.headers["Cache-Control"] = NO_STORE
        try:
            return await _service(request).search(q, limit=limit)
        except Soft360NotFoundError as error:
            raise _not_found(error) from None

    @router.get("/apps/{soft_id}", response_model=Soft360AppDetails)
    async def get_app_details(
        request: Request,
        response: Response,
        soft_id: SoftIdPath,
    ) -> Soft360AppDetails:
        response.headers["Cache-Control"] = NO_STORE
        try:
            # 详情只读服务端缓存；GET 不提供强制刷新（未知查询参数会被忽略）。
            return await _service(request).get_details(soft_id)
        except Soft360NotFoundError as error:
            raise _not_found(error) from None

    @router.get("/apps/{soft_id}/download", response_model=Release)
    async def get_app_download(
        request: Request,
        response: Response,
        soft_id: SoftIdPath,
        architecture: Architecture = "x64",
    ) -> Release:
        response.headers["Cache-Control"] = NO_STORE
        try:
            return await _service(request).resolve_download(soft_id, architecture=architecture)
        except Soft360NotFoundError as error:
            raise _not_found(error) from None

    @router.get("/apps/{soft_id}/icon")
    async def get_app_icon(request: Request, soft_id: SoftIdPath) -> Response:
        return await _image_endpoint(request, lambda service: service.get_icon(soft_id))

    @router.get("/apps/{soft_id}/screenshots/{index}")
    async def get_app_screenshot(
        request: Request, soft_id: SoftIdPath, index: ScreenshotIndexPath
    ) -> Response:
        return await _image_endpoint(
            request, lambda service: service.get_screenshot(soft_id, index)
        )

    @router.post("/refresh")
    async def refresh_catalog(request: Request, response: Response) -> dict[str, Any]:
        response.headers["Cache-Control"] = NO_STORE
        try:
            return await _service(request).refresh_catalog()
        except Soft360NotFoundError as error:
            raise _not_found(error) from None

    return router


__all__ = [
    "IMAGE_CACHE_CONTROL",
    "NO_STORE",
    "SOURCE_DISABLED_DETAIL",
    "STORE_PREFIX",
    "create_soft360_router",
]
