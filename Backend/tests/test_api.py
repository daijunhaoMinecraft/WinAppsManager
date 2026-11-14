import copy
import json
import asyncio
from pathlib import Path
import sys
import tempfile
import unittest

from fastapi.testclient import TestClient
from pydantic import ValidationError


BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR.parent))

# API tests assert the curated ``data/apps.json`` catalog exactly. Point the loader at a
# non-existent ``apps.d`` directory so the extra software stored there stays out of these
# assertions; the merge behaviour itself is covered by test_catalog_merge.py.
FIXTURE_EXTRA_DIRECTORY = BACKEND_DIR / "tests" / "no-extra-apps.d"

from Backend.config import BackendSettings, CacheConfig, GithubConfig, RefreshConfig
from Backend.main import DATA_FILE, ICON_DIR, create_app as create_application
from Backend.models import Release
from Backend.providers.base import ProviderError, ProviderProtocolError, ProviderReleaseChangedError, ProviderTimeoutError


QQ_DOWNLOAD_URL = (
    "https://qqdl.gtimg.cn/qqfile/QQNTV2/9.9.37/release/"
    "a2ca1b61/QQ_9.9.37_261103_x64_01.exe"
)
QQ_SIGNED_URL = QQ_DOWNLOAD_URL + "?sign=api-test-signature&t=123"
APP_FIELDS = {
    "id", "name", "description", "long_description", "category", "publisher",
    "version", "size_display", "license", "platforms", "website_url",
    "download_page_url", "icon_url", "icon_text", "icon_background",
    "installation_notes", "features", "tags", "is_recommended", "sort_order",
    "minimum_windows", "architectures", "latest_release",
}
RELEASE_FIELDS = {
    "id", "version", "platform", "architecture", "download_url", "file_name",
    "size_bytes", "sha256",
}


def release_fixture(architecture="x64", version="9.9.37"):
    return {
        "id": f"{version}-windows-{architecture}",
        "version": version,
        "platform": "windows",
        "architecture": architecture,
        "download_url": QQ_DOWNLOAD_URL.replace("9.9.37", version).replace("_x64_", f"_{architecture}_"),
        "file_name": f"QQ_{version}_261103_{architecture}_01.exe",
        "size_bytes": None,
        "sha256": None,
    }


class FakeQQProvider:
    """Deterministic provider fixture: API tests never contact Tencent."""

    id = "qq"

    def __init__(self):
        self.version = "9.9.37"
        self.architectures = ["x64", "x86", "arm64"]
        self.force_refreshes = 0
        self.sign_calls = 0
        self.unique_signatures = False
        self.failure = None

    async def get_releases(self, *, force_refresh=False):
        if self.failure is not None:
            raise self.failure
        self.force_refreshes += int(force_refresh)
        return [Release.model_validate(release_fixture(architecture, self.version)) for architecture in self.architectures]

    async def resolve_download(self, release):
        self.sign_calls += 1
        signature = f"signature-{self.sign_calls}" if self.unique_signatures else "api-test-signature"
        return Release.model_validate({
            **release.model_dump(),
            "download_url": f"{release.download_url}?sign={signature}&t=123",
        })


class EmptyProvider:
    async def get_releases(self, *, force_refresh=False):
        return []

    async def resolve_download(self, release):
        raise AssertionError("An empty provider cannot resolve downloads")


def create_app(*args, release_providers=None, settings=None, **kwargs):
    # Legacy API assertions deliberately model the other applications as having no package.
    # Supply all registered IDs so startup validation still runs against the current catalog.
    providers = {provider_id: EmptyProvider() for provider_id in
                 ("wechat", "vscode", "7zip", "vlc", "firefox", "obs", "notepadplusplus")}
    providers["qq"] = FakeQQProvider()
    providers.update(release_providers or {})
    if settings is None:
        # Tests must not read or write the developer's persistent cache between runs.
        # Startup warmup and the scheduled refresh are covered by test_startup_refresh.py;
        # keeping them off here keeps every provider counter deterministic.
        settings = BackendSettings(
            cache=CacheConfig(persist=False),
            refresh=RefreshConfig(on_startup=False, interval_seconds=0),
        )
    return create_application(
        *args,
        release_providers=providers,
        settings=settings,
        extra_directory=FIXTURE_EXTRA_DIRECTORY,
        **kwargs,
    )


class CatalogApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.provider = FakeQQProvider()
        cls.client = TestClient(create_app(release_providers={"qq": cls.provider}), base_url="http://127.0.0.1:8080")
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def test_health(self):
        response = self.client.get("/api/v1/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_catalog_shape_and_default_order(self):
        response = self.client.get("/api/v1/apps")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(set(payload), {"items", "total"})
        self.assertEqual(payload["total"], 8)
        self.assertEqual(
            [app["id"] for app in payload["items"]],
            ["qq", "wechat", "vscode", "7zip", "vlc", "firefox", "obs", "notepadplusplus"],
        )
        for app in payload["items"]:
            with self.subTest(app_id=app["id"]):
                self.assertEqual(set(app), APP_FIELDS)
                self.assertTrue(all(isinstance(value, str) for value in app["features"]))
                self.assertTrue(all(isinstance(value, str) for value in app["tags"]))
                self.assertIsInstance(app["is_recommended"], bool)
                self.assertEqual(app["size_display"], "以官网为准")

    def test_search_by_name_publisher_tag_and_feature(self):
        cases = {
            "VISUAL": ["vscode"],
            "腾讯": ["qq", "wechat"],
            "火狐": ["firefox"],
            "混音": ["obs"],
        }
        for search, expected_ids in cases.items():
            with self.subTest(search=search):
                response = self.client.get("/api/v1/apps", params={"search": search})
                self.assertEqual(response.status_code, 200)
                self.assertEqual([app["id"] for app in response.json()["items"]], expected_ids)
                self.assertEqual(response.json()["total"], len(expected_ids))

    def test_category_filter_and_combined_search(self):
        response = self.client.get("/api/v1/apps", params={"category": " 社交通讯 "})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([app["id"] for app in response.json()["items"]], ["qq", "wechat"])
        response = self.client.get("/api/v1/apps", params={"category": "社交通讯", "search": "WeChat"})
        self.assertEqual(response.json()["total"], 1)
        self.assertEqual(response.json()["items"][0]["id"], "wechat")

    def test_empty_filter_results_are_a_valid_collection(self):
        for params in ({"search": "不存在的软件"}, {"category": "不存在的分类"}, {"category": "开发工具", "search": "QQ"}):
            with self.subTest(params=params):
                response = self.client.get("/api/v1/apps", params=params)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"items": [], "total": 0})

    def test_whitespace_search_is_unfiltered(self):
        response = self.client.get("/api/v1/apps", params={"search": "   ", "category": " "})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total"], 8)

    def test_name_sort(self):
        response = self.client.get("/api/v1/apps", params={"sort": "name"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [app["id"] for app in response.json()["items"]],
            ["7zip", "firefox", "notepadplusplus", "obs", "qq", "vscode", "vlc", "wechat"],
        )

    def test_invalid_query_is_422(self):
        for params in ({"sort": "newest"}, {"search": "x" * 201}, {"category": "x" * 101}, {"architecture": "mips"}):
            with self.subTest(params=params):
                response = self.client.get("/api/v1/apps", params=params)
                self.assertEqual(response.status_code, 422)
                self.assertIn("detail", response.json())

    def test_qq_detail_matches_list_and_has_authoritative_release(self):
        response = self.client.get("/api/v1/apps/qq")
        self.assertEqual(response.status_code, 200)
        app = response.json()
        self.assertEqual(set(app), APP_FIELDS)
        list_app = self.client.get("/api/v1/apps", params={"search": "QQ"}).json()["items"][0]
        self.assertEqual(app, list_app)
        self.assertEqual(app["version"], "9.9.37")
        self.assertEqual(app["download_page_url"], "https://im.qq.com/pcqq/index.shtml")
        self.assertEqual(app["icon_url"], "http://127.0.0.1:8080/api/v1/apps/qq/icon")
        self.assertEqual(app["latest_release"]["download_url"], QQ_SIGNED_URL)
        self.assertIsNone(app["latest_release"]["size_bytes"])
        self.assertIsNone(app["latest_release"]["sha256"])

    def test_apps_without_package_still_have_fixed_icons(self):
        for app in self.client.get("/api/v1/apps").json()["items"]:
            if app["id"] != "qq":
                with self.subTest(app_id=app["id"]):
                    self.assertIsNone(app["latest_release"])
                    self.assertEqual(app["icon_url"], f'http://127.0.0.1:8080/api/v1/apps/{app["id"]}/icon')
                    self.assertEqual(app["version"], "以官网为准")
                    self.assertTrue(app["download_page_url"].startswith("https://"))

    def test_releases_collection_and_resource(self):
        response = self.client.get("/api/v1/apps/qq/releases")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(set(payload), {"items", "total"})
        self.assertEqual(payload["total"], 3)
        self.assertEqual([release["architecture"] for release in payload["items"]], ["x64", "x86", "arm64"])
        release = payload["items"][0]
        self.assertEqual(set(release), RELEASE_FIELDS)
        self.assertEqual(release, {
            "id": "9.9.37-windows-x64",
            "version": "9.9.37",
            "platform": "windows",
            "architecture": "x64",
            "download_url": QQ_SIGNED_URL,
            "file_name": "QQ_9.9.37_261103_x64_01.exe",
            "size_bytes": None,
            "sha256": None,
        })
        resource = self.client.get("/api/v1/apps/qq/releases/9.9.37-windows-x64")
        self.assertEqual(resource.status_code, 200)
        self.assertEqual(resource.json(), release)
        self.assertEqual(self.client.get("/api/v1/apps/qq").json()["latest_release"], release)

    def test_no_release_is_an_empty_collection(self):
        response = self.client.get("/api/v1/apps/wechat/releases")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"items": [], "total": 0})

    def test_missing_resources_are_404(self):
        paths = [
            "/api/v1/apps/missing",
            "/api/v1/apps/missing/releases",
            "/api/v1/apps/missing/releases/9.9.37-windows-x64",
            "/api/v1/apps/qq/releases/missing",
            "/api/v1/apps/wechat/releases/9.9.37-windows-x64",
            "/api/v1/apps/missing/icon",
        ]
        for path in paths:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 404)
                self.assertIn("detail", response.json())

    def test_qq_icon_is_the_existing_png(self):
        response = self.client.get("/api/v1/apps/qq/icon")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertEqual(response.content, (ICON_DIR / "qq.png").read_bytes())
        self.assertTrue(response.content.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_legacy_icon_url_remains_available(self):
        response = self.client.get("/icon/qq.png")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/png")

    def test_icon_url_uses_the_request_base(self):
        with TestClient(create_app(release_providers={"qq": FakeQQProvider()}), base_url="https://catalog.example:9443") as client:
            response = client.get("/api/v1/apps/qq")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["icon_url"], "https://catalog.example:9443/api/v1/apps/qq/icon")

    def test_architecture_selects_correct_app_and_release(self):
        for architecture in ("x64", "x86", "arm64"):
            with self.subTest(architecture=architecture):
                app = self.client.get("/api/v1/apps/qq", params={"architecture": architecture}).json()
                self.assertEqual(app["latest_release"]["architecture"], architecture)
                self.assertTrue(app["latest_release"]["file_name"].endswith(f"_{architecture}_01.exe"))
                catalog = self.client.get("/api/v1/apps", params={"search": "QQ", "architecture": architecture}).json()
                self.assertEqual(catalog["items"][0]["latest_release"]["architecture"], architecture)
                releases = self.client.get("/api/v1/apps/qq/releases", params={"architecture": architecture}).json()
                self.assertEqual(releases["total"], 1)
                self.assertEqual(releases["items"][0]["architecture"], architecture)

    def test_latest_alias_has_real_version_id_and_fresh_signing(self):
        provider = FakeQQProvider()
        provider.unique_signatures = True
        with TestClient(create_app(release_providers={"qq": provider})) as client:
            first = client.get("/api/v1/apps/qq/releases/latest", params={"architecture": "arm64"})
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.json()["id"], "9.9.37-windows-arm64")
            provider.version = "9.9.38"
            second = client.get("/api/v1/apps/qq/releases/latest", params={"architecture": "arm64"})
            self.assertEqual(second.status_code, 200)
            self.assertEqual(second.json()["id"], "9.9.38-windows-arm64")
            self.assertNotEqual(first.json()["download_url"], second.json()["download_url"])
            self.assertEqual(provider.force_refreshes, 2)
            self.assertEqual(provider.sign_calls, 2)
            self.assertEqual(second.headers["cache-control"], "no-store")

    def test_signed_resources_are_not_cacheable(self):
        for path in ("/api/v1/apps", "/api/v1/apps/qq", "/api/v1/apps/qq/releases",
                     "/api/v1/apps/qq/releases/latest", "/api/v1/apps/qq/releases/9.9.37-windows-x64"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["cache-control"], "no-store")

    def test_latest_architecture_validation_and_missing_architecture(self):
        self.assertEqual(self.client.get("/api/v1/apps/qq/releases/latest?architecture=mips").status_code, 422)
        self.assertEqual(self.client.get("/api/v1/apps/qq/releases?architecture=mips").status_code, 422)
        self.assertEqual(self.client.get("/api/v1/apps/wechat/releases/latest").status_code, 404)
        provider = FakeQQProvider()
        provider.architectures = ["x64"]
        with TestClient(create_app(release_providers={"qq": provider})) as client:
            self.assertEqual(client.get("/api/v1/apps/qq/releases/latest?architecture=arm64").status_code, 404)
            self.assertEqual(client.get("/api/v1/apps/qq/releases?architecture=arm64").json(), {"items": [], "total": 0})
            self.assertIsNone(client.get("/api/v1/apps/qq?architecture=arm64").json()["latest_release"])

    def test_provider_failures_are_gateway_errors(self):
        for failure, status in ((ProviderProtocolError("配置格式无效"), 502),
                                (ProviderTimeoutError("获取配置超时"), 504)):
            with self.subTest(status=status):
                provider = FakeQQProvider()
                provider.failure = failure
                with TestClient(create_app(release_providers={"qq": provider})) as client:
                    response = client.get("/api/v1/apps/qq/releases/latest")
                    self.assertEqual(response.status_code, status)
                    self.assertEqual(response.headers["cache-control"], "no-store")
                    self.assertIn("detail", response.json())
                    self.assertEqual(client.get("/api/v1/health").status_code, 200)
                    self.assertEqual(client.get("/api/v1/apps/qq/icon").status_code, 200)

    def test_overall_provider_deadline_returns_504(self):
        class SlowProvider(FakeQQProvider):
            async def get_releases(self, *, force_refresh=False):
                await asyncio.sleep(1)
                return []

        api = create_app(release_providers={"qq": SlowProvider()})
        with TestClient(api) as client:
            api.state.releases._timeout = 0.01
            self.assertEqual(client.get("/api/v1/apps/qq/releases/latest").status_code, 504)

    def test_provider_cannot_change_release_identity_while_signing(self):
        class InconsistentProvider(FakeQQProvider):
            async def resolve_download(self, release):
                return release.model_copy(update={"architecture": "arm64"})

        with TestClient(create_app(release_providers={"qq": InconsistentProvider()})) as client:
            self.assertEqual(client.get("/api/v1/apps/qq/releases/latest").status_code, 502)

    def test_metadata_change_during_resolution_refreshes_latest_and_collection(self):
        class ChangingProvider(FakeQQProvider):
            async def resolve_download(self, release):
                if release.version == "9.9.37":
                    self.version = "9.9.38"
                    raise ProviderReleaseChangedError("官方配置已更新")
                return await super().resolve_download(release)

        for path in ("/api/v1/apps/qq", "/api/v1/apps/qq/releases/latest", "/api/v1/apps/qq/releases"):
            with self.subTest(path=path):
                provider = ChangingProvider()
                with TestClient(create_app(release_providers={"qq": provider})) as client:
                    response = client.get(path)
                    self.assertEqual(response.status_code, 200)
                    payload = response.json()
                    if path.endswith("/releases"):
                        self.assertTrue(all(release["version"] == "9.9.38" for release in payload["items"]))
                    else:
                        self.assertEqual(payload["version"], "9.9.38")
                    self.assertGreaterEqual(provider.force_refreshes, 1)

    def test_old_resource_missing_after_metadata_refresh_returns_404(self):
        class ChangingProvider(FakeQQProvider):
            async def resolve_download(self, release):
                self.version = "9.9.38"
                raise ProviderReleaseChangedError("官方配置已更新")

        with TestClient(create_app(release_providers={"qq": ChangingProvider()})) as client:
            response = client.get("/api/v1/apps/qq/releases/9.9.37-windows-x64")
            self.assertEqual(response.status_code, 404)

    def test_version_change_retry_is_bounded_and_does_not_retry_signing_errors(self):
        class AlwaysChangingProvider(FakeQQProvider):
            attempts = 0

            async def resolve_download(self, release):
                self.attempts += 1
                raise ProviderReleaseChangedError("官方配置持续更新，请重试")

        provider = AlwaysChangingProvider()
        with TestClient(create_app(release_providers={"qq": provider})) as client:
            self.assertEqual(client.get("/api/v1/apps/qq/releases/latest").status_code, 502)
            self.assertEqual(provider.attempts, 2)

        class InvalidSigningProvider(FakeQQProvider):
            attempts = 0

            async def resolve_download(self, release):
                self.attempts += 1
                raise ProviderProtocolError("签名协议异常")

        provider = InvalidSigningProvider()
        with TestClient(create_app(release_providers={"qq": provider})) as client:
            self.assertEqual(client.get("/api/v1/apps/qq/releases/latest").status_code, 502)
            self.assertEqual(provider.attempts, 1)

    def test_static_release_resources_continue_to_work(self):
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        data["apps"][0].update(download_provider=None, version="9.9.37",
                               latest_release_id="9.9.37-windows-x64", releases=[release_fixture()])
        with tempfile.TemporaryDirectory() as directory:
            data_file = Path(directory) / "apps.json"
            data_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            with TestClient(create_app(data_file=data_file, release_providers={})) as client:
                latest = client.get("/api/v1/apps/qq/releases/latest")
                self.assertEqual(latest.status_code, 200)
                self.assertEqual(latest.json()["download_url"], QQ_DOWNLOAD_URL)
                self.assertEqual(client.get("/api/v1/apps/qq/releases").json()["total"], 1)


class CatalogStartupTests(unittest.TestCase):
    def assert_invalid_catalog(self, mutation, *, static_release=False):
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        if static_release:
            data["apps"][0].update(download_provider=None, version="9.9.37",
                                   latest_release_id="9.9.37-windows-x64", releases=[release_fixture()])
        mutation(data)
        with tempfile.TemporaryDirectory() as directory:
            data_file = Path(directory) / "apps.json"
            data_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises((ValidationError, ValueError)):
                with TestClient(create_app(data_file=data_file, release_providers={"qq": FakeQQProvider()})):
                    pass

    def test_duplicate_app_id_rejects_startup(self):
        self.assert_invalid_catalog(lambda data: data["apps"].append(copy.deepcopy(data["apps"][0])))

    def test_duplicate_release_id_rejects_startup(self):
        self.assert_invalid_catalog(lambda data: data["apps"][0]["releases"].append(copy.deepcopy(data["apps"][0]["releases"][0])), static_release=True)

    def test_missing_latest_release_rejects_startup(self):
        self.assert_invalid_catalog(lambda data: data["apps"][0].update(latest_release_id="missing"), static_release=True)

    def test_version_mismatch_rejects_startup(self):
        self.assert_invalid_catalog(lambda data: data["apps"][0].update(version="0.0.0"), static_release=True)

    def test_unsafe_resource_file_names_reject_startup(self):
        self.assert_invalid_catalog(lambda data: data["apps"][0]["releases"][0].update(file_name="../QQ.exe"), static_release=True)
        self.assert_invalid_catalog(lambda data: data["apps"][0].update(icon_file="../qq.png"))

    def test_missing_configured_icon_rejects_startup(self):
        self.assert_invalid_catalog(lambda data: data["apps"][0].update(icon_file="missing.png"))

    def test_unknown_fields_and_invalid_download_url_reject_startup(self):
        self.assert_invalid_catalog(lambda data: data["apps"][0].update(download_url="unexpected-field"))
        self.assert_invalid_catalog(lambda data: data["apps"][0]["releases"][0].update(download_url="not-a-url"), static_release=True)

    def test_unknown_provider_rejects_startup(self):
        self.assert_invalid_catalog(lambda data: data["apps"][0].update(download_provider="missing"))

    def test_provider_and_static_releases_cannot_be_mixed(self):
        self.assert_invalid_catalog(lambda data: data["apps"][0].update(releases=[release_fixture()]))

    def test_malformed_json_rejects_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            data_file = Path(directory) / "apps.json"
            data_file.write_text("{", encoding="utf-8")
            with self.assertRaises(ValidationError):
                with TestClient(create_app(data_file=data_file)):
                    pass


class SettingsRedactionTests(unittest.TestCase):
    """诊断接口可以暴露配置，但绝不能暴露 GitHub 令牌。"""

    def test_cache_endpoint_never_returns_the_github_token(self):
        secret = "ghp_" + "s" * 36
        settings = BackendSettings(
            cache=CacheConfig(persist=False),
            refresh=RefreshConfig(on_startup=False, interval_seconds=0),
            github=GithubConfig(token=secret),
        )
        with TestClient(create_app(settings=settings)) as client:
            response = client.get("/api/v1/cache")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(secret, response.text)
        github = response.json()["settings"]["github"]
        self.assertEqual(github["token"], "***")
        self.assertTrue(github["token_configured"])

    def test_empty_token_is_reported_as_not_configured(self):
        settings = BackendSettings(
            cache=CacheConfig(persist=False),
            refresh=RefreshConfig(on_startup=False, interval_seconds=0),
        )
        with TestClient(create_app(settings=settings)) as client:
            github = client.get("/api/v1/cache").json()["settings"]["github"]
        self.assertEqual(github["token"], "")
        self.assertFalse(github["token_configured"])


if __name__ == "__main__":
    unittest.main()
