"""陈旧快照兜底与数据来源报告的测试。

覆盖持久层 ``load(allow_expired=True)`` 的陈旧语义、``max_stale_seconds`` 边界，
以及 ``ReleaseMetadataCache.fetch`` 的 memory/persistent/stale/provider 来源映射。
"""

import asyncio
from pathlib import Path
import sys
import tempfile
import unittest

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR.parent))

from Backend.cache import MetadataResult, MetadataSource, ReleaseMetadataCache
from Backend.models import Release
from Backend.persistence import PersistentMetadataStore


def release(version="1.0", architecture="x64"):
    return Release(
        id=f"{version}-windows-{architecture}", version=version, platform="windows", architecture=architecture,
        download_url=f"https://downloads.example/app-{version}-{architecture}.exe",
        file_name=f"app-{version}-{architecture}.exe", size_bytes=None, sha256=None,
    )


class WallClock:
    def __init__(self, value=1_000.0):
        self.value = value

    def __call__(self):
        return self.value


class MonotonicClock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value


class StaleSnapshotStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="winapps-stale-"))
        self.clock = WallClock()
        self.store = PersistentMetadataStore(
            self.directory, ttl_seconds=60, max_stale_seconds=1_000, clock=self.clock
        )

    async def asyncTearDown(self):
        self.store.close()
        for path in self.directory.iterdir():
            path.unlink(missing_ok=True)
        self.directory.rmdir()

    def json_files(self):
        return list(self.directory.glob("*.json"))

    async def test_expired_snapshot_is_served_when_expiry_is_allowed(self):
        self.assertTrue(await self.store.save("qq", [release()]))
        self.clock.value += 61

        stored = await self.store.load("qq", allow_expired=True)
        self.assertIsNotNone(stored)
        self.assertTrue(stored.expired)
        self.assertEqual(stored.releases[0].version, "1.0")
        self.assertAlmostEqual(stored.expires_at, 1_060.0)
        self.assertAlmostEqual(stored.remaining_seconds, -1.0)
        self.assertAlmostEqual(stored.saved_at, 1_000.0)
        self.assertEqual(len(self.json_files()), 1)
        self.assertEqual(self.store.info()["stale_entries"], 1)
        self.assertEqual(self.store.info()["entries"], 0)

    async def test_expired_snapshot_without_allow_expired_is_still_deleted(self):
        await self.store.save("qq", [release()])
        self.clock.value += 61

        self.assertIsNone(await self.store.load("qq"))
        self.assertEqual(self.json_files(), [])

    async def test_snapshot_beyond_max_stale_is_deleted_even_when_allowed(self):
        await self.store.save("qq", [release()])
        # expires_at = 1060，允许陈旧 1000 秒，这里过期 1001 秒。
        self.clock.value += 60 + 1_001

        self.assertIsNone(await self.store.load("qq", allow_expired=True))
        self.assertEqual(self.json_files(), [])
        self.assertEqual(self.store.info()["stale_entries"], 0)

    async def test_restarted_store_indexes_and_serves_a_stale_snapshot(self):
        await self.store.save("qq", [release()])
        self.store.close()
        self.clock.value += 90

        reopened = PersistentMetadataStore(
            self.directory, ttl_seconds=60, max_stale_seconds=1_000, clock=self.clock
        )
        try:
            info = reopened.info()
            self.assertEqual((info["entries"], info["stale_entries"]), (0, 1))
            self.assertEqual(info["max_stale_seconds"], 1_000.0)
            stored = await reopened.load("qq", allow_expired=True)
            self.assertIsNotNone(stored)
            self.assertTrue(stored.expired)
            self.assertEqual(stored.releases[0].version, "1.0")
        finally:
            reopened.close()

    async def test_restarted_store_drops_snapshots_beyond_the_stale_limit(self):
        await self.store.save("qq", [release()])
        self.store.close()
        self.clock.value += 60 + 1_001

        reopened = PersistentMetadataStore(
            self.directory, ttl_seconds=60, max_stale_seconds=1_000, clock=self.clock
        )
        try:
            info = reopened.info()
            self.assertEqual((info["entries"], info["stale_entries"]), (0, 0))
            self.assertEqual(self.json_files(), [])
            self.assertIsNone(await reopened.load("qq", allow_expired=True))
        finally:
            reopened.close()

    async def test_info_reports_stale_entries_and_the_limit(self):
        await self.store.save("qq", [release()])
        info = self.store.info()
        self.assertEqual((info["entries"], info["stale_entries"]), (1, 0))
        self.assertEqual(info["max_stale_seconds"], 1_000.0)

        self.clock.value += 90
        info = self.store.info()
        self.assertEqual((info["entries"], info["stale_entries"]), (0, 1))

        self.clock.value += 1_100
        info = self.store.info()
        self.assertEqual((info["entries"], info["stale_entries"]), (0, 0))

    async def test_max_entries_also_prunes_stale_snapshots(self):
        store = PersistentMetadataStore(
            self.directory, ttl_seconds=60, max_entries=2, max_stale_seconds=1_000, clock=self.clock
        )
        try:
            await store.save("a", [release("1.0")])
            self.clock.value += 1
            await store.save("b", [release("2.0")])
            self.clock.value += 100
            # 保存 c 会触发配额清理：a 的过期时间最早（且已成陈旧条目），应被优先删除。
            await store.save("c", [release("3.0")])
            self.assertIsNone(await store.load("a", allow_expired=True))
            info = store.info()
            self.assertEqual((info["entries"], info["stale_entries"]), (1, 1))
            self.assertEqual(len(self.json_files()), 2)
        finally:
            store.close()

    async def test_invalid_max_stale_seconds_is_rejected(self):
        for value in (-1, float("inf"), float("nan"), True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                PersistentMetadataStore(self.directory, max_stale_seconds=value)


class FetchSourceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="winapps-stale-cache-"))
        self.wall = WallClock()
        self.mono = MonotonicClock()
        self.store = PersistentMetadataStore(
            self.directory, ttl_seconds=3_600, max_stale_seconds=604_800, clock=self.wall
        )
        self.cache = ReleaseMetadataCache(ttl_seconds=300, clock=self.mono, store=self.store)
        self.calls = 0
        self.version = "1.0"

    async def asyncTearDown(self):
        await self.cache.shutdown()
        for path in self.directory.iterdir():
            path.unlink(missing_ok=True)
        self.directory.rmdir()

    def json_files(self):
        return list(self.directory.glob("*.json"))

    async def load(self):
        self.calls += 1
        return [release(self.version)]

    def broken_loader(self):
        async def broken():
            self.calls += 1
            raise RuntimeError("upstream down")

        return broken

    def expire_memory_and_disk(self):
        self.mono.value += 301
        self.wall.value += 3_601

    async def test_fetch_reports_provider_memory_persistent_and_stale_sources(self):
        first = await self.cache.fetch("demo", self.load)
        self.assertIsInstance(first, MetadataResult)
        self.assertIs(first.source, MetadataSource.provider)
        self.assertFalse(first.degraded)
        self.assertEqual(first.releases[0].version, "1.0")

        memory = await self.cache.fetch("demo", self.load)
        self.assertIs(memory.source, MetadataSource.memory)
        self.assertFalse(memory.degraded)

        self.mono.value += 301
        persistent = await self.cache.fetch("demo", self.load)
        self.assertIs(persistent.source, MetadataSource.persistent)
        self.assertFalse(persistent.degraded)
        self.assertEqual(persistent.releases[0].version, "1.0")
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.cache.info()["persistent_hits"], 1)

        self.expire_memory_and_disk()
        stale = await self.cache.fetch("demo", self.broken_loader(), allow_stale=True)
        self.assertIs(stale.source, MetadataSource.stale)
        self.assertTrue(stale.degraded)
        self.assertEqual(stale.releases[0].version, "1.0")
        self.assertEqual(self.calls, 2)
        info = self.cache.info()
        self.assertEqual(info["stale_hits"], 1)
        self.assertEqual(info["persistent"]["stale_entries"], 1)
        self.assertEqual(info["persistent"]["max_stale_seconds"], 604_800.0)
        # 陈旧快照只是兜底，不应回填内存缓存。
        self.assertEqual(info["entries"], 0)

    async def test_stale_fallback_is_not_rewritten_as_fresh_metadata(self):
        await self.cache.fetch("demo", self.load)
        self.expire_memory_and_disk()
        stale = await self.cache.fetch("demo", self.broken_loader(), allow_stale=True)
        self.assertIs(stale.source, MetadataSource.stale)
        self.assertEqual(self.cache.info()["persistent"]["writes"], 1)

        self.version = "2.0"
        refreshed = await self.cache.fetch("demo", self.load, force_refresh=True)
        self.assertIs(refreshed.source, MetadataSource.provider)
        self.assertEqual(refreshed.releases[0].version, "2.0")
        self.assertEqual(self.cache.info()["persistent"]["writes"], 2)

    async def test_stale_fallback_returns_independent_release_copies(self):
        await self.cache.fetch("demo", self.load)
        self.expire_memory_and_disk()
        stale = await self.cache.fetch("demo", self.broken_loader(), allow_stale=True)
        stale.releases[0].version = "mutated"

        stored = await self.store.load("demo", allow_expired=True)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.releases[0].version, "1.0")

    async def test_loader_failure_without_any_snapshot_propagates(self):
        with self.assertRaises(RuntimeError):
            await self.cache.fetch("demo", self.broken_loader(), allow_stale=True)
        info = self.cache.info()
        self.assertEqual(info["stale_hits"], 0)
        self.assertEqual(self.json_files(), [])

    async def test_plain_get_keeps_propagating_instead_of_serving_stale(self):
        await self.cache.fetch("demo", self.load)
        self.expire_memory_and_disk()

        with self.assertRaises(RuntimeError):
            await self.cache.get("demo", self.broken_loader())
        self.assertEqual(self.cache.info()["stale_hits"], 0)
        # 普通读取探测过期快照时不应删除它，否则 allow_stale 的兜底就没有数据源。
        self.assertEqual(len(self.json_files()), 1)

    async def test_snapshot_beyond_max_stale_is_dropped_before_fallback(self):
        directory = Path(tempfile.mkdtemp(prefix="winapps-stale-cache-"))
        store = PersistentMetadataStore(
            directory, ttl_seconds=60, max_stale_seconds=100, clock=self.wall
        )
        cache = ReleaseMetadataCache(ttl_seconds=300, clock=self.mono, store=store)
        try:
            await cache.fetch("demo", self.load)
            self.mono.value += 301
            self.wall.value += 60 + 101
            with self.assertRaises(RuntimeError):
                await cache.fetch("demo", self.broken_loader(), allow_stale=True)
            self.assertEqual(cache.info()["stale_hits"], 0)
            self.assertEqual(list(directory.glob("*.json")), [])
        finally:
            await cache.shutdown()
            for path in directory.iterdir():
                path.unlink(missing_ok=True)
            directory.rmdir()

    async def test_coalesced_waiter_still_gets_the_stale_fallback(self):
        await self.cache.fetch("demo", self.load)
        self.expire_memory_and_disk()

        started = asyncio.Event()
        gate = asyncio.Event()

        async def broken():
            self.calls += 1
            started.set()
            await gate.wait()
            raise RuntimeError("upstream down")

        strict = asyncio.create_task(self.cache.fetch("demo", broken, allow_stale=False))
        await started.wait()
        lenient = asyncio.create_task(self.cache.fetch("demo", broken, allow_stale=True))
        await asyncio.sleep(0)  # 让 lenient 加入 strict 发起的同一个 flight
        gate.set()

        with self.assertRaises(RuntimeError):
            await strict
        result = await lenient
        self.assertIs(result.source, MetadataSource.stale)
        self.assertTrue(result.degraded)
        self.assertEqual(result.releases[0].version, "1.0")
        info = self.cache.info()
        self.assertEqual(info["coalesced"], 1)
        self.assertEqual(info["stale_hits"], 1)
        self.assertEqual(self.calls, 2)

    async def test_info_reports_stale_hits_and_persistent_limits(self):
        info = self.cache.info()
        self.assertEqual(info["stale_hits"], 0)
        self.assertEqual(info["persistent"]["max_stale_seconds"], 604_800.0)
        self.assertEqual(info["persistent"]["stale_entries"], 0)

        await self.cache.fetch("demo", self.load)
        self.expire_memory_and_disk()
        await self.cache.fetch("demo", self.broken_loader(), allow_stale=True)
        info = self.cache.info()
        self.assertEqual(info["stale_hits"], 1)
        self.assertEqual(info["persistent"]["stale_entries"], 1)

    async def test_disabled_cache_does_not_fall_back_to_stale_snapshots(self):
        await self.cache.fetch("demo", self.load)
        self.expire_memory_and_disk()

        disabled = ReleaseMetadataCache(ttl_seconds=300, clock=self.mono, enabled=False, store=self.store)
        try:
            with self.assertRaises(RuntimeError):
                await disabled.fetch("demo", self.broken_loader(), allow_stale=True)
            self.assertEqual(disabled.info()["stale_hits"], 0)
        finally:
            await disabled.shutdown()


if __name__ == "__main__":
    unittest.main()
