"""360 加密搜索接口（``enc_client/v2/search/search``）的纯 Python 实现。

360 软件管家的网页搜索不在明文目录接口里，而是走签名接口：

- ``POST https://mgr.soft.360.cn/main/enc_client/v2/search/search``，query 参数包含
  ``ver/os_ver/os_bit/mid/m2/timestamp/sk_type/sk_bit/rand`` 与 ``sign``；
- 请求体 ``{"q": ..., "skey": ...}``：``q`` 是 AES-128-CBC（key/iv 固定、PKCS#7）加密后
  的搜索 JSON 的 base64；``skey`` 是用 360 的 RSA 公钥按 PKCS#1 v1.5 type-2 加密
  ``{"key": base64(key), "iv": base64(iv)}`` 后的 base64；
- ``sign`` 是 MD5：基础参数按 key 排序、过滤空值拼成 ``k=v&...``，再依次拼接**原始**
  ``skey``、``q`` 与固定 salt；
- 响应 ``{"errno","errmsg","data","skey"}`` 里的 ``skey`` 是 360 用同一 RSA 私钥按
  PKCS#1 v1.5 type-1 padding 处理过的 JSON，用公钥做模幂即可还原出 ``{"key","iv"}``，
  再用它 AES-CBC 解 ``data`` 得到 ``{"list":[...]}``（实测每次最多 50 条）。

``Backend/requirements.txt`` 没有密码学依赖且不允许新增，所以 AES/RSA/MD5 都在这里用纯
Python 实现；它们只服务于 360 搜索，不用于任何安全用途。

错误映射沿用例表：超时 → :class:`Soft360TimeoutError`（504）；HTTP/网络/``errno`` 失败 →
:class:`Soft360UnavailableError`（502）；响应包络或加解密异常 →
:class:`Soft360ProtocolError`（502）。``errno=0`` 且 ``data``/``skey`` 为空时视为没有
结果，返回空列表（服务层会再回退本地目录匹配）。
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import math
import random
import string
import time
from typing import Any
from urllib.parse import quote

import httpx2

from .errors import (
    Soft360ProtocolError,
    Soft360TimeoutError,
    Soft360UnavailableError,
)
from .media import BROWSER_USER_AGENT

# ---------------------------------------------------------------------------
# 接口常量（逆向自 360 软件管家网页客户端，2026-11 实测）
# ---------------------------------------------------------------------------

SEARCH_BASE_URL = "https://mgr.soft.360.cn"
SEARCH_PATH = "/main/enc_client/v2/search/search"
SEARCH_REFERER = "https://cdn.soft.360.cn/"
SEARCH_ORIGIN = "https://cdn.soft.360.cn"
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
DEFAULT_TIMEOUT_SECONDS = 20.0
# 接口不提供 limit 参数，实测每次最多返回 50 条；客户端 limit 只做截断。

CLIENT_VERSION = "14.0.0.0"
OS_VERSION = "7"
OS_BIT = "64"
SK_TYPE = "1"
SK_BIT = "2048"
RANDOM_TOKEN_ALPHABET = string.ascii_letters + string.digits
MID_LENGTH = 28
M2_LENGTH = 43
# 参与签名的 query 参数（顺序无关，签名前按 key 排序；空值会被过滤）。
BASE_PARAM_KEYS = (
    "ver",
    "os_ver",
    "os_bit",
    "sdk_ver",
    "mid",
    "m2",
    "timestamp",
    "sk_type",
    "sk_bit",
    "rand",
)
# 签名 salt：360 网页客户端里硬编码的固定串。
SIGN_SALT = "sjgo144ddycl3396ddumrn3qmag7eegm"

# 请求与响应使用的对称密钥（固定值，网页客户端硬编码）。
AES_KEY = b"1234123412ABCDEF"
AES_IV = b"ABCDEF1234123412"

# 360 搜索接口使用的 RSA 公钥（SPKI DER 的 base64，2048 位 / e=65537）。
RSA_PUBLIC_KEY_SPKI_B64 = (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA4RyJX41Y/K/okDxqVwzf2S3Ni2hhYO1dW887gyX6w2+EHzYoOTJ9"
    "+DwljvHU9f1Y4IR1UHRILC93yJYg9o8Opq5HTCR5bIa4IeXz1dlBU+WwFEnHLN22Ih2V3XG2kMDpPBgMdXLFYQX2u6P96YaWK"
    "192s92NvPPVhl5jX8u58KMMJOCYGHnk7iFT1LSSJCXS0jRh6GD3vkz7wMFUCTq54OHvGEH8CD3m69cRvOvL9UujXpNt3lDnr57OG"
    "TJzdF2s1g7HFLmN1rXv1MPIosIDyVRn5vrfB8wjplkzpW33PyLVX3v9nwUBxYADsKC2OTg0M5hRkWFQERMcUe8j3B1t8wIDAQAB"
)

# q 明文里的固定字段（``kw`` 是搜索词，``mid`` / ``m2`` 随机生成）。
_SEARCH_SOURCE = "web_client"
_SEARCH_EXT_PARAM = "schannel="
_SEARCH_LANGUAGE = "zh-CN"
_SEARCH_COUNTRY = "CN"

_REQUEST_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": SEARCH_ORIGIN,
    "Referer": SEARCH_REFERER,
    "User-Agent": BROWSER_USER_AGENT,
}


# ---------------------------------------------------------------------------
# AES-128-CBC + PKCS#7（纯 Python）
# ---------------------------------------------------------------------------

SBOX = (
    0x63, 0x7C, 0x77, 0x7B, 0xF2, 0x6B, 0x6F, 0xC5, 0x30, 0x01, 0x67, 0x2B, 0xFE, 0xD7, 0xAB, 0x76,
    0xCA, 0x82, 0xC9, 0x7D, 0xFA, 0x59, 0x47, 0xF0, 0xAD, 0xD4, 0xA2, 0xAF, 0x9C, 0xA4, 0x72, 0xC0,
    0xB7, 0xFD, 0x93, 0x26, 0x36, 0x3F, 0xF7, 0xCC, 0x34, 0xA5, 0xE5, 0xF1, 0x71, 0xD8, 0x31, 0x15,
    0x04, 0xC7, 0x23, 0xC3, 0x18, 0x96, 0x05, 0x9A, 0x07, 0x12, 0x80, 0xE2, 0xEB, 0x27, 0xB2, 0x75,
    0x09, 0x83, 0x2C, 0x1A, 0x1B, 0x6E, 0x5A, 0xA0, 0x52, 0x3B, 0xD6, 0xB3, 0x29, 0xE3, 0x2F, 0x84,
    0x53, 0xD1, 0x00, 0xED, 0x20, 0xFC, 0xB1, 0x5B, 0x6A, 0xCB, 0xBE, 0x39, 0x4A, 0x4C, 0x58, 0xCF,
    0xD0, 0xEF, 0xAA, 0xFB, 0x43, 0x4D, 0x33, 0x85, 0x45, 0xF9, 0x02, 0x7F, 0x50, 0x3C, 0x9F, 0xA8,
    0x51, 0xA3, 0x40, 0x8F, 0x92, 0x9D, 0x38, 0xF5, 0xBC, 0xB6, 0xDA, 0x21, 0x10, 0xFF, 0xF3, 0xD2,
    0xCD, 0x0C, 0x13, 0xEC, 0x5F, 0x97, 0x44, 0x17, 0xC4, 0xA7, 0x7E, 0x3D, 0x64, 0x5D, 0x19, 0x73,
    0x60, 0x81, 0x4F, 0xDC, 0x22, 0x2A, 0x90, 0x88, 0x46, 0xEE, 0xB8, 0x14, 0xDE, 0x5E, 0x0B, 0xDB,
    0xE0, 0x32, 0x3A, 0x0A, 0x49, 0x06, 0x24, 0x5C, 0xC2, 0xD3, 0xAC, 0x62, 0x91, 0x95, 0xE4, 0x79,
    0xE7, 0xC8, 0x37, 0x6D, 0x8D, 0xD5, 0x4E, 0xA9, 0x6C, 0x56, 0xF4, 0xEA, 0x65, 0x7A, 0xAE, 0x08,
    0xBA, 0x78, 0x25, 0x2E, 0x1C, 0xA6, 0xB4, 0xC6, 0xE8, 0xDD, 0x74, 0x1F, 0x4B, 0xBD, 0x8B, 0x8A,
    0x70, 0x3E, 0xB5, 0x66, 0x48, 0x03, 0xF6, 0x0E, 0x61, 0x35, 0x57, 0xB9, 0x86, 0xC1, 0x1D, 0x9E,
    0xE1, 0xF8, 0x98, 0x11, 0x69, 0xD9, 0x8E, 0x94, 0x9B, 0x1E, 0x87, 0xE9, 0xCE, 0x55, 0x28, 0xDF,
    0x8C, 0xA1, 0x89, 0x0D, 0xBF, 0xE6, 0x42, 0x68, 0x41, 0x99, 0x2D, 0x0F, 0xB0, 0x54, 0xBB, 0x16,
)

INV_SBOX = tuple(SBOX.index(value) for value in range(256))
RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)
_AES_BLOCK_SIZE = 16
_AES_ROUNDS = 10


def _xtime(value: int) -> int:
    """GF(2^8) 乘 2（AES 的 ``xtime``）。"""

    shifted = (value << 1) & 0xFF
    if value & 0x80:
        shifted ^= 0x1B
    return shifted


def _gf_mul(left: int, right: int) -> int:
    """GF(2^8) 乘法（用于预计算 MixColumns 常数表）。"""

    result = 0
    while right:
        if right & 1:
            result ^= left
        left = _xtime(left)
        right >>= 1
    return result & 0xFF


# MixColumns / InvMixColumns 用到的固定乘数表：预计算后每列只需查表异或。
_MUL2 = tuple(_gf_mul(value, 2) for value in range(256))
_MUL3 = tuple(_gf_mul(value, 3) for value in range(256))
_MUL9 = tuple(_gf_mul(value, 9) for value in range(256))
_MUL11 = tuple(_gf_mul(value, 11) for value in range(256))
_MUL13 = tuple(_gf_mul(value, 13) for value in range(256))
_MUL14 = tuple(_gf_mul(value, 14) for value in range(256))


class AES128:
    """纯 Python AES-128 单块加解密（16 字节 key / 16 字节块）。

    状态按列优先存放：``state[4*column + row]``，与 FIPS-197 的字节顺序一致。
    """

    def __init__(self, key: bytes) -> None:
        if not isinstance(key, (bytes, bytearray)) or len(key) != _AES_BLOCK_SIZE:
            raise ValueError("AES-128 key 必须是 16 字节")
        self._round_keys = _expand_key(bytes(key))

    def encrypt_block(self, block: bytes) -> bytes:
        """加密一个 16 字节明文块。"""

        state = _add_round_key(_as_block(block), self._round_keys[0])
        for round_index in range(1, _AES_ROUNDS):
            state = _shift_rows(_sub_bytes(state))
            state = _add_round_key(_mix_columns(state), self._round_keys[round_index])
        state = _shift_rows(_sub_bytes(state))
        state = _add_round_key(state, self._round_keys[_AES_ROUNDS])
        return bytes(state)

    def decrypt_block(self, block: bytes) -> bytes:
        """解密一个 16 字节密文块。"""

        state = _add_round_key(_as_block(block), self._round_keys[_AES_ROUNDS])
        for round_index in range(_AES_ROUNDS - 1, 0, -1):
            state = _inv_sub_bytes(_inv_shift_rows(state))
            state = _inv_mix_columns(_add_round_key(state, self._round_keys[round_index]))
        state = _inv_sub_bytes(_inv_shift_rows(state))
        state = _add_round_key(state, self._round_keys[0])
        return bytes(state)


def _as_block(value: bytes) -> list[int]:
    if not isinstance(value, (bytes, bytearray)) or len(value) != _AES_BLOCK_SIZE:
        raise ValueError("AES 数据块必须是 16 字节")
    return list(value)


def _expand_key(key: bytes) -> tuple[list[int], ...]:
    """AES-128 key schedule：44 个 4 字节字，按 11 轮分组。"""

    words = [list(key[offset : offset + 4]) for offset in range(0, _AES_BLOCK_SIZE, 4)]
    for index in range(4, 44):
        word = list(words[index - 1])
        if index % 4 == 0:
            word = word[1:] + word[:1]
            word = [SBOX[value] for value in word]
            word[0] ^= RCON[index // 4 - 1]
        words.append([words[index - 4][position] ^ word[position] for position in range(4)])
    return tuple(
        [value for word in words[round_index * 4 : round_index * 4 + 4] for value in word]
        for round_index in range(_AES_ROUNDS + 1)
    )


def _add_round_key(state: list[int], round_key: list[int]) -> list[int]:
    return [value ^ key_byte for value, key_byte in zip(state, round_key)]


def _sub_bytes(state: list[int]) -> list[int]:
    return [SBOX[value] for value in state]


def _inv_sub_bytes(state: list[int]) -> list[int]:
    return [INV_SBOX[value] for value in state]


def _shift_rows(state: list[int]) -> list[int]:
    # 等价于对每一行循环左移 row 个字节（状态按列优先存放）。
    return [
        state[0], state[5], state[10], state[15],
        state[4], state[9], state[14], state[3],
        state[8], state[13], state[2], state[7],
        state[12], state[1], state[6], state[11],
    ]


def _inv_shift_rows(state: list[int]) -> list[int]:
    return [
        state[0], state[13], state[10], state[7],
        state[4], state[1], state[14], state[11],
        state[8], state[5], state[2], state[15],
        state[12], state[9], state[6], state[3],
    ]


def _mix_columns(state: list[int]) -> list[int]:
    mixed = [0] * _AES_BLOCK_SIZE
    for column in range(0, _AES_BLOCK_SIZE, 4):
        first, second, third, fourth = state[column : column + 4]
        mixed[column] = _MUL2[first] ^ _MUL3[second] ^ third ^ fourth
        mixed[column + 1] = first ^ _MUL2[second] ^ _MUL3[third] ^ fourth
        mixed[column + 2] = first ^ second ^ _MUL2[third] ^ _MUL3[fourth]
        mixed[column + 3] = _MUL3[first] ^ second ^ third ^ _MUL2[fourth]
    return mixed


def _inv_mix_columns(state: list[int]) -> list[int]:
    mixed = [0] * _AES_BLOCK_SIZE
    for column in range(0, _AES_BLOCK_SIZE, 4):
        first, second, third, fourth = state[column : column + 4]
        mixed[column] = _MUL14[first] ^ _MUL11[second] ^ _MUL13[third] ^ _MUL9[fourth]
        mixed[column + 1] = _MUL9[first] ^ _MUL14[second] ^ _MUL11[third] ^ _MUL13[fourth]
        mixed[column + 2] = _MUL13[first] ^ _MUL9[second] ^ _MUL14[third] ^ _MUL11[fourth]
        mixed[column + 3] = _MUL11[first] ^ _MUL13[second] ^ _MUL9[third] ^ _MUL14[fourth]
    return mixed


def _validate_cipher_key(key: bytes, iv: bytes) -> None:
    if not isinstance(key, (bytes, bytearray)) or len(key) != _AES_BLOCK_SIZE:
        raise ValueError("AES-128 key 必须是 16 字节")
    if not isinstance(iv, (bytes, bytearray)) or len(iv) != _AES_BLOCK_SIZE:
        raise ValueError("AES-CBC iv 必须是 16 字节")


def aes_cbc_encrypt(key: bytes, iv: bytes, plaintext: bytes) -> bytes:
    """AES-128-CBC 加密并追加 PKCS#7 填充（空明文也会填充一个整块）。"""

    _validate_cipher_key(key, iv)
    if not isinstance(plaintext, (bytes, bytearray)):
        raise ValueError("plaintext 必须是 bytes")
    padding = _AES_BLOCK_SIZE - len(plaintext) % _AES_BLOCK_SIZE
    data = bytes(plaintext) + bytes([padding]) * padding
    cipher = AES128(key)
    encrypted = bytearray()
    previous = bytes(iv)
    for offset in range(0, len(data), _AES_BLOCK_SIZE):
        block = bytes(
            value ^ mask for value, mask in zip(data[offset : offset + _AES_BLOCK_SIZE], previous)
        )
        previous = cipher.encrypt_block(block)
        encrypted.extend(previous)
    return bytes(encrypted)


def aes_cbc_decrypt(key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
    """AES-128-CBC 解密并校验/剥掉 PKCS#7 填充。"""

    _validate_cipher_key(key, iv)
    if not isinstance(ciphertext, (bytes, bytearray)) or not ciphertext:
        raise ValueError("ciphertext 必须是非空 bytes")
    if len(ciphertext) % _AES_BLOCK_SIZE != 0:
        raise ValueError("ciphertext 长度必须是 16 的倍数")
    cipher = AES128(key)
    decrypted = bytearray()
    previous = bytes(iv)
    for offset in range(0, len(ciphertext), _AES_BLOCK_SIZE):
        block = bytes(ciphertext[offset : offset + _AES_BLOCK_SIZE])
        plain_block = cipher.decrypt_block(block)
        decrypted.extend(
            value ^ mask for value, mask in zip(plain_block, previous)
        )
        previous = block
    padding = decrypted[-1]
    if not 1 <= padding <= _AES_BLOCK_SIZE or decrypted[-padding:] != bytes([padding]) * padding:
        raise ValueError("PKCS#7 填充无效")
    return bytes(decrypted[:-padding])


# ---------------------------------------------------------------------------
# RSA PKCS#1 v1.5（纯 Python，仅公钥运算 + 用公钥还原 type-1 padding）
# ---------------------------------------------------------------------------


def _der_read_tlv(data: bytes, offset: int) -> tuple[int, bytes, int]:
    """读取一个 DER TLV，返回 ``(tag, value, next_offset)``（只支持短/长格式长度）。"""

    if offset + 2 > len(data):
        raise ValueError("DER 数据不完整")
    tag = data[offset]
    length = data[offset + 1]
    offset += 2
    if length & 0x80:
        length_bytes = length & 0x7F
        if length_bytes == 0 or length_bytes > 4 or offset + length_bytes > len(data):
            raise ValueError("DER 长度无效")
        length = int.from_bytes(data[offset : offset + length_bytes], "big")
        offset += length_bytes
    end = offset + length
    if end > len(data):
        raise ValueError("DER 数据不完整")
    return tag, data[offset:end], end


def parse_rsa_public_key(spki_b64: str) -> tuple[int, int]:
    """解析 SPKI（``SubjectPublicKeyInfo``）base64，返回 ``(n, e)``。"""

    if not isinstance(spki_b64, str) or not spki_b64.strip():
        raise ValueError("RSA 公钥必须是 base64 字符串")
    try:
        der = base64.b64decode(spki_b64, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("RSA 公钥不是有效的 base64") from None
    tag, spki, _ = _der_read_tlv(der, 0)
    if tag != 0x30:
        raise ValueError("RSA 公钥缺少 SPKI SEQUENCE")
    tag, _algorithm, offset = _der_read_tlv(spki, 0)
    if tag != 0x30:
        raise ValueError("RSA 公钥缺少 AlgorithmIdentifier")
    tag, bit_string, _ = _der_read_tlv(spki, offset)
    if tag != 0x03 or not bit_string or bit_string[0] != 0:
        raise ValueError("RSA 公钥缺少 BIT STRING")
    tag, rsa_public_key, _ = _der_read_tlv(bit_string, 1)
    if tag != 0x30:
        raise ValueError("RSA 公钥缺少 RSAPublicKey SEQUENCE")
    tag, modulus_bytes, offset = _der_read_tlv(rsa_public_key, 0)
    if tag != 0x02:
        raise ValueError("RSA 公钥缺少模数")
    tag, exponent_bytes, _ = _der_read_tlv(rsa_public_key, offset)
    if tag != 0x02:
        raise ValueError("RSA 公钥缺少指数")
    modulus = int.from_bytes(modulus_bytes, "big")
    exponent = int.from_bytes(exponent_bytes, "big")
    if modulus <= 0 or exponent <= 0:
        raise ValueError("RSA 公钥参数无效")
    return modulus, exponent


RSA_PUBLIC_KEY = parse_rsa_public_key(RSA_PUBLIC_KEY_SPKI_B64)


def _modulus_size_bytes(public_key: tuple[int, int]) -> int:
    modulus, _exponent = public_key
    return (modulus.bit_length() + 7) // 8


def rsa_pkcs1_v15_encrypt(
    data: bytes,
    public_key: tuple[int, int] = RSA_PUBLIC_KEY,
    *,
    random_source: Any = None,
) -> bytes:
    """按 PKCS#1 v1.5 type-2 padding 用公钥加密（请求 ``skey`` 用）。"""

    if not isinstance(data, (bytes, bytearray)):
        raise ValueError("data 必须是 bytes")
    modulus, exponent = public_key
    size = _modulus_size_bytes(public_key)
    if len(data) > size - 11:
        raise ValueError("PKCS#1 v1.5 明文过长")
    random_source = random_source if random_source is not None else random.SystemRandom()
    padding = bytes(
        random_source.randint(1, 255) for _ in range(size - len(data) - 3)
    )
    encoded = b"\x00\x02" + padding + b"\x00" + bytes(data)
    cipher = pow(int.from_bytes(encoded, "big"), exponent, modulus)
    return cipher.to_bytes(size, "big")


def rsa_pkcs1_v15_public_decrypt(
    signature: bytes,
    public_key: tuple[int, int] = RSA_PUBLIC_KEY,
) -> bytes:
    """用公钥做模幂，还原 PKCS#1 v1.5 type-1 padding 里的数据（响应 ``skey`` 用）。

    360 服务端用 RSA 私钥按 type-1 padding（``00 01 FF...FF 00 data``）处理响应 skey，
    这里用公钥做 ``pow(c, e, n)`` 即可还原；这不是解密，只是公钥运算。
    """

    if not isinstance(signature, (bytes, bytearray)):
        raise ValueError("signature 必须是 bytes")
    modulus, exponent = public_key
    size = _modulus_size_bytes(public_key)
    if len(signature) != size:
        raise ValueError("RSA 密文长度与模数不匹配")
    encoded = pow(int.from_bytes(bytes(signature), "big"), exponent, modulus).to_bytes(size, "big")
    if encoded[0] != 0x00 or encoded[1] != 0x01:
        raise ValueError("RSA type-1 padding 无效")
    offset = 2
    while offset < len(encoded) and encoded[offset] == 0xFF:
        offset += 1
    # PKCS#1 v1.5 type-1 padding 要求至少 8 个 0xFF，随后是 0x00 分隔符。
    if offset - 2 < 8 or offset >= len(encoded) or encoded[offset] != 0x00:
        raise ValueError("RSA type-1 padding 无效")
    return encoded[offset + 1 :]


# ---------------------------------------------------------------------------
# 请求构造与响应解密
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SearchRequest:
    """一次加密搜索请求的完整内容（``url`` 已带签名 query）。"""

    q: str
    skey: str
    url: str
    query_params: dict[str, str]
    payload: dict[str, str]


def build_search_payload(keyword: str, mid: str, m2: str) -> dict[str, str]:
    """构造 ``q`` 里的搜索 JSON（字段与顺序取自 360 网页客户端实测请求）。"""

    return {
        "kw": keyword,
        "mid": mid,
        "m2": m2,
        "os_bit": OS_BIT,
        "os_ver": OS_VERSION,
        "machine": "",
        "src": _SEARCH_SOURCE,
        "ext_param": _SEARCH_EXT_PARAM,
        "source_id": "",
        "language": _SEARCH_LANGUAGE,
        "country_code": _SEARCH_COUNTRY,
    }


def encrypt_query(payload: Mapping[str, Any]) -> str:
    """``q``：搜索 JSON 的 UTF-8 字节经固定 key/iv 的 AES-128-CBC 加密后 base64。"""

    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(aes_cbc_encrypt(AES_KEY, AES_IV, serialized)).decode("ascii")


def encrypt_skey(
    public_key: tuple[int, int] = RSA_PUBLIC_KEY,
    *,
    random_source: Any = None,
) -> str:
    """``skey``：``{"key","iv"}`` 的 base64 经 RSA PKCS#1 v1.5 加密后再 base64。"""

    document = json.dumps(
        {
            "key": base64.b64encode(AES_KEY).decode("ascii"),
            "iv": base64.b64encode(AES_IV).decode("ascii"),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    encrypted = rsa_pkcs1_v15_encrypt(document, public_key, random_source=random_source)
    return base64.b64encode(encrypted).decode("ascii")


def build_base_params(*, mid: str, m2: str, timestamp: int, rand: int) -> dict[str, str]:
    """签名与 query 共用的基础参数（含空值 ``sdk_ver``，签名时过滤、query 时忽略）。"""

    return {
        "ver": CLIENT_VERSION,
        "os_ver": OS_VERSION,
        "os_bit": OS_BIT,
        "sdk_ver": "",
        "mid": mid,
        "m2": m2,
        "timestamp": str(timestamp),
        "sk_type": SK_TYPE,
        "sk_bit": SK_BIT,
        "rand": str(rand),
    }


def compute_signature(base_params: Mapping[str, str], skey: str, q: str) -> str:
    """``sign``：排序、过滤空值的基础参数 + 原始 ``skey`` + ``q`` + salt 的 MD5（小写）。"""

    joined = "&".join(
        f"{key}={base_params[key]}"
        for key in sorted(base_params)
        if base_params[key]
    )
    return hashlib.md5(f"{joined}{skey}{q}{SIGN_SALT}".encode("utf-8")).hexdigest()


def build_query_string(params: Mapping[str, str]) -> str:
    """把参数按插入顺序拼成 URL query（值做百分号编码，空值跳过）。"""

    return "&".join(
        f"{key}={quote(str(value), safe='')}" for key, value in params.items() if str(value)
    )


def build_search_request(
    keyword: str,
    *,
    mid: str,
    m2: str,
    timestamp: int,
    rand: int,
    base_url: str = SEARCH_BASE_URL,
    public_key: tuple[int, int] = RSA_PUBLIC_KEY,
    random_source: Any = None,
) -> SearchRequest:
    """构造一次完整的加密搜索请求（供客户端与离线测试使用）。"""

    if not isinstance(keyword, str) or not keyword:
        raise ValueError("keyword 必须是非空字符串")
    if not isinstance(mid, str) or not mid:
        raise ValueError("mid 必须是非空字符串")
    if not isinstance(m2, str) or not m2:
        raise ValueError("m2 必须是非空字符串")
    if type(timestamp) is not int or timestamp <= 0:
        raise ValueError("timestamp 必须是正整数")
    if type(rand) is not int or not 1000 <= rand <= 9999:
        raise ValueError("rand 必须是 1000–9999 的整数")
    payload = build_search_payload(keyword, mid, m2)
    q = encrypt_query(payload)
    skey = encrypt_skey(public_key, random_source=random_source)
    base_params = build_base_params(mid=mid, m2=m2, timestamp=timestamp, rand=rand)
    signature = compute_signature(base_params, skey, q)
    query_params = {**base_params, "sign": signature}
    url = f"{base_url.rstrip('/')}{SEARCH_PATH}?{build_query_string(query_params)}"
    return SearchRequest(q=q, skey=skey, url=url, query_params=query_params, payload=payload)


def _base64_decode(value: Any, what: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{what} 不是有效的 base64")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError(f"{what} 不是有效的 base64") from None


def decrypt_search_response(
    skey_b64: str,
    data_b64: str,
    public_key: tuple[int, int] = RSA_PUBLIC_KEY,
) -> Any:
    """解密响应：``skey`` → RSA 公钥运算 → ``{key,iv}`` → AES-CBC 解 ``data``。"""

    skey = _base64_decode(skey_b64, "响应 skey")
    encrypted = _base64_decode(data_b64, "响应 data")
    plaintext_key = rsa_pkcs1_v15_public_decrypt(skey, public_key)
    try:
        key_document = json.loads(plaintext_key.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("响应 skey 明文不是有效的 JSON") from None
    if not isinstance(key_document, Mapping):
        raise ValueError("响应 skey 明文结构无效")
    key = _base64_decode(key_document.get("key"), "响应密钥 key")
    iv = _base64_decode(key_document.get("iv"), "响应密钥 iv")
    if len(key) != _AES_BLOCK_SIZE or len(iv) != _AES_BLOCK_SIZE:
        raise ValueError("响应密钥长度无效（需要 AES-128）")
    plaintext = aes_cbc_decrypt(key, iv, encrypted)
    try:
        return json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("响应 data 明文不是有效的 JSON") from None


def random_token(random_source: Any, length: int) -> str:
    """生成 ``mid`` / ``m2`` 这类随机字母数字串。"""

    return "".join(random_source.choice(RANDOM_TOKEN_ALPHABET) for _ in range(length))


# ---------------------------------------------------------------------------
# 异步客户端
# ---------------------------------------------------------------------------


class Soft360SearchClient:
    """360 加密搜索接口的异步客户端（只负责传输、加解密与错误映射）。

    复用 ``Soft360StoreService`` 的 ``httpx2.AsyncClient``（同一连接池、浏览器 UA、
    ``soft360.timeout_seconds``）。测试可注入 ``httpx2.MockTransport``；``public_key`` /
    ``clock`` / ``random_source`` 是离线测试用的注入点，生产代码保持默认值。
    """

    def __init__(
        self,
        client: httpx2.AsyncClient,
        *,
        base_url: str = SEARCH_BASE_URL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        public_key: tuple[int, int] = RSA_PUBLIC_KEY,
        clock: Any = time.time,
        random_source: Any = None,
    ) -> None:
        if not isinstance(client, httpx2.AsyncClient):
            raise ValueError("client must be an httpx2.AsyncClient")
        for name, value in (
            ("timeout_seconds", timeout_seconds),
            ("connect_timeout_seconds", connect_timeout_seconds),
        ):
            if type(value) not in (int, float) or not math.isfinite(float(value)) or value <= 0:
                raise ValueError(f"{name} must be a finite positive value")
        if float(connect_timeout_seconds) > float(timeout_seconds):
            raise ValueError("connect_timeout_seconds cannot exceed timeout_seconds")
        if (
            not isinstance(base_url, str)
            or not base_url.strip()
            or not base_url.strip().casefold().startswith(("http://", "https://"))
        ):
            raise ValueError("base_url must be an http(s) URL")
        if (
            not isinstance(public_key, tuple)
            or len(public_key) != 2
            or type(public_key[0]) is not int
            or type(public_key[1]) is not int
            or public_key[0] <= 0
            or public_key[1] <= 0
        ):
            raise ValueError("public_key must be an (n, e) integer tuple")
        if not callable(clock):
            raise ValueError("clock must be callable")
        self._client = client
        self._base_url = base_url.strip().rstrip("/")
        self._timeout_seconds = float(timeout_seconds)
        self._connect_timeout_seconds = float(connect_timeout_seconds)
        self._public_key = public_key
        self._clock = clock
        self._random = random_source if random_source is not None else random.SystemRandom()

    async def search(self, keyword: str) -> list[dict[str, Any]]:
        """搜索关键词，返回 360 原始 ``list`` 条目（最多 50 条，字段未规范化）。"""

        if not isinstance(keyword, str) or not keyword.strip():
            raise ValueError("keyword 必须是非空字符串")
        request = build_search_request(
            keyword.strip(),
            mid=random_token(self._random, MID_LENGTH),
            m2=random_token(self._random, M2_LENGTH),
            timestamp=int(self._clock()),
            rand=self._random.randint(1000, 9999),
            base_url=self._base_url,
            public_key=self._public_key,
            random_source=self._random,
        )
        try:
            response = await self._client.post(
                request.url,
                json={"q": request.q, "skey": request.skey},
                headers=_REQUEST_HEADERS,
                timeout=self._timeout(),
            )
            response.raise_for_status()
        except httpx2.TimeoutException:
            raise Soft360TimeoutError("360 搜索请求超时。") from None
        except httpx2.HTTPStatusError as error:
            raise Soft360UnavailableError(
                f"360 搜索请求失败（HTTP {error.response.status_code}）。"
            ) from None
        except httpx2.HTTPError:
            raise Soft360UnavailableError("360 搜索服务暂时无法连接。") from None

        try:
            document = response.json()
        except (ValueError, UnicodeError):
            raise Soft360ProtocolError("360 搜索响应不是有效的 JSON。") from None
        if not isinstance(document, Mapping):
            raise Soft360ProtocolError("360 搜索响应结构无效。")
        errno = _coerce_int(document.get("errno"))
        if errno is None:
            raise Soft360ProtocolError("360 搜索响应缺少有效的 errno。")
        if errno != 0:
            message = _first_text(document, "errmsg", "message", "msg", "error")
            raise Soft360UnavailableError(f"360 搜索失败：{message or f'errno={errno}'}。")

        skey = document.get("skey")
        data = document.get("data")
        if not skey and not data:
            # 没有结果时 360 可能返回空的 data/skey（errno 仍是 0）。
            return []
        if not isinstance(skey, str) or not isinstance(data, str):
            raise Soft360ProtocolError("360 搜索响应缺少加密的 skey/data。")
        try:
            decrypted = decrypt_search_response(skey, data, self._public_key)
        except ValueError:
            raise Soft360ProtocolError("360 搜索响应无法解密。") from None
        if not isinstance(decrypted, Mapping):
            raise Soft360ProtocolError("360 搜索响应明文结构无效。")
        items = decrypted.get("list")
        if items is None:
            return []
        if not isinstance(items, list):
            raise Soft360ProtocolError("360 搜索结果列表结构无效。")
        results: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, Mapping):
                raise Soft360ProtocolError("360 搜索结果条目结构无效。")
            results.append(dict(item))
        return results

    def _timeout(self) -> httpx2.Timeout:
        return httpx2.Timeout(self._timeout_seconds, connect=self._connect_timeout_seconds)


def _coerce_int(value: Any) -> int | None:
    if type(value) is int:
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _first_text(document: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = document.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


__all__ = [
    "AES128",
    "AES_IV",
    "AES_KEY",
    "BASE_PARAM_KEYS",
    "CLIENT_VERSION",
    "M2_LENGTH",
    "MID_LENGTH",
    "RSA_PUBLIC_KEY",
    "RSA_PUBLIC_KEY_SPKI_B64",
    "SEARCH_BASE_URL",
    "SEARCH_PATH",
    "SIGN_SALT",
    "SearchRequest",
    "Soft360SearchClient",
    "aes_cbc_decrypt",
    "aes_cbc_encrypt",
    "build_base_params",
    "build_query_string",
    "build_search_payload",
    "build_search_request",
    "compute_signature",
    "decrypt_search_response",
    "encrypt_query",
    "encrypt_skey",
    "parse_rsa_public_key",
    "random_token",
    "rsa_pkcs1_v15_encrypt",
    "rsa_pkcs1_v15_public_decrypt",
]
