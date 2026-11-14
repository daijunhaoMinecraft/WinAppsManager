"""联想原始接口客户端 / token 管理 / 下载地址解析测试。

全部使用 ``httpx2.MockTransport`` 与本地临时目录，绝不访问真实联想接口。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
import re
import sys
import tempfile
import time
import unittest

import httpx2

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR.parent))

from Backend.lenovo.client import LenovoRawClient
from Backend.lenovo.details import DOWNLOAD_PRIORITY, LenovoDetailsProvider
from Backend.lenovo.errors import (
    LenovoNotFoundError,
    LenovoProtocolError,
    LenovoTimeoutError,
    LenovoUnavailableError,
)
from Backend.lenovo.models import StoreAppSummary
from Backend.lenovo.token import TOKEN_EXPIRY_MARGIN_SECONDS, LenovoTokenManager, parse_jwt_exp


# ---------------------------------------------------------------------------
# 测试基础设施
# ---------------------------------------------------------------------------


class MutableClock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def make_jwt(exp: int, serial: int = 0) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"typ": "JWT", "alg": "HS256"}).encode()).rstrip(b"=")
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp, "os": "win10"}).encode()).rstrip(b"=")
    return f"{header.decode()}.{payload.decode()}.signature-{serial}"


def summary(
    *,
    soft_id: str = "L105867",
    name: str = "QQ",
    version: str = "9.9.37.54395",
    architectures: tuple[str, ...] = ("x64",),
    size_bytes: int | None = 334_442_672,
) -> StoreAppSummary:
    return StoreAppSummary(
        id=f"lenovo-{soft_id.lower()}",
        soft_id=soft_id,
        name=name,
        description="测试应用",
        category="聊天社交",
        category_id="1",
        group="software",
        version=version,
        size_display="318.95 MB",
        size_bytes=size_bytes,
        platforms="Windows 10 / 11 · 64 位",
        architectures=list(architectures),
        icon_url=f"https://lsw-fast.lenovo.com.cn/logo/{soft_id}.png",
        icon_proxy_url=f"/api/v1/store/lenovo/apps/{soft_id}/icon",
        icon_text=name[:1],
        icon_background="#1679C8",
    )


def json_response(request: httpx2.Request, payload: dict, status: int = 200) -> httpx2.Response:
    return httpx2.Response(status, json=payload, request=request)


COMPOSITE_TREE = [
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
                ],
            }
        ],
    }
]

APP_ITEM = {
    "softID": "L105867",
    "softName": "QQ",
    "version": "9.9.37.54395",
    "installFileSize": "318.95 MB",
    "introduction": "QQ9全新上线",
    "logoFile": "https://lsw-fast.lenovo.com.cn/logo/qq.png",
    "captureFileList": [],
    "score": "60",
    "downloadCount": "1.01亿次",
    "supportOsbit": ["64"],
    "supportPlatform": ["win10", "win11"],
    "stable": "stable",
    "classID": "1",
}


class FakeLenovoServer:
    """一个可配置的联想接口假服务端，按 URL 路径返回 fixture。"""

    def __init__(self, clock: MutableClock) -> None:
        self.clock = clock
        self.requests: list[httpx2.Request] = []
        self.register_bodies: list[dict] = []
        self.register_count = 0
        self.token_serial = 0
        self.exp_offset = 15 * 24 * 3600
        self.last_token = ""
        self.valid_tokens: set[str] = set()
        self.reject_all_tokens = False
        self.class_list_status: object = 0
        self.details_payload: dict | None = None
        self.search_payload: dict | None = None
        self.download_payload: dict | None = None
        self.failure: Exception | None = None
        self.http_status = 200
        self.raw_body: bytes | None = None

    # -- 记录 -------------------------------------------------------------

    def _issue_token(self) -> str:
        exp = int(self.clock.now) + self.exp_offset
        token = make_jwt(exp, serial=self.token_serial)
        self.token_serial += 1
        self.last_token = token
        if not self.reject_all_tokens:
            self.valid_tokens.add(token)
        return token

    def _token_error(self, request: httpx2.Request) -> httpx2.Response | None:
        token = request.headers.get("token")
        if self.reject_all_tokens or token not in self.valid_tokens:
            return json_response(request, {"status": "502", "message": "token invalid"})
        return None

    def _data_response(self, request: httpx2.Request, data) -> httpx2.Response:
        return json_response(request, {"status": 0, "message": None, "data": data})

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        path = request.url.path
        if path == "/appbase/token/register":
            body = json.loads(request.content.decode("utf-8"))
            self.register_bodies.append(body)
            self.register_count += 1
            if self.raw_body is not None:
                return httpx2.Response(self.http_status, content=self.raw_body, request=request)
            return self._data_response(request, self._issue_token())
        if self.http_status != 200:
            return httpx2.Response(self.http_status, request=request)
        if self.raw_body is not None:
            return httpx2.Response(200, content=self.raw_body, request=request)
        if path == "/classifyservice/class/composite_class_list":
            if (error := self._token_error(request)) is not None:
                return error
            return json_response(request, {"status": self.class_list_status, "message": None, "data": COMPOSITE_TREE})
        if path == "/classifyservice/class/composite_class_app_list":
            if (error := self._token_error(request)) is not None:
                return error
            return self._data_response(request, {"count": 1, "apps": [APP_ITEM]})
        if path == "/appstorecontents/app/details_new":
            if (error := self._token_error(request)) is not None:
                return error
            payload = self.details_payload if self.details_payload is not None else {
                "status": 0,
                "message": None,
                "data": {"softID": "L105867", "softName": "QQ"},
            }
            return json_response(request, payload)
        if path == "/dlservice/getPcSoftDownloadUrlList":
            payload = self.download_payload if self.download_payload is not None else {
                "status": 0,
                "message": "",
                "data": {"downloadUrls": []},
            }
            return json_response(request, payload)
        if path == "/searchcontent/fusion/v2/app_search":
            if (error := self._token_error(request)) is not None:
                return error
            payload = self.search_payload if self.search_payload is not None else {
                "status": 0,
                "message": None,
                "data": {"dataList": []},
            }
            return json_response(request, payload)
        return httpx2.Response(404, request=request)


class LenovoClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.clock = MutableClock()
        self.server = FakeLenovoServer(self.clock)
        self.temporary = tempfile.TemporaryDirectory(prefix="wam-lenovo-token-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "lenovo"
        self.client = httpx2.AsyncClient(transport=httpx2.MockTransport(self.server.handler))
        self.addAsyncCleanup(self.client.aclose)
        self.tokens = LenovoTokenManager(
            self.client,
            self.directory,
            clock=self.clock,
            timeout_seconds=20.0,
            connect_timeout_seconds=5.0,
        )
        self.raw = LenovoRawClient(self.client, token_manager=self.tokens, clock=self.clock)

    async def test_token_is_registered_once_and_cached(self):
        tree = await self.raw.composite_class_list()
        self.assertEqual(tree, COMPOSITE_TREE)
        self.assertEqual(self.server.register_count, 1)
        first_request = self.server.requests[0]
        self.assertEqual(first_request.method, "POST")
        self.assertEqual(first_request.url.path, "/appbase/token/register")
        self.assertEqual(first_request.headers["token"] if "token" in first_request.headers else "", "")
        token_request = self.server.requests[1]
        self.assertEqual(token_request.headers["token"], self.server.last_token)

        await self.raw.class_app_list("soft", "1")
        self.assertEqual(self.server.register_count, 1)
        self.assertEqual(self.server.requests[-1].headers["token"], self.server.last_token)

        # 设备指纹形状：mac 12 位小写 hex、sn 64 位 hex、uuid 大写 GUID。
        body = self.server.register_bodies[0]
        self.assertRegex(body["mac"], r"^[0-9a-f]{12}$")
        self.assertRegex(body["sn"], r"^[0-9a-f]{64}$")
        self.assertRegex(body["uuid"], r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$")
        self.assertEqual(body["ch"], "9.0.3400.1106")
        self.assertEqual(body["osBit"], "64")

    async def test_token_is_renewed_after_expiry_margin(self):
        await self.raw.composite_class_list()
        self.assertEqual(self.server.register_count, 1)
        token_path = self.directory / "token.json"
        document = json.loads(token_path.read_text(encoding="utf-8"))
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(document["expires_at"], int(self.clock.now) + self.server.exp_offset - TOKEN_EXPIRY_MARGIN_SECONDS)
        self.assertEqual(document["token"], self.server.last_token)

        # 距离 exp 还有 1 小时（余量边界）时仍复用旧 token。
        self.clock.now = document["expires_at"] - 1
        await self.raw.composite_class_list()
        self.assertEqual(self.server.register_count, 1)
        # 到达 exp - 3600 后重新注册。
        self.clock.now = document["expires_at"]
        await self.raw.composite_class_list()
        self.assertEqual(self.server.register_count, 2)
        self.assertNotEqual(self.server.valid_tokens, set())
        self.assertEqual(len(self.server.valid_tokens), 2)

    async def test_invalid_token_triggers_one_refresh_and_retry(self):
        await self.raw.composite_class_list()
        self.assertEqual(self.server.register_count, 1)
        self.server.valid_tokens.clear()

        data = await self.raw.class_app_list("soft", "1")
        self.assertEqual(len(data["apps"]), 1)
        self.assertEqual(self.server.register_count, 2)
        data_requests = [request for request in self.server.requests if "class_app_list" in request.url.path]
        self.assertEqual(len(data_requests), 2)
        self.assertNotEqual(data_requests[0].headers["token"], data_requests[1].headers["token"])
        self.assertEqual(data_requests[1].headers["token"], self.server.last_token)

    async def test_persistently_invalid_token_raises_unavailable_after_one_retry(self):
        await self.raw.composite_class_list()
        self.server.reject_all_tokens = True
        with self.assertRaises(LenovoUnavailableError) as context:
            await self.raw.class_app_list("soft", "1")
        self.assertIn("token", str(context.exception))
        data_requests = [request for request in self.server.requests if "class_app_list" in request.url.path]
        self.assertEqual(len(data_requests), 2)
        self.assertEqual(self.server.register_count, 2)

    async def test_fingerprint_and_token_survive_a_restart(self):
        first = await self.tokens.get_token()
        document = json.loads((self.directory / "token.json").read_text(encoding="utf-8"))

        calls = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            calls.append(request)
            return json_response(request, {"status": 0, "message": None, "data": make_jwt(int(self.clock.now) + 100_000)})

        client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        restarted = LenovoTokenManager(client, self.directory, clock=self.clock)
        self.assertEqual(await restarted.get_token(), first)
        self.assertEqual(calls, [])
        loaded = json.loads((self.directory / "token.json").read_text(encoding="utf-8"))
        self.assertEqual(loaded["fingerprint"], document["fingerprint"])

    async def test_expired_disk_token_keeps_the_same_fingerprint(self):
        await self.tokens.get_token()
        first_fingerprint = self.server.register_bodies[0]

        # 让磁盘 token 过期，再用新实例注册：设备指纹必须沿用，不能每次重启换机器身份。
        path = self.directory / "token.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["expires_at"] = self.clock.now - 1
        document["token"] = ""
        path.write_text(json.dumps(document), encoding="utf-8")

        restarted = LenovoTokenManager(self.client, self.directory, clock=self.clock)
        await restarted.get_token()
        self.assertEqual(self.server.register_bodies[-1], first_fingerprint)

    async def test_request_urls_headers_and_query_parameters(self):
        await self.raw.class_app_list("soft", "2", skip=100, limit=1000)
        request = self.server.requests[-1]
        self.assertEqual(request.url.path, "/classifyservice/class/composite_class_app_list")
        params = request.url.params
        self.assertEqual(params.get("code"), "soft")
        self.assertEqual(params.get("id"), "2")
        self.assertEqual(params.get("skip"), "100")
        self.assertEqual(params.get("limit"), "1000")
        self.assertEqual(params.get("pageIndex"), "1")
        self.assertEqual(params.get("type"), "pc")
        self.assertEqual(params.get("tagId"), "-1")
        self.assertEqual(params.get("refresh"), "true")
        self.assertEqual(request.headers["token"], self.server.last_token)

        await self.raw.app_details("L105867")
        details_request = self.server.requests[-1]
        self.assertEqual(details_request.url.path, "/appstorecontents/app/details_new")
        self.assertEqual(details_request.url.params.get("softid"), "L105867")
        self.assertEqual(details_request.headers["token"], self.server.last_token)

        await self.raw.download_url_list("L105867")
        download_request = self.server.requests[-1]
        self.assertEqual(download_request.url.path, "/dlservice/getPcSoftDownloadUrlList")
        self.assertEqual(download_request.url.params.get("softid"), "L105867")
        self.assertNotIn("token", download_request.headers)

        await self.raw.search("QQ 音乐", limit=30)
        search_request = self.server.requests[-1]
        self.assertEqual(search_request.url.path, "/searchcontent/fusion/v2/app_search")
        self.assertEqual(search_request.url.params.get("searchWord"), "QQ 音乐")
        self.assertEqual(search_request.url.params.get("tab"), "0")
        self.assertEqual(search_request.url.params.get("limit"), "30")
        self.assertEqual(search_request.headers["token"], self.server.last_token)

    async def test_string_status_zero_is_accepted(self):
        self.server.class_list_status = "0"
        tree = await self.raw.composite_class_list()
        self.assertEqual(tree, COMPOSITE_TREE)

    async def test_error_mapping(self):
        self.server.failure = httpx2.ReadTimeout("slow", request=None)
        with self.assertRaises(LenovoTimeoutError):
            await self.raw.composite_class_list()
        self.server.failure = httpx2.ConnectError("down")
        with self.assertRaises(LenovoUnavailableError):
            await self.raw.composite_class_list()
        self.server.failure = None
        self.server.http_status = 500
        with self.assertRaises(LenovoUnavailableError):
            await self.raw.composite_class_list()
        self.server.http_status = 200
        self.server.raw_body = b"<html>maintenance</html>"
        with self.assertRaises(LenovoProtocolError):
            await self.raw.composite_class_list()
        self.server.raw_body = None
        self.server.class_list_status = "not-a-number"
        with self.assertRaises(LenovoProtocolError):
            await self.raw.composite_class_list()

    async def test_offline_app_details_and_failed_search_map_to_not_found(self):
        self.server.details_payload = {"status": -1, "message": "软件已下架", "data": None}
        with self.assertRaises(LenovoNotFoundError):
            await self.raw.app_details("L105867")
        self.server.details_payload = {"status": "502", "message": "软件已下架", "data": None}
        with self.assertRaises(LenovoNotFoundError):
            await self.raw.app_details("L105867")
        self.server.search_payload = {"status": -1, "message": "搜索失败", "data": None}
        with self.assertRaises(LenovoNotFoundError):
            await self.raw.search("QQ")

    async def test_download_status_errors_are_unavailable(self):
        self.server.download_payload = {"status": 500, "message": "内部错误", "data": None}
        with self.assertRaises(LenovoUnavailableError):
            await self.raw.download_url_list("L105867")

    async def test_invalid_arguments_are_rejected(self):
        with self.assertRaises(ValueError):
            await self.raw.class_app_list("other", "1")
        with self.assertRaises(ValueError):
            await self.raw.class_app_list("soft", "")
        with self.assertRaises(ValueError):
            await self.raw.class_app_list("soft", "1", limit=1001)
        with self.assertRaises(ValueError):
            await self.raw.search("")
        with self.assertRaises(ValueError):
            LenovoRawClient(self.client, timeout_seconds=0)
        with self.assertRaises(ValueError):
            LenovoRawClient(self.client, timeout_seconds=5, connect_timeout_seconds=10)


class JwtParsingTests(unittest.TestCase):
    def test_exp_is_extracted_without_verifying_the_signature(self):
        self.assertEqual(parse_jwt_exp(make_jwt(1_795_943_668)), 1_795_943_668)

    def test_malformed_tokens_return_none(self):
        for token in ("", "abc", "a.b", "a.b.c.d", "x.%s.y" % "not-base64!", 123, None):
            with self.subTest(token=token):
                self.assertIsNone(parse_jwt_exp(token))


class LenovoDownloadTests(unittest.IsolatedAsyncioTestCase):
    """下载地址优先级、fileName 清洗与 Release 映射（地址绝不缓存）。"""

    def setUp(self) -> None:
        self.server = FakeLenovoServer(MutableClock())
        self.client = httpx2.AsyncClient(transport=httpx2.MockTransport(self.server.handler))
        self.addAsyncCleanup(self.client.aclose)
        self.raw = LenovoRawClient(self.client)
        self.details = LenovoDetailsProvider(self.raw)

    def download(self, urls, *, file_name="QQ9.9.37.54395_lenovo_x64.exe", sha256="a" * 64):
        self.server.download_payload = {
            "status": 0,
            "message": "",
            "data": {
                "downloadCode": 2,
                "downloadUrls": urls,
                "fileName": file_name,
                "sha256": sha256,
            },
        }

    async def test_https_priority_hs2_bs2_ams_then_old_cdn(self):
        urls = [
            {"downLoadUrl": "https://example.invalid/hs", "downloadCode": "hs"},
            {"downLoadUrl": "https://example.invalid/hs2", "downloadCode": "hs2"},
            {"downLoadUrl": "https://example.invalid/bs2", "downloadCode": "bs2"},
            {"downLoadUrl": "https://example.invalid/ams", "downloadCode": "amsHttpsDownloadUrl"},
            {"downLoadUrl": "https://example.invalid/first", "downloadCode": "firstOldDownloadUrl"},
            {"downLoadUrl": "https://example.invalid/second", "downloadCode": "secondOldDownloadUrl"},
        ]
        self.download(urls)
        release = await self.details.resolve_download("L105867", summary())
        self.assertEqual(str(release.download_url), "https://example.invalid/hs2")
        self.assertEqual(release.file_name, "QQ9.9.37.54395_lenovo_x64.exe")
        self.assertEqual(release.sha256, "a" * 64)
        self.assertEqual(release.version, "9.9.37.54395")
        self.assertEqual(release.architecture, "x64")
        self.assertEqual(release.size_bytes, 334_442_672)
        self.assertEqual(release.id, "lenovo-l105867-9.9.37.54395")
        self.assertEqual(release.platform, "windows")
        # 下载地址绝不缓存：每次解析都会重新请求。
        await self.details.resolve_download("L105867", summary())
        self.assertEqual(len(self.server.requests), 2)
        self.assertEqual(DOWNLOAD_PRIORITY[0], "hs2")

    async def test_priority_fallbacks_skip_http_urls(self):
        cases = [
            ([{"downLoadUrl": "https://example.invalid/bs2", "downloadCode": "bs2"}], "https://example.invalid/bs2"),
            (
                [{"downLoadUrl": "https://example.invalid/ams", "downloadCode": "amsHttpsDownloadUrl"}],
                "https://example.invalid/ams",
            ),
            (
                [{"downLoadUrl": "https://example.invalid/first", "downloadCode": "firstOldDownloadUrl"}],
                "https://example.invalid/first",
            ),
            (
                [{"downLoadUrl": "https://example.invalid/second", "downloadCode": "secondOldDownloadUrl"}],
                "https://example.invalid/second",
            ),
        ]
        for urls, expected in cases:
            with self.subTest(expected=expected):
                self.download(urls)
                release = await self.details.resolve_download("L105867", summary())
                self.assertEqual(str(release.download_url), expected)

    async def test_no_https_url_is_unavailable(self):
        self.download(
            [
                {"downLoadUrl": "http://example.invalid/hs2", "downloadCode": "hs2"},
                {"downLoadUrl": "https://example.invalid/ams", "downloadCode": "amsHttpDownloadUrl"},
            ]
        )
        with self.assertRaises(LenovoUnavailableError):
            await self.details.resolve_download("L105867", summary())
        self.download([])
        with self.assertRaises(LenovoUnavailableError):
            await self.details.resolve_download("L105867", summary())

    async def test_file_name_is_decoded_cleaned_and_whitelisted(self):
        hs2 = [{"downLoadUrl": "https://example.invalid/hs2", "downloadCode": "hs2"}]
        self.download(hs2, file_name="QianwenPC_V5.2.0.298_pc_pf3000_%28zh-cn%29_release_%28Build3280179-1000-x64-lx%29.exe")
        release = await self.details.resolve_download("L105867", summary())
        self.assertEqual(
            release.file_name,
            "QianwenPC_V5.2.0.298_pc_pf3000_(zh-cn)_release_(Build3280179-1000-x64-lx).exe",
        )

        self.download(hs2, file_name="bad<>:name?.exe")
        release = await self.details.resolve_download("L105867", summary())
        self.assertEqual(release.file_name, "badname.exe")

        for raw_name in ("setup.zip", "12345", "../../evil.exe", "CON.exe", "", None):
            with self.subTest(raw_name=raw_name):
                self.download(hs2, file_name=raw_name)
                release = await self.details.resolve_download("L105867", summary())
                self.assertEqual(release.file_name, "QQ_9.9.37.54395.exe")

        self.download(hs2, file_name="Installer.MSIX")
        release = await self.details.resolve_download("L105867", summary())
        self.assertEqual(release.file_name, "Installer.MSIX")

    async def test_sha256_and_size_are_passed_through(self):
        self.download([{"downLoadUrl": "https://example.invalid/hs2", "downloadCode": "hs2"}], sha256="B" * 64)
        release = await self.details.resolve_download("L105867", summary())
        self.assertEqual(release.sha256, "b" * 64)
        self.download([{"downLoadUrl": "https://example.invalid/hs2", "downloadCode": "hs2"}], sha256="not-a-sha")
        release = await self.details.resolve_download("L105867", summary())
        self.assertIsNone(release.sha256)
        no_size = summary(size_bytes=None)
        self.assertIsNone((await self.details.resolve_download("L105867", no_size)).size_bytes)

    async def test_architecture_selection_follows_supported_bits(self):
        self.download([{"downLoadUrl": "https://example.invalid/hs2", "downloadCode": "hs2"}])
        both = summary(architectures=("x86", "x64"))
        self.assertEqual((await self.details.resolve_download("L105867", both, "x86")).architecture, "x86")
        self.assertEqual((await self.details.resolve_download("L105867", both, "x64")).architecture, "x64")
        self.assertEqual((await self.details.resolve_download("L105867", both, "arm64")).architecture, "x64")
        only_x86 = summary(architectures=("x86",))
        self.assertEqual((await self.details.resolve_download("L105867", only_x86, "x64")).architecture, "x86")

    async def test_release_id_is_sanitized(self):
        self.download([{"downLoadUrl": "https://example.invalid/hs2", "downloadCode": "hs2"}])
        release = await self.details.resolve_download(
            "L105867",
            summary(soft_id="L105867", version="5.2.0 (Build 328)"),
        )
        self.assertRegex(release.id, r"^[a-z0-9][a-z0-9.-]*$")


if __name__ == "__main__":
    unittest.main()
