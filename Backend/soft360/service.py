"""360 软件源服务门面（``Backend/soft360/`` 的公开入口）。

``main.py`` 的路由只依赖本模块的 :class:`Soft360StoreService`：

- :meth:`Soft360StoreService.get_catalog` / :meth:`get_categories` / :meth:`get_details` /
  :meth:`search` 返回 :mod:`Backend.soft360.models` 里的规范化模型；
- :meth:`preload_catalog` 启动时只读 ``soft360/catalog.json``（不联网），
  :meth:`start_catalog_refresher` / :meth:`stop_catalog_refresher` 管理一个独立的 daemon
  刷新线程（默认每 ``catalog_refresh_interval_seconds`` 秒一次；线程把刷新协程投递回主事件
  循环执行），:meth:`catalog_status` 供 ``GET /api/v1/cache`` 诊断；
- 请求路径（``GET /apps`` / ``GET /categories``）**只读内存/磁盘快照，绝不访问 360 网络**；
  只有详情/下载/截图接口按需请求 360；
- :meth:`resolve_download` 返回 :class:`Backend.models.Release`（下载地址每轮实时解析，绝不缓存）；
- :meth:`get_icon` / :meth:`get_screenshot` 返回 :class:`Backend.soft360.media.ImagePayload`；
- :meth:`refresh_catalog` / :meth:`clear_caches` / :meth:`cached_app_count` / :meth:`shutdown`
  供 ``POST /store/360/refresh``、``DELETE /cache``、``/sources`` 和 lifespan 使用。

构造参数与联想源一致；``settings`` 是 ``Backend.config.BackendSettings``，这里按鸭子类型读取
``settings.soft360.*``（用 ``getattr`` 兜底默认值），避免 config 与 soft360 之间的循环导入。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import math
from pathlib import Path
import threading
import time
from typing import TYPE_CHECKING, Any

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
    category_specs,
    normalize_soft_id,
)
from .client import Soft360RawClient
from .details import (
    DEFAULT_DETAILS_PERSISTENT_TTL_SECONDS,
    DEFAULT_DETAILS_TTL_SECONDS,
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
    Soft360AppDetails,
    Soft360AppSummary,
    Soft360Catalog,
    Soft360CategoriesResponse,
    Soft360Category,
    Soft360SearchResponse,
)

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
    ) -> None:
        if not isinstance(client, httpx2.AsyncClient):
            raise ValueError("client must be an httpx2.AsyncClient")
        if type(cache_enabled) is not bool or type(cache_persist) is not bool:
            raise ValueError("cache_enabled / cache_persist must be booleans")
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
        """读取详情；软件必须先在当前目录快照里（目录是各分类热门子集）。"""

        self._ensure_open()
        normalized = self._require_soft_id(soft_id)
        summary = self._catalog.find(normalized)
        if summary is None:
            raise Soft360NotFoundError(f"360 软件源目录中找不到 {soft_id}。")
        fields = await self._details.get_fields(normalized)
        return self._details.build_details(summary, fields)

    async def resolve_download(self, soft_id: str, architecture: str = "x64") -> "Release":
        """实时解析下载地址；软件必须先在当前目录快照里。"""

        self._ensure_open()
        normalized = self._require_soft_id(soft_id)
        summary = self._catalog.find(normalized)
        if summary is None:
            raise Soft360NotFoundError(f"360 软件源目录中找不到 {soft_id}。")
        requested = architecture if architecture in _ARCHITECTURES else "x64"
        return await self._details.resolve_download(normalized, summary, requested)

    # -- 搜索（本地匹配，360 没有公开搜索 API）-----------------------------

    async def search(self, query: str, limit: int = SEARCH_DEFAULT_LIMIT) -> Soft360SearchResponse:
        """对当前缓存目录做本地匹配：名称 / 别名 / 描述 / 分类。

        360 页面搜索走 ``enc_client`` 签名接口，本项目不逆向签名，因此搜索只覆盖已抓取的
        热门子集（与 ``GET /apps`` 的目录一致）。
        """

        self._ensure_open()
        text = query.strip() if isinstance(query, str) else ""
        if not text:
            return Soft360SearchResponse(query="", total=0, items=[])
        effective_limit = limit if type(limit) is int else SEARCH_DEFAULT_LIMIT
        effective_limit = max(SEARCH_MIN_LIMIT, min(SEARCH_MAX_LIMIT, effective_limit))
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
        if summary is None:
            raise Soft360NotFoundError(f"360 软件源目录中找不到 {soft_id} 的图标。")
        return await self._media.fetch(summary.icon_url)

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


def create_soft360_store_service(
    client: httpx2.AsyncClient,
    cache_directory: Path | str | None,
    settings: "BackendSettings | Any | None" = None,
    *,
    cache_enabled: bool = True,
    cache_persist: bool = True,
) -> Soft360StoreService:
    """创建 360 软件源服务；``main.py`` 只在 ``soft360.enabled`` 时调用。"""

    return Soft360StoreService(
        client,
        cache_directory,
        settings=settings,
        cache_enabled=cache_enabled,
        cache_persist=cache_persist,
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
    "SOURCE_DESCRIPTION",
    "SOURCE_ID",
    "SOURCE_NAME",
    "Soft360StoreService",
    "create_soft360_store_service",
]
