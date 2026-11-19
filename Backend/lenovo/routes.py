"""联想应用商店（lenovo）源的 HTTP 路由。

``Backend/main.py`` 通过 ``api.include_router(create_lenovo_router())`` 注册本模块自带的
``/api/v1/store/lenovo`` 前缀；所有路由都从 ``request.app.state.lenovo`` 取
:class:`~Backend.lenovo.service.LenovoStoreService`，为 ``None``（源未启用）时返回 503。

缓存约定（与 ``docs/store-source-design.md`` 3.4 一致）：

- GET 路由**不接受** ``refresh`` 查询参数：目录/详情只读内存/磁盘缓存，手动刷新目录只能走
  ``POST /store/lenovo/refresh``（同步）。旧客户端误传的 ``?refresh=true`` 之类只读查询参数
  会被 FastAPI 忽略，不会触发任何联想抓取；
- 目录、分类、详情、搜索、刷新和下载地址响应都带 ``Cache-Control: no-store``：缓存语义由
  服务层负责，签名下载地址绝不进入任何中间缓存；
- 图片（图标 / 截图 / 封面）响应带 ``Cache-Control: public, max-age=86400`` 与 ``ETag``，
  请求头 ``If-None-Match`` 与 ETag 相同（兼容带/不带引号、``W/`` 前缀和逗号列表）时返回 304；
- 视频代理 ``/apps/{soft_id}/screenshots/{index}/video`` 每次实时解析带 ``auth_key`` 的临时
  地址并流式转发（透传 ``Range``，返回上游 200/206/416 与内容头），响应固定
  ``Cache-Control: no-store``，不缓存 URL、不落盘、不整体读入内存；``HEAD`` 只返回响应头；
- ``LenovoNotFoundError`` 在这里转成 404；其余 ``LenovoError`` 交给 ``main.py`` 的全局
  ``ProviderError`` 异常处理器映射成 502/504，本模块不重复包装成 500。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response
from fastapi.responses import StreamingResponse

from .errors import LenovoNotFoundError
from .media import ImagePayload
from .models import (
    StoreAppDetails,
    StoreCatalog,
    StoreCategoriesResponse,
    StoreSearchResponse,
)

if "." in (__package__ or ""):
    from ..models import Architecture, Release
else:
    from models import Architecture, Release


# 本模块自带前缀，main.py 直接 include 不带额外 prefix（见设计文档 3.6）。
STORE_PREFIX = "/api/v1/store/lenovo"
# 联想 softID：数字（11488）或 L 前缀（L105867），保留原始大小写用于所有联想接口调用。
SOFT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$"
SOURCE_DISABLED_DETAIL = "联想应用商店源未启用。"
NO_STORE = "no-store"
IMAGE_CACHE_CONTROL = "public, max-age=86400"
SEARCH_MIN_LIMIT = 1
SEARCH_MAX_LIMIT = 50
SEARCH_DEFAULT_LIMIT = 30

SoftIdPath = Annotated[str, Path(min_length=1, max_length=40, pattern=SOFT_ID_PATTERN)]
ScreenshotIndexPath = Annotated[int, Path(ge=0)]


def _service(request: Request) -> Any:
    """取联想服务；源未启用（``api.state.lenovo is None``）时返回 503。"""

    service = getattr(request.app.state, "lenovo", None)
    if service is None:
        raise HTTPException(status_code=503, detail=SOURCE_DISABLED_DETAIL)
    return service


def _not_found(error: LenovoNotFoundError) -> HTTPException:
    return HTTPException(status_code=404, detail=str(error))


def _video_not_found(error: LenovoNotFoundError) -> HTTPException:
    """视频代理的 404：同样带 ``no-store``，避免中间层缓存“不是视频”的结论。"""

    return HTTPException(
        status_code=404,
        detail=str(error),
        headers={"Cache-Control": NO_STORE},
    )


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
    except LenovoNotFoundError as error:
        raise _not_found(error) from None
    return _image_response(request, payload)


def create_lenovo_router() -> APIRouter:
    """创建联想商店路由（前缀 ``/api/v1/store/lenovo``）。"""

    router = APIRouter(prefix=STORE_PREFIX, tags=["Lenovo Store"])

    @router.get("/categories", response_model=StoreCategoriesResponse)
    async def get_categories(request: Request, response: Response) -> StoreCategoriesResponse:
        response.headers["Cache-Control"] = NO_STORE
        try:
            return await _service(request).get_categories()
        except LenovoNotFoundError as error:
            raise _not_found(error) from None

    @router.get("/apps", response_model=StoreCatalog)
    async def list_apps(request: Request, response: Response) -> StoreCatalog:
        response.headers["Cache-Control"] = NO_STORE
        # GET 只读内存/磁盘快照（catalog.json），绝不访问联想接口；目录由后台定时刷新维护。
        # 未知查询参数（例如旧客户端可能带上的 ?refresh=true）会被 FastAPI 忽略，不会触发抓取；
        # 手动立即更新只走 POST /store/lenovo/refresh。
        return await _service(request).get_catalog()

    @router.get("/search", response_model=StoreSearchResponse)
    async def search_apps(
        request: Request,
        response: Response,
        q: Annotated[str, Query(min_length=1, max_length=100)],
        limit: Annotated[int, Query(ge=SEARCH_MIN_LIMIT, le=SEARCH_MAX_LIMIT)] = SEARCH_DEFAULT_LIMIT,
    ) -> StoreSearchResponse:
        response.headers["Cache-Control"] = NO_STORE
        try:
            return await _service(request).search(q, limit=limit)
        except LenovoNotFoundError as error:
            raise _not_found(error) from None

    @router.get("/apps/{soft_id}", response_model=StoreAppDetails)
    async def get_app_details(
        request: Request,
        response: Response,
        soft_id: SoftIdPath,
    ) -> StoreAppDetails:
        response.headers["Cache-Control"] = NO_STORE
        try:
            # 详情只读服务端缓存；GET 不提供强制刷新（旧客户端误传的 ?refresh=true 会被忽略）。
            return await _service(request).get_details(soft_id)
        except LenovoNotFoundError as error:
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
        except LenovoNotFoundError as error:
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

    # HEAD 必须注册在 GET 之前：Starlette 会给 GET 路由自动加 HEAD 方法，顺序反了 HEAD
    # 会落到 GET 处理器并白白读完整段视频。HEAD 只解析地址、取上游响应头，读完 0 字节。
    @router.head("/apps/{soft_id}/screenshots/{index}/video", include_in_schema=False)
    async def head_app_screenshot_video(
        request: Request, soft_id: SoftIdPath, index: ScreenshotIndexPath
    ) -> Response:
        service = _service(request)
        try:
            stream = await service.open_screenshot_video(
                soft_id, index, range_header=request.headers.get("range")
            )
        except LenovoNotFoundError as error:
            raise _video_not_found(error) from None
        try:
            return Response(
                status_code=stream.status_code,
                headers={**stream.headers, "Cache-Control": NO_STORE},
            )
        finally:
            await stream.aclose()

    @router.get("/apps/{soft_id}/screenshots/{index}/video")
    async def get_app_screenshot_video(
        request: Request, soft_id: SoftIdPath, index: ScreenshotIndexPath
    ) -> Response:
        """视频流代理：透传 Range，分块转发上游字节，不缓存签名 URL、不落盘、不整体读入内存。"""

        service = _service(request)
        try:
            stream = await service.open_screenshot_video(
                soft_id, index, range_header=request.headers.get("range")
            )
        except LenovoNotFoundError as error:
            raise _video_not_found(error) from None
        return StreamingResponse(
            stream.iter_bytes(),
            status_code=stream.status_code,
            headers={**stream.headers, "Cache-Control": NO_STORE},
        )

    @router.get("/apps/{soft_id}/cover")
    async def get_app_cover(request: Request, soft_id: SoftIdPath) -> Response:
        return await _image_endpoint(request, lambda service: service.get_cover(soft_id))

    # 手动刷新接口已禁用：目录只由后台定时线程刷新，避免 POST 被滥用重复抓取联想接口。
    # @router.post("/refresh")
    # async def refresh_catalog(request: Request, response: Response) -> dict[str, Any]:
    #     response.headers["Cache-Control"] = NO_STORE
    #     try:
    #         return await _service(request).refresh_catalog()
    #     except LenovoNotFoundError as error:
    #         raise _not_found(error) from None

    return router


__all__ = ["IMAGE_CACHE_CONTROL", "NO_STORE", "SOURCE_DISABLED_DETAIL", "STORE_PREFIX", "create_lenovo_router"]
