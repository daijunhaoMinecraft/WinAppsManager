import asyncio
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx2
from pydantic import HttpUrl

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR.parent))

from Backend.providers.base import ProviderProtocolError, ProviderReleaseChangedError, ProviderTimeoutError, ProviderUnavailableError, ReleaseProvider
from Backend.providers.firefox import CHECKSUM_URL, FirefoxReleaseProvider, LOCALE, VERSIONS_URL, WINDOWS_PLATFORMS


class FirefoxProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.version = "159.0.1"
        self.payload_override = None
        self.raw = None
        self.exception = None
        self.status = 200
        self.checksum_status = 200
        self.checksum_override = None
        self.location_override = None
        self.redirect_status = 302
        self.unavailable = set()
        self.delay = 0
        self.requests = []
        self.client = httpx2.AsyncClient(transport=httpx2.MockTransport(self._handle))
        self.provider = FirefoxReleaseProvider(self.client)

    async def asyncTearDown(self):
        await self.client.aclose()

    async def _handle(self, request):
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exception is not None:
            raise self.exception
        if str(request.url) == VERSIONS_URL:
            if self.raw is not None:
                return httpx2.Response(self.status, content=self.raw)
            payload = self.payload_override if self.payload_override is not None else {
                "LATEST_FIREFOX_VERSION": self.version, "FIREFOX_ESR": "153.6.0esr", "FIREFOX_NIGHTLY": "162.0a1",
            }
            return httpx2.Response(self.status, json=payload)
        if str(request.url) == CHECKSUM_URL.format(version=self.version):
            text = self.checksum_override
            if text is None:
                text = "\n".join(f"{'a' if architecture == 'x64' else 'b' if architecture == 'x86' else 'c'}" * 64
                                 + f"  {directory}/{LOCALE}/Firefox Setup {self.version}.exe"
                                 for architecture, (_, directory) in WINDOWS_PLATFORMS.items())
            return httpx2.Response(self.checksum_status, text=text)
        if request.url.host == "download.mozilla.org":
            params = parse_qs(request.url.query.decode())
            self.assertEqual(params["product"], [f"firefox-{self.version}-ssl"])
            self.assertEqual(params["lang"], [LOCALE])
            platform = params["os"][0]
            if platform in self.unavailable:
                return httpx2.Response(404)
            directory = next(directory for _, (candidate, directory) in WINDOWS_PLATFORMS.items() if candidate == platform)
            location = self.location_override or f"https://download-installer.cdn.mozilla.net/pub/firefox/releases/{self.version}/{directory}/{LOCALE}/Firefox Setup {self.version}.exe"
            return httpx2.Response(self.redirect_status, headers={"location": location})
        raise AssertionError("unexpected official metadata endpoint")

    async def test_stable_version_official_redirects_and_three_sha256_values(self):
        self.assertIsInstance(self.provider, ReleaseProvider)
        releases = await self.provider.get_releases()
        self.assertEqual(self.provider.id, "firefox")
        self.assertEqual([release.architecture for release in releases], ["x64", "x86", "arm64"])
        for release, digest in zip(releases, ("a", "b", "c")):
            self.assertEqual(release.version, "159.0.1")
            self.assertEqual(release.id, f"159.0.1-windows-{release.architecture}")
            self.assertEqual(release.file_name, "Firefox Setup 159.0.1.exe")
            self.assertEqual(release.sha256, digest * 64)
            self.assertIsNone(release.size_bytes)
            self.assertIn("/zh-CN/Firefox%20Setup%20159.0.1.exe", str(release.download_url))
        self.assertEqual(len(self.requests), 5)

    async def test_resolution_only_accepts_authoritative_metadata(self):
        release = (await self.provider.get_releases())[0]
        self.assertEqual(await self.provider.resolve_download(release), release)
        self.assertEqual(len(self.requests), 5)
        for changes in ({"download_url": HttpUrl("https://attacker.example/other.exe")}, {"sha256": "f" * 64}, {"architecture": "x86"}):
            with self.subTest(changes=changes), self.assertRaises(ProviderProtocolError):
                await self.provider.resolve_download(release.model_copy(update=changes))
        with self.assertRaises(ProviderProtocolError):
            await self.provider.resolve_download({})
        release.file_name = "mutated.exe"
        self.assertEqual((await self.provider.get_releases())[0].file_name, "Firefox Setup 159.0.1.exe")

    async def test_cache_force_refresh_expiry_and_version_change(self):
        with patch("Backend.providers.firefox.monotonic", return_value=100):
            old = await self.provider.get_releases()
        self.version = "159.0.2"
        with patch("Backend.providers.firefox.monotonic", return_value=159):
            self.assertEqual((await self.provider.get_releases())[0].version, "159.0.1")
        with patch("Backend.providers.firefox.monotonic", return_value=160):
            self.assertEqual((await self.provider.get_releases())[0].version, "159.0.2")
        with self.assertRaises(ProviderReleaseChangedError):
            await self.provider.resolve_download(old[0])
        self.version = "160.0"
        self.assertEqual((await self.provider.get_releases(force_refresh=True))[0].version, "160.0")

    async def test_concurrent_reads_share_one_fetch(self):
        self.delay = 0.01
        results = await asyncio.gather(*(self.provider.get_releases() for _ in range(4)))
        self.assertEqual(len(self.requests), 5)
        self.assertTrue(all(len(releases) == 3 for releases in results))

    async def test_absent_package_architecture_is_omitted(self):
        self.unavailable = {"win64-aarch64"}
        self.assertEqual([item.architecture for item in await self.provider.get_releases()], ["x64", "x86"])
        self.unavailable = {"win", "win64", "win64-aarch64"}
        with self.assertRaises(ProviderProtocolError):
            await self.provider.get_releases(force_refresh=True)

    async def test_stable_version_does_not_fall_back_to_esr_or_nightly(self):
        for version in (None, "", False, "153.6.0esr", "162.0a1", "LATEST", "1" * 51):
            self.payload_override = {"LATEST_FIREFOX_VERSION": version, "FIREFOX_ESR": "153.6.0esr"}
            with self.subTest(version=version), self.assertRaises(ProviderProtocolError):
                await self.provider.get_releases(force_refresh=True)
        self.payload_override = {"FIREFOX_ESR": "153.6.0esr"}
        with self.assertRaises(ProviderProtocolError):
            await self.provider.get_releases(force_refresh=True)

    async def test_redirects_must_match_version_architecture_locale_and_official_host(self):
        locations = [
            "http://download-installer.cdn.mozilla.net/pub/firefox/releases/159.0.1/win64/zh-CN/Firefox Setup 159.0.1.exe",
            "https://attacker.example/pub/firefox/releases/159.0.1/win64/zh-CN/Firefox Setup 159.0.1.exe",
            "https://user:password@download-installer.cdn.mozilla.net/pub/firefox/releases/159.0.1/win64/zh-CN/Firefox Setup 159.0.1.exe",
            "https://download-installer.cdn.mozilla.net/pub/firefox/releases/159.0.1/win64/en-US/Firefox Setup 159.0.1.exe",
            "https://download-installer.cdn.mozilla.net/pub/firefox/releases/159.0.2/win64/zh-CN/Firefox Setup 159.0.2.exe",
            "https://download-installer.cdn.mozilla.net/pub/firefox/releases/159.0.1/win64/zh-CN/Firefox Setup 159.0.1.exe#fragment",
            "https://download-installer.cdn.mozilla.net/%ZZ.exe",
        ]
        for location in locations:
            self.location_override = location
            with self.subTest(location=location), self.assertRaises(ProviderProtocolError):
                await self.provider.get_releases(force_refresh=True)
        self.location_override = None
        self.redirect_status = 200
        with self.assertRaises(ProviderProtocolError):
            await self.provider.get_releases(force_refresh=True)

    async def test_official_checksum_manifest_is_validated(self):
        path = "win64/zh-CN/Firefox Setup 159.0.1.exe"
        for content in (f"{'g' * 64}  {path}", f"{'a' * 64}  {path}\n{'b' * 64}  {path}", "<html>error</html>"):
            self.checksum_override = content
            with self.subTest(content=content), self.assertRaises(ProviderProtocolError):
                await self.provider.get_releases(force_refresh=True)
        self.checksum_override = f"{'a' * 64}  {path}"
        with self.assertRaises(ProviderProtocolError):
            await self.provider.get_releases(force_refresh=True)

    async def test_missing_optional_checksum_manifest_preserves_unknown_checksum(self):
        self.checksum_status = 404
        releases = await self.provider.get_releases()
        self.assertTrue(all(item.sha256 is None for item in releases))

    async def test_http_timeout_transport_and_bad_json_errors(self):
        self.status = 503
        with self.assertRaises(ProviderUnavailableError):
            await self.provider.get_releases()
        self.status = 200
        for exception, expected in ((httpx2.ReadTimeout("private details"), ProviderTimeoutError),
                                    (httpx2.ConnectError("private details"), ProviderUnavailableError)):
            self.exception = exception
            with self.assertRaises(expected) as result:
                await self.provider.get_releases()
            self.assertNotIn("private details", str(result.exception))
        self.exception = None
        for raw in (b"<html>error</html>", b"[]", b"null"):
            self.raw = raw
            with self.subTest(raw=raw), self.assertRaises(ProviderProtocolError):
                await self.provider.get_releases()
        self.raw = None
        self.assertEqual(len(await self.provider.get_releases()), 3)

    async def test_cache_duration_is_validated(self):
        for ttl in (-1, float("nan"), float("inf")):
            with self.subTest(ttl=ttl), self.assertRaises(ValueError):
                FirefoxReleaseProvider(self.client, cache_ttl=ttl)


class FirefoxProviderImportTests(unittest.TestCase):
    def test_direct_backend_script_import(self):
        result = subprocess.run([sys.executable, "-c", "from providers.firefox import FirefoxReleaseProvider; print(FirefoxReleaseProvider.id)"],
                                cwd=BACKEND_DIR, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "firefox")


if __name__ == "__main__":
    unittest.main()
