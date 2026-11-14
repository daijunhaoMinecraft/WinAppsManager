import asyncio
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import httpx2
from pydantic import HttpUrl

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR.parent))

from Backend.providers.base import ProviderProtocolError, ProviderReleaseChangedError, ProviderTimeoutError, ProviderUnavailableError
from Backend.providers.notepadplusplus import DOWNLOADS_INDEX_URL, RELEASE_API_PREFIX, NotepadPlusPlusReleaseProvider


def index_page(version="8.9.9"):
    return f'''<html><body><h3>Current Version <a href="/downloads/v{version}/">{version}</a></h3>
        <ul><li><a href="/downloads/v{version}/">Download Notepad++ v{version}</a></li>
        <li><a href="/downloads/v8.9.8.1/">Notepad++ v8.9.8.1 stable (auto-update)</a></li></ul></body></html>'''


def release_payload(version="8.9.9"):
    assets = []
    for index, architecture in enumerate(("x64", "x86", "arm64")):
        suffix = "" if architecture == "x86" else f".{architecture}"
        name = f"npp.{version}.Installer{suffix}.exe"
        assets.append({
            "name": name, "state": "uploaded", "size": 10_000_000 + index,
            "digest": f"sha256:{str(index + 1) * 64}",
            "browser_download_url": f"https://github.com/notepad-plus-plus/notepad-plus-plus/releases/download/v{version}/{name}",
        })
    assets.extend([
        {"name": f"npp.{version}.portable.x64.zip", "browser_download_url": "https://github.com/unused/portable.zip"},
        {"name": f"npp.{version}.Installer.x64.exe.sig", "browser_download_url": "https://github.com/unused/file.sig"},
        {"name": f"npp.{version}.minimalist.7z", "browser_download_url": "https://github.com/unused/minimalist.7z"},
        {"name": f"npp.{version}.x64.msi", "browser_download_url": "https://github.com/unused/file.msi"},
    ])
    return {"tag_name": f"v{version}", "draft": False, "prerelease": False, "assets": assets}


class NotepadPlusPlusProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.version = "8.9.9"
        self.index = index_page()
        self.payload = release_payload()
        self.status = 200
        self.failure = None
        self.raw_json = None
        self.delay = 0
        self.requests = []
        self.client = httpx2.AsyncClient(transport=httpx2.MockTransport(self._handle))
        self.provider = NotepadPlusPlusReleaseProvider(self.client)

    async def asyncTearDown(self):
        await self.client.aclose()

    async def _handle(self, request):
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.failure:
            raise self.failure
        if str(request.url) == DOWNLOADS_INDEX_URL:
            return httpx2.Response(self.status, text=self.index, request=request)
        self.assertEqual(str(request.url), RELEASE_API_PREFIX + self.version)
        if self.raw_json is not None:
            return httpx2.Response(self.status, content=self.raw_json, request=request)
        return httpx2.Response(self.status, json=self.payload, request=request)

    async def test_current_site_version_selects_matching_three_architecture_installers(self):
        releases = await self.provider.get_releases()
        self.assertEqual(self.provider.id, "notepadplusplus")
        self.assertEqual([item.architecture for item in releases], ["x64", "x86", "arm64"])
        self.assertTrue(all(item.version == "8.9.9" for item in releases))
        self.assertEqual([item.file_name for item in releases], [
            "npp.8.9.9.Installer.x64.exe", "npp.8.9.9.Installer.exe", "npp.8.9.9.Installer.arm64.exe",
        ])
        self.assertEqual([item.size_bytes for item in releases], [10_000_000, 10_000_001, 10_000_002])
        self.assertEqual(releases[0].sha256, "1" * 64)
        self.assertEqual(self.requests[1].headers["accept"], "application/vnd.github+json")

    async def test_historical_auto_update_release_is_not_selected(self):
        await self.provider.get_releases()
        self.assertEqual(str(self.requests[1].url), RELEASE_API_PREFIX + "8.9.9")

    async def test_script_and_style_text_do_not_fake_current_version(self):
        self.index = '<script>Current Version 99.99</script><style>Current Version 88.88</style>' + self.index
        self.assertEqual((await self.provider.get_releases())[0].version, "8.9.9")

    async def test_metadata_cache_concurrency_and_copies(self):
        self.delay = 0.01
        batches = await asyncio.gather(*(self.provider.get_releases() for _ in range(5)))
        self.assertEqual(len(self.requests), 2)
        batches[0][0].size_bytes = 1
        self.assertEqual((await self.provider.get_releases())[0].size_bytes, 10_000_000)

    async def test_forced_refresh_returns_current_new_version(self):
        await self.provider.get_releases()
        self.version = "8.9.10"
        self.index = index_page(self.version)
        self.payload = release_payload(self.version)
        self.assertEqual((await self.provider.get_releases())[0].version, "8.9.9")
        self.assertEqual((await self.provider.get_releases(force_refresh=True))[0].version, "8.9.10")
        self.assertEqual(len(self.requests), 4)

    async def test_cache_expiry_reads_new_official_version(self):
        with patch("Backend.providers._utility.monotonic", return_value=0):
            await self.provider.get_releases()
        self.version = "8.9.10"
        self.index = index_page(self.version)
        self.payload = release_payload(self.version)
        with patch("Backend.providers._utility.monotonic", return_value=61):
            self.assertEqual((await self.provider.get_releases())[0].version, "8.9.10")

    async def test_unsupported_or_not_uploaded_architectures_are_skipped(self):
        self.payload["assets"][1]["state"] = "new"
        self.payload["assets"] = self.payload["assets"][:2]
        self.assertEqual([item.architecture for item in await self.provider.get_releases()], ["x64"])

    async def test_digest_and_size_can_be_absent_without_inventing_values(self):
        for asset in self.payload["assets"][:3]:
            asset.pop("size")
            asset.pop("digest")
        releases = await self.provider.get_releases()
        self.assertTrue(all(item.size_bytes is None and item.sha256 is None for item in releases))

    async def test_non_sha256_digest_does_not_become_a_sha256(self):
        self.payload["assets"][0]["digest"] = "sha512:" + "a" * 128
        self.assertIsNone((await self.provider.get_releases())[0].sha256)

    async def test_missing_current_version_is_a_protocol_error(self):
        self.index = '<html><a href="/downloads/v8.9.8.1/">Historical release</a></html>'
        with self.assertRaises(ProviderProtocolError):
            await self.provider.get_releases()

    async def test_tag_prerelease_and_draft_must_match_current_stable_site_release(self):
        mutations = ({"tag_name": "v8.9.8.1"}, {"prerelease": True}, {"draft": True}, {"prerelease": "false"})
        for mutation in mutations:
            self.payload = {**release_payload(), **mutation}
            with self.subTest(mutation=mutation), self.assertRaises(ProviderProtocolError):
                await self.provider.get_releases(force_refresh=True)

    async def test_assets_require_a_valid_collection_and_current_installers(self):
        for assets in (None, {}, [], [None], [{"name": "npp.8.9.8.1.Installer.x64.exe"}], [{"name": "npp.8.9.9.portable.x64.zip"}]):
            self.payload = {**release_payload(), "assets": assets}
            with self.subTest(assets=assets), self.assertRaises(ProviderProtocolError):
                await self.provider.get_releases(force_refresh=True)

    async def test_installer_urls_must_have_official_host_repo_tag_and_file_name(self):
        for url in (
            "https://evil.example/npp.8.9.9.Installer.x64.exe",
            "http://github.com/notepad-plus-plus/notepad-plus-plus/releases/download/v8.9.9/npp.8.9.9.Installer.x64.exe",
            "https://github.com/other/repo/releases/download/v8.9.9/npp.8.9.9.Installer.x64.exe",
            "https://github.com/notepad-plus-plus/notepad-plus-plus/releases/download/v8.9.8.1/npp.8.9.9.Installer.x64.exe",
            "https://github.com/notepad-plus-plus/notepad-plus-plus/releases/download/v8.9.9/other.exe",
            "https://github.com/notepad-plus-plus/notepad-plus-plus/releases/download/v8.9.9/../npp.8.9.9.Installer.x64.exe",
        ):
            self.payload = release_payload()
            self.payload["assets"][0]["browser_download_url"] = url
            with self.subTest(url=url), self.assertRaises(ProviderProtocolError):
                await self.provider.get_releases(force_refresh=True)

    async def test_duplicates_invalid_size_and_invalid_sha256_are_rejected(self):
        self.payload["assets"].append(copy.deepcopy(self.payload["assets"][0]))
        with self.assertRaises(ProviderProtocolError):
            await self.provider.get_releases()
        for size in (0, -1, "100", True):
            self.payload = release_payload()
            self.payload["assets"][0]["size"] = size
            with self.subTest(size=size), self.assertRaises(ProviderProtocolError):
                await self.provider.get_releases(force_refresh=True)
        for digest in (123, "sha256:bad", "sha256:" + "z" * 64):
            self.payload = release_payload()
            self.payload["assets"][0]["digest"] = digest
            with self.subTest(digest=digest), self.assertRaises(ProviderProtocolError):
                await self.provider.get_releases(force_refresh=True)

    async def test_authoritative_resolution_rejects_tampering_and_stale_versions(self):
        release = (await self.provider.get_releases())[0]
        self.assertEqual(await self.provider.resolve_download(release), release)
        with self.assertRaises(ProviderProtocolError):
            await self.provider.resolve_download(release.model_copy(update={"download_url": HttpUrl("https://evil.example/file.exe")}))
        self.version = "8.9.10"
        self.index = index_page(self.version)
        self.payload = release_payload(self.version)
        await self.provider.get_releases(force_refresh=True)
        with self.assertRaises(ProviderReleaseChangedError):
            await self.provider.resolve_download(release)

    async def test_http_transport_timeout_and_malformed_json_are_distinct_errors(self):
        self.status = 503
        with self.assertRaises(ProviderUnavailableError):
            await self.provider.get_releases()
        self.status = 200
        self.failure = httpx2.ConnectError("transport detail")
        with self.assertRaises(ProviderUnavailableError):
            await self.provider.get_releases()
        self.failure = httpx2.ReadTimeout("timeout detail")
        with self.assertRaises(ProviderTimeoutError):
            await self.provider.get_releases()
        self.failure = None
        self.raw_json = b"<html>error</html>"
        with self.assertRaises(ProviderProtocolError):
            await self.provider.get_releases()

    async def test_errors_do_not_cache_a_bad_result(self):
        self.payload = {"message": "unavailable"}
        with self.assertRaises(ProviderProtocolError):
            await self.provider.get_releases()
        self.payload = release_payload()
        self.assertEqual(len(await self.provider.get_releases()), 3)


if __name__ == "__main__":
    unittest.main()
