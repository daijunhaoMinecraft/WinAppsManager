"""360 软件源服务门面（``Backend/soft360/`` 的公开入口）。

``main.py`` 的路由只依赖本模块的 :class:`Soft360StoreService`：

- :meth:`Soft360StoreService.get_catalog` / :meth:`get_categories` / :meth:`get_details` /
  :meth:`search` 返回 :mod:`Backend.soft360.models` 里的规范化模型；
- :meth:`preload_catalog` 启动时只读 ``soft360/catalog.json``（不联网），
  :meth:`start_catalog_refresher` / :meth:`stop_catalog_refresher` 管理一个独立的 daemon
  刷新线程（默认每 ``catalog_refresh_interval_seconds`` 秒一次；线程把刷新协程投递回主事件
  循环执行），:meth:`catalog_status` 供 ``GET /api/v1/cache`` 诊断；
- 请求路径（``GET /apps`` / ``GET /categories``）**只读内存/磁盘快照，绝不访问 360 网络**；
  详情/下载/图片接口按需请求 360；:meth:`search` 先调用逆向出来的加密搜索接口
  （:class:`~Backend.soft360.search.Soft360SearchClient`，覆盖 360 全量目录），远程失败或
  空结果时回退到已缓存的热门子集做本地匹配；
- 目录外软件（搜索结果里出现、但不在热门子集快照里）也能打开详情/下载/图标/截图：
  详情接口按 ``soft_id`` 实时抓取 ``getinfo``，再用返回字段现场合成
  :class:`Soft360AppSummary`；确实不存在（``errno``/空 ``data``）仍然 404；
- :meth:`resolve_download` 返回 :class:`Backend.models.Release`（下载地址每轮实时解析，绝不缓存）；
- :meth:`get_icon` / :meth:`get_screenshot` 返回 :class:`Backend.soft360.media.ImagePayload`；
- :meth:`refresh_catalog` / :meth:`clear_caches` / :meth:`cached_app_count` / :meth:`shutdown`
  供 ``POST /store/360/refresh``、``DELETE /cache``、``/sources`` 和 lifespan 使用。

构造参数与联想源一致；``settings`` 是 ``Backend.config.BackendSettings``，这里按鸭子类型读取
``settings.soft360.*``（用 ``getattr`` 兜底默认值），避免 config 与 soft360 之间的循环导入。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime, timezone
import logging
import math
from pathlib import Path
import threading
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx2

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查，避免 config 与 soft360 之间的循环导入
    from ..config import BackendSettings

from .catalog import (
    DEFAULT_CATALOG_MAX_STALE_SECONDS,
    DEFAULT_CATALOG_REFRESH_INTERVAL_SECONDS,
    DEFAULT_CATALOG_TTL_SECONDS,
    DEFAULT_CONCURRENCY,
    DEFAULT_PAGES_PER_CATEGORY,
    MAX_PAGES_PER_CATEGORY,
    CatalogSnapshot,
    Soft360CatalogBuilder,
    Soft360CatalogStore,
    app_id_for,
    build_architectures,
    category_specs,
    logo_url_from,
    minimum_windows_from_versions,
    normalize_soft_id,
    parse_score,
    parse_size_bytes,
    truncate_description,
)
from .client import Soft360RawClient
from .details import (
    DEFAULT_DETAILS_PERSISTENT_TTL_SECONDS,
    DEFAULT_DETAILS_TTL_SECONDS,
    Soft360DetailsFields,
    Soft360DetailsProvider,
)
from .errors import (
    Soft360Error,
    Soft360NotFoundError,
    Soft360UnavailableError,
)
from .media import (
    DEFAULT_IMAGE_CACHE_MAX_BYTES,
    DEFAULT_IMAGE_CACHE_TTL_SECONDS,
    ImagePayload,
    Soft360MediaCache,
)
from .models import (
    INSTALLATION_NOTES,
    Soft360AppDetails,
    Soft360AppSummary,
    Soft360Catalog,
    Soft360CategoriesResponse,
    Soft360Category,
    Soft360SearchResponse,
)
from .search import Soft360SearchClient

if TYPE_CHECKING:  # pragma: no cover
    from ..models import Release

logger = logging.getLogger("winappsmanager.soft360")

SOURCE_ID = "soft360"
SOURCE_NAME = "360软件管家"
SOURCE_DESCRIPTION = "360 应用中心的热门软件子集（每个分类前 N 页，默认 3 页）。"
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
SEARCH_MIN_LIMIT = 1
SEARCH_MAX_LIMIT = 50
SEARCH_DEFAULT_LIMIT = 30
# 目录外软件现场合成 summary 时的图标兜底：真实 getinfo 总会带 logo，这里只是防御性占位，
# 让模型契约（icon_url 必须是绝对 http(s) URL）始终成立。图片代理已改为无域名白名单的安全
# 公网校验，这个兜底地址也能由后端代理加载（正常流程不会用到）。
DEFAULT_ICON_URL = "https://www.360.cn/favicon.ico"
# 目录后台刷新线程：名称固定，便于诊断/测试；daemon 保证进程退出不被它拖住。
CATALOG_REFRESHER_THREAD_NAME = "soft360-catalog-refresher"
CATALOG_REFRESHER_JOIN_TIMEOUT_SECONDS = 30.0
_ARCHITECTURES = frozenset({"x64", "x86", "arm64"})


class Soft360StoreService:
    """360 软件源的统一门面（目录 / 详情 / 下载 / 搜索 / 图片 / 后台刷新）。"""

    def __init__(
        self,
        client: httpx2.AsyncClient,
        cache_directory: Path | str | None,
        *,
        settings: "BackendSettings | Any | None" = None,
        cache_enabled: bool = True,
        cache_persist: bool = True,
        search_client: "Soft360SearchClient | Any | None" = None,
    ) -> None:
        if not isinstance(client, httpx2.AsyncClient):
            raise ValueError("client must be an httpx2.AsyncClient")
        if type(cache_enabled) is not bool or type(cache_persist) is not bool:
            raise ValueError("cache_enabled / cache_persist must be booleans")
        if search_client is not None and not callable(getattr(search_client, "search", None)):
            raise ValueError("search_client must provide an async search(keyword) method")
        timeout_seconds = _read_number(settings, "timeout_seconds", DEFAULT_TIMEOUT_SECONDS, positive=True)
        connect_timeout = min(DEFAULT_CONNECT_TIMEOUT_SECONDS, timeout_seconds)
        catalog_ttl = _read_number(settings, "catalog_ttl_seconds", DEFAULT_CATALOG_TTL_SECONDS)
        catalog_refresh_interval = _read_number(
            settings,
            "catalog_refresh_interval_seconds",
            DEFAULT_CATALOG_REFRESH_INTERVAL_SECONDS,
        )
        catalog_max_stale = _read_number(
            settings, "catalog_max_stale_seconds", DEFAULT_CATALOG_MAX_STALE_SECONDS
        )
        details_ttl = _read_number(settings, "details_ttl_seconds", DEFAULT_DETAILS_TTL_SECONDS)
        details_persistent_ttl = _read_number(
            settings, "details_persistent_ttl_seconds", DEFAULT_DETAILS_PERSISTENT_TTL_SECONDS
        )
        pages_per_category = _read_int(
            settings,
            "catalog_pages_per_category",
            DEFAULT_PAGES_PER_CATEGORY,
            minimum=1,
            maximum=MAX_PAGES_PER_CATEGORY,
        )
        concurrency = _read_int(
            settings, "catalog_concurrency", DEFAULT_CONCURRENCY, minimum=1, maximum=16
        )
        image_ttl = _read_number(
            settings, "image_cache_ttl_seconds", DEFAULT_IMAGE_CACHE_TTL_SECONDS
        )
        image_max_bytes = _read_int(
            settings,
            "image_cache_max_bytes",
            DEFAULT_IMAGE_CACHE_MAX_BYTES,
            minimum=1,
            maximum=None,
        )

        directory = _prepare_cache_directory(cache_directory)
        self._http = client
        self._cache_enabled = cache_enabled
        self._directory = directory
        self._settings = settings
        self._closed = False
        self._refresh_count = 0
        self._catalog_refresh_interval = catalog_refresh_interval
        # 目录后台刷新线程：只在 start_catalog_refresher() 后存在，daemon 线程。
        self._catalog_refresher_thread: threading.Thread | None = None
        self._catalog_refresher_stop = threading.Event()
        self._catalog_refresher_interval = catalog_refresh_interval
        self._catalog_next_refresh_at: float | None = None

        self._client = Soft360RawClient(
            client,
            timeout_seconds=timeout_seconds,
            connect_timeout_seconds=connect_timeout,
        )
        self._builder = Soft360CatalogBuilder(
            self._client,
            pages_per_category=pages_per_category,
            concurrency=concurrency,
        )
        self._catalog = Soft360CatalogStore(
            self._builder,
            directory,
            ttl_seconds=catalog_ttl,
            max_stale_seconds=catalog_max_stale,
            cache_enabled=cache_enabled,
            cache_persist=cache_persist,
            refresh_interval_seconds=catalog_refresh_interval,
        )
        self._details = Soft360DetailsProvider(
            self._client,
            directory,
            ttl_seconds=details_ttl,
            persistent_ttl_seconds=details_persistent_ttl,
            cache_enabled=cache_enabled,
            cache_persist=cache_persist,
        )
        # 搜索走加密的 enc_client 接口：复用同一个 httpx2.AsyncClient（连接池、浏览器 UA、
        # soft360.timeout_seconds），不额外创建客户端，也不在启动阶段发起任何请求。
        # 测试可注入任何带 ``async def search(keyword)`` 的替身。
        self._search_client = (
            search_client
            if search_client is not None
            else Soft360SearchClient(
                client,
                timeout_seconds=timeout_seconds,
                connect_timeout_seconds=connect_timeout,
            )
        )
        self._media = Soft360MediaCache(
            client,
            directory,
            ttl_seconds=image_ttl,
            max_bytes=image_max_bytes,
            cache_enabled=cache_enabled,
            cache_persist=cache_persist,
            timeout_seconds=timeout_seconds,
            connect_timeout_seconds=connect_timeout,
        )

    # -- 目录 -------------------------------------------------------------

    async def get_catalog(self) -> Soft360Catalog:
        """读取目录快照；请求路径只读内存/磁盘缓存，**绝不联网**。

        没有任何快照时返回 ``total=0、stale=true`` 的空目录（不是错误），目录由后台刷新线程
        或 ``POST /store/360/refresh`` 更新。
        """

        self._ensure_open()
        snapshot = await self._catalog.get()
        return _catalog_response(snapshot)

    async def get_categories(self) -> Soft360CategoriesResponse:
        """24 个分类与各自在当前快照里的条目数；只读缓存，**绝不联网**。"""

        self._ensure_open()
        snapshot = await self._catalog.get()
        if snapshot is None:
            categories = [
                Soft360Category(id=spec.id, name=spec.name, count=0) for spec in category_specs()
            ]
            return Soft360CategoriesResponse(updated_at=None, total=len(categories), categories=categories)
        categories = list(snapshot.categories)
        return Soft360CategoriesResponse(
            updated_at=_utc_time(snapshot.fetched_at),
            total=len(categories),
            categories=categories,
        )

    async def preload_catalog(self) -> CatalogSnapshot | None:
        """启动时只从 ``soft360/catalog.json`` 预加载目录到内存（不联网）。

        返回载入的快照；没有可用磁盘快照时返回 ``None``。读取失败只记 WARNING，绝不阻止
        后端启动。
        """

        self._ensure_open()
        try:
            snapshot = await self._catalog.preload()
        except Exception:  # noqa: BLE001 - 预加载失败不能影响启动
            logger.warning("360 目录预加载失败，将在后台刷新后可用。", exc_info=True)
            return None
        return snapshot

    async def start_catalog_refresher(self) -> bool:
        """启动独立的 daemon 线程，按 ``catalog_refresh_interval_seconds`` 刷新目录。

        线程只负责计时：到点把 :meth:`refresh_catalog` 协程通过
        ``asyncio.run_coroutine_threadsafe`` 投递回主事件循环执行并等待结果，所有 360 HTTP
        请求都在主事件循环里完成（子线程不直接操作 async 客户端）。

        首次刷新按快照年龄决定：已有快照且年龄 ≥ interval 时立即补刷一次；快照新鲜时等到
        剩余时间；**完全没有快照时等待完整 interval，不在启动阶段联网**。
        """

        self._ensure_open()
        interval = self._catalog_refresh_interval
        if interval <= 0:
            logger.info("360 目录定时刷新未启用：catalog_refresh_interval_seconds 为 0。")
            return False
        thread = self._catalog_refresher_thread
        if thread is not None and thread.is_alive():
            if self._catalog_refresher_interval == interval:
                return True
            await self.stop_catalog_refresher()
        loop = asyncio.get_running_loop()
        first_delay = self._catalog_first_refresh_delay(interval)
        self._catalog_refresher_stop = threading.Event()
        self._catalog_refresher_interval = interval
        self._catalog_next_refresh_at = time.time() + first_delay
        thread = threading.Thread(
            target=self._catalog_refresher_main,
            args=(loop, interval, first_delay),
            name=CATALOG_REFRESHER_THREAD_NAME,
            daemon=True,
        )
        self._catalog_refresher_thread = thread
        thread.start()
        logger.info(
            "360 目录定时刷新线程已启动（%s）：每 %.1f 秒刷新一次，%s。",
            thread.name,
            interval,
            "检测到快照年龄已超过间隔，启动后立即补刷一次"
            if first_delay <= 0
            else f"首次刷新在 {first_delay:.1f} 秒后",
        )
        return True

    def _catalog_first_refresh_delay(self, interval: float) -> float:
        """按当前快照年龄计算第一次刷新前的等待秒数（0 = 立即补刷）。"""

        snapshot = self._catalog.snapshot
        if snapshot is None:
            # 没有任何快照：不在启动阶段联网，等满一个 interval 再尝试。
            return interval
        age = max(0.0, time.time() - snapshot.fetched_at)
        if age >= interval:
            return 0.0
        return max(0.0, interval - age)

    def _catalog_refresher_main(
        self,
        loop: asyncio.AbstractEventLoop,
        interval: float,
        first_delay: float,
    ) -> None:
        """daemon 线程主循环：只做计时与协程投递，失败只记 WARNING、绝不崩线程。"""

        try:
            if first_delay > 0:
                self._catalog_next_refresh_at = time.time() + first_delay
                if self._catalog_refresher_stop.wait(first_delay):
                    return
            while not self._catalog_refresher_stop.is_set():
                self._catalog_next_refresh_at = None
                try:
                    future = asyncio.run_coroutine_threadsafe(self.refresh_catalog(), loop)
                    future.result()
                except (asyncio.CancelledError, TimeoutError):
                    break
                except RuntimeError:
                    # 事件循环已随服务关闭而关闭：没有任何可等待的刷新，直接退出线程。
                    if loop.is_closed():
                        break
                    logger.warning("360 目录定时刷新出现未预期错误，保留旧快照。", exc_info=True)
                except Soft360Error as error:
                    logger.warning("360 目录定时刷新失败，保留旧快照：%s", error)
                except Exception:  # noqa: BLE001 - 单轮失败不能让线程崩掉
                    logger.warning("360 目录定时刷新出现未预期错误，保留旧快照。", exc_info=True)
                if self._catalog_refresher_stop.is_set():
                    break
                self._catalog_next_refresh_at = time.time() + interval
                if self._catalog_refresher_stop.wait(interval):
                    break
        finally:
            self._catalog_next_refresh_at = None
            logger.info("360 目录定时刷新线程已停止。")

    async def stop_catalog_refresher(self) -> None:
        """停止后台刷新线程并等待其退出；join 在线程池里做，不阻塞事件循环。"""

        thread = self._catalog_refresher_thread
        self._catalog_refresher_thread = None
        self._catalog_next_refresh_at = None
        self._catalog_refresher_stop.set()
        if thread is None or not thread.is_alive():
            return
        await asyncio.to_thread(thread.join, CATALOG_REFRESHER_JOIN_TIMEOUT_SECONDS)
        if thread.is_alive():
            logger.warning(
                "360 目录定时刷新线程未能在 %.1f 秒内退出，继续关闭服务。",
                CATALOG_REFRESHER_JOIN_TIMEOUT_SECONDS,
            )

    @property
    def catalog_refresher_thread(self) -> threading.Thread | None:
        """当前目录刷新线程（诊断/测试用；未启动或已停止时为 ``None``）。"""

        return self._catalog_refresher_thread

    def catalog_status(self) -> dict[str, Any]:
        """目录缓存与刷新线程状态（供 ``GET /api/v1/cache``，只读、不触发抓取）。"""

        status = self._catalog.catalog_status()
        thread = self._catalog_refresher_thread
        status["refresher_running"] = thread is not None and thread.is_alive()
        status["refresh_interval_seconds"] = self._catalog_refresher_interval
        status["next_refresh_at"] = (
            _iso_time(self._catalog_next_refresh_at)
            if self._catalog_next_refresh_at is not None
            else None
        )
        return status

    # -- 详情 / 下载 ------------------------------------------------------

    async def get_details(self, soft_id: str) -> Soft360AppDetails:
        """读取详情；目录内外的软件都支持，确实不存在时返回 404。

        目录条目只覆盖各分类热门子集，但搜索接口能返回全量目录里的 ``soft_id``，因此
        这里不再要求 ``soft_id`` 出现在快照里：先实时抓 ``getinfo``，目录外软件再用详情
        字段现场合成 :class:`Soft360AppSummary` 交给 :meth:`Soft360DetailsProvider.build_details`。
        """

        self._ensure_open()
        normalized = self._require_soft_id(soft_id)
        summary = self._catalog.find(normalized)
        fields = await self._details.get_fields(normalized)
        if summary is None:
            # 目录外软件：用实时详情字段合成 summary（图标/分类/架构/最低系统都来自 getinfo）。
            summary = _summary_from_details_fields(normalized, fields)
        return self._details.build_details(summary, fields)

    async def resolve_download(self, soft_id: str, architecture: str = "x64") -> "Release":
        """实时解析下载地址；目录内外都支持，软件不存在/已下架时 404。"""

        self._ensure_open()
        normalized = self._require_soft_id(soft_id)
        summary = self._catalog.find(normalized)
        requested = architecture if architecture in _ARCHITECTURES else "x64"
        return await self._details.resolve_download(normalized, summary, requested)

    # -- 搜索（加密 enc_client 接口 + 本地回退）-----------------------------

    async def search(self, query: str, limit: int = SEARCH_DEFAULT_LIMIT) -> Soft360SearchResponse:
        """搜索 360 全量目录：先走加密搜索接口，失败/空结果时回退缓存目录本地匹配。

        - 远程搜索每次最多返回 50 条，``limit`` 在本地截断（路由限制 1–50）；
        - 远程失败（超时/上游错误/解密失败）只记 WARNING，**绝不把搜索变成 500**；
        - 远程返回空结果或全部条目无法映射时，对已缓存的热门子集做名称/别名/描述/分类匹配，
          目录为空时返回空列表。
        """

        self._ensure_open()
        text = query.strip() if isinstance(query, str) else ""
        if not text:
            return Soft360SearchResponse(query="", total=0, items=[])
        effective_limit = limit if type(limit) is int else SEARCH_DEFAULT_LIMIT
        effective_limit = max(SEARCH_MIN_LIMIT, min(SEARCH_MAX_LIMIT, effective_limit))

        remote_items: list[Any] = []
        try:
            remote_items = await self._search_client.search(text)
        except Soft360Error as error:
            logger.warning("360 远程搜索失败，回退本地目录匹配：%s", error)
        except Exception:  # noqa: BLE001 - 搜索降级失败不能变成 500
            logger.warning("360 远程搜索出现未预期错误，回退本地目录匹配。", exc_info=True)
        if isinstance(remote_items, list) and remote_items:
            items = _summaries_from_search_items(remote_items, effective_limit)
            if items:
                return Soft360SearchResponse(query=text, total=len(items), items=items)
            logger.warning(
                "360 远程搜索返回的 %d 条结果都无法映射，回退本地目录匹配。", len(remote_items)
            )

        snapshot = await self._catalog.get()
        if snapshot is None:
            return Soft360SearchResponse(query=text, total=0, items=[])
        matches = _match_apps(snapshot.apps, text, effective_limit)
        return Soft360SearchResponse(query=text, total=len(matches), items=matches)

    # -- 图片 -------------------------------------------------------------

    async def get_icon(self, soft_id: str) -> ImagePayload:
        self._ensure_open()
        normalized = self._require_soft_id(soft_id)
        summary = self._catalog.find(normalized)
        if summary is not None:
            return await self._media.fetch(summary.icon_url)
        # 目录外软件：图标地址只能从实时详情里取（getinfo 的 logo）。
        fields = await self._details.get_fields(normalized)
        if not fields.icon_url:
            raise Soft360NotFoundError(f"360 软件源中找不到 {soft_id} 的图标。")
        return await self._media.fetch(fields.icon_url)

    async def get_screenshot(self, soft_id: str, index: int) -> ImagePayload:
        self._ensure_open()
        normalized = self._require_soft_id(soft_id)
        if type(index) is not int or index < 0:
            raise Soft360NotFoundError("360 软件截图索引无效。")
        fields = await self._details.get_fields(normalized)
        for screenshot in fields.screenshots:
            if screenshot.index == index:
                return await self._media.fetch(screenshot.source_url)
        raise Soft360NotFoundError(f"360 软件源中找不到 {soft_id} 的截图 {index}。")

    # -- 维护 -------------------------------------------------------------

    async def refresh_catalog(self) -> dict[str, Any]:
        """同步强制刷新目录并写盘（``POST /store/360/refresh`` 与后台刷新线程使用）。"""

        self._ensure_open()
        started = time.monotonic()
        snapshot = await self._catalog.refresh_now()
        self._refresh_count += 1
        elapsed = max(0.0, time.monotonic() - started)
        logger.info("360 目录刷新完成：%s 款，用时 %.2f 秒。", len(snapshot.apps), elapsed)
        return {
            "refreshed": True,
            "total": len(snapshot.apps),
            "categories": len(snapshot.categories),
            "updated_at": _iso_time(snapshot.fetched_at),
            "duration_seconds": round(elapsed, 2),
        }

    async def clear_caches(self) -> dict[str, Any]:
        catalog = await self._catalog.clear()
        details_cleared = await self._details.clear()
        media_cleared = await self._media.clear()
        return {
            "catalog": catalog,
            "details": {"cleared": details_cleared},
            "media": {"cleared": media_cleared},
        }

    def cached_app_count(self) -> int | None:
        """已缓存目录的条目数；目录还没加载时返回 ``None``（不能为统计触发抓取）。"""

        return self._catalog.cached_app_count()

    def cached_updated_at(self) -> datetime | None:
        """已缓存目录的抓取时间；目录还没加载时返回 ``None``（供 ``/sources`` 使用）。"""

        snapshot = self._catalog.snapshot
        if snapshot is None:
            return None
        return _utc_time(snapshot.fetched_at)

    async def shutdown(self) -> None:
        if self._closed:
            return
        # 先停后台刷新线程（set Event + join），再关闭目录缓存。
        await self.stop_catalog_refresher()
        self._closed = True
        await self._catalog.shutdown()

    # -- 内部 -------------------------------------------------------------

    def _ensure_open(self) -> None:
        if self._closed:
            raise Soft360UnavailableError("360 软件源已关闭。")

    @staticmethod
    def _require_soft_id(soft_id: str) -> str:
        normalized = normalize_soft_id(soft_id)
        if normalized is None:
            raise Soft360NotFoundError("360 软件源中的软件 ID 无效。")
        return normalized


def _catalog_response(snapshot: CatalogSnapshot | None) -> Soft360Catalog:
    if snapshot is None:
        # 没有快照时返回空目录（请求路径绝不联网抓取），客户端按“暂无数据”展示。
        return Soft360Catalog(updated_at=None, stale=True, total=0, items=[])
    return Soft360Catalog(
        updated_at=_utc_time(snapshot.fetched_at),
        stale=snapshot.stale,
        total=len(snapshot.apps),
        items=list(snapshot.apps),
    )


def _match_apps(
    apps: tuple[Soft360AppSummary, ...], query: str, limit: int
) -> list[Soft360AppSummary]:
    """对缓存目录做本地模糊匹配，按“名称前缀 > 名称包含 > 别名 > 描述/分类”排序。"""

    needle = query.casefold()
    scored: list[tuple[int, int, str, Soft360AppSummary]] = []
    for app in apps:
        name = app.name.casefold()
        bname = (app.bname or "").casefold()
        description = app.description.casefold()
        category = app.category.casefold()
        if name == needle:
            score = 0
        elif name.startswith(needle):
            score = 1
        elif needle in name:
            score = 2
        elif bname and needle in bname:
            score = 3
        elif needle in description:
            score = 4
        elif needle in category:
            score = 5
        else:
            continue
        scored.append((score, app.sort_order, name, app))
    scored.sort(key=lambda item: (item[0], item[1], item[2]))
    return [item[3] for item in scored[:limit]]


# ---------------------------------------------------------------------------
# 原始搜索结果 / 详情字段 → 规范化 summary
# ---------------------------------------------------------------------------


def _summaries_from_search_items(
    items: list[Any], limit: int
) -> list[Soft360AppSummary]:
    """把加密搜索返回的原始条目映射成 summary（按 soft_id 去重、截断到 ``limit``）。"""

    summaries: list[Soft360AppSummary] = []
    seen: set[str] = set()
    for raw in items:
        summary = _summary_from_search_item(raw, len(summaries))
        if summary is None or summary.soft_id in seen:
            continue
        seen.add(summary.soft_id)
        summaries.append(summary)
        if len(summaries) >= limit:
            break
    return summaries


def _summary_from_search_item(raw: Any, position: int) -> Soft360AppSummary | None:
    """搜索条目字段（``soft_id``/``name``/``logo``/``type_name``/``os`` 等）→ summary。

    实测字段：``soft_id``/``name``/``desc``/``logo``(字符串)/``type_name``/``size``/``score``/
    ``os{bit,ver}``/``soft_baoku_cate_id``/``snapshot``/``free{flag}``；``soft_id`` 与
    ``name`` 缺失时返回 ``None``（调用方跳过）。
    """

    if not isinstance(raw, Mapping):
        return None
    # 只接受 ``type=1`` 的软件条目（广告/活动等其它类型跳过）；字段缺失时按软件处理。
    item_type = raw.get("type")
    if item_type is not None and item_type != 1 and item_type != "1":
        return None
    soft_id = normalize_soft_id(raw.get("soft_id"))
    if soft_id is None:
        return None
    name = _text(raw.get("name"))
    if not name:
        return None
    os_info = raw.get("os")
    bits = os_info.get("bit") if isinstance(os_info, Mapping) else None
    versions = os_info.get("ver") if isinstance(os_info, Mapping) else None
    category = _text(raw.get("type_name")) or "软件"
    category_id = _string(raw.get("soft_baoku_cate_id")) or "0"
    encoded_soft_id = quote(soft_id, safe="")
    return Soft360AppSummary(
        id=app_id_for(soft_id),
        soft_id=soft_id,
        name=name,
        # 搜索条目没有独立的 bname 字段，按契约用 name 填充（客户端 name 优先，不影响展示）。
        bname=_text(raw.get("bname")) or name,
        description=truncate_description(raw.get("desc") or raw.get("brief"), name),
        category=category,
        category_id=category_id,
        size_bytes=parse_size_bytes(raw.get("size")),
        score=parse_score(raw.get("score")),
        icon_url=logo_url_from(raw.get("logo")) or DEFAULT_ICON_URL,
        icon_proxy_url=f"/api/v1/store/360/apps/{encoded_soft_id}/icon",
        architectures=build_architectures(bits),
        minimum_windows=minimum_windows_from_versions(versions) or "7",
        tags=[category],
        license="免费",
        is_free=True,
        sort_order=position,
        installation_notes=INSTALLATION_NOTES,
    )


def _summary_from_details_fields(
    soft_id: str, fields: Soft360DetailsFields
) -> Soft360AppSummary:
    """目录外软件：用 ``getinfo`` 的详情字段现场合成 summary。"""

    name = fields.name or soft_id
    category = fields.category or "软件"
    category_id = fields.category_id or "0"
    encoded_soft_id = quote(soft_id, safe="")
    return Soft360AppSummary(
        id=app_id_for(soft_id),
        soft_id=soft_id,
        name=name,
        bname=fields.bname,
        description=truncate_description(fields.brief or fields.long_description, name),
        category=category,
        category_id=category_id,
        size_bytes=fields.size_bytes,
        score=fields.score,
        icon_url=fields.icon_url or DEFAULT_ICON_URL,
        icon_proxy_url=f"/api/v1/store/360/apps/{encoded_soft_id}/icon",
        architectures=list(fields.architectures) or ["x86", "x64"],
        minimum_windows=fields.minimum_windows or "7",
        tags=[category],
        license="免费",
        is_free=True,
        sort_order=0,
        installation_notes=INSTALLATION_NOTES,
    )


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _string(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if type(value) is int:
        return str(value)
    return ""


def create_soft360_store_service(
    client: httpx2.AsyncClient,
    cache_directory: Path | str | None,
    settings: "BackendSettings | Any | None" = None,
    *,
    cache_enabled: bool = True,
    cache_persist: bool = True,
    search_client: "Soft360SearchClient | Any | None" = None,
) -> Soft360StoreService:
    """创建 360 软件源服务；``main.py`` 只在 ``soft360.enabled`` 时调用。

    ``search_client`` 只供测试注入替身；生产代码由服务自己用同一个 httpx2 客户端创建
    :class:`~Backend.soft360.search.Soft360SearchClient`。
    """

    return Soft360StoreService(
        client,
        cache_directory,
        settings=settings,
        cache_enabled=cache_enabled,
        cache_persist=cache_persist,
        search_client=search_client,
    )


def _read_setting(settings: Any, name: str, default: Any) -> Any:
    section = getattr(settings, "soft360", None) if settings is not None else None
    if section is None:
        return default
    return getattr(section, name, default)


def _read_number(
    settings: Any,
    name: str,
    default: float,
    *,
    positive: bool = False,
) -> float:
    value = _read_setting(settings, name, default)
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        return default
    number = float(value)
    if positive and number <= 0:
        return default
    if number < 0:
        return default
    return number


def _read_int(
    settings: Any,
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int | None,
) -> int:
    value = _read_setting(settings, name, default)
    if type(value) is not int:
        return default
    if value < minimum or (maximum is not None and value > maximum):
        return default
    return value


def _prepare_cache_directory(cache_directory: Path | str | None) -> Path | None:
    """把 ``settings.cache.directory`` 扩展成 ``<cache_dir>/soft360``。"""

    if cache_directory is None:
        return None
    try:
        text = str(Path(cache_directory))
    except TypeError:
        return None
    if not text.strip():
        return None
    return Path(cache_directory).expanduser() / SOURCE_ID


def _utc_time(timestamp: float) -> datetime:
    return datetime.fromtimestamp(float(timestamp), tz=timezone.utc)


def _iso_time(timestamp: float) -> str:
    return _utc_time(timestamp).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "DEFAULT_ICON_URL",
    "SOURCE_DESCRIPTION",
    "SOURCE_ID",
    "SOURCE_NAME",
    "Soft360StoreService",
    "create_soft360_store_service",
]
