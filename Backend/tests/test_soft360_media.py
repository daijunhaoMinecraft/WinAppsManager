"""360 软件源图片代理缓存的测试（MockTransport + 临时目录，禁止真实网络）。

覆盖：

- http 图片地址规范化为 https 后再请求；host 白名单（qhimg.com / mediav.com / qhres2.com）；
- Content-Type 与 URL 后缀识别、单图大小上限、空响应；
- 内存缓存命中、磁盘缓存跨实例复用、``clear`` 清理；
- 非法 URL / 白名单外主机 / 重定向到白名单外主机 / 超时 / HTTP 错误的异常映射；
- ETag 稳定性（同一 URL 恒定）。
"""

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

from Backend.soft360.errors import (
    Soft360ProtocolError,
    Soft360TimeoutError,
    Soft360UnavailableError,
)
from Backend.soft360.media import (
    MEDIA_SCHEMA_VERSION,
    Soft360MediaCache,
    detect_image_type,
    normalize_media_url,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-png-body"
IMAGE_URL = "http://p0.qhimg.com/logo/72.png"


class MutableClock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeImageUpstream:
    def __init__(self, *, content: bytes = PNG_BYTES, content_type: str = "image/png") -> None:
        self.content = content
        self.content_type = content_type
        self.urls: list[str] = []
        self.http_error = False
        self.timeout = False
        self.redirect_to: str | None = None

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.urls.append(str(request.url))
        if self.timeout:
            raise httpx2.TimeoutException("slow image")
        if self.redirect_to is not None and len(self.urls) == 1:
            return httpx2.Response(302, headers={"location": self.redirect_to}, request=request)
        if self.http_error:
            return httpx2.Response(500, content=b"boom", request=request)
        return httpx2.Response(
            200,
            content=self.content,
            headers={"Content-Type": self.content_type},
            request=request,
        )


def make_cache(
    upstream: FakeImageUpstream,
    directory: Path | None,
    clock: MutableClock,
    *,
    cache_persist: bool = True,
) -> tuple[httpx2.AsyncClient, Soft360MediaCache]:
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(upstream.handler))
    cache = Soft360MediaCache(
        http,
        directory,
        cache_persist=cache_persist,
        clock=clock,
        timeout_seconds=5.0,
    )
    return http, cache


class MediaFetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_url_is_fetched_over_https_and_cached_in_memory(self):
        upstream = FakeImageUpstream()
        clock = MutableClock()
        with tempfile.TemporaryDirectory() as directory:
            http, cache = make_cache(upstream, Path(directory), clock)
            async with http:
                payload = await cache.fetch(IMAGE_URL)
                cached = await cache.fetch(IMAGE_URL)
        self.assertEqual(payload.content, PNG_BYTES)
        self.assertEqual(payload.content_type, "image/png")
        self.assertEqual(payload.etag, sha256("https://p0.qhimg.com/logo/72.png".encode()).hexdigest()[:32])
        self.assertEqual(cached, payload)
        self.assertEqual(upstream.urls, ["https://p0.qhimg.com/logo/72.png"])
        self.assertEqual(cache.counters["hits"], 1)

    async def test_disk_cache_survives_a_new_instance(self):
        upstream = FakeImageUpstream()
        clock = MutableClock()
        with tempfile.TemporaryDirectory() as directory:
            cache_directory = Path(directory)
            http, cache = make_cache(upstream, cache_directory, clock)
            async with http:
                await cache.fetch(IMAGE_URL)
            media_directory = cache_directory / "media"
            self.assertEqual(len(list(media_directory.glob("*.png"))), 1)
            sidecar = next(media_directory.glob("*.json"))
            raw = json.loads(sidecar.read_text(encoding="utf-8"))
            self.assertEqual(raw["schema_version"], MEDIA_SCHEMA_VERSION)
            self.assertEqual(raw["url"], "https://p0.qhimg.com/logo/72.png")
            http2, second = make_cache(upstream, cache_directory, clock)
            async with http2:
                payload = await second.fetch(IMAGE_URL)
        self.assertEqual(payload.content, PNG_BYTES)
        self.assertEqual(len(upstream.urls), 1)
        self.assertEqual(second.counters["disk_hits"], 1)

    async def test_clear_removes_memory_and_disk_entries(self):
        upstream = FakeImageUpstream()
        clock = MutableClock()
        with tempfile.TemporaryDirectory() as directory:
            cache_directory = Path(directory)
            http, cache = make_cache(upstream, cache_directory, clock)
            async with http:
                await cache.fetch(IMAGE_URL)
                deleted = await cache.clear()
                self.assertGreaterEqual(deleted, 1)
                self.assertEqual(cache.counters["disk_entries"], 0)
                await cache.fetch(IMAGE_URL)
        self.assertEqual(len(upstream.urls), 2)
        self.assertEqual(cache.counters["disk_entries"], 1)

    async def test_generic_content_type_uses_the_url_suffix(self):
        upstream = FakeImageUpstream(content=b"\xff\xd8\xff\xe0fake-jpeg", content_type="application/octet-stream")
        clock = MutableClock()
        with tempfile.TemporaryDirectory() as directory:
            http, cache = make_cache(upstream, Path(directory), clock)
            async with http:
                payload = await cache.fetch("https://p0.qhimg.com/shot.jpg")
        self.assertEqual(payload.content_type, "image/jpeg")

    async def test_unsupported_content_type_maps_to_protocol_error(self):
        upstream = FakeImageUpstream(content=b"<html>not image</html>", content_type="text/html")
        clock = MutableClock()
        with tempfile.TemporaryDirectory() as directory:
            http, cache = make_cache(upstream, Path(directory), clock)
            async with http:
                with self.assertRaises(Soft360ProtocolError):
                    await cache.fetch(IMAGE_URL)

    async def test_empty_image_maps_to_protocol_error(self):
        upstream = FakeImageUpstream(content=b"")
        clock = MutableClock()
        with tempfile.TemporaryDirectory() as directory:
            http, cache = make_cache(upstream, Path(directory), clock)
            async with http:
                with self.assertRaises(Soft360ProtocolError):
                    await cache.fetch(IMAGE_URL)

    async def test_oversized_content_length_maps_to_protocol_error(self):
        upstream = FakeImageUpstream()

        def handler(request):
            return httpx2.Response(
                200,
                content=b"x",
                headers={"Content-Type": "image/png", "Content-Length": str(11 * 1024 * 1024)},
                request=request,
            )

        clock = MutableClock()
        with tempfile.TemporaryDirectory() as directory:
            http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
            cache = Soft360MediaCache(http, Path(directory), clock=clock)
            async with http:
                with self.assertRaises(Soft360ProtocolError):
                    await cache.fetch(IMAGE_URL)


class MediaValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_is_normalized_to_https(self):
        self.assertEqual(
            normalize_media_url("http://p0.qhimg.com/a.png"), "https://p0.qhimg.com/a.png"
        )
        self.assertEqual(
            normalize_media_url("https://p0.qhimg.com/a.png?x=1"), "https://p0.qhimg.com/a.png?x=1"
        )
        self.assertEqual(
            normalize_media_url("http://s1.mediav.com/a.jpg"), "https://s1.mediav.com/a.jpg"
        )
        self.assertEqual(
            normalize_media_url("https://img.qhres2.com/a.png"), "https://img.qhres2.com/a.png"
        )

    async def test_disallowed_hosts_and_invalid_urls_are_rejected(self):
        upstream = FakeImageUpstream()
        clock = MutableClock()
        with tempfile.TemporaryDirectory() as directory:
            http, cache = make_cache(upstream, Path(directory), clock)
            async with http:
                for url in (
                    "https://evil.example.com/a.png",
                    "http://qhimg.com.evil.example.com/a.png",
                    "ftp://p0.qhimg.com/a.png",
                    "https://user:pass@p0.qhimg.com/a.png",
                    "not a url",
                    None,
                    123,
                ):
                    with self.subTest(url=url), self.assertRaises(Soft360ProtocolError):
                        await cache.fetch(url)
        self.assertEqual(upstream.urls, [])

    async def test_redirect_to_a_disallowed_host_is_rejected(self):
        upstream = FakeImageUpstream()
        upstream.redirect_to = "https://evil.example.com/a.png"
        clock = MutableClock()
        with tempfile.TemporaryDirectory() as directory:
            http, cache = make_cache(upstream, Path(directory), clock)
            async with http:
                with self.assertRaises(Soft360ProtocolError):
                    await cache.fetch(IMAGE_URL)

    async def test_timeout_and_http_errors_are_mapped(self):
        clock = MutableClock()
        cases = (
            ("timeout", Soft360TimeoutError()),
            ("http_error", Soft360UnavailableError()),
        )
        for mode, expected in cases:
            upstream = FakeImageUpstream()
            setattr(upstream, mode, True)
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                http, cache = make_cache(upstream, Path(directory), clock)
                async with http:
                    with self.assertRaises(type(expected)):
                        await cache.fetch(IMAGE_URL)

    async def test_etag_is_stable_and_detect_image_type_handles_suffixes(self):
        self.assertEqual(
            Soft360MediaCache.etag_for(IMAGE_URL), Soft360MediaCache.etag_for(IMAGE_URL)
        )
        self.assertNotEqual(
            Soft360MediaCache.etag_for(IMAGE_URL), Soft360MediaCache.etag_for(IMAGE_URL + "?v=2")
        )
        self.assertEqual(detect_image_type("image/png", "https://x/a"), ("image/png", "png"))
        self.assertEqual(detect_image_type("", "https://x/a.webp"), ("image/webp", "webp"))
        self.assertIsNone(detect_image_type("text/html", "https://x/a.txt"))


if __name__ == "__main__":
    unittest.main()
