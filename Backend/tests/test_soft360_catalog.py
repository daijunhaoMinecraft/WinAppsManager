"""360 软件源目录抓取与缓存的测试（MockTransport + 临时目录，禁止真实网络）。

覆盖：

- 24 分类固定清单、分页抓取（``pages_per_category``）、空数组提前结束；
- 按 ``soft_id`` 去重、首个出现的分类作为归属分类、分类计数、logo http→https、
  描述截断、大小/评分解析；
- 分类抓取失败重试一次后整轮失败（不发布半成品）；
- 内存/磁盘快照：``refresh_now`` 写 ``catalog.json``、``preload`` 只读磁盘、``get`` 只读缓存
  （无快照返回 ``None``，绝不联网）、损坏/过旧快照忽略、``clear`` 清理；
- 独立后台刷新线程（``soft360-catalog-refresher``）按小间隔刷新并写盘，``shutdown`` 后退出；
- ``normalize_soft_id`` / ``minimum_windows_from_versions`` 等规范化辅助函数。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

import httpx2

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR.parent))

from Backend.config import BackendSettings, Soft360Config
from Backend.soft360.catalog import (
    CATALOG_FILE_NAME,
    SOFT360_CATEGORIES,
    Soft360CatalogBuilder,
    Soft360CatalogStore,
    app_id_for,
    build_architectures,
    category_specs,
    minimum_windows_from_versions,
    normalize_soft_id,
)
from Backend.soft360.client import Soft360RawClient
from Backend.soft360.errors import (
    Soft360NotFoundError,
    Soft360ProtocolError,
    Soft360UnavailableError,
)
from Backend.soft360.service import Soft360StoreService

# 与 service._prepare_cache_directory 一致：缓存根目录下的 ``soft360`` 子目录。
SOURCE_DIR_NAME = "soft360"


# ---------------------------------------------------------------------------
# 测试基础设施
# ---------------------------------------------------------------------------


class MutableClock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def item(soft_id: str, name: str, **overrides) -> dict:
    data = {
        "soft_id": soft_id,
        "name": name,
        "bname": f"{name} EN",
        "brief": f"{name} 简介",
        "desc": f"{name} 详细描述",
        "soft_size": 1024,
        "soft_score": 88,
        "logo": {
            "32": f"http://p0.qhimg.com/{soft_id}/32.png",
            "48": f"https://p0.qhimg.com/{soft_id}/48.png",
            "72": f"https://p0.qhimg.com/{soft_id}/72.png",
        },
    }
    data.update(overrides)
    return data


class FakeCatalogUpstream:
    """按 (cate_id, page) 返回固定列表的 360 列表接口替身。"""

    def __init__(self, pages: dict[tuple[str, int], list[dict]] | None = None) -> None:
        self.pages = pages or {}
        self.requests: list[dict[str, str]] = []
        self.fail_categories: set[str] = set()
        self.forced_error: Exception | None = None

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        if self.forced_error is not None:
            raise self.forced_error
        params = dict(request.url.params)
        self.requests.append(params)
        if request.url.path.endswith("/soft/getinfo"):
            # 服务层测试只缓存目录；详情接口默认返回空，表示“目录里没有这款软件”。
            return httpx2.Response(200, json={"errno": 0, "data": {}}, request=request)
        cate_id = params.get("cate_id", "")
        if cate_id in self.fail_categories:
            return httpx2.Response(
                200, json={"errno": 10001, "errmsg": "upstream boom", "data": None}, request=request
            )
        page = int(params.get("page", "1"))
        return httpx2.Response(
            200, json={"errno": 0, "data": self.pages.get((cate_id, page), [])}, request=request
        )

    def count(self, cate_id: str | None = None) -> int:
        if cate_id is None:
            return len(self.requests)
        return sum(1 for params in self.requests if params.get("cate_id") == cate_id)


def make_http(upstream: FakeCatalogUpstream) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(upstream.handler))


async def make_builder(
    upstream: FakeCatalogUpstream,
    *,
    pages_per_category: int = 3,
    clock=None,
) -> tuple[httpx2.AsyncClient, Soft360CatalogBuilder]:
    http = make_http(upstream)
    client = Soft360RawClient(http)
    return http, Soft360CatalogBuilder(
        client,
        pages_per_category=pages_per_category,
        clock=clock or MutableClock(),
    )


# ---------------------------------------------------------------------------
# 规范化辅助函数
# ---------------------------------------------------------------------------


class NormalizationTests(unittest.TestCase):
    def test_the_fixed_category_list_has_24_entries(self):
        self.assertEqual(len(SOFT360_CATEGORIES), 24)
        self.assertEqual(len(category_specs()), 24)
        self.assertEqual(category_specs()[0].id, "149")
        self.assertEqual(category_specs()[0].name, "聊天工具")
        self.assertEqual(category_specs()[-1].id, "1900000010")
        self.assertEqual(category_specs()[-1].name, "win7专区")
        self.assertEqual(len({spec.id for spec in category_specs()}), 24)

    def test_app_id_and_soft_id_normalization(self):
        self.assertEqual(app_id_for("1010"), "soft360-1010")
        self.assertEqual(normalize_soft_id("1010"), "1010")
        self.assertEqual(normalize_soft_id(" soft360-1010 "), "1010")
        self.assertEqual(normalize_soft_id("SOFT360-1010"), "1010")
        for value in (None, "", "  ", "bad id", "-1010"):
            with self.subTest(value=value):
                self.assertIsNone(normalize_soft_id(value))
        # 整数会被规范成字符串（与联想源 normalize_soft_id 的宽松行为一致）。
        self.assertEqual(normalize_soft_id(1010), "1010")

    def test_minimum_windows_from_os_versions(self):
        self.assertEqual(minimum_windows_from_versions([5, 6, 7, 8]), "7")
        self.assertEqual(minimum_windows_from_versions([8]), "10")
        self.assertEqual(minimum_windows_from_versions([6, 7]), "8")
        # 低于 7 的编码统一按下限 7 处理。
        self.assertEqual(minimum_windows_from_versions([1, 2, 3, 4]), "7")
        self.assertIsNone(minimum_windows_from_versions([]))
        self.assertIsNone(minimum_windows_from_versions(None))
        self.assertIsNone(minimum_windows_from_versions([99]))
        self.assertEqual(minimum_windows_from_versions(["8"]), "10")

    def test_build_architectures_from_bits(self):
        self.assertEqual(build_architectures([32, 64]), ["x86", "x64"])
        self.assertEqual(build_architectures([64]), ["x64"])
        self.assertEqual(build_architectures([32]), ["x86"])
        self.assertEqual(build_architectures(None), ["x86", "x64"])
        self.assertEqual(build_architectures([]), ["x86", "x64"])


# ---------------------------------------------------------------------------
# builder
# ---------------------------------------------------------------------------


class CatalogBuilderTests(unittest.IsolatedAsyncioTestCase):
    async def test_build_dedupes_by_soft_id_and_keeps_the_first_category(self):
        chat_item = item("1010", "微信")
        video_item = item("2001", "爱奇艺")
        duplicate = item("1010", "微信", brief="重复出现的微信")
        upstream = FakeCatalogUpstream(
            pages={
                ("149", 1): [chat_item],
                ("149", 2): [],
                ("150", 1): [duplicate, video_item],
                ("150", 2): [],
            }
        )
        http, builder = await make_builder(upstream, pages_per_category=3)
        async with http:
            snapshot = await builder.build()
        self.assertEqual(len(snapshot.apps), 2)
        by_soft_id = {app.soft_id: app for app in snapshot.apps}
        self.assertEqual(by_soft_id["1010"].category, "聊天工具")
        self.assertEqual(by_soft_id["1010"].category_id, "149")
        self.assertEqual(by_soft_id["2001"].category, "视频软件")
        self.assertEqual(by_soft_id["1010"].id, "soft360-1010")
        self.assertEqual(by_soft_id["1010"].sort_order, 0)
        self.assertEqual(by_soft_id["1010"].size_bytes, 1024)
        self.assertEqual(by_soft_id["1010"].score, 88)
        self.assertEqual(by_soft_id["1010"].architectures, ["x86", "x64"])
        self.assertEqual(by_soft_id["1010"].minimum_windows, "7")
        self.assertEqual(by_soft_id["1010"].icon_proxy_url, "/api/v1/store/360/apps/1010/icon")
        # 目录列表里的 http logo 统一规范化为 https（优先 72 大图）。
        self.assertEqual(by_soft_id["1010"].icon_url, "https://p0.qhimg.com/1010/72.png")
        self.assertEqual(len(snapshot.categories), 24)
        counts = {category.id: category.count for category in snapshot.categories}
        self.assertEqual(counts["149"], 1)
        self.assertEqual(counts["150"], 1)
        self.assertEqual(counts["157"], 0)
        # 每个分类最多抓 pages_per_category 页；空数组提前结束。
        self.assertEqual(upstream.count("149"), 2)
        self.assertEqual(upstream.count("150"), 2)
        self.assertEqual(upstream.count("157"), 1)
        self.assertEqual(upstream.count(), 24 + 2)

    async def test_pages_per_category_bounds_the_number_of_requests(self):
        pages = {}
        for category_id, _name in SOFT360_CATEGORIES:
            for page in (1, 2):
                pages[(category_id, page)] = [item(f"{category_id}{page}", f"应用{page}")]
        upstream = FakeCatalogUpstream(pages=pages)
        http, builder = await make_builder(upstream, pages_per_category=2)
        async with http:
            snapshot = await builder.build()
        self.assertEqual(len(snapshot.apps), 48)
        self.assertEqual(upstream.count(), 48)
        # sort_order 是分类内的整体位置：第二页第一条紧随第一页，为 1。
        self.assertEqual(snapshot.apps[0].sort_order, 0)
        self.assertEqual(snapshot.apps[1].sort_order, 1)

    async def test_items_without_soft_id_name_or_logo_are_skipped(self):
        upstream = FakeCatalogUpstream(
            pages={
                ("149", 1): [
                    item("1010", "微信"),
                    item("", "缺少 ID"),
                    item("1011", ""),
                    {"soft_id": "1012", "name": "无图标", "logo": {}},
                ]
            }
        )
        http, builder = await make_builder(upstream, pages_per_category=1)
        async with http:
            snapshot = await builder.build()
        self.assertEqual([app.soft_id for app in snapshot.apps], ["1010"])

    async def test_empty_catalog_is_a_protocol_error(self):
        upstream = FakeCatalogUpstream()
        http, builder = await make_builder(upstream, pages_per_category=1)
        async with http:
            with self.assertRaises(Soft360ProtocolError):
                await builder.build()

    async def test_category_failure_is_retried_once_then_fails_the_round(self):
        upstream = FakeCatalogUpstream(pages={("149", 1): [item("1010", "微信")]})
        upstream.fail_categories = {"149"}
        http, builder = await make_builder(upstream, pages_per_category=1)
        async with http:
            with self.assertRaises(Soft360UnavailableError):
                await builder.build()
        self.assertEqual(upstream.count("149"), 2)

    async def test_builder_validates_arguments(self):
        upstream = FakeCatalogUpstream()
        http = make_http(upstream)
        client = Soft360RawClient(http)
        async with http:
            for kwargs in (
                {"pages_per_category": 0},
                {"pages_per_category": 21},
                {"pages_per_category": 1.5},
                {"page_size": 101},
                {"concurrency": 0},
                {"concurrency": 17},
            ):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    Soft360CatalogBuilder(client, **kwargs)
            with self.assertRaises(ValueError):
                Soft360CatalogBuilder(object())


# ---------------------------------------------------------------------------
# store（内存 + 磁盘快照；请求只读）
# ---------------------------------------------------------------------------


def make_store(
    upstream: FakeCatalogUpstream,
    http: httpx2.AsyncClient,
    directory: Path,
    clock: MutableClock,
    *,
    cache_persist: bool = True,
    ttl_seconds: float = 21600.0,
    max_stale_seconds: float = 604800.0,
) -> Soft360CatalogStore:
    client = Soft360RawClient(http)
    builder = Soft360CatalogBuilder(client, pages_per_category=1, clock=clock)
    return Soft360CatalogStore(
        builder,
        directory,
        ttl_seconds=ttl_seconds,
        max_stale_seconds=max_stale_seconds,
        cache_persist=cache_persist,
        refresh_interval_seconds=1800.0,
        clock=clock,
    )


class CatalogStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_now_writes_the_disk_snapshot_and_preload_reads_it(self):
        upstream = FakeCatalogUpstream(pages={("149", 1): [item("1010", "微信")]})
        clock = MutableClock()
        with tempfile.TemporaryDirectory() as directory:
            cache_directory = Path(directory)
            http = make_http(upstream)
            async with http:
                store = make_store(upstream, http, cache_directory, clock)
                snapshot = await store.refresh_now()
                self.assertEqual(len(snapshot.apps), 1)
                path = cache_directory / CATALOG_FILE_NAME
                self.assertTrue(path.is_file())
                raw = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(raw["schema_version"], 1)
                self.assertEqual(raw["apps"][0]["soft_id"], "1010")

                # 新 store 的 preload 只读磁盘，不发请求。
                requests_before = upstream.count()
                second = make_store(upstream, http, cache_directory, clock)
                preloaded = await second.preload()
                self.assertIsNotNone(preloaded)
                self.assertEqual(len(preloaded.apps), 1)
                self.assertEqual(upstream.count(), requests_before)
                # get() 也只读缓存。
                fetched = await second.get()
                self.assertIsNotNone(fetched)
                self.assertEqual(fetched.stale, False)
                self.assertEqual(upstream.count(), requests_before)

    async def test_get_without_any_snapshot_returns_none_and_never_fetches(self):
        upstream = FakeCatalogUpstream()
        clock = MutableClock()
        with tempfile.TemporaryDirectory() as directory:
            http = make_http(upstream)
            async with http:
                store = make_store(upstream, http, Path(directory), clock)
                self.assertIsNone(await store.get())
                self.assertIsNone(await store.preload())
        self.assertEqual(upstream.count(), 0)

    async def test_stale_flag_follows_the_catalog_ttl(self):
        upstream = FakeCatalogUpstream(pages={("149", 1): [item("1010", "微信")]})
        clock = MutableClock()
        with tempfile.TemporaryDirectory() as directory:
            http = make_http(upstream)
            async with http:
                store = make_store(
                    upstream, http, Path(directory), clock, ttl_seconds=100.0
                )
                await store.refresh_now()
                self.assertFalse((await store.get()).stale)
                clock.now += 101.0
                self.assertTrue((await store.get()).stale)

    async def test_expired_or_corrupt_disk_snapshot_is_ignored_and_removed(self):
        upstream = FakeCatalogUpstream(pages={("149", 1): [item("1010", "微信")]})
        clock = MutableClock()
        with tempfile.TemporaryDirectory() as directory:
            cache_directory = Path(directory)
            http = make_http(upstream)
            async with http:
                store = make_store(upstream, http, cache_directory, clock)
                await store.refresh_now()
                path = cache_directory / CATALOG_FILE_NAME
                # 超过 max_stale_seconds 的快照不可用，且会被删除。
                clock.now += 604801.0
                second = make_store(upstream, http, cache_directory, clock)
                self.assertIsNone(await second.get())
                self.assertFalse(path.exists())

                # 损坏 JSON 同样忽略。
                path.write_text("{not json", encoding="utf-8")
                third = make_store(upstream, http, cache_directory, clock)
                self.assertIsNone(await third.get())
                self.assertFalse(path.exists())

    async def test_clear_removes_memory_and_disk_state(self):
        upstream = FakeCatalogUpstream(pages={("149", 1): [item("1010", "微信")]})
        clock = MutableClock()
        with tempfile.TemporaryDirectory() as directory:
            cache_directory = Path(directory)
            http = make_http(upstream)
            async with http:
                store = make_store(upstream, http, cache_directory, clock)
                await store.refresh_now()
                result = await store.clear()
                self.assertTrue(result["cleared"])
                self.assertEqual(result["apps"], 1)
                self.assertTrue(result["disk_cleared"])
                self.assertFalse((cache_directory / CATALOG_FILE_NAME).exists())
                self.assertIsNone(store.snapshot)
                self.assertIsNone(await store.get())

    async def test_catalog_status_reports_snapshot_age_and_counters(self):
        upstream = FakeCatalogUpstream(pages={("149", 1): [item("1010", "微信")]})
        clock = MutableClock()
        with tempfile.TemporaryDirectory() as directory:
            http = make_http(upstream)
            async with http:
                store = make_store(upstream, http, Path(directory), clock)
                status = store.catalog_status()
                self.assertIsNone(status["apps"])
                self.assertFalse(status["refresher_running"])
                await store.refresh_now()
                clock.now += 10.0
                status = store.catalog_status()
                self.assertEqual(status["apps"], 1)
                self.assertEqual(status["categories"], 24)
                self.assertEqual(status["age_seconds"], 10.0)
                self.assertEqual(status["refreshes"], 1)
                self.assertEqual(status["last_error"], None)
                self.assertEqual(status["refresh_interval_seconds"], 1800.0)

    async def test_failed_refresh_keeps_the_previous_snapshot(self):
        upstream = FakeCatalogUpstream(pages={("149", 1): [item("1010", "微信")]})
        clock = MutableClock()
        with tempfile.TemporaryDirectory() as directory:
            http = make_http(upstream)
            async with http:
                store = make_store(upstream, http, Path(directory), clock)
                await store.refresh_now()
                upstream.fail_categories = {"149"}
                with self.assertRaises(Soft360UnavailableError):
                    await store.refresh_now()
                self.assertEqual(len(store.snapshot.apps), 1)
                self.assertIsNotNone(store.catalog_status()["last_error"])


# ---------------------------------------------------------------------------
# 后台刷新线程
# ---------------------------------------------------------------------------


class CatalogRefresherThreadTests(unittest.IsolatedAsyncioTestCase):
    async def test_background_thread_refreshes_and_writes_the_snapshot(self):
        upstream = FakeCatalogUpstream(pages={("149", 1): [item("1010", "微信")]})
        settings = BackendSettings(
            soft360=Soft360Config(
                catalog_refresh_interval_seconds=0.05,
                catalog_pages_per_category=1,
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            cache_directory = Path(directory)
            http = make_http(upstream)
            async with http:
                service = Soft360StoreService(
                    http, cache_directory, settings=settings, cache_persist=True
                )
                self.assertTrue(await service.start_catalog_refresher())
                self.assertTrue(service.catalog_status()["refresher_running"])
                deadline = time.monotonic() + 3.0
                catalog_path = cache_directory / SOURCE_DIR_NAME / CATALOG_FILE_NAME
                while not catalog_path.is_file() and time.monotonic() < deadline:
                    await asyncio.sleep(0.02)
                self.assertEqual(service.cached_app_count(), 1)
                self.assertTrue(catalog_path.is_file())
                await service.shutdown()
                self.assertFalse(service.catalog_status()["refresher_running"])
                self.assertIsNone(service.catalog_refresher_thread)

    async def test_start_refresher_with_zero_interval_is_disabled(self):
        settings = BackendSettings(
            soft360=Soft360Config(catalog_refresh_interval_seconds=0)
        )
        with tempfile.TemporaryDirectory() as directory:
            http = make_http(FakeCatalogUpstream())
            async with http:
                service = Soft360StoreService(
                    http, directory, settings=settings, cache_persist=False
                )
                try:
                    self.assertFalse(await service.start_catalog_refresher())
                    self.assertFalse(service.catalog_status()["refresher_running"])
                finally:
                    await service.shutdown()


# 与 service._prepare_cache_directory 一致：缓存根目录下的 ``soft360`` 子目录。
SOURCE_DIR_NAME = "soft360"


# ---------------------------------------------------------------------------
# 服务层：本地搜索 / 目录外软件 404 / 关闭后的错误
# ---------------------------------------------------------------------------


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_matches_the_cached_catalog_without_network(self):
        pages = {
            ("149", 1): [
                item("1010", "微信", brief="国民级聊天工具"),
                item("2001", "微信视频号", brief="短视频"),
                item("3001", "QQ", brief="聊天工具"),
            ]
        }
        upstream = FakeCatalogUpstream(pages=pages)
        settings = BackendSettings(
            soft360=Soft360Config(
                catalog_pages_per_category=1, catalog_refresh_interval_seconds=0
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            http = make_http(upstream)
            async with http:
                service = Soft360StoreService(http, directory, settings=settings, cache_persist=False)
                try:
                    await service.refresh_catalog()
                    requests_before = upstream.count()
                    result = await service.search("微信", limit=10)
                    self.assertEqual([app.name for app in result.items], ["微信", "微信视频号"])
                    self.assertEqual(result.total, 2)
                    category = await service.search("聊天工具", limit=10)
                    # 目录内所有条目都属于“聊天工具”，按分类匹配时全部命中。
                    self.assertEqual(
                        {app.name for app in category.items}, {"微信", "微信视频号", "QQ"}
                    )
                    limited = await service.search("微信", limit=1)
                    self.assertEqual(len(limited.items), 1)
                    empty = await service.search("   ")
                    self.assertEqual(empty.total, 0)
                    # 本地搜索只读缓存，不产生任何 360 请求。
                    self.assertEqual(upstream.count(), requests_before)
                finally:
                    await service.shutdown()

    async def test_details_for_an_app_outside_the_catalog_are_not_found(self):
        upstream = FakeCatalogUpstream(pages={("149", 1): [item("1010", "微信")]})
        settings = BackendSettings(
            soft360=Soft360Config(
                catalog_pages_per_category=1, catalog_refresh_interval_seconds=0
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            http = make_http(upstream)
            async with http:
                service = Soft360StoreService(http, directory, settings=settings, cache_persist=False)
                try:
                    await service.refresh_catalog()
                    with self.assertRaises(Soft360NotFoundError):
                        await service.get_details("9999")
                    with self.assertRaises(Soft360NotFoundError):
                        await service.get_details("not a valid id")
                    with self.assertRaises(Soft360NotFoundError):
                        await service.get_screenshot("1010", 0)
                finally:
                    await service.shutdown()

    async def test_closed_service_rejects_further_reads(self):
        settings = BackendSettings(soft360=Soft360Config(catalog_refresh_interval_seconds=0))
        with tempfile.TemporaryDirectory() as directory:
            http = make_http(FakeCatalogUpstream())
            async with http:
                service = Soft360StoreService(http, directory, settings=settings, cache_persist=False)
                await service.shutdown()
                with self.assertRaises(Soft360UnavailableError):
                    await service.get_catalog()
                with self.assertRaises(Soft360UnavailableError):
                    await service.refresh_catalog()


if __name__ == "__main__":
    unittest.main()
