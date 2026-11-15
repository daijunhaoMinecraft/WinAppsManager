"""统一软件源 API（契约见 ``docs/source-api.md``）。

客户端只读取 ``GET /api/v1/sources`` 并使用 ``/api/v1/sources/{source_id}/...`` 的统一接口；
新增来源只需要在服务端注册一个新的 :class:`SourceAdapter`（见
:func:`Backend.unified.factory.create_source_registry`），客户端无需改代码。

公开入口：

- :func:`create_source_registry`：在 lifespan 里按当前配置创建来源注册表；
- :func:`create_unified_router`：注册 ``/api/v1/sources`` 与统一子路由；
- :class:`SourceRegistry` / :class:`SourceAdapter`：来源注册与适配器基类；
- :mod:`Backend.unified.models`：统一响应模型（``SourceAppSummary``、``SourceAppDetails``、
  ``SourceCatalog``、``SourceCategoriesResponse``、``SourceSearchResponse`` 等）。
"""

from __future__ import annotations

from .adapters import SourceAdapter, SourceRegistry
from .builtin_adapter import BuiltinSourceAdapter
from .factory import create_source_registry
from .lenovo_adapter import LenovoSourceAdapter
from .models import (
    SourceAppDetails,
    SourceAppSummary,
    SourceCapabilities,
    SourceCatalog,
    SourceCategoriesResponse,
    SourceCategory,
    SourceDetailTag,
    SourceInfo,
    SourceScreenshot,
    SourceSearchResponse,
    SourcesResponse,
)
from .routes import create_unified_router
from .soft360_adapter import Soft360SourceAdapter

__all__ = [
    "BuiltinSourceAdapter",
    "LenovoSourceAdapter",
    "SourceAdapter",
    "SourceAppDetails",
    "SourceAppSummary",
    "SourceCapabilities",
    "SourceCatalog",
    "SourceCategoriesResponse",
    "SourceCategory",
    "SourceDetailTag",
    "SourceInfo",
    "SourceRegistry",
    "SourceScreenshot",
    "SourceSearchResponse",
    "SourcesResponse",
    "Soft360SourceAdapter",
    "create_source_registry",
    "create_unified_router",
]
