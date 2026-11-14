import asyncio
from pathlib import Path
import sys
import unittest

import httpx2
from pydantic import HttpUrl

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from Backend.providers.base import ProviderProtocolError, ProviderReleaseChangedError, ProviderTimeoutError, ProviderUnavailableError
from Backend.providers.wechat import DOWNLOAD_PAGE, WeChatReleaseProvider


def page(version="4.2.0", compatibility="3.9.12"):
    return f'''<html><body><div id="newVersion"><div class="download">
    <a class="download-button" id="downloadButton" href="https://dldir1v6.qq.com/weixin/Universal/Windows/WeChatWin_{version}.exe">
      <div class="download-text">下载 {version} (64位)</div><!-- stale download-version 3.9.12 -->
      <div>适用于 Windows7 及以上版本</div></a></div></div>
    <div id="oldVersion" style="display:none"><a class="download-button" href="https://dldir1v6.qq.com/weixin/Windows/WeChatSetup_x86.exe">
      <div>下载 {compatibility}</div></a></div></body></html>'''


class WeChatProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = 0
        self.html = page()

        def handler(request):
            self.calls += 1
            self.assertEqual(str(request.url).split("?", 1)[0], DOWNLOAD_PAGE)
            self.assertEqual(request.url.params.get("lang"), "zh_CN")
            return httpx2.Response(200, text=self.html)

        self.client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
        self.provider = WeChatReleaseProvider(self.client)

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_current_64bit_and_official_32bit_keep_separate_versions(self):
        releases = await self.provider.get_releases()
        self.assertEqual([(item.architecture, item.version) for item in releases], [("x64", "4.2.0"), ("x86", "3.9.12")])
        self.assertEqual(releases[0].file_name, "WeChatWin_4.2.0.exe")
        self.assertEqual(releases[1].file_name, "WeChatSetup_x86.exe")
        self.assertIsNone(releases[0].sha256)
        self.assertIsNone(releases[0].size_bytes)

    async def test_cache_copy_and_forced_version_refresh(self):
        old = (await self.provider.get_releases())[0]
        old.file_name = "tampered.exe"
        self.assertEqual((await self.provider.get_releases())[0].file_name, "WeChatWin_4.2.0.exe")
        self.assertEqual(self.calls, 1)
        self.html = page("4.2.1")
        current = (await self.provider.get_releases(force_refresh=True))[0]
        self.assertEqual(current.version, "4.2.1")
        self.assertEqual(self.calls, 2)

    async def test_concurrent_requests_share_one_fetch(self):
        await asyncio.gather(*(self.provider.get_releases() for _ in range(5)))
        self.assertEqual(self.calls, 1)

    async def test_resolve_does_not_need_the_qq_signature_service(self):
        release = (await self.provider.get_releases())[0]
        resolved = await self.provider.resolve_download(release)
        self.assertEqual(resolved, release)
        self.assertEqual(self.calls, 1)

    async def test_tampered_metadata_is_rejected(self):
        release = (await self.provider.get_releases())[0]
        release.download_url = HttpUrl("https://dldir1v6.qq.com/other.exe")
        with self.assertRaises(ProviderProtocolError):
            await self.provider.resolve_download(release)

    async def test_version_change_is_reported_as_changed(self):
        old = (await self.provider.get_releases())[0]
        self.html = page("4.2.1")
        await self.provider.get_releases(force_refresh=True)
        with self.assertRaises(ProviderReleaseChangedError):
            await self.provider.resolve_download(old)

    async def test_arm64_is_not_invented_from_a_64bit_download(self):
        self.assertNotIn("arm64", [release.architecture for release in await self.provider.get_releases()])

    async def test_explicit_new_arm64_download_is_supported(self):
        self.html = page().replace("</body>", '''<div id="newVersion"><a class="download-button" href="https://dldir1v6.qq.com/weixin/Universal/Windows/WeChatWin_4.2.0_arm64.exe">下载 4.2.0 (ARM64)</a></div></body>''')
        self.assertIn("arm64", [release.architecture for release in await self.provider.get_releases()])

    async def test_comments_and_unrelated_download_links_are_ignored(self):
        self.html = page().replace("</body>", '<a href="https://evil.invalid/tool.exe" class="download-button">99.0.0</a></body>')
        self.assertEqual((await self.provider.get_releases())[0].version, "4.2.0")

    async def test_missing_version_or_architecture_is_rejected(self):
        for html in (page().replace("下载 4.2.0 (64位)", "下载 (64位)"), page().replace("下载 4.2.0 (64位)", "下载 4.2.0")):
            with self.subTest(html=html):
                with self.assertRaises(ProviderProtocolError):
                    WeChatReleaseProvider._parse_page(html)

    async def test_invalid_addresses_and_file_names_are_rejected(self):
        for url in ("http://dldir1v6.qq.com/setup.exe", "https://evil.invalid/setup.exe", "https://dldir1v6.qq.com/file.zip",
                    "https://dldir1v6.qq.com/%2fsetup.exe", "https://user@dldir1v6.qq.com/setup.exe", "https://dldir1v6.qq.com/NUL.exe"):
            with self.subTest(url=url):
                with self.assertRaises(ProviderProtocolError):
                    WeChatReleaseProvider._parse_page(page().replace("https://dldir1v6.qq.com/weixin/Universal/Windows/WeChatWin_4.2.0.exe", url))

    async def test_duplicate_architecture_and_empty_pages_are_rejected(self):
        for html in (page().replace("</body>", '<div id="newVersion"><a class="download-button" href="https://dldir1v6.qq.com/WeChatWin_4.2.0.exe">下载 4.2.0 (64位)</a></div></body>'), '<html>server error</html>'):
            with self.assertRaises(ProviderProtocolError):
                WeChatReleaseProvider._parse_page(html)

    async def test_http_timeout_and_transport_errors_are_typed(self):
        cases = ((503, None, ProviderUnavailableError), (200, httpx2.ConnectError("sensitive"), ProviderUnavailableError),
                 (200, httpx2.ReadTimeout("sensitive"), ProviderTimeoutError))
        for status, error, expected in cases:
            def handler(request):
                if error is not None:
                    raise error
                return httpx2.Response(status, text="error")
            async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
                with self.assertRaises(expected) as caught:
                    await WeChatReleaseProvider(client).get_releases()
                self.assertNotIn("sensitive", str(caught.exception))

    async def test_failed_response_is_not_cached(self):
        self.html = "error"
        with self.assertRaises(ProviderProtocolError):
            await self.provider.get_releases()
        self.html = page()
        self.assertEqual((await self.provider.get_releases())[0].version, "4.2.0")
        self.assertEqual(self.calls, 2)


if __name__ == "__main__":
    unittest.main()
