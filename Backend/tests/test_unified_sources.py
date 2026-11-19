"""统一软件源 API（``/api/v1/sources`` 与 ``/api/v1/sources/{source_id}/...``）测试。

覆盖：

- ``/sources`` 列出三个来源并下发 ``capabilities``（联想/360 除手动刷新外均为 true，内置精选
  无搜索/截图/封面/视频，三个来源的 ``refresh`` 均为 false）；
- 三个来源的统一 ``categories`` / ``apps`` / ``search`` / 详情 / ``download`` / 图标 / 截图 /
  封面 / 视频接口的基本形状、``Cache-Control`` 与错误映射（404/502/504）；手动刷新接口已禁用（404）；
- 未启用/未知来源统一 404，未启用来源仍出现在 ``/sources`` 且 ``enabled=false``；
- 旧路由（``/api/v1/apps``、``/store/lenovo/*``、``/store/360/*``）行为不变；
- **新增来源只需注册适配器**：自定义 adapter 注册进 :class:`SourceRegistry` 后，统一路由与
  ``/sources`` 自动可用，不需要改路由代码；
- 统一模型的严格校验（字段禁止多余项、``total`` 必须等于条目数、``video_proxy_url`` 只属于
  视频截图）。

全部使用 ``TestClient`` + 注入的假服务/假 provider：不访问真实网络，也不读写开发机的持久缓存。
"""

from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError


BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR.parent))

# 与 test_api.py 相同：指向不存在的 apps.d，保持内置精选目录断言稳定（8 款）。
FIXTURE_EXTRA_DIRECTORY = BACKEND_DIR / "tests" / "no-extra-apps.d"

from Backend.config import BackendSettings, BuiltinConfig, CacheConfig, LenovoConfig, RefreshConfig, Soft360Config
from Backend.lenovo import (
    INSTALLATION_NOTES as LENOVO_INSTALLATION_NOTES,
    ImagePayload as LenovoImagePayload,
    LenovoNotFoundError,
    LenovoTimeoutError,
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
from Backend.soft360 import (
    INSTALLATION_NOTES as SOFT360_INSTALLATION_NOTES,
    ImagePayload as Soft360ImagePayload,
    Soft360AppDetails,
    Soft360AppSummary,
    Soft360Catalog,
    Soft360CategoriesResponse,
    Soft360Category,
    Soft360NotFoundError,
    Soft360Screenshot,
    Soft360SearchResponse,
)
from Backend.unified import (
    SourceAdapter,
    SourceAppDetails,
    SourceAppSummary,
    SourceCapabilities,
    SourceCatalog,
    SourceRegistry,
    SourceScreenshot,
    create_source_registry,
    create_unified_router,
)
from Backend.unified.routes import SOURCE_DISABLED_DETAIL, UNSUPPORTED_DETAIL


UPDATED_AT = datetime(2026, 11, 14, 9, 0, 0, tzinfo=timezone.utc)
LENOVO_SOFT_ID = "L105867"
LENOVO_DOWNLOAD_URL = "https://pc-lsw7.lenovomm.cn/dlserver/QQ_lenovo_x64.exe?sign=api-test&t=1"
LENOVO_ICON_ETAG = "0123456789abcdef0123456789abcdef"
SOFT360_SOFT_ID = "1010"
SOFT360_DOWNLOAD_URL = "https://cdn-download.soft.360.cn/wx/wechat_1010.exe"
SOFT360_ICON_ETAG = "soft3600123456789abcdef0123456789"
BUILTIN_APP_ID = "qq"
BUILTIN_PROVIDER_ID = "qq"


# ---------------------------------------------------------------------------
# 联想源假服务与模型
# ---------------------------------------------------------------------------


def make_lenovo_summary(soft_id=LENOVO_SOFT_ID, name="QQ"):
    return StoreAppSummary.model_validate({
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
        "installation_notes": LENOVO_INSTALLATION_NOTES,
    })


def make_lenovo_details(summary=None):
    summary = summary or make_lenovo_summary()
    app_path = f"/api/v1/store/lenovo/apps/{summary.soft_id}"
    return StoreAppDetails(
        **summary.model_dump(),
        long_description="QQ 的详细描述。",
        release_notes="修复已知问题并提升稳定性。",
        screenshots=[
            StoreScreenshot(
                index=0,
                kind="image",
                url="https://lsw-fast.lenovo.com.cn/img/qq-1.png",
                proxy_url=f"{app_path}/screenshots/0",
            ),
            StoreScreenshot(
                index=1,
                kind="video",
                url="https://imgs.lenovomm.cn/thumb.jpg",
                proxy_url=f"{app_path}/screenshots/1",
            ),
        ],
        cover_url="https://imgs.lenovomm.cn/cover.jpg",
        cover_proxy_url=f"{app_path}/cover",
        labels=["聊天社交", "办公辅助工具"],
        ranking="聊天社交类下载榜第2名",
        detail_tags=[StoreDetailTag(name="安全认证", tips="软件通过多款主流安全软件检测。")],
    )


def make_lenovo_release(architecture="x64"):
    return Release.model_validate({
        "id": f"lenovo-{LENOVO_SOFT_ID.lower()}-9.9.37.54395",
        "version": "9.9.37.54395",
        "platform": "windows",
        "architecture": architecture,
        "download_url": LENOVO_DOWNLOAD_URL,
        "file_name": "QQ9.9.37.54395_lenovo_x64.exe",
        "size_bytes": 334442672,
        "sha256": "7019f1a55557b4260cb7385f015a521378748d06dfd9915905e14f0585f4e855",
    })


class FakeVideoStream:
    """联想视频代理握手后的假流（结构同 ``lenovo.service.ScreenshotVideoStream``）。"""

    def __init__(self, body=b"fake-video-bytes"):
        self.status_code = 206
        self.headers = {
            "Content-Type": "video/mp4",
            "Content-Range": f"bytes 0-{len(body) - 1}/{len(body) * 2}",
            "Accept-Ranges": "bytes",
        }
        self._body = body
        self.closed = False

    async def iter_bytes(self):
        try:
            yield self._body
        finally:
            await self.aclose()

    async def aclose(self):
        self.closed = True


class FakeLenovoService:
    """确定性的联想服务替身：返回真实 pydantic 模型，异常可注入。"""

    def __init__(self):
        self.summary = make_lenovo_summary()
        self.catalog = StoreCatalog(
            updated_at=UPDATED_AT, stale=False, total=1, items=[self.summary]
        )
        self.categories = StoreCategoriesResponse(
            updated_at=UPDATED_AT,
            groups=[
                StoreCategoryGroup(
                    code="software", name="软件", categories=[StoreCategory(id="1", name="聊天社交", count=228)]
                ),
                StoreCategoryGroup(
                    code="game", name="游戏", categories=[StoreCategory(id="19", name="角色扮演", count=205)]
                ),
            ],
        )
        self.details = make_lenovo_details(self.summary)
        self.image = LenovoImagePayload(
            content=b"\x89PNG\r\n\x1a\nlenovo-image", content_type="image/png", etag=LENOVO_ICON_ETAG
        )
        self.failures = {}
        self.calls = []
        self.video_streams = []

    def _raise(self, name):
        error = self.failures.get(name)
        if error is not None:
            raise error

    def _require_known(self, soft_id):
        if soft_id != LENOVO_SOFT_ID:
            raise LenovoNotFoundError(f"联想应用商店中找不到 {soft_id}。")

    async def get_catalog(self, *, force_refresh=False):
        self.calls.append(("get_catalog", force_refresh))
        self._raise("get_catalog")
        return self.catalog

    async def get_categories(self, *, force_refresh=False):
        self.calls.append(("get_categories", force_refresh))
        self._raise("get_categories")
        return self.categories

    async def get_details(self, soft_id, *, force_refresh=False):
        self.calls.append(("get_details", soft_id))
        self._raise("get_details")
        self._require_known(soft_id)
        return self.details

    async def resolve_download(self, soft_id, architecture="x64"):
        self.calls.append(("resolve_download", soft_id, architecture))
        self._raise("resolve_download")
        self._require_known(soft_id)
        return make_lenovo_release(architecture)

    async def search(self, query, limit=30):
        self.calls.append(("search", query, limit))
        self._raise("search")
        return StoreSearchResponse(query=query, total=1, items=[self.summary])

    async def get_icon(self, soft_id):
        self.calls.append(("get_icon", soft_id))
        self._raise("get_icon")
        self._require_known(soft_id)
        return self.image

    async def get_screenshot(self, soft_id, index):
        self.calls.append(("get_screenshot", soft_id, index))
        self._raise("get_screenshot")
        self._require_known(soft_id)
        return self.image

    async def get_cover(self, soft_id):
        self.calls.append(("get_cover", soft_id))
        self._raise("get_cover")
        self._require_known(soft_id)
        return self.image

    async def open_screenshot_video(self, soft_id, index, *, range_header=None):
        self.calls.append(("open_screenshot_video", soft_id, index, range_header))
        self._raise("open_screenshot_video")
        self._require_known(soft_id)
        stream = FakeVideoStream()
        self.video_streams.append(stream)
        return stream

    async def refresh_catalog(self):
        self.calls.append(("refresh_catalog",))
        self._raise("refresh_catalog")
        return {
            "refreshed": True,
            "total": 1,
            "updated_at": "2026-11-14T09:00:00Z",
            "duration_seconds": 1.25,
        }

    def cached_app_count(self):
        return 5111

    def cached_updated_at(self):
        return UPDATED_AT


# ---------------------------------------------------------------------------
# 360 源假服务与模型
# ---------------------------------------------------------------------------


def make_soft360_summary(soft_id=SOFT360_SOFT_ID, name="微信"):
    return Soft360AppSummary.model_validate({
        "id": f"soft360-{soft_id}",
        "soft_id": soft_id,
        "name": name,
        "description": "微信，是一个生活方式",
        "category": "聊天工具",
        "category_id": "149",
        "size_bytes": 182000000,
        "score": 90,
        "icon_url": "https://p0.qhimg.com/logo/1010.png",
        "icon_proxy_url": f"/api/v1/store/360/apps/{soft_id}/icon",
        "architectures": ["x86", "x64"],
        "minimum_windows": "7",
        "tags": ["聊天工具"],
        "license": "免费",
        "is_free": True,
        "sort_order": 0,
        "installation_notes": SOFT360_INSTALLATION_NOTES,
    })


def make_soft360_details(summary=None):
    summary = summary or make_soft360_summary()
    app_path = f"/api/v1/store/360/apps/{summary.soft_id}"
    data = summary.model_dump()
    data.update(
        version="3.9.12.55",
        publisher="腾讯",
        long_description="微信 PC 版详细描述。",
        screenshots=[
            Soft360Screenshot(
                index=0,
                url="https://p15.qhmsg.com/shot-0.png",
                proxy_url=f"{app_path}/screenshots/0",
            )
        ],
    )
    return Soft360AppDetails(**data)


def make_soft360_release(architecture="x64"):
    return Release.model_validate({
        "id": "soft360-1010-3.9.12.55-windows-x64",
        "version": "3.9.12.55",
        "platform": "windows",
        "architecture": architecture,
        "download_url": SOFT360_DOWNLOAD_URL,
        "file_name": "wechat_1010.exe",
        "size_bytes": 182000000,
        "sha256": None,
    })


class FakeSoft360Service:
    """确定性的 360 服务替身。"""

    def __init__(self):
        self.summary = make_soft360_summary()
        self.catalog = Soft360Catalog(updated_at=UPDATED_AT, stale=False, total=1, items=[self.summary])
        self.categories = Soft360CategoriesResponse(
            updated_at=UPDATED_AT, total=1, categories=[Soft360Category(id="149", name="聊天工具", count=12)]
        )
        self.details = make_soft360_details(self.summary)
        self.image = Soft360ImagePayload(
            content=b"\x89PNG\r\n\x1a\nsoft360-image",
            content_type="image/png",
            etag=SOFT360_ICON_ETAG,
        )
        self.failures = {}
        self.calls = []

    def _raise(self, name):
        error = self.failures.get(name)
        if error is not None:
            raise error

    async def get_catalog(self):
        self.calls.append(("get_catalog",))
        self._raise("get_catalog")
        return self.catalog

    async def get_categories(self):
        self.calls.append(("get_categories",))
        self._raise("get_categories")
        return self.categories

    async def get_details(self, soft_id):
        self.calls.append(("get_details", soft_id))
        self._raise("get_details")
        if soft_id != SOFT360_SOFT_ID:
            raise Soft360NotFoundError(f"360 软件源中找不到 {soft_id}。")
        return self.details

    async def resolve_download(self, soft_id, architecture="x64"):
        self.calls.append(("resolve_download", soft_id, architecture))
        self._raise("resolve_download")
        if soft_id != SOFT360_SOFT_ID:
            raise Soft360NotFoundError(f"360 软件源中找不到 {soft_id}。")
        return make_soft360_release(architecture)

    async def search(self, query, limit=30):
        self.calls.append(("search", query, limit))
        self._raise("search")
        return Soft360SearchResponse(query=query, total=1, items=[self.summary])

    async def get_icon(self, soft_id):
        self.calls.append(("get_icon", soft_id))
        self._raise("get_icon")
        if soft_id != SOFT360_SOFT_ID:
            raise Soft360NotFoundError(f"360 软件源中找不到 {soft_id}。")
        return self.image

    async def get_screenshot(self, soft_id, index):
        self.calls.append(("get_screenshot", soft_id, index))
        self._raise("get_screenshot")
        if soft_id != SOFT360_SOFT_ID:
            raise Soft360NotFoundError(f"360 软件源中找不到 {soft_id}。")
        return self.image

    async def refresh_catalog(self):
        self.calls.append(("refresh_catalog",))
        self._raise("refresh_catalog")
        return {"refreshed": True, "total": 1, "updated_at": "2026-11-14T09:00:00Z", "duration_seconds": 2.5}

    def cached_app_count(self):
        return 5940

    def cached_updated_at(self):
        return UPDATED_AT


# ---------------------------------------------------------------------------
# 内置精选源假 provider
# ---------------------------------------------------------------------------


class FakeBuiltinProvider:
    """确定性 provider：返回 1.2.3 的 x64/x86 安装包，并记录强制刷新次数。"""

    def __init__(self, app_id=BUILTIN_PROVIDER_ID):
        self.id = app_id
        self.calls = 0
        self.force_refreshes = 0

    async def get_releases(self, *, force_refresh=False):
        self.calls += 1
        self.force_refreshes += int(force_refresh)
        return [self._release(architecture) for architecture in ("x64", "x86")]

    async def resolve_download(self, release):
        data = release.model_dump(mode="json")
        data["download_url"] = f"{data['download_url']}?sign=builtin-test&t=1"
        return Release.model_validate(data)

    def _release(self, architecture):
        return Release.model_validate({
            "id": f"builtin-{BUILTIN_APP_ID}-1.2.3-windows-{architecture}",
            "version": "1.2.3",
            "platform": "windows",
            "architecture": architecture,
            "download_url": f"https://downloads.example/{BUILTIN_APP_ID}-1.2.3-{architecture}.exe",
            "file_name": f"{BUILTIN_APP_ID}-1.2.3-{architecture}.exe",
            "size_bytes": 1024,
            "sha256": None,
        })


# ---------------------------------------------------------------------------
# 应用构造
# ---------------------------------------------------------------------------


def unified_settings(*, lenovo=True, soft360=True, builtin=True):
    # persist=False：不写开发机的 Backend/.cache；关闭启动预热与定时刷新保持断言确定性。
    return BackendSettings(
        cache=CacheConfig(persist=False),
        refresh=RefreshConfig(on_startup=False, interval_seconds=0),
        lenovo=LenovoConfig(enabled=lenovo),
        soft360=Soft360Config(enabled=soft360),
        builtin=BuiltinConfig(enabled=builtin, app_ids=[BUILTIN_APP_ID]),
    )


def build_app(*, settings=None, lenovo_service=None, soft360_service=None, providers=None):
    return create_application(
        release_providers=providers or {BUILTIN_PROVIDER_ID: FakeBuiltinProvider()},
        settings=settings if settings is not None else unified_settings(),
        extra_directory=FIXTURE_EXTRA_DIRECTORY,
        lenovo_service=lenovo_service if lenovo_service is not None else FakeLenovoService(),
        soft360_service=soft360_service if soft360_service is not None else FakeSoft360Service(),
    )


def _sources(client):
    response = client.get("/api/v1/sources")
    assert response.status_code == 200, response.text
    return {item["id"]: item for item in response.json()["items"]}


class SourcesListTests(unittest.TestCase):
    def test_sources_list_three_sources_with_capabilities(self):
        with TestClient(build_app()) as client:
            response = client.get("/api/v1/sources")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            payload = response.json()
            self.assertEqual(payload["total"], 3)
            # 注册顺序与旧 /sources 一致：lenovo、builtin、soft360。
            self.assertEqual([item["id"] for item in payload["items"]], ["lenovo", "builtin", "soft360"])
            by_id = {item["id"]: item for item in payload["items"]}

        lenovo = by_id["lenovo"]
        self.assertTrue(lenovo["enabled"])
        self.assertTrue(lenovo["is_default"])
        self.assertEqual(lenovo["search_mode"], "catalog")
        self.assertFalse(lenovo["online_search"])
        self.assertEqual(lenovo["app_count"], 5111)
        self.assertTrue(lenovo["updated_at"].startswith("2026-11-14T09:00:00"))
        # 联想：目录/分类/搜索/详情/下载/截图/封面/视频支持；手动刷新接口已禁用。
        self.assertEqual(lenovo["capabilities"], {
            "categories": True,
            "catalog": True,
            "search": True,
            "details": True,
            "download": True,
            "screenshots": True,
            "cover": True,
            "video": True,
            "refresh": False,
        })

        builtin = by_id["builtin"]
        self.assertEqual(builtin["name"], "内置精选")
        self.assertEqual(builtin["description"], "项目内置的精选软件目录。")
        self.assertEqual(builtin["app_count"], 1)
        self.assertEqual(builtin["search_mode"], "catalog")
        self.assertEqual(builtin["capabilities"], {
            "categories": True,
            "catalog": True,
            "search": False,
            "details": True,
            "download": True,
            "screenshots": False,
            "cover": False,
            "video": False,
            "refresh": False,
        })

        soft360 = by_id["soft360"]
        self.assertTrue(soft360["enabled"])
        self.assertFalse(soft360["is_default"])
        self.assertEqual(soft360["search_mode"], "api")
        self.assertTrue(soft360["online_search"])
        self.assertEqual(soft360["app_count"], 5940)
        # 360：截图/搜索 true，封面/视频 false；手动刷新接口已禁用。
        self.assertEqual(soft360["capabilities"], {
            "categories": True,
            "catalog": True,
            "search": True,
            "details": True,
            "download": True,
            "screenshots": True,
            "cover": False,
            "video": False,
            "refresh": False,
        })

    def test_disabled_sources_are_reported_but_unified_routes_404(self):
        settings = unified_settings(lenovo=False, soft360=False)
        with TestClient(build_app(settings=settings)) as client:
            by_id = _sources(client)
            self.assertFalse(by_id["lenovo"]["enabled"])
            self.assertIsNone(by_id["lenovo"]["app_count"])
            self.assertFalse(by_id["soft360"]["enabled"])
            self.assertIsNone(by_id["soft360"]["app_count"])
            # capabilities 仍如实描述来源支持的能力（禁用只影响 enabled）。
            self.assertTrue(by_id["lenovo"]["capabilities"]["video"])

            for path in (
                "/api/v1/sources/lenovo/apps",
                "/api/v1/sources/lenovo/categories",
                "/api/v1/sources/lenovo/apps/L105867",
                "/api/v1/sources/lenovo/apps/L105867/download",
                "/api/v1/sources/soft360/apps",
            ):
                with self.subTest(path=path):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 404)
                    self.assertEqual(response.json()["detail"], SOURCE_DISABLED_DETAIL)

    def test_unknown_source_is_404(self):
        with TestClient(build_app()) as client:
            for path in (
                "/api/v1/sources/nope/apps",
                "/api/v1/sources/nope/categories",
                "/api/v1/sources/nope/search?q=x",
            ):
                with self.subTest(path=path):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 404)
                    self.assertEqual(response.json()["detail"], SOURCE_DISABLED_DETAIL)

    def test_builtin_disabled_is_reported_but_unified_routes_404(self):
        with TestClient(build_app(settings=unified_settings(builtin=False))) as client:
            builtin = _sources(client)["builtin"]
            self.assertFalse(builtin["enabled"])
            self.assertEqual(builtin["app_count"], 1)  # 与旧 /sources 一致：禁用也照实报数量
            response = client.get("/api/v1/sources/builtin/apps")
            self.assertEqual(response.status_code, 404)


class LenovoUnifiedApiTests(unittest.TestCase):
    def test_categories_flattens_software_and_game_groups(self):
        service = FakeLenovoService()
        with TestClient(build_app(lenovo_service=service)) as client:
            response = client.get("/api/v1/sources/lenovo/categories")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        payload = response.json()
        self.assertEqual(payload["source"], "lenovo")
        self.assertTrue(payload["updated_at"].startswith("2026-11-14T09:00:00"))
        self.assertEqual(payload["categories"], [
            {"id": "1", "name": "聊天社交", "count": 228, "group": "software"},
            {"id": "19", "name": "角色扮演", "count": 205, "group": "game"},
        ])

    def test_catalog_uses_source_local_ids_and_unified_proxy_paths(self):
        service = FakeLenovoService()
        with TestClient(build_app(lenovo_service=service)) as client:
            response = client.get("/api/v1/sources/lenovo/apps")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            payload = response.json()
            # 请求路径只读目录缓存：统一 GET 不触发联想抓取。
            self.assertEqual(service.calls, [("get_catalog", False)])
        self.assertEqual(payload["source"], "lenovo")
        self.assertEqual(payload["source_name"], "联想应用商店")
        self.assertFalse(payload["stale"])
        self.assertEqual(payload["total"], 1)
        item = payload["items"][0]
        # id 是来源内 softID（保留大小写），不是带来源前缀的旧主键。
        self.assertEqual(item["id"], LENOVO_SOFT_ID)
        self.assertEqual(item["name"], "QQ")
        self.assertEqual(item["version"], "9.9.37.54395")
        self.assertEqual(item["architectures"], ["x64"])
        self.assertEqual(item["group"], "software")
        # 图标仍优先 CDN 直连地址；代理兜底改成统一前缀。
        self.assertEqual(item["icon_url"], "https://lsw-fast.lenovo.com.cn/logo/L105867.png")
        self.assertEqual(item["icon_proxy_url"], "/api/v1/sources/lenovo/apps/L105867/icon")

    def test_search(self):
        service = FakeLenovoService()
        with TestClient(build_app(lenovo_service=service)) as client:
            response = client.get("/api/v1/sources/lenovo/search", params={"q": "QQ", "limit": 5})
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["source"], "lenovo")
            self.assertEqual(payload["query"], "QQ")
            self.assertEqual(payload["total"], 1)
            self.assertEqual(payload["items"][0]["id"], LENOVO_SOFT_ID)
            self.assertEqual(service.calls, [("search", "QQ", 5)])
            # q 缺失或超长由统一路由校验成 422。
            self.assertEqual(client.get("/api/v1/sources/lenovo/search").status_code, 422)
            self.assertEqual(
                client.get("/api/v1/sources/lenovo/search", params={"q": "x" * 101}).status_code, 422
            )

    def test_details_include_screenshots_cover_and_video_proxy_urls(self):
        service = FakeLenovoService()
        with TestClient(build_app(lenovo_service=service)) as client:
            response = client.get(f"/api/v1/sources/lenovo/apps/{LENOVO_SOFT_ID}")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["id"], LENOVO_SOFT_ID)
        self.assertEqual(payload["long_description"], "QQ 的详细描述。")
        self.assertEqual(payload["release_notes"], "修复已知问题并提升稳定性。")
        self.assertEqual(payload["labels"], ["聊天社交", "办公辅助工具"])
        self.assertEqual(payload["ranking"], "聊天社交类下载榜第2名")
        self.assertEqual(payload["detail_tags"], [
            {"name": "安全认证", "tips": "软件通过多款主流安全软件检测。"}
        ])
        self.assertEqual(payload["cover_url"], "https://imgs.lenovomm.cn/cover.jpg")
        self.assertEqual(payload["cover_proxy_url"], "/api/v1/sources/lenovo/apps/L105867/cover")
        image, video = payload["screenshots"]
        self.assertEqual(image, {
            "index": 0,
            "kind": "image",
            "url": "https://lsw-fast.lenovo.com.cn/img/qq-1.png",
            "proxy_url": "/api/v1/sources/lenovo/apps/L105867/screenshots/0",
            "video_proxy_url": None,
        })
        self.assertEqual(video["kind"], "video")
        self.assertEqual(video["url"], "https://imgs.lenovomm.cn/thumb.jpg")
        self.assertEqual(
            video["video_proxy_url"],
            "/api/v1/sources/lenovo/apps/L105867/screenshots/1/video",
        )

    def test_download_returns_release_with_no_store(self):
        service = FakeLenovoService()
        with TestClient(build_app(lenovo_service=service)) as client:
            response = client.get(
                f"/api/v1/sources/lenovo/apps/{LENOVO_SOFT_ID}/download",
                params={"architecture": "x86"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            payload = response.json()
            self.assertEqual(payload["architecture"], "x86")
            self.assertEqual(payload["download_url"], LENOVO_DOWNLOAD_URL)
            self.assertEqual(payload["version"], "9.9.37.54395")
            # 统一路由把 architecture 原样传给来源服务。
            self.assertIn(("resolve_download", LENOVO_SOFT_ID, "x86"), service.calls)

    def test_icon_screenshot_cover_and_conditional_requests(self):
        service = FakeLenovoService()
        with TestClient(build_app(lenovo_service=service)) as client:
            icon = client.get(f"/api/v1/sources/lenovo/apps/{LENOVO_SOFT_ID}/icon")
            self.assertEqual(icon.status_code, 200)
            self.assertEqual(icon.content, service.image.content)
            self.assertEqual(icon.headers["content-type"], "image/png")
            self.assertEqual(icon.headers["etag"], f'"{LENOVO_ICON_ETAG}"')
            self.assertEqual(icon.headers["cache-control"], "public, max-age=86400")
            cached = client.get(
                f"/api/v1/sources/lenovo/apps/{LENOVO_SOFT_ID}/icon",
                headers={"If-None-Match": f'W/"{LENOVO_ICON_ETAG}"'},
            )
            self.assertEqual(cached.status_code, 304)

            screenshot = client.get(f"/api/v1/sources/lenovo/apps/{LENOVO_SOFT_ID}/screenshots/0")
            self.assertEqual(screenshot.status_code, 200)
            self.assertEqual(screenshot.content, service.image.content)
            cover = client.get(f"/api/v1/sources/lenovo/apps/{LENOVO_SOFT_ID}/cover")
            self.assertEqual(cover.status_code, 200)
            self.assertEqual(cover.content, service.image.content)

    def test_video_proxy_streams_and_head_only_reads_headers(self):
        service = FakeLenovoService()
        with TestClient(build_app(lenovo_service=service)) as client:
            path = f"/api/v1/sources/lenovo/apps/{LENOVO_SOFT_ID}/screenshots/1/video"
            response = client.get(path, headers={"Range": "bytes=0-3"})
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.content, b"fake-video-bytes")
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertIn("content-range", response.headers)
            # Range 头透传给来源服务。
            self.assertIn(
                ("open_screenshot_video", LENOVO_SOFT_ID, 1, "bytes=0-3"), service.calls
            )

            head = client.head(path)
            self.assertEqual(head.status_code, 206)
            self.assertEqual(head.content, b"")
            self.assertIn("content-range", head.headers)
            # HEAD 取完响应头就关闭上游流（两次请求两个流，且都已关闭）。
            self.assertEqual(len(service.video_streams), 2)
            self.assertTrue(all(stream.closed for stream in service.video_streams))

    def test_refresh(self):
        service = FakeLenovoService()
        with TestClient(build_app(lenovo_service=service)) as client:
            response = client.post("/api/v1/sources/lenovo/refresh")
        self.assertEqual(response.status_code, 404)

    def test_missing_app_is_404(self):
        service = FakeLenovoService()
        with TestClient(build_app(lenovo_service=service)) as client:
            for path in (
                "/api/v1/sources/lenovo/apps/doesnotexist",
                "/api/v1/sources/lenovo/apps/doesnotexist/download",
            ):
                with self.subTest(path=path):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 404)

    def test_upstream_errors_map_to_502_and_504(self):
        service = FakeLenovoService()
        service.failures["get_details"] = LenovoTimeoutError("联想请求超时。")
        with TestClient(build_app(lenovo_service=service)) as client:
            response = client.get(f"/api/v1/sources/lenovo/apps/{LENOVO_SOFT_ID}")
            self.assertEqual(response.status_code, 504)
            self.assertEqual(response.headers["cache-control"], "no-store")


class Soft360UnifiedApiTests(unittest.TestCase):
    def test_categories_are_flat_without_groups(self):
        service = FakeSoft360Service()
        with TestClient(build_app(soft360_service=service)) as client:
            response = client.get("/api/v1/sources/soft360/categories")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["source"], "soft360")
        self.assertEqual(payload["categories"], [
            {"id": "149", "name": "聊天工具", "count": 12, "group": None}
        ])

    def test_catalog_has_null_version_but_source_local_ids(self):
        service = FakeSoft360Service()
        with TestClient(build_app(soft360_service=service)) as client:
            response = client.get("/api/v1/sources/soft360/apps")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        payload = response.json()
        self.assertEqual(payload["source"], "soft360")
        self.assertEqual(payload["source_name"], "360软件管家")
        item = payload["items"][0]
        self.assertEqual(item["id"], SOFT360_SOFT_ID)
        self.assertIsNone(item["version"])  # 目录接口没有版本，详情才有
        self.assertIsNone(item["group"])
        self.assertEqual(item["icon_proxy_url"], "/api/v1/sources/soft360/apps/1010/icon")
        self.assertEqual(item["architectures"], ["x86", "x64"])

    def test_search_is_served_by_the_server(self):
        service = FakeSoft360Service()
        with TestClient(build_app(soft360_service=service)) as client:
            response = client.get("/api/v1/sources/soft360/search", params={"q": "微信"})
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["query"], "微信")
            self.assertEqual(payload["total"], 1)
            self.assertEqual(payload["items"][0]["id"], SOFT360_SOFT_ID)
            self.assertIn(("search", "微信", 30), service.calls)

    def test_details_and_screenshots(self):
        service = FakeSoft360Service()
        with TestClient(build_app(soft360_service=service)) as client:
            response = client.get(f"/api/v1/sources/soft360/apps/{SOFT360_SOFT_ID}")
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["version"], "3.9.12.55")
            self.assertEqual(payload["publisher"], "腾讯")
            self.assertEqual(payload["long_description"], "微信 PC 版详细描述。")
            self.assertIsNone(payload["cover_url"])
            self.assertIsNone(payload["cover_proxy_url"])
            self.assertEqual(payload["screenshots"], [{
                "index": 0,
                "kind": "image",
                "url": "https://p15.qhmsg.com/shot-0.png",
                "proxy_url": "/api/v1/sources/soft360/apps/1010/screenshots/0",
                "video_proxy_url": None,
            }])
            screenshot = client.get(f"/api/v1/sources/soft360/apps/{SOFT360_SOFT_ID}/screenshots/0")
            self.assertEqual(screenshot.status_code, 200)
            self.assertEqual(screenshot.content, service.image.content)

    def test_download_and_icon(self):
        service = FakeSoft360Service()
        with TestClient(build_app(soft360_service=service)) as client:
            response = client.get(f"/api/v1/sources/soft360/apps/{SOFT360_SOFT_ID}/download")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(response.json()["download_url"], SOFT360_DOWNLOAD_URL)
            self.assertIsNone(response.json()["sha256"])

            icon = client.get(f"/api/v1/sources/soft360/apps/{SOFT360_SOFT_ID}/icon")
            self.assertEqual(icon.status_code, 200)
            self.assertEqual(icon.content, service.image.content)
            self.assertEqual(icon.headers["etag"], f'"{SOFT360_ICON_ETAG}"')

    def test_cover_and_video_capabilities_are_unsupported(self):
        service = FakeSoft360Service()
        with TestClient(build_app(soft360_service=service)) as client:
            for path in (
                f"/api/v1/sources/soft360/apps/{SOFT360_SOFT_ID}/cover",
                f"/api/v1/sources/soft360/apps/{SOFT360_SOFT_ID}/screenshots/0/video",
            ):
                with self.subTest(path=path):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 404)
                    self.assertEqual(response.json()["detail"], UNSUPPORTED_DETAIL)

    def test_refresh(self):
        service = FakeSoft360Service()
        with TestClient(build_app(soft360_service=service)) as client:
            response = client.post("/api/v1/sources/soft360/refresh")
        self.assertEqual(response.status_code, 404)

    def test_missing_app_is_404(self):
        with TestClient(build_app()) as client:
            self.assertEqual(client.get("/api/v1/sources/soft360/apps/999").status_code, 404)


class BuiltinUnifiedApiTests(unittest.TestCase):
    def test_categories_derived_from_catalog(self):
        with TestClient(build_app()) as client:
            response = client.get("/api/v1/sources/builtin/categories")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["source"], "builtin")
        self.assertEqual(payload["categories"], [
            {"id": "社交通讯", "name": "社交通讯", "count": 1, "group": None}
        ])

    def test_catalog_uses_provider_version_and_unified_icon_proxy(self):
        provider = FakeBuiltinProvider()
        with TestClient(build_app(providers={BUILTIN_PROVIDER_ID: provider})) as client:
            response = client.get("/api/v1/sources/builtin/apps")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            payload = response.json()
        self.assertEqual(payload["source"], "builtin")
        self.assertEqual(payload["source_name"], "内置精选")
        self.assertEqual(payload["total"], 1)
        item = payload["items"][0]
        self.assertEqual(item["id"], BUILTIN_APP_ID)
        # 版本来自 provider（与旧 /apps 的 latest_release 同一条解析路径）。
        self.assertEqual(item["version"], "1.2.3")
        self.assertIsNone(item["icon_url"])
        self.assertEqual(item["icon_proxy_url"], "/api/v1/sources/builtin/apps/qq/icon")
        self.assertEqual(item["architectures"], ["x64", "x86", "arm64"])
        self.assertGreaterEqual(provider.calls, 1)

    def test_details_and_download(self):
        provider = FakeBuiltinProvider()
        with TestClient(build_app(providers={BUILTIN_PROVIDER_ID: provider})) as client:
            details = client.get("/api/v1/sources/builtin/apps/qq")
            self.assertEqual(details.status_code, 200)
            payload = details.json()
            self.assertEqual(payload["id"], "qq")
            self.assertEqual(payload["version"], "1.2.3")
            self.assertEqual(payload["screenshots"], [])
            self.assertIsNone(payload["cover_url"])
            self.assertTrue(payload["long_description"])

            download = client.get("/api/v1/sources/builtin/apps/qq/download")
            self.assertEqual(download.status_code, 200)
            self.assertEqual(download.headers["cache-control"], "no-store")
            release = download.json()
            self.assertEqual(release["architecture"], "x64")
            self.assertIn("sign=builtin-test", release["download_url"])
            # 下载地址始终重新解析（/releases/latest 语义）。
            self.assertGreaterEqual(provider.force_refreshes, 1)

            x86 = client.get(
                "/api/v1/sources/builtin/apps/qq/download", params={"architecture": "x86"}
            )
            self.assertEqual(x86.status_code, 200)
            self.assertEqual(x86.json()["architecture"], "x86")

    def test_icon_is_served_from_backend_assets(self):
        with TestClient(build_app()) as client:
            response = client.get("/api/v1/sources/builtin/apps/qq/icon")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "public, max-age=604800")
            self.assertTrue(response.headers["etag"])
            self.assertGreater(len(response.content), 0)
            cached = client.get(
                "/api/v1/sources/builtin/apps/qq/icon",
                headers={"If-None-Match": response.headers["etag"]},
            )
            self.assertEqual(cached.status_code, 304)

    def test_unsupported_capabilities_are_404(self):
        with TestClient(build_app()) as client:
            for path in (
                "/api/v1/sources/builtin/search?q=qq",
                "/api/v1/sources/builtin/apps/qq/screenshots/0",
                "/api/v1/sources/builtin/apps/qq/cover",
                "/api/v1/sources/builtin/apps/qq/screenshots/0/video",
            ):
                with self.subTest(path=path):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 404)
                    self.assertEqual(response.json()["detail"], UNSUPPORTED_DETAIL)
            refresh = client.post("/api/v1/sources/builtin/refresh")
            self.assertEqual(refresh.status_code, 404)

    def test_missing_app_is_404(self):
        with TestClient(build_app()) as client:
            # 白名单外的应用（数据仍加载但未暴露）在统一接口里同样 404。
            self.assertEqual(client.get("/api/v1/sources/builtin/apps/wechat").status_code, 404)


class LegacyCompatibilityTests(unittest.TestCase):
    def test_legacy_routes_are_unchanged(self):
        with TestClient(build_app()) as client:
            # 内置精选旧接口仍在。
            apps = client.get("/api/v1/apps")
            self.assertEqual(apps.status_code, 200)
            self.assertEqual([item["id"] for item in apps.json()["items"]], ["qq"])
            self.assertEqual(client.get("/api/v1/apps/qq").status_code, 200)
            # 联想 / 360 旧接口仍在。
            self.assertEqual(client.get("/api/v1/store/lenovo/apps").status_code, 200)
            self.assertEqual(client.get("/api/v1/store/360/apps").status_code, 200)
            self.assertEqual(client.get("/api/v1/store/lenovo/apps/L105867").status_code, 200)
            self.assertEqual(client.get("/api/v1/store/soft360-does-not-exist").status_code, 404)

    def test_unified_and_legacy_share_the_same_source_service(self):
        service = FakeLenovoService()
        with TestClient(build_app(lenovo_service=service)) as client:
            legacy = client.get("/api/v1/store/lenovo/apps")
            unified = client.get("/api/v1/sources/lenovo/apps")
        self.assertEqual(legacy.status_code, 200)
        self.assertEqual(unified.status_code, 200)
        # 两边都只读取同一份目录快照（没有额外抓取，也没有递归调用统一路由）。
        self.assertEqual([call[0] for call in service.calls].count("get_catalog"), 2)


# ---------------------------------------------------------------------------
# 新增来源：只需要实现/注册 adapter
# ---------------------------------------------------------------------------


class CustomSourceAdapter(SourceAdapter):
    """测试用“新来源”：只实现目录/详情/下载就能被统一路由与客户端发现。"""

    source_id = "myoem"
    source_name = "OEM 商店"
    source_description = "测试用的新增来源。"
    search_mode = "catalog"
    capabilities = SourceCapabilities(
        categories=True, catalog=True, details=True, download=True
    )

    def __init__(self, *, enabled=True):
        super().__init__(enabled=enabled)
        self.calls = []

    def app_count(self):
        return 1

    async def list_apps(self):
        self.calls.append("list_apps")
        return SourceCatalog(
            source=self.source_id,
            source_name=self.source_name,
            total=1,
            items=[SourceAppSummary(
                id="app1",
                name="OEM 应用",
                description="来源注册即接入。",
                category="工具",
            )],
        )

    async def get_app(self, app_id):
        self.calls.append(("get_app", app_id))
        if app_id != "app1":
            from Backend.unified.errors import SourceNotFoundError

            raise SourceNotFoundError("应用不存在。")
        return SourceAppDetails(
            id="app1",
            name="OEM 应用",
            description="来源注册即接入。",
            category="工具",
            long_description="详情。",
            screenshots=[],
        )

    async def get_download(self, app_id, architecture):
        self.calls.append(("get_download", app_id, architecture))
        return Release.model_validate({
            "id": "myoem-app1-1.0.0-windows-x64",
            "version": "1.0.0",
            "platform": "windows",
            "architecture": architecture,
            "download_url": "https://oem.example/app1-1.0.0.exe",
            "file_name": "app1-1.0.0.exe",
            "size_bytes": 2048,
            "sha256": None,
        })


class NewSourceRegistrationTests(unittest.TestCase):
    def test_registry_adapter_is_all_the_unified_api_needs(self):
        adapter = CustomSourceAdapter()
        api = FastAPI()
        api.state.source_registry = SourceRegistry([adapter])
        api.include_router(create_unified_router())

        with TestClient(api) as client:
            sources = client.get("/api/v1/sources")
            self.assertEqual(sources.status_code, 200)
            payload = sources.json()
            self.assertEqual(payload["total"], 1)
            self.assertEqual(payload["items"][0]["id"], "myoem")
            self.assertEqual(payload["items"][0]["name"], "OEM 商店")
            self.assertEqual(payload["items"][0]["app_count"], 1)
            self.assertEqual(payload["items"][0]["capabilities"], {
                "categories": True,
                "catalog": True,
                "search": False,
                "details": True,
                "download": True,
                "screenshots": False,
                "cover": False,
                "video": False,
                "refresh": False,
            })

            # 统一路由对新增来源立即可用（没有任何来源 ID 分支）。
            apps = client.get("/api/v1/sources/myoem/apps")
            self.assertEqual(apps.status_code, 200)
            self.assertEqual(apps.json()["items"][0]["id"], "app1")
            self.assertEqual(apps.json()["source"], "myoem")
            detail = client.get("/api/v1/sources/myoem/apps/app1")
            self.assertEqual(detail.status_code, 200)
            self.assertEqual(detail.json()["name"], "OEM 应用")
            download = client.get("/api/v1/sources/myoem/apps/app1/download")
            self.assertEqual(download.status_code, 200)
            self.assertEqual(download.json()["architecture"], "x64")
            # 未实现的能力自动 404，来源不需要写任何额外分支。
            for path in (
                "/api/v1/sources/myoem/search?q=x",
                "/api/v1/sources/myoem/apps/app1/cover",
                "/api/v1/sources/myoem/apps/app1/screenshots/0",
            ):
                with self.subTest(path=path):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 404)
                    self.assertEqual(response.json()["detail"], UNSUPPORTED_DETAIL)

    def test_disabled_registered_source_is_hidden_but_listed(self):
        adapter = CustomSourceAdapter(enabled=False)
        api = FastAPI()
        api.state.source_registry = SourceRegistry([adapter])
        api.include_router(create_unified_router())
        with TestClient(api) as client:
            self.assertFalse(client.get("/api/v1/sources").json()["items"][0]["enabled"])
            self.assertEqual(client.get("/api/v1/sources/myoem/apps").status_code, 404)

    def test_factory_appends_extra_adapters_after_builtin_sources(self):
        with TestClient(build_app()) as client:
            api = client.app
            registry = create_source_registry(
                catalog=api.state.catalog,
                releases=api.state.releases,
                warmup=api.state.warmup,
                icons=api.state.icons,
                settings=api.state.settings,
                lenovo_service=api.state.lenovo,
                soft360_service=api.state.soft360,
                extra_adapters=[CustomSourceAdapter()],
            )
        self.assertEqual(
            [adapter.source_id for adapter in registry.adapters],
            ["lenovo", "builtin", "soft360", "myoem"],
        )
        self.assertEqual(registry.sources_response().total, 4)
        self.assertIsNotNone(registry.get("myoem"))


# ---------------------------------------------------------------------------
# 统一模型契约
# ---------------------------------------------------------------------------


class UnifiedModelContractTests(unittest.TestCase):
    def test_core_fields_are_required(self):
        with self.assertRaises(ValidationError):
            SourceAppSummary.model_validate({"id": "a", "name": "n", "description": "d"})
        with self.assertRaises(ValidationError):
            SourceAppSummary.model_validate({
                "id": "a",
                "name": "n",
                "description": "d",
                "category": "c",
                "unknown_field": True,
            })

    def test_total_must_match_items(self):
        with self.assertRaises(ValidationError):
            SourceCatalog(source="lenovo", source_name="联想应用商店", total=1, items=[])

    def test_video_proxy_url_only_for_video_screenshots(self):
        with self.assertRaises(ValidationError):
            SourceScreenshot(
                index=0,
                kind="image",
                proxy_url="/api/v1/sources/lenovo/apps/1/screenshots/0",
                video_proxy_url="/api/v1/sources/lenovo/apps/1/screenshots/0/video",
            )
        screenshot = SourceScreenshot(
            index=1,
            kind="video",
            video_proxy_url="/api/v1/sources/lenovo/apps/1/screenshots/1/video",
        )
        self.assertEqual(screenshot.kind, "video")

    def test_proxy_urls_must_be_backend_relative(self):
        with self.assertRaises(ValidationError):
            SourceAppSummary(
                id="a",
                name="n",
                description="d",
                category="c",
                icon_proxy_url="https://evil.example/icon.png",
            )


if __name__ == "__main__":
    unittest.main()
