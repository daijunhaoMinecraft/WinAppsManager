"""统一软件源适配器工厂（**新增来源的唯一注册点**）。

``Backend/main.py`` 的 lifespan 在创建完目录/provider/版本服务/图标服务/联想与 360 服务之后
调用 :func:`create_source_registry`，把适配器实例交给 :class:`SourceRegistry`；统一路由与
``GET /api/v1/sources`` 全部由注册表驱动。

新增一个软件源（例如 ``myoem``）：

1. 新建 ``Backend/unified/myoem_adapter.py``，实现 :class:`SourceAdapter` 子类（声明
   ``source_id`` / ``source_name`` / ``source_description`` / ``capabilities``，覆盖需要的
   目录/详情/搜索/下载/媒体方法）；
2. 在本文件的 :func:`create_source_registry` 里 ``append`` 一个实例（如果是需要配置/客户端
   的新服务，再按联想/360 的模式在 ``main.py`` lifespan 里创建它并传入）；
3. 需要的话在 ``Backend/config.py`` 增加开关；客户端**不需要任何改动**。

``extra_adapters`` 是给测试/嵌入式扩展用的注册缝：额外适配器追加在三个内置来源之后。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .adapters import SourceAdapter, SourceRegistry
from .builtin_adapter import BuiltinSourceAdapter
from .lenovo_adapter import LenovoSourceAdapter
from .soft360_adapter import Soft360SourceAdapter


def create_source_registry(
    *,
    catalog: Any,
    releases: Any,
    warmup: Any,
    icons: Any,
    settings: Any,
    lenovo_service: Any | None,
    soft360_service: Any | None,
    extra_adapters: Iterable[SourceAdapter] = (),
) -> SourceRegistry:
    """按固定顺序（联想 → 内置精选 → 360）注册三个内置来源，再追加扩展适配器。

    联想 / 360 的来源优先级与旧 ``/sources`` 一致：联想是默认来源，客户端首次启动或当前来源
    不可用时回退到 ``is_default=true`` 的来源。
    """

    adapters: list[SourceAdapter] = [
        LenovoSourceAdapter(lenovo_service, enabled=settings.lenovo.enabled),
        BuiltinSourceAdapter(
            catalog,
            releases,
            warmup,
            icons,
            settings,
            enabled=settings.builtin.enabled,
        ),
        Soft360SourceAdapter(soft360_service, enabled=settings.soft360.enabled),
    ]
    adapters.extend(extra_adapters)
    return SourceRegistry(adapters)


__all__ = ["create_source_registry"]
