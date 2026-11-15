"""统一软件源适配器接口与注册表。

新增一个软件源**只需要在服务端做两件事**：

1. 实现一个 :class:`SourceAdapter` 子类（声明来源描述、``capabilities``，并实现目录/详情/
   搜索/下载等能力）；
2. 在 :func:`Backend.unified.factory.create_source_registry` 里注册一个实例。

统一路由 ``/api/v1/sources/{source_id}/...`` 与 ``/api/v1/sources`` 都由注册表驱动，客户端
不需要任何改动（它只读 ``/sources`` 的 ``capabilities`` 和各统一接口）。

适配器方法约定：

- 成功时返回 :mod:`Backend.unified.models` 的模型（媒体方法直接返回 ``fastapi.Response``）；
- 来源内“找不到应用/资源”必须翻译成 :class:`SourceNotFoundError`；
- 来源不支持的能力抛 :class:`SourceUnsupportedError`（统一路由通常已按 capabilities 提前
  返回 404）；
- 上游 502/504 类异常（``ProviderError`` 子类）原样抛出，由 ``main.py`` 全局处理器映射。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from .errors import SourceUnsupportedError
from .models import (
    SourceAppDetails,
    SourceCapabilities,
    SourceCatalog,
    SourceCategoriesResponse,
    SourceInfo,
    SourceSearchResponse,
    SourcesResponse,
)

if TYPE_CHECKING:  # pragma: no cover - 仅类型提示，避免运行时循环导入
    from fastapi import Request, Response

    from ..models import Architecture, Release
    from .media import VideoStreamLike


class SourceAdapter:
    """一个软件源的统一接口适配器（子类覆盖类属性与需要的能力方法）。"""

    #: 来源 id（小写），同时是统一路由的一级路径参数。
    source_id: str = ""
    #: 展示名称（``/sources`` 下发，客户端不再硬编码）。
    source_name: str = ""
    #: 一句话描述（``/sources`` 下发）。
    source_description: str = ""
    #: ``catalog`` = 目录完整、客户端本地搜索；``api`` = 目录是子集、必须调用服务端搜索。
    search_mode: Literal["catalog", "api"] = "catalog"
    #: 旧客户端兼容字段。
    online_search: bool = False
    #: 是否默认来源（客户端首次启动/原来源不可用时回退到它）。
    is_default: bool = False
    #: 该来源支持的能力；客户端据此决定请求哪些统一接口。
    capabilities: SourceCapabilities = SourceCapabilities()

    def __init__(self, *, enabled: bool = True) -> None:
        if type(enabled) is not bool:
            raise ValueError("enabled 必须是布尔值")
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        """来源是否启用；未启用的来源在 ``/sources`` 里 ``enabled=false``，统一路由返回 404。"""

        return self._enabled

    def app_count(self) -> int | None:
        """已缓存/可用的应用数量；无法统计时返回 ``None``（不允许为统计触发抓取）。"""

        return None

    def updated_at(self) -> datetime | None:
        """目录最近更新时间；未知时返回 ``None``。"""

        return None

    def info(self) -> SourceInfo:
        """构造 ``/api/v1/sources`` 里的来源描述。"""

        return SourceInfo(
            id=self.source_id,
            name=self.source_name,
            description=self.source_description,
            enabled=self._enabled,
            search_mode=self.search_mode,
            online_search=self.online_search,
            app_count=self.app_count(),
            is_default=self.is_default,
            updated_at=self.updated_at(),
            capabilities=self.capabilities,
        )

    # -- 能力方法：默认“不支持”，由具体来源覆盖 -------------------------------

    async def list_categories(self) -> SourceCategoriesResponse:
        """分类列表；没有分类的来源返回空数组（客户端显示“全部”）。"""

        return SourceCategoriesResponse(source=self.source_id, categories=[])

    async def list_apps(self) -> SourceCatalog:
        raise SourceUnsupportedError(f"软件源 {self.source_id} 不支持目录接口。")

    async def search(self, query: str, limit: int) -> SourceSearchResponse:
        raise SourceUnsupportedError(f"软件源 {self.source_id} 不支持服务端搜索。")

    async def get_app(self, app_id: str) -> SourceAppDetails:
        raise SourceUnsupportedError(f"软件源 {self.source_id} 不支持详情接口。")

    async def get_download(self, app_id: str, architecture: "Architecture") -> "Release":
        raise SourceUnsupportedError(f"软件源 {self.source_id} 不支持下载接口。")

    async def refresh(self) -> dict:
        raise SourceUnsupportedError(f"软件源 {self.source_id} 不支持刷新。")

    async def icon_response(self, request: "Request", app_id: str) -> "Response":
        raise SourceUnsupportedError(f"软件源 {self.source_id} 不提供图标。")

    async def screenshot_response(self, request: "Request", app_id: str, index: int) -> "Response":
        raise SourceUnsupportedError(f"软件源 {self.source_id} 不支持截图。")

    async def cover_response(self, request: "Request", app_id: str) -> "Response":
        raise SourceUnsupportedError(f"软件源 {self.source_id} 不支持封面。")

    async def open_video(
        self, app_id: str, index: int, *, range_header: str | None = None
    ) -> "VideoStreamLike":
        raise SourceUnsupportedError(f"软件源 {self.source_id} 不支持视频。")


class SourceRegistry:
    """按注册顺序保存来源适配器，``/sources`` 与统一路由都从这里取来源。"""

    def __init__(self, adapters: Iterable[SourceAdapter]) -> None:
        items = tuple(adapters)
        if not items:
            raise ValueError("至少需要注册一个软件源适配器")
        known: list[str] = []
        for adapter in items:
            if not isinstance(adapter, SourceAdapter):
                raise ValueError(f"无效的软件源适配器：{adapter!r}")
            if not adapter.source_id:
                raise ValueError("软件源适配器缺少 source_id")
            if adapter.source_id in known:
                raise ValueError(f"重复注册软件源：{adapter.source_id}")
            known.append(adapter.source_id)
        self._adapters = items
        self._by_id = {adapter.source_id: adapter for adapter in items}

    @property
    def adapters(self) -> tuple[SourceAdapter, ...]:
        return self._adapters

    def get(self, source_id: str) -> SourceAdapter | None:
        return self._by_id.get(source_id)

    def sources_response(self) -> SourcesResponse:
        """构造 ``GET /api/v1/sources`` 的响应（顺序 = 注册顺序）。"""

        items = [adapter.info() for adapter in self._adapters]
        return SourcesResponse(items=items, total=len(items))


__all__ = ["SourceAdapter", "SourceRegistry"]
