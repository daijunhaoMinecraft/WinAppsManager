"""360 加密搜索接口、搜索映射与目录外详情的测试（全部离线，禁止真实网络）。

覆盖：

- 纯 Python AES-128（FIPS-197 已知向量）、AES-128-CBC/PKCS#7（NIST 向量与往返）；
- RSA PKCS#1 v1.5：SPKI 解析、type-2 加密（用测试私钥还原 padding）、type-1 公钥还原；
- 请求构造：``q`` 可被同一 AES 解回、``skey`` 长度、签名排序/过滤规则、URL query；
- 响应解密与 :class:`Soft360SearchClient` 的 MockTransport 端到端（成功 / 无结果 / HTTP /
  超时 / 非法 JSON / errno / 解密失败 / 包络异常）；
- 服务层：搜索结果映射、``limit`` 截断、去重、无效条目跳过、远程失败/空结果回退本地目录；
- 目录外软件（搜索结果里出现但不在热门目录快照里）的详情/下载/图标/截图，以及确实不存在
  时仍然 404；
- 路由层（ASGITransport，不启动 lifespan）：``GET /api/v1/store/360/search`` 的 200、参数
  422、上游失败时回退缓存目录仍然 200（不会变成 5xx）。

测试用的 RSA 私钥是仓库里固定的 1024 位测试密钥（只为构造 PKCS#1 v1.5 padding，不对应任何
真实服务）；生产代码只使用 ``Backend/soft360/search.py`` 里 360 的 2048 位公钥。
"""

from __future__ import annotations

import base64
from contextlib import asynccontextmanager
import json
from pathlib import Path
import random
import sys
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

import httpx2

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR.parent))

from Backend.config import (
    BackendSettings,
    CacheConfig,
    LenovoConfig,
    RefreshConfig,
    Soft360Config,
)
from Backend.main import create_app
from Backend.models import Release
from Backend.soft360.errors import (
    Soft360NotFoundError,
    Soft360ProtocolError,
    Soft360TimeoutError,
    Soft360UnavailableError,
)
from Backend.soft360.search import (
    AES128,
    AES_IV,
    AES_KEY,
    BASE_PARAM_KEYS,
    M2_LENGTH,
    MID_LENGTH,
    RSA_PUBLIC_KEY,
    SIGN_SALT,
    Soft360SearchClient,
    aes_cbc_decrypt,
    aes_cbc_encrypt,
    build_search_request,
    compute_signature,
    decrypt_search_response,
    parse_rsa_public_key,
    rsa_pkcs1_v15_encrypt,
    rsa_pkcs1_v15_public_decrypt,
)
from Backend.soft360.service import (
    DEFAULT_ICON_URL,
    Soft360StoreService,
)

# ---------------------------------------------------------------------------
# 测试基础设施：固定 RSA 私钥、假上游、搜索条目
# ---------------------------------------------------------------------------

# 仓库固定的 1024 位测试 RSA 密钥（n, d；e=65537），由确定性随机种子生成，只用于测试。
TEST_RSA_N = int(
    "60792419256957173672723950520833277219988899391819939291376694019103101733370"
    "29157839143485294710481237376581557449908917419920895239025386821242211062224"
    "91838169970093113896554787055297502694354622045734399997616580268728869586793"
    "65137107938634651437202852390207965573559782557564847363720299304096619083303"
)
TEST_RSA_E = 65537
TEST_RSA_D = int(
    "78531001026809196684816357951901143620310057257907381485389183448392031871265"
    "52764128078603919163058143587613022471417503834025100186702004185213933938505"
    "40840742434201531074262108574489228067680733413527450707211039546903155438245"
    "9352156565490197149261874194341332636727806197980027174906571303504118265953"
)
TEST_PUBLIC_KEY = (TEST_RSA_N, TEST_RSA_E)
TEST_MODULUS_BYTES = (TEST_RSA_N.bit_length() + 7) // 8

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"360-search-test-image"
IMAGE_URL = "https://p0.qhimg.com/logo/72.png"
FIXED_NOW = 1_760_000_000.0


def search_item(soft_id="600001358", name="微信", **overrides):
    """一条实测结构的加密搜索条目（字段名与类型照抄真实响应）。"""

    data = {
        "type": 1,
        "soft_id": int(soft_id) if str(soft_id).isdigit() else soft_id,
        "name": name,
        "desc": "一个生活方式",
        "logo": "https://p8.qhimg.com/dm/72_72_100/t11eec6e52fecc71f655bb449f0.png",
        "type_name": "聊天工具",
        "size": 259798216,
        "score": 82,
        "os": {"bit": [64], "ver": [4, 5, 6, 7, 8]},
        "soft_baoku_cate_id": 149,
        "snapshot": ["http://p0.qhimg.com/t11098f6bcd82d1f1917be4989d.png"],
        "free": {"flag": 2, "intro": ""},
    }
    data.update(overrides)
    return data


def info_payload(**overrides):
    """一份 ``getinfo`` 详情响应（含 logo / snapshot / 默认版本与下载信息）。"""

    raw = {
        "name": "微信",
        "bname": "WeChat",
        "corp": "腾讯",
        "desc": "微信是一款跨平台的通讯工具",
        "brief": "国民级聊天工具",
        "soft_size": 259798216,
        "soft_score": 90,
        "logo": {
            "32": "http://p0.qhimg.com/logo/32.png",
            "72": IMAGE_URL,
        },
        "baoku_cate": {"id": 149, "name": "聊天工具"},
        "snapshot": [
            "http://p0.qhimg.com/shot1.png",
            {"url": "https://p0.qhimg.com/shot2.png"},
        ],
        "edition_info": {
            "default_edition": {
                "version": "4.2.0.52",
                "os": {"bit": [64], "ver": [4, 5, 6, 7, 8]},
                "down": {
                    "default": {
                        "name": "WeChat_4.2.0.52.exe",
                        "size": 259798216,
                        "p2p": "https://dl.360.cn/wechat_4.2.0.52.exe",
                    }
                },
            }
        },
    }
    raw.update(overrides)
    return raw


def list_item(soft_id="1010", name="微信"):
    """目录列表接口（明文）里的一条记录。"""

    return {
        "soft_id": soft_id,
        "name": name,
        "bname": f"{name} EN",
        "brief": f"{name} 简介",
        "desc": f"{name} 详细描述",
        "soft_size": 1024,
        "soft_score": 88,
        "logo": {
            "32": f"http://p0.qhimg.com/{soft_id}/32.png",
            "72": f"https://p0.qhimg.com/{soft_id}/72.png",
        },
    }


def encode_type1_signature(payload: bytes) -> bytes:
    """用测试私钥构造 PKCS#1 v1.5 type-1 签名（模拟 360 响应 skey）。"""

    padding_length = TEST_MODULUS_BYTES - len(payload) - 3
    if padding_length < 8:
        raise AssertionError("测试密钥太短，无法容纳 type-1 padding")
    encoded = b"\x00\x01" + b"\xff" * padding_length + b"\x00" + payload
    return pow(int.from_bytes(encoded, "big"), TEST_RSA_D, TEST_RSA_N).to_bytes(
        TEST_MODULUS_BYTES, "big"
    )


def encrypt_response_items(
    items,
    *,
    key: bytes = AES_KEY,
    iv: bytes = AES_IV,
) -> tuple[str, str]:
    """构造 360 风格的加密响应：返回 ``(skey_base64, data_base64)``。"""

    plaintext = json.dumps({"list": items}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    data = base64.b64encode(aes_cbc_encrypt(key, iv, plaintext)).decode("ascii")
    key_document = json.dumps(
        {
            "key": base64.b64encode(key).decode("ascii"),
            "iv": base64.b64encode(iv).decode("ascii"),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    skey = base64.b64encode(encode_type1_signature(key_document)).decode("ascii")
    return skey, data


class FakeSearchUpstream:
    """加密搜索接口替身：记录请求，可切换错误模式，默认返回加密的成功响应。"""

    def __init__(self, items=None) -> None:
        self.items = list(items) if items is not None else [search_item()]
        self.requests: list[httpx2.Request] = []
        self.mode = "ok"
        self.key = AES_KEY
        self.iv = AES_IV

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.mode == "http-error":
            return httpx2.Response(500, json={"errno": 500}, request=request)
        if self.mode == "timeout":
            raise httpx2.TimeoutException("slow search upstream")
        if self.mode == "connection":
            raise httpx2.ConnectError("search upstream refused", request=request)
        if self.mode == "invalid-json":
            return httpx2.Response(
                200,
                content=b"<html>not json</html>",
                headers={"content-type": "text/html"},
                request=request,
            )
        if self.mode == "top-level-list":
            return httpx2.Response(200, json=[1, 2, 3], request=request)
        if self.mode == "missing-errno":
            return httpx2.Response(200, json={"data": None}, request=request)
        if self.mode == "errno":
            return httpx2.Response(
                200, json={"errno": 10009, "errmsg": "mid invalid"}, request=request
            )
        if self.mode == "bad-crypto":
            return httpx2.Response(
                200,
                json={"errno": 0, "errmsg": "", "data": "@@@@", "skey": "@@@@", "type": 1},
                request=request,
            )
        if self.mode == "empty":
            return httpx2.Response(
                200, json={"errno": 0, "errmsg": "", "data": "", "skey": "", "type": 1},
                request=request,
            )
        skey, data = encrypt_response_items(self.items, key=self.key, iv=self.iv)
        return httpx2.Response(
            200,
            json={"errno": 0, "errmsg": "", "time": 1, "data": data, "skey": skey, "type": 1},
            request=request,
        )


class StaticSearchClient:
    """可注入的搜索替身：返回固定原始条目或抛指定异常。"""

    def __init__(self, items=None, error: Exception | None = None) -> None:
        self.items = list(items) if items is not None else []
        self.error = error
        self.calls: list[str] = []

    async def search(self, keyword: str):
        self.calls.append(keyword)
        if self.error is not None:
            raise self.error
        return list(self.items)


class FakeStoreUpstream:
    """360 明文目录/详情/图片接口与加密搜索接口的合成替身。"""

    def __init__(self, infos=None, pages=None) -> None:
        self.infos = dict(infos or {})
        self.pages = dict(pages or {})
        self.requests: list[httpx2.Request] = []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/soft/list"):
            params = dict(request.url.params)
            page = int(params.get("page", "1"))
            return httpx2.Response(
                200,
                json={"errno": 0, "data": self.pages.get((params.get("cate_id"), page), [])},
                request=request,
            )
        if path.endswith("/soft/getinfo"):
            wanted = request.url.params.get("soft_id", "").split(",")
            data = {soft_id: self.infos[soft_id] for soft_id in wanted if soft_id in self.infos}
            return httpx2.Response(200, json={"errno": 0, "data": data}, request=request)
        # 其余请求视为图片 CDN（媒体代理会 GET qhimg 地址）。
        return httpx2.Response(
            200, content=PNG_BYTES, headers={"content-type": "image/png"}, request=request
        )


def store_settings(*, pages_per_category: int = 1) -> BackendSettings:
    return BackendSettings(
        cache=CacheConfig(persist=False),
        refresh=RefreshConfig(on_startup=False, interval_seconds=0),
        lenovo=LenovoConfig(enabled=False),
        soft360=Soft360Config(
            catalog_pages_per_category=pages_per_category,
            catalog_refresh_interval_seconds=0,
        ),
    )


def make_search_service(directory, upstream: FakeStoreUpstream, search_client):
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(upstream.handler))
    service = Soft360StoreService(
        http,
        directory,
        settings=store_settings(),
        cache_persist=False,
        search_client=search_client,
    )
    return http, service


# ---------------------------------------------------------------------------
# AES-128（FIPS-197 已知向量）
# ---------------------------------------------------------------------------


class AesTests(unittest.TestCase):
    def test_fips_197_aes_128_known_vector(self):
        cipher = AES128(bytes(range(16)))
        block = bytes.fromhex("00112233445566778899aabbccddeeff")
        encrypted = cipher.encrypt_block(block)
        self.assertEqual(encrypted.hex(), "69c4e0d86a7b0430d8cdb78070b4c55a")
        self.assertEqual(cipher.decrypt_block(encrypted), block)

    def test_nist_cbc_known_vector_and_roundtrip(self):
        key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
        iv = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        plaintext = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a")
        encrypted = aes_cbc_encrypt(key, iv, plaintext)
        # 第一块与 NIST SP 800-38A CBC-AES128 向量一致（后面是我们补的 PKCS#7 块）。
        self.assertEqual(encrypted[:16].hex(), "7649abac8119b246cee98e9b12e9197d")
        self.assertEqual(aes_cbc_decrypt(key, iv, encrypted), plaintext)

    def test_pkcs7_padding_roundtrip_for_arbitrary_lengths(self):
        for length in (0, 1, 15, 16, 17, 31, 32, 100):
            with self.subTest(length=length):
                plaintext = bytes(range(256))[:length]
                encrypted = aes_cbc_encrypt(AES_KEY, AES_IV, plaintext)
                self.assertEqual(len(encrypted) % 16, 0)
                self.assertGreaterEqual(len(encrypted), length + 1)
                self.assertEqual(aes_cbc_decrypt(AES_KEY, AES_IV, encrypted), plaintext)

    def test_invalid_keys_ivs_and_padding_are_rejected(self):
        for key, iv in ((b"short", AES_IV), (AES_KEY, b"short")):
            with self.subTest(key=key, iv=iv), self.assertRaises(ValueError):
                aes_cbc_encrypt(key, iv, b"data")
        with self.assertRaises(ValueError):
            aes_cbc_decrypt(AES_KEY, AES_IV, b"not a multiple of 16")
        with self.assertRaises(ValueError):
            aes_cbc_decrypt(AES_KEY, AES_IV, b"")
        # 篡改最后一个字节会让 PKCS#7 padding 校验失败。
        encrypted = bytearray(aes_cbc_encrypt(AES_KEY, AES_IV, b"hello"))
        encrypted[-1] ^= 0xFF
        with self.assertRaises(ValueError):
            aes_cbc_decrypt(AES_KEY, AES_IV, bytes(encrypted))


# ---------------------------------------------------------------------------
# RSA PKCS#1 v1.5 与 SPKI 解析
# ---------------------------------------------------------------------------


class RsaTests(unittest.TestCase):
    def test_the_production_public_key_is_2048_bit_with_e_65537(self):
        self.assertEqual(RSA_PUBLIC_KEY[0].bit_length(), 2048)
        self.assertEqual(RSA_PUBLIC_KEY[1], 65537)

    def test_spki_parsing_rejects_invalid_input(self):
        for value in ("", "not base64 @@", base64.b64encode(b"garbage").decode(), 123):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_rsa_public_key(value)

    def test_type2_encryption_uses_random_nonzero_padding(self):
        payload = b"hello skey"
        encrypted = rsa_pkcs1_v15_encrypt(
            payload, TEST_PUBLIC_KEY, random_source=random.Random(7)
        )
        self.assertEqual(len(encrypted), TEST_MODULUS_BYTES)
        decoded = pow(int.from_bytes(encrypted, "big"), TEST_RSA_D, TEST_RSA_N).to_bytes(
            TEST_MODULUS_BYTES, "big"
        )
        self.assertEqual(decoded[:2], b"\x00\x02")
        separator = decoded.index(b"\x00", 2)
        self.assertGreaterEqual(separator - 2, 8)
        self.assertNotIn(0, decoded[2:separator])
        self.assertEqual(decoded[separator + 1 :], payload)

    def test_type2_encryption_rejects_oversized_payload(self):
        with self.assertRaises(ValueError):
            rsa_pkcs1_v15_encrypt(b"x" * (TEST_MODULUS_BYTES - 10), TEST_PUBLIC_KEY)

    def test_public_operation_recovers_type1_padding(self):
        payload = b'{"key":"abc","iv":"def"}'
        signature = encode_type1_signature(payload)
        self.assertEqual(rsa_pkcs1_v15_public_decrypt(signature, TEST_PUBLIC_KEY), payload)

    def test_public_operation_rejects_wrong_padding_and_length(self):
        # type-2 padding（00 02 ...）不是签名 padding。
        wrong = rsa_pkcs1_v15_encrypt(b"data", TEST_PUBLIC_KEY, random_source=random.Random(1))
        with self.assertRaises(ValueError):
            rsa_pkcs1_v15_public_decrypt(wrong, TEST_PUBLIC_KEY)
        with self.assertRaises(ValueError):
            rsa_pkcs1_v15_public_decrypt(b"\x00" * 4, TEST_PUBLIC_KEY)
        with self.assertRaises(ValueError):
            rsa_pkcs1_v15_public_decrypt(b"short", TEST_PUBLIC_KEY)

    def test_type1_padding_requires_at_least_eight_ff_bytes(self):
        # 构造 ``00 01 FF FF 00 data``：0xFF 只有 2 个，必须拒绝。
        encoded = b"\x00\x01\xff\xff\x00" + b"data"
        number = int.from_bytes(encoded + b"\x00" * (TEST_MODULUS_BYTES - len(encoded)), "big")
        signature = pow(number, TEST_RSA_D, TEST_RSA_N).to_bytes(TEST_MODULUS_BYTES, "big")
        with self.assertRaises(ValueError):
            rsa_pkcs1_v15_public_decrypt(signature, TEST_PUBLIC_KEY)


# ---------------------------------------------------------------------------
# 请求构造与响应解密
# ---------------------------------------------------------------------------


class RequestBuildingTests(unittest.TestCase):
    def _request(self, keyword: str = "微信"):
        return build_search_request(
            keyword,
            mid="M" * MID_LENGTH,
            m2="2" * M2_LENGTH,
            timestamp=1_700_000_000,
            rand=4321,
            public_key=TEST_PUBLIC_KEY,
            random_source=random.Random(11),
        )

    def test_the_query_parameter_layout_matches_the_browser_client(self):
        request = self._request()
        params = request.query_params
        self.assertEqual(list(params), [*BASE_PARAM_KEYS, "sign"])
        self.assertEqual(params["ver"], "14.0.0.0")
        self.assertEqual(params["os_ver"], "7")
        self.assertEqual(params["os_bit"], "64")
        self.assertEqual(params["sk_type"], "1")
        self.assertEqual(params["sk_bit"], "2048")
        self.assertEqual(params["timestamp"], "1700000000")
        self.assertEqual(params["rand"], "4321")
        self.assertEqual(params["mid"], "M" * MID_LENGTH)
        self.assertEqual(params["m2"], "2" * M2_LENGTH)
        self.assertEqual(params["sdk_ver"], "")
        self.assertEqual(len(params["sign"]), 32)
        # sdk_ver 为空，签名与 URL 里都会过滤掉。
        query = parse_qs(urlsplit(request.url).query)
        self.assertNotIn("sdk_ver", query)
        self.assertEqual(query["sign"], [params["sign"]])
        self.assertEqual(urlsplit(request.url).path, "/main/enc_client/v2/search/search")

    def test_signature_is_md5_of_sorted_base_params_plus_raw_skey_q_and_salt(self):
        request = self._request()
        base = {key: request.query_params[key] for key in BASE_PARAM_KEYS}
        expected = compute_signature(base, request.skey, request.q)
        self.assertEqual(request.query_params["sign"], expected)
        # 参数插入顺序不影响签名（签名前会按 key 排序）。
        shuffled = dict(reversed(list(base.items())))
        self.assertEqual(compute_signature(shuffled, request.skey, request.q), expected)
        # 直接按协议拼接一遍，确认没有额外字段/分隔符。
        joined = "&".join(f"{key}={base[key]}" for key in sorted(base) if base[key])
        manual = __import__("hashlib").md5(
            f"{joined}{request.skey}{request.q}{SIGN_SALT}".encode("utf-8")
        ).hexdigest()
        self.assertEqual(expected, manual)
        self.assertEqual(SIGN_SALT, "sjgo144ddycl3396ddumrn3qmag7eegm")

    def test_q_decrypts_to_the_expected_search_json(self):
        request = self._request()
        decrypted = json.loads(aes_cbc_decrypt(AES_KEY, AES_IV, base64.b64decode(request.q)))
        self.assertEqual(
            decrypted,
            {
                "kw": "微信",
                "mid": "M" * MID_LENGTH,
                "m2": "2" * M2_LENGTH,
                "os_bit": "64",
                "os_ver": "7",
                "machine": "",
                "src": "web_client",
                "ext_param": "schannel=",
                "source_id": "",
                "language": "zh-CN",
                "country_code": "CN",
            },
        )

    def test_skey_is_pkcs1_v15_encrypted_under_the_given_public_key(self):
        request = self._request()
        self.assertEqual(len(base64.b64decode(request.skey)), TEST_MODULUS_BYTES)
        # 用测试私钥还原 type-2 padding 里的 {"key","iv"}。
        decoded = pow(
            int.from_bytes(base64.b64decode(request.skey), "big"), TEST_RSA_D, TEST_RSA_N
        ).to_bytes(TEST_MODULUS_BYTES, "big")
        self.assertEqual(decoded[:2], b"\x00\x02")
        separator = decoded.index(b"\x00", 2)
        document = json.loads(decoded[separator + 1 :])
        self.assertEqual(base64.b64decode(document["key"]), AES_KEY)
        self.assertEqual(base64.b64decode(document["iv"]), AES_IV)

    def test_tokens_are_random_but_mid_and_m2_keep_their_lengths(self):
        first = build_search_request(
            "微信",
            mid="a" * MID_LENGTH,
            m2="b" * M2_LENGTH,
            timestamp=1_700_000_000,
            rand=1234,
            public_key=TEST_PUBLIC_KEY,
            random_source=random.Random(1),
        )
        second = build_search_request(
            "微信",
            mid="a" * MID_LENGTH,
            m2="b" * M2_LENGTH,
            timestamp=1_700_000_000,
            rand=1234,
            public_key=TEST_PUBLIC_KEY,
            random_source=random.Random(2),
        )
        self.assertNotEqual(first.skey, second.skey)  # RSA 的 PS 随机填充

    def test_build_search_request_validates_arguments(self):
        cases = (
            dict(keyword=""),
            dict(mid=""),
            dict(timestamp=0),
            dict(rand=999),
            dict(rand=10000),
        )
        base = dict(
            keyword="微信",
            mid="M" * MID_LENGTH,
            m2="2" * M2_LENGTH,
            timestamp=1_700_000_000,
            rand=1234,
            public_key=TEST_PUBLIC_KEY,
        )
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                build_search_request(**{**base, **case})


class ResponseDecryptionTests(unittest.TestCase):
    def test_encrypted_list_is_decrypted_with_the_embedded_key_and_iv(self):
        items = [search_item(), search_item("105045671", "企业微信")]
        skey, data = encrypt_response_items(items)
        decrypted = decrypt_search_response(skey, data, TEST_PUBLIC_KEY)
        self.assertEqual([item["soft_id"] for item in decrypted["list"]], [600001358, 105045671])

    def test_a_random_aes_key_and_iv_are_honoured(self):
        key = bytes(range(16))
        iv = bytes(range(16, 32))
        skey, data = encrypt_response_items([search_item()], key=key, iv=iv)
        decrypted = decrypt_search_response(skey, data, TEST_PUBLIC_KEY)
        self.assertEqual(decrypted["list"][0]["name"], "微信")

    def test_invalid_base64_and_padding_are_rejected(self):
        with self.assertRaises(ValueError):
            decrypt_search_response("@@@@", "AAAA", TEST_PUBLIC_KEY)
        skey, data = encrypt_response_items([search_item()])
        with self.assertRaises(ValueError):
            decrypt_search_response(skey[:-8], data, TEST_PUBLIC_KEY)
        # skey 里的 AES 密钥与加密 data 的密钥不一致：AES 解密后 padding/UTF-8 必然失败。
        fixed_skey, _unused = encrypt_response_items([], key=AES_KEY, iv=AES_IV)
        _other_skey, other_data = encrypt_response_items(
            [], key=b"0123456789abcdef", iv=b"fedcba9876543210"
        )
        with self.assertRaises(ValueError):
            decrypt_search_response(fixed_skey, other_data, TEST_PUBLIC_KEY)

    def test_non_json_plaintext_is_rejected(self):
        plaintext = b"not json data"
        key_document = json.dumps(
            {
                "key": base64.b64encode(AES_KEY).decode(),
                "iv": base64.b64encode(AES_IV).decode(),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        skey = base64.b64encode(encode_type1_signature(key_document)).decode()
        data = base64.b64encode(aes_cbc_encrypt(AES_KEY, AES_IV, plaintext)).decode()
        with self.assertRaises(ValueError):
            decrypt_search_response(skey, data, TEST_PUBLIC_KEY)

    def test_non_aes_128_key_lengths_are_rejected(self):
        key_document = json.dumps(
            {
                "key": base64.b64encode(b"short-key").decode(),
                "iv": base64.b64encode(AES_IV).decode(),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        skey = base64.b64encode(encode_type1_signature(key_document)).decode()
        data = base64.b64encode(aes_cbc_encrypt(AES_KEY, AES_IV, b'{"list":[]}')).decode()
        with self.assertRaises(ValueError):
            decrypt_search_response(skey, data, TEST_PUBLIC_KEY)


# ---------------------------------------------------------------------------
# Soft360SearchClient（MockTransport 端到端）
# ---------------------------------------------------------------------------


class SearchClientTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self, upstream: FakeSearchUpstream) -> tuple[httpx2.AsyncClient, Soft360SearchClient]:
        http = httpx2.AsyncClient(transport=httpx2.MockTransport(upstream.handler))
        client = Soft360SearchClient(
            http,
            public_key=TEST_PUBLIC_KEY,
            clock=lambda: FIXED_NOW,
            random_source=random.Random(2026),
        )
        return http, client

    async def test_successful_search_sends_the_signed_encrypted_request(self):
        upstream = FakeSearchUpstream([search_item(), search_item("105045671", "企业微信")])
        http, client = self.make_client(upstream)
        async with http:
            items = await client.search("微信")
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["soft_id"], 600001358)
        self.assertEqual(len(upstream.requests), 1)
        request = upstream.requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.url.path, "/main/enc_client/v2/search/search")

        params = {key: value for key, value in request.url.params.items()}
        self.assertEqual(params["ver"], "14.0.0.0")
        self.assertEqual(params["timestamp"], str(int(FIXED_NOW)))
        self.assertEqual(len(params["mid"]), MID_LENGTH)
        self.assertEqual(len(params["m2"]), M2_LENGTH)
        self.assertTrue(params["mid"].isalnum() and params["m2"].isalnum())
        self.assertIn("sign", params)
        body = json.loads(request.content)
        self.assertEqual(set(body), {"q", "skey"})
        # 服务端视角：用排序后的基础参数 + 原始 skey/q 能复算出同一个签名。
        base = {key: params.get(key, "") for key in BASE_PARAM_KEYS}
        self.assertEqual(compute_signature(base, body["skey"], body["q"]), params["sign"])
        # q 能被固定 AES 密钥解回搜索词。
        payload = json.loads(aes_cbc_decrypt(AES_KEY, AES_IV, base64.b64decode(body["q"])))
        self.assertEqual(payload["kw"], "微信")
        self.assertEqual(payload["src"], "web_client")
        self.assertEqual(request.headers["content-type"], "application/json")

    async def test_empty_encrypted_payload_returns_an_empty_list(self):
        upstream = FakeSearchUpstream([])
        http, client = self.make_client(upstream)
        async with http:
            self.assertEqual(await client.search("不存在的软件"), [])

    async def test_empty_data_and_skey_is_treated_as_no_results(self):
        upstream = FakeSearchUpstream()
        upstream.mode = "empty"
        http, client = self.make_client(upstream)
        async with http:
            self.assertEqual(await client.search("微信"), [])

    async def test_upstream_errors_are_mapped(self):
        cases = (
            ("http-error", Soft360UnavailableError),
            ("timeout", Soft360TimeoutError),
            ("connection", Soft360UnavailableError),
            ("invalid-json", Soft360ProtocolError),
            ("top-level-list", Soft360ProtocolError),
            ("missing-errno", Soft360ProtocolError),
            ("errno", Soft360UnavailableError),
            ("bad-crypto", Soft360ProtocolError),
        )
        for mode, expected in cases:
            with self.subTest(mode=mode):
                upstream = FakeSearchUpstream()
                upstream.mode = mode
                http, client = self.make_client(upstream)
                async with http:
                    with self.assertRaises(expected):
                        await client.search("微信")

    async def test_client_configuration_is_validated(self):
        upstream = FakeSearchUpstream()
        http = httpx2.AsyncClient(transport=httpx2.MockTransport(upstream.handler))
        try:
            with self.assertRaises(ValueError):
                Soft360SearchClient(object())  # type: ignore[arg-type]
            with self.assertRaises(ValueError):
                Soft360SearchClient(http, timeout_seconds=0)
            with self.assertRaises(ValueError):
                Soft360SearchClient(http, connect_timeout_seconds=100, timeout_seconds=5)
            with self.assertRaises(ValueError):
                Soft360SearchClient(http, base_url="ftp://example.test")
            with self.assertRaises(ValueError):
                Soft360SearchClient(http, public_key=(0, 65537))  # type: ignore[arg-type]
            with self.assertRaises(ValueError):
                Soft360SearchClient(http, clock="not callable")  # type: ignore[arg-type]
            client = Soft360SearchClient(http, public_key=TEST_PUBLIC_KEY)
            async with http:
                with self.assertRaises(ValueError):
                    await client.search("   ")
        finally:
            if not http.is_closed:
                await http.aclose()

    async def test_the_service_uses_the_configured_timeout(self):
        upstream = FakeSearchUpstream()
        http = httpx2.AsyncClient(transport=httpx2.MockTransport(upstream.handler))
        client = Soft360SearchClient(
            http, timeout_seconds=7.5, connect_timeout_seconds=2.5, public_key=TEST_PUBLIC_KEY
        )
        timeout = client._timeout()  # noqa: SLF001 - 测试直接校验 timeout 配置
        self.assertEqual(
            (timeout.connect, timeout.read, timeout.write, timeout.pool),
            (2.5, 7.5, 7.5, 7.5),
        )
        async with http:
            self.assertEqual(len(await client.search("微信")), 1)


# ---------------------------------------------------------------------------
# 服务层：搜索映射 / 回退 / 目录外详情
# ---------------------------------------------------------------------------


class ServiceSearchTests(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def _service(self, search_client):
        directory = tempfile.mkdtemp(prefix="soft360-search-")
        self.addCleanup(_remove_directory, directory)
        upstream = FakeStoreUpstream()
        http, service = make_search_service(directory, upstream, search_client)
        try:
            async with http:
                yield service, upstream
        finally:
            await service.shutdown()

    async def test_remote_results_are_mapped_to_the_summary_contract(self):
        search_client = StaticSearchClient([search_item()])
        async with self._service(search_client) as (service, _upstream):
            response = await service.search("微信", limit=10)
        self.assertEqual(search_client.calls, ["微信"])
        self.assertEqual(response.total, 1)
        self.assertEqual(response.query, "微信")
        app = response.items[0]
        self.assertEqual(app.id, "soft360-600001358")
        self.assertEqual(app.soft_id, "600001358")
        self.assertEqual(app.name, "微信")
        # 搜索条目没有独立 bname 字段，按契约回填 name。
        self.assertEqual(app.bname, "微信")
        self.assertEqual(app.description, "一个生活方式")
        self.assertEqual(app.category, "聊天工具")
        self.assertEqual(app.category_id, "149")
        self.assertEqual(app.size_bytes, 259798216)
        self.assertEqual(app.score, 82)
        self.assertEqual(
            app.icon_url, "https://p8.qhimg.com/dm/72_72_100/t11eec6e52fecc71f655bb449f0.png"
        )
        self.assertEqual(app.icon_proxy_url, "/api/v1/store/360/apps/600001358/icon")
        self.assertEqual(app.architectures, ["x64"])
        self.assertEqual(app.minimum_windows, "7")
        self.assertEqual(app.license, "免费")
        self.assertTrue(app.is_free)

    async def test_limit_truncates_and_duplicates_are_dropped(self):
        items = [
            search_item("1", "A"),
            search_item("1", "A"),
            search_item("2", "B"),
            search_item("3", "C"),
        ]
        search_client = StaticSearchClient(items)
        async with self._service(search_client) as (service, _upstream):
            limited = await service.search("A", limit=2)
            unlimited = await service.search("A", limit=999)
        self.assertEqual([app.soft_id for app in limited.items], ["1", "2"])
        self.assertEqual(limited.total, 2)
        # limit 上限是 50（接口每次最多返回 50 条），999 会被夹到 50。
        self.assertEqual(unlimited.total, 3)

    async def test_invalid_items_are_skipped(self):
        items = [
            search_item(),
            {"name": "没有 soft_id"},
            {"soft_id": 12, "logo": "https://p0.qhimg.com/x.png"},  # 没有 name
            search_item("8", "广告", type=2),  # 非软件条目
            search_item("9", "有效", logo=None),
        ]
        async with self._service(StaticSearchClient(items)) as (service, _upstream):
            response = await service.search("微信", limit=10)
        self.assertEqual([app.soft_id for app in response.items], ["600001358", "9"])
        # 缺 logo 时用防御性兜底图标，条目仍然可用。
        self.assertEqual(response.items[1].icon_url, DEFAULT_ICON_URL)

    async def test_remote_failure_falls_back_to_the_cached_catalog(self):
        directory = tempfile.mkdtemp(prefix="soft360-search-")
        self.addCleanup(_remove_directory, directory)
        upstream = FakeStoreUpstream(pages={("149", 1): [list_item("1010", "微信")]})
        search_client = StaticSearchClient(error=Soft360UnavailableError("搜索接口 502"))
        http, service = make_search_service(directory, upstream, search_client)
        try:
            async with http:
                await service.refresh_catalog()
                requests_before = len(upstream.requests)
                response = await service.search("微信", limit=10)
                # 本地回退只读内存快照，不会再访问 360。
                self.assertEqual(len(upstream.requests), requests_before)
        finally:
            await service.shutdown()
        self.assertEqual(search_client.calls, ["微信"])
        self.assertEqual([app.soft_id for app in response.items], ["1010"])
        self.assertEqual(response.total, 1)

    async def test_empty_remote_result_falls_back_to_the_cached_catalog(self):
        directory = tempfile.mkdtemp(prefix="soft360-search-")
        self.addCleanup(_remove_directory, directory)
        upstream = FakeStoreUpstream(pages={("149", 1): [list_item("1010", "微信")]})
        http, service = make_search_service(directory, upstream, StaticSearchClient([]))
        try:
            async with http:
                await service.refresh_catalog()
                response = await service.search("微信", limit=10)
        finally:
            await service.shutdown()
        self.assertEqual([app.soft_id for app in response.items], ["1010"])

    async def test_unusable_remote_items_also_fall_back(self):
        directory = tempfile.mkdtemp(prefix="soft360-search-")
        self.addCleanup(_remove_directory, directory)
        upstream = FakeStoreUpstream(pages={("149", 1): [list_item("1010", "微信")]})
        http, service = make_search_service(
            directory, upstream, StaticSearchClient([{"name": "没有 id"}])
        )
        try:
            async with http:
                await service.refresh_catalog()
                response = await service.search("微信", limit=10)
        finally:
            await service.shutdown()
        self.assertEqual([app.soft_id for app in response.items], ["1010"])

    async def test_no_catalog_and_a_failing_search_return_an_empty_result(self):
        search_client = StaticSearchClient(error=Soft360TimeoutError("搜索超时"))
        async with self._service(search_client) as (service, upstream):
            response = await service.search("微信", limit=10)
        self.assertEqual(response.total, 0)
        self.assertEqual(response.items, [])
        self.assertEqual(len(upstream.requests), 0)

    async def test_blank_queries_do_not_reach_the_remote_search(self):
        search_client = StaticSearchClient([search_item()])
        async with self._service(search_client) as (service, _upstream):
            response = await service.search("   ")
        self.assertEqual(response.total, 0)
        self.assertEqual(search_client.calls, [])

    async def test_unexpected_search_errors_still_fall_back(self):
        search_client = StaticSearchClient(error=RuntimeError("boom"))
        with self.assertLogs("winappsmanager.soft360", level="WARNING"):
            async with self._service(search_client) as (service, _upstream):
                response = await service.search("微信", limit=10)
        self.assertEqual(response.total, 0)


class DirectoryOutTests(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def _service(self, infos, pages=None):
        directory = tempfile.mkdtemp(prefix="soft360-out-")
        self.addCleanup(_remove_directory, directory)
        upstream = FakeStoreUpstream(infos=infos, pages=pages)
        http, service = make_search_service(directory, upstream, StaticSearchClient([]))
        try:
            async with http:
                yield service, upstream
        finally:
            await service.shutdown()

    async def test_details_outside_the_catalog_are_synthesized_from_getinfo(self):
        async with self._service({"600001358": info_payload()}) as (service, _upstream):
            details = await service.get_details("600001358")
        self.assertEqual(details.id, "soft360-600001358")
        self.assertEqual(details.name, "微信")
        self.assertEqual(details.category, "聊天工具")
        self.assertEqual(details.category_id, "149")
        self.assertEqual(details.version, "4.2.0.52")
        self.assertEqual(details.publisher, "腾讯")
        self.assertEqual(details.long_description, "微信是一款跨平台的通讯工具")
        self.assertEqual(details.size_bytes, 259798216)
        self.assertEqual(details.score, 90)
        # getinfo 的 http logo 被规范化成 https，并覆盖 summary 的兜底图标。
        self.assertEqual(details.icon_url, "https://p0.qhimg.com/logo/72.png")
        self.assertEqual(details.icon_proxy_url, "/api/v1/store/360/apps/600001358/icon")
        self.assertEqual(details.architectures, ["x64"])
        self.assertEqual(details.minimum_windows, "7")
        self.assertEqual(
            [(shot.url, shot.proxy_url) for shot in details.screenshots],
            [
                (
                    "https://p0.qhimg.com/shot1.png",
                    "/api/v1/store/360/apps/600001358/screenshots/0",
                ),
                (
                    "https://p0.qhimg.com/shot2.png",
                    "/api/v1/store/360/apps/600001358/screenshots/1",
                ),
            ],
        )

    async def test_prefixed_ids_are_accepted_for_directory_out_details(self):
        async with self._service({"600001358": info_payload()}) as (service, _upstream):
            details = await service.get_details("soft360-600001358")
        self.assertEqual(details.soft_id, "600001358")

    async def test_unknown_software_outside_the_catalog_still_maps_to_404(self):
        async with self._service({}) as (service, _upstream):
            with self.assertRaises(Soft360NotFoundError):
                await service.get_details("9999")
            with self.assertRaises(Soft360NotFoundError):
                await service.get_icon("9999")
            with self.assertRaises(Soft360NotFoundError):
                await service.resolve_download("9999")
            with self.assertRaises(Soft360NotFoundError):
                await service.get_screenshot("9999", 0)

    async def test_downloads_outside_the_catalog_are_resolved_live(self):
        async with self._service({"600001358": info_payload()}) as (service, _upstream):
            release = await service.resolve_download("600001358", architecture="x64")
        self.assertIsInstance(release, Release)
        self.assertEqual(str(release.download_url), "https://dl.360.cn/wechat_4.2.0.52.exe")
        self.assertEqual(release.version, "4.2.0.52")
        self.assertEqual(release.architecture, "x64")
        self.assertEqual(release.file_name, "WeChat_4.2.0.52.exe")
        self.assertEqual(release.size_bytes, 259798216)
        self.assertIsNone(release.sha256)
        self.assertEqual(release.id, "soft360-600001358-4.2.0.52")

    async def test_icon_and_screenshots_outside_the_catalog_are_proxied(self):
        async with self._service({"600001358": info_payload()}) as (service, upstream):
            icon = await service.get_icon("600001358")
            screenshot = await service.get_screenshot("600001358", 1)
            missing = None
            try:
                await service.get_screenshot("600001358", 5)
            except Soft360NotFoundError as error:
                missing = error
        self.assertEqual(icon.content, PNG_BYTES)
        self.assertEqual(icon.content_type, "image/png")
        self.assertEqual(screenshot.content, PNG_BYTES)
        self.assertIsNotNone(missing)
        fetched = [str(request.url) for request in upstream.requests]
        self.assertIn("https://p0.qhimg.com/logo/72.png", fetched)
        self.assertIn("https://p0.qhimg.com/shot2.png", fetched)

    async def test_a_catalog_entry_still_uses_the_catalog_summary(self):
        pages = {("149", 1): [list_item("1010", "微信")]}
        async with self._service({"1010": info_payload()}, pages=pages) as (service, _upstream):
            await service.refresh_catalog()
            details = await service.get_details("1010")
        self.assertEqual(details.name, "微信")
        self.assertEqual(details.version, "4.2.0.52")
        self.assertTrue(details.icon_proxy_url.endswith("/1010/icon"))


# ---------------------------------------------------------------------------
# 路由层：/api/v1/store/360/search（ASGITransport，不启动 lifespan）
# ---------------------------------------------------------------------------


class SearchRouteTests(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def _client(self, search_client, *, pages=None, refresh_catalog=False):
        """起一个真实服务 + ``/api/v1/store/360/search`` 路由（不启动 lifespan）。"""

        directory = tempfile.mkdtemp(prefix="soft360-route-")
        self.addCleanup(_remove_directory, directory)
        upstream = FakeStoreUpstream(pages=pages)
        http, service = make_search_service(directory, upstream, search_client)
        try:
            async with http:
                if refresh_catalog:
                    await service.refresh_catalog()
                app = create_app(settings=store_settings(), soft360_service=service)
                app.state.soft360 = service
                async with httpx2.AsyncClient(
                    transport=httpx2.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    yield client
        finally:
            await service.shutdown()

    async def test_remote_search_is_exposed_with_directory_out_items(self):
        async with self._client(StaticSearchClient([search_item()])) as client:
            response = await client.get("/api/v1/store/360/search", params={"q": "微信", "limit": 10})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        payload = response.json()
        self.assertEqual(payload["query"], "微信")
        self.assertEqual(payload["total"], 1)
        item = payload["items"][0]
        self.assertEqual(item["id"], "soft360-600001358")
        self.assertEqual(item["soft_id"], "600001358")
        self.assertEqual(item["architectures"], ["x64"])
        self.assertEqual(item["icon_proxy_url"], "/api/v1/store/360/apps/600001358/icon")

    async def test_search_falls_back_to_the_catalog_instead_of_failing(self):
        pages = {("149", 1): [list_item("1010", "微信")]}
        async with self._client(
            StaticSearchClient(error=Soft360ProtocolError("解密失败")),
            pages=pages,
            refresh_catalog=True,
        ) as client:
            response = await client.get("/api/v1/store/360/search", params={"q": "微信"})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual([item["id"] for item in payload["items"]], ["soft360-1010"])

    async def test_search_returns_an_empty_result_when_everything_fails(self):
        async with self._client(
            StaticSearchClient(error=Soft360UnavailableError("上游 502"))
        ) as client:
            response = await client.get("/api/v1/store/360/search", params={"q": "missing"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total"], 0)

    async def test_search_validates_query_and_limit(self):
        search_client = StaticSearchClient([search_item()])
        async with self._client(search_client) as client:
            for path in (
                "/api/v1/store/360/search",
                "/api/v1/store/360/search?q=",
                "/api/v1/store/360/search?q=a&limit=0",
                "/api/v1/store/360/search?q=a&limit=51",
            ):
                with self.subTest(path=path):
                    response = await client.get(path)
                    self.assertEqual(response.status_code, 422)
        self.assertEqual(search_client.calls, [])


class DirectoryOutRouteTests(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def _client(self, infos):
        directory = tempfile.mkdtemp(prefix="soft360-out-route-")
        self.addCleanup(_remove_directory, directory)
        upstream = FakeStoreUpstream(infos=infos)
        http, service = make_search_service(directory, upstream, StaticSearchClient([]))
        try:
            async with http:
                app = create_app(settings=store_settings(), soft360_service=service)
                app.state.soft360 = service
                async with httpx2.AsyncClient(
                    transport=httpx2.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    yield client
        finally:
            await service.shutdown()

    async def test_directory_out_details_download_icon_and_screenshot_are_served(self):
        async with self._client({"600001358": info_payload()}) as client:
            details = await client.get("/api/v1/store/360/apps/600001358")
            download = await client.get("/api/v1/store/360/apps/600001358/download")
            icon = await client.get("/api/v1/store/360/apps/600001358/icon")
            screenshot = await client.get("/api/v1/store/360/apps/600001358/screenshots/1")
        self.assertEqual(details.status_code, 200)
        self.assertEqual(details.headers["cache-control"], "no-store")
        payload = details.json()
        self.assertEqual(payload["id"], "soft360-600001358")
        self.assertEqual(payload["soft_id"], "600001358")
        self.assertEqual(payload["name"], "微信")
        self.assertEqual(payload["version"], "4.2.0.52")
        self.assertEqual(payload["icon_url"], "https://p0.qhimg.com/logo/72.png")
        self.assertEqual(
            [(shot["url"], shot["proxy_url"]) for shot in payload["screenshots"]],
            [
                (
                    "https://p0.qhimg.com/shot1.png",
                    "/api/v1/store/360/apps/600001358/screenshots/0",
                ),
                (
                    "https://p0.qhimg.com/shot2.png",
                    "/api/v1/store/360/apps/600001358/screenshots/1",
                ),
            ],
        )
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.headers["cache-control"], "no-store")
        release = download.json()
        self.assertEqual(release["id"], "soft360-600001358-4.2.0.52")
        self.assertEqual(release["file_name"], "WeChat_4.2.0.52.exe")
        self.assertEqual(release["architecture"], "x64")
        self.assertIsNone(release["sha256"])
        self.assertEqual(icon.status_code, 200)
        self.assertEqual(icon.content, PNG_BYTES)
        self.assertEqual(icon.headers["content-type"], "image/png")
        self.assertEqual(screenshot.status_code, 200)
        self.assertEqual(screenshot.content, PNG_BYTES)

    async def test_unknown_software_still_maps_to_404(self):
        async with self._client({}) as client:
            for path in (
                "/api/v1/store/360/apps/9999",
                "/api/v1/store/360/apps/9999/download",
                "/api/v1/store/360/apps/9999/icon",
                "/api/v1/store/360/apps/9999/screenshots/0",
            ):
                with self.subTest(path=path):
                    response = await client.get(path)
                    self.assertEqual(response.status_code, 404)


def _remove_directory(path: str) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
