"""联想应用商店（``/api/v1/store/lenovo/*``）路由与 ``/sources``、缓存的 API 测试。

测试通过 ``create_app(..., lenovo_service=FakeLenovoService())`` 注入假服务，假服务返回真实的
pydantic 模型并可按需抛联想异常；同时注入空的 release provider 并关闭启动预热/定时刷新，
因此这些用例不会访问真实网络、不读写开发机的持久缓存。
"""

import json
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest

from fastapi.testclient import TestClient


BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR.parent))

# 与 test_api.py 相同：指向不存在的 apps.d，保持内置精选源断言稳定（8 款）。
FIXTURE_EXTRA_DIRECTORY = BACKEND_DIR / "tests" / "no-extra-apps.d"

from Backend.config import BackendSettings, CacheConfig, LenovoConfig, RefreshConfig
from Backend.lenovo import (
    INSTALLATION_NOTES,
    ImagePayload,
    LenovoNotFoundError,
    LenovoProtocolError,
    LenovoTimeoutError,
    LenovoUnavailableError,
    StoreAppDetails,
    StoreAppSummary,
    StoreCatalog,
    StoreCategoriesResponse,
    StoreCategory,
    StoreCategoryGroup,
    StoreDetailTag,
    StoreScreenshot,
    StoreSearchResponse,
)
from Backend.main import create_app as create_application
from Backend.models import Release


UPDATED_AT = datetime(2026, 11, 14, 9, 0, 0, tzinfo=timezone.utc)
SOFT_ID = "L105867"
DOWNLOAD_URL = (
    "https://pc-lsw7.lenovomm.cn/dlserver/QQ9.9.37.54395_lenovo_x64.exe?sign=api-test&t=1"
)
ICON_BYTES = b"\x89PNG\r\n\x1a\n" + b"lenovo-icon-bytes"
ICON_ETAG = "0123456789abcdef0123456789abcdef"
PROVIDER_IDS = ("wechat", "vscode", "7zip", "vlc", "firefox", "obs", "notepadplusplus", "qq")
DISABLED_DETAIL = "联想应用商店源未启用。"


def make_summary(soft_id=SOFT_ID, name="QQ", **overrides):
    data = {
        "id": f"lenovo-{soft_id.lower()}",
        "soft_id": soft_id,
        "name": name,
        "description": "QQ9全新上线，一起轻松做自己",
        "category": "聊天社交",
        "category_id": "1",
        "group": "software",
        "version": "9.9.37.54395",
        "size_display": "318.95 MB",
        "size_bytes": 334442672,
        "publisher": None,
        "platforms": "Windows 10 / 11 · 64 位",
        "architectures": ["x64"],
        "minimum_windows": "10",
        "icon_url": f"https://lsw-fast.lenovo.com.cn/logo/{soft_id}.png",
        "icon_proxy_url": f"/api/v1/store/lenovo/apps/{soft_id}/icon",
        "icon_text": "QQ",
        "icon_background": "#1679C8",
        "tags": ["聊天社交"],
        "score": 60,
        "download_count": "1.01亿次",
        "license": "免费",
        "is_free": True,
        "is_recommended": True,
        "is_beta": False,
        "sort_order": 0,
        "installation_notes": INSTALLATION_NOTES,
    }
    data.update(overrides)
    return StoreAppSummary.model_validate(data)


def make_catalog(items=None):
    items = list(items or [make_summary()])
    return StoreCatalog(updated_at=UPDATED_AT, stale=False, total=len(items), items=items)


def make_categories():
    return StoreCategoriesResponse(
        updated_at=UPDATED_AT,
        groups=[
            StoreCategoryGroup(
                code="software",
                name="软件",
                categories=[StoreCategory(id="1", name="聊天社交", count=228)],
            ),
            StoreCategoryGroup(
                code="game",
                name="游戏",
                categories=[StoreCategory(id="19", name="角色扮演", count=205)],
            ),
        ],
    )


def make_details(summary=None):
    summary = summary or make_summary()
    return StoreAppDetails(
        **summary.model_dump(),
        long_description="QQ 的详细描述。",
        release_notes="修复已知问题并提升稳定性。",
        screenshots=[
            StoreScreenshot(index=0, kind="image", url=f"/api/v1/store/lenovo/apps/{SOFT_ID}/screenshots/0"),
            StoreScreenshot(index=1, kind="video", url=f"/api/v1/store/lenovo/apps/{SOFT_ID}/screenshots/1"),
        ],
        cover_url=f"/api/v1/store/lenovo/apps/{SOFT_ID}/cover",
        labels=["聊天社交", "办公辅助工具"],
        ranking="聊天社交类下载榜第2名",
        detail_tags=[StoreDetailTag(name="安全认证", tips="软件通过多款主流安全软件检测。")],
    )


def make_release(architecture="x64"):
    return Release.model_validate({
        "id": f"lenovo-{SOFT_ID.lower()}-9.9.37.54395",
        "version": "9.9.37.54395",
        "platform": "windows",
        "architecture": architecture,
        "download_url": DOWNLOAD_URL,
        "file_name": "QQ9.9.37.54395_lenovo_x64.exe",
        "size_bytes": 334442672,
        "sha256": "7019f1a55557b4260cb7385f015a521378748d06dfd9915905e14f0585f4e855",
    })


class FakeLenovoService:
    """确定性的联想服务替身：返回真实模型，异常可注入，调用次数可断言。"""

    def __init__(self):
        self.summary = make_summary()
        self.catalog = make_catalog([self.summary])
        self.categories = make_categories()
        self.details = make_details(self.summary)
        self.search_response = StoreSearchResponse(query="QQ", total=1, items=[self.summary])
        self.refresh_result = {
            "refreshed": True,
            "total": 1,
            "updated_at": "2026-11-14T09:00:00Z",
            "duration_seconds": 1.5,
        }
        self.clear_result = {
            "catalog": {"cleared": True, "apps": 1, "disk_cleared": True},
            "details": {"cleared": 1},
            "media": {"cleared": 2},
            "search": {"cleared": 0},
        }
        self.cached_count = 1
        self.cached_updated = UPDATED_AT
        self.catalog_status_result = {
            "apps": 1,
            "fetched_at": "2026-11-14T09:00:00Z",
            "age_seconds": 12.5,
            "refresh_interval_seconds": 300.0,
            "refresher_running": True,
            "next_refresh_at": "2026-11-14T09:05:00Z",
            "last_refresh_at": "2026-11-14T09:00:00Z",
            "last_error": None,
            "refreshes": 3,
            "background_refreshes": 1,
            "stale_served": 0,
        }
        self.preload_calls = 0
        self.preload_failure = None
        self.refresher_calls = 0
        self.catalog_calls = []
        self.details_calls = []
        self.download_calls = []
        self.search_calls = []
        self.icon_calls = []
        self.screenshot_calls = []
        self.cover_calls = []
        self.refresh_calls = 0
        self.clear_calls = 0
        self.shutdown_calls = 0
        self.failures = {}
        self.images = {
            "icon": ImagePayload(content=ICON_BYTES, content_type="image/png", etag=ICON_ETAG),
            "screenshot": ImagePayload(
                content=b"fake-jpeg-bytes", content_type="image/jpeg", etag="screenshot-etag-0123456789"
            ),
            "cover": ImagePayload(
                content=b"fake-cover-bytes", content_type="image/png", etag="cover-etag-0123456789abcd"
            ),
        }

    def _raise(self, name):
        error = self.failures.get(name)
        if error is not None:
            raise error

    async def get_catalog(self, *, force_refresh=False):
        self.catalog_calls.append(force_refresh)
        self._raise("get_catalog")
        return self.catalog

    async def get_categories(self, *, force_refresh=False):
        self._raise("get_categories")
        return self.categories

    async def get_details(self, soft_id, *, force_refresh=False):
        self.details_calls.append((soft_id, force_refresh))
        self._raise("get_details")
        return self.details

    async def resolve_download(self, soft_id, architecture="x64"):
        self.download_calls.append((soft_id, architecture))
        self._raise("resolve_download")
        return make_release(architecture)

    async def search(self, query, limit=30):
        self.search_calls.append((query, limit))
        self._raise("search")
        return self.search_response

    async def get_icon(self, soft_id):
        self.icon_calls.append(soft_id)
        self._raise("get_icon")
        return self.images["icon"]

    async def get_screenshot(self, soft_id, index):
        self.screenshot_calls.append((soft_id, index))
        self._raise("get_screenshot")
        return self.images["screenshot"]

    async def get_cover(self, soft_id):
        self.cover_calls.append(soft_id)
        self._raise("get_cover")
        return self.images["cover"]

    async def refresh_catalog(self):
        self.refresh_calls += 1
        self._raise("refresh_catalog")
        return dict(self.refresh_result)

    async def clear_caches(self):
        self.clear_calls += 1
        self._raise("clear_caches")
        return json.loads(json.dumps(self.clear_result))

    def cached_app_count(self):
        return self.cached_count

    def cached_updated_at(self):
        return self.cached_updated

    async def preload_catalog(self):
        self.preload_calls += 1
        if self.preload_failure is not None:
            raise self.preload_failure
        return None

    def catalog_status(self):
        return json.loads(json.dumps(self.catalog_status_result))

    async def start_catalog_refresher(self):
        self.refresher_calls += 1
        return True

    async def stop_catalog_refresher(self):
        self.refresher_calls += 1

    async def shutdown(self):
        self.shutdown_calls += 1


class EmptyProvider:
    async def get_releases(self, *, force_refresh=False):
        return []

    async def resolve_download(self, release):
        raise AssertionError("测试替身不解析下载地址")


def lenovo_settings(*, enabled=True):
    # persist=False：默认配置也绝不写开发机的 Backend/.cache；测试注入假服务时同样适用。
    return BackendSettings(
        cache=CacheConfig(persist=False),
        refresh=RefreshConfig(on_startup=False, interval_seconds=0),
        lenovo=LenovoConfig(enabled=enabled),
    )


def create_app(*, lenovo_service=None, settings=None, **kwargs):
    providers = {provider_id: EmptyProvider() for provider_id in PROVIDER_IDS}
    return create_application(
        release_providers=providers,
        settings=settings if settings is not None else lenovo_settings(),
        extra_directory=FIXTURE_EXTRA_DIRECTORY,
        lenovo_service=lenovo_service,
        **kwargs,
    )


def model_json(model):
    return json.loads(model.model_dump_json())


class SourcesApiTests(unittest.TestCase):
    def test_sources_reports_both_sources_without_fetching_the_catalog(self):
        service = FakeLenovoService()
        service.cached_count = 5111
        api = create_app(lenovo_service=service)
        with TestClient(api) as client:
            response = client.get("/api/v1/sources")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            payload = response.json()
            self.assertEqual(payload["total"], 3)
            lenovo, builtin, soft360 = payload["items"]
            self.assertEqual(soft360["id"], "soft360")
            self.assertFalse(soft360["is_default"])
            self.assertEqual(lenovo["id"], "lenovo")
            self.assertEqual(lenovo["name"], "联想应用商店")
            self.assertEqual(lenovo["description"], "联想官方 PC 应用商店目录，含软件与游戏。")
            self.assertTrue(lenovo["enabled"])
            self.assertEqual(lenovo["app_count"], 5111)
            self.assertTrue(lenovo["is_default"])
            self.assertTrue(lenovo["updated_at"].startswith("2026-11-14T09:00:00"))
            self.assertEqual(builtin, {
                "id": "builtin",
                "name": "内置精选",
                "description": "项目内置的精选软件目录。",
                "enabled": True,
                "app_count": 8,
                "is_default": False,
                "updated_at": None,
            })
            self.assertEqual(service.catalog_calls, [])

    def test_sources_reports_null_count_when_the_catalog_is_not_loaded(self):
        service = FakeLenovoService()
        service.cached_count = None
        service.cached_updated = None
        with TestClient(create_app(lenovo_service=service)) as client:
            lenovo = client.get("/api/v1/sources").json()["items"][0]
        self.assertIsNone(lenovo["app_count"])
        self.assertIsNone(lenovo["updated_at"])
        self.assertTrue(lenovo["enabled"])

    def test_disabled_source_is_reported_but_not_created(self):
        api = create_app(settings=lenovo_settings(enabled=False))
        with TestClient(api) as client:
            response = client.get("/api/v1/sources")
            self.assertEqual(response.status_code, 200)
            lenovo, builtin, _soft360 = response.json()["items"]
            self.assertFalse(lenovo["enabled"])
            self.assertIsNone(lenovo["app_count"])
            self.assertIsNone(lenovo["updated_at"])
            self.assertTrue(lenovo["is_default"])
            self.assertEqual(builtin["app_count"], 8)
            self.assertIsNone(api.state.lenovo)

    def test_disabled_source_ignores_an_injected_service(self):
        service = FakeLenovoService()
        api = create_app(lenovo_service=service, settings=lenovo_settings(enabled=False))
        with TestClient(api) as client:
            self.assertIsNone(api.state.lenovo)
            self.assertEqual(client.get("/api/v1/sources").json()["items"][0]["enabled"], False)
            self.assertEqual(client.get("/api/v1/store/lenovo/apps").status_code, 503)
        self.assertEqual(service.shutdown_calls, 0)


class CatalogApiTests(unittest.TestCase):
    def test_categories(self):
        service = FakeLenovoService()
        with TestClient(create_app(lenovo_service=service)) as client:
            response = client.get("/api/v1/store/lenovo/categories")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json(), model_json(service.categories))

    def test_catalog(self):
        service = FakeLenovoService()
        with TestClient(create_app(lenovo_service=service)) as client:
            response = client.get("/api/v1/store/lenovo/apps")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json(), model_json(service.catalog))
        item = response.json()["items"][0]
        # 客户端列表直连联想 CDN：icon_url 是绝对地址，icon_proxy_url 只是兜底。
        self.assertEqual(item["icon_url"], "https://lsw-fast.lenovo.com.cn/logo/L105867.png")
        self.assertEqual(item["icon_proxy_url"], "/api/v1/store/lenovo/apps/L105867/icon")
        self.assertEqual(service.catalog_calls, [False])

    def test_get_ignores_the_read_only_refresh_query_and_only_reads_the_cache(self):
        service = FakeLenovoService()
        with TestClient(create_app(lenovo_service=service)) as client:
            plain = client.get("/api/v1/store/lenovo/apps")
            refreshed = client.get("/api/v1/store/lenovo/apps", params={"refresh": "true"})
        self.assertEqual(plain.status_code, 200)
        self.assertEqual(refreshed.status_code, 200)
        self.assertEqual(refreshed.headers["cache-control"], "no-store")
        # 旧客户端可能仍带 ?refresh=true：FastAPI 忽略未知查询参数，路由永远只读缓存。
        self.assertEqual(refreshed.json(), plain.json())
        self.assertEqual(refreshed.json(), model_json(service.catalog))
        self.assertEqual(service.catalog_calls, [False, False])

    def test_get_catalog_failure_is_a_gateway_error(self):
        service = FakeLenovoService()
        service.failures["get_catalog"] = LenovoUnavailableError("联想目录暂时不可用")
        with TestClient(create_app(lenovo_service=service)) as client:
            response = client.get("/api/v1/store/lenovo/apps", params={"refresh": "true"})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("联想目录暂时不可用", response.json()["detail"])
        # 失败只在普通读取路径上体现，没有第二个「强制刷新」调用。
        self.assertEqual(service.catalog_calls, [False])
        self.assertEqual(service.refresh_calls, 0)

    def test_refresh_endpoint_is_the_only_manual_refresh_entry(self):
        service = FakeLenovoService()
        with TestClient(create_app(lenovo_service=service)) as client:
            # GET 带 refresh 参数先证明不会触发刷新，再 POST 才真正同步刷新。
            client.get("/api/v1/store/lenovo/apps", params={"refresh": "true"})
            response = client.post("/api/v1/store/lenovo/refresh")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json(), service.refresh_result)
        self.assertTrue(response.json()["refreshed"])
        self.assertEqual(service.catalog_calls, [False])
        self.assertEqual(service.refresh_calls, 1)

    def test_error_mapping_is_502_and_504(self):
        for failure, status in (
            (LenovoProtocolError("联想响应结构无效"), 502),
            (LenovoTimeoutError("联想请求超时"), 504),
        ):
            with self.subTest(status=status):
                service = FakeLenovoService()
                service.failures["get_categories"] = failure
                with TestClient(create_app(lenovo_service=service)) as client:
                    response = client.get("/api/v1/store/lenovo/categories")
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(response.json()["detail"], str(failure))


class DetailsApiTests(unittest.TestCase):
    def test_details(self):
        service = FakeLenovoService()
        with TestClient(create_app(lenovo_service=service)) as client:
            response = client.get(f"/api/v1/store/lenovo/apps/{SOFT_ID}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json(), model_json(service.details))
        payload = response.json()
        self.assertEqual(payload["icon_url"], "https://lsw-fast.lenovo.com.cn/logo/L105867.png")
        self.assertEqual(payload["icon_proxy_url"], "/api/v1/store/lenovo/apps/L105867/icon")
        self.assertEqual(service.details_calls, [(SOFT_ID, False)])

    def test_details_ignores_the_refresh_query_and_preserves_soft_id_case(self):
        service = FakeLenovoService()
        with TestClient(create_app(lenovo_service=service)) as client:
            response = client.get(f"/api/v1/store/lenovo/apps/{SOFT_ID}", params={"refresh": "true"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), model_json(service.details))
        # GET 详情永远不会强制刷新（旧客户端带来的 refresh=true 被当作未知查询参数忽略）。
        self.assertEqual(service.details_calls, [(SOFT_ID, False)])

    def test_missing_app_is_404(self):
        service = FakeLenovoService()
        service.failures["get_details"] = LenovoNotFoundError("联想应用商店中找不到该应用。")
        with TestClient(create_app(lenovo_service=service)) as client:
            response = client.get("/api/v1/store/lenovo/apps/L999999")
        self.assertEqual(response.status_code, 404)
        self.assertIn("找不到", response.json()["detail"])

    def test_invalid_soft_id_is_422(self):
        service = FakeLenovoService()
        with TestClient(create_app(lenovo_service=service)) as client:
            for soft_id in ("-abc", "bad id", "x" * 41, "L105867!"):
                with self.subTest(soft_id=soft_id):
                    response = client.get(f"/api/v1/store/lenovo/apps/{soft_id}")
                    self.assertEqual(response.status_code, 422)
        self.assertEqual(service.details_calls, [])


class DownloadApiTests(unittest.TestCase):
    def test_download_returns_a_release_without_caching(self):
        service = FakeLenovoService()
        with TestClient(create_app(lenovo_service=service)) as client:
            response = client.get(f"/api/v1/store/lenovo/apps/{SOFT_ID}/download")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json(), model_json(make_release("x64")))
        self.assertEqual(service.download_calls, [(SOFT_ID, "x64")])

    def test_download_architecture(self):
        service = FakeLenovoService()
        with TestClient(create_app(lenovo_service=service)) as client:
            response = client.get(
                f"/api/v1/store/lenovo/apps/{SOFT_ID}/download", params={"architecture": "x86"}
            )
            invalid = client.get(
                f"/api/v1/store/lenovo/apps/{SOFT_ID}/download", params={"architecture": "mips"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["architecture"], "x86")
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(service.download_calls, [(SOFT_ID, "x86")])

    def test_download_is_not_affected_by_the_read_only_refresh_query(self):
        service = FakeLenovoService()
        with TestClient(create_app(lenovo_service=service)) as client:
            plain = client.get(f"/api/v1/store/lenovo/apps/{SOFT_ID}/download")
            with_query = client.get(
                f"/api/v1/store/lenovo/apps/{SOFT_ID}/download", params={"refresh": "true"}
            )
        self.assertEqual(with_query.status_code, 200)
        self.assertEqual(with_query.json(), plain.json())
        # 下载地址本来就是实时解析，多出来的 refresh 参数没有任何刷新语义。
        self.assertEqual(service.download_calls, [(SOFT_ID, "x64"), (SOFT_ID, "x64")])

    def test_download_not_found_and_gateway_errors(self):
        service = FakeLenovoService()
        service.failures["resolve_download"] = LenovoNotFoundError("联想应用商店中找不到该应用。")
        with TestClient(create_app(lenovo_service=service)) as client:
            self.assertEqual(
                client.get(f"/api/v1/store/lenovo/apps/{SOFT_ID}/download").status_code, 404
            )
        for failure, status in (
            (LenovoUnavailableError("暂无下载地址"), 502),
            (LenovoTimeoutError("联想下载地址解析超时"), 504),
        ):
            with self.subTest(status=status):
                service = FakeLenovoService()
                service.failures["resolve_download"] = failure
                with TestClient(create_app(lenovo_service=service)) as client:
                    response = client.get(f"/api/v1/store/lenovo/apps/{SOFT_ID}/download")
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.headers["cache-control"], "no-store")


class SearchApiTests(unittest.TestCase):
    def test_search_passes_query_and_limit(self):
        service = FakeLenovoService()
        with TestClient(create_app(lenovo_service=service)) as client:
            response = client.get("/api/v1/store/lenovo/search", params={"q": "QQ", "limit": 10})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json(), model_json(service.search_response))
        self.assertEqual(service.search_calls, [("QQ", 10)])

    def test_search_default_limit(self):
        service = FakeLenovoService()
        with TestClient(create_app(lenovo_service=service)) as client:
            self.assertEqual(client.get("/api/v1/store/lenovo/search", params={"q": "微信"}).status_code, 200)
        self.assertEqual(service.search_calls, [("微信", 30)])

    def test_search_parameter_validation(self):
        service = FakeLenovoService()
        with TestClient(create_app(lenovo_service=service)) as client:
            invalid_requests = (
                {},  # 缺少 q
                {"q": ""},
                {"q": "x" * 101},
                {"q": "QQ", "limit": 0},
                {"q": "QQ", "limit": 51},
            )
            for params in invalid_requests:
                with self.subTest(params=params):
                    self.assertEqual(
                        client.get("/api/v1/store/lenovo/search", params=params).status_code, 422
                    )
            self.assertEqual(
                client.get("/api/v1/store/lenovo/search", params={"q": "QQ", "limit": 50}).status_code,
                200,
            )
        self.assertEqual(service.search_calls, [("QQ", 50)])


class ImageApiTests(unittest.TestCase):
    def test_icon_serves_bytes_with_etag_and_long_cache(self):
        service = FakeLenovoService()
        with TestClient(create_app(lenovo_service=service)) as client:
            response = client.get(f"/api/v1/store/lenovo/apps/{SOFT_ID}/icon")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertEqual(response.headers["cache-control"], "public, max-age=86400")
        self.assertEqual(response.headers["etag"], f'"{ICON_ETAG}"')
        self.assertEqual(response.content, ICON_BYTES)
        self.assertEqual(service.icon_calls, [SOFT_ID])

    def test_icon_conditional_requests_return_304(self):
        service = FakeLenovoService()
        with TestClient(create_app(lenovo_service=service)) as client:
            path = f"/api/v1/store/lenovo/apps/{SOFT_ID}/icon"
            for header in (ICON_ETAG, f'"{ICON_ETAG}"', f'W/"{ICON_ETAG}"', "*"):
                with self.subTest(header=header):
                    response = client.get(path, headers={"If-None-Match": header})
                    self.assertEqual(response.status_code, 304)
                    self.assertEqual(response.content, b"")
                    self.assertEqual(response.headers["etag"], f'"{ICON_ETAG}"')
                    self.assertEqual(response.headers["cache-control"], "public, max-age=86400")
            stale = client.get(path, headers={"If-None-Match": '"outdated-etag"'})
            self.assertEqual(stale.status_code, 200)
            self.assertEqual(stale.content, ICON_BYTES)

    def test_screenshot_and_cover(self):
        service = FakeLenovoService()
        with TestClient(create_app(lenovo_service=service)) as client:
            screenshot = client.get(f"/api/v1/store/lenovo/apps/{SOFT_ID}/screenshots/2")
            cover = client.get(f"/api/v1/store/lenovo/apps/{SOFT_ID}/cover")
        self.assertEqual(screenshot.status_code, 200)
        self.assertEqual(screenshot.headers["content-type"], "image/jpeg")
        self.assertEqual(screenshot.content, service.images["screenshot"].content)
        self.assertEqual(service.screenshot_calls, [(SOFT_ID, 2)])
        self.assertEqual(cover.status_code, 200)
        self.assertEqual(cover.headers["content-type"], "image/png")
        self.assertEqual(cover.content, service.images["cover"].content)
        self.assertEqual(service.cover_calls, [SOFT_ID])

    def test_negative_screenshot_index_is_422(self):
        service = FakeLenovoService()
        with TestClient(create_app(lenovo_service=service)) as client:
            response = client.get(f"/api/v1/store/lenovo/apps/{SOFT_ID}/screenshots/-1")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(service.screenshot_calls, [])

    def test_missing_images_are_404(self):
        for name, path in (
            ("get_icon", f"/api/v1/store/lenovo/apps/{SOFT_ID}/icon"),
            ("get_screenshot", f"/api/v1/store/lenovo/apps/{SOFT_ID}/screenshots/9"),
            ("get_cover", f"/api/v1/store/lenovo/apps/{SOFT_ID}/cover"),
        ):
            with self.subTest(name=name):
                service = FakeLenovoService()
                service.failures[name] = LenovoNotFoundError("联想应用商店中找不到对应图片。")
                with TestClient(create_app(lenovo_service=service)) as client:
                    response = client.get(path)
                self.assertEqual(response.status_code, 404)
                self.assertIn("找不到", response.json()["detail"])

    def test_image_gateway_errors(self):
        for failure, status in (
            (LenovoUnavailableError("联想图片服务暂时无法连接"), 502),
            (LenovoTimeoutError("联想图片请求超时"), 504),
        ):
            with self.subTest(status=status):
                service = FakeLenovoService()
                service.failures["get_icon"] = failure
                with TestClient(create_app(lenovo_service=service)) as client:
                    response = client.get(f"/api/v1/store/lenovo/apps/{SOFT_ID}/icon")
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.headers["cache-control"], "no-store")


class DisabledSourceTests(unittest.TestCase):
    def test_every_store_route_returns_503_when_the_source_is_disabled(self):
        api = create_app(settings=lenovo_settings(enabled=False))
        paths = [
            "/api/v1/store/lenovo/categories",
            "/api/v1/store/lenovo/apps",
            "/api/v1/store/lenovo/search?q=QQ",
            f"/api/v1/store/lenovo/apps/{SOFT_ID}",
            f"/api/v1/store/lenovo/apps/{SOFT_ID}/download",
            f"/api/v1/store/lenovo/apps/{SOFT_ID}/icon",
            f"/api/v1/store/lenovo/apps/{SOFT_ID}/screenshots/0",
            f"/api/v1/store/lenovo/apps/{SOFT_ID}/screenshots/1/video",
            f"/api/v1/store/lenovo/apps/{SOFT_ID}/cover",
        ]
        with TestClient(api) as client:
            for path in paths:
                with self.subTest(path=path):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(response.json()["detail"], DISABLED_DETAIL)
            video_head = client.head(f"/api/v1/store/lenovo/apps/{SOFT_ID}/screenshots/1/video")
            refresh = client.post("/api/v1/store/lenovo/refresh")
        self.assertEqual(video_head.status_code, 503)
        self.assertEqual(refresh.status_code, 503)
        self.assertEqual(refresh.json()["detail"], DISABLED_DETAIL)


class CacheApiTests(unittest.TestCase):
    def test_get_cache_includes_a_lenovo_summary_without_fetching(self):
        service = FakeLenovoService()
        service.cached_count = 5111
        with TestClient(create_app(lenovo_service=service)) as client:
            response = client.get("/api/v1/cache")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        lenovo = response.json()["lenovo"]
        self.assertEqual(lenovo["enabled"], True)
        self.assertEqual(lenovo["cached_app_count"], 5111)
        self.assertTrue(lenovo["cached_updated_at"].startswith("2026-11-14T09:00:00"))
        # 目录缓存与后台定时刷新状态来自 service.catalog_status()，不会触发抓取。
        self.assertEqual(lenovo["catalog"], service.catalog_status_result)
        self.assertEqual(lenovo["catalog"]["apps"], 1)
        self.assertTrue(lenovo["catalog"]["refresher_running"])
        self.assertEqual(lenovo["catalog"]["refresh_interval_seconds"], 300.0)
        self.assertEqual(service.catalog_calls, [])

    def test_delete_cache_clears_lenovo_caches_without_breaking_the_existing_result(self):
        service = FakeLenovoService()
        with TestClient(create_app(lenovo_service=service)) as client:
            response = client.delete("/api/v1/cache")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        payload = response.json()
        self.assertIsInstance(payload["cleared"], int)
        self.assertIn("persistent_cleared", payload)
        self.assertEqual(service.clear_calls, 1)
        self.assertTrue(payload["lenovo"]["cleared"])
        self.assertEqual(payload["lenovo"]["details"], {"cleared": 1})
        self.assertEqual(payload["lenovo"]["media"], {"cleared": 2})

    def test_delete_cache_survives_lenovo_cleanup_failures(self):
        service = FakeLenovoService()
        service.failures["clear_caches"] = LenovoUnavailableError("联想缓存目录不可写")
        with TestClient(create_app(lenovo_service=service)) as client:
            response = client.delete("/api/v1/cache")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIsInstance(payload["cleared"], int)
        self.assertEqual(payload["lenovo"], {"cleared": False})

    def test_delete_cache_with_a_disabled_source(self):
        with TestClient(create_app(settings=lenovo_settings(enabled=False))) as client:
            response = client.delete("/api/v1/cache")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["lenovo"])


class LifespanTests(unittest.TestCase):
    def test_injected_service_preloads_but_is_not_shutdown_or_rescheduled(self):
        service = FakeLenovoService()
        api = create_app(lenovo_service=service)
        with TestClient(api) as client:
            self.assertIs(api.state.lenovo, service)
            self.assertEqual(client.get("/api/v1/sources").status_code, 200)
            # 启动只调用（可选的）preload_catalog；注入的服务不启动定时刷新。
            self.assertEqual(service.preload_calls, 1)
            self.assertEqual(service.refresher_calls, 0)
        self.assertEqual(service.shutdown_calls, 0)

    def test_preload_failure_does_not_block_startup(self):
        service = FakeLenovoService()
        service.preload_failure = LenovoUnavailableError("磁盘快照损坏")
        api = create_app(lenovo_service=service)
        with TestClient(api) as client:
            self.assertEqual(service.preload_calls, 1)
            self.assertEqual(client.get("/api/v1/sources").status_code, 200)
            self.assertEqual(client.get("/api/v1/store/lenovo/apps").status_code, 200)

    def test_a_service_without_the_optional_lifecycle_hooks_still_starts(self):
        class BareLenovoService:
            def cached_app_count(self):
                return None

            def cached_updated_at(self):
                return None

            async def get_catalog(self, *, force_refresh=False):
                return make_catalog()

        service = BareLenovoService()
        with TestClient(create_app(lenovo_service=service)) as client:
            self.assertEqual(client.get("/api/v1/sources").status_code, 200)
            cache = client.get("/api/v1/cache").json()["lenovo"]
        self.assertIsNone(cache["catalog"])

    def test_default_service_is_created_without_fetching_the_catalog(self):
        api = create_app()
        with TestClient(api) as client:
            service = api.state.lenovo
            self.assertIsNotNone(service)
            # 启动时绝不抓取联想目录：目录还没加载，所以 app_count 是 null。
            self.assertIsNone(service.cached_app_count())
            lenovo = client.get("/api/v1/sources").json()["items"][0]
            self.assertIsNone(lenovo["app_count"])
            self.assertTrue(lenovo["enabled"])
            # 默认配置每 300 秒后台刷新一次；首次等待一个间隔，测试期间不会联网。
            status = service.catalog_status()
            self.assertTrue(status["refresher_running"])
            self.assertEqual(status["refresh_interval_seconds"], 300.0)
        # 内部创建的服务在关闭时自己 shutdown（停止定时刷新，避免任务泄漏）。
        self.assertTrue(service._closed)
        self.assertFalse(service.catalog_status()["refresher_running"])


if __name__ == "__main__":
    unittest.main()
