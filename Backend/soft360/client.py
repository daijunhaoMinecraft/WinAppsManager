"""360 软件源（360 应用中心）的原始接口客户端。

封装两个**明文 JSON、无需 token/签名**的接口：

- 目录列表 ``GET https://cdn-mgr.soft.360.cn/main/cdn_client/v1/soft/list``：
  ``cate_id`` + ``page_size``（最大 100）+ ``page``（从 1 开始），响应包络是
  ``{"errno": 0, "data": [...]}``，``data`` 为空数组表示该分类没有更多页；
- 软件详情 ``GET https://mgr.soft.360.cn/main/cdn_client/v1/soft/getinfo``：
  ``soft_id`` 是逗号分隔的 ID 列表（单次最多 50 个），``data`` 是
  ``{soft_id: {...}}`` 的对象。

客户端只负责传输、包络与错误映射，不缓存、不规范化字段：

- 超时映射成 :class:`Soft360TimeoutError`（504）；
- HTTP / 网络错误映射成 :class:`Soft360UnavailableError`（502）；
- 响应结构或 ``errno`` 异常映射成 :class:`Soft360ProtocolError`（502）；
- 详情查询里 360 返回失败（``errno != 0``，或是 ``errno=0`` 但 ``data=null``）时映射成
  “没有详情”：``errno != 0`` 抛 :class:`Soft360NotFoundError`（路由 404），``data=null``
  返回空对象由服务层映射 404；因为详情接口只会在 ID 不存在/下架时这样回答。

接口调研结论见任务说明与 ``Backend/README.md`` 的“360 软件源”章节。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
from typing import Any
from urllib.parse import urlsplit

import httpx2

from .errors import (
    Soft360NotFoundError,
    Soft360ProtocolError,
    Soft360TimeoutError,
    Soft360UnavailableError,
)

LIST_BASE_URL = "https://cdn-mgr.soft.360.cn"
DETAILS_BASE_URL = "https://mgr.soft.360.cn"
LIST_PATH = "/main/cdn_client/v1/soft/list"
INFO_PATH = "/main/cdn_client/v1/soft/getinfo"
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 100
# 实测单次 getinfo 超过 51 个 ID 会返回 errno=10002；这里用 50 留出安全余量。
MAX_BATCH_IDS = 50
SOURCE = "web_client"
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0


class Soft360RawClient:
    """360 软件源原始接口的异步客户端（不含缓存）。"""

    def __init__(
        self,
        client: httpx2.AsyncClient,
        *,
        list_base_url: str = LIST_BASE_URL,
        details_base_url: str = DETAILS_BASE_URL,
        timeout_seconds: float = 20.0,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    ) -> None:
        if not isinstance(client, httpx2.AsyncClient):
            raise ValueError("client must be an httpx2.AsyncClient")
        for name, value in (("timeout_seconds", timeout_seconds), ("connect_timeout_seconds", connect_timeout_seconds)):
            if type(value) not in (int, float) or not math.isfinite(float(value)) or value <= 0:
                raise ValueError(f"{name} must be a finite positive value")
        if float(connect_timeout_seconds) > float(timeout_seconds):
            raise ValueError("connect_timeout_seconds cannot exceed timeout_seconds")
        self._client = client
        self._timeout_seconds = float(timeout_seconds)
        self._connect_timeout_seconds = float(connect_timeout_seconds)
        self._list_base_url = _validate_base_url(list_base_url, "list_base_url")
        self._details_base_url = _validate_base_url(details_base_url, "details_base_url")

    # -- 两个原始接口 -----------------------------------------------------

    async def list_softs(self, cate_id: str, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE) -> list[Any]:
        """某个分类的一页软件列表；``data`` 为空数组表示没有更多页。"""

        if not isinstance(cate_id, str) or not cate_id.strip():
            raise ValueError("cate_id must be a non-empty string")
        if type(page) is not int or page < 1:
            raise ValueError("page must be a positive integer")
        if type(page_size) is not int or not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValueError(f"page_size must be an integer between 1 and {MAX_PAGE_SIZE}")
        data = await self._get_data(
            f"{self._list_base_url}{LIST_PATH}",
            params={
                "cate_id": cate_id.strip(),
                "page_size": str(page_size),
                "src": SOURCE,
                "machine": "",
                "page": str(page),
            },
            operation="软件列表",
        )
        if not isinstance(data, list):
            raise Soft360ProtocolError("360 软件列表响应结构无效。")
        return data

    async def get_info(self, soft_ids: Sequence[str]) -> dict[str, Any]:
        """批量查询软件详情；返回 ``{soft_id: {...}}``。

        实测查询不存在/已下架的 ``soft_id`` 时 360 仍返回 ``errno=0`` 且 ``data=null``，
        这里按“没有详情”返回空对象（服务层再映射成 404），而不是把上游的空结果当成协议错误。
        """

        ids = _validate_soft_ids(soft_ids)
        data = await self._get_data(
            f"{self._details_base_url}{INFO_PATH}",
            params={
                "scene": "1",
                "machine": "",
                "soft_id": ",".join(ids),
            },
            operation="软件详情",
            offline_on_error=True,
        )
        if data is None:
            return {}
        if not isinstance(data, Mapping):
            raise Soft360ProtocolError("360 软件详情响应结构无效。")
        return dict(data)

    # -- 传输与响应包络 ---------------------------------------------------

    async def _get_data(
        self,
        url: str,
        *,
        params: Mapping[str, str],
        operation: str,
        offline_on_error: bool = False,
    ) -> Any:
        try:
            response = await self._client.get(
                url,
                params=dict(params),
                headers={"Accept": "application/json"},
                timeout=self._timeout(),
                follow_redirects=True,
            )
            response.raise_for_status()
        except httpx2.TimeoutException:
            raise Soft360TimeoutError(f"360 {operation}请求超时。") from None
        except httpx2.HTTPStatusError as error:
            raise Soft360UnavailableError(
                f"360 {operation}请求失败（HTTP {error.response.status_code}）。"
            ) from None
        except httpx2.HTTPError:
            raise Soft360UnavailableError(f"360 {operation}服务暂时无法连接。") from None

        try:
            document = response.json()
        except (ValueError, UnicodeError):
            raise Soft360ProtocolError(f"360 {operation}响应不是有效的 JSON。") from None
        if not isinstance(document, Mapping):
            raise Soft360ProtocolError(f"360 {operation}响应结构无效。")
        errno = _coerce_int(document.get("errno"))
        if errno is None:
            raise Soft360ProtocolError(f"360 {operation}响应缺少有效的 errno。")
        if errno == 0:
            return document.get("data")
        message = _first_text(document, "errmsg", "message", "msg", "error")
        if offline_on_error:
            raise Soft360NotFoundError(f"360 {operation}失败：{message or f'errno={errno}'}。")
        raise Soft360UnavailableError(f"360 {operation}失败：{message or f'errno={errno}'}。")

    def _timeout(self) -> httpx2.Timeout:
        return httpx2.Timeout(self._timeout_seconds, connect=self._connect_timeout_seconds)


def _validate_base_url(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    text = value.strip().rstrip("/")
    try:
        parsed = urlsplit(text)
    except ValueError:
        raise ValueError(f"{name} must be an http(s) URL") from None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"{name} must be an http(s) URL")
    return text


def _validate_soft_ids(soft_ids: Any) -> list[str]:
    if isinstance(soft_ids, str) or not isinstance(soft_ids, Sequence):
        raise ValueError("soft_ids must be a non-empty sequence of strings")
    values: list[str] = []
    seen: set[str] = set()
    for value in soft_ids:
        if not isinstance(value, str):
            raise ValueError("soft_ids must be a non-empty sequence of strings")
        text = value.strip()
        if not text:
            raise ValueError("soft_ids must be a non-empty sequence of strings")
        if "," in text:
            raise ValueError("soft_ids must not contain commas")
        if text in seen:
            raise ValueError("soft_ids must not contain duplicates")
        seen.add(text)
        values.append(text)
    if not values:
        raise ValueError("soft_ids must be a non-empty sequence of strings")
    if len(values) > MAX_BATCH_IDS:
        raise ValueError(f"soft_ids can contain at most {MAX_BATCH_IDS} entries")
    return values


def _coerce_int(value: Any) -> int | None:
    if type(value) is int:
        return value
    if isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            return int(text)
        except ValueError:
            return None
    return None


def _first_text(document: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = document.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "DETAILS_BASE_URL",
    "INFO_PATH",
    "LIST_BASE_URL",
    "LIST_PATH",
    "MAX_BATCH_IDS",
    "MAX_PAGE_SIZE",
    "Soft360RawClient",
]
