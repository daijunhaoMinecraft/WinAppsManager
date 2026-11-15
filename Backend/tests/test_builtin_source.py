"""内置精选源启用范围（``builtin`` 段）与白名单过滤的测试。

覆盖：

- ``builtin.app_ids`` 白名单只暴露指定软件、其余软件 404，数据条目仍完整加载；
- 只为白名单内的软件创建 provider / 预热 / 定时刷新所需的数据；
- ``builtin.enabled=false`` 时 ``/api/v1/apps*`` 与 ``/api/v1/refresh`` 返回 503，
  但不创建 provider、不崩溃的 ``/health``、``/status``、``/cache`` 仍正常；
- ``/api/v1/sources`` 的 builtin 项反映配置的 enabled 与过滤后的 app_count；
- 未知 app id 让启动失败；
- 配置默认值、提交的 ``config.json`` 与环境变量覆盖。

全部使用 TestClient + 注入的假 provider，不访问真实网络，也不读写开发机的持久缓存。
"""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient


BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR.parent))

from Backend.catalog import CatalogRepository
from Backend.config import (
    DEFAULT_CONFIG_FILE,
    BackendSettings,
    BuiltinConfig,
    CacheConfig,
    LenovoConfig,
    RefreshConfig,
)
from Backend.main import DATA_FILE, ICON_DIR, create_app as create_application
from Backend.models import Release


WHITELIST = ("clashverge", "telegram")
DISABLED_DETAIL = "内置精选源未启用。"
# 与 test_api.py 相同：指向不存在的 apps.d，使内置精选目录固定为 8 款（用于「空白名单=全部」）。
FIXTURE_EXTRA_DIRECTORY = BACKEND_DIR / "tests" / "no-extra-apps.d"
EMPTY_PROVIDER_IDS = ("wechat", "vscode", "7zip", "vlc", "firefox", "obs", "notepadplusplus", "qq")


class FakeProvider:
    """确定性 provider：记录调用次数，返回固定版本。"""

    def __init__(self, app_id, architectures=("x64",)):
        self.id = app_id
        self.architectures = architectures
        self.calls = 0
        self.force_refreshes = 0

    async def get_releases(self, *, force_refresh=False):
        self.calls += 1
        self.force_refreshes += int(force_refresh)
        return [
            Release(
                id=f"1.2.3-windows-{architecture}",
                version="1.2.3",
                platform="windows",
                architecture=architecture,
                download_url=f"https://downloads.example/{self.id}-1.2.3-{architecture}.exe",
                file_name=f"{self.id}-1.2.3-{architecture}.exe",
            )
            for architecture in self.architectures
        ]

    async def resolve_download(self, release):
        return release.model_copy(deep=True)


def builtin_settings(*, enabled=True, app_ids=WHITELIST):
    # lenovo 关闭：这些用例只关心内置源，不必创建联想服务。
    return BackendSettings(
        cache=CacheConfig(persist=False),
        refresh=RefreshConfig(on_startup=False, interval_seconds=0),
        lenovo=LenovoConfig(enabled=False),
        builtin=BuiltinConfig(enabled=enabled, app_ids=list(app_ids)),
    )


def build_app(*, settings=None, release_providers=None):
    if settings is None:
        settings = builtin_settings()
    if release_providers is None:
        release_providers = {
            "clashverge": FakeProvider("clashverge"),
            "telegram": FakeProvider("telegram", ("x64", "x86", "arm64")),
        }
    return create_application(release_providers=release_providers, settings=settings)


def write_config(directory, payload):
    path = Path(directory) / "config.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


class BuiltinWhitelistTests(unittest.TestCase):
    def test_catalog_exposes_only_the_whitelisted_apps(self):
        with TestClient(build_app()) as client:
            response = client.get("/api/v1/apps")
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["total"], 2)
            self.assertEqual({item["id"] for item in payload["items"]}, {"clashverge", "telegram"})

            # 搜索/分类同样只在白名单内生效。
            search = client.get("/api/v1/apps", params={"search": "Telegram"}).json()
            self.assertEqual([item["id"] for item in search["items"]], ["telegram"])
            hidden_category = client.get("/api/v1/apps", params={"category": "开发工具"}).json()
            self.assertEqual(hidden_category, {"items": [], "total": 0})

    def test_hidden_apps_are_404_everywhere(self):
        with TestClient(build_app()) as client:
            for path in (
                "/api/v1/apps/qq",
                "/api/v1/apps/wechat",
                "/api/v1/apps/qq/releases",
                "/api/v1/apps/qq/releases/latest",
                "/api/v1/apps/qq/releases/9.9.37-windows-x64",
                "/api/v1/apps/qq/icon",
            ):
                with self.subTest(path=path):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 404)

    def test_whitelisted_apps_keep_details_and_releases(self):
        with TestClient(build_app()) as client:
            clash = client.get("/api/v1/apps/clashverge")
            self.assertEqual(clash.status_code, 200)
            self.assertEqual(clash.json()["version"], "1.2.3")
            self.assertEqual(clash.json()["latest_release"]["architecture"], "x64")

            telegram = client.get("/api/v1/apps/telegram")
            self.assertEqual(telegram.status_code, 200)
            releases = client.get("/api/v1/apps/telegram/releases").json()
            self.assertEqual(releases["total"], 3)
            self.assertEqual(
                [release["architecture"] for release in releases["items"]],
                ["x64", "x86", "arm64"],
            )
            latest = client.get("/api/v1/apps/telegram/releases/latest", params={"architecture": "x86"})
            self.assertEqual(latest.status_code, 200)
            self.assertEqual(latest.json()["architecture"], "x86")

    def test_sources_reports_builtin_enabled_and_filtered_app_count(self):
        with TestClient(build_app()) as client:
            response = client.get("/api/v1/sources")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        by_id = {item["id"]: item for item in response.json()["items"]}
        self.assertEqual(by_id["builtin"]["enabled"], True)
        self.assertEqual(by_id["builtin"]["app_count"], 2)
        self.assertFalse(by_id["builtin"]["is_default"])
        # 搜索方式由服务端下发：内置精选取白名单、联想完整目录 → catalog；360 子集 → api。
        # online_search 只是旧客户端兼容字段。
        self.assertEqual(by_id["builtin"]["search_mode"], "catalog")
        self.assertFalse(by_id["builtin"]["online_search"])
        self.assertEqual(by_id["lenovo"]["search_mode"], "catalog")
        self.assertFalse(by_id["lenovo"]["online_search"])
        self.assertEqual(by_id["soft360"]["search_mode"], "api")
        self.assertTrue(by_id["soft360"]["online_search"])
        self.assertEqual(by_id["lenovo"]["enabled"], False)
        self.assertIsNone(by_id["lenovo"]["app_count"])

    def test_full_catalog_is_still_loaded_but_only_whitelisted_apps_are_exposed(self):
        api = build_app()
        with TestClient(api) as client:
            self.assertEqual(client.get("/api/v1/health").status_code, 200)
            catalog = api.state.catalog
            self.assertEqual(catalog.all_app_count, 45)
            self.assertEqual(len(catalog.all_apps), 45)
            self.assertEqual(catalog.app_count, 2)
            self.assertEqual(catalog.allowed_app_ids, frozenset(WHITELIST))
            self.assertIsNotNone(catalog.get_app("telegram"))
            self.assertIsNone(catalog.get_app("qq"))

    def test_only_required_providers_are_registered_and_refreshed(self):
        clash = FakeProvider("clashverge")
        telegram = FakeProvider("telegram", ("x64", "x86", "arm64"))
        hidden = FakeProvider("qq")
        api = build_app(
            release_providers={"clashverge": clash, "telegram": telegram, "qq": hidden}
        )
        with TestClient(api) as client:
            # 白名单外的 provider 不会被注册（因此不可能被抓取）。
            self.assertEqual(set(api.state.releases._providers), {"clashverge", "telegram"})
            response = client.post("/api/v1/refresh")
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload["started"])
            self.assertEqual(payload["total"], 2)
            status = client.get("/api/v1/status").json()
        self.assertEqual(set(status["warmup"]["apps"]), {"clashverge", "telegram"})
        self.assertEqual(status["warmup"]["apps"]["clashverge"]["status"], "ok")
        self.assertEqual(clash.calls, 1)
        self.assertEqual(telegram.calls, 1)
        self.assertEqual(hidden.calls, 0)

    def test_empty_app_ids_means_all_apps(self):
        providers = {app_id: FakeProvider(app_id) for app_id in EMPTY_PROVIDER_IDS}
        api = create_application(
            release_providers=providers,
            settings=builtin_settings(app_ids=()),
            extra_directory=FIXTURE_EXTRA_DIRECTORY,
        )
        with TestClient(api) as client:
            self.assertEqual(client.get("/api/v1/apps").json()["total"], 8)
            builtin = {item["id"]: item for item in client.get("/api/v1/sources").json()["items"]}["builtin"]
            self.assertEqual(builtin["app_count"], 8)


class BuiltinDisabledTests(unittest.TestCase):
    def make_app(self):
        self.clash = FakeProvider("clashverge")
        self.telegram = FakeProvider("telegram", ("x64", "x86", "arm64"))
        return build_app(
            settings=builtin_settings(enabled=False),
            release_providers={"clashverge": self.clash, "telegram": self.telegram},
        )

    def test_apps_routes_return_503(self):
        with TestClient(self.make_app()) as client:
            for path in (
                "/api/v1/apps",
                "/api/v1/apps/clashverge",
                "/api/v1/apps/clashverge/releases",
                "/api/v1/apps/clashverge/releases/latest",
                "/api/v1/apps/clashverge/releases/1.2.3-windows-x64",
                "/api/v1/apps/clashverge/icon",
            ):
                with self.subTest(path=path):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(response.json()["detail"], DISABLED_DETAIL)
            refresh = client.post("/api/v1/refresh")
            self.assertEqual(refresh.status_code, 503)
            self.assertEqual(refresh.json()["detail"], DISABLED_DETAIL)

    def test_no_provider_is_registered_or_called(self):
        api = self.make_app()
        with TestClient(api) as client:
            self.assertEqual(api.state.releases._providers, {})
            client.get("/api/v1/apps")
            client.post("/api/v1/refresh")
            self.assertEqual(api.state.warmup.report()["apps"], {})
        self.assertEqual(self.clash.calls, 0)
        self.assertEqual(self.telegram.calls, 0)

    def test_diagnostics_and_cache_endpoints_do_not_crash(self):
        with TestClient(self.make_app()) as client:
            self.assertEqual(client.get("/api/v1/health").status_code, 200)
            status = client.get("/api/v1/status")
            self.assertEqual(status.status_code, 200)
            self.assertEqual(status.json()["warmup"]["apps"], {})
            self.assertEqual(
                status.json()["warmup"]["counts"], {"ok": 0, "cached": 0, "unavailable": 0}
            )
            self.assertFalse(status.json()["refresh"]["enabled"])
            cache = client.get("/api/v1/cache")
            self.assertEqual(cache.status_code, 200)
            self.assertEqual(cache.json()["entries"], 0)
            self.assertFalse(cache.json()["refresh"]["enabled"])
            self.assertFalse(cache.json()["settings"]["builtin"]["enabled"])
            cleared = client.delete("/api/v1/cache")
            self.assertEqual(cleared.status_code, 200)
            self.assertIn("cleared", cleared.json())

    def test_sources_still_reports_the_disabled_source(self):
        with TestClient(self.make_app()) as client:
            builtin = {item["id"]: item for item in client.get("/api/v1/sources").json()["items"]}["builtin"]
        self.assertFalse(builtin["enabled"])
        self.assertEqual(builtin["app_count"], 2)
        self.assertFalse(builtin["is_default"])

    def test_provider_factory_is_not_called_when_disabled(self):
        settings = builtin_settings(enabled=False)
        with mock.patch("Backend.main.create_release_providers") as factory:
            api = create_application(settings=settings)
            with TestClient(api) as client:
                self.assertEqual(client.get("/api/v1/health").status_code, 200)
                self.assertEqual(client.get("/api/v1/apps").status_code, 503)
                self.assertEqual(api.state.releases._providers, {})
                factory.assert_not_called()


class BuiltinStartupValidationTests(unittest.TestCase):
    def test_unknown_app_id_fails_startup(self):
        settings = builtin_settings(app_ids=("clashverge", "missing-app"))
        with self.assertRaises(ValueError):
            with TestClient(build_app(settings=settings)):
                pass

    def test_unknown_app_id_fails_startup_even_when_the_source_is_disabled(self):
        settings = builtin_settings(enabled=False, app_ids=("clashverge", "missing-app"))
        with self.assertRaises(ValueError):
            with TestClient(build_app(settings=settings)):
                pass


class CatalogRepositoryWhitelistTests(unittest.TestCase):
    def repository(self, allowed_app_ids):
        return CatalogRepository(DATA_FILE, ICON_DIR, allowed_app_ids=allowed_app_ids)

    def test_allow_list_filters_queries_but_keeps_the_full_catalog(self):
        repository = self.repository(["telegram"])
        self.assertIsNotNone(repository.get_app("telegram"))
        self.assertIsNone(repository.get_app("clashverge"))
        self.assertEqual([app.id for app in repository.list_apps(None, None, "name")], ["telegram"])
        self.assertEqual(repository.app_count, 1)
        self.assertEqual(repository.all_app_count, 45)
        self.assertEqual(repository.allowed_app_ids, frozenset({"telegram"}))
        self.assertEqual(len(repository.all_apps), 45)
        # 图标校验仍针对完整目录进行：被过滤掉的软件图标路径仍可解析。
        hidden = next(app for app in repository.all_apps if app.id == "qq")
        self.assertIsNotNone(repository.icon_path(hidden))

    def test_no_allow_list_exposes_every_app(self):
        repository = self.repository(None)
        self.assertEqual(repository.app_count, 45)
        self.assertEqual(repository.all_app_count, 45)
        self.assertIsNone(repository.allowed_app_ids)
        self.assertIsNotNone(repository.get_app("qq"))

    def test_empty_allow_list_exposes_nothing(self):
        repository = self.repository(())
        self.assertEqual(repository.app_count, 0)
        self.assertEqual(repository.list_apps(None, None, "recommended"), [])
        self.assertEqual(repository.all_app_count, 45)

    def test_unknown_allow_list_ids_are_rejected(self):
        with self.assertRaises(ValueError):
            self.repository(["telegram", "missing-app"])
        with self.assertRaises(ValueError):
            self.repository([""])


class BuiltinConfigTests(unittest.TestCase):
    def test_defaults(self):
        config = BuiltinConfig()
        self.assertTrue(config.enabled)
        self.assertEqual(config.app_ids, [])
        self.assertEqual(BackendSettings().builtin.model_dump(), {"enabled": True, "app_ids": []})

    def test_committed_configuration_enables_the_two_apps(self):
        committed = json.loads(DEFAULT_CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertEqual(committed["builtin"], {"enabled": True, "app_ids": ["clashverge", "telegram"]})
        loaded = BackendSettings.load(environ={})
        self.assertTrue(loaded.builtin.enabled)
        self.assertEqual(loaded.builtin.app_ids, ["clashverge", "telegram"])

    def test_file_and_environment_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(directory, {"builtin": {"enabled": False, "app_ids": ["telegram"]}})
            settings = BackendSettings.load(path, environ={})
            self.assertFalse(settings.builtin.enabled)
            self.assertEqual(settings.builtin.app_ids, ["telegram"])

            settings = BackendSettings.load(
                path,
                environ={
                    "WINAPPSMANAGER_BUILTIN_ENABLED": "yes",
                    "WINAPPSMANAGER_BUILTIN_APP_IDS": " clashverge , telegram ",
                },
            )
            self.assertTrue(settings.builtin.enabled)
            self.assertEqual(settings.builtin.app_ids, ["clashverge", "telegram"])

    def test_environment_empty_app_ids_means_no_whitelist(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(directory, {"builtin": {"app_ids": ["telegram"]}})
            settings = BackendSettings.load(path, environ={"WINAPPSMANAGER_BUILTIN_APP_IDS": ""})
        self.assertEqual(settings.builtin.app_ids, [])

        # 变量未设置时保留配置文件里的白名单。
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(directory, {"builtin": {"app_ids": ["telegram"]}})
            settings = BackendSettings.load(path, environ={})
        self.assertEqual(settings.builtin.app_ids, ["telegram"])

    def test_invalid_file_values_are_rejected(self):
        invalid_payloads = (
            {"builtin": {"enabled": "yes"}},
            {"builtin": {"app_ids": ["ClashVerge"]}},
            {"builtin": {"app_ids": ["bad id"]}},
            {"builtin": {"app_ids": ["-starts-with-dash"]}},
            {"builtin": {"app_ids": ["telegram", "telegram"]}},
            {"builtin": {"app_ids": [f"a{index}" for index in range(201)]}},
            {"builtin": {"unknown": True}},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as directory:
                path = write_config(directory, payload)
                with self.assertRaises(ValueError):
                    BackendSettings.load(path, environ={})

    def test_invalid_environment_values_are_rejected(self):
        for environ in (
            {"WINAPPSMANAGER_BUILTIN_ENABLED": "maybe"},
            {"WINAPPSMANAGER_BUILTIN_APP_IDS": "Telegram"},
            {"WINAPPSMANAGER_BUILTIN_APP_IDS": "telegram,telegram"},
        ):
            with self.subTest(environ=environ), tempfile.TemporaryDirectory() as directory:
                path = write_config(directory, {"builtin": {}})
                with self.assertRaises(ValueError):
                    BackendSettings.load(path, environ=environ)

    def test_strict_validation_applies_to_direct_construction_and_assignment(self):
        with self.assertRaises(ValueError):
            BuiltinConfig(enabled="yes")
        with self.assertRaises(ValueError):
            BuiltinConfig(app_ids=["telegram", "telegram"])
        settings = BackendSettings()
        with self.assertRaises(ValueError):
            settings.builtin.app_ids = ["Bad"]
        with self.assertRaises(ValueError):
            settings.builtin.enabled = 1


if __name__ == "__main__":
    unittest.main()
