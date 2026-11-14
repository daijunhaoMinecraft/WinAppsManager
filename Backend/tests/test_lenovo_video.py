"""联想视频截图代理（实时解析 + Range 流式转发 + 不缓存签名 URL）测试。

全部离线：``httpx2.MockTransport`` 同时模拟联想 token/详情 JSON 与视频 CDN 字节流，
不访问真实网络；缓存目录与日志都注入临时目录/关闭文件日志，不写开发机 ``Backend/.cache``。

覆盖：

- 详情实时解析视频地址：不读/不写详情缓存、index 越界/非视频 → NotFound、非 https/
  白名单外主机/用户信息 → ProtocolError、softID 不一致 → ProtocolError；
- ``GET /api/v1/store/lenovo/apps/{soft_id}/screenshots/{index}/video``：200 流式、
  ``Range`` → 206 并透传 ``Content-Range``/``Accept-Ranges``、416 透传、
  404（图片/越界）、403 auth_key 过期后重新解析一次并重试成功、二次 403 → 502、
  上游 5xx → 502、超时 → 504、源禁用 → 503；
- ``Cache-Control: no-store``、``HEAD`` 只返回响应头不读视频体；
- 签名视频 URL/auth_key 不写日志、不落盘、不进入任何缓存文件。
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import logging
from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

import httpx2
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR.parent))

from Backend.config import (
    BackendSettings,
    CacheConfig,
    LenovoConfig,
    LoggingConfig,
    RefreshConfig,
)
from Backend.lenovo.client import LenovoRawClient
from Backend.lenovo.details import (
    ALLOWED_VIDEO_DOMAIN_SUFFIXES,
    ALLOWED_VIDEO_HOSTS,
    LenovoDetailsProvider,
)
from Backend.lenovo.errors import LenovoNotFoundError, LenovoProtocolError
from Backend.lenovo.service import (
    DEFAULT_VIDEO_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_VIDEO_READ_TIMEOUT_SECONDS,
    create_lenovo_store_service,
)
from Backend.main import create_app as create_application


# 与 test_api.py / test_lenovo_api.py 相同：指向不存在的 apps.d，保持内置精选源断言稳定。
FIXTURE_EXTRA_DIRECTORY = BACKEND_DIR / "tests" / "no-extra-apps.d"
PROVIDER_IDS = ("wechat", "vscode", "7zip", "vlc", "firefox", "obs", "notepadplusplus", "qq")
LOGGER_NAME = "winappsmanager.lenovo"

SOFT_ID = "L105867"
VIDEO_HOST = "pc-store-video.lenovomm.cn"
VIDEO_PATH = "/v/L105867-preview.mp4"
# 两个带不同 auth_key 的临时地址：用于验证“401/403/410 重新解析后重试”。
VIDEO_URL = f"https://{VIDEO_HOST}{VIDEO_PATH}?auth_key=secret-key-1"
VIDEO_URL_2 = f"https://{VIDEO_HOST}{VIDEO_PATH}?auth_key=secret-key-2"
VIDEO_URL_PATH = f"/api/v1/store/lenovo/apps/{SOFT_ID}/screenshots/1/video"
THUMB_URL = "https://imgs.lenovomm.cn/thumb.jpg"
VIDEO_BYTES = b"FAKE-MP4-BYTES-" + bytes(range(64)) + b"-END"


def make_jwt(exp: int) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"typ": "JWT", "alg": "HS256"}).encode()).rstrip(b"=")
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=")
    return f"{header.decode()}.{payload.decode()}.signature"


def make_details_data(video_url: str = VIDEO_URL, *, video_type: str = "1") -> dict:
    """captureFileList：index 0 图片、index 1 视频、index 2 图片（顺序即客户端索引）。"""

    return {
        "softID": SOFT_ID,
        "softName": "QQ",
        "version": "9.9.37.54395",
        "detailInfo": "QQ 的详细描述。",
        "whatNew": "修复已知问题并提升稳定性。",
        "captureFileList": [
            {"type": "0", "url": "https://lsw-fast.lenovo.com.cn/img/0.png", "videoImg": None},
            {
                "type": video_type,
                "url": video_url,
                "videoImg": THUMB_URL,
            },
            {"type": "0", "url": "https://lsw-fast.lenovo.com.cn/img/2.jpg", "videoImg": None},
        ],
        "cover": {"coverImg": "https://imgs.lenovomm.cn/cover.jpg"},
        "appLabelList": None,
        "classRankingDesc": None,
        "detailsTag": None,
    }


def _parse_range(value: str, total: int) -> tuple[int, int]:
    spec = value.split("=", 1)[1]
    start_text, _, end_text = spec.partition("-")
    start = int(start_text) if start_text else 0
    end = int(end_text) if end_text else total - 1
    return start, min(end, total - 1)


class FakeUpstream:
    """联想 token/详情 + 视频 CDN 的 MockTransport 处理器（记录每个请求）。"""

    def __init__(self, video_body: bytes = VIDEO_BYTES) -> None:
        self.video_body = video_body
        self.requests: list[httpx2.Request] = []
        self.details_requests = 0
        self.token_requests = 0
        self.video_requests: list[httpx2.Request] = []
        self.default_video_url = VIDEO_URL
        # 每次详情请求依次消费一个 URL；为空时用 default_video_url。
        self.details_urls: list[str] = []
        self.details_data: dict | None = None
        self.details_failure: Exception | None = None
        # 每个视频请求依次消费一个状态码；为空时按 Range 正常响应。
        self.video_statuses: list[int] = []
        self.video_failure: Exception | None = None

    def _ok(self, request: httpx2.Request, data) -> httpx2.Response:
        return httpx2.Response(200, json={"status": 0, "message": None, "data": data}, request=request)

    def _next_video_url(self) -> str:
        return self.details_urls.pop(0) if self.details_urls else self.default_video_url

    def _video_response(self, request: httpx2.Request) -> httpx2.Response:
        status = self.video_statuses.pop(0) if self.video_statuses else 200
        if status != 200:
            return httpx2.Response(status, content=b"upstream-error", request=request)
        total = len(self.video_body)
        range_header = request.headers.get("range")
        if range_header:
            start, end = _parse_range(range_header, total)
            if start >= total:
                return httpx2.Response(
                    416,
                    headers={"content-range": f"bytes */{total}", "accept-ranges": "bytes"},
                    content=b"",
                    request=request,
                )
            chunk = self.video_body[start : end + 1]
            return httpx2.Response(
                206,
                headers={
                    "content-type": "video/mp4",
                    "content-length": str(len(chunk)),
                    "content-range": f"bytes {start}-{end}/{total}",
                    "accept-ranges": "bytes",
                },
                content=chunk,
                request=request,
            )
        return httpx2.Response(
            200,
            headers={
                "content-type": "video/mp4",
                "content-length": str(total),
                "accept-ranges": "bytes",
            },
            content=self.video_body,
            request=request,
        )

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/appbase/token/register":
            self.token_requests += 1
            return self._ok(request, make_jwt(int(time.time()) + 15 * 24 * 3600))
        if path == "/appstorecontents/app/details_new":
            self.details_requests += 1
            if self.details_failure is not None:
                raise self.details_failure
            data = (
                copy.deepcopy(self.details_data)
                if self.details_data is not None
                else make_details_data(self._next_video_url())
            )
            return self._ok(request, data)
        if request.url.host == VIDEO_HOST:
            self.video_requests.append(request)
            if self.video_failure is not None:
                raise self.video_failure
            return self._video_response(request)
        raise AssertionError(f"未预期的上游请求: {request.url}")


class RecordingHandler(logging.Handler):
    """收集 ``winappsmanager.lenovo`` 的日志文本，用于断言不泄漏签名 URL。"""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.messages.append(record.getMessage())
        except Exception:  # noqa: BLE001 - 日志文本格式化失败不影响测试
            self.messages.append(str(record.msg))


def make_service_settings() -> SimpleNamespace:
    return SimpleNamespace(
        lenovo=SimpleNamespace(
            catalog_ttl_seconds=21600.0,
            catalog_refresh_interval_seconds=0.0,
            catalog_max_stale_seconds=604800.0,
            details_ttl_seconds=900.0,
            details_persistent_ttl_seconds=21600.0,
            search_cache_ttl_seconds=60.0,
            timeout_seconds=20.0,
            catalog_concurrency=1,
            image_cache_ttl_seconds=2592000.0,
            image_cache_max_bytes=268435456,
        )
    )


def make_app_settings() -> BackendSettings:
    return BackendSettings(
        cache=CacheConfig(persist=False),
        logging=LoggingConfig(enabled=False, console=False),
        refresh=RefreshConfig(on_startup=False, interval_seconds=0),
        lenovo=LenovoConfig(enabled=True),
    )


class EmptyProvider:
    async def get_releases(self, *, force_refresh=False):
        return []

    async def resolve_download(self, release):
        raise AssertionError("测试替身不解析下载地址")


def _assert_no_signed_url_leak(test: unittest.TestCase, text: str) -> None:
    test.assertNotIn("auth_key", text)
    test.assertNotIn("secret-key", text)


class ScreenshotVideoProviderTests(unittest.IsolatedAsyncioTestCase):
    """详情层：实时解析视频地址、URL 白名单校验、绝不缓存。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="wam-lenovo-video-details-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "lenovo"
        self.upstream = FakeUpstream()
        self.http = httpx2.AsyncClient(transport=httpx2.MockTransport(self.upstream.handler))
        self.addAsyncCleanup(self.http.aclose)
        self.raw = LenovoRawClient(self.http)
        self.provider = LenovoDetailsProvider(self.raw, self.directory)
        self.logs = RecordingHandler()
        logger = logging.getLogger(LOGGER_NAME)
        logger.addHandler(self.logs)
        self.addCleanup(logger.removeHandler, self.logs)

    async def test_resolves_a_fresh_url_on_every_call_without_caching(self):
        first = await self.provider.resolve_screenshot_video_url(SOFT_ID, 1)
        self.assertEqual(first, VIDEO_URL)
        self.assertEqual(self.upstream.details_requests, 1)

        # 第二次换成一个新的 auth_key：仍然实时请求详情，不复用任何缓存。
        self.upstream.details_urls = [VIDEO_URL_2]
        second = await self.provider.resolve_screenshot_video_url(SOFT_ID, 1)
        self.assertEqual(second, VIDEO_URL_2)
        self.assertEqual(self.upstream.details_requests, 2)

        # 详情解析不写磁盘缓存（缓存里本来就不会有视频 URL），也不经过详情缓存计数器。
        self.assertFalse((self.directory / "details").exists())
        self.assertEqual(self.provider.counters, {"hits": 0, "misses": 0, "stale_hits": 0})
        _assert_no_signed_url_leak(self, json.dumps(self.logs.messages))
        for path in self.directory.rglob("*") if self.directory.exists() else []:
            if path.is_file():
                _assert_no_signed_url_leak(self, path.read_text(encoding="utf-8", errors="ignore"))

    async def test_wildcard_lenovomm_cdn_hosts_are_allowed(self):
        self.upstream.details_data = make_details_data(
            "https://video2.lenovomm.cn/v/x.mp4?auth_key=ok"
        )
        resolved = await self.provider.resolve_screenshot_video_url(SOFT_ID, 1)
        self.assertEqual(resolved, "https://video2.lenovomm.cn/v/x.mp4?auth_key=ok")
        # 白名单常量本身覆盖显式主机与 *.lenovomm.cn 放宽规则。
        self.assertIn(VIDEO_HOST, ALLOWED_VIDEO_HOSTS)
        self.assertIn(".lenovomm.cn", ALLOWED_VIDEO_DOMAIN_SUFFIXES)

    async def test_image_and_out_of_range_indices_are_not_found(self):
        for index in (0, 2, 3, 99):
            with self.subTest(index=index):
                with self.assertRaises(LenovoNotFoundError) as context:
                    await self.provider.resolve_screenshot_video_url(SOFT_ID, index)
                _assert_no_signed_url_leak(self, str(context.exception))
        # 只有合法的视频索引才会真正请求详情；上面的三次 NotFound 已经各请求一次。
        self.assertEqual(self.upstream.details_requests, 4)

    async def test_invalid_index_is_not_found_without_a_network_call(self):
        for index in (-1, "1", None, True):
            with self.subTest(index=index):
                with self.assertRaises(LenovoNotFoundError):
                    await self.provider.resolve_screenshot_video_url(SOFT_ID, index)
        self.assertEqual(self.upstream.details_requests, 0)

    async def test_non_video_and_missing_urls_are_not_found(self):
        self.upstream.details_data = make_details_data(video_type="0")
        with self.assertRaises(LenovoNotFoundError):
            await self.provider.resolve_screenshot_video_url(SOFT_ID, 1)
        for missing in ("", "   ", None):
            with self.subTest(missing=missing):
                self.upstream.details_data = make_details_data(missing)
                with self.assertRaises(LenovoNotFoundError) as context:
                    await self.provider.resolve_screenshot_video_url(SOFT_ID, 1)
                _assert_no_signed_url_leak(self, str(context.exception))

    async def test_unsafe_or_non_whitelisted_urls_are_protocol_errors(self):
        invalid_urls = (
            VIDEO_URL.replace("https://", "http://"),
            "https://evil.example.com/v.mp4?auth_key=x",
            f"https://{VIDEO_HOST}.evil.example/v.mp4?auth_key=x",
            f"https://user:password@{VIDEO_HOST}{VIDEO_PATH}?auth_key=x",
            f"https://{VIDEO_HOST}{VIDEO_PATH}?auth_key=x\nInjected: 1",
            "https:///v.mp4?auth_key=x",
        )
        for url in invalid_urls:
            with self.subTest(url=url):
                self.upstream.details_data = make_details_data(url)
                with self.assertRaises(LenovoProtocolError) as context:
                    await self.provider.resolve_screenshot_video_url(SOFT_ID, 1)
                # 校验错误的文案只说明原因，绝不回显带 auth_key 的 URL。
                _assert_no_signed_url_leak(self, str(context.exception))

    async def test_mismatched_detail_identity_is_a_protocol_error(self):
        data = make_details_data()
        data["softID"] = "OTHER"
        self.upstream.details_data = data
        with self.assertRaises(LenovoProtocolError):
            await self.provider.resolve_screenshot_video_url(SOFT_ID, 1)

    async def test_malformed_capture_lists_are_protocol_errors(self):
        data = make_details_data()
        data["captureFileList"] = "broken"
        self.upstream.details_data = data
        with self.assertRaises(LenovoProtocolError):
            await self.provider.resolve_screenshot_video_url(SOFT_ID, 1)
        data["captureFileList"] = ["broken", "broken", "broken"]
        self.upstream.details_data = data
        with self.assertRaises(LenovoProtocolError):
            await self.provider.resolve_screenshot_video_url(SOFT_ID, 1)
        data["captureFileList"] = None
        self.upstream.details_data = data
        with self.assertRaises(LenovoNotFoundError):
            await self.provider.resolve_screenshot_video_url(SOFT_ID, 1)


class ScreenshotVideoApiTests(unittest.TestCase):
    """路由 + 真实 ``LenovoStoreService``：Range 透传、重试与错误映射。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="wam-lenovo-video-api-")
        self.addCleanup(self.temporary.cleanup)
        self.cache_directory = Path(self.temporary.name) / "cache"
        self.upstream = FakeUpstream()
        self.http = httpx2.AsyncClient(transport=httpx2.MockTransport(self.upstream.handler))
        self.service = create_lenovo_store_service(
            self.http,
            self.cache_directory,
            make_service_settings(),
            cache_enabled=True,
            cache_persist=True,
        )
        providers = {provider_id: EmptyProvider() for provider_id in PROVIDER_IDS}
        self.api = create_application(
            release_providers=providers,
            settings=make_app_settings(),
            extra_directory=FIXTURE_EXTRA_DIRECTORY,
            lenovo_service=self.service,
        )
        self.client = TestClient(self.api)
        self.client.__enter__()
        self.addCleanup(self._close)
        self.logs = RecordingHandler()
        logger = logging.getLogger(LOGGER_NAME)
        logger.addHandler(self.logs)
        self.addCleanup(logger.removeHandler, self.logs)

    def _close(self) -> None:
        self.client.__exit__(None, None, None)
        try:
            asyncio.run(self.service.shutdown())
        finally:
            asyncio.run(self.http.aclose())

    def _assert_no_leak_anywhere(self) -> None:
        _assert_no_signed_url_leak(self, "\n".join(self.logs.messages))
        self.assertFalse((self.cache_directory / "lenovo" / "details").exists())
        if self.cache_directory.exists():
            for path in self.cache_directory.rglob("*"):
                if path.is_file():
                    _assert_no_signed_url_leak(
                        self, path.read_text(encoding="utf-8", errors="ignore")
                    )

    def test_streams_the_video_with_forwarded_headers_and_no_store(self):
        response = self.client.get(VIDEO_URL_PATH)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, VIDEO_BYTES)
        self.assertEqual(response.headers["content-type"], "video/mp4")
        self.assertEqual(response.headers["content-length"], str(len(VIDEO_BYTES)))
        self.assertEqual(response.headers["accept-ranges"], "bytes")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.upstream.details_requests, 1)
        self.assertEqual(len(self.upstream.video_requests), 1)
        # 客户端没带 Range 时不向上游发送 Range。
        self.assertNotIn("range", self.upstream.video_requests[0].headers)
        # 视频地址每次实时解析：第二次请求仍然重新请求详情，不读详情缓存。
        self.client.get(VIDEO_URL_PATH)
        self.assertEqual(self.upstream.details_requests, 2)
        self.assertEqual(len(self.upstream.video_requests), 2)
        self._assert_no_leak_anywhere()

    def test_range_request_is_forwarded_and_partial_response_headers_are_preserved(self):
        range_header = "bytes=0-15"
        response = self.client.get(VIDEO_URL_PATH, headers={"Range": range_header})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, VIDEO_BYTES[:16])
        self.assertEqual(response.headers["content-range"], f"bytes 0-15/{len(VIDEO_BYTES)}")
        self.assertEqual(response.headers["content-length"], "16")
        self.assertEqual(response.headers["accept-ranges"], "bytes")
        self.assertEqual(response.headers["content-type"], "video/mp4")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(len(self.upstream.video_requests), 1)
        self.assertEqual(self.upstream.video_requests[0].headers["range"], range_header)
        self._assert_no_leak_anywhere()

    def test_unsatisfiable_range_status_416_is_passed_through(self):
        response = self.client.get(
            VIDEO_URL_PATH, headers={"Range": f"bytes={len(VIDEO_BYTES) + 100}-"}
        )
        self.assertEqual(response.status_code, 416)
        self.assertEqual(response.headers["content-range"], f"bytes */{len(VIDEO_BYTES)}")
        self.assertEqual(response.headers["accept-ranges"], "bytes")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.content, b"")

    def test_image_index_and_out_of_range_are_404(self):
        image = self.client.get(f"/api/v1/store/lenovo/apps/{SOFT_ID}/screenshots/0/video")
        self.assertEqual(image.status_code, 404)
        self.assertEqual(image.headers["cache-control"], "no-store")
        self.assertIn("不是视频", image.json()["detail"])
        _assert_no_signed_url_leak(self, image.text)

        out_of_range = self.client.get(
            f"/api/v1/store/lenovo/apps/{SOFT_ID}/screenshots/99/video"
        )
        self.assertEqual(out_of_range.status_code, 404)
        self.assertIn("找不到", out_of_range.json()["detail"])
        # 非视频/越界不会去请求视频 CDN。
        self.assertEqual(self.upstream.video_requests, [])

        negative = self.client.get(f"/api/v1/store/lenovo/apps/{SOFT_ID}/screenshots/-1/video")
        self.assertEqual(negative.status_code, 422)
        self.assertEqual(self.upstream.details_requests, 2)

    def test_expired_auth_key_re_resolves_once_and_retries(self):
        self.upstream.details_urls = [VIDEO_URL, VIDEO_URL_2]
        self.upstream.video_statuses = [403]
        response = self.client.get(VIDEO_URL_PATH, headers={"Range": "bytes=0-7"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, VIDEO_BYTES[:8])
        self.assertEqual(response.headers["cache-control"], "no-store")
        # 第一次 403 → 重新解析详情拿到新 auth_key → 重试一次。
        self.assertEqual(self.upstream.details_requests, 2)
        self.assertEqual(len(self.upstream.video_requests), 2)
        self.assertEqual(
            self.upstream.video_requests[0].url.query, b"auth_key=secret-key-1"
        )
        self.assertEqual(
            self.upstream.video_requests[1].url.query, b"auth_key=secret-key-2"
        )
        for request in self.upstream.video_requests:
            self.assertEqual(request.headers["range"], "bytes=0-7")
        self._assert_no_leak_anywhere()

    def test_persistent_auth_failure_is_a_gateway_error(self):
        self.upstream.video_statuses = [401, 410]
        response = self.client.get(VIDEO_URL_PATH)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.upstream.details_requests, 2)
        self.assertEqual(len(self.upstream.video_requests), 2)
        _assert_no_signed_url_leak(self, response.text)

    def test_unsupported_multi_range_is_not_forwarded(self):
        response = self.client.get(VIDEO_URL_PATH, headers={"Range": "bytes=0-1,4-5"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, VIDEO_BYTES)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn("range", self.upstream.video_requests[0].headers)

    def test_upstream_server_error_is_502(self):
        self.upstream.video_statuses = [500]
        response = self.client.get(VIDEO_URL_PATH)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("500", response.json()["detail"])
        # 只有“auth_key 过期”会重试；其它错误只请求一次。
        self.assertEqual(self.upstream.details_requests, 1)
        self.assertEqual(len(self.upstream.video_requests), 1)

    def test_upstream_video_404_is_a_gateway_error(self):
        # 视频 CDN 自身的 404 不当作“应用/截图不存在”（那是详情层结论），按上游错误 502。
        self.upstream.video_statuses = [404]
        response = self.client.get(VIDEO_URL_PATH)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.upstream.details_requests, 1)
        self.assertEqual(len(self.upstream.video_requests), 1)

    def test_upstream_timeout_is_504(self):
        self.upstream.video_failure = httpx2.ReadTimeout("slow upstream", request=None)
        response = self.client.get(VIDEO_URL_PATH)
        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.headers["cache-control"], "no-store")
        _assert_no_signed_url_leak(self, response.text)
        self.assertEqual(len(self.upstream.video_requests), 1)

    def test_head_returns_metadata_without_downloading_the_body(self):
        response = self.client.head(VIDEO_URL_PATH)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"")
        self.assertEqual(response.headers["content-type"], "video/mp4")
        self.assertEqual(response.headers["content-length"], str(len(VIDEO_BYTES)))
        self.assertEqual(response.headers["accept-ranges"], "bytes")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.upstream.details_requests, 1)
        self.assertEqual(len(self.upstream.video_requests), 1)
        self.assertTrue(
            any("联想视频代理完成" in message for message in self.logs.messages)
        )

    def test_video_timeouts_are_wider_than_the_regular_lenovo_requests(self):
        self.assertGreaterEqual(DEFAULT_VIDEO_READ_TIMEOUT_SECONDS, 60.0)
        self.assertGreaterEqual(DEFAULT_VIDEO_CONNECT_TIMEOUT_SECONDS, 5.0)
        timeout = self.service._video_timeout()
        self.assertEqual(timeout.read, DEFAULT_VIDEO_READ_TIMEOUT_SECONDS)
        self.assertEqual(timeout.connect, DEFAULT_VIDEO_CONNECT_TIMEOUT_SECONDS)


class DisabledVideoSourceTests(unittest.TestCase):
    """源禁用时视频路由（GET 与 HEAD）返回 503。"""

    def test_video_route_is_503_when_the_source_is_disabled(self):
        providers = {provider_id: EmptyProvider() for provider_id in PROVIDER_IDS}
        settings = BackendSettings(
            cache=CacheConfig(persist=False),
            logging=LoggingConfig(enabled=False, console=False),
            refresh=RefreshConfig(on_startup=False, interval_seconds=0),
            lenovo=LenovoConfig(enabled=False),
        )
        api = create_application(
            release_providers=providers,
            settings=settings,
            extra_directory=FIXTURE_EXTRA_DIRECTORY,
        )
        with TestClient(api) as client:
            response = client.get(VIDEO_URL_PATH)
            head = client.head(VIDEO_URL_PATH)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "联想应用商店源未启用。")
        self.assertEqual(head.status_code, 503)


if __name__ == "__main__":
    unittest.main()
