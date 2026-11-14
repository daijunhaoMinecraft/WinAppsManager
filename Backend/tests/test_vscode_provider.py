import asyncio
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import httpx2
from pydantic import HttpUrl

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR.parent))

from Backend.models import Release
from Backend.providers.base import ProviderProtocolError, ProviderReleaseChangedError, ProviderTimeoutError, ProviderUnavailableError, ReleaseProvider
from Backend.providers.vscode import UPDATE_API, VSCodeReleaseProvider, WINDOWS_PLATFORMS


def update_payload(architecture, version="1.145.2"):
    commit = "c4453f8ea430795f941c1b2ff6c9321135a74a02"
    return {
        "url": f"https://vscode.download.prss.microsoft.com/dbazure/download/stable/{commit}/VSCodeUserSetup-{architecture}-{version}.exe",
        "name": version,
        "productVersion": version,
        "version": commit,
        "hash": "9ec820db54043cabd25d47f7ac4dbebab59cfad5",
        "sha256hash": "706ba8b326d87637a923676076eb2936060be6de5c0ae4691b3f76d2cac54f08",
        "timestamp": 1793939987076,
    }


class VSCodeProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.payloads = {architecture: update_payload(architecture) for architecture in WINDOWS_PLATFORMS}
        self.statuses = {}
        self.exception = None
        self.raw = None
        self.delay = 0
        self.requests = []
        self.client = httpx2.AsyncClient(transport=httpx2.MockTransport(self._handle))
        self.provider = VSCodeReleaseProvider(self.client)

    async def asyncTearDown(self):
        await self.client.aclose()

    async def _handle(self, request):
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exception is not None:
            raise self.exception
        architecture = next((architecture for architecture, platform in WINDOWS_PLATFORMS.items()
                             if str(request.url) == UPDATE_API.format(platform=platform)), None)
        if architecture is None:
            raise AssertionError("unexpected update endpoint")
        status = self.statuses.get(architecture, 200)
        if self.raw is not None:
            return httpx2.Response(status, content=self.raw)
        return httpx2.Response(status, json=self.payloads[architecture])

    async def test_real_update_shape_uses_product_version_and_sha256(self):
        self.assertIsInstance(self.provider, ReleaseProvider)
        self.assertEqual(self.provider.id, "vscode")
        releases = await self.provider.get_releases()
        self.assertEqual([release.architecture for release in releases], ["x64", "arm64"])
        for release in releases:
            self.assertEqual(release.version, "1.145.2")
            self.assertEqual(release.id, f"1.145.2-windows-{release.architecture}")
            self.assertEqual(release.file_name, f"VSCodeUserSetup-{release.architecture}-1.145.2.exe")
            self.assertEqual(release.sha256, self.payloads[release.architecture]["sha256hash"])
            self.assertIsNone(release.size_bytes)
            self.assertNotEqual(release.version, self.payloads[release.architecture]["version"])
            self.assertNotEqual(release.sha256, self.payloads[release.architecture]["hash"])
        self.assertTrue(all(request.method == "GET" for request in self.requests))
        self.assertFalse(any("/win32-user/" in str(request.url) for request in self.requests))

    async def test_every_available_architecture_resolves_without_extra_request(self):
        releases = await self.provider.get_releases()
        for release in releases:
            self.assertEqual(await self.provider.resolve_download(release), release)
        self.assertEqual(len(self.requests), 2)

    async def test_unavailable_platforms_are_omitted(self):
        for status in (204, 404):
            self.statuses["arm64"] = status
            self.assertEqual([item.architecture for item in await self.provider.get_releases(force_refresh=True)], ["x64"])
        self.statuses["x64"] = 204
        with self.assertRaises(ProviderProtocolError):
            await self.provider.get_releases(force_refresh=True)

    async def test_cache_force_refresh_and_expiry_track_updated_version(self):
        with patch("Backend.providers.vscode.monotonic", return_value=100):
            first = await self.provider.get_releases()
        self.payloads = {architecture: update_payload(architecture, "1.146.0") for architecture in WINDOWS_PLATFORMS}
        with patch("Backend.providers.vscode.monotonic", return_value=159):
            self.assertEqual((await self.provider.get_releases())[0].version, "1.145.2")
        with patch("Backend.providers.vscode.monotonic", return_value=160):
            self.assertEqual((await self.provider.get_releases())[0].version, "1.146.0")
        with self.assertRaises(ProviderReleaseChangedError):
            await self.provider.resolve_download(first[0])
        self.payloads = {architecture: update_payload(architecture, "1.146.1") for architecture in WINDOWS_PLATFORMS}
        self.assertEqual((await self.provider.get_releases(force_refresh=True))[0].version, "1.146.1")

    async def test_concurrent_reads_share_one_metadata_fetch(self):
        self.delay = 0.02
        results = await asyncio.gather(*(self.provider.get_releases() for _ in range(5)))
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(all(len(releases) == 2 for releases in results))

    async def test_cache_copies_and_caller_metadata_validation(self):
        releases = await self.provider.get_releases()
        releases[0].download_url = HttpUrl("https://attacker.example/installer.exe")
        self.assertNotEqual(str((await self.provider.get_releases())[0].download_url), str(releases[0].download_url))
        with self.assertRaises(ProviderProtocolError):
            await self.provider.resolve_download(releases[0])
        with self.assertRaises(ProviderProtocolError):
            await self.provider.resolve_download({})
        original = (await self.provider.get_releases())[0]
        for changes in ({"sha256": "f" * 64}, {"size_bytes": 100}, {"file_name": "other.exe"}, {"architecture": "x86"}):
            with self.subTest(changes=changes), self.assertRaises(ProviderProtocolError):
                await self.provider.resolve_download(original.model_copy(update=changes))

    async def test_missing_product_version_never_uses_commit(self):
        for version in (None, "", "latest", False, "1.145.2-insider", "1" * 51):
            self.payloads["x64"] = update_payload("x64")
            self.payloads["x64"]["productVersion"] = version
            with self.subTest(version=version), self.assertRaises(ProviderProtocolError):
                await self.provider.get_releases(force_refresh=True)
        self.payloads["x64"].pop("productVersion")
        with self.assertRaises(ProviderProtocolError):
            await self.provider.get_releases(force_refresh=True)

    async def test_invalid_optional_sha256_and_size_are_rejected(self):
        for value in ("short", "g" * 64, False, "a" * 40):
            self.payloads["x64"] = update_payload("x64")
            self.payloads["x64"]["sha256hash"] = value
            with self.subTest(hash=value), self.assertRaises(ProviderProtocolError):
                await self.provider.get_releases(force_refresh=True)
        for size in (0, -1, "100", True):
            self.payloads["x64"] = update_payload("x64")
            self.payloads["x64"]["size"] = size
            with self.subTest(size=size), self.assertRaises(ProviderProtocolError):
                await self.provider.get_releases(force_refresh=True)
        self.payloads["x64"] = update_payload("x64")
        self.payloads["x64"]["size"] = 123
        self.payloads["x64"].pop("sha256hash")
        release = (await self.provider.get_releases(force_refresh=True))[0]
        self.assertEqual(release.size_bytes, 123)
        self.assertIsNone(release.sha256)

    async def test_download_url_and_package_identity_are_checked(self):
        urls = ["http://vscode.download.prss.microsoft.com/a.exe", "https://attacker.example/VSCodeUserSetup-x64-1.145.2.exe",
                "https://user:password@vscode.download.prss.microsoft.com/VSCodeUserSetup-x64-1.145.2.exe",
                "https://vscode.download.prss.microsoft.com/VSCodeUserSetup-x64-1.145.2.exe#fragment",
                "https://vscode.download.prss.microsoft.com/%ZZ.exe",
                "https://vscode.download.prss.microsoft.com/VSCodeUserSetup-arm64-1.145.2.exe",
                "https://vscode.download.prss.microsoft.com/VSCodeUserSetup-x64-1.145.1.exe"]
        for url in urls:
            self.payloads["x64"] = update_payload("x64")
            self.payloads["x64"]["url"] = url
            with self.subTest(url=url), self.assertRaises(ProviderProtocolError):
                await self.provider.get_releases(force_refresh=True)

    async def test_http_timeout_transport_and_invalid_json_errors(self):
        self.statuses["x64"] = 503
        with self.assertRaises(ProviderUnavailableError):
            await self.provider.get_releases()
        self.statuses.clear()
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
        self.assertEqual(len(await self.provider.get_releases()), 2)

    async def test_cache_duration_is_validated(self):
        for ttl in (-1, float("nan"), float("inf")):
            with self.subTest(ttl=ttl), self.assertRaises(ValueError):
                VSCodeReleaseProvider(self.client, cache_ttl=ttl)


class VSCodeProviderImportTests(unittest.TestCase):
    def test_direct_backend_script_import(self):
        result = subprocess.run([sys.executable, "-c", "from providers.vscode import VSCodeReleaseProvider; print(VSCodeReleaseProvider.id)"],
                                cwd=BACKEND_DIR, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "vscode")


if __name__ == "__main__":
    unittest.main()
