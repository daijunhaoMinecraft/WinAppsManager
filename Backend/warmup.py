"""启动预热与软件可用性注册表。

后端启动时先对每个由下载提供器管理版本的软件抓取一次官网资料：

* 抓取成功（来源为 ``provider``）→ 标记 ``ok`` 并写入缓存；
* 抓取失败但磁盘上仍有快照（``stale`` / ``persistent``，含已过期快照）→ 回退到快照，
  标记 ``cached``，软件仍然可以列出；
* 抓取失败且没有任何兜底数据 → 标记 ``unavailable``，之后 API 层会把该软件从目录中隐藏。

目录里声明的静态版本软件（``download_provider is None``）不需要访问网络，直接以目录内的
版本标记 ``ok``。

本模块只负责“抓取一次 + 记录可用性”。定时重复刷新由 ``scheduler.PeriodicRefresher``
复用 :meth:`CatalogWarmup.warm_all` 完成，因此 ``warm_all`` 可以重复调用，后一次结果覆盖
前一次。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
import logging
import math
import time
from typing import Any, Literal

if __package__:
    from .cache import ReleaseMetadataCache
    from .catalog import CatalogRepository
    from .models import AppDefinition, Release
    from .providers.base import ReleaseProvider
else:
    from cache import ReleaseMetadataCache
    from catalog import CatalogRepository
    from models import AppDefinition, Release
    from providers.base import ReleaseProvider


logger = logging.getLogger("winappsmanager.warmup")

AppStatus = Literal["ok", "cached", "unavailable"]

TIMEOUT_ERROR_MESSAGE = "获取软件安装包信息超时，请稍后重试。"
UNKNOWN_PROVIDER_ERROR_TEMPLATE = "未注册的下载提供器：{0}"


@dataclass(frozen=True)
class AppAvailability:
    """单个软件最近一次预热的结论。"""

    app_id: str
    status: AppStatus
    releases: tuple[Release, ...] = ()
    error: str | None = None
    checked_at: float = 0.0


@dataclass(frozen=True)
class WarmupReport:
    """一次预热（启动预热或定时刷新）的整体结果。"""

    started_at: float
    duration_seconds: float
    results: tuple[AppAvailability, ...]

    def summary(self) -> dict:
        """返回给日志/接口使用的汇总计数。"""

        counts = _count_statuses(self.results)
        return {
            "total": len(self.results),
            "ok": counts["ok"],
            "cached": counts["cached"],
            "unavailable": counts["unavailable"],
            "duration_seconds": self.duration_seconds,
            "started_at": self.started_at,
        }


def _count_statuses(results: Iterable[AppAvailability]) -> dict[str, int]:
    counts = {"ok": 0, "cached": 0, "unavailable": 0}
    for result in results:
        counts[result.status] += 1
    return counts


def _source_name(source: Any) -> str:
    """``MetadataSource`` 枚举统一取字符串值，缺省时返回空字符串。"""

    value = getattr(source, "value", source)
    return value if isinstance(value, str) else ""


def _status_for_source(source: str, degraded: bool) -> AppStatus:
    """把缓存来源映射成对外的可用状态。

    ``provider`` 是本次真正抓到的官网数据；``memory`` 是仍在有效期内的内存快照，两者都
    算正常可用。``stale`` / ``persistent`` 是回退结果，标记为 ``cached``。没有可用来源但
    也没有报错时按成功处理（例如提供器当前确实没有安装包）。
    """

    if source in ("stale", "persistent", "cached"):
        return "cached"
    if degraded:
        return "cached"
    return "ok"


def _error_text(error: BaseException) -> str:
    text = str(error).strip()
    return text or type(error).__name__


class CatalogWarmup:
    """启动预热：为每个软件抓取一次官方版本并记录可用性。"""

    def __init__(
        self,
        catalog: CatalogRepository,
        providers: Mapping[str, ReleaseProvider],
        metadata_cache: ReleaseMetadataCache,
        *,
        timeout_seconds: float = 10.0,
        max_concurrency: int = 4,
        use_stale_cache: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if (
            type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be finite and positive")
        if type(max_concurrency) is not int or max_concurrency <= 0:
            raise ValueError("max_concurrency must be a positive integer")
        if type(use_stale_cache) is not bool:
            raise ValueError("use_stale_cache must be a boolean")
        if not callable(clock):
            raise ValueError("clock must be callable")
        self._catalog = catalog
        self._providers = providers
        self._metadata_cache = metadata_cache
        self.timeout_seconds = float(timeout_seconds)
        self.max_concurrency = max_concurrency
        self.use_stale_cache = use_stale_cache
        self._clock = clock
        # 并发闸门只在这里使用；warm_all 只负责 gather，避免嵌套获取造成死锁。
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._availability: dict[str, AppAvailability] = {}

    async def warm_all(
        self, *, force_refresh: bool = True, app_ids: Iterable[str] | None = None
    ) -> WarmupReport:
        """抓取全部（或指定子集）软件，返回本次预热的汇总。"""

        started_at = self._clock()
        apps = self._select_apps(app_ids)
        results = tuple(
            await asyncio.gather(
                *(self.warm_app(app, force_refresh=force_refresh) for app in apps)
            )
        )
        duration = max(0.0, self._clock() - started_at)
        report = WarmupReport(started_at=started_at, duration_seconds=duration, results=results)
        counts = _count_statuses(results)
        logger.info(
            "预热完成：共 %d 个软件，成功 %d，复用持久缓存 %d，不可用 %d，用时 %.2f 秒。",
            len(results),
            counts["ok"],
            counts["cached"],
            counts["unavailable"],
            duration,
        )
        return report

    async def warm_app(
        self, app: AppDefinition, *, force_refresh: bool = True
    ) -> AppAvailability:
        """抓取单个软件。除取消外绝不抛异常，失败统一转成 ``unavailable``。

        ``force_refresh`` 默认为真，保证启动预热一定会抓一次官网，而不是直接吃磁盘快照。
        """

        if app.download_provider is None:
            availability = AppAvailability(
                app_id=app.id,
                status="ok",
                releases=tuple(app.releases),
                error=None,
                checked_at=self._clock(),
            )
            self._remember(availability)
            logger.debug("软件 %s 使用目录内置版本，跳过抓取。", app.id)
            return availability

        async with self._semaphore:
            availability = await self._probe(app, force_refresh=force_refresh)
        self._remember(availability)
        return availability

    def availability(self, app_id: str) -> AppAvailability | None:
        """最近一次预热的结论；尚未预热过的软件返回 ``None``。"""

        return self._availability.get(app_id)

    def is_available(self, app_id: str) -> bool:
        """未知软件默认可用，保证没有预热时行为与之前一致。"""

        result = self._availability.get(app_id)
        return True if result is None else result.status != "unavailable"

    def available_app_ids(self) -> frozenset[str]:
        """当前可用（``ok`` 或 ``cached``）的软件 ID 集合。"""

        return frozenset(
            app_id for app_id, result in self._availability.items() if result.status != "unavailable"
        )

    def report(self) -> dict:
        """把当前注册表整理成可直接返回给接口的结构。"""

        apps = {
            app_id: {
                "status": result.status,
                "error": result.error,
                "checked_at": result.checked_at,
                "release_count": len(result.releases),
            }
            for app_id, result in sorted(self._availability.items())
        }
        return {"apps": apps, "counts": _count_statuses(self._availability.values())}

    async def _probe(
        self, app: AppDefinition, *, force_refresh: bool
    ) -> AppAvailability:
        provider_id = app.download_provider
        provider = self._providers.get(provider_id) if provider_id is not None else None
        if provider is None:
            error = UNKNOWN_PROVIDER_ERROR_TEMPLATE.format(provider_id)
            logger.warning("软件 %s 不可用：%s", app.id, error)
            return self._unavailable(app, error)

        async def loader() -> list[Release]:
            # 必须强制刷新，否则重启后只会读到磁盘快照而不去抓官网。
            return await provider.get_releases(force_refresh=True)

        try:
            outcome = await asyncio.wait_for(
                self._metadata_cache.fetch(
                    provider_id,
                    loader,
                    force_refresh=force_refresh,
                    allow_stale=self.use_stale_cache,
                ),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "软件 %s 预热超时（超过 %.2f 秒），先标记为不可用。", app.id, self.timeout_seconds
            )
            return self._unavailable(app, TIMEOUT_ERROR_MESSAGE)
        except asyncio.CancelledError:
            # 取消必须原样抛出，否则服务关闭时的取消失效。
            raise
        except Exception as error:  # 单个软件失败不能中断整次预热。
            logger.warning("软件 %s 预热失败，标记为不可用：%s", app.id, _error_text(error))
            return self._unavailable(app, _error_text(error))

        releases = tuple(outcome.releases)
        source = _source_name(getattr(outcome, "source", None))
        status = _status_for_source(source, bool(getattr(outcome, "degraded", False)))
        if status == "cached":
            if source == "stale":
                logger.warning(
                    "软件 %s 抓取失败，已回退到陈旧快照（共 %d 个安装包）。",
                    app.id,
                    len(releases),
                )
            else:
                logger.info(
                    "软件 %s 复用持久缓存（来源 %s，共 %d 个安装包）。",
                    app.id,
                    source or "unknown",
                    len(releases),
                )
        else:
            logger.info("软件 %s 预热成功：来源 %s，共 %d 个安装包。", app.id, source or "unknown", len(releases))
        return AppAvailability(
            app_id=app.id,
            status=status,
            releases=releases,
            error=None,
            checked_at=self._clock(),
        )

    def _unavailable(self, app: AppDefinition, error: str) -> AppAvailability:
        return AppAvailability(
            app_id=app.id,
            status="unavailable",
            releases=(),
            error=error,
            checked_at=self._clock(),
        )

    def _remember(self, availability: AppAvailability) -> None:
        # 重复预热时直接覆盖，注册表始终只保留最近一次结论。
        self._availability[availability.app_id] = availability

    def _select_apps(self, app_ids: Iterable[str] | None) -> tuple[AppDefinition, ...]:
        apps = tuple(self._catalog.apps)
        if app_ids is None:
            return apps
        wanted = {app_id for app_id in app_ids if isinstance(app_id, str) and app_id}
        # 保持目录顺序，未知 ID 直接忽略。
        return tuple(app for app in apps if app.id in wanted)
