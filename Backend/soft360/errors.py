"""360 软件源（``Backend/soft360``）的异常类型。

这些异常继承 ``Backend/providers/base.py`` 的 :class:`ProviderError` /
:class:`ProviderTimeoutError`，因此 ``Backend/main.py`` 现有的全局异常处理器会把它们
自动映射成 502 / 504。``Soft360NotFoundError`` 由路由自己捕获并转成 404。

调用约定：

- ``Soft360Error``：360 上游不可用、协议异常等 502 类错误；
- ``Soft360TimeoutError``：请求超时，映射 504；
- ``Soft360UnavailableError``：上游返回错误状态或网络失败（502）；
- ``Soft360ProtocolError``：响应结构 / 字段不符合预期（502）；
- ``Soft360NotFoundError``：软件已下架、不存在或没有对应资源（404）。
"""

from __future__ import annotations

# 与 Backend 其他模块一致：既支持 ``Backend.soft360`` 子包导入（测试与 uvicorn），
# 也支持 ``python Backend/main.py`` 把 Backend 目录放进 sys.path 后的顶层导入。
if "." in (__package__ or ""):
    from ..providers.base import ProviderError, ProviderTimeoutError
else:
    from providers.base import ProviderError, ProviderTimeoutError


class Soft360Error(ProviderError):
    """360 软件源上游无法提供可用数据。"""


class Soft360TimeoutError(ProviderTimeoutError, Soft360Error):
    """360 软件源请求超时（映射 504）。"""


class Soft360UnavailableError(Soft360Error):
    """360 软件源上游返回错误状态或网络错误（映射 502）。"""


class Soft360ProtocolError(Soft360Error):
    """360 软件源响应不符合预期协议（映射 502）。"""


class Soft360NotFoundError(Soft360Error):
    """360 软件源中找不到对应软件或资源（由路由映射 404）。"""


__all__ = [
    "Soft360Error",
    "Soft360NotFoundError",
    "Soft360ProtocolError",
    "Soft360TimeoutError",
    "Soft360UnavailableError",
]
