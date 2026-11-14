import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

from pydantic import HttpUrl

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR.parent))

from Backend.cache import ReleaseMetadataCache
from Backend.models import AppDefinition, Release
from Backend.providers.base import ProviderProtocolError, ProviderReleaseChangedError, ProviderTimeoutError
from Backend.releases import ReleaseService


def release(version="1.0", architecture="x64"):
    return Release(
        id=f"{version}-windows-{architecture}", version=version, platform="windows", architecture=architecture,
        download_url=f"https://downloads.example/app-{version}-{architecture}.exe",
        file_name=f"app-{version}-{architecture}.exe", size_bytes=None, sha256=None,
    )


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


class MetadataCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = Clock()
        self.cache = ReleaseMetadataCache(clock=self.clock, load_timeout_seconds=0.2)
        self.extra_caches = []

    async def asyncTearDown(self):
        await self.cache.shutdown()
        for cache in self.extra_caches:
            await cache.shutdown()

    async def test_defaults_info_and_independent_snapshot_copies(self):
        source = [release()]
        calls = 0

        async def load():
            nonlocal calls
            calls += 1
            return source

        first = await self.cache.get("demo", load)
        first[0].version = "caller mutation"
        source[0].version = "provider mutation"
        second = await self.cache.get("demo", load)
        self.assertEqual(second[0].version, "1.0")
        self.assertEqual(calls, 1)
        info = self.cache.info()
        self.assertEqual(info["ttl_seconds"], 300.0)
        self.assertEqual(info["max_entries"], 64)
        self.assertEqual((info["entries"], info["hits"], info["misses"]), (1, 1, 1))

    async def test_expiry_fetches_current_metadata(self):
        version = "1.0"
        calls = 0

        async def load():
            nonlocal calls
            calls += 1
            return [release(version)]

        await self.cache.get("demo", load)
        version = "2.0"
        self.clock.value = 299
        self.assertEqual((await self.cache.get("demo", load))[0].version, "1.0")
        self.clock.value = 300
        self.assertEqual(self.cache.info()["entries"], 0)
        self.assertEqual((await self.cache.get("demo", load))[0].version, "2.0")
        self.assertEqual(calls, 2)

    async def test_forced_refresh_bypasses_a_valid_entry(self):
        version = "1.0"

        async def load():
            return [release(version)]

        await self.cache.get("demo", load)
        version = "2.0"
        self.assertEqual((await self.cache.get("demo", load, force_refresh=True))[0].version, "2.0")
        self.assertEqual(self.cache.info()["refreshes"], 1)

    async def test_empty_successful_metadata_is_cached(self):
        calls = 0

        async def load():
            nonlocal calls
            calls += 1
            return []

        self.assertEqual(await self.cache.get("empty", load), [])
        self.assertEqual(await self.cache.get("empty", load), [])
        self.assertEqual(calls, 1)
        self.assertEqual(self.cache.info()["entries"], 1)

    async def test_concurrent_cold_requests_share_one_loader(self):
        gate = asyncio.Event()
        started = asyncio.Event()
        calls = 0

        async def load():
            nonlocal calls
            calls += 1
            started.set()
            await gate.wait()
            return [release()]

        callers = [asyncio.create_task(self.cache.get("demo", load)) for _ in range(8)]
        await started.wait()
        self.assertEqual(self.cache.info()["inflight"], 1)
        gate.set()
        results = await asyncio.gather(*callers)
        self.assertEqual(calls, 1)
        self.assertEqual(self.cache.info()["coalesced"], 7)
        results[0][0].version = "modified"
        self.assertEqual(results[1][0].version, "1.0")

    async def test_same_round_forced_requests_share_the_new_refresh(self):
        calls = 0

        async def load():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            return [release(str(calls) + ".0")]

        await self.cache.get("demo", load)
        results = await asyncio.gather(*(self.cache.get("demo", load, force_refresh=True) for _ in range(6)))
        self.assertTrue(all(batch[0].version == "2.0" for batch in results))
        self.assertEqual(calls, 2)
        self.assertEqual(self.cache.info()["refreshes"], 1)

    async def test_normal_request_joins_an_inflight_forced_refresh(self):
        async def initial():
            return [release()]

        await self.cache.get("demo", initial)
        gate = asyncio.Event()
        started = asyncio.Event()

        async def refreshed():
            started.set()
            await gate.wait()
            return [release("2.0")]

        forced = asyncio.create_task(self.cache.get("demo", refreshed, force_refresh=True))
        await started.wait()
        normal = asyncio.create_task(self.cache.get("demo", initial))
        await asyncio.sleep(0)
        gate.set()
        self.assertEqual((await forced)[0].version, "2.0")
        self.assertEqual((await normal)[0].version, "2.0")

    async def test_one_cancelled_awaiter_does_not_cancel_shared_load(self):
        gate = asyncio.Event()
        started = asyncio.Event()
        was_cancelled = False

        async def load():
            nonlocal was_cancelled
            started.set()
            try:
                await gate.wait()
            except asyncio.CancelledError:
                was_cancelled = True
                raise
            return [release()]

        first = asyncio.create_task(self.cache.get("demo", load))
        second = asyncio.create_task(self.cache.get("demo", load))
        await started.wait()
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertFalse(was_cancelled)
        gate.set()
        self.assertEqual((await second)[0].version, "1.0")
        self.assertEqual(self.cache.info()["entries"], 1)

    async def test_detached_load_has_a_deadline_and_observes_its_failure(self):
        cache = ReleaseMetadataCache(load_timeout_seconds=0.02)
        self.extra_caches.append(cache)
        cancelled = asyncio.Event()
        loop = asyncio.get_running_loop()
        original_handler = loop.get_exception_handler()
        unhandled = []
        loop.set_exception_handler(lambda current_loop, context: unhandled.append(context))

        async def load():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        try:
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(cache.get("demo", load), timeout=0.005)
            await asyncio.wait_for(cancelled.wait(), timeout=0.1)
            await asyncio.sleep(0)
            self.assertEqual(cache.info()["pending_loads"], 0)
            self.assertEqual(cache.info()["entries"], 0)
            self.assertEqual(unhandled, [])
        finally:
            loop.set_exception_handler(original_handler)

    async def test_errors_are_not_cached_or_returned_as_stale_success(self):
        calls = 0
        broken = False

        async def load():
            nonlocal calls
            calls += 1
            if broken:
                raise ProviderProtocolError("broken metadata")
            return [release()]

        await self.cache.get("demo", load)
        broken = True
        with self.assertRaises(ProviderProtocolError):
            await self.cache.get("demo", load, force_refresh=True)
        self.clock.value = 301
        for _ in range(2):
            with self.assertRaises(ProviderProtocolError):
                await self.cache.get("demo", load)
        self.assertEqual(calls, 4)
        self.assertEqual(self.cache.info()["entries"], 0)

    async def test_clear_generation_prevents_old_results_and_callbacks_from_replacing_new_load(self):
        old_gate = asyncio.Event()
        old_started = asyncio.Event()
        new_gate = asyncio.Event()
        new_started = asyncio.Event()

        async def old_load():
            old_started.set()
            await old_gate.wait()
            return [release("1.0")]

        async def new_load():
            new_started.set()
            await new_gate.wait()
            return [release("2.0")]

        old = asyncio.create_task(self.cache.get("demo", old_load))
        await old_started.wait()
        self.assertEqual(await self.cache.clear(), 0)
        current = asyncio.create_task(self.cache.get("demo", new_load))
        await new_started.wait()
        old_gate.set()
        self.assertEqual((await old)[0].version, "1.0")
        self.assertEqual(self.cache.info()["entries"], 0)
        self.assertEqual(self.cache.info()["inflight"], 1)
        new_gate.set()
        self.assertEqual((await current)[0].version, "2.0")
        self.assertEqual((await self.cache.get("demo", old_load))[0].version, "2.0")

    async def test_clear_counts_entries_and_resets_statistics(self):
        async def load():
            return [release()]

        await self.cache.get("one", load)
        await self.cache.get("two", load)
        await self.cache.get("one", load)
        self.assertEqual(await self.cache.clear(), 2)
        info = self.cache.info()
        self.assertEqual((info["entries"], info["hits"], info["misses"], info["refreshes"], info["coalesced"]), (0, 0, 0, 0, 0))

    async def test_lru_keeps_the_cache_bounded(self):
        cache = ReleaseMetadataCache(max_entries=2)
        self.extra_caches.append(cache)
        calls = []

        async def load(key):
            calls.append(key)
            return [release()]

        for key in ("a", "b", "a", "c", "b"):
            await cache.get(key, lambda key=key: load(key))
        self.assertEqual(calls, ["a", "b", "c", "b"])
        self.assertEqual(cache.info()["entries"], 2)

    async def test_zero_ttl_disables_persistent_hits(self):
        cache = ReleaseMetadataCache(ttl_seconds=0)
        self.extra_caches.append(cache)
        calls = 0

        async def load():
            nonlocal calls
            calls += 1
            return [release()]

        await cache.get("demo", load)
        await cache.get("demo", load)
        self.assertEqual(calls, 2)
        self.assertEqual(cache.info()["entries"], 0)

    async def test_invalid_metadata_cannot_fill_the_cache(self):
        for metadata in (None, {}, [object()], [release(), release()]):
            async def load(metadata=metadata):
                return metadata

            with self.subTest(metadata=metadata), self.assertRaises(ProviderProtocolError):
                await self.cache.get("demo", load)
        self.assertEqual(self.cache.info()["entries"], 0)

    async def test_shutdown_cancels_and_waits_for_load_cleanup(self):
        started = asyncio.Event()
        cleaned = asyncio.Event()

        async def load():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.set()

        caller = asyncio.create_task(self.cache.get("demo", load))
        await started.wait()
        await self.cache.shutdown()
        self.assertTrue(cleaned.is_set())
        self.assertEqual(self.cache.info()["pending_loads"], 0)
        self.assertTrue(self.cache.info()["closed"])
        with self.assertRaises(asyncio.CancelledError):
            await caller
        with self.assertRaises(RuntimeError):
            await self.cache.get("demo", load)
        await self.cache.shutdown()

    async def test_shutdown_also_tracks_loads_invalidated_by_clear(self):
        started = asyncio.Event()
        cleaned = asyncio.Event()

        async def load():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        caller = asyncio.create_task(self.cache.get("demo", load))
        await started.wait()
        await self.cache.clear()
        self.assertEqual(self.cache.info()["inflight"], 0)
        self.assertEqual(self.cache.info()["pending_loads"], 1)
        await self.cache.shutdown()
        self.assertTrue(cleaned.is_set())
        with self.assertRaises(asyncio.CancelledError):
            await caller

    async def test_configuration_validation(self):
        for kwargs in ({"ttl_seconds": -1}, {"ttl_seconds": float("inf")}, {"ttl_seconds": True},
                       {"max_entries": 0}, {"max_entries": False}, {"load_timeout_seconds": 0}, {"clock": None},
                       {"enabled": "yes"}, {"store": object()}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ReleaseMetadataCache(**kwargs)


def app_definition(provider="demo"):
    return AppDefinition(
        id="demo", name="Demo", description="Demo software", long_description="Demo software details",
        category="Tools", publisher="Demo", version="1.0", website_url="https://example.com/",
        download_page_url="https://example.com/download", icon_text="D", icon_background="#112233",
        installation_notes="Run installer", features=[], tags=[], download_provider=provider,
        latest_release_id=None, releases=[],
    )


class MetadataProvider:
    def __init__(self):
        self.version = "1.0"
        self.loads = []
        self.signs = 0
        self.gate = None
        self.started = asyncio.Event()

    async def get_releases(self, *, force_refresh=False):
        self.loads.append(force_refresh)
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        return [release(self.version)]

    async def resolve_download(self, item):
        self.signs += 1
        # Intentionally mutate the request copy; even this signer must not poison metadata cache.
        item.download_url = HttpUrl(f"{item.download_url}?sign=signature-{self.signs}")
        return item


class ReleaseServiceCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = app_definition()
        self.provider = MetadataProvider()
        self.service = ReleaseService(SimpleNamespace(apps=[self.app]), {"demo": self.provider}, timeout=0.3)

    async def asyncTearDown(self):
        await self.service.shutdown()

    async def test_hits_reuse_metadata_but_each_download_is_resolved_fresh(self):
        first = await self.service.latest(self.app, "x64")
        second = await self.service.latest(self.app, "x64")
        self.assertNotEqual(first.download_url, second.download_url)
        self.assertEqual(self.provider.loads, [True])
        self.assertEqual(self.provider.signs, 2)
        metadata = await self.service._metadata(self.app)
        self.assertNotIn("sign=", str(metadata[0].download_url))
        self.assertEqual(self.service.cache_info()["hits"], 2)

    async def test_explicit_latest_collection_and_resource_refresh_force_provider(self):
        await self.service.latest(self.app, "x64")
        self.provider.version = "2.0"
        latest = await self.service.latest(self.app, "x64", force_refresh=True)
        self.assertEqual(latest.version, "2.0")
        await self.service.collection(self.app, force_refresh=True)
        await self.service.resource(self.app, "2.0-windows-x64", force_refresh=True)
        self.assertEqual(self.provider.loads, [True, True, True, True])
        self.assertEqual(self.service.cache_info()["refreshes"], 3)

    async def test_clear_ensures_next_load_bypasses_inner_provider_cache(self):
        await self.service.latest(self.app, "x64")
        self.assertEqual(await self.service.clear_cache(), 1)
        self.provider.version = "2.0"
        self.assertEqual((await self.service.latest(self.app, "x64")).version, "2.0")
        self.assertEqual(self.provider.loads, [True, True])

    async def test_static_releases_do_not_use_metadata_cache(self):
        static = app_definition(None)
        static.releases = [release()]
        static.latest_release_id = static.releases[0].id
        service = ReleaseService(SimpleNamespace(apps=[static]), {}, timeout=0.3)
        try:
            self.assertEqual((await service.latest(static, "x64")).version, "1.0")
            self.assertEqual(service.cache_info()["entries"], 0)
            self.assertEqual(service.cache_info()["misses"], 0)
        finally:
            await service.shutdown()

    async def test_changed_version_retry_refreshes_outer_cache(self):
        class ChangingProvider(MetadataProvider):
            async def resolve_download(self, item):
                if item.version == "1.0":
                    self.version = "2.0"
                    raise ProviderReleaseChangedError("official version changed")
                return await super().resolve_download(item)

        provider = ChangingProvider()
        service = ReleaseService(SimpleNamespace(apps=[self.app]), {"demo": provider}, timeout=0.3)
        try:
            self.assertEqual((await service.latest(self.app, "x64")).version, "2.0")
            self.assertEqual(provider.loads, [True, True])
            self.assertEqual(service.cache_info()["refreshes"], 1)
        finally:
            await service.shutdown()

    async def test_one_api_deadline_does_not_cancel_other_shared_api_request(self):
        self.provider.gate = asyncio.Event()
        self.service._timeout = 0.01
        first = asyncio.create_task(self.service.latest(self.app, "x64"))
        await self.provider.started.wait()
        self.service._timeout = 0.3
        second = asyncio.create_task(self.service.latest(self.app, "x64"))
        with self.assertRaises(ProviderTimeoutError):
            await first
        self.provider.gate.set()
        self.assertEqual((await second).version, "1.0")
        self.assertEqual(self.provider.loads, [True])

    async def test_service_shutdown_stops_pending_metadata_load(self):
        self.provider.gate = asyncio.Event()
        caller = asyncio.create_task(self.service.latest(self.app, "x64"))
        await self.provider.started.wait()
        await self.service.shutdown()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        self.assertEqual(self.service.cache_info()["pending_loads"], 0)


if __name__ == "__main__":
    unittest.main()
