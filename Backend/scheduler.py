"""定时刷新调度：按配置间隔重新抓取所有 provider 的最新版本。

本模块只决定“什么时候抓”以及“同一时间只允许一轮抓取”，真正的抓取动作由
``Backend/warmup.py`` 的 ``CatalogWarmup`` 完成（鸭子类型：``async def
warm_all(*, force_refresh=True, app_ids=None)``）。因此这里不读取配置文件、不构造
``CatalogWarmup``，也不依赖 ``Backend.main``。

单飞（single-flight）规则：

* ``refresh_now()`` 与循环内部共用同一把 ``asyncio.Lock``；
* 已有一轮在跑时，新的调用立即返回 ``None`` 并计入 ``coalesced``；
* 被合并的轮次不会排队补跑，下一轮仍按原间隔到来。

失败处理：provider 抓取失败只记录并计入 ``failures``，循环继续运行；只有
``asyncio.CancelledError`` 会原样抛出，``stop()`` 依赖它结束循环。
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅类型检查时导入；warmup.py 尚未落地也不能让本模块导入失败
    if __package__:
        from .warmup import CatalogWarmup
    else:
        from warmup import CatalogWarmup


LOGGER = logging.getLogger("winappsmanager.refresh")


class PeriodicRefresher:
    """按固定间隔重跑 ``warm_all`` 的调度器。

    ``interval_seconds`` 为 0（或负数）表示禁用定时刷新：``start()`` 不创建任务、
    ``running`` 保持 ``False``，但 ``refresh_now()`` 仍可手动触发一轮。
    ``jitter_seconds`` 为正数时每轮等待时间落在 ``[interval, interval + jitter)``
    之间，避免多个进程在同一时刻一起抓取。
    """

    def __init__(
        self,
        warmup: CatalogWarmup,
        *,
        interval_seconds: float,
        jitter_seconds: float = 0.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        random: Callable[[], float] = random.random,
    ) -> None:
        if type(interval_seconds) not in (int, float) or not math.isfinite(interval_seconds):
            raise ValueError("interval_seconds 必须是有限数字")
        if type(jitter_seconds) not in (int, float) or not math.isfinite(jitter_seconds):
            raise ValueError("jitter_seconds 必须是有限数字")
        if not callable(sleep):
            raise ValueError("sleep 必须可调用")
        if not callable(clock):
            raise ValueError("clock 必须可调用")
        if not callable(random):
            raise ValueError("random 必须可调用")
        self._warmup = warmup
        self._interval_seconds = float(interval_seconds)
        self._jitter_seconds = float(jitter_seconds)
        self._sleep = sleep
        self._clock = clock
        self._random = random
        # 单飞闸门：refresh_now 与循环内部共用。
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._runs = 0
        self._failures = 0
        self._coalesced = 0
        self._last_run_at: float | None = None
        self._last_duration_seconds: float | None = None
        self._last_error: str | None = None

    @property
    def interval_seconds(self) -> float:
        """配置的刷新间隔（秒）。"""

        return self._interval_seconds

    @property
    def jitter_seconds(self) -> float:
        """配置的抖动上限（秒）。"""

        return self._jitter_seconds

    @property
    def enabled(self) -> bool:
        """是否启用了定时刷新（``interval_seconds > 0``）。"""

        return self._interval_seconds > 0.0

    @property
    def running(self) -> bool:
        """调度循环当前是否在运行。"""

        task = self._task
        return task is not None and not task.done()

    def start(self) -> None:
        """启动周期循环；幂等，重复调用不会创建第二个任务。"""

        if not self.enabled:
            LOGGER.info("定时刷新未启用：refresh.interval_seconds 为 %s，不启动循环。", self._interval_seconds)
            return
        if self.running:
            return
        self._task = asyncio.get_running_loop().create_task(self._run_loop(), name="catalog-refresh-loop")
        LOGGER.info(
            "定时刷新已启动：间隔 %.1f 秒，抖动 %.1f 秒。",
            self._interval_seconds,
            max(0.0, self._jitter_seconds),
        )

    async def stop(self) -> None:
        """停止周期循环；幂等，未启动或被重复调用都安全。"""

        task = self._task
        self._task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        # 循环任务因取消而结束时，CancelledError 会作为结果返回，不会再次上抛。
        outcomes = await asyncio.gather(task, return_exceptions=True)
        outcome = outcomes[0] if outcomes else None
        if isinstance(outcome, BaseException) and not isinstance(outcome, asyncio.CancelledError):
            LOGGER.warning("定时刷新循环异常结束：%s", outcome)
        LOGGER.info("定时刷新已停止。")

    async def refresh_now(self) -> object | None:
        """立即跑一轮刷新并返回 ``warm_all`` 的结果。

        已有一轮在跑时立即返回 ``None``（本轮被合并，计入 ``coalesced``）；抓取失败时
        也会返回 ``None``，失败原因记录在 ``report()["last_error"]`` 中。
        """

        if self._lock.locked():
            self._coalesced += 1
            LOGGER.debug("已有一轮刷新在进行，合并本次请求。")
            return None
        async with self._lock:
            return await self._refresh_once()

    def report(self) -> dict[str, object]:
        """返回调度统计，供健康检查 / ``/cache`` 接口展示。

        ``last_run_at`` / ``last_duration_seconds`` 在还没有完整跑过一轮时为 ``None``
        （失败的一轮也算跑过）；``last_error`` 在最近一轮成功后被清空。
        """

        return {
            "enabled": self.enabled,
            "interval_seconds": self._interval_seconds,
            "jitter_seconds": self._jitter_seconds,
            "running": self.running,
            "runs": self._runs,
            "failures": self._failures,
            "coalesced": self._coalesced,
            "last_run_at": self._last_run_at,
            "last_duration_seconds": self._last_duration_seconds,
            "last_error": self._last_error,
        }

    def _next_delay(self) -> float:
        """本轮等待时长；``jitter_seconds <= 0`` 时等于 ``interval_seconds``。"""

        if self._jitter_seconds <= 0.0:
            return self._interval_seconds
        return self._interval_seconds + self._random() * self._jitter_seconds

    async def _run_loop(self) -> None:
        """周期循环：等待 → 抓取一轮 → 等待……

        除 ``asyncio.CancelledError`` 之外的任何异常都被记录后继续循环。
        """

        while True:
            try:
                await self._sleep(self._next_delay())
                await self.refresh_now()
            except asyncio.CancelledError:
                LOGGER.info("定时刷新循环已取消。")
                raise
            except Exception as error:  # noqa: BLE001 - 后台循环不能因任何单轮失败而终止
                self._failures += 1
                self._last_error = f"{type(error).__name__}: {error}"
                LOGGER.exception("定时刷新轮次失败（已记录，循环继续）：%s", error)

    async def _refresh_once(self) -> object | None:
        """执行一轮 ``warm_all`` 并更新统计；成功返回其结果。"""

        started_at = self._clock()
        try:
            result = await self._warmup.warm_all(force_refresh=True)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - 单轮失败只记录，不终止循环
            self._failures += 1
            self._last_run_at = started_at
            self._last_duration_seconds = max(0.0, self._clock() - started_at)
            self._last_error = f"{type(error).__name__}: {error}"
            LOGGER.exception("定时刷新失败（已记录，循环继续）：%s", error)
            return None
        self._runs += 1
        self._last_run_at = started_at
        self._last_duration_seconds = max(0.0, self._clock() - started_at)
        self._last_error = None
        LOGGER.info("定时刷新完成，用时 %.3f 秒。", self._last_duration_seconds)
        return result
