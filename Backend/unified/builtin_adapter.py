"""内置精选源的统一 API 适配器（``/api/v1/sources/builtin/...``）。

内置精选源的数据与版本解析来自既有模块（不是 HTTP 递归调用！）：

- 目录/详情来自 :class:`Backend.catalog.CatalogRepository`（``builtin.app_ids`` 白名单已由它
  过滤），分类从目录派生；
- 版本与下载地址复用 :class:`Backend.releases.ReleaseService`（与 ``/api/v1/apps``、
  ``/api/v1/apps/{id}/releases/latest`` 完全同一条解析/签名路径）；
- 图标复用 :class:`Backend.icons.IconService`（同一份 ETag/304 与长 TTL 策略）。

能力差异：内置精选没有截图/封面/视频，也不提供在线搜索与目录刷新（``capabilities`` 均为
``false``）；客户端按 ``search_mode=catalog`` 在本地搜索。
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import Any

from .adapters import SourceAdapter
from .errors import SourceNotFoundError, SourceUnavailableError
from .models import (
    SourceAppDetails,
    SourceAppSummary,
    SourceCapabilities,
    SourceCatalog,
    SourceCategoriesResponse,
    SourceCategory,
)

if "." in (__package__ or ""):
    from ..catalog import CatalogRepository
    from ..icons import IconService
    from ..models import AppDefinition, Architecture, Release
    from ..releases import ReleaseService
    from ..warmup import CatalogWarmup
else:
    from catalog import CatalogRepository
    from icons import IconService
    from models import AppDefinition, Architecture, Release
    from releases import ReleaseService
    from warmup import CatalogWarmup


logger = logging.getLogger("winappsmanager.unified.builtin")

SOURCE_ID = "builtin"
SOURCE_NAME = "内置精选"
SOURCE_DESCRIPTION = "项目内置的精选软件目录。"
SOURCE_DISABLED_MESSAGE = "内置精选源未启用。"
UNIFIED_PREFIX = f"/api/v1/sources/{SOURCE_ID}"


class BuiltinSourceAdapter(SourceAdapter):
    """内置精选源的统一接口适配器（catalog/details/download/categories）。"""

    source_id = SOURCE_ID
    source_name = SOURCE_NAME
    source_description = SOURCE_DESCRIPTION
    search_mode = "catalog"
    online_search = False
    is_default = False
    capabilities = SourceCapabilities(
        categories=True,
        catalog=True,
        search=False,
        details=True,
        download=True,
        screenshots=False,
        cover=False,
        video=False,
        refresh=False,
    )

    def __init__(
        self,
        catalog: CatalogRepository,
        releases: ReleaseService,
        warmup: CatalogWarmup,
        icons: IconService,
        settings: Any,
        *,
        enabled: bool = True,
    ) -> None:
        super().__init__(enabled=enabled)
        self._catalog = catalog
        self._releases = releases
        self._warmup = warmup
        self._icons = icons
        self._settings = settings

    # -- 来源描述 ---------------------------------------------------------

    def app_count(self) -> int | None:
        # 与旧 ``/sources`` 行为一致：即使 builtin.enabled=false 也照实返回过滤后的数量。
        return self._catalog.app_count

    def _require(self) -> None:
        if not self.enabled:
            raise SourceUnavailableError(SOURCE_DISABLED_MESSAGE)

    def _drop_unavailable(self) -> bool:
        refresh = getattr(self._settings, "refresh", None)
        return bool(getattr(refresh, "drop_unavailable_apps", True))

    # -- 目录 / 分类 -------------------------------------------------------

    async def list_categories(self) -> SourceCategoriesResponse:
        self._require()
        apps = self._visible_apps()
        ordered: "OrderedDict[str, SourceCategory]" = OrderedDict()
        for app in apps:
            existing = ordered.get(app.category)
            if existing is None:
                ordered[app.category] = SourceCategory(
                    id=app.category, name=app.category, count=1, group=None
                )
            else:
                existing.count += 1
        return SourceCategoriesResponse(source=SOURCE_ID, updated_at=None, categories=list(ordered.values()))

    async def list_apps(self) -> SourceCatalog:
        self._require()
        apps = self._visible_apps()
        # 与旧 ``/apps`` 一致：逐个软件隔离失败，单个来源抓不到版本时仍然保留卡片。
        outcomes = await asyncio.gather(
            *(self._latest_release(app) for app in apps), return_exceptions=True
        )
        items: list[SourceAppSummary] = []
        for app, outcome in zip(apps, outcomes):
            release: Release | None
            if isinstance(outcome, BaseException):
                if isinstance(outcome, asyncio.CancelledError):
                    raise outcome
                logger.warning("内置精选源获取软件 %s 的版本信息失败：%s", app.id, outcome)
                release = None
            else:
                release = outcome
            items.append(self._summary(app, release))
        return SourceCatalog(
            source=SOURCE_ID,
            source_name=SOURCE_NAME,
            updated_at=None,
            stale=False,
            total=len(items),
            items=items,
        )

    # -- 详情 / 下载 -------------------------------------------------------

    async def get_app(self, app_id: str) -> SourceAppDetails:
        self._require()
        app = self._get_app(app_id)
        if self._drop_unavailable() and not self._warmup.is_available(app.id):
            # 与旧 ``/apps/{app_id}`` 一致：预热失败的软件详情返回 503。
            raise SourceUnavailableError("该软件暂时无法获取官方版本信息，请稍后重试。")
        release = await self._latest_release(app)
        return self._details(app, release)

    async def get_download(self, app_id: str, architecture: Architecture) -> Release:
        self._require()
        app = self._get_app(app_id)
        # 与 ``/api/v1/apps/{app_id}/releases/latest`` 一致：下载地址始终重新解析/签名。
        release = await self._releases.latest(app, architecture, force_refresh=True)
        if release is None:
            raise SourceNotFoundError(f"软件 {app.name} 没有适用于 {architecture} 的安装包。")
        return release

    async def icon_response(self, request, app_id: str):
        self._require()
        app = self._get_app(app_id)
        icon_path = self._catalog.icon_path(app)
        if icon_path is None:
            raise SourceNotFoundError("该软件没有配置图标。")
        return self._icons.response(request, icon_path)

    # -- 内部 -------------------------------------------------------------

    def _visible_apps(self) -> list[AppDefinition]:
        """按旧 ``/apps`` 的默认顺序（推荐优先）返回当前暴露的软件。"""

        apps = self._catalog.list_apps(None, None, "recommended")
        if self._drop_unavailable():
            apps = [app for app in apps if self._warmup.is_available(app.id)]
        return apps

    def _get_app(self, app_id: str) -> AppDefinition:
        app = self._catalog.get_app(app_id)
        if app is None:
            raise SourceNotFoundError(f"找不到软件 {app_id}。")
        return app

    async def _latest_release(self, app: AppDefinition) -> Release | None:
        """解析软件的展示版本；失败只记 warning（单个软件失败不影响整个目录）。"""

        try:
            return await self._releases.latest(app, self._preferred_architecture(app))
        except Exception as error:  # noqa: BLE001 - 单个软件失败不能中断目录响应
            logger.warning("内置精选源解析软件 %s 的版本失败：%s", app.id, error)
            return None

    @staticmethod
    def _preferred_architecture(app: AppDefinition) -> Architecture:
        architectures = app.architectures
        if not architectures:
            return "x64"
        return "x64" if "x64" in architectures else architectures[0]

    def _summary(self, app: AppDefinition, release: Release | None) -> SourceAppSummary:
        return SourceAppSummary(**self._summary_fields(app, release))

    def _summary_fields(self, app: AppDefinition, release: Release | None) -> dict[str, Any]:
        return {
            "id": app.id,
            "name": app.name,
            "description": app.description,
            "category": app.category,
            # 内置目录没有单独的分类 id：直接复用分类名，客户端用它做 categories 过滤。
            "category_id": app.category,
            "group": None,
            "version": release.version if release is not None else app.version,
            "size_display": app.size_display,
            "size_bytes": None,
            "publisher": app.publisher,
            "platforms": app.platforms,
            "architectures": list(app.architectures) if app.architectures is not None else None,
            "minimum_windows": app.minimum_windows,
            # 内置精选用后端图标资源；没有独立 CDN，客户端走 icon_proxy_url。
            "icon_url": None,
            "icon_proxy_url": f"{UNIFIED_PREFIX}/apps/{app.id}/icon",
            "icon_text": app.icon_text,
            "icon_background": app.icon_background,
            "tags": list(app.tags),
            "score": None,
            "download_count": None,
            "license": app.license,
            "is_free": None,
            "is_recommended": app.is_recommended,
            "is_beta": False,
            "sort_order": app.sort_order,
            "installation_notes": app.installation_notes,
            # 内置目录自带官网与下载页；其它占位字段保持 None（响应里为 null）。
            "website_url": str(app.website_url),
            "download_page_url": str(app.download_page_url),
        }

    def _details(self, app: AppDefinition, release: Release | None) -> SourceAppDetails:
        return SourceAppDetails(
            **self._summary_fields(app, release),
            long_description=app.long_description,
            release_notes="",
            screenshots=[],
            cover_url=None,
            cover_proxy_url=None,
            labels=[],
            ranking=None,
            detail_tags=[],
        )


__all__ = [
    "SOURCE_DESCRIPTION",
    "SOURCE_ID",
    "SOURCE_NAME",
    "BuiltinSourceAdapter",
]
