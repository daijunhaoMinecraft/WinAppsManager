"""联想应用商店服务门面（``Backend/lenovo/`` 的公开入口）。

``main.py`` 的路由只依赖本模块的 :class:`LenovoStoreService`：

- :meth:`LenovoStoreService.get_catalog` / :meth:`get_categories` / :meth:`get_details` /
  :meth:`search` 返回 :mod:`Backend.lenovo.models` 里的规范化模型；
- :meth:`preload_catalog` 启动时只读 ``lenovo/catalog.json``（不联网），
  :meth:`start_catalog_refresher` / :meth:`stop_catalog_refresher` 管理一个独立的 daemon
  刷新线程（默认每 ``catalog_refresh_interval_seconds`` 秒一次，线程把刷新协程投递回主事件
  循环执行），:meth:`catalog_status` 供 ``GET /api/v1/cache`` 诊断；
- :meth:`resolve_download` 返回 :class:`Backend.models.Release`（下载地址绝不缓存）；
- :meth:`open_screenshot_video` 实时解析视频地址并打开上游流，交给路由按 Range 流式转发
  （视频地址绝不缓存/写日志，响应体也绝不落盘或整体读入内存）；
- :meth:`get_icon` / :meth:`get_screenshot` / :meth:`get_cover` 返回
  :class:`Backend.lenovo.media.ImagePayload`；
- :meth:`refresh_catalog` / :meth:`clear_caches` / :meth:`cached_app_count` / :meth:`shutdown`
  供 ``POST /store/lenovo/refresh``、``DELETE /cache``、``/sources`` 和 lifespan 使用。

构造参数与 ``docs/store-source-design.md`` 3.5 一致；``settings`` 是
``Backend.config.BackendSettings``，这里按鸭子类型读取 ``settings.lenovo.*``（用 ``getattr``
兜底默认值），这样 backend-api 接线前后都能 import，也不会产生循环导入。
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
import concurrent.futures
import contextlib
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import math
from pathlib import Path
import threading
import time
from typing import TYPE_CHECKING, Any

import httpx2

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查，避免 config 与 lenovo 之间的循环导入
    from ..config import BackendSettings

from .catalog import (
    DEFAULT_CATALOG_MAX_STALE_SECONDS,
    DEFAULT_CATALOG_REFRESH_INTERVAL_SECONDS,
    DEFAULT_CATALOG_TTL_SECONDS,
    DEFAULT_CONCURRENCY,
    CatalogSnapshot,
    LenovoCatalogBuilder,
    LenovoCatalogStore,
    build_search_summary,
    normalize_soft_id,
)
from .client import LenovoRawClient
from .details import (
    DEFAULT_DETAILS_PERSISTENT_TTL_SECONDS,
    DEFAULT_DETAILS_TTL_SECONDS,
    LenovoDetailsProvider,
)
from .errors import (
    LenovoError,
    LenovoNotFoundError,
    LenovoProtocolError,
    LenovoTimeoutError,
    LenovoUnavailableError,
)
from .media import (
    DEFAULT_IMAGE_CACHE_MAX_BYTES,
    DEFAULT_IMAGE_CACHE_TTL_SECONDS,
    ImagePayload,
    LenovoMediaCache,
)
from .models import (
    StoreAppDetails,
    StoreAppSummary,
    StoreCatalog,
    StoreCategoriesResponse,
    StoreSearchResponse,
)
from .token import LenovoTokenManager

if TYPE_CHECKING:  # pragma: no cover
    from ..models import Release

logger = logging.getLogger("winappsmanager.lenovo")

SOURCE_ID = "lenovo"
SOURCE_NAME = "联想应用商店"
SOURCE_DESCRIPTION = "联想官方 PC 应用商店目录，含软件与游戏。"
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
DEFAULT_SEARCH_CACHE_TTL_SECONDS = 60.0
DEFAULT_SEARCH_CACHE_MAX_ENTRIES = 64
# 视频代理：连接 5 秒、单次读取 60 秒；视频体积大，读取期限比普通联想请求宽。
DEFAULT_VIDEO_CONNECT_TIMEOUT_SECONDS = 5.0
DEFAULT_VIDEO_READ_TIMEOUT_SECONDS = 60.0
# 上游 401/403/410 说明 auth_key 失效：重新解析一次地址并重试一次。
VIDEO_PROXY_EXPIRED_STATUSES = frozenset({401, 403, 410})
# 可透传给客户端的上游状态：正常/部分内容/范围不可满足。
VIDEO_PROXY_ALLOWED_STATUSES = frozenset({200, 206, 416})
# 只转发这几个上游头；Location、Set-Cookie 等一律不转发（也不缓存）。
_FORWARDED_VIDEO_HEADERS: tuple[str, ...] = (
    "content-type",
    "content-length",
    "content-range",
    "accept-ranges",
)
SEARCH_MIN_LIMIT = 1
SEARCH_MAX_LIMIT = 50
SEARCH_DEFAULT_LIMIT = 30
# 目录后台刷新线程：名称固定，便于诊断/测试；daemon 保证进程退出不被它拖住。
CATALOG_REFRESHER_THREAD_NAME = "lenovo-catalog-refresher"
CATALOG_REFRESHER_JOIN_TIMEOUT_SECONDS = 30.0
_SEARCH_CACHE_TTL = DEFAULT_SEARCH_CACHE_TTL_SECONDS
_ARCHITECTURES = frozenset({"x64", "x86", "arm64"})


@dataclass(frozen=True)
class _SearchEntry:
    expires_at: float
    items: tuple[StoreAppSummary, ...]


class ScreenshotVideoStream:
    """一次已完成握手的视频代理流。

    由 :meth:`LenovoStoreService.open_screenshot_video` 创建；包含上游状态码、可透传的响应头
    和按块读取的字节迭代器。调用方迭代 :meth:`iter_bytes`（迭代结束自动关闭上游），或用
    :meth:`aclose` 显式关闭（HEAD 只取头时）。读流不整体缓冲、不落盘；日志只含
    ``soft_id``/``index``/状态码/字节数/耗时，绝不包含视频 URL 或 ``auth_key``。
    """

    def __init__(
        self,
        *,
        soft_id: str,
        index: int,
        status_code: int,
        headers: dict[str, str],
        response: httpx2.Response,
        stack: contextlib.AsyncExitStack,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.soft_id = soft_id
        self.index = index
        self.status_code = status_code
        self.headers = headers
        self._response = response
        self._stack = stack
        self._clock = clock
        self._started_at = clock()
        self._bytes_sent = 0
        self._closed = False

    async def iter_bytes(self) -> AsyncIterator[bytes]:
        """按上游分块转发字节；结束（含客户端断开）时关闭上游连接并记日志。"""

        try:
            async for chunk in self._response.aiter_bytes():
                if not chunk:
                    continue
                self._bytes_sent += len(chunk)
                yield chunk
        except httpx2.TimeoutException:
            # 不能记录异常文本：httpx2 的异常消息可能带完整上游 URL（含 auth_key）。
            logger.warning(
                "联想视频代理读取超时：soft_id=%s index=%d 已发送=%d 字节。",
                self.soft_id,
                self.index,
                self._bytes_sent,
            )
        except httpx2.HTTPError:
            logger.warning(
                "联想视频代理读取中断：soft_id=%s index=%d 已发送=%d 字节。",
                self.soft_id,
                self.index,
                self._bytes_sent,
            )
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        """幂等关闭上游连接；关闭失败只记 DEBUG 且不带异常文本。"""

        if self._closed:
            return
        self._closed = True
        try:
            await self._stack.aclose()
        except Exception:  # noqa: BLE001 - 响应清理阶段不能让关闭异常再次抛出
            logger.debug("关闭联想视频代理上游连接失败。")
        elapsed = max(0.0, self._clock() - self._started_at)
        logger.info(
            "联想视频代理完成：soft_id=%s index=%d status=%d bytes=%d 用时=%.2f 秒。",
            self.soft_id,
            self.index,
            self.status_code,
            self._bytes_sent,
            elapsed,
        )


class LenovoStoreService:
    """联想应用商店源的统一门面（token / 目录 / 详情 / 下载 / 搜索 / 图片 / 视频代理）。"""

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
        search_ttl = _read_number(
            settings, "search_cache_ttl_seconds", DEFAULT_SEARCH_CACHE_TTL_SECONDS
        )
        concurrency = _read_int(
            settings, "catalog_concurrency", DEFAULT_CONCURRENCY, minimum=1, maximum=6
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
        # 视频代理要直接用底层 AsyncClient 开流（LenovoRawClient 只封装 JSON 接口）。
        self._http = client
        self._cache_enabled = cache_enabled
        self._directory = directory
        self._settings = settings
        self._closed = False
        self._search_ttl = search_ttl
        self._search_cache: OrderedDict[tuple[str, int], _SearchEntry] = OrderedDict()
        self._search_lock = asyncio.Lock()
        self._refresh_count = 0
        self._catalog_refresh_interval = catalog_refresh_interval
        # 目录后台刷新线程：只在 start_catalog_refresher() 后存在，daemon 线程。
        self._catalog_refresher_thread: threading.Thread | None = None
        self._catalog_refresher_stop = threading.Event()
        self._catalog_refresher_future: concurrent.futures.Future[Any] | None = None
        self._catalog_refresher_interval = catalog_refresh_interval
        self._catalog_next_refresh_at: float | None = None

        self._token = LenovoTokenManager(
            client,
            directory,
            cache_enabled=cache_enabled,
            cache_persist=cache_persist,
            timeout_seconds=timeout_seconds,
            connect_timeout_seconds=connect_timeout,
        )
        self._client = LenovoRawClient(
            client,
            token_manager=self._token,
            timeout_seconds=timeout_seconds,
            connect_timeout_seconds=connect_timeout,
        )
        self._builder = LenovoCatalogBuilder(self._client, concurrency=concurrency)
        self._catalog = LenovoCatalogStore(
            self._builder,
            directory,
            ttl_seconds=catalog_ttl,
            max_stale_seconds=catalog_max_stale,
            cache_enabled=cache_enabled,
            cache_persist=cache_persist,
            refresh_interval_seconds=catalog_refresh_interval,
        )
        self._details = LenovoDetailsProvider(
            self._client,
            directory,
            ttl_seconds=details_ttl,
            persistent_ttl_seconds=details_persistent_ttl,
            cache_enabled=cache_enabled,
            cache_persist=cache_persist,
        )
        self._media = LenovoMediaCache(
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

    async def get_catalog(self, *, force_refresh: bool = False) -> StoreCatalog:
        """读取目录快照；请求路径只读内存/磁盘缓存。

        ``force_refresh=True`` 在已有快照时只触发后台刷新并立即返回当前缓存（不等待联网），
        只有完全没有快照的冷启动才会同步抓取一次。
        """

        self._ensure_open()
        snapshot = await self._catalog.get(force_refresh=force_refresh)
        return _catalog_response(snapshot)

    async def get_categories(self, *, force_refresh: bool = False) -> StoreCategoriesResponse:
        self._ensure_open()
        snapshot = await self._catalog.get(force_refresh=force_refresh)
        return StoreCategoriesResponse(
            source=SOURCE_ID,
            updated_at=_utc_time(snapshot.fetched_at),
            groups=list(snapshot.groups),
        )

    async def preload_catalog(self) -> CatalogSnapshot | None:
        """启动时只从 ``lenovo/catalog.json`` 预加载目录到内存（不联网）。

        返回载入的快照；没有可用磁盘快照时返回 ``None``，由首次请求按冷启动兜底抓取。
        读取失败只记 WARNING，绝不阻止后端启动。
        """

        self._ensure_open()
        try:
            snapshot = await self._catalog.preload()
        except Exception:  # noqa: BLE001 - 预加载失败不能影响启动
            logger.warning("联想目录预加载失败，将在首次请求时按需抓取。", exc_info=True)
            return None
        # 预加载的成功/未命中日志由 LenovoCatalogStore.preload() 负责，这里不重复输出。
        return snapshot

    async def start_catalog_refresher(self) -> bool:
        """启动独立的 daemon 线程，按 ``catalog_refresh_interval_seconds`` 刷新目录。

        线程只负责计时：到点把 :meth:`refresh_catalog` 协程通过
        ``asyncio.run_coroutine_threadsafe`` 投递回主事件循环执行并等待结果，所有联想 HTTP
        请求都在主事件循环里完成（子线程不直接操作 async 客户端）。

        首次刷新按快照年龄决定：已有快照且年龄 ≥ interval 时立即补刷一次；快照新鲜时等到
        剩余时间；**完全没有快照时等待完整 interval，不立即联网**（首个请求按冷启动兜底）。
        """

        self._ensure_open()
        interval = self._catalog_refresh_interval
        if interval <= 0:
            logger.info("联想目录定时刷新未启用：catalog_refresh_interval_seconds 为 0。")
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
            "联想目录定时刷新线程已启动（%s）：每 %.1f 秒刷新一次，%s。",
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
            # 没有任何快照：不在启动阶段联网，等满一个 interval 再尝试；首个请求走冷启动兜底。
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
                    self._catalog_refresher_future = future
                    future.result()
                except (asyncio.CancelledError, concurrent.futures.CancelledError):
                    break
                except LenovoError as error:
                    logger.warning("联想目录定时刷新失败，保留旧快照：%s", error)
                except Exception:  # noqa: BLE001 - 单轮失败不能让线程崩掉
                    logger.warning("联想目录定时刷新出现未预期错误，保留旧快照。", exc_info=True)
                finally:
                    self._catalog_refresher_future = None
                if self._catalog_refresher_stop.is_set():
                    break
                self._catalog_next_refresh_at = time.time() + interval
                if self._catalog_refresher_stop.wait(interval):
                    break
        finally:
            self._catalog_next_refresh_at = None
            logger.info("联想目录定时刷新线程已停止。")

    async def stop_catalog_refresher(self) -> None:
        """停止后台刷新线程并等待其退出；join 在线程池里做，不阻塞事件循环。"""

        thread = self._catalog_refresher_thread
        self._catalog_refresher_thread = None
        self._catalog_next_refresh_at = None
        self._catalog_refresher_stop.set()
        future = self._catalog_refresher_future
        self._catalog_refresher_future = None
        if future is not None:
            future.cancel()
        if thread is None or not thread.is_alive():
            return
        await asyncio.to_thread(thread.join, CATALOG_REFRESHER_JOIN_TIMEOUT_SECONDS)
        if thread.is_alive():
            logger.warning(
                "联想目录定时刷新线程未能在 %.1f 秒内退出，继续关闭服务。",
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

    async def get_details(self, soft_id: str, *, force_refresh: bool = False) -> StoreAppDetails:
        self._ensure_open()
        normalized = self._require_soft_id(soft_id)
        summary = self._catalog.find(normalized)
        # 先拉详情：已下架的应用会直接 404，不必为了一个不存在的应用抓完整目录。
        fields = await self._details.get_fields(normalized, force_refresh=force_refresh)
        if summary is None:
            await self.get_catalog()
            summary = self._catalog.find(normalized)
        if summary is None:
            raise LenovoNotFoundError(f"联想应用商店中找不到 {soft_id}。")
        return self._details.build_details(summary, fields)

    async def resolve_download(self, soft_id: str, architecture: str = "x64") -> "Release":
        self._ensure_open()
        normalized = self._require_soft_id(soft_id)
        summary = self._catalog.find(normalized)
        if summary is None:
            await self.get_catalog()
            summary = self._catalog.find(normalized)
        if summary is None:
            raise LenovoNotFoundError(f"联想应用商店中找不到 {soft_id}。")
        requested = architecture if architecture in _ARCHITECTURES else "x64"
        return await self._details.resolve_download(normalized, summary, requested)

    # -- 搜索 -------------------------------------------------------------

    async def search(self, query: str, limit: int = SEARCH_DEFAULT_LIMIT) -> StoreSearchResponse:
        self._ensure_open()
        text = query.strip() if isinstance(query, str) else ""
        if not text:
            return StoreSearchResponse(source=SOURCE_ID, query="", total=0, items=[])
        effective_limit = limit if type(limit) is int else SEARCH_DEFAULT_LIMIT
        effective_limit = max(SEARCH_MIN_LIMIT, min(SEARCH_MAX_LIMIT, effective_limit))
        key = (text.casefold(), effective_limit)

        if self._cache_enabled:
            async with self._search_lock:
                cached = self._search_cache.get(key)
                if cached is not None and self._clock() < cached.expires_at:
                    self._search_cache.move_to_end(key)
                    return StoreSearchResponse(
                        source=SOURCE_ID,
                        query=text,
                        total=len(cached.items),
                        items=list(cached.items),
                    )
                items = await self._search_uncached(text, effective_limit)
                self._search_cache[key] = _SearchEntry(
                    expires_at=self._clock() + self._search_ttl,
                    items=tuple(items),
                )
                self._search_cache.move_to_end(key)
                while len(self._search_cache) > DEFAULT_SEARCH_CACHE_MAX_ENTRIES:
                    self._search_cache.popitem(last=False)
                return StoreSearchResponse(
                    source=SOURCE_ID, query=text, total=len(items), items=list(items)
                )

        items = await self._search_uncached(text, effective_limit)
        return StoreSearchResponse(source=SOURCE_ID, query=text, total=len(items), items=list(items))

    async def _search_uncached(self, query: str, limit: int) -> list[StoreAppSummary]:
        data = await self._client.search(query, limit)
        raw_items = data.get("dataList")
        if raw_items is None:
            raw_items = []
        if not isinstance(raw_items, list):
            raise LenovoProtocolError("联想应用搜索响应结构无效。")
        items: list[StoreAppSummary] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            item_type = raw.get("itemType")
            if item_type is not None and str(item_type) != "1":
                continue
            summary = build_search_summary(raw, len(items))
            if summary is not None:
                items.append(summary)
        return items

    # -- 图片 -------------------------------------------------------------

    async def get_icon(self, soft_id: str) -> ImagePayload:
        self._ensure_open()
        normalized = self._require_soft_id(soft_id)
        summary = self._catalog.find(normalized)
        if summary is None:
            await self.get_catalog()
            summary = self._catalog.find(normalized)
        if summary is None:
            raise LenovoNotFoundError(f"联想应用商店中找不到 {soft_id} 的图标。")
        # 客户端列表已直连联想 CDN 取图标；本代理只在直连失败（icon_proxy_url）时兜底。
        return await self._media.fetch(summary.icon_url)

    async def get_screenshot(self, soft_id: str, index: int) -> ImagePayload:
        self._ensure_open()
        normalized = self._require_soft_id(soft_id)
        if type(index) is not int or index < 0:
            raise LenovoNotFoundError("联想应用截图索引无效。")
        fields = await self._details.get_fields(normalized)
        for screenshot in fields.screenshots:
            if screenshot.index == index:
                return await self._media.fetch(screenshot.source_url)
        raise LenovoNotFoundError(f"联想应用商店中找不到 {soft_id} 的截图 {index}。")

    async def get_cover(self, soft_id: str) -> ImagePayload:
        self._ensure_open()
        normalized = self._require_soft_id(soft_id)
        fields = await self._details.get_fields(normalized)
        if fields.cover_source is None:
            raise LenovoNotFoundError(f"联想应用商店中找不到 {soft_id} 的封面。")
        return await self._media.fetch(fields.cover_source)

    # -- 视频代理 ---------------------------------------------------------

    async def open_screenshot_video(
        self, soft_id: str, index: int, *, range_header: str | None = None
    ) -> ScreenshotVideoStream:
        """实时解析视频地址并打开上游流（不缓存、不整体读入内存），交给路由转发。

        - 上游 ``401/403/410`` 视为 ``auth_key`` 过期：丢弃连接、重新解析一次地址后重试一次；
        - 只有 ``200/206/416`` 会返回给客户端，其它状态抛
          :class:`LenovoUnavailableError`（502），超时抛 :class:`LenovoTimeoutError`（504）；
        - 越界/非视频/地址缺失抛 :class:`LenovoNotFoundError`（路由 404），
          非法地址或视频 CDN 白名单外主机抛 :class:`LenovoProtocolError`（502）；
          （图片 URL 已改为无域名白名单的安全公网校验，视频来源仍保持联想视频 CDN 限制）
        - ``range_header`` 只透传合法的单段 ``bytes=``，非法值直接忽略（上游返回 200 全量）。
        """

        self._ensure_open()
        normalized = self._require_soft_id(soft_id)
        if type(index) is not int or index < 0:
            raise LenovoNotFoundError("联想应用截图索引无效。")
        video_url = await self._details.resolve_screenshot_video_url(normalized, index)
        for attempt in range(2):
            response, stack = await self._open_video_response(video_url, range_header)
            status = response.status_code
            if status in VIDEO_PROXY_EXPIRED_STATUSES and attempt == 0:
                await _close_stack_quietly(stack)
                video_url = await self._details.resolve_screenshot_video_url(normalized, index)
                continue
            if status not in VIDEO_PROXY_ALLOWED_STATUSES:
                await _close_stack_quietly(stack)
                raise LenovoUnavailableError(f"联想视频服务返回 HTTP {status}。")
            return ScreenshotVideoStream(
                soft_id=normalized,
                index=index,
                status_code=status,
                headers=_forwarded_video_headers(response),
                response=response,
                stack=stack,
            )
        # 循环最多两轮：第二轮仍 401/403/410 时会在上面的状态检查里抛 502。
        raise LenovoUnavailableError("联想视频服务暂时不可用。")

    async def _open_video_response(
        self, url: str, range_header: str | None
    ) -> tuple[httpx2.Response, contextlib.AsyncExitStack]:
        # 要求上游按原样传输（不压缩）：否则 aiter_bytes 解压后的字节数与上游
        # Content-Length 不一致，播放器的 Range/长度计算会错位。
        headers = {"Accept": "video/*,*/*;q=0.8", "Accept-Encoding": "identity"}
        normalized_range = _normalize_range_header(range_header)
        if normalized_range is not None:
            headers["Range"] = normalized_range
        stack = contextlib.AsyncExitStack()
        try:
            response = await stack.enter_async_context(
                self._http.stream(
                    "GET",
                    url,
                    headers=headers,
                    timeout=self._video_timeout(),
                    follow_redirects=False,
                )
            )
        except httpx2.TimeoutException:
            await _close_stack_quietly(stack)
            raise LenovoTimeoutError("联想视频请求超时。") from None
        except httpx2.HTTPError:
            await _close_stack_quietly(stack)
            raise LenovoUnavailableError("联想视频服务暂时无法连接。") from None
        return response, stack

    def _video_timeout(self) -> httpx2.Timeout:
        return httpx2.Timeout(
            DEFAULT_VIDEO_READ_TIMEOUT_SECONDS,
            connect=DEFAULT_VIDEO_CONNECT_TIMEOUT_SECONDS,
        )

    # -- 维护 -------------------------------------------------------------

    async def refresh_catalog(self) -> dict[str, Any]:
        """同步强制刷新目录并写盘（``POST /store/lenovo/refresh`` 与后台刷新线程使用）。"""

        self._ensure_open()
        started = time.monotonic()
        snapshot = await self._catalog.refresh_now()
        self._refresh_count += 1
        elapsed = max(0.0, time.monotonic() - started)
        logger.info("联想目录刷新完成：%s 款，用时 %.2f 秒。", len(snapshot.apps), elapsed)
        return {
            "refreshed": True,
            "total": len(snapshot.apps),
            "updated_at": _iso_time(snapshot.fetched_at),
            "duration_seconds": round(elapsed, 2),
        }

    async def clear_caches(self) -> dict[str, Any]:
        catalog = await self._catalog.clear()
        details_cleared = await self._details.clear()
        media_cleared = await self._media.clear()
        search_cleared = len(self._search_cache)
        self._search_cache.clear()
        return {
            "catalog": catalog,
            "details": {"cleared": details_cleared},
            "media": {"cleared": media_cleared},
            "search": {"cleared": search_cleared},
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
        self._search_cache.clear()

    # -- 内部 -------------------------------------------------------------

    def _ensure_open(self) -> None:
        if self._closed:
            raise LenovoUnavailableError("联想应用商店源已关闭。")

    @staticmethod
    def _require_soft_id(soft_id: str) -> str:
        normalized = normalize_soft_id(soft_id)
        if normalized is None:
            raise LenovoNotFoundError("联想应用商店中的应用 ID 无效。")
        return normalized

    def _clock(self) -> float:
        return time.monotonic()


def _catalog_response(snapshot: CatalogSnapshot) -> StoreCatalog:
    return StoreCatalog(
        source=SOURCE_ID,
        source_name=SOURCE_NAME,
        updated_at=_utc_time(snapshot.fetched_at),
        stale=snapshot.stale,
        total=len(snapshot.apps),
        items=list(snapshot.apps),
    )


def create_lenovo_store_service(
    client: httpx2.AsyncClient,
    cache_directory: Path | str | None,
    settings: "BackendSettings | Any | None" = None,
    *,
    cache_enabled: bool = True,
    cache_persist: bool = True,
) -> LenovoStoreService:
    """按设计文档 3.5 创建联想商店服务；``main.py`` 只在 ``lenovo.enabled`` 时调用。"""

    return LenovoStoreService(
        client,
        cache_directory,
        settings=settings,
        cache_enabled=cache_enabled,
        cache_persist=cache_persist,
    )


def _read_setting(settings: Any, name: str, default: Any) -> Any:
    section = getattr(settings, "lenovo", None) if settings is not None else None
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
    """把 ``settings.cache.directory`` 扩展成 ``<cache_dir>/lenovo``。"""

    if cache_directory is None:
        return None
    try:
        text = str(Path(cache_directory))
    except TypeError:
        return None
    if not text.strip():
        return None
    return Path(cache_directory).expanduser() / SOURCE_ID


def _normalize_range_header(value: str | None) -> str | None:
    """只接受单段 ``bytes=start-end`` / ``bytes=start-`` / ``bytes=-suffix``。

    多段 Range、带额外字段或含控制字符的请求头一律忽略（返回 ``None``），不转发给上游。
    """

    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or any(ord(character) < 32 or ord(character) > 126 for character in text):
        return None
    if not text.casefold().startswith("bytes="):
        return None
    spec = text.split("=", 1)[1]
    if not spec or "," in spec:
        return None
    start, dash, end = spec.partition("-")
    if not dash or (not start and not end):
        return None
    if start and not start.isdigit():
        return None
    if end and not end.isdigit():
        return None
    return f"bytes={start}-{end}"


def _forwarded_video_headers(response: httpx2.Response) -> dict[str, str]:
    """从上游响应里挑出可透传的头；视频 CDN 没给 ``Accept-Ranges`` 时对 200/206 补 ``bytes``。"""

    headers: dict[str, str] = {}
    for name in _FORWARDED_VIDEO_HEADERS:
        value = response.headers.get(name)
        if isinstance(value, str) and value.strip():
            headers[name.title()] = value.strip()
    if "Accept-Ranges" not in headers and response.status_code in (200, 206):
        headers["Accept-Ranges"] = "bytes"
    return headers


async def _close_stack_quietly(stack: contextlib.AsyncExitStack) -> None:
    try:
        await stack.aclose()
    except Exception:  # noqa: BLE001 - 丢弃连接失败不应掩盖原始错误
        logger.debug("关闭联想视频代理上游连接失败。")


def _utc_time(timestamp: float) -> datetime:
    return datetime.fromtimestamp(float(timestamp), tz=timezone.utc)


def _iso_time(timestamp: float) -> str:
    return _utc_time(timestamp).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "SOURCE_DESCRIPTION",
    "SOURCE_ID",
    "SOURCE_NAME",
    "LenovoStoreService",
    "ScreenshotVideoStream",
    "create_lenovo_store_service",
]
