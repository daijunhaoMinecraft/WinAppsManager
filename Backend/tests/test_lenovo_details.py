"""联想详情缓存 / 截图映射 / 下载解析测试（MockTransport + 临时目录）。"""

from __future__ import annotations

import base64
from hashlib import sha256
import json
from pathlib import Path
import sys
import tempfile
import unittest

import httpx2

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR.parent))

from Backend.lenovo.client import LenovoRawClient
from Backend.lenovo.details import (
    DETAILS_SCHEMA_VERSION,
    DetailsFields,
    LenovoDetailsProvider,
    ScreenshotSource,
)
from Backend.lenovo.errors import (
    LenovoNotFoundError,
    LenovoProtocolError,
    LenovoTimeoutError,
    LenovoUnavailableError,
)
from Backend.lenovo.models import StoreAppDetails, StoreAppSummary, StoreScreenshot


class MutableClock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def make_jwt(exp: int) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"typ": "JWT", "alg": "HS256"}).encode()).rstrip(b"=")
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=")
    return f"{header.decode()}.{payload.decode()}.signature"


def summary() -> StoreAppSummary:
    return StoreAppSummary(
        id="lenovo-l105867",
        soft_id="L105867",
        name="QQ",
        description="QQ9全新上线",
        category="聊天社交",
        category_id="1",
        group="software",
        version="9.9.37.54395",
        size_display="318.95 MB",
        size_bytes=334_442_672,
        platforms="Windows 10 / 11 · 64 位",
        architectures=["x64"],
        icon_url="https://lsw-fast.lenovo.com.cn/logo/L105867.png",
        icon_proxy_url="/api/v1/store/lenovo/apps/L105867/icon",
        icon_text="QQ",
        icon_background="#1679C8",
    )


DETAILS_DATA = {
    "softID": "L105867",
    "softName": "QQ",
    "version": "9.9.37.54395",
    "detailInfo": "详细描述文本",
    "whatNew": "更新说明文本",
    "captureFileList": [
        {"type": "0", "url": "https://lsw-fast.lenovo.com.cn/img/1.png", "videoImg": None},
        {
            "type": "1",
            "url": "https://pc-store-video.lenovomm.cn/v.mp4?auth_key=secret-token",
            "videoImg": "https://imgs.lenovomm.cn/thumb.jpg",
        },
        {"type": "0", "url": "https://lsw-fast.lenovo.com.cn/img/2.jpg", "videoImg": None},
    ],
    "cover": {"coverImg": "https://imgs.lenovomm.cn/cover.jpg"},
    "appLabelList": [{"labelName": "AI对话"}, {"labelName": "办公辅助工具"}, {"labelName": "AI对话"}],
    "classRankingDesc": {"targetDesc": "聊天社交类下载榜第2名"},
    "detailsTag": [
        {"name": "安全认证", "tips": "软件通过多款主流安全软件检测"},
        {"name": "无提示", "tips": None},
    ],
}

DOWNLOAD_DATA = {
    "downloadCode": 2,
    "downloadUrls": [
        {"downLoadUrl": "https://pc-lsw7.lenovomm.cn/dl?sign=1", "downloadCode": "hs2"},
    ],
    "fileName": "QQ9.9.37.54395_lenovo_x64.exe",
    "sha256": "7019f1a55557b4260cb7385f015a521378748d06dfd9915905e14f0585f4e855",
}


class DetailsServer:
    def __init__(self, clock: MutableClock) -> None:
        self.clock = clock
        self.requests: list[httpx2.Request] = []
        self.details_payload: dict | None = None
        self.download_payload: dict | None = None
        self.failure: Exception | None = None
        self.register_count = 0

    def _ok(self, request: httpx2.Request, data) -> httpx2.Response:
        return httpx2.Response(200, json={"status": 0, "message": None, "data": data}, request=request)

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        path = request.url.path
        if path == "/appbase/token/register":
            self.register_count += 1
            return self._ok(request, make_jwt(int(self.clock.now) + 15 * 24 * 3600))
        if path == "/appstorecontents/app/details_new":
            payload = self.details_payload
            if payload is None:
                payload = {"status": 0, "message": None, "data": DETAILS_DATA}
            return httpx2.Response(200, json=payload, request=request)
        if path == "/dlservice/getPcSoftDownloadUrlList":
            payload = self.download_payload
            if payload is None:
                payload = {"status": 0, "message": "", "data": DOWNLOAD_DATA}
            return httpx2.Response(200, json=payload, request=request)
        return httpx2.Response(404, request=request)

    def detail_request_count(self) -> int:
        return sum(1 for request in self.requests if request.url.path == "/appstorecontents/app/details_new")


class LenovoDetailsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.clock = MutableClock()
        self.temporary = tempfile.TemporaryDirectory(prefix="wam-lenovo-details-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "lenovo"
        self.server = DetailsServer(self.clock)
        self.client = httpx2.AsyncClient(transport=httpx2.MockTransport(self.server.handler))
        self.addAsyncCleanup(self.client.aclose)
        self.raw = LenovoRawClient(self.client, clock=self.clock)
        self.provider = LenovoDetailsProvider(
            self.raw,
            self.directory,
            ttl_seconds=900.0,
            persistent_ttl_seconds=21600.0,
            clock=self.clock,
        )

    async def test_details_fields_and_screenshot_indices(self):
        fields = await self.provider.get_fields("L105867")
        self.assertEqual(fields.long_description, "详细描述文本")
        self.assertEqual(fields.release_notes, "更新说明文本")
        self.assertEqual(
            [(shot.index, shot.kind, shot.source_url) for shot in fields.screenshots],
            [
                (0, "image", "https://lsw-fast.lenovo.com.cn/img/1.png"),
                (1, "video", "https://imgs.lenovomm.cn/thumb.jpg"),
                (2, "image", "https://lsw-fast.lenovo.com.cn/img/2.jpg"),
            ],
        )
        # 视频 url 带 auth_key，只保留缩略图。
        self.assertNotIn("auth_key", json.dumps([shot.source_url for shot in fields.screenshots]))
        self.assertEqual(fields.cover_source, "https://imgs.lenovomm.cn/cover.jpg")
        self.assertEqual(fields.labels, ("AI对话", "办公辅助工具"))
        self.assertEqual(fields.ranking, "聊天社交类下载榜第2名")
        self.assertEqual(
            fields.detail_tags,
            (("安全认证", "软件通过多款主流安全软件检测"), ("无提示", None)),
        )

        details = self.provider.build_details(summary(), fields)
        self.assertIsInstance(details, StoreAppDetails)
        self.assertEqual(details.id, "lenovo-l105867")
        self.assertEqual(details.version, "9.9.37.54395")
        # 截图/视频缩略图优先联想 CDN 绝对 https 地址，proxy_url 始终是后端代理路径。
        self.assertEqual(
            [(shot.index, shot.kind, shot.url, shot.proxy_url) for shot in details.screenshots],
            [
                (
                    0,
                    "image",
                    "https://lsw-fast.lenovo.com.cn/img/1.png",
                    "/api/v1/store/lenovo/apps/L105867/screenshots/0",
                ),
                (
                    1,
                    "video",
                    "https://imgs.lenovomm.cn/thumb.jpg",
                    "/api/v1/store/lenovo/apps/L105867/screenshots/1",
                ),
                (
                    2,
                    "image",
                    "https://lsw-fast.lenovo.com.cn/img/2.jpg",
                    "/api/v1/store/lenovo/apps/L105867/screenshots/2",
                ),
            ],
        )
        self.assertEqual(details.cover_url, "https://imgs.lenovomm.cn/cover.jpg")
        self.assertEqual(details.cover_proxy_url, "/api/v1/store/lenovo/apps/L105867/cover")
        self.assertEqual([tag.name for tag in details.detail_tags], ["安全认证", "无提示"])
        self.assertEqual(details.long_description, "详细描述文本")
        # 详情继承 summary 的图标字段：客户端仍可直连联想 CDN，代理路径作兜底。
        self.assertEqual(details.icon_url, "https://lsw-fast.lenovo.com.cn/logo/L105867.png")
        self.assertEqual(details.icon_proxy_url, "/api/v1/store/lenovo/apps/L105867/icon")

    async def test_non_direct_image_urls_fall_back_to_the_proxy_path(self):
        """内网/回环等不安全的图片地址回退后端代理路径；任意公网 CDN 直接返回原地址。"""

        fields = DetailsFields(
            long_description="描述",
            release_notes="",
            screenshots=(
                ScreenshotSource(index=0, kind="image", source_url="http://lsw-fast.lenovo.com.cn/a.png"),
                ScreenshotSource(
                    index=1, kind="image", source_url="https://p15.qhmsg.com/t01ef3b2bb4170fa1f0.png"
                ),
                ScreenshotSource(
                    index=2, kind="video", source_url="https://unknown-cdn.vendor-name.net/thumb.jpg"
                ),
                ScreenshotSource(index=3, kind="image", source_url="http://127.0.0.1/a.png"),
                ScreenshotSource(index=4, kind="image", source_url="http://192.168.1.20/a.png"),
                ScreenshotSource(index=5, kind="image", source_url="https://pc1.gtimg.com/a.png"),
            ),
            cover_source="http://p15.qhmsg.com/cover.png",
            labels=(),
            ranking=None,
            detail_tags=(),
        )
        details = self.provider.build_details(summary(), fields)
        self.assertEqual(
            [shot.url for shot in details.screenshots],
            [
                "http://lsw-fast.lenovo.com.cn/a.png",
                "https://p15.qhmsg.com/t01ef3b2bb4170fa1f0.png",
                "https://unknown-cdn.vendor-name.net/thumb.jpg",
                "/api/v1/store/lenovo/apps/L105867/screenshots/3",
                "/api/v1/store/lenovo/apps/L105867/screenshots/4",
                "https://pc1.gtimg.com/a.png",
            ],
        )
        self.assertEqual(
            [shot.proxy_url for shot in details.screenshots],
            [f"/api/v1/store/lenovo/apps/L105867/screenshots/{index}" for index in range(6)],
        )
        # 封面是名单外公网 CDN 时直接返回；cover_proxy_url 保持代理路径。
        self.assertEqual(details.cover_url, "http://p15.qhmsg.com/cover.png")
        self.assertEqual(details.cover_proxy_url, "/api/v1/store/lenovo/apps/L105867/cover")

        unsafe_cover = DetailsFields(
            long_description="描述",
            release_notes="",
            screenshots=(),
            cover_source="http://127.0.0.1/cover.png",
            labels=(),
            ranking=None,
            detail_tags=(),
        )
        unsafe_details = self.provider.build_details(summary(), unsafe_cover)
        self.assertEqual(unsafe_details.cover_url, "/api/v1/store/lenovo/apps/L105867/cover")
        self.assertEqual(unsafe_details.cover_proxy_url, "/api/v1/store/lenovo/apps/L105867/cover")

        without_cover = DetailsFields(
            long_description="描述",
            release_notes="",
            screenshots=(),
            cover_source=None,
            labels=(),
            ranking=None,
            detail_tags=(),
        )
        no_cover_details = self.provider.build_details(summary(), without_cover)
        self.assertIsNone(no_cover_details.cover_url)
        self.assertIsNone(no_cover_details.cover_proxy_url)
        self.assertEqual(no_cover_details.screenshots, [])

    async def test_screenshot_urls_reject_internal_and_malformed_addresses(self):
        """模型层拒绝内网/回环/带凭据等不安全 URL：url 只能是安全公网 http(s) 或本后端代理路径。"""

        valid = [
            "https://lsw-fast.lenovo.com.cn/a.png",
            "http://lsw-fast.lenovo.com.cn/a.png",
            "https://imgs.lenovomm.cn/a.png",
            "https://pc1.gtimg.com/a.png",
            "http://p15.qhmsg.com/a.png",
            "https://unknown-cdn.vendor-name.net/a.png",
            "/api/v1/store/lenovo/apps/L105867/screenshots/0",
        ]
        for url in valid:
            with self.subTest(url=url):
                screenshot = StoreScreenshot(index=0, kind="image", url=url)
                self.assertEqual(screenshot.url, url)
        for url in (
            "http://127.0.0.1/a.png",
            "http://localhost/a.png",
            "https://[::1]/a.png",
            "http://10.0.0.8/a.png",
            "https://printer.local/a.png",
            "https://user:pass@imgs.lenovomm.cn/a.png",
            "ftp://imgs.lenovomm.cn/a.png",
            "/api/v1/store/360/apps/1/screenshots/0",
            "not a url",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                StoreScreenshot(index=0, kind="image", url=url)
        with self.assertRaises(ValueError):
            StoreScreenshot(
                index=0,
                kind="image",
                url="https://imgs.lenovomm.cn/a.png",
                proxy_url="https://imgs.lenovomm.cn/a.png",
            )
        with self.assertRaises(ValueError):
            StoreAppDetails(
                **summary().model_dump(),
                long_description="描述",
                cover_url="http://127.0.0.1/cover.png",
            )
        with self.assertRaises(ValueError):
            StoreAppDetails(
                **summary().model_dump(),
                long_description="描述",
                cover_proxy_url="/api/v1/store/360/apps/1/cover",
            )

    async def test_video_url_is_never_written_to_disk(self):
        await self.provider.get_fields("L105867")
        details_directory = self.directory / "details"
        files = list(details_directory.glob("*.json"))
        self.assertEqual(len(files), 1)
        expected_name = sha256(b"L105867").hexdigest() + ".json"
        self.assertEqual(files[0].name, expected_name)
        text = files[0].read_text(encoding="utf-8")
        self.assertNotIn("auth_key", text)
        self.assertNotIn("pc-store-video.lenovomm.cn/v.mp4", text)
        self.assertIn("https://imgs.lenovomm.cn/thumb.jpg", text)
        document = json.loads(text)
        self.assertEqual(document["schema_version"], DETAILS_SCHEMA_VERSION)
        self.assertEqual(document["soft_id"], "L105867")
        self.assertEqual(document["fetched_at"], self.clock.now)
        self.assertIsInstance(document["screenshots"], list)

    async def test_memory_cache_avoids_repeated_requests(self):
        await self.provider.get_fields("L105867")
        await self.provider.get_fields("L105867")
        self.assertEqual(self.server.detail_request_count(), 1)
        self.assertEqual(self.provider.counters["hits"], 1)

    async def test_disk_cache_roundtrip(self):
        first = await self.provider.get_fields("L105867")

        def exploding_handler(request: httpx2.Request) -> httpx2.Response:
            raise AssertionError(f"不应访问网络: {request.url}")

        client = httpx2.AsyncClient(transport=httpx2.MockTransport(exploding_handler))
        self.addAsyncCleanup(client.aclose)
        raw = LenovoRawClient(client, clock=self.clock)
        restarted = LenovoDetailsProvider(raw, self.directory, clock=self.clock)
        loaded = await restarted.get_fields("L105867")
        self.assertEqual(loaded, first)

    async def test_memory_ttl_expiry_reuses_fresh_disk_entry(self):
        await self.provider.get_fields("L105867")
        baseline = self.server.detail_request_count()
        self.clock.now += 901  # 内存 TTL 900 秒，磁盘 TTL 21600 秒
        loaded = await self.provider.get_fields("L105867")
        self.assertEqual(loaded.long_description, "详细描述文本")
        self.assertEqual(self.server.detail_request_count(), baseline)

    async def test_stale_details_fallback_when_upstream_fails(self):
        await self.provider.get_fields("L105867")
        self.clock.now += 21601  # 磁盘 TTL 也过期
        self.server.failure = httpx2.ReadTimeout("slow", request=None)
        fields = await self.provider.get_fields("L105867")
        self.assertEqual(fields.long_description, "详细描述文本")
        self.assertEqual(self.provider.counters["stale_hits"], 1)
        self.server.failure = None

    async def test_offline_app_raises_not_found_and_is_not_cached(self):
        self.server.details_payload = {"status": -1, "message": "软件已下架", "data": None}
        with self.assertRaises(LenovoNotFoundError):
            await self.provider.get_fields("L105867")
        self.assertEqual(list((self.directory / "details").glob("*.json")) if (self.directory / "details").is_dir() else [], [])
        # 恢复上架后重新抓取成功（NotFound 没有污染缓存）。
        self.server.details_payload = None
        fields = await self.provider.get_fields("L105867")
        self.assertEqual(fields.long_description, "详细描述文本")

    async def test_mismatched_or_malformed_details_are_protocol_errors(self):
        self.server.details_payload = {"status": 0, "message": None, "data": {**DETAILS_DATA, "softID": "OTHER"}}
        with self.assertRaises(LenovoProtocolError):
            await self.provider.get_fields("L105867")
        self.server.details_payload = {"status": 0, "message": None, "data": {**DETAILS_DATA, "captureFileList": "broken"}}
        with self.assertRaises(LenovoProtocolError):
            await self.provider.get_fields("L105867")
        self.server.details_payload = {"status": 0, "message": None, "data": {**DETAILS_DATA, "captureFileList": None}}
        fields = await self.provider.get_fields("L105867")
        self.assertEqual(fields.screenshots, ())

    async def test_missing_optional_fields(self):
        minimal = {
            "softID": "L105867",
            "softName": "QQ",
            "detailInfo": "",
            "whatNew": None,
            "captureFileList": [
                {"type": "0", "url": "", "videoImg": None},
                {"type": "1", "url": "https://example.invalid/v.mp4", "videoImg": None},
                {"type": "9", "url": "https://lsw-fast.lenovo.com.cn/x.png"},
            ],
            "cover": {"coverImg": ""},
            "appLabelList": None,
            "classRankingDesc": None,
            "detailsTag": None,
        }
        self.server.details_payload = {"status": 0, "message": None, "data": minimal}
        fields = await self.provider.get_fields("L105867")
        self.assertEqual(fields.long_description, "")
        self.assertEqual(fields.release_notes, "")
        self.assertEqual(fields.screenshots, ())
        self.assertIsNone(fields.cover_source)
        self.assertEqual(fields.labels, ())
        self.assertIsNone(fields.ranking)
        self.assertEqual(fields.detail_tags, ())

    async def test_timeout_and_http_errors_are_typed(self):
        self.server.failure = httpx2.ReadTimeout("slow", request=None)
        with self.assertRaises(LenovoTimeoutError):
            await self.provider.get_fields("L105867")
        self.server.failure = None
        self.server.details_payload = {"status": 500, "message": "上游错误", "data": None}
        with self.assertRaises(LenovoUnavailableError):
            await self.provider.get_fields("L105867")

    async def test_download_resolution_error_branches(self):
        self.server.download_payload = {"status": 0, "message": "", "data": {"downloadUrls": None}}
        with self.assertRaises(LenovoUnavailableError):
            await self.provider.resolve_download("L105867", summary())
        self.server.download_payload = {"status": 500, "message": "内部错误", "data": None}
        with self.assertRaises(LenovoUnavailableError):
            await self.provider.resolve_download("L105867", summary())
        self.server.download_payload = {
            "status": 0,
            "message": "",
            "data": {"downloadUrls": [{"downLoadUrl": "http://only-http/x.exe", "downloadCode": "hs2"}]},
        }
        with self.assertRaises(LenovoUnavailableError):
            await self.provider.resolve_download("L105867", summary())

    async def test_download_addresses_are_never_cached(self):
        await self.provider.resolve_download("L105867", summary())
        await self.provider.resolve_download("L105867", summary())
        self.assertEqual(len([r for r in self.server.requests if r.url.path == "/dlservice/getPcSoftDownloadUrlList"]), 2)
        self.assertFalse((self.directory / "details").exists())

    async def test_clear_removes_memory_and_disk(self):
        await self.provider.get_fields("L105867")
        removed = await self.provider.clear()
        self.assertGreaterEqual(removed, 1)
        self.assertEqual(list((self.directory / "details").glob("*.json")), [])
        await self.provider.get_fields("L105867")
        self.assertEqual(self.server.detail_request_count(), 2)


class LenovoDetailsDiskCorruptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_corrupt_disk_details_are_ignored(self):
        clock = MutableClock()
        temporary = tempfile.TemporaryDirectory(prefix="wam-lenovo-details-corrupt-")
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name) / "lenovo"
        details_directory = directory / "details"
        details_directory.mkdir(parents=True)
        (details_directory / (sha256(b"L105867").hexdigest() + ".json")).write_text("{oops", encoding="utf-8")

        server = DetailsServer(clock)
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(server.handler))
        self.addAsyncCleanup(client.aclose)
        raw = LenovoRawClient(client, clock=clock)
        provider = LenovoDetailsProvider(raw, directory, clock=clock)
        fields = await provider.get_fields("L105867")
        self.assertEqual(fields.long_description, "详细描述文本")
        self.assertEqual(server.detail_request_count(), 1)
        # 损坏文件被删除，重新写入了可用缓存。
        self.assertEqual(len(list(details_directory.glob("*.json"))), 1)
        self.assertNotIn("oops", (details_directory / (sha256(b"L105867").hexdigest() + ".json")).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
