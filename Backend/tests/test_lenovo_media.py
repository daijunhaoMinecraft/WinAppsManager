"""联想图片代理缓存测试：UA、host 白名单、格式、磁盘往返、TTL、淘汰、ETag。"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import sys
import tempfile
import unittest

import httpx2

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR.parent))

from Backend.lenovo.errors import (
    LenovoProtocolError,
    LenovoTimeoutError,
    LenovoUnavailableError,
)
from Backend.lenovo.media import (
    BROWSER_USER_AGENT,
    LenovoMediaCache,
    detect_image_type,
    validate_media_url,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 64
JPG_BYTES = b"\xff\xd8\xff\xe0" + b"1" * 64
ICON_BYTES = b"\x00\x00\x01\x00" + b"2" * 64
GIF_BYTES = b"GIF89a" + b"3" * 64
BMP_BYTES = b"BM" + b"4" * 64


class MutableClock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class MediaServer:
    def __init__(self, clock: MutableClock) -> None:
        self.clock = clock
        self.requests: list[httpx2.Request] = []
        self.status = 200
        self.content_type: str | None = "image/png"
        self.data: bytes = PNG_BYTES
        self.headers: dict[str, str] = {}
        self.failure: Exception | None = None

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        headers = dict(self.headers)
        if self.content_type is not None:
            headers["content-type"] = self.content_type
        return httpx2.Response(self.status, content=self.data, headers=headers, request=request)


class LenovoMediaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.clock = MutableClock()
        self.temporary = tempfile.TemporaryDirectory(prefix="wam-lenovo-media-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "lenovo"
        self.server = MediaServer(self.clock)
        self.client = httpx2.AsyncClient(transport=httpx2.MockTransport(self.server.handler))
        self.addAsyncCleanup(self.client.aclose)

    def make_cache(self, **kwargs) -> LenovoMediaCache:
        return LenovoMediaCache(self.client, self.directory, clock=self.clock, **kwargs)

    @staticmethod
    def icon_url(name: str = "logo.png") -> str:
        return f"https://lsw-fast.lenovo.com.cn/appstore/apps/adp/logo/{name}"

    async def test_browser_user_agent_is_sent(self):
        cache = self.make_cache()
        payload = await cache.fetch(self.icon_url())
        self.assertEqual(payload.content, PNG_BYTES)
        self.assertEqual(payload.content_type, "image/png")
        self.assertEqual(self.server.requests[0].headers["user-agent"], BROWSER_USER_AGENT)
        self.assertNotIn("curl", self.server.requests[0].headers["user-agent"].lower())

    async def test_etag_is_stable_sha256_prefix(self):
        cache = self.make_cache()
        url = self.icon_url()
        payload = await cache.fetch(url)
        expected = sha256(url.encode("utf-8")).hexdigest()[:32]
        self.assertEqual(payload.etag, expected)
        self.assertEqual(LenovoMediaCache.etag_for(url), expected)

    def test_host_whitelist(self):
        allowed = [
            "https://lsw-fast.lenovo.com.cn/a.png",
            "http://imgs.lenovomm.cn/a.jpg",
            "https://pc1.gtimg.com/a.gif",
            "https://pc-store-video.lenovomm.cn/a.png",
            "https://cdn.lenovomm.cn/a.png",
            "https://foo.lenovo.com.cn/a.png",
        ]
        for url in allowed:
            with self.subTest(url=url):
                self.assertEqual(validate_media_url(url), url)

        rejected = [
            "https://evil.example/a.png",
            "https://lenovomm.cn/a.png",
            "https://imgs.lenovomm.cn.evil.com/a.png",
            "https://evil.com/imgs.lenovomm.cn/a.png",
            "https://evil-lenovomm.cn/a.png",
            "ftp://imgs.lenovomm.cn/a.png",
            "file:///C:/Windows/a.png",
            "https://user:pass@imgs.lenovomm.cn/a.png",
            "https:///a.png",
            "not a url",
            "",
            None,
            123,
        ]
        for url in rejected:
            with self.subTest(url=url), self.assertRaises(LenovoProtocolError):
                validate_media_url(url)

    async def test_non_whitelisted_host_is_rejected_without_request(self):
        cache = self.make_cache()
        with self.assertRaises(LenovoProtocolError):
            await cache.fetch("https://evil.example/a.png")
        self.assertEqual(self.server.requests, [])

    async def test_format_detection_via_content_type_and_url_suffix(self):
        cases = [
            ("image/png", "https://lsw-fast.lenovo.com.cn/a", (PNG_BYTES, "image/png")),
            ("image/jpeg", "https://lsw-fast.lenovo.com.cn/a", (JPG_BYTES, "image/jpeg")),
            ("image/x-icon", "https://lsw-fast.lenovo.com.cn/a", (ICON_BYTES, "image/x-icon")),
            ("image/gif", "https://lsw-fast.lenovo.com.cn/a", (GIF_BYTES, "image/gif")),
            ("image/bmp", "https://lsw-fast.lenovo.com.cn/a", (BMP_BYTES, "image/bmp")),
            ("application/octet-stream", "https://lsw-fast.lenovo.com.cn/a.jpg", (JPG_BYTES, "image/jpeg")),
            (None, "https://lsw-fast.lenovo.com.cn/a.ico", (ICON_BYTES, "image/x-icon")),
        ]
        for index, (content_type, url, (data, expected_type)) in enumerate(cases):
            with self.subTest(content_type=content_type, url=url):
                self.server.content_type = content_type
                self.server.data = data
                cache = self.make_cache(cache_enabled=False)
                payload = await cache.fetch(url + (f"?v={index}" if "?" not in url else f"&v={index}"))
                self.assertEqual(payload.content_type, expected_type)

    def test_detect_image_type_rejects_unsupported_formats(self):
        self.assertIsNone(detect_image_type("image/webp", "https://imgs.lenovomm.cn/a.webp"))
        self.assertIsNone(detect_image_type("text/html", "https://imgs.lenovomm.cn/a.png"))
        self.assertIsNone(detect_image_type("image/svg+xml", "https://imgs.lenovomm.cn/a.svg"))
        self.assertEqual(detect_image_type("IMAGE/PNG; charset=binary", "x"), ("image/png", "png"))

    async def test_upstream_errors_are_mapped(self):
        cache = self.make_cache(cache_enabled=False)
        self.server.status = 403
        with self.assertRaises(LenovoUnavailableError):
            await cache.fetch(self.icon_url())
        self.server.status = 500
        with self.assertRaises(LenovoUnavailableError):
            await cache.fetch(self.icon_url())
        self.server.status = 200
        self.server.failure = httpx2.ReadTimeout("slow", request=None)
        with self.assertRaises(LenovoTimeoutError):
            await cache.fetch(self.icon_url())
        self.server.failure = httpx2.ConnectError("down")
        with self.assertRaises(LenovoUnavailableError):
            await cache.fetch(self.icon_url())
        self.server.failure = None
        self.server.data = b""
        with self.assertRaises(LenovoProtocolError):
            await cache.fetch(self.icon_url())
        self.server.data = PNG_BYTES
        self.server.content_type = "text/html"
        with self.assertRaises(LenovoProtocolError):
            await cache.fetch(self.icon_url())
        self.server.content_type = "image/png"
        # 单图大小上限（这里把上限压到 10 字节）。
        small = self.make_cache(cache_enabled=False, max_image_bytes=10)
        with self.assertRaises(LenovoProtocolError):
            await small.fetch(self.icon_url())
        self.server.headers = {"content-length": "999999"}
        with self.assertRaises(LenovoProtocolError):
            await small.fetch(self.icon_url())

    async def test_disk_roundtrip_and_sidecar_metadata(self):
        url = self.icon_url()
        cache = self.make_cache()
        first = await cache.fetch(url)
        key = sha256(url.encode("utf-8")).hexdigest()
        media_directory = self.directory / "media"
        self.assertTrue((media_directory / f"{key}.png").is_file())
        sidecar = json.loads((media_directory / f"{key}.json").read_text(encoding="utf-8"))
        self.assertEqual(sidecar["url"], url)
        self.assertEqual(sidecar["content_type"], "image/png")
        self.assertEqual(sidecar["fetched_at"], self.clock.now)
        self.assertEqual(sidecar["size"], len(PNG_BYTES))

        def exploding_handler(request: httpx2.Request) -> httpx2.Response:
            raise AssertionError(f"不应访问网络: {request.url}")

        client = httpx2.AsyncClient(transport=httpx2.MockTransport(exploding_handler))
        self.addAsyncCleanup(client.aclose)
        restarted = LenovoMediaCache(client, self.directory, clock=self.clock)
        payload = await restarted.fetch(url)
        self.assertEqual(payload.content, first.content)
        self.assertEqual(payload.content_type, "image/png")
        self.assertEqual(payload.etag, first.etag)
        self.assertEqual(restarted.counters["disk_hits"], 1)

    async def test_expired_disk_entry_is_refetched(self):
        cache = self.make_cache(ttl_seconds=60.0)
        url = self.icon_url()
        await cache.fetch(url)
        self.assertEqual(len(self.server.requests), 1)
        self.clock.now += 61
        await cache.fetch(url)
        self.assertEqual(len(self.server.requests), 2)

    async def test_capacity_eviction_removes_the_oldest_entry(self):
        size = len(PNG_BYTES)
        cache = self.make_cache(ttl_seconds=3600.0, max_bytes=size + 10)
        old_url = self.icon_url("old.png")
        new_url = self.icon_url("new.png")
        await cache.fetch(old_url)
        self.clock.now += 10
        await cache.fetch(new_url)
        self.assertEqual(cache.counters["evicted"], 1)
        self.assertEqual(cache.counters["disk_entries"], 1)
        media_directory = self.directory / "media"
        media_files = [path for path in media_directory.iterdir() if path.suffix == ".png"]
        self.assertEqual(len(media_files), 1)
        self.assertEqual(media_files[0].stem, sha256(new_url.encode("utf-8")).hexdigest())
        # 被淘汰的旧图需要重新下载：用新实例绕过内存缓存，只保留磁盘状态。
        restarted = self.make_cache(ttl_seconds=3600.0, max_bytes=size + 10)
        requests_before = len(self.server.requests)
        await restarted.fetch(old_url)
        self.assertEqual(len(self.server.requests), requests_before + 1)

    async def test_memory_lru_eviction(self):
        cache = self.make_cache(cache_persist=False, memory_max_entries=1, ttl_seconds=3600.0)
        first_url = self.icon_url("first.png")
        second_url = self.icon_url("second.png")
        await cache.fetch(first_url)
        await cache.fetch(second_url)
        self.assertEqual(cache.counters["memory_entries"], 1)
        requests_before = len(self.server.requests)
        await cache.fetch(first_url)
        self.assertEqual(len(self.server.requests), requests_before + 1)
        # 重新拉取 first 又把 second 挤出内存，所以再次请求要走网络。
        await cache.fetch(second_url)
        self.assertEqual(len(self.server.requests), requests_before + 2)

    async def test_memory_cache_hit_avoids_network(self):
        cache = self.make_cache(cache_persist=False)
        url = self.icon_url()
        await cache.fetch(url)
        await cache.fetch(url)
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(cache.counters["hits"], 1)
        self.assertFalse((self.directory / "media").exists())

    async def test_cache_disabled_never_writes_memory_or_disk(self):
        cache = self.make_cache(cache_enabled=False)
        url = self.icon_url()
        await cache.fetch(url)
        await cache.fetch(url)
        self.assertEqual(len(self.server.requests), 2)
        self.assertFalse((self.directory / "media").exists())
        self.assertEqual(cache.counters["memory_entries"], 0)

    async def test_redirects_must_stay_inside_the_whitelist(self):
        def handler(request: httpx2.Request) -> httpx2.Response:
            if request.url.host == "lsw-fast.lenovo.com.cn":
                return httpx2.Response(
                    302,
                    headers={"location": "https://imgs.lenovomm.cn/new.png"},
                    request=request,
                )
            return httpx2.Response(
                200, content=PNG_BYTES, headers={"content-type": "image/png"}, request=request
            )

        client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        cache = LenovoMediaCache(client, None)
        payload = await cache.fetch("https://lsw-fast.lenovo.com.cn/old.png")
        self.assertEqual(payload.content, PNG_BYTES)

        def evil_redirect(request: httpx2.Request) -> httpx2.Response:
            if request.url.host == "lsw-fast.lenovo.com.cn":
                return httpx2.Response(
                    302, headers={"location": "https://evil.example/new.png"}, request=request
                )
            return httpx2.Response(
                200, content=PNG_BYTES, headers={"content-type": "image/png"}, request=request
            )

        evil_client = httpx2.AsyncClient(transport=httpx2.MockTransport(evil_redirect))
        self.addAsyncCleanup(evil_client.aclose)
        evil_cache = LenovoMediaCache(evil_client, None)
        with self.assertRaises(LenovoProtocolError):
            await evil_cache.fetch("https://lsw-fast.lenovo.com.cn/old.png")

    async def test_clear_removes_disk_and_memory(self):
        cache = self.make_cache()
        await cache.fetch(self.icon_url("a.png"))
        await cache.fetch(self.icon_url("b.png"))
        self.assertEqual(cache.counters["disk_entries"], 2)
        deleted = await cache.clear()
        self.assertGreaterEqual(deleted, 2)
        self.assertEqual(cache.counters["disk_entries"], 0)
        self.assertEqual(cache.counters["memory_entries"], 0)
        self.assertEqual(list((self.directory / "media").iterdir()), [])
        await cache.fetch(self.icon_url("a.png"))
        self.assertEqual(len(self.server.requests), 3)

    async def test_query_string_is_part_of_the_cache_key(self):
        cache = self.make_cache(cache_persist=False)
        first = "https://imgs.lenovomm.cn/a.png?x-oss-process=image/resize,w_200"
        second = "https://imgs.lenovomm.cn/a.png?x-oss-process=image/resize,w_400"
        await cache.fetch(first)
        await cache.fetch(second)
        self.assertEqual(len(self.server.requests), 2)
        self.assertNotEqual(
            LenovoMediaCache.etag_for(first),
            LenovoMediaCache.etag_for(second),
        )


if __name__ == "__main__":
    unittest.main()
