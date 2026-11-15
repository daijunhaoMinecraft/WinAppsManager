"""``Backend/url_safety.py`` 的单元测试：公网图片 URL 的安全策略（不联网）。

覆盖：
- 任意安全的公网 http(s) 地址都允许（不再有 CDN 域名白名单：360 的 p15.qhmsg.com、
  未知厂商 CDN 子域、http/https 都可）；
- 拒绝 userinfo、localhost、单标签主机名、内网后缀、私有/回环/链路本地/保留 IP 字面量、
  数字 IP 伪装主机名、控制字符/空白/反斜杠、非 http(s) 协议、超长地址；
- 重定向目标解析使用同一套校验（相对 Location 与跨域跳转）。
"""

from pathlib import Path
import sys
import unittest

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR.parent))

from Backend.url_safety import (  # noqa: E402
    is_public_host,
    resolve_redirect_url,
    validate_public_http_url,
)


class PublicUrlAllowTests(unittest.TestCase):
    def test_arbitrary_public_image_domains_are_allowed(self):
        allowed = [
            "http://p15.qhmsg.com/t01ef3b2bb4170fa1f0.png",
            "https://p15.qhmsg.com/t01ef3b2bb4170fa1f0.png",
            "https://p10.qhmsg.com/t01e33f3a65e1d9118a.png",
            "https://p0.qhimg.com/shot1.png",
            "https://s1.mediav.com/a.jpg",
            "https://img.qhres2.com/a.png",
            "https://unknown-cdn.vendor-name.example.net/path/a.webp?x=1",
            "https://lsw-fast.lenovo.com.cn/appstore/normal/apps/a.png",
            "http://imgs.lenovomm.cn/a.jpg",
            "https://cdn.example.com:8443/a.png",
            "https://xn--fiqs8s.example.cn/a.png",
            "https://example.com./a.png",
        ]
        for url in allowed:
            with self.subTest(url=url):
                self.assertEqual(validate_public_http_url(url), url)

    def test_scheme_can_be_restricted_to_https(self):
        self.assertEqual(
            validate_public_http_url("https://p15.qhmsg.com/a.png", require_https=True),
            "https://p15.qhmsg.com/a.png",
        )
        self.assertIsNone(
            validate_public_http_url("http://p15.qhmsg.com/a.png", require_https=True)
        )

    def test_length_limit_is_enforced(self):
        self.assertEqual(validate_public_http_url("https://a.example.net/" + "x" * 20, max_length=64), "https://a.example.net/" + "x" * 20)
        self.assertIsNone(validate_public_http_url("https://a.example.net/" + "x" * 80, max_length=64))


class PublicUrlRejectTests(unittest.TestCase):
    def test_internal_and_unsafe_addresses_are_rejected(self):
        rejected = [
            # 回环 / 私网 / 链路本地 / 保留 IP 字面量
            "http://127.0.0.1/a.png",
            "http://127.0.0.1:8080/a.png",
            "https://[::1]/a.png",
            "http://10.1.2.3/a.png",
            "http://172.16.0.9/a.png",
            "http://192.168.1.20/a.png",
            "http://169.254.1.1/a.png",
            "http://0.0.0.0/a.png",
            "http://100.64.0.1/a.png",
            "http://240.0.0.1/a.png",
            "https://[fe80::1]/a.png",
            "https://[::ffff:127.0.0.1]/a.png",
            # 伪装成主机名的数字 IP
            "http://2130706433/a.png",
            "http://127.1/a.png",
            "http://0x7f.0.0.1/a.png",
            "http://0177.0.0.1/a.png",
            # 本机 / 单标签 / 内网后缀
            "http://localhost/a.png",
            "http://localhost:8000/a.png",
            "http://foo.localhost/a.png",
            "http://intranet/a.png",
            "https://printer.local/a.png",
            "https://nas.home.arpa/a.png",
            "https://files.internal/a.png",
            "https://db.localdomain/a.png",
            "https://x.corp/a.png",
            # userinfo / 控制字符 / 空白 / 反斜杠
            "https://user:pass@p15.qhmsg.com/a.png",
            "https://user@p15.qhmsg.com/a.png",
            "https://p15.qhmsg.com/a b.png",
            "https://p15.qhmsg.com/\ta.png",
            "https://p15.qhmsg.com/\na.png",
            "https://p15.qhmsg.com/a\\b.png",
            "https://p15.qhmsg.com/a\x00.png",
            # 非 http(s) / 不完整
            "ftp://p15.qhmsg.com/a.png",
            "file:///C:/Windows/a.png",
            "javascript:alert(1)",
            "//p15.qhmsg.com/a.png",
            "/api/v1/store/360/apps/1010/screenshots/0",
            "not a url",
            "",
            "   ",
            None,
            123,
        ]
        for url in rejected:
            with self.subTest(url=url):
                self.assertIsNone(validate_public_http_url(url))

    def test_is_public_host_handles_edge_cases(self):
        self.assertTrue(is_public_host("p15.qhmsg.com"))
        self.assertTrue(is_public_host("P15.QHMSG.COM"))
        self.assertTrue(is_public_host("example.com."))
        self.assertFalse(is_public_host("localhost"))
        self.assertFalse(is_public_host("printer.local"))
        self.assertFalse(is_public_host("127.0.0.1"))
        self.assertFalse(is_public_host(""))
        self.assertFalse(is_public_host("a" * 300 + ".com"))


class RedirectValidationTests(unittest.TestCase):
    def test_relative_and_cross_origin_redirects_are_resolved_safely(self):
        self.assertEqual(
            resolve_redirect_url("https://p0.qhimg.com/a.png", "/b.png"),
            "https://p0.qhimg.com/b.png",
        )
        self.assertEqual(
            resolve_redirect_url(
                "http://p15.qhmsg.com/a.png", "https://img.qhres2.com/b.png"
            ),
            "https://img.qhres2.com/b.png",
        )
        self.assertEqual(
            resolve_redirect_url("https://p0.qhimg.com/a.png", "//p10.qhmsg.com/b.png"),
            "https://p10.qhmsg.com/b.png",
        )

    def test_redirects_to_internal_addresses_are_rejected(self):
        for location in (
            "http://127.0.0.1/a.png",
            "http://localhost/a.png",
            "http://192.168.0.1/a.png",
            "https://printer.local/a.png",
            "ftp://p0.qhimg.com/a.png",
            "",
            None,
        ):
            with self.subTest(location=location):
                self.assertIsNone(
                    resolve_redirect_url("https://p0.qhimg.com/a.png", location)
                )


if __name__ == "__main__":
    unittest.main()
