"""联想目录“请求只读缓存 + 独立后台线程刷新”的测试（MockTransport + 临时目录，无真实网络）。

覆盖：

- 独立 daemon 线程（`lenovo-catalog-refresher`）随 service/lifespan 启动即存在且启动不联网；
- 首次刷新策略：陈旧快照立即补刷、新鲜快照等剩余间隔、没有快照等满一个间隔；
- 0.05 秒小间隔连续多轮刷新并写 ``catalog.json``；刷新失败只记 WARNING、保留旧快照；
- ``shutdown`` 后线程 join 退出（``is_alive()==False``）；
- ``create_app`` lifespan：真实服务启动时预加载磁盘快照并启动线程，关闭时停止；
- ``GET`` 带 ``refresh=true`` 只读缓存，不触发抓取/后台刷新、不改写 ``catalog.json``；
- ``GET /api/v1/cache`` 的 ``lenovo.catalog`` 状态字段。
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

from fastapi.testclient import TestClient

import httpx2

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR.parent))

from Backend.config import (
    BackendSettings,
    CacheConfig,
    LenovoConfig,
    LoggingConfig,
    RefreshConfig,
)
from Backend.lenovo.catalog import (
    DEFAULT_CATALOG_REFRESH_INTERVAL_SECONDS,
    LenovoCatalogBuilder,
    LenovoCatalogStore,
)
from Backend.lenovo.client import LenovoRawClient
from Backend.lenovo.models import StoreAppSummary, StoreCategory, StoreCategoryGroup
from Backend.lenovo.service import create_lenovo_store_service
from Backend.main import create_app as create_application


# 与 test_api.py / test_lenovo_api.py 相同：指向不存在的 apps.d，保持内置精选源为 8 款。
FIXTURE_EXTRA_DIRECTORY = BACKEND_DIR / "tests" / "no-extra-apps.d"
PROVIDER_IDS = ("wechat", "vscode", "7zip", "vlc", "firefox", "obs", "notepadplusplus", "qq")


def make_jwt(exp: int) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"typ": "JWT", "alg": "HS256"}).encode()).rstrip(b"=")
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=")
    return f"{header.decode()}.{payload.decode()}.signature"


def app_item(soft_id: str = "L105867", name: str = "QQ") -> dict:
    return {
        "softID": soft_id,
        "softName": name,
        "version": "9.9.37.54395",
        "installFileSize": "318.95 MB",
        "introduction": f"{name} 简介",
        "logoFile": f"https://lsw-fast.lenovo.com.cn/logo/{soft_id}.png",
        "captureFileList": [],
        "score": "60",
        "downloadCount": "277万次",
        "supportOsbit": ["64"],
        "supportPlatform": ["win10", "win11"],
        "stable": "stable",
        "classID": "1",
    }


TREE = [
    {
        "code": "soft",
        "name": "软件",
        "categories": [
            {
                "type": "pc",
                "name": "PC",
                "appClassList": [{"classId": "1", "className": "聊天社交", "tags": None}],
            }
        ],
    }
]


class FakeUpstream:
    """最小的联想接口替身：token + 分类树 + 一个分类。"""

    def __init__(self) -> None:
        self.class_requests = 0
        self.request_count = 0
        self.fail = False

    def _json(self, request: httpx2.Request, payload: dict) -> httpx2.Response:
        return httpx2.Response(200, json=payload, request=request)

    def _ok(self, request: httpx2.Request, data) -> httpx2.Response:
        return self._json(request, {"status": 0, "message": None, "data": data})

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.request_count += 1
        path = request.url.path
        if path == "/appbase/token/register":
            return self._ok(request, make_jwt(int(time.time()) + 15 * 24 * 3600))
        if path == "/classifyservice/class/composite_class_list":
            return self._ok(request, TREE)
        if path == "/classifyservice/class/composite_class_app_list":
            self.class_requests += 1
            if self.fail:
                return self._json(request, {"status": 500, "message": "上游错误", "data": None})
            return self._ok(request, {"count": 1, "apps": [app_item()], "code": "ranklist"})
        raise AssertionError(f"未预期的联想请求: {request.url}")


def make_service_settings(*, refresh_interval: float = 300.0, ttl: float = 21600.0) -> SimpleNamespace:
    return SimpleNamespace(
        lenovo=SimpleNamespace(
            catalog_ttl_seconds=ttl,
            catalog_refresh_interval_seconds=refresh_interval,
            catalog_max_stale_seconds=604800.0,
            details_ttl_seconds=900.0,
            details_persistent_ttl_seconds=21600.0,
            search_cache_ttl_seconds=60.0,
            timeout_seconds=20.0,
            catalog_concurrency=2,
            image_cache_ttl_seconds=2592000.0,
            image_cache_max_bytes=268435456,
        )
    )


async def wait_for(predicate, *, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


class StoreBackgroundRefreshTests(unittest.IsolatedAsyncioTestCase):
    """store 自身的后台单飞刷新（force_refresh 路径）；定时刷新线程在服务层。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="wam-lenovo-refresher-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "lenovo"
        self.upstream = FakeUpstream()
        self.client = httpx2.AsyncClient(transport=httpx2.MockTransport(self.upstream.handler))
        self.addAsyncCleanup(self.client.aclose)

    def make_store(self, **kwargs) -> LenovoCatalogStore:
        builder = LenovoCatalogBuilder(LenovoRawClient(self.client))
        return LenovoCatalogStore(builder, self.directory, **kwargs)

    async def test_force_refresh_returns_before_the_background_scrape_runs(self):
        store = self.make_store(ttl_seconds=60.0)
        await store.get()
        self.assertEqual(self.upstream.class_requests, 1)

        # get(force_refresh=True) 只创建后台任务就返回：事件循环还没让出给后台抓取。
        snapshot = await store.get(force_refresh=True)
        self.assertEqual(len(snapshot.apps), 1)
        self.assertEqual(self.upstream.class_requests, 1)
        self.assertFalse(store.catalog_status()["refresher_running"])

        await store.wait_for_pending_refresh()
        self.assertEqual(self.upstream.class_requests, 2)
        self.assertEqual(store.counters["background_refreshes"], 1)
        await store.shutdown()


class ServiceRefresherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="wam-lenovo-service-refresher-")
        self.addCleanup(self.temporary.cleanup)
        self.cache_directory = Path(self.temporary.name) / "cache"
        self.upstream = FakeUpstream()
        self.client = httpx2.AsyncClient(transport=httpx2.MockTransport(self.upstream.handler))
        self.addAsyncCleanup(self.client.aclose)

    def make_service(self, *, refresh_interval: float = 300.0):
        return create_lenovo_store_service(
            self.client,
            self.cache_directory,
            make_service_settings(refresh_interval=refresh_interval),
            cache_enabled=True,
            cache_persist=True,
        )

    async def test_thread_is_created_alive_and_daemon_after_start(self):
        write_catalog_file(self.cache_directory)  # 新鲜快照：第一次刷新要等剩余间隔
        service = self.make_service(refresh_interval=300)
        thread = None
        try:
            self.assertIsNotNone(await service.preload_catalog())
            self.assertEqual(self.upstream.request_count, 0)  # 预加载只读磁盘
            self.assertTrue(await service.start_catalog_refresher())
            thread = service.catalog_refresher_thread
            self.assertIsNotNone(thread)
            self.assertTrue(thread.is_alive())
            self.assertTrue(thread.daemon)
            self.assertEqual(thread.name, "lenovo-catalog-refresher")
            status = service.catalog_status()
            self.assertTrue(status["refresher_running"])
            self.assertEqual(status["refresh_interval_seconds"], 300.0)
            self.assertIsNotNone(status["next_refresh_at"])
            self.assertEqual(status["last_error"], None)
            await asyncio.sleep(0.1)
            self.assertEqual(self.upstream.request_count, 0)  # 启动阶段不联网
        finally:
            await service.shutdown()
        self.assertIsNotNone(thread)
        self.assertFalse(thread.is_alive())
        self.assertFalse(service.catalog_status()["refresher_running"])

    async def test_thread_refreshes_periodically_and_writes_the_disk(self):
        service = self.make_service(refresh_interval=0.05)
        try:
            catalog = await service.get_catalog()  # 冷启动一轮并写盘
            self.assertEqual(catalog.total, 1)
            catalog_path = self.cache_directory / "lenovo" / "catalog.json"
            self.assertTrue(catalog_path.is_file())
            self.assertEqual(service.catalog_status()["refreshes"], 1)

            with self.assertLogs("winappsmanager.lenovo", level="INFO") as captured:
                self.assertTrue(await service.start_catalog_refresher())
                self.assertTrue(
                    await wait_for(lambda: service.catalog_status()["refreshes"] >= 3),
                    "daemon 线程没有按 0.05 秒间隔连续刷新",
                )
            logs = "\n".join(captured.output)
            self.assertIn("定时刷新线程已启动", logs)
            self.assertIn("联想目录刷新完成", logs)
            self.assertEqual(service.catalog_status()["last_error"], None)
            self.assertGreaterEqual(self.upstream.class_requests, 3)
            # 每轮刷新都原子写盘，文件始终是合法快照。
            document = json.loads(catalog_path.read_text(encoding="utf-8"))
            self.assertEqual(len(document["apps"]), 1)
        finally:
            await service.shutdown()

    async def test_stale_snapshot_is_refreshed_immediately_on_start(self):
        write_catalog_file(self.cache_directory, fetched_at=time.time() - 600.0)
        service = self.make_service(refresh_interval=300)
        try:
            loaded = await service.preload_catalog()
            self.assertIsNotNone(loaded)
            self.assertEqual(self.upstream.request_count, 0)  # 预加载不联网
            self.assertTrue(await service.start_catalog_refresher())
            self.assertTrue(
                await wait_for(lambda: service.catalog_status()["refreshes"] >= 1, timeout=2.0),
                "启动时陈旧快照没有立即补刷一次",
            )
            self.assertGreater(self.upstream.request_count, 0)
        finally:
            await service.shutdown()

    async def test_fresh_snapshot_waits_for_the_remaining_interval(self):
        write_catalog_file(self.cache_directory)
        service = self.make_service(refresh_interval=0.4)
        try:
            self.assertIsNotNone(await service.preload_catalog())
            self.assertTrue(await service.start_catalog_refresher())
            await asyncio.sleep(0.15)
            self.assertEqual(self.upstream.request_count, 0)
            self.assertEqual(service.catalog_status()["refreshes"], 0)
            self.assertTrue(
                await wait_for(lambda: service.catalog_status()["refreshes"] >= 1),
                "快照新鲜时没有在剩余间隔后刷新",
            )
        finally:
            await service.shutdown()

    async def test_no_snapshot_waits_one_interval_before_network(self):
        service = self.make_service(refresh_interval=0.3)
        try:
            self.assertIsNone(await service.preload_catalog())
            self.assertEqual(self.upstream.request_count, 0)
            self.assertTrue(await service.start_catalog_refresher())
            await asyncio.sleep(0.15)
            self.assertEqual(self.upstream.request_count, 0)  # 没有快照也不立即联网
            self.assertEqual(service.catalog_status()["refreshes"], 0)
            self.assertTrue(
                await wait_for(lambda: service.catalog_status()["refreshes"] >= 1),
                "没有快照时线程没有在一个间隔后刷新",
            )
            self.assertGreater(self.upstream.class_requests, 0)
        finally:
            await service.shutdown()

    async def test_refresher_failure_keeps_the_old_snapshot_and_records_the_error(self):
        write_catalog_file(self.cache_directory)
        service = self.make_service(refresh_interval=0.05)
        try:
            self.assertIsNotNone(await service.preload_catalog())
            self.upstream.fail = True
            self.assertTrue(await service.start_catalog_refresher())
            self.assertTrue(
                await wait_for(lambda: service.catalog_status()["last_error"] is not None),
                "刷新失败后没有记录 last_error",
            )
            kept = await service.get_catalog()
            self.assertEqual(kept.total, 1)
            status = service.catalog_status()
            self.assertTrue(status["refresher_running"])
            self.assertIn("联想", status["last_error"])
            self.assertEqual(status["refreshes"], 0)  # 失败轮不计入成功次数
        finally:
            await service.shutdown()

    async def test_shutdown_joins_the_thread(self):
        write_catalog_file(self.cache_directory, fetched_at=time.time() - 600.0)
        service = self.make_service(refresh_interval=300)
        thread = None
        try:
            await service.preload_catalog()
            self.assertTrue(await service.start_catalog_refresher())
            thread = service.catalog_refresher_thread
            self.assertIsNotNone(thread)
            self.assertTrue(thread.is_alive())
        finally:
            await service.shutdown()
        self.assertIsNotNone(thread)
        self.assertFalse(thread.is_alive())
        self.assertFalse(service.catalog_status()["refresher_running"])
        self.assertIsNone(service.catalog_refresher_thread)

    async def test_service_disables_the_refresher_when_the_interval_is_zero(self):
        service = self.make_service(refresh_interval=0)
        try:
            with self.assertLogs("winappsmanager.lenovo", level="INFO") as captured:
                self.assertFalse(await service.start_catalog_refresher())
            self.assertIn("未启用", "\n".join(captured.output))
            status = service.catalog_status()
            self.assertFalse(status["refresher_running"])
            self.assertEqual(status["refresh_interval_seconds"], 0.0)
            self.assertIsNone(service.catalog_refresher_thread)
        finally:
            await service.shutdown()

    async def test_service_preload_loads_the_disk_snapshot_without_network(self):
        service = self.make_service()
        await service.get_catalog()
        await service.shutdown()

        def exploding_handler(request: httpx2.Request) -> httpx2.Response:
            raise AssertionError(f"预加载不应访问网络: {request.url}")

        offline_client = httpx2.AsyncClient(transport=httpx2.MockTransport(exploding_handler))
        self.addAsyncCleanup(offline_client.aclose)
        restarted = create_lenovo_store_service(
            offline_client,
            self.cache_directory,
            make_service_settings(refresh_interval=300),
            cache_enabled=True,
            cache_persist=True,
        )
        try:
            loaded = await restarted.preload_catalog()
            self.assertIsNotNone(loaded)
            self.assertEqual(len(loaded.apps), 1)
            self.assertEqual(restarted.cached_app_count(), 1)
            catalog = await restarted.get_catalog()
            self.assertEqual(catalog.total, 1)
            self.assertEqual(restarted.catalog_status()["apps"], 1)
        finally:
            await restarted.shutdown()

    async def test_service_status_keeps_the_configured_interval_before_start(self):
        service = self.make_service(refresh_interval=DEFAULT_CATALOG_REFRESH_INTERVAL_SECONDS)
        try:
            status = service.catalog_status()
            self.assertEqual(status["refresh_interval_seconds"], DEFAULT_CATALOG_REFRESH_INTERVAL_SECONDS)
        finally:
            await service.shutdown()


def summary_document(*, fetched_at: float | None = None) -> dict:
    """直接写一份合法的 catalog.json（schema 见 LenovoCatalogStore._write_disk_sync）。"""

    summary = StoreAppSummary.model_validate({
        "id": "lenovo-l105867",
        "soft_id": "L105867",
        "name": "QQ",
        "description": "QQ 简介",
        "category": "聊天社交",
        "category_id": "1",
        "group": "software",
        "version": "9.9.37.54395",
        "size_display": "318.95 MB",
        "platforms": "Windows 10 / 11 · 64 位",
        "architectures": ["x64"],
        "icon_url": "https://lsw-fast.lenovo.com.cn/logo/L105867.png",
        "icon_proxy_url": "/api/v1/store/lenovo/apps/L105867/icon",
        "icon_text": "QQ",
        "icon_background": "#1679C8",
        "tags": ["聊天社交"],
    })
    group = StoreCategoryGroup(
        code="software",
        name="软件",
        categories=[StoreCategory(id="1", name="聊天社交", count=1)],
    )
    return {
        "schema_version": 1,
        "fetched_at": time.time() if fetched_at is None else float(fetched_at),
        "apps": [summary.model_dump(mode="json")],
        "logo_urls": {"L105867": summary.icon_url},
        "groups": [group.model_dump(mode="json")],
    }


def write_catalog_file(cache_directory: Path, *, fetched_at: float | None = None) -> Path:
    path = cache_directory / "lenovo" / "catalog.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(summary_document(fetched_at=fetched_at), ensure_ascii=False), encoding="utf-8"
    )
    return path


class EmptyProvider:
    async def get_releases(self, *, force_refresh=False):
        return []

    async def resolve_download(self, release):
        raise AssertionError("测试替身不解析下载地址")


def make_app_settings(cache_directory: Path, *, refresh_interval: float = 300.0) -> BackendSettings:
    return BackendSettings(
        cache=CacheConfig(persist=True, directory=cache_directory),
        logging=LoggingConfig(enabled=False),
        refresh=RefreshConfig(on_startup=False, interval_seconds=0),
        lenovo=LenovoConfig(catalog_refresh_interval_seconds=refresh_interval),
    )


def create_app(cache_directory: Path, *, refresh_interval: float = 300.0):
    providers = {provider_id: EmptyProvider() for provider_id in PROVIDER_IDS}
    return create_application(
        release_providers=providers,
        settings=make_app_settings(cache_directory, refresh_interval=refresh_interval),
        extra_directory=FIXTURE_EXTRA_DIRECTORY,
    )


class MainLifespanRefresherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="wam-lenovo-lifespan-")
        self.addCleanup(self.temporary.cleanup)
        self.cache_directory = Path(self.temporary.name) / "cache"

    def test_lifespan_preloads_the_disk_snapshot_and_starts_the_thread(self):
        catalog_path = write_catalog_file(self.cache_directory)
        before = catalog_path.stat()
        api = create_app(self.cache_directory)
        thread = None
        with TestClient(api) as client:
            service = api.state.lenovo
            # 启动只读磁盘快照：0 次网络请求即可看到目录。
            self.assertEqual(service.cached_app_count(), 1)
            status = service.catalog_status()
            self.assertEqual(status["apps"], 1)
            self.assertRegex(status["fetched_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
            self.assertTrue(status["refresher_running"])
            self.assertEqual(status["refresh_interval_seconds"], 300.0)
            self.assertIsNotNone(status["next_refresh_at"])
            self.assertEqual(status["refreshes"], 0)

            # 线程在服务启动时即存在：独立的 daemon 线程。
            thread = service.catalog_refresher_thread
            self.assertIsNotNone(thread)
            self.assertTrue(thread.is_alive())
            self.assertTrue(thread.daemon)
            self.assertEqual(thread.name, "lenovo-catalog-refresher")

            # 请求直接命中预加载的内存快照（真实 HTTP 客户端没有 mock，联网就会失败/变慢）。
            catalog = client.get("/api/v1/store/lenovo/apps")
            self.assertEqual(catalog.status_code, 200)
            self.assertEqual(catalog.json()["total"], 1)
            categories = client.get("/api/v1/store/lenovo/categories")
            self.assertEqual(categories.status_code, 200)

            cache = client.get("/api/v1/cache")
            self.assertEqual(cache.status_code, 200)
            lenovo = cache.json()["lenovo"]
            self.assertEqual(lenovo["cached_app_count"], 1)
            self.assertEqual(lenovo["catalog"]["apps"], 1)
            self.assertTrue(lenovo["catalog"]["refresher_running"])
            self.assertEqual(lenovo["catalog"]["refresh_interval_seconds"], 300.0)
            self.assertEqual(lenovo["catalog"]["last_error"], None)

            time.sleep(0.1)  # 线程在等 300 秒，期间不应联网/改写快照
            self.assertEqual(service.catalog_status()["refreshes"], 0)
            after = catalog_path.stat()
            self.assertEqual((after.st_mtime_ns, after.st_size), (before.st_mtime_ns, before.st_size))
        # 关闭服务时线程被 join 退出。
        self.assertIsNotNone(thread)
        self.assertFalse(thread.is_alive())
        self.assertFalse(api.state.lenovo.catalog_status()["refresher_running"])

    def test_lifespan_does_not_start_the_thread_when_the_interval_is_zero(self):
        write_catalog_file(self.cache_directory)
        api = create_app(self.cache_directory, refresh_interval=0)
        with TestClient(api) as client:
            status = api.state.lenovo.catalog_status()
            self.assertFalse(status["refresher_running"])
            self.assertEqual(status["refresh_interval_seconds"], 0.0)
            self.assertIsNone(api.state.lenovo.catalog_refresher_thread)
            cache = client.get("/api/v1/cache").json()["lenovo"]
            self.assertFalse(cache["catalog"]["refresher_running"])
            self.assertEqual(cache["catalog"]["apps"], 1)


class GetQueryDoesNotTriggerScrapingTests(unittest.TestCase):
    """GET 只读：旧客户端（或任何外部调用方）带 ?refresh=true 也必须只读缓存。

    用真实 `LenovoStoreService`（磁盘快照 + 记录请求的 mock 传输层）注入 `create_app`，
    任何联想请求都会增加计数；同时检查 catalog.json 的 mtime/size 不变。
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="wam-lenovo-get-refresh-")
        self.addCleanup(self.temporary.cleanup)
        self.cache_directory = Path(self.temporary.name) / "cache"
        self.catalog_path = write_catalog_file(self.cache_directory)
        self.before = self.catalog_path.stat()
        self.upstream = FakeUpstream()
        self.client = httpx2.AsyncClient(transport=httpx2.MockTransport(self.upstream.handler))
        self.service = create_lenovo_store_service(
            self.client,
            self.cache_directory,
            make_service_settings(refresh_interval=300),
            cache_enabled=True,
            cache_persist=True,
        )
        asyncio.run(self.service.preload_catalog())
        self.addCleanup(self._close)
        providers = {provider_id: EmptyProvider() for provider_id in PROVIDER_IDS}
        self.api = create_application(
            release_providers=providers,
            settings=make_app_settings(self.cache_directory),
            extra_directory=FIXTURE_EXTRA_DIRECTORY,
            lenovo_service=self.service,
        )

    def _close(self) -> None:
        try:
            asyncio.run(self.service.shutdown())
        finally:
            asyncio.run(self.client.aclose())

    def test_get_refresh_query_is_ignored_on_catalog_and_categories(self):
        with TestClient(self.api) as client:
            plain = client.get("/api/v1/store/lenovo/apps")
            refreshed = client.get("/api/v1/store/lenovo/apps", params={"refresh": "true"})
            categories = client.get("/api/v1/store/lenovo/categories", params={"refresh": "true"})
            time.sleep(0.3)  # 给任何潜在后台刷新任务执行机会（若被误触发会被下面的计数抓到）
            status = self.service.catalog_status()
            after = self.catalog_path.stat()
        self.assertEqual(refreshed.status_code, 200)
        self.assertEqual(refreshed.content, plain.content)
        self.assertEqual(refreshed.json()["total"], 1)
        self.assertEqual(categories.status_code, 200)
        self.assertEqual(status["refreshes"], 0)
        self.assertEqual(status["background_refreshes"], 0)
        self.assertIsNone(status["last_error"])
        self.assertEqual(self.upstream.request_count, 0)
        self.assertEqual(
            (after.st_mtime_ns, after.st_size), (self.before.st_mtime_ns, self.before.st_size)
        )


if __name__ == "__main__":
    unittest.main()
