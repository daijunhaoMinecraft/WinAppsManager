import asyncio
import copy
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
from Backend.providers.obs import LATEST_URL, OBSReleaseProvider


SHA256 = "37ecde4d9a28e8aeeb07623c8469b5d558eb264e02ccf435bb859891c401758b"


def asset(version="33.0.1", architecture="x64", suffix="Installer.exe"):
    name = f"OBS-Studio-{version}-Windows-{architecture}-{suffix}"
    return {
        "name": name,
        "browser_download_url": f"https://github.com/obsproject/obs-studio/releases/download/{version}/{name}",
        "size": 186662440,
        "digest": f"sha256:{SHA256}",
        "content_type": "application/x-msdos-program",
    }


def payload(version="33.0.1"):
    return {
        "tag_name": version, "name": f"OBS Studio {version}", "draft": False, "prerelease": False,
        "assets": [asset(version), asset(version, "arm64", "PDBs.zip"),
                   asset(version, "arm64", "portable.zip"),
                   {"name": f"obs-studio-{version}-macos-apple.dmg", "browser_download_url": "https://example.test/mac.dmg"}],
    }


class OBSProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.payload = payload()
        self.status = 200
        self.raw = None
        self.exception = None
        self.delay = 0
        self.requests = []
        self.client = httpx2.AsyncClient(transport=httpx2.MockTransport(self.handle))
        self.provider = OBSReleaseProvider(self.client)

    async def asyncTearDown(self):
        await self.client.aclose()

    async def handle(self, request):
        self.requests.append(request)
        self.assertEqual(str(request.url), LATEST_URL)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exception is not None:
            raise self.exception
        if self.raw is not None:
            return httpx2.Response(self.status, content=self.raw, request=request)
        return httpx2.Response(self.status, json=self.payload, request=request)

    async def test_captured_release_has_windows_x64_installer_size_and_digest(self):
        releases = await self.provider.get_releases()
        self.assertEqual(self.provider.id, "obs")
        self.assertEqual(len(releases), 1)
        release = releases[0]
        self.assertEqual(release.id, "33.0.1-windows-x64")
        self.assertEqual(release.version, "33.0.1")
        self.assertEqual(release.architecture, "x64")
        self.assertEqual(release.file_name, "OBS-Studio-33.0.1-Windows-x64-Installer.exe")
        self.assertEqual(release.size_bytes, 186662440)
        self.assertEqual(release.sha256, SHA256)
        self.assertEqual(await self.provider.resolve_download(release), release)
        self.assertEqual(len(self.requests), 1)

    async def test_only_real_current_exe_architectures_are_advertised(self):
        self.payload["assets"].append(asset(architecture="arm64"))
        self.payload["assets"].append(asset("32.0.0", "x86"))
        self.assertEqual([item.architecture for item in await self.provider.get_releases()], ["x64", "arm64"])

    async def test_prerelease_draft_and_missing_status_are_rejected(self):
        for mutation in ({"prerelease": True}, {"draft": True}, {"prerelease": "false"}, {"draft": None}):
            self.payload = payload()
            self.payload.update(mutation)
            with self.subTest(mutation=mutation):
                with self.assertRaises(ProviderProtocolError):
                    await self.provider.get_releases(force_refresh=True)

    async def test_version_assets_and_json_shape_are_validated(self):
        for mutation in ({"tag_name": "33.0.1-beta"}, {"tag_name": None}, {"assets": {}}, {"assets": []}, {"assets": [None]}):
            self.payload = payload()
            self.payload.update(mutation)
            with self.subTest(mutation=mutation):
                with self.assertRaises(ProviderProtocolError):
                    await self.provider.get_releases(force_refresh=True)

    async def test_wrong_url_owner_file_scheme_or_digest_are_rejected(self):
        changes = [{"browser_download_url": "https://example.test/OBS-Studio-33.0.1-Windows-x64-Installer.exe"},
                   {"browser_download_url": "https://github.com/evil/project/releases/download/33.0.1/OBS-Studio-33.0.1-Windows-x64-Installer.exe"},
                   {"browser_download_url": "https://github.com/obsproject/obs-studio/releases/download/33.0.1/other.exe"},
                   {"browser_download_url": "http://github.com/obsproject/obs-studio/releases/download/33.0.1/OBS-Studio-33.0.1-Windows-x64-Installer.exe"},
                   {"digest": "sha256:bad"}, {"size": -1}, {"size": True}]
        for mutation in changes:
            self.payload = payload()
            self.payload["assets"][0].update(mutation)
            with self.subTest(mutation=mutation):
                with self.assertRaises(ProviderProtocolError):
                    await self.provider.get_releases(force_refresh=True)

    async def test_duplicate_current_installers_are_ambiguous(self):
        self.payload["assets"].append(copy.deepcopy(self.payload["assets"][0]))
        with self.assertRaises(ProviderProtocolError):
            await self.provider.get_releases()

    async def test_absent_size_and_digest_stay_unknown(self):
        self.payload["assets"][0].pop("size")
        self.payload["assets"][0].pop("digest")
        release = (await self.provider.get_releases())[0]
        self.assertIsNone(release.size_bytes)
        self.assertIsNone(release.sha256)

    async def test_cache_copy_and_force_refresh(self):
        release = (await self.provider.get_releases())[0]
        release.version = "0.0.0"
        self.assertEqual((await self.provider.get_releases())[0].version, "33.0.1")
        self.payload = payload("33.0.2")
        self.assertEqual((await self.provider.get_releases(force_refresh=True))[0].version, "33.0.2")
        self.assertEqual(len(self.requests), 2)

    async def test_concurrent_metadata_reads_use_one_http_request(self):
        self.delay = 0.02
        await asyncio.gather(*(self.provider.get_releases() for _ in range(4)))
        self.assertEqual(len(self.requests), 1)

    async def test_expired_version_uses_changed_error_and_tamper_is_protocol_error(self):
        with patch("Backend.providers._media.monotonic", return_value=0):
            old = (await self.provider.get_releases())[0]
            with self.assertRaises(ProviderProtocolError):
                await self.provider.resolve_download(old.model_copy(update={"download_url": HttpUrl("https://example.test/wrong.exe")}))
        self.payload = payload("33.0.2")
        with patch("Backend.providers._media.monotonic", return_value=61):
            with self.assertRaises(ProviderReleaseChangedError):
                await self.provider.resolve_download(old)

    async def test_non_json_and_non_object_responses_are_protocol_errors(self):
        self.raw = b"<html>temporary error</html>"
        with self.assertRaises(ProviderProtocolError):
            await self.provider.get_releases()
        self.raw = None
        self.payload = []
        with self.assertRaises(ProviderProtocolError):
            await self.provider.get_releases()

    async def test_http_timeout_and_transport_errors_are_classified(self):
        self.status = 403
        with self.assertRaises(ProviderUnavailableError):
            await self.provider.get_releases()
        self.status = 200
        for exception, expected in ((httpx2.ReadTimeout("private details"), ProviderTimeoutError),
                                    (httpx2.ConnectError("private details"), ProviderUnavailableError)):
            self.exception = exception
            with self.subTest(expected=expected):
                with self.assertRaises(expected) as caught:
                    await self.provider.get_releases()
                self.assertNotIn("private details", str(caught.exception))
