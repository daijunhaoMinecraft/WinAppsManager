"""联想目录 builder / store / 服务门面测试（MockTransport + 临时目录，无真实网络）。"""

from __future__ import annotations

import asyncio
import base64
from datetime import timezone
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

import httpx2

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR.parent))

from Backend.lenovo.catalog import (
    LenovoCatalogBuilder,
    LenovoCatalogStore,
    app_id_for,
    build_architectures,
    format_platforms,
    icon_background_for,
    icon_text_for,
    minimum_windows_from_platforms,
    parse_size_bytes,
)
from Backend.lenovo.client import LenovoRawClient
from Backend.lenovo.errors import LenovoNotFoundError, LenovoProtocolError, LenovoUnavailableError
from Backend.lenovo.models import StoreAppDetails, StoreAppSummary
from Backend.lenovo.service import create_lenovo_store_service


# ---------------------------------------------------------------------------
# 测试基础设施
# ---------------------------------------------------------------------------


class MutableClock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class TickingClock:
    """每次读取前进一小步的时钟：用于验证并发 refresh_now 的“合并成一轮”逻辑。"""

    def __init__(self, now: float = 1_700_000_000.0, step: float = 0.001) -> None:
        self.now = now
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def make_jwt(exp: int) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"typ": "JWT", "alg": "HS256"}).encode()).rstrip(b"=")
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=")
    return f"{header.decode()}.{payload.decode()}.signature"


def app_item(
    soft_id: str,
    name: str,
    *,
    class_id: str | None = "1",
    size: str = "318.95 MB",
    osbit: tuple[str, ...] = ("64",),
    platforms: tuple[str, ...] = ("win10", "win11"),
    score: str = "60",
    downloads: str = "277万次",
    stable: str = "stable",
    logo: str | None = None,
    introduction: str | None = None,
) -> dict:
    return {
        "softID": soft_id,
        "softName": name,
        "version": "1.2.3",
        "installFileSize": size,
        "introduction": introduction if introduction is not None else f"{name} 简介",
        # logo=None 用默认绝对地址；显式传 "" / 相对路径用于覆盖“无有效 logo”分支。
        "logoFile": (
            logo if logo is not None else f"https://lsw-fast.lenovo.com.cn/logo/{soft_id}.png"
        ),
        "captureFileList": [],
        "score": score,
        "downloadCount": downloads,
        "supportOsbit": list(osbit),
        "supportPlatform": list(platforms),
        "stable": stable,
        "classID": class_id,
    }


DEFAULT_TREE = [
    {
        "code": "soft",
        "name": "软件",
        "categories": [
            {
                "type": "pc",
                "name": "PC",
                "appClassList": [
                    {"classId": "-1", "className": "全部", "tags": None},
                    {"classId": "1", "className": "聊天社交", "tags": None},
                    {"classId": "2", "className": "输入法", "tags": None},
                ],
            },
            {
                "type": "web",
                "name": "网页",
                "appClassList": [{"classId": "9", "className": "网页分类", "tags": None}],
            },
        ],
    },
    {
        "code": "game",
        "name": "游戏",
        "categories": [
            {
                "type": "pc",
                "name": "PC",
                "appClassList": [
                    {"classId": "-1", "className": "全部", "tags": None},
                    {"classId": "19", "className": "角色扮演", "tags": None},
                ],
            }
        ],
    },
]


def default_classes() -> dict[tuple[str, str], list[dict]]:
    return {
        ("soft", "1"): [
            app_item("11488", "微信", class_id="1"),
            app_item("L105867", "QQ", class_id="2", size="525KB", logo="https://pc1.gtimg.com/qq.png"),
            app_item("C100", "无主分类", class_id=None),
        ],
        ("soft", "2"): [
            app_item("L105867", "QQ", class_id="2", size="525KB", logo="https://pc1.gtimg.com/qq.png"),
            app_item("D200", "输入法X", class_id="2", downloads="1,000.7 万次"),
        ],
        ("game", "19"): [
            app_item(
                "G300",
                "游戏Y",
                class_id="19",
                size="26.91 GB",
                osbit=("32", "64"),
                platforms=("win7",),
                stable="beta",
                downloads="5万次",
            )
        ],
    }


DETAILS_PAYLOAD = {
    "softID": "L105867",
    "softName": "QQ",
    "version": "9.9.37.54395",
    "detailInfo": "详细描述文本",
    "whatNew": "更新说明文本",
    "captureFileList": [
        {"type": "0", "url": "https://lsw-fast.lenovo.com.cn/img/1.png", "videoImg": None},
        {
            "type": "1",
            "url": "https://pc-store-video.lenovomm.cn/v.mp4?auth_key=secret",
            "videoImg": "https://imgs.lenovomm.cn/thumb.jpg",
        },
    ],
    "cover": {"coverImg": "https://imgs.lenovomm.cn/cover.jpg"},
    "appLabelList": [{"labelName": "AI对话"}, {"labelName": "办公辅助工具"}],
    "classRankingDesc": {"targetDesc": "聊天社交类下载榜第2名"},
    "detailsTag": [{"name": "安全认证", "tips": "软件通过检测"}],
}

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 32


class CatalogServer:
    """假的联想接口：分类树 + 按分类分页的应用列表 + 详情/搜索/图片。"""

    def __init__(
        self,
        clock: MutableClock,
        classes: dict[tuple[str, str], list[dict]],
        *,
        tree: list[dict] | None = None,
    ) -> None:
        self.clock = clock
        self.classes = classes
        self.tree = tree if tree is not None else DEFAULT_TREE
        self.requests: list[httpx2.Request] = []
        self.class_requests: list[tuple[str, str, int]] = []
        self.fail_counts: dict[tuple[str, str], int] = {}
        self.register_count = 0
        self.details_payload = DETAILS_PAYLOAD
        self.search_payload = {
            "status": 0,
            "message": None,
            "data": {
                "dataList": [
                    {**app_item("L105867", "QQ", class_id="2"), "itemType": 1, "className": "输入法"},
                    {**app_item("99999", "广告应用"), "itemType": 1, "isAdvert": "4"},
                    # 无 logo 或 logo 不是绝对地址的搜索项无法直连 CDN，必须被过滤。
                    {**app_item("77777", "无图标应用"), "itemType": 1, "logoFile": ""},
                    {**app_item("88888", "相对图标应用"), "itemType": 1, "logoFile": "/relative/logo.png"},
                    {"itemType": 2, "softName": "非应用"},
                ]
            },
        }
        self.image_requests: list[httpx2.Request] = []

    def _json(self, request: httpx2.Request, payload: dict, status: int = 200) -> httpx2.Response:
        return httpx2.Response(status, json=payload, request=request)

    def _ok(self, request: httpx2.Request, data) -> httpx2.Response:
        return self._json(request, {"status": 0, "message": None, "data": data})

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/appbase/token/register":
            self.register_count += 1
            # 服务门面用真实时钟创建 token 管理器（不注入 clock），因此 token 的 exp 也按真实时间给。
            return self._ok(request, make_jwt(int(time.time()) + 15 * 24 * 3600))
        if path == "/classifyservice/class/composite_class_list":
            return self._ok(request, self.tree)
        if path == "/classifyservice/class/composite_class_app_list":
            params = request.url.params
            code, class_id = params.get("code"), params.get("id")
            skip, limit = int(params.get("skip", "0")), int(params.get("limit", "1000"))
            self.class_requests.append((code, class_id, skip))
            remaining = self.fail_counts.get((code, class_id), 0)
            if remaining > 0:
                self.fail_counts[(code, class_id)] = remaining - 1
                return self._json(request, {"status": 500, "message": "上游错误", "data": None})
            apps = self.classes.get((code, class_id), [])
            return self._ok(
                request,
                {"count": len(apps), "apps": apps[skip : skip + limit], "code": "ranklist"},
            )
        if path == "/appstorecontents/app/details_new":
            return self._json(request, {"status": 0, "message": None, "data": self.details_payload})
        if path == "/dlservice/getPcSoftDownloadUrlList":
            return self._ok(request, {"downloadUrls": []})
        if path == "/searchcontent/fusion/v2/app_search":
            return self._json(request, self.search_payload)
        # 图片代理（任意白名单域名路径都返回一张 png）。
        self.image_requests.append(request)
        return httpx2.Response(
            200,
            content=PNG_BYTES,
            headers={"content-type": "image/png"},
            request=request,
        )

    def reset_counters(self) -> None:
        self.requests.clear()
        self.class_requests.clear()

    def class_fetch_count(self, code: str, class_id: str) -> int:
        return sum(1 for request_code, request_class, _ in self.class_requests if request_code == code and request_class == class_id)


class LenovoCatalogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.clock = MutableClock()
        self.temporary = tempfile.TemporaryDirectory(prefix="wam-lenovo-catalog-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "lenovo"
        self.server = CatalogServer(self.clock, default_classes())
        self.client = httpx2.AsyncClient(transport=httpx2.MockTransport(self.server.handler))
        self.addAsyncCleanup(self.client.aclose)

    def make_builder(self, *, concurrency: int = 3) -> LenovoCatalogBuilder:
        raw = LenovoRawClient(self.client, clock=self.clock)
        return LenovoCatalogBuilder(raw, concurrency=concurrency, clock=self.clock)

    def make_store(self, *, builder: LenovoCatalogBuilder | None = None, **kwargs) -> LenovoCatalogStore:
        return LenovoCatalogStore(
            builder if builder is not None else self.make_builder(),
            self.directory,
            clock=self.clock,
            **kwargs,
        )

    def make_offline_store(self, **kwargs) -> LenovoCatalogStore:
        """新实例 + 会抛错的传输层：任何网络请求都会让测试失败（验证只读磁盘缓存）。"""

        def exploding_handler(request: httpx2.Request) -> httpx2.Response:
            raise AssertionError(f"请求路径不应访问网络: {request.url}")

        client = httpx2.AsyncClient(transport=httpx2.MockTransport(exploding_handler))
        self.addAsyncCleanup(client.aclose)
        raw = LenovoRawClient(client, clock=self.clock)
        return LenovoCatalogStore(
            LenovoCatalogBuilder(raw, clock=self.clock), self.directory, clock=self.clock, **kwargs
        )

    async def test_catalog_is_a_deduped_union_of_pc_classes(self):
        snapshot = await self.make_builder().build()
        apps = {app.soft_id: app for app in snapshot.apps}
        self.assertEqual(set(apps), {"11488", "L105867", "C100", "D200", "G300"})
        self.assertEqual(len(snapshot.apps), 5)
        # -1 “全部”与 type=web 分类都不抓取。
        self.assertEqual(self.server.class_fetch_count("soft", "-1"), 0)
        self.assertEqual(self.server.class_fetch_count("soft", "9"), 0)

        # 归属：优先使用列表项 classID 命中的分类（QQ 首次出现在“聊天社交”，classID=2）。
        qq = apps["L105867"]
        self.assertEqual(qq.category, "输入法")
        self.assertEqual(qq.category_id, "2")
        self.assertEqual(qq.tags, ["输入法"])
        # classID 为 null 时回退到实际抓取到的第一个分类。
        fallback = apps["C100"]
        self.assertEqual(fallback.category, "聊天社交")

        wechat = apps["11488"]
        self.assertEqual(wechat.id, "lenovo-11488")
        self.assertEqual(wechat.group, "software")
        self.assertEqual(wechat.publisher, None)
        self.assertEqual(wechat.license, "免费")
        self.assertTrue(wechat.is_free)
        self.assertFalse(wechat.is_beta)
        self.assertTrue(wechat.is_recommended)
        self.assertEqual(wechat.sort_order, 0)
        # 客户端列表直连联想 CDN：icon_url 是原始 logo 绝对地址，代理路径只作兜底。
        self.assertEqual(wechat.icon_url, "https://lsw-fast.lenovo.com.cn/logo/11488.png")
        self.assertEqual(wechat.icon_proxy_url, "/api/v1/store/lenovo/apps/11488/icon")
        self.assertEqual(wechat.installation_notes, "安装包由联想应用商店提供，下载完成后按安装向导操作。")
        self.assertEqual(wechat.size_bytes, int(round(318.95 * 1024**2)))

        game = apps["G300"]
        self.assertEqual(game.group, "game")
        self.assertEqual(game.category, "角色扮演")
        self.assertEqual(game.architectures, ["x86", "x64"])
        self.assertEqual(game.minimum_windows, "7")
        self.assertTrue(game.is_beta)
        self.assertEqual(game.icon_url, "https://lsw-fast.lenovo.com.cn/logo/G300.png")
        self.assertEqual(game.icon_proxy_url, "/api/v1/store/lenovo/apps/G300/icon")

        # 分类树按 soft/game 顺序给出，count 取上游 total；web 分类不出现。
        self.assertEqual([group.code for group in snapshot.groups], ["software", "game"])
        self.assertEqual([category.id for category in snapshot.groups[0].categories], ["1", "2"])
        self.assertEqual([category.count for category in snapshot.groups[0].categories], [3, 2])
        self.assertEqual([category.id for category in snapshot.groups[1].categories], ["19"])

        # 原始 logo URL 同时保留在快照索引里，供后端 icon 代理兜底。
        self.assertEqual(snapshot.logo_urls["L105867"], "https://pc1.gtimg.com/qq.png")

    async def test_entries_without_an_absolute_logo_are_skipped_with_a_warning(self):
        self.server.classes = {
            ("soft", "1"): [
                app_item("11488", "微信", logo=""),
                app_item("L105867", "QQ", logo="/relative/logo.png"),
                app_item("C100", "正常应用", logo="https://lsw-fast.lenovo.com.cn/logo/c100.png"),
            ]
        }
        self.server.tree = [
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
        with self.assertLogs("winappsmanager.lenovo", level="WARNING") as captured:
            snapshot = await self.make_builder().build()
        self.assertEqual([app.soft_id for app in snapshot.apps], ["C100"])
        self.assertEqual(snapshot.apps[0].icon_url, "https://lsw-fast.lenovo.com.cn/logo/c100.png")
        self.assertIn("logoFile", "\n".join(captured.output))

    async def test_a_catalog_with_only_invalid_logos_is_a_protocol_error(self):
        self.server.classes = {
            ("soft", "1"): [
                app_item("11488", "微信", logo=""),
                app_item("L105867", "QQ", logo="not-a-url"),
            ]
        }
        self.server.tree = [
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
        with self.assertLogs("winappsmanager.lenovo", level="WARNING"):
            with self.assertRaises(LenovoProtocolError):
                await self.make_builder().build()

    async def test_page_size_and_pagination(self):
        many = [app_item(f"P{index:04d}", f"应用{index}", class_id="5") for index in range(1500)]
        self.server.classes = {("soft", "5"): many}
        self.server.tree = [
            {
                "code": "soft",
                "name": "软件",
                "categories": [
                    {
                        "type": "pc",
                        "name": "PC",
                        "appClassList": [{"classId": "5", "className": "批量分类", "tags": None}],
                    }
                ],
            }
        ]
        snapshot = await self.make_builder().build()
        self.assertEqual(len(snapshot.apps), 1500)
        self.assertEqual([skip for _, _, skip in self.server.class_requests], [0, 1000])

    async def test_single_class_hard_limit(self):
        many = [app_item(f"Q{index:04d}", f"应用{index}", class_id="6") for index in range(6000)]
        self.server.classes = {("soft", "6"): many}
        self.server.tree = [
            {
                "code": "soft",
                "name": "软件",
                "categories": [
                    {
                        "type": "pc",
                        "name": "PC",
                        "appClassList": [{"classId": "6", "className": "超大分类", "tags": None}],
                    }
                ],
            }
        ]
        snapshot = await self.make_builder().build()
        self.assertEqual(len(snapshot.apps), 5000)
        self.assertEqual([skip for _, _, skip in self.server.class_requests], [0, 1000, 2000, 3000, 4000])

    async def test_class_failure_is_retried_once(self):
        self.server.fail_counts[("soft", "1")] = 1
        snapshot = await self.make_builder().build()
        self.assertEqual(len(snapshot.apps), 5)
        self.assertEqual(self.server.class_fetch_count("soft", "1"), 2)

    async def test_class_failure_after_retry_fails_the_whole_build(self):
        self.server.fail_counts[("soft", "1")] = 2
        with self.assertRaises(LenovoUnavailableError):
            await self.make_builder().build()
        self.assertEqual(self.server.class_fetch_count("soft", "1"), 2)

    async def test_catalog_rejects_an_empty_class_tree(self):
        self.server.tree = []
        with self.assertRaises(LenovoProtocolError):
            await self.make_builder().build()
        self.server.tree = [
            {
                "code": "soft",
                "name": "软件",
                "categories": [{"type": "web", "name": "网页", "appClassList": []}],
            }
        ]
        with self.assertRaises(LenovoProtocolError):
            await self.make_builder().build()

    async def test_recommended_flag_covers_the_first_ten(self):
        apps = [app_item(f"R{index:03d}", f"应用{index}", class_id="7") for index in range(12)]
        self.server.classes = {("soft", "7"): apps}
        self.server.tree = [
            {
                "code": "soft",
                "name": "软件",
                "categories": [
                    {
                        "type": "pc",
                        "name": "PC",
                        "appClassList": [{"classId": "7", "className": "推荐分类", "tags": None}],
                    }
                ],
            }
        ]
        snapshot = await self.make_builder().build()
        self.assertEqual([app.is_recommended for app in snapshot.apps], [True] * 10 + [False, False])
        self.assertEqual([app.sort_order for app in snapshot.apps], list(range(12)))

    async def test_builder_concurrency_is_bounded(self):
        active = 0
        peak = 0

        async def handler(request: httpx2.Request) -> httpx2.Response:
            nonlocal active, peak
            if request.url.path == "/classifyservice/class/composite_class_app_list":
                active += 1
                peak = max(peak, active)
                try:
                    await asyncio.sleep(0.01)
                    return self.server.handler(request)
                finally:
                    active -= 1
            return self.server.handler(request)

        client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        raw = LenovoRawClient(client, clock=self.clock)
        builder = LenovoCatalogBuilder(raw, concurrency=2, clock=self.clock)
        await builder.build()
        self.assertEqual(peak, 2)

    async def test_disk_snapshot_roundtrip(self):
        first_store = self.make_store()
        snapshot = await first_store.get()
        self.assertEqual(len(snapshot.apps), 5)
        catalog_file = self.directory / "catalog.json"
        self.assertTrue(catalog_file.is_file())
        document = json.loads(catalog_file.read_text(encoding="utf-8"))
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(len(document["apps"]), 5)
        self.assertIn("groups", document)

        # 新实例、不可用的传输层：只从磁盘快照读取，不发任何请求。
        def exploding_handler(request: httpx2.Request) -> httpx2.Response:
            raise AssertionError(f"不应访问网络: {request.url}")

        client = httpx2.AsyncClient(transport=httpx2.MockTransport(exploding_handler))
        self.addAsyncCleanup(client.aclose)
        raw = LenovoRawClient(client, clock=self.clock)
        restarted = LenovoCatalogStore(LenovoCatalogBuilder(raw, clock=self.clock), self.directory, clock=self.clock)
        loaded = await restarted.get()
        self.assertEqual([app.soft_id for app in loaded.apps], [app.soft_id for app in snapshot.apps])
        self.assertFalse(loaded.stale)
        self.assertEqual(restarted.find("L105867").name, "QQ")
        self.assertEqual(restarted.logo_source("L105867"), "https://pc1.gtimg.com/qq.png")

    async def test_corrupt_disk_snapshot_is_ignored_and_rebuilt(self):
        self.directory.mkdir(parents=True)
        (self.directory / "catalog.json").write_text("{not json", encoding="utf-8")
        store = self.make_store()
        snapshot = await store.get()
        self.assertEqual(len(snapshot.apps), 5)
        self.assertGreater(len(self.server.class_requests), 0)
        self.assertTrue((self.directory / "catalog.json").is_file())

    async def test_expired_memory_snapshot_is_served_stale_without_refetching(self):
        store = self.make_store(ttl_seconds=60.0, max_stale_seconds=7 * 24 * 3600.0)
        await store.get()
        baseline = len(self.server.class_requests)
        self.clock.now += 61

        # 请求路径只读缓存：陈旧只体现在 stale 标记上，绝不调度后台刷新，更不联网。
        stale = await store.get()
        self.assertTrue(stale.stale)
        self.assertEqual(len(stale.apps), 5)
        self.assertEqual(len(self.server.class_requests), baseline)
        await asyncio.sleep(0)
        self.assertEqual(len(self.server.class_requests), baseline)
        self.assertEqual(store.counters["background_refreshes"], 0)
        self.assertGreaterEqual(store.counters["stale_served"], 1)
        self.assertEqual(store.catalog_status()["age_seconds"], 61.0)

    async def test_force_refresh_returns_the_cache_then_refreshes_in_the_background(self):
        store = self.make_store(ttl_seconds=60.0)
        await store.get()
        baseline = len(self.server.class_requests)
        self.clock.now += 61

        # force_refresh 有快照时立即返回当前快照（stale 由 TTL 决定），不等待联网。
        snapshot = await store.get(force_refresh=True)
        self.assertTrue(snapshot.stale)
        self.assertEqual(len(snapshot.apps), 5)
        self.assertEqual(len(self.server.class_requests), baseline)

        await store.wait_for_pending_refresh()
        self.assertGreater(len(self.server.class_requests), baseline)
        fresh = await store.get()
        self.assertFalse(fresh.stale)
        self.assertEqual(store.counters["background_refreshes"], 1)

    async def test_concurrent_force_refreshes_start_a_single_background_refresh(self):
        store = self.make_store()
        await store.get()
        baseline = len(self.server.class_requests)
        first, second = await asyncio.gather(
            store.get(force_refresh=True), store.get(force_refresh=True)
        )
        self.assertFalse(first.stale)
        self.assertFalse(second.stale)
        await store.wait_for_pending_refresh()
        # 一轮后台刷新恰好抓取全部 3 个分类（单飞：两个并发请求只排一个任务）。
        self.assertEqual(len(self.server.class_requests) - baseline, 3)
        self.assertEqual(store.counters["background_refreshes"], 1)

    async def test_force_refresh_failure_keeps_the_previous_snapshot(self):
        store = self.make_store()
        original = await store.get()
        self.server.fail_counts[("soft", "1")] = 100

        # 有快照时 force_refresh 立即返回旧快照并触发后台刷新；后台失败只记 WARNING。
        snapshot = await store.get(force_refresh=True)
        self.assertEqual([app.soft_id for app in snapshot.apps], [app.soft_id for app in original.apps])
        await store.wait_for_pending_refresh()
        kept = await store.get()
        self.assertEqual([app.soft_id for app in kept.apps], [app.soft_id for app in original.apps])
        self.assertFalse(kept.stale)
        self.assertEqual(store.counters["background_refreshes"], 0)
        self.assertIsNotNone(store.catalog_status()["last_error"])

        # 显式同步刷新失败会抛错，同样保留旧快照。
        with self.assertRaises(LenovoUnavailableError):
            await store.refresh_now()
        still_kept = await store.get()
        self.assertEqual([app.soft_id for app in still_kept.apps], [app.soft_id for app in original.apps])

    async def test_force_refresh_without_snapshot_surfaces_the_error(self):
        store = self.make_store()
        self.server.fail_counts[("soft", "1")] = 2
        with self.assertRaises(LenovoUnavailableError):
            await store.get(force_refresh=True)
        self.assertIsNone(store.snapshot)
        self.server.fail_counts[("soft", "1")] = 2
        with self.assertRaises(LenovoUnavailableError):
            await store.get()

    async def test_expired_snapshot_beyond_max_stale_is_discarded(self):
        first = self.make_store(ttl_seconds=60.0, max_stale_seconds=120.0)
        await first.get()
        baseline = len(self.server.class_requests)
        self.clock.now += 1000
        restarted = self.make_store(ttl_seconds=60.0, max_stale_seconds=120.0)
        snapshot = await restarted.get()
        self.assertFalse(snapshot.stale)
        self.assertEqual(len(snapshot.apps), 5)
        self.assertGreater(len(self.server.class_requests), baseline)

    async def test_cache_disabled_rebuilds_every_time_without_disk(self):
        store = self.make_store(cache_enabled=False)
        await store.get()
        await store.get()
        self.assertEqual(len(self.server.class_requests), 6)
        self.assertFalse((self.directory / "catalog.json").exists())
        self.assertIsNone(store.cached_app_count())

    async def test_cache_persist_disabled_keeps_only_memory(self):
        store = self.make_store(cache_persist=False)
        await store.get()
        await store.get()
        self.assertEqual(len(self.server.class_requests), 3)
        self.assertFalse((self.directory / "catalog.json").exists())
        self.assertEqual(store.cached_app_count(), 5)

    async def test_preload_loads_the_disk_snapshot_without_network(self):
        snapshot = await self.make_store().get()
        expected = [(app.soft_id, app.name) for app in snapshot.apps]

        restarted = self.make_offline_store()
        with self.assertLogs("winappsmanager.lenovo", level="INFO") as captured:
            loaded = await restarted.preload()
        self.assertIsNotNone(loaded)
        self.assertEqual([(app.soft_id, app.name) for app in loaded.apps], expected)
        self.assertFalse(loaded.stale)
        self.assertEqual(restarted.cached_app_count(), 5)
        self.assertIn("预加载", "\n".join(captured.output))
        # 预加载后请求路径直接命中内存快照，绝不再访问网络。
        for _ in range(3):
            served = await restarted.get()
            self.assertEqual(len(served.apps), 5)

    async def test_request_path_loads_the_disk_snapshot_on_first_read(self):
        await self.make_store().get()
        restarted = self.make_offline_store()
        loaded = await restarted.get()
        self.assertEqual(len(loaded.apps), 5)
        self.assertFalse(loaded.stale)
        self.assertEqual(restarted.cached_app_count(), 5)
        self.assertEqual(restarted.find("L105867").name, "QQ")

    async def test_cold_start_request_fetches_once_and_writes_the_disk_snapshot(self):
        store = self.make_store()
        snapshot = await store.get()
        self.assertEqual(len(snapshot.apps), 5)
        self.assertFalse(snapshot.stale)
        self.assertEqual(len(self.server.class_requests), 3)
        self.assertTrue((self.directory / "catalog.json").is_file())
        # 之后的请求只读内存快照。
        await store.get()
        await store.get(force_refresh=True)
        await store.wait_for_pending_refresh()
        self.assertEqual(store.counters["refreshes"], 2)

    async def test_preload_without_a_disk_snapshot_returns_none(self):
        store = self.make_offline_store()
        self.assertIsNone(await store.preload())
        self.assertIsNone(store.cached_app_count())

    async def test_preload_respects_cache_persist_and_cache_enabled(self):
        await self.make_store().get()
        self.assertIsNone(await self.make_offline_store(cache_persist=False).preload())
        self.assertIsNone(await self.make_offline_store(cache_enabled=False).preload())

    async def test_refresh_now_forces_a_synchronous_refresh_and_writes_the_disk(self):
        store = self.make_store()
        await store.get()
        self.server.reset_counters()
        refreshed = await store.refresh_now()
        self.assertEqual(len(refreshed.apps), 5)
        # 一轮完整抓取恰好是 3 个分类；同步完成并落盘。
        self.assertEqual(len(self.server.class_requests), 3)
        self.assertEqual(store.counters["refreshes"], 2)
        self.assertTrue((self.directory / "catalog.json").is_file())
        status = store.catalog_status()
        self.assertEqual(status["apps"], 5)
        self.assertRegex(status["fetched_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(status["refreshes"], 2)
        self.assertIsNone(status["last_error"])

    async def test_concurrent_refresh_now_calls_are_merged_into_one_scrape(self):
        clock = TickingClock()
        raw = LenovoRawClient(self.client, clock=clock)
        builder = LenovoCatalogBuilder(raw, clock=clock)
        store = LenovoCatalogStore(builder, self.directory, clock=clock)
        await store.get()  # 冷启动一轮
        self.server.reset_counters()
        # POST /store/lenovo/refresh 并发调用（或定时刷新与手动刷新撞车）只抓取一轮：
        # 锁 + fetched_at 判断让后到的一次直接复用刚完成的结果。
        await asyncio.gather(store.refresh_now(), store.refresh_now())
        self.assertEqual(len(self.server.class_requests), 3)
        self.assertEqual(store.counters["refreshes"], 2)
        await store.shutdown()

    async def test_catalog_status_reports_cache_age_and_counters(self):
        store = self.make_store(ttl_seconds=60.0, refresh_interval_seconds=300.0)
        status = store.catalog_status()
        self.assertIsNone(status["apps"])
        self.assertIsNone(status["fetched_at"])
        self.assertIsNone(status["age_seconds"])
        self.assertFalse(status["refresher_running"])
        self.assertEqual(status["refresh_interval_seconds"], 300.0)
        self.assertIsNone(status["next_refresh_at"])
        self.assertEqual(
            set(status),
            {
                "apps",
                "fetched_at",
                "age_seconds",
                "refresh_interval_seconds",
                "refresher_running",
                "next_refresh_at",
                "last_refresh_at",
                "last_error",
                "refreshes",
                "background_refreshes",
                "stale_served",
            },
        )

        await store.get()
        status = store.catalog_status()
        self.assertEqual(status["apps"], 5)
        self.assertEqual(status["age_seconds"], 0.0)
        self.assertEqual(status["refreshes"], 1)
        self.assertEqual(status["background_refreshes"], 0)
        self.assertEqual(status["stale_served"], 0)
        self.assertIsNone(status["last_error"])

        self.clock.now += 61
        await store.get()
        status = store.catalog_status()
        self.assertEqual(status["age_seconds"], 61.0)
        self.assertEqual(status["stale_served"], 1)

    async def test_clear_removes_memory_and_disk_snapshot(self):
        store = self.make_store()
        await store.get()
        self.assertEqual(store.cached_app_count(), 5)
        result = await store.clear()
        self.assertTrue(result["cleared"])
        self.assertIsNone(store.cached_app_count())
        self.assertFalse((self.directory / "catalog.json").exists())
        await store.get()
        self.assertEqual(store.cached_app_count(), 5)

    async def test_clear_without_cache_persist_does_not_delete_foreign_disk_snapshot(self):
        await self.make_store().get()
        catalog_file = self.directory / "catalog.json"
        self.assertTrue(catalog_file.is_file())
        store = self.make_store(cache_persist=False)
        result = await store.clear()
        self.assertFalse(result["cleared"])
        self.assertFalse(result["disk_cleared"])
        self.assertTrue(catalog_file.is_file())

    async def test_find_and_logo_source_are_case_insensitive(self):
        store = self.make_store()
        await store.get()
        self.assertEqual(store.find("l105867").soft_id, "L105867")
        self.assertEqual(store.logo_source("l105867"), "https://pc1.gtimg.com/qq.png")
        self.assertIsNone(store.find("missing"))


class LenovoCatalogHelperTests(unittest.TestCase):
    def test_size_parsing(self):
        cases = {
            "318.95 MB": int(round(318.95 * 1024**2)),
            "525KB": 525 * 1024,
            "26.91 GB": int(round(26.91 * 1024**3)),
            "1,000.7 MB": int(round(1000.7 * 1024**2)),
            "2 TB": 2 * 1024**4,
            " 1.5 GB ": int(round(1.5 * 1024**3)),
            "0 KB": None,
            "": None,
            None: None,
            1024: None,
            "abc MB": None,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_size_bytes(text), expected)

    def test_architectures_and_minimum_windows(self):
        self.assertEqual(build_architectures(["64"]), ["x64"])
        self.assertEqual(build_architectures(["32", "64"]), ["x86", "x64"])
        self.assertEqual(build_architectures(["64", "32"]), ["x86", "x64"])
        self.assertEqual(build_architectures([]), ["x64"])
        self.assertEqual(build_architectures(None), ["x64"])
        self.assertEqual(minimum_windows_from_platforms(["win11", "win10"]), "10")
        self.assertEqual(minimum_windows_from_platforms(["win11", "win8_1"]), "8.1")
        self.assertEqual(minimum_windows_from_platforms(["win7", "win10"]), "7")
        self.assertIsNone(minimum_windows_from_platforms([]))
        self.assertIsNone(minimum_windows_from_platforms(None))
        self.assertEqual(format_platforms(["64"], ["win10", "win11"]), "Windows 10 / 11 · 64 位")
        self.assertEqual(format_platforms(["32", "64"], ["win7"]), "Windows 7 · 32 / 64 位")
        self.assertEqual(format_platforms([], []), "Windows")

    def test_icon_text_and_background_are_stable(self):
        self.assertEqual(icon_text_for("QQ"), "QQ")
        self.assertEqual(icon_text_for("微信"), "微")
        self.assertEqual(icon_text_for("7-Zip"), "7")
        self.assertEqual(icon_text_for(""), "?")
        first = icon_background_for("微信")
        self.assertEqual(first, icon_background_for("微信"))
        self.assertRegex(first, r"^#[0-9a-fA-F]{6}$")
        self.assertNotEqual(first, icon_background_for("QQ"))
        self.assertEqual(app_id_for("L105867"), "lenovo-l105867")


def summary_payload(**overrides) -> dict:
    """合法 StoreAppSummary 的最小字段集；icon_url 是联想 CDN 绝对地址。"""

    data = {
        "id": "lenovo-l105867",
        "soft_id": "L105867",
        "name": "QQ",
        "description": "QQ9全新上线",
        "category": "聊天社交",
        "category_id": "1",
        "group": "software",
        "version": "9.9.37.54395",
        "size_display": "318.95 MB",
        "platforms": "Windows 10 / 11 · 64 位",
        "architectures": ["x64"],
        "icon_url": "https://lsw-fast.lenovo.com.cn/logo/L105867.png",
        "icon_text": "QQ",
        "icon_background": "#1679C8",
        "tags": ["聊天社交"],
    }
    data.update(overrides)
    return data


class LenovoStoreModelContractTests(unittest.TestCase):
    """客户端直连图标：icon_url 绝对、icon_proxy_url 可选且必须是后端相对路径。"""

    def test_icon_url_must_be_an_absolute_http_url(self):
        valid = StoreAppSummary.model_validate(summary_payload())
        self.assertEqual(valid.icon_url, "https://lsw-fast.lenovo.com.cn/logo/L105867.png")
        self.assertIsNone(valid.icon_proxy_url)

        with_proxy = StoreAppSummary.model_validate(
            summary_payload(icon_proxy_url="/api/v1/store/lenovo/apps/L105867/icon")
        )
        self.assertEqual(with_proxy.icon_proxy_url, "/api/v1/store/lenovo/apps/L105867/icon")

        invalid = (
            "/api/v1/store/lenovo/apps/L105867/icon",
            "ftp://lsw-fast.lenovo.com.cn/logo/L105867.png",
            "https://",
            "https:///logo.png",
            "https://user:pass@lsw-fast.lenovo.com.cn/logo/L105867.png",
            "https://lsw-fast.lenovo.com.cn/" + "a" * 1024,
            "",
        )
        for value in invalid:
            with self.subTest(icon_url=value), self.assertRaises(ValueError):
                StoreAppSummary.model_validate(summary_payload(icon_url=value))

    def test_icon_proxy_url_must_be_a_store_relative_path(self):
        for value in ("https://evil.example/icon", "/other/icon", "/api/v1/store/", ""):
            with self.subTest(icon_proxy_url=value), self.assertRaises(ValueError):
                StoreAppSummary.model_validate(summary_payload(icon_proxy_url=value))

    def test_icon_url_length_boundary_and_details_inherit_icon_fields(self):
        prefix = "https://lsw-fast.lenovo.com.cn/logo/"
        boundary = prefix + "a" * (1024 - len(prefix))
        self.assertEqual(len(boundary), 1024)
        summary = StoreAppSummary.model_validate(
            summary_payload(
                icon_url=boundary,
                icon_proxy_url="/api/v1/store/lenovo/apps/L105867/icon",
            )
        )
        self.assertEqual(summary.icon_url, boundary)
        details = StoreAppDetails(**summary.model_dump(), long_description="详细描述")
        self.assertEqual(details.icon_url, boundary)
        self.assertEqual(details.icon_proxy_url, "/api/v1/store/lenovo/apps/L105867/icon")


# ---------------------------------------------------------------------------
# 服务门面接线（backend-api 直接依赖的公开 API）
# ---------------------------------------------------------------------------


class LenovoServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.clock = MutableClock()
        self.temporary = tempfile.TemporaryDirectory(prefix="wam-lenovo-service-")
        self.addCleanup(self.temporary.cleanup)
        self.cache_directory = Path(self.temporary.name) / "cache"
        self.server = CatalogServer(self.clock, default_classes())
        self.client = httpx2.AsyncClient(transport=httpx2.MockTransport(self.server.handler))
        self.addAsyncCleanup(self.client.aclose)
        self.settings = SimpleNamespace(
            lenovo=SimpleNamespace(
                catalog_ttl_seconds=21600.0,
                catalog_refresh_interval_seconds=300.0,
                catalog_max_stale_seconds=604800.0,
                details_ttl_seconds=900.0,
                details_persistent_ttl_seconds=21600.0,
                search_cache_ttl_seconds=60.0,
                timeout_seconds=20.0,
                catalog_concurrency=3,
                image_cache_ttl_seconds=2592000.0,
                image_cache_max_bytes=268435456,
            )
        )
        self.service = create_lenovo_store_service(
            self.client, self.cache_directory, self.settings, cache_enabled=True, cache_persist=True
        )

    async def asyncTearDown(self) -> None:
        await self.service.shutdown()

    async def test_catalog_categories_and_counts(self):
        self.assertIsNone(self.service.cached_app_count())
        self.assertIsNone(self.service.cached_updated_at())
        catalog = await self.service.get_catalog()
        self.assertEqual(catalog.source, "lenovo")
        self.assertEqual(catalog.source_name, "联想应用商店")
        self.assertEqual(catalog.total, 5)
        self.assertEqual(len(catalog.items), 5)
        self.assertFalse(catalog.stale)
        self.assertIsNotNone(catalog.updated_at)
        self.assertEqual(self.service.cached_app_count(), 5)
        self.assertIsNotNone(self.service.cached_updated_at())

        categories = await self.service.get_categories()
        self.assertEqual([group.code for group in categories.groups], ["software", "game"])
        self.assertEqual(categories.groups[0].categories[0].count, 3)
        self.assertIsNotNone(categories.updated_at)

        # 缓存命中：再次读取不会重新抓取。
        requests_before = len(self.server.class_requests)
        await self.service.get_catalog()
        self.assertEqual(len(self.server.class_requests), requests_before)

    async def test_details_and_media_proxies(self):
        await self.service.get_catalog()
        details = await self.service.get_details("L105867")
        self.assertEqual(details.soft_id, "L105867")
        self.assertEqual(details.category, "输入法")
        self.assertEqual(details.long_description, "详细描述文本")
        self.assertEqual(details.release_notes, "更新说明文本")
        self.assertEqual([(shot.index, shot.kind) for shot in details.screenshots], [(0, "image"), (1, "video")])
        # 截图/封面优先返回联想 CDN 绝对 https 地址，proxy_url 始终是后端代理路径。
        self.assertEqual(details.screenshots[0].url, "https://lsw-fast.lenovo.com.cn/img/1.png")
        self.assertEqual(details.screenshots[1].url, "https://imgs.lenovomm.cn/thumb.jpg")
        self.assertEqual(
            details.screenshots[0].proxy_url, "/api/v1/store/lenovo/apps/L105867/screenshots/0"
        )
        self.assertEqual(
            details.screenshots[1].proxy_url, "/api/v1/store/lenovo/apps/L105867/screenshots/1"
        )
        self.assertEqual(details.cover_url, "https://imgs.lenovomm.cn/cover.jpg")
        self.assertEqual(details.cover_proxy_url, "/api/v1/store/lenovo/apps/L105867/cover")
        self.assertEqual(details.labels, ["AI对话", "办公辅助工具"])
        self.assertEqual(details.ranking, "聊天社交类下载榜第2名")
        self.assertEqual(details.detail_tags[0].name, "安全认证")
        self.assertEqual(details.detail_tags[0].tips, "软件通过检测")
        self.assertEqual(details.size_bytes, 525 * 1024)
        # 详情继承 summary 的图标字段：icon_url 仍是联想 CDN 绝对地址。
        self.assertEqual(details.icon_url, "https://pc1.gtimg.com/qq.png")
        self.assertEqual(details.icon_proxy_url, "/api/v1/store/lenovo/apps/L105867/icon")
        self.assertNotIn("auth_key", details.model_dump_json())

        icon = await self.service.get_icon("L105867")
        self.assertEqual(icon.content, PNG_BYTES)
        self.assertEqual(icon.content_type, "image/png")
        self.assertEqual(icon.etag, self.service_media_etag("https://pc1.gtimg.com/qq.png"))
        screenshot = await self.service.get_screenshot("L105867", 0)
        self.assertEqual(screenshot.content_type, "image/png")
        cover = await self.service.get_cover("L105867")
        self.assertEqual(cover.content, PNG_BYTES)
        with self.assertRaises(LenovoNotFoundError):
            await self.service.get_screenshot("L105867", 99)
        self.server.details_payload = {"status": -1, "message": "软件已下架", "data": None}
        with self.assertRaises(LenovoNotFoundError):
            await self.service.get_details("NOPE")

    def service_media_etag(self, url: str) -> str:
        from hashlib import sha256

        return sha256(url.encode("utf-8")).hexdigest()[:32]

    async def test_search_results_are_cached_and_filtered(self):
        response = await self.service.search("QQ", limit=30)
        self.assertEqual(response.query, "QQ")
        self.assertEqual(response.total, 2)
        self.assertEqual([item.soft_id for item in response.items], ["L105867", "99999"])
        self.assertEqual(response.items[0].category, "输入法")
        # 搜索项同样直连 CDN；没有绝对 logo 的条目被过滤，不产生非法模型。
        self.assertEqual(response.items[0].icon_url, "https://lsw-fast.lenovo.com.cn/logo/L105867.png")
        self.assertEqual(response.items[0].icon_proxy_url, "/api/v1/store/lenovo/apps/L105867/icon")
        self.assertNotIn("77777", [item.soft_id for item in response.items])
        self.assertNotIn("88888", [item.soft_id for item in response.items])

        requests_before = len(self.server.requests)
        cached = await self.service.search("QQ")
        self.assertEqual(cached.total, 2)
        self.assertEqual(len(self.server.requests), requests_before)

        empty = await self.service.search("   ")
        self.assertEqual(empty.total, 0)

    async def test_refresh_and_clear(self):
        await self.service.get_catalog()
        refreshed = await self.service.refresh_catalog()
        self.assertTrue(refreshed["refreshed"])
        self.assertEqual(refreshed["total"], 5)
        self.assertRegex(refreshed["updated_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertGreaterEqual(refreshed["duration_seconds"], 0.0)
        self.assertTrue((self.cache_directory / "lenovo" / "catalog.json").is_file())

        result = await self.service.clear_caches()
        self.assertIn("catalog", result)
        self.assertIn("details", result)
        self.assertIn("media", result)
        self.assertIsNone(self.service.cached_app_count())

    async def test_preload_catalog_reads_the_disk_snapshot_without_network(self):
        await self.service.get_catalog()
        expected = await self.service.get_catalog()
        await self.service.shutdown()

        def exploding_handler(request: httpx2.Request) -> httpx2.Response:
            raise AssertionError(f"预加载不应访问网络: {request.url}")

        client = httpx2.AsyncClient(transport=httpx2.MockTransport(exploding_handler))
        self.addAsyncCleanup(client.aclose)
        restarted = create_lenovo_store_service(
            client, self.cache_directory, self.settings, cache_enabled=True, cache_persist=True
        )
        try:
            with self.assertLogs("winappsmanager.lenovo", level="INFO") as captured:
                loaded = await restarted.preload_catalog()
            self.assertIsNotNone(loaded)
            self.assertEqual(len(loaded.apps), len(expected.items))
            self.assertEqual(restarted.cached_app_count(), 5)
            self.assertIn("预加载", "\n".join(captured.output))
            # 预加载后请求只读内存快照。
            catalog = await restarted.get_catalog()
            self.assertEqual(catalog.total, 5)
            status = restarted.catalog_status()
            self.assertEqual(status["apps"], 5)
            self.assertEqual(status["refresh_interval_seconds"], 300.0)
            self.assertFalse(status["refresher_running"])
        finally:
            await restarted.shutdown()

    async def test_catalog_status_reports_the_live_service_state(self):
        status = self.service.catalog_status()
        self.assertIsNone(status["apps"])
        self.assertEqual(status["refresh_interval_seconds"], 300.0)
        self.assertEqual(status["refreshes"], 0)
        await self.service.get_catalog()
        status = self.service.catalog_status()
        self.assertEqual(status["apps"], 5)
        self.assertEqual(status["refreshes"], 1)
        self.assertFalse(status["refresher_running"])
        self.assertIsNone(status["next_refresh_at"])

    async def test_shutdown_rejects_further_reads(self):
        await self.service.get_catalog()
        await self.service.shutdown()
        with self.assertRaises(LenovoUnavailableError):
            await self.service.get_catalog()

    async def test_service_works_before_lenovo_config_exists(self):
        service = create_lenovo_store_service(self.client, None, None)
        try:
            catalog = await service.get_catalog()
            self.assertEqual(catalog.total, 5)
            self.assertEqual(service.cached_app_count(), 5)
        finally:
            await service.shutdown()


if __name__ == "__main__":
    unittest.main()
