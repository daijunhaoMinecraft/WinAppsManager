import asyncio
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import httpx2
from pydantic import HttpUrl

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR.parent))

from Backend.models import Release
from Backend.providers.base import ProviderProtocolError, ProviderReleaseChangedError, ProviderTimeoutError, ProviderUnavailableError
from Backend.providers.sevenzip import DOWNLOAD_PAGE_URL, SevenZipReleaseProvider


def download_page(version="26.06", architectures=("x64", "x86", "arm64")):
    compact = version.replace(".", "")
    rows = []
    for architecture in architectures:
        suffix = "" if architecture == "x86" else f"-{architecture}"
        file_name = f"7z{compact}{suffix}.exe"
        system = "32-bit Windows x86" if architecture == "x86" else f"64-bit Windows {architecture}"
        rows.append(
            f'<tr><td class="Item" align="center"><a href="https://github.com/ip7z/7zip/releases/download/{version}/{file_name}">Download</a></td>'
            f'<td class="Item" align="center">.exe</td><td class="Item" align="left">{system}</td>'
            '<td class="Item" rowspan="3">7-Zip for Windows</td></tr>'
        )
    return f'''<html><body><table class="layout"><tr><td>
      <p><b>Download 7-Zip {version} (2026-11-06) for Windows</b>:</p>
      <table cellspacing="1" cellpadding="6"><tr><th>Link</th><th>Type</th><th>System</th><th>Description</th></tr>
      {"".join(rows)}
      <tr><td><a href="a/7z{compact}-x64.msi">Download</a></td><td>.msi</td></tr>
      <tr><td><a href="a/7z{compact}-extra.7z">Portable tools</a></td><td>.7z</td></tr>
      <tr><td><a href="a/7zr.exe">Console</a></td><td>.exe</td></tr></table>
      <p><b>Download 7-Zip 23.01 (2023-06-20) for Windows</b>:</p>
      <a href="a/7z2301-x64.exe">Old x64</a><a href="a/7z2301.exe">Old x86</a>
      </td></tr></table></body></html>'''


class SevenZipProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.content = download_page()
        self.status = 200
        self.failure = None
        self.delay = 0
        self.requests = []
        self.client = httpx2.AsyncClient(transport=httpx2.MockTransport(self._handle))
        self.provider = SevenZipReleaseProvider(self.client)

    async def asyncTearDown(self):
        await self.client.aclose()

    async def _handle(self, request):
        self.requests.append(request)
        self.assertEqual(str(request.url), DOWNLOAD_PAGE_URL)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.failure:
            raise self.failure
        return httpx2.Response(self.status, text=self.content, request=request)

    async def test_chrome_captured_nested_table_has_three_current_exe_installers(self):
        releases = await self.provider.get_releases()
        self.assertEqual(self.provider.id, "7zip")
        self.assertEqual([item.architecture for item in releases], ["x64", "x86", "arm64"])
        self.assertTrue(all(item.version == "26.06" for item in releases))
        self.assertTrue(all(item.platform == "windows" for item in releases))
        self.assertEqual(releases[0].file_name, "7z2606-x64.exe")
        self.assertEqual(releases[1].file_name, "7z2606.exe")
        self.assertEqual(releases[2].file_name, "7z2606-arm64.exe")
        self.assertTrue(all(item.size_bytes is None and item.sha256 is None for item in releases))

    async def test_cache_and_concurrent_requests_share_one_fetch(self):
        self.delay = 0.01
        batches = await asyncio.gather(*(self.provider.get_releases() for _ in range(6)))
        self.assertEqual(len(self.requests), 1)
        batches[0][0].file_name = "changed.exe"
        self.assertEqual((await self.provider.get_releases())[0].file_name, "7z2606-x64.exe")

    async def test_force_refresh_reads_new_stable_version(self):
        await self.provider.get_releases()
        self.content = download_page("26.07")
        self.assertEqual((await self.provider.get_releases())[0].version, "26.06")
        current = await self.provider.get_releases(force_refresh=True)
        self.assertEqual(current[0].version, "26.07")
        self.assertEqual(len(self.requests), 2)

    async def test_expired_metadata_refreshes(self):
        with patch("Backend.providers._utility.monotonic", return_value=0):
            await self.provider.get_releases()
        self.content = download_page("26.07")
        with patch("Backend.providers._utility.monotonic", return_value=61):
            self.assertEqual((await self.provider.get_releases())[0].version, "26.07")

    async def test_missing_current_architecture_does_not_fall_back_to_history(self):
        self.content = download_page(architectures=("x64",))
        releases = await self.provider.get_releases()
        self.assertEqual([item.architecture for item in releases], ["x64"])

    async def test_newer_beta_is_not_the_current_stable_version(self):
        self.content = '<p><b>Download 7-Zip 27.00 beta for Windows</b></p><a href="a/7z2700-x64.exe">Beta</a>' + self.content
        self.assertEqual((await self.provider.get_releases())[0].version, "26.06")

    async def test_official_relative_download_addresses_are_supported(self):
        self.content = self.content.replace("https://github.com/ip7z/7zip/releases/download/26.06/", "a/")
        releases = await self.provider.get_releases()
        self.assertEqual(str(releases[0].download_url), "https://www.7-zip.org/a/7z2606-x64.exe")

    async def test_page_layout_can_change_without_selecting_outer_or_historical_tables(self):
        self.content = self.content.replace('<p><b>Download', '<h2><b>Download').replace('for Windows</b>:</p>', 'for Windows</b>:</h2>')
        self.assertEqual(len(await self.provider.get_releases()), 3)

    async def test_missing_version_and_missing_current_installer_are_protocol_errors(self):
        for content in ('<html>Service error</html>', '<p>Download 7-Zip 27.01 for Windows</p><a href="a/7z2606.exe">History</a>'):
            with self.subTest(content=content):
                self.content = content
                with self.assertRaises(ProviderProtocolError):
                    await self.provider.get_releases(force_refresh=True)

    async def test_official_host_repo_and_protocol_are_validated(self):
        for replacement in (
            "https://evil.example/ip7z/7zip/releases/download/26.06/",
            "http://github.com/ip7z/7zip/releases/download/26.06/",
            "https://github.com/other/repo/releases/download/26.06/",
            "https://user:password@github.com/ip7z/7zip/releases/download/26.06/",
        ):
            self.content = download_page().replace("https://github.com/ip7z/7zip/releases/download/26.06/", replacement)
            with self.subTest(replacement=replacement), self.assertRaises(ProviderProtocolError):
                await self.provider.get_releases(force_refresh=True)

    async def test_resolve_accepts_only_authoritative_metadata(self):
        release = (await self.provider.get_releases())[0]
        resolved = await self.provider.resolve_download(release)
        self.assertEqual(resolved, release)
        self.assertIsNot(resolved, release)
        with self.assertRaises(ProviderProtocolError):
            await self.provider.resolve_download(release.model_copy(update={"download_url": HttpUrl("https://evil.example/file.exe")}))
        with self.assertRaises(ProviderProtocolError):
            await self.provider.resolve_download(object())

    async def test_metadata_version_change_raises_refreshable_error(self):
        release = (await self.provider.get_releases())[0]
        self.content = download_page("26.07")
        await self.provider.get_releases(force_refresh=True)
        with self.assertRaises(ProviderReleaseChangedError):
            await self.provider.resolve_download(release)

    async def test_http_errors_and_transports_have_visible_provider_errors(self):
        self.status = 503
        with self.assertRaises(ProviderUnavailableError):
            await self.provider.get_releases()
        self.status = 200
        self.failure = httpx2.ConnectError("transport detail")
        with self.assertRaises(ProviderUnavailableError):
            await self.provider.get_releases()

    async def test_timeouts_are_distinct(self):
        self.failure = httpx2.ReadTimeout("timeout detail")
        with self.assertRaises(ProviderTimeoutError):
            await self.provider.get_releases()

    async def test_failed_metadata_is_not_cached(self):
        self.content = "not a download page"
        with self.assertRaises(ProviderProtocolError):
            await self.provider.get_releases()
        self.content = download_page()
        self.assertEqual(len(await self.provider.get_releases()), 3)
        self.assertEqual(len(self.requests), 2)

    async def test_cache_duration_is_validated(self):
        for value in (-1, float("inf"), float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                SevenZipReleaseProvider(self.client, cache_ttl=value)


if __name__ == "__main__":
    unittest.main()
