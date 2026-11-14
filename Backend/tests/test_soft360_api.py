"""360 软件源路由（``/api/v1/store/360/*``）与 ``/sources``、``/cache`` 的 API 测试。

测试通过 ``create_app(..., soft360_service=FakeSoft360Service())`` 注入假服务，假服务返回真实
pydantic 模型并可按需抛 360 异常；同时注入空的 release provider 并关闭启动预热/定时刷新，
因此这些用例不会访问真实 360 网络、不读写开发机的持久缓存。
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import unittest

from fastapi.testclient import TestClient


BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR.parent))

# 与 test_api.py 相同：指向不存在的 apps.d，保持内置精选源断言稳定（8 款）。
FIXTURE_EXTRA_DIRECTORY = BACKEND_DIR / "tests" / "no-extra-apps.d"
PROVIDER_IDS = ("wechat", "vscode", "7zip", "vlc", "firefox", "obs", "notepadplusplus", "qq")

from Backend.config import BackendSettings, CacheConfig, LenovoConfig, RefreshConfig, Soft360Config
from Backend.main import create_app as create_application
from Backend.models import Release
from Backend.soft360 import (
    INSTALLATION_NOTES,
    ImagePayload,
    Soft360AppDetails,
    Soft360AppSummary,
    Soft360Catalog,
    Soft360CategoriesResponse,
    Soft360Category,
    Soft360NotFoundError,
    Soft360ProtocolError,
    Soft360Screenshot,
    Soft360SearchResponse,
    Soft360TimeoutError,
    Soft360UnavailableError,
)


UPDATED_AT = datetime(2026, 11, 14, 9, 0, 0, tzinfo=timezone.utc)
SOFT_ID = "1010"
ICON_BYTES = b"\x89PNG\r\n\x1a\n" + b"360-icon-bytes"
ICON_ETAG = "0123456789abcdef0123456789abcdef"
DISABLED_DETAIL = "360 软件源未启用。"


class EmptyProvider:
    async def get_releases(self, *, force_refresh=False):
        return []

    async def resolve_download(self, release):
        raise AssertionError("An empty provider cannot resolve downloads")


def make_summary(soft_id=SOFT_ID, name="微信", **overrides):
    data = {
        "id": f"soft360-{soft_id}",
        "soft_id": soft_id,
        "name": name,
        "bname": "WeChat",
        "description": "微信简介",
        "category": "聊天工具",
        "category_id": "149",
        "size_bytes": 123456789,
        "score": 90,
        "icon_url": f"https://p0.qhimg.com/{soft_id}/72.png",
        "icon_proxy_url": f"/api/v1/store/360/apps/{soft_id}/icon",
        "architectures": ["x86", "x64"],
        "minimum_windows": "7",
        "tags": ["聊天工具"],
        "license": "免费",
        "is_free": True,
        "sort_order": 0,
        "installation_notes": INSTALLATION_NOTES,
    }
    data.update(overrides)
    return Soft360AppSummary.model_validate(data)


def make_catalog(items=None, *, stale=False):
    items = list(items) if items is not None else [make_summary()]
    return Soft360Catalog(updated_at=UPDATED_AT, stale=stale, total=len(items), items=items)


def make_categories():
    return Soft360CategoriesResponse(
        updated_at=UPDATED_AT,
        total=2,
        categories=[
            Soft360Category(id="149", name="聊天工具", count=1),
            Soft360Category(id="150", name="视频软件", count=0),
        ],
    )


def make_details(summary=None):
    summary = summary or make_summary()
    return Soft360AppDetails(
        **summary.model_dump(),
        version="3.9.12",
        publisher="腾讯",
        long_description="微信的详细描述。",
        screenshots=[
            Soft360Screenshot(index=0, url=f"/api/v1/store/360/apps/{SOFT_ID}/screenshots/0"),
        ],
    )


def make_release(architecture="x64"):
    return Release.model_validate({
        "id": f"soft360-{SOFT_ID}-3.9.12",
        "version": "3.9.12",
        "platform": "windows",
        "architecture": architecture,
        "download_url": "https://dl.360.cn/wechat_3.9.12.exe",
        "file_name": "wechat_3.9.12.exe",
        "size_bytes": 123456789,
        "sha256": None,
    })


class FakeSoft360Service:
    """确定性的 360 服务替身：返回真实模型，异常可注入，调用次数可断言。"""

    def __init__(self):
        self.summary = make_summary()
        self.catalog = make_catalog([self.summary])
        self.categories = make_categories()
        self.details = make_details(self.summary)
        self.release = make_release()
        self.search_response = Soft360SearchResponse(query="微信", total=1, items=[self.summary])
        self.icon = ImagePayload(content=ICON_BYTES, content_type="image/png", etag=ICON_ETAG)
        self.error: Exception | None = None
        self.catalog_calls = 0
        self.categories_calls = 0
        self.details_calls = 0
        self.download_calls = 0
        self.search_calls = []
        self.refresh_calls = 0
        self.clear_calls = 0
        self.preload_calls = 0
        self.refresher_calls = 0
        self.shutdown_calls = 0
        self.cached_count = 5111
        self.cached_updated = UPDATED_AT
        self.catalog_status_value = {
            "apps": 5111,
            "categories": 24,
            "fetched_at": "2026-11-14T09:00:00Z",
            "age_seconds": 1.0,
            "refresh_interval_seconds": 1800.0,
            "refresher_running": True,
            "next_refresh_at": "2026-11-14T09:30:00Z",
            "last_error": None,
        }

    def cached_app_count(self):
        return self.cached_count

    def cached_updated_at(self):
        return self.cached_updated

    def catalog_status(self):
        return self.catalog_status_value

    async def preload_catalog(self):
        self.preload_calls += 1

    async def start_catalog_refresher(self):
        self.refresher_calls += 1

    async def shutdown(self):
        self.shutdown_calls += 1

    async def get_catalog(self):
        self.catalog_calls += 1
        if self.error is not None:
            raise self.error
        return self.catalog

    async def get_categories(self):
        self.categories_calls += 1
        if self.error is not None:
            raise self.error
        return self.categories

    async def get_details(self, soft_id):
        self.details_calls += 1
        if self.error is not None:
            raise self.error
        return self.details

    async def resolve_download(self, soft_id, architecture="x64"):
        self.download_calls += 1
        if self.error is not None:
            raise self.error
        return make_release(architecture)

    async def search(self, query, limit=30):
        self.search_calls.append((query, limit))
        if self.error is not None:
            raise self.error
        return self.search_response

    async def get_icon(self, soft_id):
        if self.error is not None:
            raise self.error
        return self.icon

    async def get_screenshot(self, soft_id, index):
        if self.error is not None:
            raise self.error
        return self.icon

    async def refresh_catalog(self):
        self.refresh_calls += 1
        if self.error is not None:
            raise self.error
        return {
            "refreshed": True,
            "total": 5111,
            "categories": 24,
            "updated_at": "2026-11-14T10:00:00Z",
            "duration_seconds": 1.5,
        }

    async def clear_caches(self):
        self.clear_calls += 1
        if self.error is not None:
            raise self.error
        return {
            "catalog": {"cleared": True, "apps": 5111, "disk_cleared": True},
            "details": {"cleared": 0},
            "media": {"cleared": 3},
        }


def soft360_settings(*, enabled=True, interval=1800.0):
    return BackendSettings(
        cache=CacheConfig(persist=False),
        refresh=RefreshConfig(on_startup=False, interval_seconds=0),
        lenovo=LenovoConfig(enabled=False),
        soft360=Soft360Config(enabled=enabled, catalog_refresh_interval_seconds=interval),
    )


def create_app(*, soft360_service=None, settings=None):
    providers = {provider_id: EmptyProvider() for provider_id in PROVIDER_IDS}
    return create_application(
        release_providers=providers,
        settings=settings if settings is not None else soft360_settings(),
        extra_directory=FIXTURE_EXTRA_DIRECTORY,
        soft360_service=soft360_service,
    )


def model_json(model):
    return json.loads(model.model_dump_json())


class SourcesApiTests(unittest.TestCase):
    def test_sources_reports_soft360_without_fetching_the_catalog(self):
        service = FakeSoft360Service()
        with TestClient(create_app(soft360_service=service)) as client:
            response = client.get("/api/v1/sources")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            payload = response.json()
            self.assertEqual(payload["total"], 3)
            items = {item["id"]: item for item in payload["items"]}
            self.assertEqual(list(items), ["lenovo", "builtin", "soft360"])
            soft360 = items["soft360"]
            self.assertEqual(soft360["name"], "360软件管家")
            self.assertEqual(
                soft360["description"], "360 应用中心的热门软件子集（每个分类前 N 页，默认 3 页）。"
            )
            self.assertTrue(soft360["enabled"])
            self.assertFalse(soft360["is_default"])
            self.assertEqual(soft360["app_count"], 5111)
            self.assertTrue(soft360["updated_at"].startswith("2026-11-14T09:00:00"))
            self.assertFalse(items["lenovo"]["enabled"])
            self.assertEqual(service.catalog_calls, 0)

    def test_sources_reports_null_count_when_the_catalog_is_not_loaded(self):
        service = FakeSoft360Service()
        service.cached_count = None
        service.cached_updated = None
        with TestClient(create_app(soft360_service=service)) as client:
            soft360 = {item["id"]: item for item in client.get("/api/v1/sources").json()["items"]}[
                "soft360"
            ]
        self.assertIsNone(soft360["app_count"])
        self.assertIsNone(soft360["updated_at"])

    def test_disabled_source_is_reported_but_not_created(self):
        with TestClient(create_app(settings=soft360_settings(enabled=False))) as client:
            payload = client.get("/api/v1/sources").json()
            soft360 = {item["id"]: item for item in payload["items"]}["soft360"]
            self.assertFalse(soft360["enabled"])
            self.assertIsNone(soft360["app_count"])
            self.assertIsNone(soft360["updated_at"])
            self.assertEqual(client.get("/api/v1/store/360/apps").status_code, 503)


class CatalogApiTests(unittest.TestCase):
    def test_categories(self):
        service = FakeSoft360Service()
        with TestClient(create_app(soft360_service=service)) as client:
            response = client.get("/api/v1/store/360/categories")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json(), model_json(service.categories))

    def test_catalog(self):
        service = FakeSoft360Service()
        with TestClient(create_app(soft360_service=service)) as client:
            response = client.get("/api/v1/store/360/apps")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        payload = response.json()
        self.assertEqual(payload, model_json(service.catalog))
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["source"], "soft360")
        self.assertEqual(payload["items"][0]["id"], "soft360-1010")

    def test_get_catalog_ignores_query_parameters_and_never_fetches(self):
        service = FakeSoft360Service()
        with TestClient(create_app(soft360_service=service)) as client:
            response = client.get("/api/v1/store/360/apps?refresh=true&page=2")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(service.catalog_calls, 1)
        self.assertEqual(service.refresh_calls, 0)

    def test_empty_catalog_is_returned_as_an_empty_list(self):
        service = FakeSoft360Service()
        service.catalog = make_catalog([], stale=True)
        with TestClient(create_app(soft360_service=service)) as client:
            payload = client.get("/api/v1/store/360/apps").json()
        self.assertEqual(payload["total"], 0)
        self.assertEqual(payload["items"], [])
        self.assertTrue(payload["stale"])


class DetailsApiTests(unittest.TestCase):
    def test_details(self):
        service = FakeSoft360Service()
        with TestClient(create_app(soft360_service=service)) as client:
            response = client.get(f"/api/v1/store/360/apps/{SOFT_ID}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json(), model_json(service.details))

    def test_soft360_prefixed_id_is_accepted(self):
        service = FakeSoft360Service()
        with TestClient(create_app(soft360_service=service)) as client:
            response = client.get(f"/api/v1/store/360/apps/soft360-{SOFT_ID}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(service.details_calls, 1)

    def test_not_found_maps_to_404(self):
        service = FakeSoft360Service()
        service.error = Soft360NotFoundError("360 软件源目录中找不到 9999。")
        with TestClient(create_app(soft360_service=service)) as client:
            response = client.get("/api/v1/store/360/apps/9999")
        self.assertEqual(response.status_code, 404)
        self.assertIn("找不到", response.json()["detail"])

    def test_protocol_error_maps_to_502_and_timeout_to_504(self):
        cases = ((Soft360ProtocolError("bad"), 502), (Soft360TimeoutError("slow"), 504))
        for error, expected in cases:
            service = FakeSoft360Service()
            service.error = error
            with self.subTest(status=expected), TestClient(
                create_app(soft360_service=service)
            ) as client:
                response = client.get(f"/api/v1/store/360/apps/{SOFT_ID}")
            self.assertEqual(response.status_code, expected)
            self.assertEqual(response.headers["cache-control"], "no-store")


class DownloadApiTests(unittest.TestCase):
    def test_download_returns_a_release_without_cache(self):
        service = FakeSoft360Service()
        with TestClient(create_app(soft360_service=service)) as client:
            response = client.get(f"/api/v1/store/360/apps/{SOFT_ID}/download?architecture=x86")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        payload = response.json()
        self.assertEqual(payload["architecture"], "x86")
        self.assertEqual(payload["sha256"], None)
        self.assertEqual(payload["file_name"], "wechat_3.9.12.exe")
        self.assertEqual(service.download_calls, 1)

    def test_invalid_architecture_is_rejected_by_fastapi(self):
        service = FakeSoft360Service()
        with TestClient(create_app(soft360_service=service)) as client:
            response = client.get(f"/api/v1/store/360/apps/{SOFT_ID}/download?architecture=mips")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(service.download_calls, 0)


class ImageApiTests(unittest.TestCase):
    def test_icon_returns_bytes_and_supports_etag_304(self):
        service = FakeSoft360Service()
        with TestClient(create_app(soft360_service=service)) as client:
            response = client.get(f"/api/v1/store/360/apps/{SOFT_ID}/icon")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, ICON_BYTES)
            self.assertEqual(response.headers["content-type"], "image/png")
            self.assertEqual(response.headers["etag"], f'"{ICON_ETAG}"')
            self.assertEqual(response.headers["cache-control"], "public, max-age=86400")
            cached = client.get(
                f"/api/v1/store/360/apps/{SOFT_ID}/icon",
                headers={"If-None-Match": f'W/"{ICON_ETAG}"'},
            )
            self.assertEqual(cached.status_code, 304)
            self.assertEqual(cached.content, b"")

    def test_screenshot_returns_the_proxied_image(self):
        service = FakeSoft360Service()
        with TestClient(create_app(soft360_service=service)) as client:
            response = client.get(f"/api/v1/store/360/apps/{SOFT_ID}/screenshots/0")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, ICON_BYTES)
            missing = client.get(f"/api/v1/store/360/apps/{SOFT_ID}/screenshots/5")
        self.assertEqual(missing.status_code, 200)  # 假服务对任何索引都返回同一张图
        self.assertEqual(service.details_calls, 0)

    def test_icon_not_found_maps_to_404(self):
        service = FakeSoft360Service()
        service.error = Soft360NotFoundError("找不到图标。")
        with TestClient(create_app(soft360_service=service)) as client:
            response = client.get(f"/api/v1/store/360/apps/{SOFT_ID}/icon")
        self.assertEqual(response.status_code, 404)


class SearchApiTests(unittest.TestCase):
    def test_search_uses_local_matching_and_forwards_the_limit(self):
        service = FakeSoft360Service()
        with TestClient(create_app(soft360_service=service)) as client:
            response = client.get("/api/v1/store/360/search?q=微信&limit=10")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json(), model_json(service.search_response))
        self.assertEqual(service.search_calls, [("微信", 10)])

    def test_search_validates_query_and_limit(self):
        service = FakeSoft360Service()
        with TestClient(create_app(soft360_service=service)) as client:
            self.assertEqual(client.get("/api/v1/store/360/search").status_code, 422)
            self.assertEqual(client.get("/api/v1/store/360/search?q=").status_code, 422)
            self.assertEqual(
                client.get("/api/v1/store/360/search?q=a&limit=0").status_code, 422
            )
            self.assertEqual(
                client.get("/api/v1/store/360/search?q=a&limit=51").status_code, 422
            )
        self.assertEqual(service.search_calls, [])


class RefreshApiTests(unittest.TestCase):
    def test_post_refresh_returns_the_refresh_report(self):
        service = FakeSoft360Service()
        with TestClient(create_app(soft360_service=service)) as client:
            response = client.post("/api/v1/store/360/refresh")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json()["refreshed"], True)
        self.assertEqual(response.json()["total"], 5111)
        self.assertEqual(service.refresh_calls, 1)

    def test_get_refresh_is_not_allowed(self):
        service = FakeSoft360Service()
        with TestClient(create_app(soft360_service=service)) as client:
            self.assertEqual(client.get("/api/v1/store/360/refresh").status_code, 405)
        self.assertEqual(service.refresh_calls, 0)


class CacheApiTests(unittest.TestCase):
    def test_cache_reports_the_soft360_section(self):
        service = FakeSoft360Service()
        with TestClient(create_app(soft360_service=service)) as client:
            payload = client.get("/api/v1/cache").json()
        self.assertTrue(payload["soft360"]["enabled"])
        self.assertEqual(payload["soft360"]["cached_app_count"], 5111)
        self.assertTrue(payload["soft360"]["cached_updated_at"].startswith("2026-11-14T09:00:00"))
        self.assertEqual(payload["soft360"]["catalog"]["categories"], 24)
        self.assertTrue(payload["soft360"]["catalog"]["refresher_running"])

    def test_delete_cache_clears_the_soft360_caches(self):
        service = FakeSoft360Service()
        with TestClient(create_app(soft360_service=service)) as client:
            response = client.delete("/api/v1/cache")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["soft360"]["cleared"])
        self.assertEqual(response.json()["soft360"]["media"]["cleared"], 3)
        self.assertEqual(service.clear_calls, 1)

    def test_cache_section_is_empty_when_the_source_is_disabled(self):
        with TestClient(create_app(settings=soft360_settings(enabled=False))) as client:
            payload = client.get("/api/v1/cache").json()
            cleared = client.delete("/api/v1/cache").json()
        self.assertFalse(payload["soft360"]["enabled"])
        self.assertIsNone(payload["soft360"]["cached_app_count"])
        self.assertIsNone(payload["soft360"]["catalog"])
        self.assertIsNone(cleared["soft360"])


class DisabledSourceApiTests(unittest.TestCase):
    def test_every_soft360_route_returns_503_when_disabled(self):
        with TestClient(create_app(settings=soft360_settings(enabled=False))) as client:
            paths = (
                ("get", "/api/v1/store/360/categories"),
                ("get", "/api/v1/store/360/apps"),
                ("get", "/api/v1/store/360/search?q=x"),
                ("get", f"/api/v1/store/360/apps/{SOFT_ID}"),
                ("get", f"/api/v1/store/360/apps/{SOFT_ID}/download"),
                ("get", f"/api/v1/store/360/apps/{SOFT_ID}/icon"),
                ("get", f"/api/v1/store/360/apps/{SOFT_ID}/screenshots/0"),
                ("post", "/api/v1/store/360/refresh"),
            )
            for method, path in paths:
                with self.subTest(path=path):
                    response = getattr(client, method)(path)
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(response.json()["detail"], DISABLED_DETAIL)


class LifespanTests(unittest.TestCase):
    def test_injected_service_is_preloaded_but_not_started_or_shutdown(self):
        service = FakeSoft360Service()
        api = create_app(soft360_service=service)
        with TestClient(api) as client:
            self.assertIs(api.state.soft360, service)
            self.assertEqual(client.get("/api/v1/sources").status_code, 200)
            self.assertEqual(service.preload_calls, 1)
            self.assertEqual(service.refresher_calls, 0)
        self.assertEqual(service.shutdown_calls, 0)

    def test_a_service_without_the_optional_lifecycle_hooks_still_starts(self):
        class BareSoft360Service:
            def cached_app_count(self):
                return None

            def cached_updated_at(self):
                return None

            async def get_catalog(self):
                return make_catalog([])

        with TestClient(create_app(soft360_service=BareSoft360Service())) as client:
            self.assertEqual(client.get("/api/v1/sources").status_code, 200)
            self.assertEqual(client.get("/api/v1/store/360/apps").status_code, 200)
            self.assertIsNone(client.get("/api/v1/cache").json()["soft360"]["catalog"])


if __name__ == "__main__":
    unittest.main()
