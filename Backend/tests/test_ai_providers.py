import asyncio
from pathlib import Path
import sys
import unittest

import httpx2
from pydantic import HttpUrl

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR.parent))

from Backend.providers.base import (
    ProviderProtocolError,
    ProviderReleaseChangedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from Backend.providers.deepseek_harness import DOWNLOAD_URL as DSH_DOWNLOAD_URL
from Backend.providers.deepseek_harness import DeepSeekHarnessReleaseProvider
from Backend.providers.doubao import LANDING_CLICK_URL, WIN_X64_SURL_TOKEN, DoubaoReleaseProvider
from Backend.providers.workbuddy import UPDATE_URL as WORKBUDDY_UPDATE_URL
from Backend.providers.workbuddy import WorkBuddyReleaseProvider


WORKBUDDY_VERSION = "5.9.3.42398134"
WORKBUDDY_FILE = f"WorkBuddy-win32-x64-user-{WORKBUDDY_VERSION}-d4692421.exe"
WORKBUDDY_DOWNLOAD_URL = f"https://download.codebuddy.cn/workbuddy/saas/win32-x64-user/{WORKBUDDY_FILE}"
WORKBUDDY_SHA256 = "b5edb52bd6c5b6c7296cf2fff697b019c86d11992a8a814c3010d611896852f9"
WORKBUDDY_SIZE = 450_174_440

DSH_VERSION = "0.2.2-rc.3"
DSH_OFFICIAL_FILE = f"deepseek-harness-{DSH_VERSION}-win-x64.exe"
DSH_FILE = "dsh-latest-windows-x64.exe"
DSH_SIZE = 299_595_752

DOUBAO_BUILD = "2035012"
DOUBAO_FILE = "doubao_client_webdaoliu_doudi_online_installer_win_v2035012_59db_1794554757.exe"
DOUBAO_DOWNLOAD_URL = (
    f"https://lf9-apk.ugapk.cn/package/pc/9c6c4f1e-8d3a-4b8e-9b8d-0e7a2d4c1f33/{DOUBAO_FILE}"
    "?v=1794554759&filename=doubao_client_77737216855801411777_ext.exe"
)
DOUBAO_SIZE = 7_466_992


def workbuddy_payload(**overrides):
    payload = {
        "version": WORKBUDDY_VERSION,
        "url": WORKBUDDY_DOWNLOAD_URL,
        "productVersion": WORKBUDDY_VERSION,
        "sha256hash": WORKBUDDY_SHA256,
        "timestamp": 1793121334,
        "hash": "",
        "name": "",
        "supportsFastUpdate": False,
    }
    payload.update(overrides)
    return payload


def doubao_payload(url=DOUBAO_DOWNLOAD_URL, **overrides):
    payload = {"ret": 0, "errmsg": "success", "log_id": "202611141125317A63205C12C0C053C0A5",
               "data": {"uuid": 37216855801411, "download_url": url}}
    payload.update(overrides)
    return payload


class WorkBuddyProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.payload = workbuddy_payload()
        self.status = 200
        self.raw_body = None
        self.failure = None
        self.delay = 0
        self.head_status = 200
        self.head_headers = {"content-length": str(WORKBUDDY_SIZE)}
        self.head_failure = None
        self.requests = []
        self.client = httpx2.AsyncClient(transport=httpx2.MockTransport(self._handle))
        self.provider = WorkBuddyReleaseProvider(self.client)

    async def asyncTearDown(self):
        await self.client.aclose()

    async def _handle(self, request):
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.failure:
            raise self.failure
        if request.method == "HEAD":
            if self.head_failure:
                raise self.head_failure
            return httpx2.Response(self.head_status, headers=self.head_headers, request=request)
        self.assertEqual(str(request.url), WORKBUDDY_UPDATE_URL)
        if self.raw_body is not None:
            return httpx2.Response(self.status, content=self.raw_body, request=request)
        return httpx2.Response(self.status, json=self.payload, request=request)

    async def test_update_document_yields_the_single_x64_installer(self):
        releases = await self.provider.get_releases()
        self.assertEqual(self.provider.id, "workbuddy")
        self.assertEqual(self.provider.display_name, "WorkBuddy")
        self.assertEqual(len(releases), 1)
        release = releases[0]
        self.assertEqual(release.id, f"{WORKBUDDY_VERSION}-windows-x64")
        self.assertEqual(release.version, WORKBUDDY_VERSION)
        self.assertEqual(release.platform, "windows")
        self.assertEqual(release.architecture, "x64")
        self.assertEqual(release.file_name, WORKBUDDY_FILE)
        self.assertEqual(str(release.download_url), WORKBUDDY_DOWNLOAD_URL)
        self.assertEqual(release.size_bytes, WORKBUDDY_SIZE)
        self.assertEqual(release.sha256, WORKBUDDY_SHA256)
        self.assertEqual(self.requests[0].headers["accept"], "application/json")
        self.assertEqual(self.requests[1].method, "HEAD")

    async def test_sha256hash_is_lowercased_and_installer_name_matches_version(self):
        self.payload["sha256hash"] = WORKBUDDY_SHA256.upper()
        release = (await self.provider.get_releases())[0]
        self.assertEqual(release.sha256, WORKBUDDY_SHA256)
        self.assertTrue(release.file_name.startswith(f"WorkBuddy-win32-x64-user-{release.version}-"))

    async def test_version_must_be_an_official_dotted_version(self):
        for version in (None, 5_903, "5.9", "5.9.3.4.5", "5.9.3-beta", " 5.9.3", "", "v5.9.3"):
            self.payload = workbuddy_payload(version=version)
            with self.subTest(version=version), self.assertRaises(ProviderProtocolError):
                await self.provider.get_releases(force_refresh=True)

    async def test_installer_url_must_stay_on_the_official_cdn(self):
        for url in (
            None,
            "",
            f"http://download.codebuddy.cn/workbuddy/saas/win32-x64-user/{WORKBUDDY_FILE}",
            f"https://evil.example/workbuddy/saas/win32-x64-user/{WORKBUDDY_FILE}",
            f"https://download.codebuddy.cn/other/path/{WORKBUDDY_FILE}",
            f"https://download.codebuddy.cn/workbuddy/saas/win32-x64-user/{WORKBUDDY_FILE}.zip",
            WORKBUDDY_DOWNLOAD_URL + "#fragment",
        ):
            self.payload = workbuddy_payload(url=url)
            with self.subTest(url=url), self.assertRaises(ProviderProtocolError):
                await self.provider.get_releases(force_refresh=True)

    async def test_installer_file_name_must_carry_the_same_version(self):
        other = "WorkBuddy-win32-x64-user-5.9.3.42398135-d4692421.exe"
        self.payload = workbuddy_payload(
            url=f"https://download.codebuddy.cn/workbuddy/saas/win32-x64-user/{other}"
        )
        with self.assertRaises(ProviderProtocolError):
            await self.provider.get_releases()

    async def test_sha256hash_must_be_a_real_sha256(self):
        for value in (None, "", "sha256:" + "a" * 64, "a" * 63, "z" * 64, 123):
            self.payload = workbuddy_payload(sha256hash=value)
            with self.subTest(value=value), self.assertRaises(ProviderProtocolError):
                await self.provider.get_releases(force_refresh=True)

    async def test_installer_size_is_optional_metadata(self):
        self.head_status = 500
        release = (await self.provider.get_releases())[0]
        self.assertIsNone(release.size_bytes)
        self.head_status = 200
        self.head_headers = {}
        self.assertIsNone((await self.provider.get_releases(force_refresh=True))[0].size_bytes)

    async def test_invalid_or_missing_head_metadata_fails_cleanly(self):
        for headers in ({"content-length": "abc"}, {"content-length": "0"}, {"content-length": "-5"}):
            self.head_headers = headers
            with self.subTest(headers=headers), self.assertRaises(ProviderProtocolError):
                await self.provider.get_releases(force_refresh=True)

    async def test_head_timeout_surfaces_but_head_transport_error_does_not(self):
        self.head_failure = httpx2.ReadTimeout("head timeout")
        with self.assertRaises(ProviderTimeoutError):
            await self.provider.get_releases()
        self.head_failure = httpx2.ConnectError("head transport")
        self.assertIsNone((await self.provider.get_releases(force_refresh=True))[0].size_bytes)

    async def test_http_and_transport_errors_on_the_update_document_are_distinct(self):
        self.status = 503
        with self.assertRaises(ProviderUnavailableError):
            await self.provider.get_releases()
        self.status = 200
        self.failure = httpx2.ConnectError("transport detail")
        with self.assertRaises(ProviderUnavailableError):
            await self.provider.get_releases(force_refresh=True)
        self.failure = httpx2.ReadTimeout("timeout detail")
        with self.assertRaises(ProviderTimeoutError):
            await self.provider.get_releases(force_refresh=True)

    async def test_malformed_json_is_a_protocol_error(self):
        self.raw_body = b"<html>maintenance</html>"
        with self.assertRaises(ProviderProtocolError):
            await self.provider.get_releases()
        self.raw_body = b'["not", "an", "object"]'
        with self.assertRaises(ProviderProtocolError):
            await self.provider.get_releases(force_refresh=True)

    async def test_errors_do_not_cache_a_partial_result(self):
        self.payload = {"version": WORKBUDDY_VERSION}
        with self.assertRaises(ProviderProtocolError):
            await self.provider.get_releases()
        self.payload = workbuddy_payload()
        self.assertEqual(len(await self.provider.get_releases()), 1)

    async def test_metadata_cache_returns_copies_and_refresh_requeries(self):
        first = await self.provider.get_releases()
        first[0].size_bytes = 1
        self.assertEqual((await self.provider.get_releases())[0].size_bytes, WORKBUDDY_SIZE)
        self.assertEqual(len(self.requests), 2)
        await self.provider.get_releases(force_refresh=True)
        self.assertEqual(len(self.requests), 4)

    async def test_resolve_download_checks_release_identity(self):
        release = (await self.provider.get_releases())[0]
        self.assertEqual(await self.provider.resolve_download(release), release)
        for mutation in (
            {"id": "9.9.9-windows-x64"},
            {"file_name": "evil.exe"},
            {"download_url": HttpUrl("https://evil.example/file.exe")},
            {"sha256": "0" * 64},
        ):
            with self.subTest(mutation=mutation), self.assertRaises(ProviderProtocolError):
                await self.provider.resolve_download(release.model_copy(update=mutation))
        self.payload = workbuddy_payload(version="5.9.4.1", url=(
            "https://download.codebuddy.cn/workbuddy/saas/win32-x64-user/"
            "WorkBuddy-win32-x64-user-5.9.4.1-aabbccdd.exe"
        ))
        await self.provider.get_releases(force_refresh=True)
        with self.assertRaises(ProviderReleaseChangedError):
            await self.provider.resolve_download(release)


class DeepSeekHarnessProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.disposition = f'attachment; filename="{DSH_OFFICIAL_FILE}"'
        self.head_status = 200
        self.head_headers = {
            "content-disposition": self.disposition,
            "content-length": str(DSH_SIZE),
            "content-type": "application/vnd.microsoft.portable-executable",
        }
        self.head_failure = None
        self.range_status = 206
        self.range_headers = {
            "content-disposition": self.disposition,
            "content-length": "1",
            "content-range": f"bytes 0-0/{DSH_SIZE}",
        }
        self.range_failure = None
        self.requests = []
        self.client = httpx2.AsyncClient(transport=httpx2.MockTransport(self._handle))
        self.provider = DeepSeekHarnessReleaseProvider(self.client)

    async def asyncTearDown(self):
        await self.client.aclose()

    async def _handle(self, request):
        self.requests.append(request)
        self.assertEqual(str(request.url), DSH_DOWNLOAD_URL)
        if request.method == "HEAD":
            if self.head_failure:
                raise self.head_failure
            headers = dict(self.head_headers)
            if self.disposition is None:
                headers.pop("content-disposition", None)
            else:
                headers["content-disposition"] = self.disposition
            return httpx2.Response(self.head_status, headers=headers, request=request)
        self.assertEqual(request.headers.get("range"), "bytes=0-0")
        if self.range_failure:
            raise self.range_failure
        headers = dict(self.range_headers)
        if self.disposition is None:
            headers.pop("content-disposition", None)
        else:
            headers["content-disposition"] = self.disposition
        return httpx2.Response(self.range_status, headers=headers, request=request, content=b"M")

    async def test_content_disposition_file_name_supplies_the_version(self):
        releases = await self.provider.get_releases()
        self.assertEqual(self.provider.id, "deepseekharness")
        self.assertEqual(self.provider.display_name, "DeepSeek Harness")
        self.assertEqual(len(releases), 1)
        release = releases[0]
        self.assertEqual(release.id, f"{DSH_VERSION}-windows-x64")
        self.assertEqual(release.version, DSH_VERSION)
        self.assertEqual(release.architecture, "x64")
        self.assertEqual(release.file_name, DSH_FILE)
        self.assertEqual(str(release.download_url), DSH_DOWNLOAD_URL)
        self.assertEqual(release.size_bytes, DSH_SIZE)
        self.assertIsNone(release.sha256)
        self.assertEqual(self.requests[0].method, "HEAD")

    async def test_unquoted_and_extended_file_names_are_supported(self):
        self.disposition = f"attachment; filename={DSH_OFFICIAL_FILE}"
        self.assertEqual((await self.provider.get_releases())[0].version, DSH_VERSION)
        self.disposition = f"attachment; filename*=UTF-8''{DSH_OFFICIAL_FILE}"
        self.assertEqual((await self.provider.get_releases(force_refresh=True))[0].version, DSH_VERSION)

    async def test_missing_or_malformed_content_disposition_is_a_protocol_error(self):
        for disposition in (
            None,
            "",
            "attachment",
            'attachment; filename="deepseek-harness-win-x64.exe"',
            'attachment; filename="deepseek-harness-0.2.2-RC.3-win-x64.exe"',
            'attachment; filename="deepseek-harness-0.2.2-rc.3-win-arm64.exe"',
            'attachment; filename="deepseek-harness-0.2.2-rc.3-win-x64.zip"',
            'attachment; filename="../../deepseek-harness-0.2.2-rc.3-win-x64.exe"',
        ):
            self.disposition = disposition
            with self.subTest(disposition=disposition), self.assertRaises(ProviderProtocolError):
                await self.provider.get_releases(force_refresh=True)

    async def test_head_rejected_falls_back_to_a_one_byte_ranged_get(self):
        self.head_status = 405
        release = (await self.provider.get_releases())[0]
        self.assertEqual(release.version, DSH_VERSION)
        self.assertEqual(release.size_bytes, DSH_SIZE)
        self.assertEqual([request.method for request in self.requests], ["HEAD", "GET"])
        self.assertEqual(self.requests[1].headers.get("range"), "bytes=0-0")

    async def test_ranged_fallback_accepts_a_server_that_ignores_the_range(self):
        self.head_status = 501
        self.range_status = 200
        self.range_headers = {"content-disposition": self.disposition, "content-length": str(DSH_SIZE)}
        self.assertEqual((await self.provider.get_releases())[0].size_bytes, DSH_SIZE)

    async def test_missing_size_is_not_invented(self):
        self.head_headers = {"content-disposition": self.disposition}
        self.assertIsNone((await self.provider.get_releases())[0].size_bytes)

    async def test_invalid_size_headers_are_protocol_errors(self):
        for headers in (
            {"content-disposition": self.disposition, "content-length": "abc"},
            {"content-disposition": self.disposition, "content-length": "0"},
            {"content-disposition": self.disposition, "content-range": "bytes 0-0/*"},
        ):
            self.head_headers = headers
            with self.subTest(headers=headers), self.assertRaises(ProviderProtocolError):
                await self.provider.get_releases(force_refresh=True)

    async def test_head_failures_map_to_provider_errors(self):
        self.head_status = 404
        with self.assertRaises(ProviderUnavailableError):
            await self.provider.get_releases()
        self.head_status = 200
        self.head_failure = httpx2.ReadTimeout("head timeout")
        with self.assertRaises(ProviderTimeoutError):
            await self.provider.get_releases(force_refresh=True)
        self.head_failure = httpx2.ConnectError("head transport")
        with self.assertRaises(ProviderUnavailableError):
            await self.provider.get_releases(force_refresh=True)

    async def test_ranged_fallback_failures_map_to_provider_errors(self):
        self.head_status = 405
        self.range_status = 500
        with self.assertRaises(ProviderUnavailableError):
            await self.provider.get_releases()
        self.range_status = 206
        self.range_failure = httpx2.ReadTimeout("range timeout")
        with self.assertRaises(ProviderTimeoutError):
            await self.provider.get_releases(force_refresh=True)

    async def test_metadata_cache_returns_copies_and_refresh_requeries(self):
        await self.provider.get_releases()
        await self.provider.get_releases()
        self.assertEqual(len(self.requests), 1)
        await self.provider.get_releases(force_refresh=True)
        self.assertEqual(len(self.requests), 2)

    async def test_resolve_download_checks_release_identity(self):
        release = (await self.provider.get_releases())[0]
        self.assertEqual(await self.provider.resolve_download(release), release)
        for mutation in (
            {"id": "9.9.9-windows-x64"},
            {"file_name": "latest.exe"},
            {"download_url": HttpUrl("https://evil.example/dsh.exe")},
            {"size_bytes": 1},
        ):
            with self.subTest(mutation=mutation), self.assertRaises(ProviderProtocolError):
                await self.provider.resolve_download(release.model_copy(update=mutation))
        self.disposition = 'attachment; filename="deepseek-harness-0.3.0-win-x64.exe"'
        await self.provider.get_releases(force_refresh=True)
        with self.assertRaises(ProviderReleaseChangedError):
            await self.provider.resolve_download(release)


class DoubaoProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.payload = doubao_payload()
        self.status = 200
        self.raw_body = None
        self.failure = None
        self.head_status = 200
        self.head_headers = {"content-length": str(DOUBAO_SIZE)}
        self.head_failure = None
        self.requests = []
        self.client = httpx2.AsyncClient(transport=httpx2.MockTransport(self._handle))
        self.provider = DoubaoReleaseProvider(self.client)

    async def asyncTearDown(self):
        await self.client.aclose()

    async def _handle(self, request):
        self.requests.append(request)
        if request.method == "HEAD":
            if self.head_failure:
                raise self.head_failure
            return httpx2.Response(self.head_status, headers=self.head_headers, request=request)
        self.assertEqual(request.method, "POST")
        self.assertEqual(str(request.url), LANDING_CLICK_URL)
        self.assertEqual(request.headers["content-type"], "application/json")
        self.assertEqual(request.headers["origin"], "https://www.doubao.com")
        self.assertEqual(request.headers["referer"], "https://www.doubao.com/")
        if self.failure:
            raise self.failure
        if self.raw_body is not None:
            return httpx2.Response(self.status, content=self.raw_body, request=request)
        return httpx2.Response(self.status, json=self.payload, request=request)

    async def test_landing_click_response_yields_the_online_installer(self):
        releases = await self.provider.get_releases()
        self.assertEqual(self.provider.id, "doubao")
        self.assertEqual(self.provider.display_name, "豆包")
        self.assertEqual(len(releases), 1)
        release = releases[0]
        self.assertEqual(release.id, f"{DOUBAO_BUILD}-windows-x64")
        self.assertEqual(release.version, DOUBAO_BUILD)
        self.assertEqual(release.architecture, "x64")
        self.assertEqual(release.file_name, DOUBAO_FILE)
        self.assertEqual(release.file_name, str(release.download_url).split("?", 1)[0].rsplit("/", 1)[-1])
        self.assertEqual(release.size_bytes, DOUBAO_SIZE)
        self.assertIsNone(release.sha256)
        self.assertEqual(self.requests[1].method, "HEAD")

    async def test_landing_click_request_matches_the_official_page_payload(self):
        await self.provider.get_releases()
        body = self.requests[0].content.decode("utf-8")
        self.assertEqual(
            body,
            '{"type":1,"content":"{\\"web_url\\":\\"https%3A%2F%2Fwww.doubao.com%2Fdownload%2Fdesktop\\",'
            f'\\"surl_token\\":\\"{WIN_X64_SURL_TOKEN}\\"}}",' + '"os":"windows"}',
        )
        self.assertIn("https%3A%2F%2Fwww.doubao.com%2Fdownload%2Fdesktop", body)
        self.assertNotIn("+", body)

    async def test_rejected_landing_token_is_an_unavailable_provider(self):
        self.payload = {"ret": -1, "errmsg": "GetTokenConfig err", "log_id": "x", "data": None}
        with self.assertRaises(ProviderUnavailableError) as context:
            await self.provider.get_releases()
        self.assertIn("GetTokenConfig err", str(context.exception))
        self.payload = {"ret": "0", "data": {"download_url": DOUBAO_DOWNLOAD_URL}}
        with self.assertRaises(ProviderUnavailableError):
            await self.provider.get_releases(force_refresh=True)

    async def test_malformed_api_responses_are_protocol_errors(self):
        for payload in (["not", "an", "object"], {"ret": 0, "data": None}, {"ret": 0, "data": []}):
            self.payload = payload
            with self.subTest(payload=payload), self.assertRaises(ProviderProtocolError):
                await self.provider.get_releases(force_refresh=True)
        self.raw_body = b"<html>maintenance</html>"
        with self.assertRaises(ProviderProtocolError):
            await self.provider.get_releases(force_refresh=True)

    async def test_download_url_must_be_the_official_cdn_online_installer(self):
        for url in (
            None,
            "",
            f"http://lf9-apk.ugapk.cn/package/pc/x/{DOUBAO_FILE}",
            f"https://evil.example/package/pc/x/{DOUBAO_FILE}",
            f"https://lf9-apk.ugapk.cn/other/pc/x/{DOUBAO_FILE}",
            "https://lf9-apk.ugapk.cn/package/pc/x/doubao_client_webdaoliu_doudi_arm_installer_win_1_v2035012_2a56_1794554757.exe",
            "https://lf9-apk.ugapk.cn/package/pc/x/doubao_client_online_installer_win_v2035012_59db_1794554757.zip",
            "https://lf9-apk.ugapk.cn/package/pc/x/doubao_client_online_installer_win_latest_59db_1794554757.exe",
        ):
            self.payload = doubao_payload(url=url)
            with self.subTest(url=url), self.assertRaises(ProviderProtocolError):
                await self.provider.get_releases(force_refresh=True)

    async def test_installer_size_is_optional_metadata(self):
        self.head_status = 403
        self.assertIsNone((await self.provider.get_releases())[0].size_bytes)
        self.head_status = 200
        self.head_headers = {}
        self.assertIsNone((await self.provider.get_releases(force_refresh=True))[0].size_bytes)
        self.head_headers = {"content-length": "0"}
        with self.assertRaises(ProviderProtocolError):
            await self.provider.get_releases(force_refresh=True)

    async def test_head_timeout_surfaces_but_head_transport_error_does_not(self):
        self.head_failure = httpx2.ReadTimeout("head timeout")
        with self.assertRaises(ProviderTimeoutError):
            await self.provider.get_releases()
        self.head_failure = httpx2.ConnectError("head transport")
        self.assertIsNone((await self.provider.get_releases(force_refresh=True))[0].size_bytes)

    async def test_landing_click_http_and_timeout_errors_are_distinct(self):
        self.status = 502
        with self.assertRaises(ProviderUnavailableError):
            await self.provider.get_releases()
        self.status = 200
        self.failure = httpx2.ReadTimeout("timeout detail")
        with self.assertRaises(ProviderTimeoutError):
            await self.provider.get_releases(force_refresh=True)
        self.failure = httpx2.ConnectError("transport detail")
        with self.assertRaises(ProviderUnavailableError):
            await self.provider.get_releases(force_refresh=True)

    async def test_errors_do_not_cache_a_failed_lookup(self):
        self.payload = {"ret": -1, "errmsg": "params err", "data": None}
        with self.assertRaises(ProviderUnavailableError):
            await self.provider.get_releases()
        self.payload = doubao_payload()
        self.assertEqual((await self.provider.get_releases())[0].version, DOUBAO_BUILD)

    async def test_resolve_download_checks_release_identity(self):
        release = (await self.provider.get_releases())[0]
        self.assertEqual(await self.provider.resolve_download(release), release)
        for mutation in (
            {"id": "9999999-windows-x64"},
            {"file_name": "doubao.exe"},
            {"download_url": HttpUrl("https://evil.example/doubao.exe")},
            {"size_bytes": 1},
        ):
            with self.subTest(mutation=mutation), self.assertRaises(ProviderProtocolError):
                await self.provider.resolve_download(release.model_copy(update=mutation))
        self.payload = doubao_payload(
            url=f"https://lf9-apk.ugapk.cn/package/pc/9c6c4f1e-8d3a-4b8e-9b8d-0e7a2d4c1f33/"
                f"doubao_client_webdaoliu_doudi_online_installer_win_v2035013_59db_1794554757.exe"
        )
        await self.provider.get_releases(force_refresh=True)
        with self.assertRaises(ProviderReleaseChangedError):
            await self.provider.resolve_download(release)


if __name__ == "__main__":
    unittest.main()
