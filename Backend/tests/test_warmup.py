"""启动预热模块的单元测试：全部使用注入的假 provider，不访问真实官网。"""

import asyncio
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR.parent))

from Backend.cache import ReleaseMetadataCache
from Backend.models import AppDefinition, Release
from Backend.persistence import PersistentMetadataStore
from Backend.providers.base import ProviderUnavailableError
from Backend.warmup import (
    TIMEOUT_ERROR_MESSAGE,
    AppAvailability,
    CatalogWarmup,
    WarmupReport,
)


CATALOG_APP_IDS = ("qq", "wechat", "vscode", "7zip", "vlc", "firefox", "obs", "notepadplusplus")


def release(provider_id="demo", version="1.0", architecture="x64"):
    """构造一条未签名的版本元数据。"""

    return Release(
        id=f"{version}-windows-{architecture}",
        version=version,
        platform="windows",
        architecture=architecture,
        download_url=f"https://downloads.example/{provider_id}/{version}/{architecture}.exe",
        file_name=f"{provider_id}-{version}-{architecture}.exe",
        size_bytes=None,
        sha256=None,
    )


def app_definition(app_id, *, provider="demo", static_releases=()):
    """构造目录条目；``provider=None`` 表示目录里带静态版本的软件。"""

    static = tuple(static_releases)
    return AppDefinition(
        id=app_id,
        name=app_id,
        description="测试用软件",
        long_description="测试用的软件目录条目",
        category="测试",
        publisher="测试",
        version=static[0].version if static else "以官网为准",
        website_url="https://example.com/",
        download_page_url="https://example.com/download",
        icon_text="T",
        icon_background="#112233",
        installation_notes="运行安装程序",
        features=[],
        tags=[],
        download_provider=provider,
        latest_release_id=static[0].id if static else None,
        releases=list(static),
    )


class ConcurrencyProbe:
    """记录 provider 调用同时在场的最大数量。"""

    def __init__(self):
        self.active = 0
        self.peak = 0

    def enter(self):
        self.active += 1
        self.peak = max(self.peak, self.active)

    def leave(self):
        self.active -= 1


class FakeProvider:
    """确定性的假 provider：可配置失败、延时与返回的架构，测试不会联网。"""

    def __init__(
        self,
        provider_id,
        *,
        version="1.0",
        architectures=("x64",),
        failure=None,
        delay=0.0,
        probe=None,
    ):
        self.id = provider_id
        self.version = version
        self.architectures = list(architectures)
        self.failure = failure
        self.delay = delay
        self.probe = probe
        self.calls = 0
        self.forced_calls = 0

    async def get_releases(self, *, force_refresh=False):
        self.calls += 1
        if force_refresh:
            self.forced_calls += 1
        if self.probe is not None:
            self.probe.enter()
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.failure is not None:
                raise self.failure
            return [
                release(self.id, self.version, architecture)
                for architecture in self.architectures
            ]
        finally:
            if self.probe is not None:
                self.probe.leave()

    async def resolve_download(self, item):
        raise AssertionError("预热只抓取元数据，不应签名下载地址")


class ExplodingProvider:
    """一旦被调用就让测试失败的 provider。"""

    def __init__(self):
        self.calls = 0

    async def get_releases(self, *, force_refresh=False):
        self.calls += 1
        raise AssertionError("静态版本软件不应该调用任何 provider")

    async def resolve_download(self, item):
        raise AssertionError("静态版本软件不应该调用任何 provider")


class WarmupTestCase(unittest.IsolatedAsyncioTestCase):
    """公共夹具：临时目录、真实缓存实例与自动清理。"""

    async def asyncSetUp(self):
        self._temp_directories: list[Path] = []
        self._caches: list[ReleaseMetadataCache] = []
        self.directory = self.new_directory()

    async def asyncTearDown(self):
        for cache in self._caches:
            await cache.shutdown()
        for directory in self._temp_directories:
            shutil.rmtree(directory, ignore_errors=True)

    def new_directory(self) -> Path:
        directory = Path(tempfile.mkdtemp(prefix="wam-warmup-"))
        self._temp_directories.append(directory)
        return directory

    def build_cache(
        self, *, load_timeout_seconds=5.0, store_ttl_seconds=3600.0
    ) -> ReleaseMetadataCache:
        """真实的内存缓存 + 临时目录上的真实磁盘快照。"""

        store = PersistentMetadataStore(self.directory, ttl_seconds=store_ttl_seconds)
        cache = ReleaseMetadataCache(
            store=store, ttl_seconds=300.0, load_timeout_seconds=load_timeout_seconds
        )
        self._caches.append(cache)
        return cache

    def build_warmup(
        self,
        apps,
        providers,
        *,
        cache=None,
        timeout_seconds=5.0,
        max_concurrency=4,
        use_stale_cache=True,
    ) -> CatalogWarmup:
        return CatalogWarmup(
            SimpleNamespace(apps=tuple(apps)),
            providers,
            cache if cache is not None else self.build_cache(),
            timeout_seconds=timeout_seconds,
            max_concurrency=max_concurrency,
            use_stale_cache=use_stale_cache,
        )


class WarmupSuccessTests(WarmupTestCase):
    async def test_all_providers_succeed_and_report_counts(self):
        apps = [app_definition(app_id, provider=app_id) for app_id in CATALOG_APP_IDS]
        providers = {app_id: FakeProvider(app_id) for app_id in CATALOG_APP_IDS}
        warmup = self.build_warmup(apps, providers)

        report = await warmup.warm_all()

        self.assertIsInstance(report, WarmupReport)
        self.assertEqual(len(report.results), len(CATALOG_APP_IDS))
        self.assertEqual({result.status for result in report.results}, {"ok"})
        self.assertEqual(
            report.summary(),
            {
                "total": 8,
                "ok": 8,
                "cached": 0,
                "unavailable": 0,
                "duration_seconds": report.duration_seconds,
                "started_at": report.started_at,
            },
        )
        self.assertGreaterEqual(report.duration_seconds, 0.0)
        for app_id in CATALOG_APP_IDS:
            with self.subTest(app_id=app_id):
                availability = warmup.availability(app_id)
                self.assertIsInstance(availability, AppAvailability)
                self.assertEqual(availability.status, "ok")
                self.assertEqual(len(availability.releases), 1)
                self.assertIsNone(availability.error)
                self.assertGreater(availability.checked_at, 0.0)
                self.assertTrue(warmup.is_available(app_id))
                # 预热必须强制刷新，否则重启后只会直接吃磁盘快照。
                self.assertEqual(providers[app_id].calls, 1)
                self.assertEqual(providers[app_id].forced_calls, 1)
        self.assertEqual(warmup.available_app_ids(), frozenset(CATALOG_APP_IDS))
        self.assertEqual(warmup.report()["counts"], {"ok": 8, "cached": 0, "unavailable": 0})

    async def test_provider_without_releases_is_still_available(self):
        app = app_definition("demo")
        provider = FakeProvider("demo", architectures=())
        warmup = self.build_warmup([app], {"demo": provider})

        report = await warmup.warm_all()

        availability = report.results[0]
        self.assertEqual(availability.status, "ok")
        self.assertEqual(availability.releases, ())
        self.assertIsNone(availability.error)
        # 该软件当前没有安装包，不等于抓取失败，不能被隐藏。
        self.assertTrue(warmup.is_available("demo"))
        self.assertEqual(warmup.available_app_ids(), frozenset({"demo"}))
        self.assertEqual(report.summary()["ok"], 1)
        self.assertEqual(warmup.report()["apps"]["demo"]["release_count"], 0)


class WarmupFallbackTests(WarmupTestCase):
    async def test_failed_provider_falls_back_to_persistent_snapshot(self):
        app = app_definition("demo")
        provider = FakeProvider("demo")
        first_cache = self.build_cache()
        first_warmup = self.build_warmup([app], {"demo": provider}, cache=first_cache)
        self.assertEqual((await first_warmup.warm_all()).results[0].status, "ok")
        # 模拟后端重启：旧缓存关闭，新缓存重新扫描同一个磁盘目录。
        await first_cache.shutdown()

        provider.failure = ProviderUnavailableError("官网暂时不可用")
        restarted_cache = self.build_cache()
        restarted = self.build_warmup([app], {"demo": provider}, cache=restarted_cache)

        report = await restarted.warm_all()

        availability = report.results[0]
        self.assertEqual(availability.status, "cached")
        self.assertIsNone(availability.error)
        self.assertEqual(len(availability.releases), 1)
        self.assertEqual(availability.releases[0].version, "1.0")
        self.assertTrue(restarted.is_available("demo"))
        self.assertEqual(restarted.available_app_ids(), frozenset({"demo"}))
        self.assertEqual(restarted.report()["counts"], {"ok": 0, "cached": 1, "unavailable": 0})
        # 第二次仍然先强制抓官网，失败后才回退到快照。
        self.assertEqual(provider.calls, 2)
        self.assertEqual(provider.forced_calls, 2)

    async def test_persistent_snapshot_maps_to_cached_when_not_forced(self):
        app = app_definition("demo")
        provider = FakeProvider("demo")
        first_cache = self.build_cache()
        first_warmup = self.build_warmup([app], {"demo": provider}, cache=first_cache)
        await first_warmup.warm_all()
        await first_cache.shutdown()

        restarted = self.build_warmup([app], {"demo": provider}, cache=self.build_cache())

        availability = (await restarted.warm_all(force_refresh=False)).results[0]

        # force_refresh=False 时直接复用磁盘快照，来源是 persistent，对外记为 cached。
        self.assertEqual(availability.status, "cached")
        self.assertIsNone(availability.error)
        self.assertEqual(len(availability.releases), 1)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(restarted.report()["counts"], {"ok": 0, "cached": 1, "unavailable": 0})

    async def test_expired_snapshot_is_still_usable_as_fallback(self):
        app = app_definition("demo")
        provider = FakeProvider("demo")
        # 磁盘快照 TTL 为 0，抓取成功后立即过期，只能当作“陈旧但可用”的兜底。
        expired_cache = self.build_cache(store_ttl_seconds=0.0)
        first_warmup = self.build_warmup([app], {"demo": provider}, cache=expired_cache)
        self.assertEqual((await first_warmup.warm_all()).results[0].status, "ok")
        await expired_cache.shutdown()

        provider.failure = ProviderUnavailableError("官网暂时不可用")
        restarted = self.build_warmup(
            [app], {"demo": provider}, cache=self.build_cache(store_ttl_seconds=0.0)
        )

        availability = (await restarted.warm_all()).results[0]

        self.assertEqual(availability.status, "cached")
        self.assertIsNone(availability.error)
        self.assertEqual(len(availability.releases), 1)
        self.assertEqual(availability.releases[0].version, "1.0")
        self.assertTrue(restarted.is_available("demo"))

    async def test_failure_without_snapshot_marks_app_unavailable(self):
        app = app_definition("demo")
        provider = FakeProvider("demo", failure=ProviderUnavailableError("官网暂时不可用"))
        warmup = self.build_warmup([app], {"demo": provider})

        report = await warmup.warm_all()

        availability = report.results[0]
        self.assertEqual(availability.status, "unavailable")
        self.assertEqual(availability.releases, ())
        self.assertIn("官网暂时不可用", availability.error)
        self.assertFalse(warmup.is_available("demo"))
        self.assertEqual(warmup.available_app_ids(), frozenset())
        self.assertEqual(warmup.report()["counts"], {"ok": 0, "cached": 0, "unavailable": 1})

    async def test_unexpected_exception_is_captured_as_unavailable(self):
        app = app_definition("demo")
        provider = FakeProvider("demo", failure=ValueError("解析官网配置失败"))
        warmup = self.build_warmup([app], {"demo": provider})

        # warm_app 绝不抛异常（取消除外），普通异常也要变成 unavailable。
        availability = await warmup.warm_app(app)

        self.assertEqual(availability.status, "unavailable")
        self.assertEqual(availability.releases, ())
        self.assertIn("解析官网配置失败", availability.error)
        self.assertFalse(warmup.is_available("demo"))

    async def test_timeout_is_reported_as_unavailable(self):
        app = app_definition("demo")
        provider = FakeProvider("demo", delay=10.0)
        warmup = self.build_warmup([app], {"demo": provider}, timeout_seconds=0.01)

        availability = await warmup.warm_app(app)

        self.assertEqual(availability.status, "unavailable")
        self.assertEqual(availability.error, TIMEOUT_ERROR_MESSAGE)
        self.assertIn("超时", availability.error)
        self.assertFalse(warmup.is_available("demo"))


class WarmupStaticAppTests(WarmupTestCase):
    async def test_static_releases_never_call_a_provider(self):
        static_release = release("static", version="2.0")
        static_app = app_definition("demo", provider=None, static_releases=(static_release,))
        empty_app = app_definition("empty", provider=None)
        exploding = ExplodingProvider()
        cache = self.build_cache()
        warmup = self.build_warmup([static_app, empty_app], {"demo": exploding}, cache=cache)

        report = await warmup.warm_all()

        self.assertEqual([result.app_id for result in report.results], ["demo", "empty"])
        self.assertEqual(report.results[0].status, "ok")
        self.assertEqual(report.results[0].releases, (static_release,))
        self.assertEqual(report.results[1].status, "ok")
        self.assertEqual(report.results[1].releases, ())
        self.assertEqual(exploding.calls, 0)
        # 静态版本软件完全不碰缓存。
        self.assertEqual(cache.info()["misses"], 0)
        self.assertEqual(cache.info()["entries"], 0)
        self.assertTrue(warmup.is_available("demo"))
        self.assertTrue(warmup.is_available("empty"))


class WarmupConcurrencyTests(WarmupTestCase):
    async def test_max_concurrency_one_serializes_provider_calls(self):
        probe = ConcurrencyProbe()
        apps = [app_definition(f"app{index}", provider=f"provider{index}") for index in range(4)]
        providers = {
            f"provider{index}": FakeProvider(
                f"provider{index}", delay=0.02, probe=probe
            )
            for index in range(4)
        }
        warmup = self.build_warmup(apps, providers, max_concurrency=1)

        report = await warmup.warm_all()

        self.assertEqual({result.status for result in report.results}, {"ok"})
        self.assertEqual(probe.peak, 1)

    async def test_higher_concurrency_allows_parallel_provider_calls(self):
        probe = ConcurrencyProbe()
        apps = [app_definition(f"app{index}", provider=f"provider{index}") for index in range(4)]
        providers = {
            f"provider{index}": FakeProvider(
                f"provider{index}", delay=0.02, probe=probe
            )
            for index in range(4)
        }
        warmup = self.build_warmup(apps, providers, max_concurrency=4)

        await warmup.warm_all()

        self.assertGreater(probe.peak, 1)


class WarmupRegistryTests(WarmupTestCase):
    async def test_report_structure_and_unknown_apps_are_available(self):
        app = app_definition("demo")
        warmup = self.build_warmup([app], {"demo": FakeProvider("demo")})

        # 尚未预热时默认可用，行为与改造前保持一致。
        self.assertTrue(warmup.is_available("never-warmed"))
        self.assertIsNone(warmup.availability("never-warmed"))
        self.assertEqual(
            warmup.report(), {"apps": {}, "counts": {"ok": 0, "cached": 0, "unavailable": 0}}
        )

        await warmup.warm_all()

        snapshot = warmup.report()
        self.assertEqual(set(snapshot), {"apps", "counts"})
        self.assertEqual(set(snapshot["counts"]), {"ok", "cached", "unavailable"})
        self.assertEqual(set(snapshot["apps"]), {"demo"})
        self.assertEqual(
            set(snapshot["apps"]["demo"]),
            {"status", "error", "checked_at", "release_count"},
        )
        self.assertEqual(snapshot["apps"]["demo"]["status"], "ok")
        self.assertIsNone(snapshot["apps"]["demo"]["error"])
        self.assertEqual(snapshot["apps"]["demo"]["release_count"], 1)
        self.assertGreater(snapshot["apps"]["demo"]["checked_at"], 0.0)
        self.assertTrue(warmup.is_available("unknown-app"))

    async def test_later_success_replaces_a_previous_failure(self):
        app = app_definition("demo")
        provider = FakeProvider("demo", failure=ProviderUnavailableError("官网暂时不可用"))
        warmup = self.build_warmup([app], {"demo": provider})

        await warmup.warm_all()
        self.assertFalse(warmup.is_available("demo"))
        provider.failure = None

        await warmup.warm_all()

        self.assertTrue(warmup.is_available("demo"))
        self.assertEqual(warmup.report()["apps"]["demo"]["status"], "ok")
        self.assertEqual(warmup.report()["counts"], {"ok": 1, "cached": 0, "unavailable": 0})


class WarmupSelectionTests(WarmupTestCase):
    async def test_warm_all_can_be_repeated_and_can_refresh_a_subset(self):
        apps = [
            app_definition("alpha", provider="alpha-provider"),
            app_definition("beta", provider="beta-provider"),
        ]
        providers = {
            "alpha-provider": FakeProvider("alpha-provider"),
            "beta-provider": FakeProvider("beta-provider"),
        }
        warmup = self.build_warmup(apps, providers)

        await warmup.warm_all()
        await warmup.warm_all()
        self.assertEqual(providers["alpha-provider"].calls, 2)
        self.assertEqual(providers["beta-provider"].calls, 2)

        # 不强制刷新时命中内存快照：来源 memory，对外仍记为 ok。
        memory_hits = await warmup.warm_all(force_refresh=False)
        self.assertEqual({result.status for result in memory_hits.results}, {"ok"})
        self.assertEqual(providers["alpha-provider"].calls, 2)
        self.assertEqual(providers["beta-provider"].calls, 2)

        subset = await warmup.warm_all(app_ids=["alpha", "missing-app"])
        self.assertEqual([result.app_id for result in subset.results], ["alpha"])
        self.assertEqual(providers["alpha-provider"].calls, 3)
        self.assertEqual(providers["beta-provider"].calls, 2)
        # 子集刷新不能丢掉另一个软件已经记录的可用状态。
        self.assertEqual(warmup.report()["counts"], {"ok": 2, "cached": 0, "unavailable": 0})
        self.assertEqual(warmup.available_app_ids(), frozenset({"alpha", "beta"}))

        empty = await warmup.warm_all(app_ids=[])
        self.assertEqual(empty.results, ())
        self.assertEqual(empty.summary()["total"], 0)


if __name__ == "__main__":
    unittest.main()
