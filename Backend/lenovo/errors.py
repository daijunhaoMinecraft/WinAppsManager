"""联想应用商店（lenovo）软件源的异常类型。

这些异常继承 ``Backend/providers/base.py`` 的 :class:`ProviderError` /
:class:`ProviderTimeoutError`，因此 ``Backend/main.py`` 现有的全局异常处理器会把它们
自动映射成 502 / 504。``LenovoNotFoundError`` 由路由自己捕获并转成 404。

调用约定：

- ``LenovoError``：联想上游不可用、协议异常等 502 类错误；
- ``LenovoTimeoutError``：请求超时，映射 504；
- ``LenovoUnavailableError``：上游返回错误状态或网络失败（502）；
- ``LenovoProtocolError``：响应结构 / 字段不符合预期（502）；
- ``LenovoNotFoundError``：应用已下架、不存在或没有对应资源（404）。
"""

from __future__ import annotations

# 与 Backend 其他模块一致：既支持 ``Backend.lenovo`` 子包导入（测试与 uvicorn），
# 也支持 ``python Backend/main.py`` 把 Backend 目录放进 sys.path 后的顶层导入。
if "." in (__package__ or ""):
    from ..providers.base import ProviderError, ProviderTimeoutError
else:
    from providers.base import ProviderError, ProviderTimeoutError


class LenovoError(ProviderError):
    """联想应用商店上游无法提供可用数据。"""


class LenovoTimeoutError(ProviderTimeoutError, LenovoError):
    """联想应用商店请求超时（映射 504）。"""


class LenovoUnavailableError(LenovoError):
    """联想应用商店上游返回错误状态或网络错误（映射 502）。"""


class LenovoProtocolError(LenovoError):
    """联想应用商店响应不符合预期协议（映射 502）。"""


class LenovoNotFoundError(LenovoError):
    """联想应用商店中找不到对应应用或资源（由路由映射 404）。"""


__all__ = [
    "LenovoError",
    "LenovoNotFoundError",
    "LenovoProtocolError",
    "LenovoTimeoutError",
    "LenovoUnavailableError",
]
