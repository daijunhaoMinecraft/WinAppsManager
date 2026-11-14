"""360 软件源详情缓存与下载地址解析的测试（MockTransport + 临时目录，禁止真实网络）。

覆盖：

- ``getinfo`` 字段映射：名称/厂商/描述/大小/评分/分类、``os.bit`` 架构、``os.ver`` 最低
  Windows（低于 7 按下限）、``snapshot`` 截图 https 规范化与代理路径；
- 详情内存/磁盘缓存、TTL、抓取失败回退陈旧详情、``clear``；
- ``resolve_download``：``p2p`` 的 ``|||`` 分隔、``multi_urls`` 回退、https 优先、扩展名过滤、
  文件名清洗与回退、大小/版本/架构/``sha256=None``；实时解析不写缓存；
- 404/502 类错误映射（未知 ID、无可用地址）。
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

import httpx2

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR.parent))

from Backend.models import Release
from Backend.soft360.catalog import app_id_for
from Backend.soft360.client import Soft360RawClient
from Backend.soft360.details import (
    DEFAULT_DETAILS_PERSISTENT_TTL_SECONDS,
    DEFAULT_DETAILS_TTL_SECONDS,
    Soft360DetailsProvider,
)
from Backend.soft360.errors import (
    Soft360NotFoundError,
    Soft360ProtocolError,
    Soft360UnavailableError,
)
from Backend.soft360.models import Soft360AppSummary


# ---------------------------------------------------------------------------
# 测试基础设施
# ---------------------------------------------------------------------------


class MutableClock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def summary(
    *,
    soft_id: str = "1010",
    name: str = "微信",
    size_bytes: int | None = 111,
) -> Soft360AppSummary:
    return Soft360AppSummary(
        id=app_id_for(soft_id),
        soft_id=soft_id,
        name=name,
        bname="WeChat",
        description="目录简介",
        category="聊天工具",
        category_id="149",
        size_bytes=size_bytes,
        score=50,
        icon_url="https://p0.qhimg.com/1010/72.png",
        icon_proxy_url=f"/api/v1/store/360/apps/{soft_id}/icon",
        architectures=["x86", "x64"],
        minimum_windows="7",
        tags=["聊天工具"],
    )


def info_payload(**overrides) -> dict:
    raw = {
        "name": "微信",
        "bname": "WeChat",
        "corp": "腾讯",
        "desc": "微信的详细描述",
        "brief": "微信简介",
        "soft_size": 123456789,
        "soft_score": 90,
        "baoku_cate": {"id": 149, "name": "聊天工具"},
        "snapshot": [
            "http://p0.qhimg.com/shot1.png",
            {"url": "https://p0.qhimg.com/shot2.png"},
            {"img": "not a url"},
            123,
        ],
        "edition_info": {
            "default_edition": {
                "version": "3.9.12",
                "edition_id": "ed-1",
                "os": {"bit": [64], "ver": [5, 6, 7, 8]},
                "down": {
                    "default": {
                        "name": "WeChat_3.9.12.exe",
                        "size": 123456789,
                        "md5": "0123456789abcdef0123456789abcdef",
                        "p2p": "http://dl.360.cn/a.exe|||https://dl.360.cn/b.exe",
                        "multi_urls": ["https://vendor.example.com/setup.exe"],
                    }
                },
            }
        },
    }
    raw.update(overrides)
    return raw


class FakeInfoUpstream:
    """``getinfo`` 替身：按 soft_id 返回固定详情；可切换为 HTTP 500。"""

    def __init__(self, infos: dict[str, dict]) -> None:
        self.infos = infos
        self.requests = 0
        self.http_error = False

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests += 1
        if self.http_error:
            return httpx2.Response(500, json={"errno": 500, "data": None}, request=request)
        wanted = request.url.params.get("soft_id", "").split(",")
        data = {soft_id: self.infos[soft_id] for soft_id in wanted if soft_id in self.infos}
        return httpx2.Response(200, json={"errno": 0, "data": data}, request=request)


def make_provider(
    upstream: FakeInfoUpstream,
    directory: Path,
    clock: MutableClock,
    *,
    cache_persist: bool = True,
) -> tuple[httpx2.AsyncClient, Soft360DetailsProvider]:
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(upstream.handler))
    provider = Soft360DetailsProvider(
        Soft360RawClient(http),
        directory,
        cache_persist=cache_persist,
        clock=clock,
    )
    return http, provider


# ---------------------------------------------------------------------------
# 详情映射与缓存
# ---------------------------------------------------------------------------


class DetailsMappingTests(unittest.IsolatedAsyncioTestCase):
    async def test_fields_are_mapped_from_getinfo(self):
        upstream = FakeInfoUpstream({"1010": info_payload()})
        clock = MutableClock()
        with tempfile.TemporaryDirectory() as directory:
            http, provider = make_provider(upstream, Path(directory), clock)
            async with http:
                fields = await provider.get_fields("1010")
                details = provider.build_details(summary(), fields)
        self.assertEqual(fields.name, "微信")
        self.assertEqual(fields.bname, "WeChat")
        self.assertEqual(fields.publisher, "腾讯")
        self.assertEqual(fields.version, "3.9.12")
        self.assertEqual(fields.brief, "微信简介")
        self.assertEqual(fields.long_description, "微信的详细描述")
        self.assertEqual(fields.size_bytes, 123456789)
        self.assertEqual(fields.score, 90)
        self.assertEqual(fields.category, "聊天工具")
        self.assertEqual(fields.category_id, "149")
        self.assertEqual(fields.architectures, ("x64",))
        self.assertEqual(fields.minimum_windows, "7")
        # 截图：只保留合法图片地址，http 统一规范化成 https，索引是 snapshot 原始顺序。
        self.assertEqual(
            [(item.index, item.source_url) for item in fields.screenshots],
            [(0, "https://p0.qhimg.com/shot1.png"), (1, "https://p0.qhimg.com/shot2.png")],
        )
        self.assertEqual(details.version, "3.9.12")
        self.assertEqual(details.publisher, "腾讯")
        self.assertEqual(details.long_description, "微信的详细描述")
        self.assertEqual(details.architectures, ["x64"])
        self.assertEqual(details.minimum_windows, "7")
        self.assertEqual(details.size_bytes, 123456789)
        self.assertEqual(
            [shot.url for shot in details.screenshots],
            [
                "/api/v1/store/360/apps/1010/screenshots/0",
                "/api/v1/store/360/apps/1010/screenshots/1",
            ],
        )

    async def test_missing_optional_fields_fall_back_to_the_catalog_summary(self):
        upstream = FakeInfoUpstream({"1010": {"name": "微信", "edition_info": {}}})
        clock = MutableClock()
        with tempfile.TemporaryDirectory() as directory:
            http, provider = make_provider(upstream, Path(directory), clock)
            async with http:
                fields = await provider.get_fields("1010")
                details = provider.build_details(summary(), fields)
        self.assertEqual(fields.version, "")
        self.assertEqual(fields.publisher, None)
        self.assertEqual(fields.architectures, ())
        self.assertIsNone(fields.minimum_windows)
        self.assertEqual(details.version, "以官网为准")
        self.assertEqual(details.architectures, ["x86", "x64"])
        self.assertEqual(details.minimum_windows, "7")
        self.assertEqual(details.size_bytes, 111)

    async def test_unknown_soft_id_maps_to_not_found(self):
        upstream = FakeInfoUpstream({})
        clock = MutableClock()
        with tempfile.TemporaryDirectory() as directory:
            http, provider = make_provider(upstream, Path(directory), clock)
            async with http:
                with self.assertRaises(Soft360NotFoundError):
                    await provider.get_fields("1010")

    async def test_invalid_snapshot_structure_maps_to_protocol_error(self):
        upstream = FakeInfoUpstream({"1010": info_payload(snapshot="not-a-list")})
        clock = MutableClock()
        with tempfile.TemporaryDirectory() as directory:
            http, provider = make_provider(upstream, Path(directory), clock)
            async with http:
                with self.assertRaises(Soft360ProtocolError):
                    await provider.get_fields("1010")

    async def test_memory_cache_avoids_repeated_requests(self):
        upstream = FakeInfoUpstream({"1010": info_payload()})
        clock = MutableClock()
        with tempfile.TemporaryDirectory() as directory:
            http, provider = make_provider(upstream, Path(directory), clock)
            async with http:
                await provider.get_fields("1010")
                await provider.get_fields("1010")
        self.assertEqual(upstream.requests, 1)
        self.assertEqual(provider.counters["hits"], 1)

    async def test_disk_cache_is_reused_by_a_new_provider(self):
        upstream = FakeInfoUpstream({"1010": info_payload()})
        clock = MutableClock()
        with tempfile.TemporaryDirectory() as directory:
            cache_directory = Path(directory)
            http, provider = make_provider(upstream, cache_directory, clock)
            async with http:
                await provider.get_fields("1010")
                self.assertEqual(upstream.requests, 1)
                http2, second = make_provider(upstream, cache_directory, clock)
                async with http2:
                    fields = await second.get_fields("1010")
                self.assertEqual(fields.version, "3.9.12")
            self.assertEqual(upstream.requests, 1)

    async def test_expired_memory_cache_is_served_from_the_fresh_disk_snapshot(self):
        upstream = FakeInfoUpstream({"1010": info_payload()})
        clock = MutableClock()
        with tempfile.TemporaryDirectory() as directory:
            http, provider = make_provider(upstream, Path(directory), clock)
            async with http:
                await provider.get_fields("1010")
                clock.now += DEFAULT_DETAILS_TTL_SECONDS + 1.0
                fields = await provider.get_fields("1010")
        self.assertEqual(fields.version, "3.9.12")
        self.assertEqual(upstream.requests, 1)

    async def test_fetch_failure_falls_back_to_a_stale_disk_snapshot(self):
        upstream = FakeInfoUpstream({"1010": info_payload()})
        clock = MutableClock()
        with tempfile.TemporaryDirectory() as directory:
            cache_directory = Path(directory)
            http, provider = make_provider(upstream, cache_directory, clock)
            async with http:
                await provider.get_fields("1010")
            # 超过内存 TTL 与磁盘 TTL；新 provider 内存为空，上游改成 500，
            # 只能回退到磁盘里的陈旧详情。
            clock.now += DEFAULT_DETAILS_PERSISTENT_TTL_SECONDS + 1.0
            upstream.http_error = True
            http2, second = make_provider(upstream, cache_directory, clock)
            async with http2:
                fields = await second.get_fields("1010")
        self.assertEqual(fields.version, "3.9.12")
        self.assertEqual(second.counters["stale_hits"], 1)

    async def test_disk_snapshot_expires_after_the_persistent_ttl(self):
        upstream = FakeInfoUpstream({"1010": info_payload()})
        clock = MutableClock()
        with tempfile.TemporaryDirectory() as directory:
            cache_directory = Path(directory)
            http, provider = make_provider(upstream, cache_directory, clock)
            async with http:
                await provider.get_fields("1010")
            clock.now += DEFAULT_DETAILS_PERSISTENT_TTL_SECONDS + 1.0
            http2, second = make_provider(upstream, cache_directory, clock)
            async with http2:
                await second.get_fields("1010")
        self.assertEqual(upstream.requests, 2)

    async def test_clear_removes_memory_and_disk_details(self):
        upstream = FakeInfoUpstream({"1010": info_payload()})
        clock = MutableClock()
        with tempfile.TemporaryDirectory() as directory:
            cache_directory = Path(directory)
            http, provider = make_provider(upstream, cache_directory, clock)
            async with http:
                await provider.get_fields("1010")
                deleted = await provider.clear()
                self.assertEqual(deleted, 1)
                details_directory = cache_directory / "details"
                self.assertFalse(any(details_directory.glob("*.json")))
                await provider.get_fields("1010")
        self.assertEqual(upstream.requests, 2)

    async def test_disk_cache_roundtrip_keeps_the_mapped_fields(self):
        upstream = FakeInfoUpstream({"1010": info_payload()})
        clock = MutableClock()
        with tempfile.TemporaryDirectory() as directory:
            cache_directory = Path(directory)
            http, provider = make_provider(upstream, cache_directory, clock)
            async with http:
                await provider.get_fields("1010")
            files = list((cache_directory / "details").glob("*.json"))
            self.assertEqual(len(files), 1)
            raw = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(raw["schema_version"], 1)
            self.assertEqual(raw["soft_id"], "1010")
            self.assertEqual(raw["version"], "3.9.12")
            self.assertEqual(raw["screenshots"][0]["source_url"], "https://p0.qhimg.com/shot1.png")
            # 下载地址绝不写入详情缓存。
            self.assertNotIn("download_url", json.dumps(raw))
            self.assertNotIn("p2p", json.dumps(raw))


# ---------------------------------------------------------------------------
# 下载地址解析
# ---------------------------------------------------------------------------


class DownloadResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def _resolve(self, info: dict, *, architecture: str = "x64", catalog_summary=None):
        upstream = FakeInfoUpstream({"1010": info})
        clock = MutableClock()
        with tempfile.TemporaryDirectory() as directory:
            http, provider = make_provider(upstream, Path(directory), clock)
            async with http:
                release = await provider.resolve_download(
                    "1010", catalog_summary or summary(), architecture
                )
        self.assertIsInstance(release, Release)
        return release, upstream.requests

    async def test_https_url_is_preferred_from_p2p(self):
        release, requests = await self._resolve(info_payload())
        self.assertEqual(str(release.download_url), "https://dl.360.cn/b.exe")
        self.assertEqual(release.version, "3.9.12")
        self.assertEqual(release.architecture, "x64")
        self.assertEqual(release.file_name, "WeChat_3.9.12.exe")
        self.assertEqual(release.size_bytes, 123456789)
        self.assertIsNone(release.sha256)
        self.assertEqual(release.id, "soft360-1010-3.9.12")
        self.assertEqual(requests, 1)

    async def test_p2p_pdown_descriptor_embeds_the_360_cdn_url(self):
        info = info_payload()
        down = info["edition_info"]["default_edition"]["down"]["default"]
        down["p2p"] = (
            "pdown://p2=539061FB7B9C8511|b2=123|p3=20"
            "|https://cdn-download.soft.360.cn/softmanager-installation/20260101/1010_x.exe"
            "|||https://vendor.example.com/setup.exe"
        )
        down.pop("multi_urls")
        release, _requests = await self._resolve(info)
        self.assertEqual(
            str(release.download_url),
            "https://cdn-download.soft.360.cn/softmanager-installation/20260101/1010_x.exe",
        )

    async def test_multi_urls_is_used_when_p2p_has_no_installer(self):
        info = info_payload()
        down = info["edition_info"]["default_edition"]["down"]["default"]
        down["p2p"] = "https://dl.360.cn/page.html"
        down["multi_urls"] = ["http://cdn.example.com/setup.msi"]
        release, _requests = await self._resolve(info)
        self.assertEqual(str(release.download_url), "http://cdn.example.com/setup.msi")

    async def test_unsafe_file_names_fall_back_to_a_safe_name(self):
        cases = (
            ("../../evil.exe", "微信_3.9.12.exe"),
            ("readme.txt", "微信_3.9.12.exe"),
            ("C:\\Windows\\evil.exe", "微信_3.9.12.exe"),
            ("", "微信_3.9.12.exe"),
            ("合规安装包.exe", "合规安装包.exe"),
        )
        for raw_name, expected in cases:
            with self.subTest(raw_name=raw_name):
                info = info_payload()
                info["edition_info"]["default_edition"]["down"]["default"]["name"] = raw_name
                release, _requests = await self._resolve(info)
                self.assertEqual(release.file_name, expected)

    async def test_size_falls_back_to_the_catalog_size(self):
        info = info_payload()
        down = info["edition_info"]["default_edition"]["down"]["default"]
        down.pop("size")
        release, _requests = await self._resolve(info, catalog_summary=summary(size_bytes=777))
        self.assertEqual(release.size_bytes, 777)

    async def test_version_falls_back_to_latest(self):
        info = info_payload()
        info["edition_info"]["default_edition"]["version"] = ""
        release, _requests = await self._resolve(info)
        self.assertEqual(release.version, "latest")
        self.assertEqual(release.id, "soft360-1010-latest")

    async def test_architecture_comes_from_os_bit(self):
        info = info_payload()
        info["edition_info"]["default_edition"]["os"] = {"bit": [32], "ver": [6, 7]}
        release, _requests = await self._resolve(info, architecture="x64")
        self.assertEqual(release.architecture, "x86")
        # os.bit 缺失时使用目录条目的架构（x64 优先）。
        del info["edition_info"]["default_edition"]["os"]
        release, _requests = await self._resolve(info, architecture="arm64")
        self.assertEqual(release.architecture, "x64")

    async def test_missing_installer_url_maps_to_unavailable(self):
        info = info_payload()
        down = info["edition_info"]["default_edition"]["down"]["default"]
        down.pop("p2p")
        down.pop("multi_urls")
        upstream = FakeInfoUpstream({"1010": info})
        clock = MutableClock()
        with tempfile.TemporaryDirectory() as directory:
            http, provider = make_provider(upstream, Path(directory), clock)
            async with http:
                with self.assertRaises(Soft360UnavailableError):
                    await provider.resolve_download("1010", summary())

    async def test_unknown_soft_id_maps_to_not_found(self):
        upstream = FakeInfoUpstream({})
        clock = MutableClock()
        with tempfile.TemporaryDirectory() as directory:
            http, provider = make_provider(upstream, Path(directory), clock)
            async with http:
                with self.assertRaises(Soft360NotFoundError):
                    await provider.resolve_download("1010", summary())

    async def test_resolution_does_not_read_or_write_the_details_cache(self):
        upstream = FakeInfoUpstream({"1010": info_payload()})
        clock = MutableClock()
        with tempfile.TemporaryDirectory() as directory:
            cache_directory = Path(directory)
            http, provider = make_provider(upstream, cache_directory, clock)
            async with http:
                await provider.resolve_download("1010", summary())
                await provider.resolve_download("1010", summary())
                self.assertEqual(upstream.requests, 2)
                details_directory = cache_directory / "details"
                self.assertFalse(details_directory.exists())

    async def test_printable_architecture_argument_is_validated(self):
        upstream = FakeInfoUpstream({"1010": info_payload()})
        clock = MutableClock()
        with tempfile.TemporaryDirectory() as directory:
            http, provider = make_provider(upstream, Path(directory), clock)
            async with http:
                release = await provider.resolve_download("1010", summary(), "arm64")
        # arm64 不在 os.bit 支持的架构里，回退到 os.bit 支持的首个架构。
        self.assertEqual(release.architecture, "x64")


if __name__ == "__main__":
    unittest.main()
