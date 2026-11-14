"""``Backend.scheduler.PeriodicRefresher`` 的单元测试。

调度节奏完全由注入的 ``sleep`` 控制：``FakeSleep`` 记录 delay 后挂起，直到测试显式
放行，因此断言 jitter、循环节奏和取消行为都不需要真实等待时间。
"""

import asyncio
from pathlib import Path
import sys
import unittest


BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR.parent))

from Backend.scheduler import PeriodicRefresher


MAX_SPINS = 500


async def wait_until(predicate):
    """只让出事件循环（不消耗真实时间），直到条件成立。"""

    for _ in range(MAX_SPINS):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("等待条件超时：调度循环没有继续推进")


class FakeWarmup:
    """记录调用次数/参数的 ``warm_all`` 替身，可配置抛错或挂起。"""

    def __init__(self, *, error: BaseException | None = None, gate: asyncio.Event | None = None) -> None:
        self.error = error
        self.gate = gate
        self.calls: list[dict[str, object]] = []
        self.started = asyncio.Event()

    async def warm_all(self, *, force_refresh: bool = True, app_ids=None):
        self.calls.append({"force_refresh": force_refresh, "app_ids": app_ids})
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return {"apps": len(self.calls)}


class FakeSleep:
    """可放行的假 sleep：记录 delay，挂起直到测试调用 ``release()``。"""

    def __init__(self) -> None:
        self.delays: list[float] = []
        self._gates: list[asyncio.Event] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        gate = asyncio.Event()
        self._gates.append(gate)
        await gate.wait()

    async def wait_calls(self, count: int) -> None:
        await wait_until(lambda: len(self.delays) >= count)

    def release(self, index: int = 0) -> None:
        self._gates[index].set()

    @property
    def call_count(self) -> int:
        return len(self.delays)


class FakeClock:
    """常量时钟，便于精确断言 ``last_run_at`` / ``last_duration_seconds``。"""

    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class PeriodicRefresherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._refreshers: list[PeriodicRefresher] = []

    async def asyncTearDown(self) -> None:
        # 每个测试都必须停掉循环，避免残留任务导致警告或挂起。
        for refresher in self._refreshers:
            await refresher.stop()

    def make_refresher(self, warmup, **kwargs) -> PeriodicRefresher:
        refresher = PeriodicRefresher(warmup, **kwargs)
        self._refreshers.append(refresher)
        return refresher

    async def test_disabled_interval_does_not_start_loop_but_manual_refresh_works(self):
        warmup = FakeWarmup()
        sleep = FakeSleep()
        refresher = self.make_refresher(warmup, interval_seconds=0, jitter_seconds=0, sleep=sleep)

        refresher.start()
        refresher.start()

        self.assertFalse(refresher.running)
        self.assertEqual(sleep.delays, [])
        report = refresher.report()
        self.assertFalse(report["enabled"])
        self.assertFalse(report["running"])
        self.assertEqual(report["interval_seconds"], 0.0)
        self.assertIsNone(report["last_run_at"])
        self.assertIsNone(report["last_duration_seconds"])
        self.assertIsNone(report["last_error"])

        result = await refresher.refresh_now()

        self.assertEqual(result, {"apps": 1})
        self.assertEqual(len(warmup.calls), 1)
        self.assertTrue(warmup.calls[0]["force_refresh"])
        self.assertEqual(refresher.report()["runs"], 1)

    async def test_loop_runs_one_round_after_sleep_is_released(self):
        def forbidden_random() -> float:
            raise AssertionError("jitter_seconds <= 0 时不应调用 random")

        warmup = FakeWarmup()
        sleep = FakeSleep()
        refresher = self.make_refresher(
            warmup,
            interval_seconds=60,
            jitter_seconds=0,
            sleep=sleep,
            clock=FakeClock(100.0),
            random=forbidden_random,
        )
        refresher.start()

        await sleep.wait_calls(1)
        self.assertEqual(sleep.delays, [60.0])
        self.assertTrue(refresher.running)

        sleep.release(0)
        await wait_until(lambda: refresher.report()["runs"] == 1)

        self.assertEqual(len(warmup.calls), 1)
        self.assertTrue(warmup.calls[0]["force_refresh"])
        report = refresher.report()
        self.assertEqual(report["runs"], 1)
        self.assertEqual(report["failures"], 0)
        self.assertEqual(report["last_run_at"], 100.0)
        self.assertEqual(report["last_duration_seconds"], 0.0)
        self.assertIsNone(report["last_error"])
        self.assertTrue(report["running"])

    async def test_failure_is_recorded_and_loop_continues(self):
        warmup = FakeWarmup(error=RuntimeError("官网抓取失败"))
        sleep = FakeSleep()
        refresher = self.make_refresher(warmup, interval_seconds=30, sleep=sleep)
        refresher.start()

        await sleep.wait_calls(1)
        with self.assertLogs("winappsmanager.refresh", level="ERROR") as captured:
            sleep.release(0)
            await wait_until(lambda: refresher.report()["failures"] == 1)

        report = refresher.report()
        self.assertEqual(report["failures"], 1)
        self.assertEqual(report["runs"], 0)
        self.assertIn("官网抓取失败", str(report["last_error"]))
        self.assertIsNotNone(report["last_run_at"])
        self.assertTrue(report["running"])
        self.assertTrue(any("定时刷新" in line for line in captured.output))

        # 循环没有终止：再放行一轮仍会调用 warm_all。
        await sleep.wait_calls(2)
        with self.assertLogs("winappsmanager.refresh", level="ERROR"):
            sleep.release(1)
            await wait_until(lambda: refresher.report()["failures"] == 2)

        self.assertEqual(len(warmup.calls), 2)
        self.assertTrue(refresher.running)

    async def test_concurrent_refresh_now_coalesces_into_one_round(self):
        gate = asyncio.Event()
        warmup = FakeWarmup(gate=gate)
        refresher = self.make_refresher(warmup, interval_seconds=0)

        first = asyncio.create_task(refresher.refresh_now())
        await warmup.started.wait()
        merged = await asyncio.create_task(refresher.refresh_now())

        self.assertIsNone(merged)
        self.assertEqual(len(warmup.calls), 1)

        gate.set()
        self.assertEqual(await first, {"apps": 1})
        report = refresher.report()
        self.assertEqual(report["runs"], 1)
        self.assertEqual(report["coalesced"], 1)
        self.assertEqual(len(warmup.calls), 1)

    async def test_refresh_now_during_loop_round_is_coalesced(self):
        gate = asyncio.Event()
        warmup = FakeWarmup(gate=gate)
        sleep = FakeSleep()
        refresher = self.make_refresher(warmup, interval_seconds=60, sleep=sleep)
        refresher.start()

        await sleep.wait_calls(1)
        sleep.release(0)
        await warmup.started.wait()

        # 循环正在抓取时的手动刷新必须被合并，而不是并行开第二轮。
        self.assertIsNone(await refresher.refresh_now())

        gate.set()
        await wait_until(lambda: refresher.report()["runs"] == 1)
        report = refresher.report()
        self.assertEqual(report["coalesced"], 1)
        self.assertEqual(len(warmup.calls), 1)

    async def test_stop_is_idempotent_and_prevents_further_rounds(self):
        warmup = FakeWarmup()
        sleep = FakeSleep()
        refresher = self.make_refresher(warmup, interval_seconds=60, sleep=sleep)
        refresher.start()
        await sleep.wait_calls(1)
        self.assertTrue(refresher.running)

        await refresher.stop()
        self.assertFalse(refresher.running)
        await refresher.stop()
        self.assertFalse(refresher.running)

        sleep.release(0)
        for _ in range(10):
            await asyncio.sleep(0)

        self.assertEqual(len(warmup.calls), 0)
        self.assertEqual(refresher.report()["runs"], 0)
        self.assertFalse(refresher.report()["running"])

    async def test_stop_before_start_is_safe(self):
        refresher = self.make_refresher(FakeWarmup(), interval_seconds=60, sleep=FakeSleep())

        await refresher.stop()

        self.assertFalse(refresher.running)

    async def test_start_is_idempotent(self):
        warmup = FakeWarmup()
        sleep = FakeSleep()
        refresher = self.make_refresher(warmup, interval_seconds=60, sleep=sleep)

        refresher.start()
        refresher.start()
        refresher.start()
        await sleep.wait_calls(1)

        # 只允许一个循环在推进：三个 start() 之后 sleep 仍然只被调用一次。
        self.assertEqual(len(sleep.delays), 1)

        sleep.release(0)
        await wait_until(lambda: refresher.report()["runs"] == 1)
        self.assertEqual(len(warmup.calls), 1)

    async def test_restart_after_stop(self):
        warmup = FakeWarmup()
        sleep = FakeSleep()
        refresher = self.make_refresher(warmup, interval_seconds=60, sleep=sleep)
        refresher.start()
        await sleep.wait_calls(1)

        await refresher.stop()
        self.assertFalse(refresher.running)

        refresher.start()
        self.assertTrue(refresher.running)
        await sleep.wait_calls(2)

        sleep.release(1)
        await wait_until(lambda: refresher.report()["runs"] == 1)

        self.assertEqual(len(warmup.calls), 1)

    async def test_jitter_keeps_delay_within_bounds(self):
        warmup = FakeWarmup()
        sleep = FakeSleep()
        refresher = self.make_refresher(
            warmup,
            interval_seconds=10,
            jitter_seconds=5,
            sleep=sleep,
            random=lambda: 0.5,
        )
        refresher.start()
        await sleep.wait_calls(1)

        self.assertEqual(sleep.delays[0], 12.5)
        self.assertGreaterEqual(sleep.delays[0], 10.0)
        self.assertLessEqual(sleep.delays[0], 15.0)

    async def test_jitter_uses_random_for_every_round(self):
        warmup = FakeWarmup()
        sleep = FakeSleep()
        draws = iter([0.0, 0.999])

        refresher = self.make_refresher(
            warmup,
            interval_seconds=10,
            jitter_seconds=5,
            sleep=sleep,
            random=lambda: next(draws),
        )
        refresher.start()

        await sleep.wait_calls(1)
        sleep.release(0)
        await wait_until(lambda: refresher.report()["runs"] == 1)
        await sleep.wait_calls(2)

        self.assertEqual(sleep.delays[0], 10.0)
        self.assertAlmostEqual(sleep.delays[1], 14.995)
        self.assertLessEqual(sleep.delays[1], 15.0)

    async def test_report_exposes_expected_fields(self):
        refresher = self.make_refresher(
            FakeWarmup(),
            interval_seconds=15,
            jitter_seconds=2,
            sleep=FakeSleep(),
        )

        report = refresher.report()

        self.assertEqual(
            set(report),
            {
                "enabled",
                "interval_seconds",
                "jitter_seconds",
                "running",
                "runs",
                "failures",
                "coalesced",
                "last_run_at",
                "last_duration_seconds",
                "last_error",
            },
        )
        self.assertTrue(report["enabled"])
        self.assertEqual(report["interval_seconds"], 15.0)
        self.assertEqual(report["jitter_seconds"], 2.0)
        self.assertFalse(report["running"])
        self.assertEqual((report["runs"], report["failures"], report["coalesced"]), (0, 0, 0))
        self.assertIsNone(report["last_run_at"])
        self.assertIsNone(report["last_duration_seconds"])
        self.assertIsNone(report["last_error"])


if __name__ == "__main__":
    unittest.main()
