import asyncio
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import httpx2
from pydantic import HttpUrl

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from Backend.providers.base import (
    ProviderProtocolError,
    ProviderReleaseChangedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from Backend.providers.vlc import PAGE_URL, VLCReleaseProvider


def platforms(version="3.0.24"):
    result = {}
    for key, directory in (("windows", "win32"), ("windows64", "win64"), ("windowsarm64", "winarm64")):
        result[key] = {
            "name": key, "latestVersion": version, "size": "44&nbsp;MB",
            "location": f"//get.videolan.org/vlc/{version}/{directory}/vlc-{version}-{directory}.exe",
        }
    result["macosx"] = {"latestVersion": "99.0.0", "location": "//example.test/mac.dmg"}
    return result


class VLCProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.platforms = platforms()
        self.requests = []
        self.raw = None
        self.status = 200
        self.exception = None
        self.delay = 0
        self.client = httpx2.AsyncClient(transport=httpx2.MockTransport(self.handle))
        self.provider = VLCReleaseProvider(self.client)

    async def asyncTearDown(self):
        await self.client.aclose()

    async def handle(self, request):
        self.requests.append(request)
        self.assertEqual(str(request.url), PAGE_URL)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exception is not None:
            raise self.exception
        html = self.raw if self.raw is not None else f"<html><script>var PLATFORMS = {json.dumps(self.platforms)};</script></html>"
        return httpx2.Response(self.status, text=html, request=request)

    async def test_captured_platforms_select_three_windows_installers(self):
        releases = await self.provider.get_releases()
        self.assertEqual(self.provider.id, "vlc")
        self.assertEqual([item.architecture for item in releases], ["x64", "x86", "arm64"])
        for item, directory in zip(releases, ["win64", "win32", "winarm64"]):
            self.assertEqual(item.version, "3.0.24")
            self.assertEqual(item.id, f"3.0.24-windows-{item.architecture}")
            self.assertEqual(item.file_name, f"vlc-3.0.24-{directory}.exe")
            self.assertEqual(str(item.download_url), f"https://get.videolan.org/vlc/3.0.24/{directory}/{item.file_name}")
            self.assertIsNone(item.size_bytes)
            self.assertIsNone(item.sha256)
            self.assertEqual(await self.provider.resolve_download(item), item)
        self.assertEqual(len(self.requests), 1)

    async def test_cached_metadata_is_copied_and_force_refresh_updates_version(self):
        first = await self.provider.get_releases()
        first[0].version = "0.0.0"
        self.assertEqual((await self.provider.get_releases())[0].version, "3.0.24")
        self.platforms = platforms("3.0.25")
        self.assertEqual((await self.provider.get_releases(force_refresh=True))[0].version, "3.0.25")
        self.assertEqual(len(self.requests), 2)

    async def test_concurrent_reads_share_one_fetch(self):
        self.delay = 0.02
        await asyncio.gather(*(self.provider.get_releases() for _ in range(5)))
        self.assertEqual(len(self.requests), 1)

    async def test_expiry_and_version_change_are_reported(self):
        with patch("Backend.providers._media.monotonic", return_value=0):
            old = (await self.provider.get_releases())[0]
        self.platforms = platforms("3.0.25")
        with patch("Backend.providers._media.monotonic", return_value=61):
            with self.assertRaises(ProviderReleaseChangedError):
                await self.provider.resolve_download(old)

    async def test_missing_architecture_is_not_filled_from_another_platform(self):
        del self.platforms["windowsarm64"]
        releases = await self.provider.get_releases()
        self.assertEqual([item.architecture for item in releases], ["x64", "x86"])

    async def test_older_architecture_does_not_claim_to_be_latest(self):
        self.platforms["windowsarm64"] = platforms("3.0.23")["windowsarm64"]
        self.assertEqual([item.architecture for item in await self.provider.get_releases()], ["x64", "x86"])

    async def test_tampered_metadata_is_rejected(self):
        release = (await self.provider.get_releases())[0]
        for mutation in ({"download_url": HttpUrl("https://example.test/unrelated.exe")},
                         {"file_name": "unrelated.exe"}, {"size_bytes": 12}):
            with self.subTest(mutation=mutation):
                with self.assertRaises(ProviderProtocolError):
                    await self.provider.resolve_download(release.model_copy(update=mutation))

    async def test_json_parser_does_not_execute_javascript(self):
        self.raw = "<script>var PLATFORMS = (function(){return {};})();</script>"
        with self.assertRaises(ProviderProtocolError):
            await self.provider.get_releases()

    async def test_missing_or_invalid_platforms_are_protocol_errors(self):
        for html in ("<html>unavailable</html>", "<script>var PLATFORMS = [];</script>",
                     "<script>var PLATFORMS = {oops};</script>"):
            self.raw = html
            with self.subTest(html=html):
                with self.assertRaises(ProviderProtocolError):
                    await self.provider.get_releases(force_refresh=True)

    async def test_invalid_version_and_installer_identity_are_rejected(self):
        base = platforms()
        mutations = [{"latestVersion": "3.0.24-beta"}, {"latestVersion": 3},
                     {"location": "//get.videolan.org/vlc/3.0.23/win64/vlc-3.0.23-win64.exe"},
                     {"location": "//get.videolan.org/vlc/3.0.24/win32/vlc-3.0.24-win32.exe"},
                     {"location": "//example.test/vlc-3.0.24-win64.exe"},
                     {"location": "http://get.videolan.org/vlc/3.0.24/win64/vlc-3.0.24-win64.exe"},
                     {"location": "//get.videolan.org/vlc/3.0.24/win64/vlc-3.0.24-win64.zip"}]
        for mutation in mutations:
            self.platforms = copy.deepcopy(base)
            self.platforms["windows64"].update(mutation)
            with self.subTest(mutation=mutation):
                with self.assertRaises(ProviderProtocolError):
                    await self.provider.get_releases(force_refresh=True)

    async def test_failed_fetch_is_not_cached(self):
        self.status = 503
        with self.assertRaises(ProviderUnavailableError):
            await self.provider.get_releases()
        self.status = 200
        await self.provider.get_releases()
        self.assertEqual(len(self.requests), 2)

    async def test_timeout_and_transport_errors_are_classified(self):
        for error, expected in ((httpx2.ReadTimeout("private upstream"), ProviderTimeoutError),
                                (httpx2.ConnectError("private upstream"), ProviderUnavailableError)):
            self.exception = error
            with self.subTest(expected=expected):
                with self.assertRaises(expected) as caught:
                    await self.provider.get_releases(force_refresh=True)
                self.assertNotIn("private upstream", str(caught.exception))

    async def test_invalid_cache_duration_is_rejected(self):
        for value in (-1, float("nan"), float("inf")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    VLCReleaseProvider(self.client, cache_ttl=value)
