"""联想 PC 商店的原始接口客户端。

封装 5 个真实接口（分类树、分类应用列表、应用详情、下载地址列表、应用搜索），负责：

- 给需要认证的请求加 ``token`` 头，并在上游返回 ``token invalid`` 时刷新 token 重试一次；
- 兼容 ``status`` 为数字 ``0`` 与字符串 ``"0"`` 的响应包络，先判 ``status`` 再读 ``data``；
- 把超时映射成 :class:`LenovoTimeoutError`（504），HTTP / 网络错误映射成
  :class:`LenovoUnavailableError`（502），响应结构异常映射成 :class:`LenovoProtocolError`；
- 应用详情下架（``status=-1`` / “软件已下架”）映射成 :class:`LenovoNotFoundError`（404），
  搜索失败同样按“找不到结果”处理。

接口调研见 ``docs/lenovo-store-api.md``；客户端只返回 ``data`` 字段，规范化由上层完成。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import math
import time
from typing import Any
from urllib.parse import urlsplit

import httpx2

from .errors import LenovoNotFoundError, LenovoProtocolError, LenovoTimeoutError, LenovoUnavailableError
from .token import BASE_URL, LenovoTokenManager, coerce_status

COMPOSITE_CLASS_LIST_PATH = "/classifyservice/class/composite_class_list"
CLASS_APP_LIST_PATH = "/classifyservice/class/composite_class_app_list"
APP_DETAILS_PATH = "/appstorecontents/app/details_new"
DOWNLOAD_URL_LIST_PATH = "/dlservice/getPcSoftDownloadUrlList"
SEARCH_PATH = "/searchcontent/fusion/v2/app_search"

TOKEN_INVALID_MARKER = "token invalid"
_OFFLINE_MARKERS = ("下架", "已下线", "不存在")


class LenovoRawClient:
    """联想 PC 商店原始接口的异步客户端（不含缓存）。"""

    def __init__(
        self,
        client: httpx2.AsyncClient,
        *,
        token_manager: LenovoTokenManager | None = None,
        timeout_seconds: float = 20.0,
        connect_timeout_seconds: float = 5.0,
        base_url: str = BASE_URL,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(client, httpx2.AsyncClient):
            raise ValueError("client must be an httpx2.AsyncClient")
        for name, value in (("timeout_seconds", timeout_seconds), ("connect_timeout_seconds", connect_timeout_seconds)):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a finite positive value")
        if self._is_too_long_connect(float(connect_timeout_seconds), float(timeout_seconds)):
            raise ValueError("connect_timeout_seconds cannot exceed timeout_seconds")
        if not callable(clock):
            raise ValueError("clock must be callable")
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url must be a non-empty string")
        parsed = urlsplit(base_url.strip())
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("base_url must be an http(s) URL")

        timeout = float(timeout_seconds)
        connect = float(connect_timeout_seconds)
        self._client = client
        self._timeout_seconds = timeout
        self._connect_timeout_seconds = connect
        self._base_url = base_url.strip().rstrip("/")
        self._token = token_manager if token_manager is not None else LenovoTokenManager(
            client,
            None,
            cache_enabled=False,
            cache_persist=False,
            timeout_seconds=timeout,
            connect_timeout_seconds=connect,
            clock=clock,
            base_url=self._base_url,
        )

    @staticmethod
    def _is_too_long_connect(connect: float, timeout: float) -> bool:
        return connect > timeout

    # -- 5 个原始接口 -----------------------------------------------------

    async def composite_class_list(self) -> list[Any]:
        """分类树：``data`` 是 ``soft`` / ``game`` 两个顶层分组的数组。"""

        data = await self._get_data(
            COMPOSITE_CLASS_LIST_PATH,
            operation="分类树",
            requires_token=True,
        )
        if not isinstance(data, list):
            raise LenovoProtocolError("联想分类树响应结构无效。")
        return data

    async def class_app_list(self, code: str, class_id: str, skip: int = 0, limit: int = 1000) -> dict[str, Any]:
        """某个分类下的应用列表；``skip`` 是真实偏移量，``limit`` 实测最大 1000。"""

        if not isinstance(code, str) or code not in ("soft", "game"):
            raise ValueError("code must be 'soft' or 'game'")
        if not isinstance(class_id, str) or not class_id:
            raise ValueError("class_id must be a non-empty string")
        if type(skip) is not int or skip < 0:
            raise ValueError("skip must be a non-negative integer")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer between 1 and 1000")
        data = await self._get_data(
            CLASS_APP_LIST_PATH,
            params={
                "code": code,
                "id": class_id,
                "pageIndex": "1",
                "type": "pc",
                "tagId": "-1",
                "refresh": "true",
                "skip": str(skip),
                "limit": str(limit),
            },
            operation="分类应用列表",
            requires_token=True,
        )
        if not isinstance(data, dict):
            raise LenovoProtocolError("联想分类应用列表响应结构无效。")
        return data

    async def app_details(self, soft_id: str) -> dict[str, Any]:
        """应用详情；已下架时抛 :class:`LenovoNotFoundError`。"""

        self._require_soft_id(soft_id)
        data = await self._get_data(
            APP_DETAILS_PATH,
            params={"softid": soft_id},
            operation="应用详情",
            requires_token=True,
            offline_on_error=True,
        )
        if not isinstance(data, dict):
            raise LenovoProtocolError("联想应用详情响应结构无效。")
        return data

    async def download_url_list(self, soft_id: str) -> dict[str, Any]:
        """下载地址列表（无需 token，地址带时效，绝不能缓存）。"""

        self._require_soft_id(soft_id)
        data = await self._get_data(
            DOWNLOAD_URL_LIST_PATH,
            params={"softid": soft_id},
            operation="下载地址",
            requires_token=False,
            offline_on_error=True,
        )
        if not isinstance(data, dict):
            raise LenovoProtocolError("联想下载地址响应结构无效。")
        return data

    async def search(self, word: str, limit: int = 30) -> dict[str, Any]:
        """应用搜索；联想实际最多返回约 31 条，``skip`` / ``limit`` 基本不生效。"""

        if not isinstance(word, str) or not word.strip():
            raise ValueError("word must be a non-empty string")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer between 1 and 100")
        data = await self._get_data(
            SEARCH_PATH,
            params={
                "searchWord": word.strip(),
                "searchMode": "0",
                "tab": "0",
                "skip": "0",
                "limit": str(limit),
                "algoBizInfo": "",
                "firstId": "",
                "androidType": "0",
                "type": "0",
                "category": "0",
                "serviceBizInfo": "",
                "wallpaperType": "0",
                "sort": "0",
            },
            operation="应用搜索",
            requires_token=True,
            search_failure_on_error=True,
        )
        if not isinstance(data, dict):
            raise LenovoProtocolError("联想应用搜索响应结构无效。")
        return data

    # -- 传输与响应包络 ---------------------------------------------------

    async def _get_data(
        self,
        path: str,
        *,
        operation: str,
        requires_token: bool,
        params: Mapping[str, str] | None = None,
        offline_on_error: bool = False,
        search_failure_on_error: bool = False,
    ) -> Any:
        token: str | None = None
        if requires_token:
            token = await self._token.get_token()
        attempts = 2 if requires_token else 1
        for attempt in range(attempts):
            headers = {"Accept": "application/json"}
            if token is not None:
                headers["token"] = token
            document = await self._request_document(
                path,
                params=params,
                headers=headers,
                operation=operation,
            )
            status = coerce_status(document.get("status"))
            if status is None:
                raise LenovoProtocolError(f"联想{operation}响应缺少有效的 status。")
            if status == 0:
                return document.get("data")

            message = _text(document.get("message"))
            if token is not None and TOKEN_INVALID_MARKER in message.casefold():
                if attempt == 0:
                    token = await self._token.refresh_token(token)
                    continue
                raise LenovoUnavailableError(f"联想{operation}认证失败：刷新 token 后仍无效。")
            if offline_on_error and _looks_offline(status, message):
                raise LenovoNotFoundError(f"联想{operation}失败：{message or '软件已下架'}。")
            if search_failure_on_error:
                raise LenovoNotFoundError(f"联想{operation}失败：{message or '搜索失败'}。")
            raise LenovoUnavailableError(f"联想{operation}失败：{message or f'status={status}'}。")
        # 循环最多两轮，正常不会走到这里。
        raise LenovoUnavailableError(f"联想{operation}服务暂时不可用。")

    async def _request_document(
        self,
        path: str,
        *,
        params: Mapping[str, str] | None,
        headers: Mapping[str, str],
        operation: str,
    ) -> dict[str, Any]:
        try:
            response = await self._client.get(
                f"{self._base_url}{path}",
                params=dict(params) if params is not None else None,
                headers=dict(headers),
                timeout=self._timeout(),
                follow_redirects=False,
            )
            response.raise_for_status()
        except httpx2.TimeoutException:
            raise LenovoTimeoutError(f"联想{operation}请求超时。") from None
        except httpx2.HTTPStatusError as error:
            raise LenovoUnavailableError(
                f"联想{operation}请求失败（HTTP {error.response.status_code}）。"
            ) from None
        except httpx2.HTTPError:
            raise LenovoUnavailableError(f"联想{operation}服务暂时无法连接。") from None

        try:
            document = response.json()
        except (ValueError, UnicodeError):
            raise LenovoProtocolError(f"联想{operation}响应不是有效的 JSON。") from None
        if not isinstance(document, dict):
            raise LenovoProtocolError(f"联想{operation}响应结构无效。")
        return document

    def _timeout(self) -> httpx2.Timeout:
        return httpx2.Timeout(self._timeout_seconds, connect=self._connect_timeout_seconds)

    @staticmethod
    def _require_soft_id(soft_id: str) -> None:
        if not isinstance(soft_id, str) or not soft_id.strip():
            raise ValueError("soft_id must be a non-empty string")


def _looks_offline(status: int, message: str) -> bool:
    if status == -1:
        return True
    folded = message.casefold()
    return any(marker in message or marker in folded for marker in _OFFLINE_MARKERS)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


__all__ = [
    "APP_DETAILS_PATH",
    "CLASS_APP_LIST_PATH",
    "COMPOSITE_CLASS_LIST_PATH",
    "DOWNLOAD_URL_LIST_PATH",
    "LenovoRawClient",
    "SEARCH_PATH",
]
