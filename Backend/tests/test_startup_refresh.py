"""启动预热、持久化缓存回退与逐 app 隔离的 API 层测试。

这些测试覆盖 ``/api/v1/apps`` 曾经因为单个 provider 超时整体返回 504 的回归场景，以及
"启动先抓取一次 → 失败回退持久化缓存 → 都没有则隐藏软件"的新启动流程。
"""

import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR.parent))

from Backend.config import BackendSettings, CacheConfig, ProvidersConfig, RefreshConfig
from Backend.main import create_app as create_application
from Backend.providers.base import ProviderTimeoutError, ProviderUnavailableError
from Backend.tests.test_api import EmptyProvider, FIXTURE_EXTRA_DIRECTORY, FakeQQProvider

PROVIDER_IDS = ("wechat", "vscode", "7zip", "vlc", "firefox", "obs", "notepadplusplus")


class FailingProvider(FakeQQProvider):
    """官网不可用：用于验证回退与隐藏逻辑。"""

    async def get_releases(self, *, force_refresh=False):
        self.force_refreshes += int(force_refresh)
        raise ProviderUnavailableError("官方服务器暂时不可用")


class TimingOutProvider(FakeQQProvider):
    """官网超时：以前会让整个 /api/v1/apps 变成 504。"""

    async def get_releases(self, *, force_refresh=False):
        self.force_refreshes += int(force_refresh)
        raise ProviderTimeoutError("获取软件安装包信息超时，请稍后重试。")


class SigningFailsProvider(FakeQQProvider):
    """元数据抓得到，但签名/下载地址解析失败（例如官方签名接口超时）。"""

    async def resolve_download(self, release):
        raise ProviderTimeoutError("获取软件安装包信息超时，请稍后重试。")


class SlowProvider(FakeQQProvider):
    async def get_releases(self, *, force_refresh=False):
        self.force_refreshes += int(force_refresh)
        await asyncio.sleep(5)
        return []


def create_app(*, release_providers=None, settings=None):
    providers = {provider_id: EmptyProvider() for provider_id in PROVIDER_IDS}
    providers["qq"] = FakeQQProvider()
    providers.update(release_providers or {})
    if settings is None:
        settings = BackendSettings(
            cache=CacheConfig(persist=False),
            refresh=RefreshConfig(on_startup=True, interval_seconds=0),
        )
    return create_application(
        release_providers=providers, settings=settings, extra_directory=FIXTURE_EXTRA_DIRECTORY
    )


class StartupWarmupTests(unittest.TestCase):
    def test_startup_scrapes_every_provider_before_serving(self):
        provider = FakeQQProvider()
        with TestClient(create_app(release_providers={"qq": provider})) as client:
            self.assertEqual(provider.force_refreshes, 1)
            status = client.get("/api/v1/status")
            self.assertEqual(status.status_code, 200)
            self.assertEqual(status.headers["cache-control"], "no-store")
            payload = status.json()
            self.assertEqual(payload["warmup"]["counts"]["ok"], 8)
            self.assertEqual(payload["warmup"]["apps"]["qq"]["status"], "ok")
            self.assertEqual(payload["warmup"]["apps"]["qq"]["release_count"], 3)
            catalog = client.get("/api/v1/apps").json()
            self.assertEqual(catalog["total"], 8)
            self.assertEqual(client.get("/api/v1/apps/qq").json()["version"], "9.9.37")

    def test_startup_scrape_is_persisted_for_the_next_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = BackendSettings(
                cache=CacheConfig(persist=True, directory=Path(directory)),
                refresh=RefreshConfig(on_startup=True, interval_seconds=0),
            )
            with TestClient(create_app(release_providers={"qq": FakeQQProvider()}, settings=settings)) as client:
                cache_info = client.get("/api/v1/cache").json()
                self.assertIn("warmup", cache_info)
                self.assertIn("refresh", cache_info)
                self.assertGreaterEqual(cache_info["persistent"]["entries"], 1)
            self.assertNotEqual(list(Path(directory).glob("*.json")), [])

    def test_failed_scrape_falls_back_to_the_persistent_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = BackendSettings(
                cache=CacheConfig(persist=True, directory=Path(directory)),
                refresh=RefreshConfig(on_startup=True, interval_seconds=0),
            )
            with TestClient(create_app(release_providers={"qq": FakeQQProvider()}, settings=settings)):
                pass

            failing = FailingProvider()
            with TestClient(create_app(release_providers={"qq": failing}, settings=settings)) as client:
                self.assertEqual(failing.force_refreshes, 1)
                status = client.get("/api/v1/status").json()
                self.assertEqual(status["warmup"]["apps"]["qq"]["status"], "cached")
                catalog = client.get("/api/v1/apps").json()
                self.assertEqual(catalog["total"], 8)
                qq = next(app for app in catalog["items"] if app["id"] == "qq")
                self.assertEqual(qq["version"], "9.9.37")
                detail = client.get("/api/v1/apps/qq")
                self.assertEqual(detail.status_code, 200)
                self.assertEqual(detail.json()["version"], "9.9.37")

    def test_app_without_any_data_is_hidden_and_answers_503(self):
        with TestClient(create_app(release_providers={"qq": FailingProvider()})) as client:
            catalog = client.get("/api/v1/apps")
            self.assertEqual(catalog.status_code, 200)
            payload = catalog.json()
            self.assertEqual(payload["total"], 7)
            self.assertNotIn("qq", [app["id"] for app in payload["items"]])
            self.assertEqual(payload["total"], len(payload["items"]))
            detail = client.get("/api/v1/apps/qq")
            self.assertEqual(detail.status_code, 503)
            self.assertIn("detail", detail.json())
            self.assertEqual(client.get("/api/v1/health").status_code, 200)
            self.assertEqual(client.get("/api/v1/apps/qq/icon").status_code, 200)
            status = client.get("/api/v1/status").json()
            self.assertEqual(status["warmup"]["apps"]["qq"]["status"], "unavailable")
            self.assertEqual(status["warmup"]["counts"]["unavailable"], 1)

    def test_hidden_app_stays_out_of_filtered_and_sorted_views(self):
        with TestClient(create_app(release_providers={"qq": FailingProvider()})) as client:
            for params in ({"search": "QQ"}, {"sort": "name"}, {"category": "社交通讯"}):
                with self.subTest(params=params):
                    payload = client.get("/api/v1/apps", params=params).json()
                    self.assertNotIn("qq", [app["id"] for app in payload["items"]])
                    self.assertEqual(payload["total"], len(payload["items"]))
            self.assertEqual(client.get("/api/v1/apps", params={"search": "QQ"}).json()["total"], 0)


class CatalogIsolationTests(unittest.TestCase):
    """单个 provider 失败不应再让整个目录变成 502/504。"""

    def test_one_failing_provider_does_not_fail_the_catalog(self):
        for provider, expected_status in ((FailingProvider(), 200), (TimingOutProvider(), 200)):
            with self.subTest(provider=type(provider).__name__):
                settings = BackendSettings(
                    cache=CacheConfig(persist=False),
                    refresh=RefreshConfig(on_startup=False, interval_seconds=0),
                )
                with TestClient(create_app(release_providers={"qq": provider}, settings=settings)) as client:
                    for params in ({"refresh": "true"}, {"architecture": "x64"}, {}):
                        with self.subTest(params=params):
                            response = client.get("/api/v1/apps", params=params)
                            self.assertEqual(response.status_code, expected_status)
                            self.assertEqual(response.headers["cache-control"], "no-store")
                            self.assertEqual(response.json()["total"], 7)

    def test_explicit_release_endpoint_still_reports_gateway_errors(self):
        settings = BackendSettings(
            cache=CacheConfig(persist=False),
            refresh=RefreshConfig(on_startup=False, interval_seconds=0),
        )
        with TestClient(create_app(release_providers={"qq": TimingOutProvider()}, settings=settings)) as client:
            self.assertEqual(client.get("/api/v1/apps/qq/releases/latest").status_code, 504)
            self.assertEqual(client.get("/api/v1/apps/qq/releases/9.9.37-windows-x64").status_code, 504)

    def test_drop_unavailable_apps_false_keeps_the_app_without_a_package(self):
        settings = BackendSettings(
            cache=CacheConfig(persist=False),
            refresh=RefreshConfig(on_startup=True, interval_seconds=0, drop_unavailable_apps=False),
        )
        with TestClient(create_app(release_providers={"qq": FailingProvider()}, settings=settings)) as client:
            payload = client.get("/api/v1/apps").json()
            self.assertEqual(payload["total"], 8)
            qq = next(app for app in payload["items"] if app["id"] == "qq")
            self.assertIsNone(qq["latest_release"])
            self.assertEqual(qq["version"], "以官网为准")


    def test_forgetful_refresh_falls_back_to_the_snapshot_instead_of_hiding_app(self):
        """真实场景：客户端列表带 refresh=true，某个官网超时但磁盘上有快照。"""

        with tempfile.TemporaryDirectory() as directory:
            settings = BackendSettings(
                cache=CacheConfig(persist=True, directory=Path(directory)),
                refresh=RefreshConfig(on_startup=False, interval_seconds=0),
            )
            with TestClient(create_app(release_providers={"qq": FakeQQProvider()}, settings=settings)) as client:
                self.assertEqual(client.get("/api/v1/apps").json()["total"], 8)

            with TestClient(create_app(release_providers={"qq": TimingOutProvider()}, settings=settings)) as client:
                catalog = client.get("/api/v1/apps", params={"refresh": "true"})
                self.assertEqual(catalog.status_code, 200)
                payload = catalog.json()
                self.assertEqual(payload["total"], 8)
                qq = next(app for app in payload["items"] if app["id"] == "qq")
                self.assertEqual(qq["version"], "9.9.37")


    def test_signing_failure_keeps_the_card_without_a_download_link(self):
        with TestClient(create_app(release_providers={"qq": SigningFailsProvider()})) as client:
            for params in ({}, {"refresh": "true"}):
                with self.subTest(params=params):
                    payload = client.get("/api/v1/apps", params=params).json()
                    self.assertEqual(payload["total"], 8)
                    qq = next(app for app in payload["items"] if app["id"] == "qq")
                    self.assertIsNone(qq["latest_release"])
                    self.assertEqual(qq["version"], "以官网为准")
            self.assertEqual(client.get("/api/v1/apps/qq").status_code, 504)


class RefreshScheduleTests(unittest.TestCase):
    def test_zero_interval_disables_the_scheduler(self):
        provider = FakeQQProvider()
        with TestClient(create_app(release_providers={"qq": provider})) as client:
            report = client.get("/api/v1/status").json()["refresh"]
            self.assertFalse(report["enabled"])
            self.assertFalse(report["running"])
            self.assertEqual(provider.force_refreshes, 1)

    def test_manual_refresh_endpoint_is_disabled(self):
        provider = FakeQQProvider()
        with TestClient(create_app(release_providers={"qq": provider})) as client:
            self.assertEqual(provider.force_refreshes, 1)
            response = client.post("/api/v1/refresh")
            self.assertEqual(response.status_code, 404)
            # 禁用后不会触发任何新的抓取，也不会计入定时刷新统计。
            self.assertEqual(provider.force_refreshes, 1)
            report = client.get("/api/v1/status").json()["refresh"]
            self.assertEqual(report["runs"], 0)

    def test_scheduler_interval_is_reported_from_the_configuration(self):
        settings = BackendSettings(
            cache=CacheConfig(persist=False),
            refresh=RefreshConfig(on_startup=False, interval_seconds=120, jitter_seconds=5),
        )
        with TestClient(create_app(release_providers={"qq": FakeQQProvider()}, settings=settings)) as client:
            report = client.get("/api/v1/status").json()["refresh"]
            self.assertTrue(report["enabled"])
            self.assertTrue(report["running"])
            self.assertEqual(report["interval_seconds"], 120.0)
            self.assertEqual(report["jitter_seconds"], 5.0)

    def test_startup_budget_does_not_block_serving(self):
        settings = BackendSettings(
            cache=CacheConfig(persist=False),
            providers=ProvidersConfig(timeout_seconds=0.2),
            refresh=RefreshConfig(on_startup=True, startup_timeout_seconds=0.05, interval_seconds=0),
        )
        started = time.monotonic()
        with TestClient(create_app(release_providers={"qq": SlowProvider()}, settings=settings)) as client:
            startup_seconds = time.monotonic() - started
            self.assertLess(startup_seconds, 2.0)
            self.assertEqual(client.get("/api/v1/health").status_code, 200)
            catalog = client.get("/api/v1/apps")
            self.assertEqual(catalog.status_code, 200)
            self.assertEqual(catalog.json()["total"], 7)


if __name__ == "__main__":
    unittest.main()
