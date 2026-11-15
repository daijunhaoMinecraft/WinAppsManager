"""统一软件源 API 的跨来源异常。

来源适配器把各来源自己的 ``*NotFoundError`` 等异常翻译成这里的类型，统一路由据此映射
HTTP 状态码；来源自身的 502/504 类异常（``ProviderError`` / ``ProviderTimeoutError`` 的
子类）不翻译，交给 ``Backend/main.py`` 的全局处理器映射。

- :class:`SourceNotFoundError` → 404（应用/截图/封面不存在）
- :class:`SourceUnsupportedError` → 404（来源没有该能力，客户端本应根据 capabilities 不请求）
- :class:`SourceUnavailableError` → 503（来源被禁用或当前不可用）
"""

from __future__ import annotations


class SourceError(Exception):
    """统一软件源 API 的异常基类。"""


class SourceNotFoundError(SourceError):
    """来源中找不到对应的应用或资源（映射 404）。"""


class SourceUnsupportedError(SourceError):
    """来源不支持该能力（映射 404）。"""


class SourceUnavailableError(SourceError):
    """来源被禁用或当前不可用（映射 503）。"""


__all__ = [
    "SourceError",
    "SourceNotFoundError",
    "SourceUnavailableError",
    "SourceUnsupportedError",
]
