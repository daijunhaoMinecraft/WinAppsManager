import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR.parent))

from Backend import persistence as persistence_module
from Backend.cache import ReleaseMetadataCache
from Backend.config import BackendSettings, CacheConfig, RefreshConfig
from Backend.models import Release
from Backend.persistence import SCHEMA_VERSION, PersistentMetadataStore
from Backend.tests.test_api import FakeQQProvider, create_app

from fastapi.testclient import TestClient


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


class PersistentStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="winapps-cache-"))
        self.clock = WallClock()
        self.store = PersistentMetadataStore(self.directory, ttl_seconds=60, clock=self.clock)

    async def asyncTearDown(self):
        self.store.close()
        for path in self.directory.iterdir():
            path.unlink(missing_ok=True)
        self.directory.rmdir()

    async def test_snapshots_survive_a_store_restart(self):
        self.assertTrue(await self.store.save("qq", [release()]))
        self.store.close()

        self.clock.value += 30
        reopened = PersistentMetadataStore(self.directory, ttl_seconds=60, clock=self.clock)
        try:
            stored = await reopened.load("qq")
            self.assertIsNotNone(stored)
            self.assertEqual(stored.releases[0].version, "1.0")
            self.assertAlmostEqual(stored.remaining_seconds, 30.0)
        finally:
            reopened.close()

    async def test_expired_snapshot_is_deleted_and_ignored(self):
        await self.store.save("qq", [release()])
        self.clock.value += 61
        self.assertIsNone(await self.store.load("qq"))
        self.assertEqual(list(self.directory.glob("*.json")), [])

    async def test_corrupt_or_old_schema_file_is_ignored(self):
        path = self.store._path("qq")
        path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(await self.store.load("qq"))

        path.write_text(json.dumps({
            "schema_version": SCHEMA_VERSION + 1,
            "key": "qq",
            "expires_at": self.clock.value + 60,
            "releases": [release().model_dump(mode="json")],
        }), encoding="utf-8")
        self.assertIsNone(await self.store.load("qq"))

        reopened = PersistentMetadataStore(self.directory, ttl_seconds=60, clock=self.clock)
        try:
            self.assertEqual(reopened.info()["entries"], 0)
        finally:
            reopened.close()

    async def test_clear_removes_files_and_blocks_stale_writes(self):
        await self.store.save("qq", [release()])
        await self.store.save("wechat", [release()])
        self.assertEqual(await self.store.clear(), 2)
        self.assertEqual(list(self.directory.glob("*.json")), [])
        self.assertIsNone(await self.store.load("qq"))
        self.assertEqual(self.store.info()["entries"], 0)

    async def test_store_prunes_to_max_entries(self):
        store = PersistentMetadataStore(self.directory, ttl_seconds=60, max_entries=2, clock=self.clock)
        try:
            for index, key in enumerate(("a", "b", "c")):
                self.clock.value += 1
                await store.save(key, [release(str(index + 1) + ".0")])
            self.assertEqual(store.info()["entries"], 2)
            self.assertIsNone(await store.load("a"))
        finally:
            store.close()

    async def test_write_leaves_no_temporary_files(self):
        await self.store.save("qq", [release()])
        self.assertEqual(list(self.directory.glob("*.tmp")), [])

    async def test_invalid_configuration_is_rejected(self):
        for kwargs in ({"ttl_seconds": -1}, {"ttl_seconds": float("inf")}, {"max_entries": 0},
                       {"max_entries": True}, {"clock": None}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                PersistentMetadataStore(self.directory, **kwargs)


class TransientWriteFailureTests(unittest.IsolatedAsyncioTestCase):
    """Windows 上杀毒/索引服务会短暂占用刚创建的临时文件，写入必须能重试。"""

    async def asyncSetUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="winapps-cache-"))
        self.store = PersistentMetadataStore(self.directory, ttl_seconds=60, clock=WallClock())

    async def asyncTearDown(self):
        self.store.close()
        for path in self.directory.iterdir():
            path.unlink(missing_ok=True)
        self.directory.rmdir()

    async def test_transient_lock_errors_are_retried(self):
        real_mkstemp = tempfile.mkstemp
        attempts = []

        def flaky(*args, **kwargs):
            attempts.append(1)
            if len(attempts) <= 2:
                raise PermissionError(13, "The process cannot access the file")
            return real_mkstemp(*args, **kwargs)

        with mock.patch.object(persistence_module.tempfile, "mkstemp", side_effect=flaky):
            self.assertTrue(await self.store.save("qq", [release()]))
        self.assertEqual(len(attempts), 3)
        stored = await self.store.load("qq")
        self.assertIsNotNone(stored)
        self.assertEqual(stored.releases[0].version, "1.0")

    async def test_permanent_write_failure_is_reported_without_raising(self):
        with mock.patch.object(
            persistence_module.tempfile, "mkstemp", side_effect=PermissionError(13, "denied")
        ), self.assertLogs("winappsmanager.cache", level="WARNING"):
            self.assertFalse(await self.store.save("qq", [release()]))
        self.assertEqual(self.store.info()["errors"], 1)
        self.assertEqual(list(self.directory.glob("*.json")), [])


class CachePersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="winapps-cache-"))
        self.wall = WallClock()
        self.mono = MonotonicClock()
        self.caches = []
        self.calls = 0
        self.version = "1.0"

    async def asyncTearDown(self):
        for cache in self.caches:
            await cache.shutdown()
        for path in self.directory.iterdir():
            path.unlink(missing_ok=True)
        self.directory.rmdir()

    def new_cache(self, *, ttl_seconds=300.0, persistent_ttl_seconds=3600.0, enabled=True):
        store = PersistentMetadataStore(self.directory, ttl_seconds=persistent_ttl_seconds, clock=self.wall)
        cache = ReleaseMetadataCache(ttl_seconds=ttl_seconds, clock=self.mono, enabled=enabled, store=store)
        self.caches.append(cache)
        return cache

    async def load(self):
        self.calls += 1
        return [release(self.version)]

    async def test_restart_serves_disk_snapshot_without_calling_the_provider(self):
        first = self.new_cache()
        self.assertEqual((await first.get("demo", self.load))[0].version, "1.0")
        self.assertEqual(self.calls, 1)
        self.assertEqual(first.info()["persistent"]["entries"], 1)
        await first.shutdown()

        self.version = "2.0"
        second = self.new_cache()
        self.assertEqual((await second.get("demo", self.load))[0].version, "1.0")
        self.assertEqual(self.calls, 1)
        info = second.info()
        self.assertEqual(info["hits"], 1)
        self.assertEqual(info["persistent_hits"], 1)
        self.assertEqual(info["misses"], 0)

    async def test_forced_refresh_ignores_and_rewrites_the_disk_snapshot(self):
        first = self.new_cache()
        await first.get("demo", self.load)
        await first.shutdown()

        self.version = "2.0"
        second = self.new_cache()
        self.assertEqual((await second.get("demo", self.load, force_refresh=True))[0].version, "2.0")
        self.assertEqual(self.calls, 2)
        self.assertEqual(second.info()["refreshes"], 1)
        await second.shutdown()

        self.version = "3.0"
        third = self.new_cache()
        self.assertEqual((await third.get("demo", self.load))[0].version, "2.0")
        self.assertEqual(self.calls, 2)

    async def test_memory_ttl_expires_but_the_longer_disk_ttl_still_serves(self):
        cache = self.new_cache(ttl_seconds=10)
        await cache.get("demo", self.load)
        self.mono.value += 11
        self.assertEqual(cache.info()["entries"], 0)
        self.assertEqual((await cache.get("demo", self.load))[0].version, "1.0")
        self.assertEqual(self.calls, 1)
        self.assertEqual(cache.info()["persistent_hits"], 1)

    async def test_persistent_expiry_forces_a_new_provider_load(self):
        cache = self.new_cache(persistent_ttl_seconds=60)
        await cache.get("demo", self.load)
        self.mono.value += 301
        self.wall.value += 61
        self.version = "2.0"
        self.assertEqual((await cache.get("demo", self.load))[0].version, "2.0")
        self.assertEqual(self.calls, 2)

    async def test_clear_removes_disk_snapshots(self):
        cache = self.new_cache()
        await cache.get("demo", self.load)
        self.assertEqual(await cache.clear(), 1)
        self.assertEqual(list(self.directory.glob("*.json")), [])
        self.version = "2.0"
        self.assertEqual((await cache.get("demo", self.load))[0].version, "2.0")
        self.assertEqual(self.calls, 2)

    async def test_disabled_cache_never_stores_or_reuses_metadata(self):
        cache = self.new_cache(enabled=False)
        await cache.get("demo", self.load)
        await cache.get("demo", self.load)
        self.assertEqual(self.calls, 2)
        self.assertEqual(cache.info()["entries"], 0)
        self.assertEqual(cache.info()["persistent"]["entries"], 0)
        self.assertEqual(list(self.directory.glob("*.json")), [])

    async def test_loader_failure_does_not_replace_a_valid_disk_snapshot(self):
        cache = self.new_cache()
        await cache.get("demo", self.load)

        async def broken():
            self.calls += 1
            raise RuntimeError("upstream down")

        with self.assertRaises(RuntimeError):
            await cache.get("demo", broken, force_refresh=True)
        self.assertEqual((await cache.get("demo", self.load))[0].version, "1.0")
        self.assertEqual(self.calls, 2)


class ApiPersistenceTests(unittest.TestCase):
    def test_backend_restart_reuses_disk_metadata_but_latest_always_refreshes(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = BackendSettings(cache=CacheConfig(
                persist=True, directory=Path(directory), ttl_seconds=300, persistent_ttl_seconds=3600,
            ), refresh=RefreshConfig(on_startup=False, interval_seconds=0))
            first_provider = FakeQQProvider()
            with TestClient(create_app(release_providers={"qq": first_provider}, settings=settings)) as client:
                self.assertEqual(client.get("/api/v1/apps/qq").json()["version"], "9.9.37")
                cache_info = client.get("/api/v1/cache").json()
                self.assertTrue(cache_info["persistent"]["enabled"])
                self.assertEqual(cache_info["persistent"]["entries"], 1)
                self.assertEqual(cache_info["settings"]["cache"]["ttl_seconds"], 300.0)
            self.assertNotEqual(list(Path(directory).glob("*.json")), [])

            second_provider = FakeQQProvider()
            second_provider.version = "9.9.38"
            with TestClient(create_app(release_providers={"qq": second_provider}, settings=settings)) as client:
                self.assertEqual(client.get("/api/v1/apps/qq").json()["version"], "9.9.37")
                self.assertEqual(second_provider.force_refreshes, 0)

                latest = client.get("/api/v1/apps/qq/releases/latest")
                self.assertEqual(latest.status_code, 200)
                self.assertEqual(latest.json()["version"], "9.9.38")
                self.assertEqual(second_provider.force_refreshes, 1)

                cleared = client.delete("/api/v1/cache")
                self.assertEqual(cleared.status_code, 200)
                self.assertEqual(cleared.json()["cleared"], 1)
                self.assertEqual(cleared.json()["persistent_cleared"], 1)
                self.assertEqual(cleared.json()["persistent"]["entries"], 0)
                self.assertEqual(list(Path(directory).glob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
