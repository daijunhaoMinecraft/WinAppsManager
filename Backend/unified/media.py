"""统一媒体路由的共享辅助：图片 ETag/304、视频流式转发。

联想 / 360 的图片代理返回 :class:`ImagePayload`（``content`` / ``content_type`` / ``etag``），
这里统一构造 ``Cache-Control: public, max-age=86400`` + 带引号 ``ETag`` 的响应，并在
``If-None-Match`` 命中时返回 304（与 ``/store/...`` 旧路由保持完全一致的缓存语义）。
视频则是“一次已完成握手的上游流”，统一路由只按块转发，不缓存、不落盘、不整体读入内存。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from fastapi import Request, Response
from fastapi.responses import StreamingResponse

# 图片缓存策略：与 ``/store/lenovo`` / ``/store/360`` 的图片路由一致。
IMAGE_CACHE_CONTROL = "public, max-age=86400"
# 目录/分类/详情/搜索/下载/刷新/视频代理统一不让中间层缓存。
NO_STORE = "no-store"


@runtime_checkable
class ImagePayloadLike(Protocol):
    """``lenovo.media.ImagePayload`` / ``soft360.media.ImagePayload`` 的结构约定。"""

    content: bytes
    content_type: str
    etag: str


@runtime_checkable
class VideoStreamLike(Protocol):
    """联想视频代理已握手的上游流（``lenovo.service.ScreenshotVideoStream``）。"""

    status_code: int
    headers: dict[str, str]

    def iter_bytes(self) -> AsyncIterator[bytes]:  # pragma: no cover - 结构协议
        ...

    async def aclose(self) -> None:  # pragma: no cover - 结构协议
        ...


def etag_matches(header: str | None, etag: str) -> bool:
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


def image_response(request: Request, payload: ImagePayloadLike) -> Response:
    """构造图片响应：长 TTL + 带引号 ETag，``If-None-Match`` 命中时返回 304。"""

    headers = {
        "Cache-Control": IMAGE_CACHE_CONTROL,
        "ETag": f'"{payload.etag}"',
    }
    if etag_matches(request.headers.get("if-none-match"), payload.etag):
        return Response(status_code=304, headers=headers)
    return Response(content=payload.content, media_type=payload.content_type, headers=headers)


def video_response(stream: VideoStreamLike) -> StreamingResponse:
    """把已握手的上游视频流包装成流式响应（透传 Range 相关头，固定 ``no-store``）。"""

    return StreamingResponse(
        stream.iter_bytes(),
        status_code=stream.status_code,
        headers={**stream.headers, "Cache-Control": NO_STORE},
    )


def video_head_response(stream: VideoStreamLike) -> Response:
    """HEAD 视频请求：只返回与 GET 相同的状态和响应头，不下载视频体。"""

    return Response(
        status_code=stream.status_code,
        headers={**stream.headers, "Cache-Control": NO_STORE},
    )


__all__ = [
    "IMAGE_CACHE_CONTROL",
    "NO_STORE",
    "ImagePayloadLike",
    "VideoStreamLike",
    "etag_matches",
    "image_response",
    "video_head_response",
    "video_response",
]
