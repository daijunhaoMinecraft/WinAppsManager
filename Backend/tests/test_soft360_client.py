"""360 软件源原始接口客户端的测试（``httpx2.MockTransport``，禁止真实网络）。

覆盖：

- 目录列表的 URL/查询参数（``cate_id`` / ``page_size`` / ``src=web_client`` / ``page``）；
- 详情接口把 ID 列表用逗号拼接，单次最多 50 个；
- ``errno != 0``、非 JSON、非法包络、HTTP 错误、超时、连接错误的异常映射；
- 参数校验（非法 cate_id / page / page_size / soft_ids、非法 base_url）。
"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

import httpx2

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR.parent))

from Backend.soft360.client import (
    LIST_PATH,
    MAX_BATCH_IDS,
    MAX_PAGE_SIZE,
    Soft360RawClient,
)
from Backend.soft360.errors import (
    Soft360NotFoundError,
    Soft360ProtocolError,
    Soft360TimeoutError,
    Soft360UnavailableError,
)


LIST_ITEM = {
    "soft_id": "1010",
    "name": "微信",
    "bname": "WeChat",
    "brief": "国民级聊天工具",
    "soft_size": 123456789,
    "soft_score": 95,
    "logo": {
        "32": "http://p0.qhimg.com/logo/32.png",
        "48": "https://p0.qhimg.com/logo/48.png",
        "72": "https://p0.qhimg.com/logo/72.png",
    },
}

INFO_ITEM = {
    "name": "微信",
    "corp": "腾讯",
    "desc": "微信详细描述",
    "edition_info": {"default_edition": {"version": "3.9.12"}},
}


def ok(payload):
    return {"errno": 0, "data": payload}


class RecordingTransport:
    """记录请求并返回固定响应的最小 360 上游替身。"""

    def __init__(self, responder):
        self.requests: list[httpx2.Request] = []
        self.responder = responder

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        result = self.responder(request)
        if isinstance(result, httpx2.Response):
            return result
        status, payload = result
        if isinstance(payload, str):
            return httpx2.Response(
                status, content=payload.encode("utf-8"), request=request,
                headers={"Content-Type": "text/plain"},
            )
        return httpx2.Response(status, json=payload, request=request)


def client_for(transport: RecordingTransport, **kwargs) -> Soft360RawClient:
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(transport.handler))
    return Soft360RawClient(http, **kwargs)


class Soft360ClientListTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_request_uses_the_documented_query_parameters(self):
        transport = RecordingTransport(lambda request: (200, ok([LIST_ITEM])))
        client = client_for(transport)
        try:
            data = await client.list_softs("149", page=2, page_size=100)
        finally:
            await client._client.aclose()
        self.assertEqual(data, [LIST_ITEM])
        self.assertEqual(len(transport.requests), 1)
        request = transport.requests[0]
        self.assertEqual(request.url.path, LIST_PATH)
        self.assertEqual(request.url.host, "cdn-mgr.soft.360.cn")
        self.assertEqual(request.url.scheme, "https")
        self.assertEqual(request.url.params["cate_id"], "149")
        self.assertEqual(request.url.params["page"], "2")
        self.assertEqual(request.url.params["page_size"], "100")
        self.assertEqual(request.url.params["src"], "web_client")
        self.assertEqual(request.url.params["machine"], "")
        self.assertEqual(request.headers["accept"], "application/json")

    async def test_list_accepts_every_page_size_up_to_the_api_limit(self):
        transport = RecordingTransport(lambda request: (200, ok([])))
        client = client_for(transport)
        try:
            await client.list_softs("149", page=1, page_size=MAX_PAGE_SIZE)
        finally:
            await client._client.aclose()
        self.assertEqual(transport.requests[0].url.params["page_size"], str(MAX_PAGE_SIZE))

    async def test_list_rejects_invalid_arguments(self):
        transport = RecordingTransport(lambda request: (200, ok([])))
        client = client_for(transport)
        try:
            for cate_id in ("", "   ", None, 149):
                with self.subTest(cate_id=cate_id), self.assertRaises(ValueError):
                    await client.list_softs(cate_id)
            for page in (0, -1, 1.5, "1", None):
                with self.subTest(page=page), self.assertRaises(ValueError):
                    await client.list_softs("149", page=page)
            for page_size in (0, 1.0, MAX_PAGE_SIZE + 1, None):
                with self.subTest(page_size=page_size), self.assertRaises(ValueError):
                    await client.list_softs("149", page_size=page_size)
        finally:
            await client._client.aclose()

    async def test_list_error_envelope_maps_to_unavailable(self):
        transport = RecordingTransport(
            lambda request: (200, {"errno": 10001, "errmsg": "page_size too large", "data": None})
        )
        client = client_for(transport)
        try:
            with self.assertRaises(Soft360UnavailableError) as raised:
                await client.list_softs("149", page_size=100)
        finally:
            await client._client.aclose()
        self.assertIn("page_size too large", str(raised.exception))

    async def test_list_non_object_data_maps_to_protocol_error(self):
        transport = RecordingTransport(lambda request: (200, ok({"apps": []})))
        client = client_for(transport)
        try:
            with self.assertRaises(Soft360ProtocolError):
                await client.list_softs("149")
        finally:
            await client._client.aclose()

    async def test_list_missing_errno_maps_to_protocol_error(self):
        transport = RecordingTransport(lambda request: (200, {"data": [LIST_ITEM]}))
        client = client_for(transport)
        try:
            with self.assertRaises(Soft360ProtocolError):
                await client.list_softs("149")
        finally:
            await client._client.aclose()

    async def test_list_accepts_string_errno_zero(self):
        transport = RecordingTransport(lambda request: (200, {"errno": "0", "data": [LIST_ITEM]}))
        client = client_for(transport)
        try:
            self.assertEqual(await client.list_softs("149"), [LIST_ITEM])
        finally:
            await client._client.aclose()


class Soft360ClientErrorMappingTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_json_maps_to_protocol_error(self):
        transport = RecordingTransport(lambda request: (200, "<html>not json</html>"))
        client = client_for(transport)
        try:
            with self.assertRaises(Soft360ProtocolError):
                await client.list_softs("149")
        finally:
            await client._client.aclose()

    async def test_http_error_maps_to_unavailable(self):
        transport = RecordingTransport(lambda request: (500, {"errno": 500, "data": None}))
        client = client_for(transport)
        try:
            with self.assertRaises(Soft360UnavailableError) as raised:
                await client.list_softs("149")
        finally:
            await client._client.aclose()
        self.assertIn("500", str(raised.exception))

    async def test_timeout_maps_to_timeout_error(self):
        def handler(request):
            raise httpx2.TimeoutException("slow upstream")

        transport = RecordingTransport(handler)
        client = client_for(transport)
        try:
            with self.assertRaises(Soft360TimeoutError):
                await client.list_softs("149")
        finally:
            await client._client.aclose()

    async def test_connection_error_maps_to_unavailable(self):
        def handler(request):
            raise httpx2.ConnectError("refused", request=request)

        transport = RecordingTransport(handler)
        client = client_for(transport)
        try:
            with self.assertRaises(Soft360UnavailableError):
                await client.list_softs("149")
        finally:
            await client._client.aclose()


class Soft360ClientInfoTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_info_joins_ids_with_commas(self):
        transport = RecordingTransport(
            lambda request: (200, ok({"1010": INFO_ITEM, "1011": INFO_ITEM}))
        )
        client = client_for(transport)
        try:
            data = await client.get_info(["1010", "1011"])
        finally:
            await client._client.aclose()
        self.assertEqual(data, {"1010": INFO_ITEM, "1011": INFO_ITEM})
        request = transport.requests[0]
        self.assertEqual(request.url.path, "/main/cdn_client/v1/soft/getinfo")
        self.assertEqual(request.url.host, "mgr.soft.360.cn")
        self.assertEqual(request.url.params["scene"], "1")
        self.assertEqual(request.url.params["soft_id"], "1010,1011")
        self.assertEqual(request.url.params["machine"], "")

    async def test_get_info_allows_at_most_fifty_ids(self):
        transport = RecordingTransport(lambda request: (200, ok({})))
        client = client_for(transport)
        try:
            ids = [str(1000 + index) for index in range(MAX_BATCH_IDS)]
            await client.get_info(ids)
            with self.assertRaises(ValueError):
                await client.get_info(ids + ["9999"])
        finally:
            await client._client.aclose()
        self.assertEqual(len(transport.requests), 1)

    async def test_get_info_rejects_invalid_arguments(self):
        transport = RecordingTransport(lambda request: (200, ok({})))
        client = client_for(transport)
        try:
            for value in ([], "", "1010", [1010], ["1010", "1010"], ["10,10"], [""], [None]):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    await client.get_info(value)
        finally:
            await client._client.aclose()

    async def test_get_info_error_envelope_maps_to_not_found(self):
        transport = RecordingTransport(
            lambda request: (200, {"errno": 10002, "errmsg": "soft not found", "data": None})
        )
        client = client_for(transport)
        try:
            with self.assertRaises(Soft360NotFoundError) as raised:
                await client.get_info(["1010"])
        finally:
            await client._client.aclose()
        self.assertIn("soft not found", str(raised.exception))

    async def test_get_info_non_object_data_maps_to_protocol_error(self):
        transport = RecordingTransport(lambda request: (200, ok([])))
        client = client_for(transport)
        try:
            with self.assertRaises(Soft360ProtocolError):
                await client.get_info(["1010"])
        finally:
            await client._client.aclose()

    async def test_get_info_null_data_means_no_details(self):
        # 实测不存在/已下架的 soft_id 返回 errno=0 且 data=null；按空详情处理（服务层再 404）。
        transport = RecordingTransport(lambda request: (200, ok(None)))
        client = client_for(transport)
        try:
            self.assertEqual(await client.get_info(["999999999999"]), {})
        finally:
            await client._client.aclose()


class Soft360ClientConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_base_urls_are_rejected(self):
        transport = RecordingTransport(lambda request: (200, ok([])))
        http = httpx2.AsyncClient(transport=httpx2.MockTransport(transport.handler))
        try:
            for kwargs in (
                {"list_base_url": ""},
                {"list_base_url": "ftp://cdn-mgr.soft.360.cn"},
                {"details_base_url": "not a url"},
            ):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    Soft360RawClient(http, **kwargs)
            for kwargs in (
                {"timeout_seconds": 0},
                {"timeout_seconds": float("inf")},
                {"connect_timeout_seconds": 30, "timeout_seconds": 10},
            ):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    Soft360RawClient(http, **kwargs)
        finally:
            await http.aclose()

    async def test_client_must_be_httpx2_async_client(self):
        with self.assertRaises(ValueError):
            Soft360RawClient(object())


if __name__ == "__main__":
    unittest.main()
