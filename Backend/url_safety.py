"""公网 HTTP(S) URL 安全校验（联想 / 360 图片直连与后端图片代理共用）。

背景：早期联想/360 图片地址使用**固定 CDN 域名白名单**校验，遇到名单外的真实 CDN（例如
360 详情截图使用的 ``p15.qhmsg.com``）就会 422/502。现在不再维护域名白名单：只要满足
“安全的公网 http(s) 地址”就允许直连或由后端代理，从而兼容任意真实 CDN 子域，同时保留防
SSRF 的底线：

- 只接受 ``http`` / ``https``（调用方可要求 https），必须有 host，端口合法；
- 拒绝 userinfo（``user:pass@host``）、控制字符、空白和反斜杠；
- 拒绝 ``localhost``、单标签主机名（``http://intranet/...`` 这类内网常见写法）与内网后缀
  （``.local`` / ``.internal`` / ``.lan`` / ``.home.arpa`` / ``.localdomain`` 等）；
- 拒绝回环/私网/链路本地/保留/组播 IP 字面量（IPv4、IPv6 与 IPv4-mapped IPv6），也拒绝
  ``127.1`` / ``2130706433`` / ``0x7f.0.0.1`` 这类伪装成主机名的数字 IP 写法；
- 不做 DNS 解析：公网域名解析到内网 IP（DNS rebinding）不在本函数防护范围内。后端只监听
  ``127.0.0.1``，图片代理还会逐跳校验重定向目标，风险面有限；需要更强防护时应在代理层
  解析 DNS 后再校验。

图片代理（``lenovo/media.py``、``soft360/media.py``）在请求前以及**每次重定向**前都调用
同一个 :func:`validate_public_http_url`，所以 30x 跳转不会把请求带到内网/回环地址。
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any
from urllib.parse import urljoin, urlsplit

# 图片代理 URL 可能带较长的签名查询串；模型里的直连地址另有 1024 的长度上限。
MAX_PUBLIC_URL_LENGTH = 4096
# 主机名最长 253 个字符（不含末尾点）。
_MAX_HOST_LENGTH = 253
# ``2130706433`` / ``0x7f.0.0.1`` / ``0177.0.0.1`` 等“数字 IP”写法：操作系统/部分 HTTP 栈
# 会把它解析成对应 IP，因此即使 ipaddress 不认识也一律拒绝。
_NUMERIC_IP_PATTERN = re.compile(
    r"^(?:0x[0-9a-f]+|\d+)(?:\.(?:0x[0-9a-f]+|\d+)){0,3}$", re.IGNORECASE
)
# 这些后缀只用于内网/本机/保留用途，公网图片不应出现。
_INTERNAL_HOST_SUFFIXES: tuple[str, ...] = (
    ".localhost",
    ".local",
    ".localdomain",
    ".internal",
    ".intranet",
    ".lan",
    ".corp",
    ".home",
    ".home.arpa",
    ".private",
    ".onion",
    ".test",
    ".invalid",
    ".example",
)


def is_public_host(host: str) -> bool:
    """判断主机名是否是“可对外访问的公网主机”。

    IP 字面量按 :mod:`ipaddress` 的全局地址规则判断（回环/私网/链路本地/保留/组播都不算），
    域名则拒绝单标签主机名与 :data:`_INTERNAL_HOST_SUFFIXES` 里的内网后缀。
    """

    if not isinstance(host, str):
        return False
    normalized = host.strip().casefold().rstrip(".")
    if not normalized or len(normalized) > _MAX_HOST_LENGTH:
        return False
    # 百分号编码的主机名容易被不同解析器区别对待，直接拒绝。
    if "%" in normalized:
        return False

    address = _parse_ip_literal(normalized)
    if address is not None:
        return _address_is_public(address)
    if _NUMERIC_IP_PATTERN.fullmatch(normalized):
        return False
    # 单标签主机名（``intranet``、``localhost``、``fileserver``）通常是内网机器名。
    if "." not in normalized:
        return False
    return not any(normalized.endswith(suffix) for suffix in _INTERNAL_HOST_SUFFIXES)


def validate_public_http_url(
    value: Any,
    *,
    max_length: int = MAX_PUBLIC_URL_LENGTH,
    require_https: bool = False,
) -> str | None:
    """校验并返回安全的公网 http(s) URL；不安全/不合法时返回 ``None``。

    ``require_https=True`` 时只接受 https（例如未来需要强制安全传输的调用方）。返回的是
    ``strip()`` 后的原始地址，不做 http→https 升级：上游给什么协议就用什么协议，避免把
    只支持 http 的真实 CDN 改坏。
    """

    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > max_length:
        return None
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in text):
        return None
    if any(character.isspace() for character in text) or "\\" in text:
        return None
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.casefold()
    if scheme not in ("http", "https"):
        return None
    if require_https and scheme != "https":
        return None
    if not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    if port == 0:
        return None
    if not is_public_host(parsed.hostname):
        return None
    return text


def resolve_redirect_url(base_url: str, location: Any) -> str | None:
    """把重定向 ``Location`` 解析成绝对地址并做同一套安全校验；不安全时返回 ``None``。

    ``base_url`` 是当前请求地址（也必须是已通过 :func:`validate_public_http_url` 的安全地址），
    相对 ``Location`` 按 RFC 3986 用 :func:`urllib.parse.urljoin` 解析。
    """

    if not isinstance(location, str):
        return None
    target = location.strip()
    if not target:
        return None
    try:
        absolute = urljoin(base_url, target)
    except ValueError:
        return None
    return validate_public_http_url(absolute)


def _parse_ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """``host`` 是 IP 字面量时返回地址对象，否则返回 ``None``。"""

    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _address_is_public(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    """IP 地址只允许全局可路由地址（回环/私网/链路本地/保留/组播一律拒绝）。"""

    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            # ``::ffff:127.0.0.1`` 这类 IPv4-mapped 地址按内嵌的 IPv4 判断。
            address = address.ipv4_mapped
        elif address.is_site_local:
            return False
    if (
        address.is_unspecified
        or address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
    ):
        return False
    return bool(address.is_global)


__all__ = [
    "MAX_PUBLIC_URL_LENGTH",
    "is_public_host",
    "resolve_redirect_url",
    "validate_public_http_url",
]
