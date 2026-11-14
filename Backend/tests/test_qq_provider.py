import asyncio
import json
from pathlib import Path
import subprocess
import sys
import traceback
import unittest
from unittest.mock import patch

import httpx2
from pydantic import HttpUrl

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR.parent))

from Backend.models import Release
from Backend.providers import (
    ProviderError,
    ProviderProtocolError,
    ProviderReleaseChangedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    QQReleaseProvider,
    ReleaseProvider,
)
from Backend.providers.qq import OIDB_HEADER


CONFIG_URL = "https://qq-web.cdn-go.cn/im.qq.com_new/latest/rainbow/pcConfig.json"
SIGN_URL = "https://im.qq.com/http2rpc/gotrpc/noauth/trpc.qqntv2.urlsign.UrlSign/GetSign"
NT_DOWNLOAD_FIELDS = {
    "x64": "ntDownloadX64Url",
    "x86": "ntDownloadUrl",
    "arm64": "ntDownloadARMUrl",
}


def configuration(version="9.9.37"):
    windows = {
        "version": version,
        "downloadUrl": "https://dldir1.qq.com/qqfile/qq/PCQQ9.7Legacy.exe",
        "unknown_future_field": {"ignored": True},
    }
    for architecture, field in NT_DOWNLOAD_FIELDS.items():
        windows[field] = (
            f"https://qqdl.gtimg.cn/qqfile/QQNTV2/{version}/release/test/"
            f"QQ_{version}_nt_{architecture}.exe"
        )
    return {"Windows": windows, "Mac": {"version": "unrelated"}, "version": "0.0.0"}


class QQProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.config = configuration()
        self.requests = []
        self.config_status = 200
        self.sign_status = 200
        self.config_exception = None
        self.sign_exception = None
        self.config_raw = None
        self.sign_raw = None
        self.sign_payload = None
        self.config_delay = 0
        self.sign_count = 0
        self.client = httpx2.AsyncClient(transport=httpx2.MockTransport(self._handle_request))
        self.provider = QQReleaseProvider(self.client)

    async def asyncTearDown(self):
        await self.client.aclose()

    async def _handle_request(self, request):
        self.requests.append(request)
        if str(request.url) == CONFIG_URL:
            if self.config_delay:
                await asyncio.sleep(self.config_delay)
            if self.config_exception is not None:
                raise self.config_exception
            if self.config_raw is not None:
                return httpx2.Response(self.config_status, content=self.config_raw, request=request)
            return httpx2.Response(self.config_status, json=self.config, request=request)
        if str(request.url) == SIGN_URL:
            self.sign_count += 1
            if self.sign_exception is not None:
                raise self.sign_exception
            if self.sign_raw is not None:
                return httpx2.Response(self.sign_status, content=self.sign_raw, request=request)
            payload = self.sign_payload
            if payload is None:
                unsigned_url = json.loads(request.content)["url"]
                payload = {
                    "retcode": 0,
                    "error": {"code": 0},
                    "data": {"url": f"{unsigned_url}?sign=token-{self.sign_count}"},
                }
            return httpx2.Response(self.sign_status, json=payload, request=request)
        raise AssertionError(f"Unexpected provider endpoint: {request.url.host}{request.url.path}")

    def config_request_count(self):
        return sum(str(request.url) == CONFIG_URL for request in self.requests)

    async def test_real_windows_dictionary_and_three_nt_architectures(self):
        self.assertIsInstance(self.provider, ReleaseProvider)
        self.assertEqual(self.provider.id, "qq")
        releases = await self.provider.get_releases()
        self.assertEqual([release.architecture for release in releases], ["x64", "x86", "arm64"])
        for release in releases:
            with self.subTest(architecture=release.architecture):
                self.assertIsInstance(release, Release)
                self.assertEqual(release.id, f"9.9.37-windows-{release.architecture}")
                self.assertEqual(release.version, "9.9.37")
                self.assertEqual(release.platform, "windows")
                self.assertEqual(release.file_name, f"QQ_9.9.37_nt_{release.architecture}.exe")
                self.assertEqual(str(release.download_url), self.config["Windows"][NT_DOWNLOAD_FIELDS[release.architecture]])
                self.assertIsNone(release.size_bytes)
                self.assertIsNone(release.sha256)
                self.assertNotIn("Legacy", str(release.download_url))
        self.assertEqual(self.requests[0].method, "GET")

    async def test_missing_architecture_is_skipped(self):
        del self.config["Windows"]["ntDownloadX64Url"]
        self.config["Windows"]["ntDownloadARMUrl"] = None
        releases = await self.provider.get_releases()
        self.assertEqual([release.architecture for release in releases], ["x86"])

    async def test_legacy_download_url_is_never_used_as_a_fallback(self):
        for field in NT_DOWNLOAD_FIELDS.values():
            self.config["Windows"].pop(field)
        with self.assertRaises(ProviderProtocolError):
            await self.provider.get_releases()
        self.assertEqual(self.sign_count, 0)

    async def test_unknown_fields_do_not_change_metadata(self):
        self.config["Windows"]["someNewDownloadField"] = "https://example.test/unrelated.exe"
        self.config["FuturePlatform"] = ["anything"]
        releases = await self.provider.get_releases()
        self.assertEqual(len(releases), 3)
        self.assertTrue(all(release.version == "9.9.37" for release in releases))

    async def test_windows_and_windows_version_are_required(self):
        cases = [
            {},
            {"Windows": []},
            {"windows": configuration()["Windows"]},
            {"Windows": {"ntDownloadX64Url": self.config["Windows"]["ntDownloadX64Url"]}, "version": "9.9.37"},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                self.config = payload
                with self.assertRaises(ProviderProtocolError):
                    await self.provider.get_releases(force_refresh=True)

    async def test_invalid_windows_versions_are_rejected(self):
        for version in (None, 9, False, "", "latest", "9.9.37-beta", "9" * 51, "９.９.３７"):
            with self.subTest(version=version):
                self.config = configuration()
                self.config["Windows"]["version"] = version
                with self.assertRaises(ProviderProtocolError):
                    await self.provider.get_releases(force_refresh=True)

    async def test_cache_and_force_refresh_use_updated_version(self):
        first = await self.provider.get_releases()
        self.config = configuration("9.9.38")
        cached = await self.provider.get_releases()
        refreshed = await self.provider.get_releases(force_refresh=True)
        self.assertEqual(first[0].version, "9.9.37")
        self.assertEqual(cached[0].version, "9.9.37")
        self.assertEqual(refreshed[0].version, "9.9.38")
        self.assertEqual(refreshed[0].id, "9.9.38-windows-x64")
        self.assertIn("9.9.38", str(refreshed[0].download_url))
        self.assertEqual(self.config_request_count(), 2)

    async def test_expired_cache_refreshes_metadata(self):
        with patch("Backend.providers.qq.monotonic", return_value=100):
            await self.provider.get_releases()
        self.config = configuration("9.9.38")
        with patch("Backend.providers.qq.monotonic", return_value=159):
            self.assertEqual((await self.provider.get_releases())[0].version, "9.9.37")
        with patch("Backend.providers.qq.monotonic", return_value=160):
            self.assertEqual((await self.provider.get_releases())[0].version, "9.9.38")
        self.assertEqual(self.config_request_count(), 2)

    async def test_ttl_expiry_and_version_update_raise_release_changed(self):
        with patch("Backend.providers.qq.monotonic", return_value=0):
            await self.provider.get_releases()
        with patch("Backend.providers.qq.monotonic", return_value=59):
            selected_release = (await self.provider.get_releases())[0]
        self.config = configuration("9.9.38")
        with patch("Backend.providers.qq.monotonic", return_value=61):
            with self.assertRaises(ProviderReleaseChangedError) as context:
                await self.provider.resolve_download(selected_release)
            current_release = (await self.provider.get_releases())[0]
            resolved = await self.provider.resolve_download(current_release)
        self.assertIsInstance(context.exception, ProviderProtocolError)
        self.assertEqual(selected_release.id, "9.9.37-windows-x64")
        self.assertEqual(resolved.id, "9.9.38-windows-x64")
        self.assertEqual(self.config_request_count(), 2)
        self.assertEqual(self.sign_count, 1)

    async def test_same_id_metadata_tampering_is_not_release_changed(self):
        release = (await self.provider.get_releases())[0]
        for mutation in (
            {"download_url": HttpUrl("https://evil.example/QQ_9.9.37_nt_x64.exe")},
            {"version": "9.9.38"},
            {"file_name": "different.exe"},
            {"size_bytes": 123},
        ):
            with self.subTest(fields=list(mutation)):
                with self.assertRaises(ProviderProtocolError) as context:
                    await self.provider.resolve_download(release.model_copy(update=mutation))
                self.assertNotIsInstance(context.exception, ProviderReleaseChangedError)
        self.assertEqual(self.sign_count, 0)

    async def test_concurrent_metadata_requests_share_one_successful_fetch(self):
        self.config_delay = 0.02
        results = await asyncio.gather(*(self.provider.get_releases() for _ in range(8)))
        self.assertEqual(self.config_request_count(), 1)
        self.assertTrue(all(result[0].version == "9.9.37" for result in results))
        self.assertIsNot(results[0][0], results[1][0])

    async def test_returned_metadata_cannot_mutate_the_cache(self):
        first = await self.provider.get_releases()
        first[0].file_name = "tampered.exe"
        first[0].download_url = HttpUrl("https://example.test/tampered.exe")
        second = await self.provider.get_releases()
        self.assertEqual(second[0].file_name, "QQ_9.9.37_nt_x64.exe")
        self.assertEqual(str(second[0].download_url), self.config["Windows"]["ntDownloadX64Url"])
        self.assertEqual(self.config_request_count(), 1)

    async def test_failed_metadata_fetch_is_not_cached(self):
        self.config_status = 503
        with self.assertRaises(ProviderUnavailableError):
            await self.provider.get_releases()
        self.config_status = 200
        self.assertEqual(len(await self.provider.get_releases()), 3)
        self.assertEqual(self.config_request_count(), 2)

    async def test_invalid_urls_are_rejected(self):
        cases = [
            123,
            "not-a-url",
            "http://qqdl.gtimg.cn/setup.exe",
            "ftp://qqdl.gtimg.cn/setup.exe",
            "https:///setup.exe",
            "https://user:password@qqdl.gtimg.cn/setup.exe",
            "https://qqdl.gtimg.cn:invalid/setup.exe",
            "https://qqdl.gtimg.cn/setup.exe#fragment",
            "https://qqdl.gtimg.cn/%ZZ.exe",
            "https://qqdl.gtimg.cn/%FF.exe",
        ]
        for url in cases:
            with self.subTest(url=url):
                self.config = configuration()
                self.config["Windows"]["ntDownloadX64Url"] = url
                with self.assertRaises(ProviderProtocolError):
                    await self.provider.get_releases(force_refresh=True)

    async def test_unsafe_or_non_executable_file_names_are_rejected(self):
        cases = ["setup.html", "CON.exe", "COM1.exe", "..%2Fsetup.exe", "QQ%5Csetup.exe", "QQ%00.exe", "QQ.exe%20", "%20QQ.exe"]
        for file_name in cases:
            with self.subTest(file_name=file_name):
                self.config = configuration()
                self.config["Windows"]["ntDownloadX64Url"] = f"https://qqdl.gtimg.cn/{file_name}"
                with self.assertRaises(ProviderProtocolError):
                    await self.provider.get_releases(force_refresh=True)

    async def test_url_path_file_name_is_decoded(self):
        self.config["Windows"]["ntDownloadX64Url"] = "https://qqdl.gtimg.cn/QQ_9.9.37%20x64.exe"
        releases = await self.provider.get_releases()
        self.assertEqual(releases[0].file_name, "QQ_9.9.37 x64.exe")

    async def test_sign_request_has_exact_header_and_unsigned_url_body(self):
        release = (await self.provider.get_releases())[0]
        resolved = await self.provider.resolve_download(release)
        request = self.requests[-1]
        self.assertEqual(str(request.url), SIGN_URL)
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.headers["x-oidb"], '{"uint32_command":"0x9b8e","uint32_service_type":1}')
        self.assertEqual(request.headers["x-oidb"], OIDB_HEADER)
        self.assertEqual(request.headers["content-type"], "application/json")
        self.assertEqual(json.loads(request.content), {"url": str(release.download_url)})
        self.assertEqual(resolved.id, release.id)
        self.assertEqual(resolved.file_name, release.file_name)
        self.assertEqual(str(resolved.download_url), f"{release.download_url}?sign=token-1")

    async def test_each_resolution_signs_again_and_never_caches_signed_urls(self):
        release = (await self.provider.get_releases())[0]
        original_url = str(release.download_url)
        first = await self.provider.resolve_download(release)
        second = await self.provider.resolve_download(release)
        self.assertNotEqual(first.download_url, second.download_url)
        self.assertTrue(str(first.download_url).endswith("sign=token-1"))
        self.assertTrue(str(second.download_url).endswith("sign=token-2"))
        self.assertEqual(self.sign_count, 2)
        self.assertEqual(self.config_request_count(), 1)
        self.assertEqual(str((await self.provider.get_releases())[0].download_url), original_url)
        self.assertEqual(str(release.download_url), original_url)

    async def test_each_nt_architecture_can_be_resolved(self):
        releases = await self.provider.get_releases()
        for release in releases:
            resolved = await self.provider.resolve_download(release)
            self.assertEqual(resolved.architecture, release.architecture)
            self.assertEqual(resolved.file_name, release.file_name)
        self.assertEqual(self.sign_count, 3)

    async def test_caller_cannot_ask_to_sign_an_unrelated_address_or_metadata(self):
        release = (await self.provider.get_releases())[0]
        mutations = [
            {"download_url": HttpUrl("https://evil.example/QQ_9.9.37_nt_x64.exe")},
            {"download_url": HttpUrl(f"{release.download_url}?caller=modified")},
            {"id": "9.9.38-windows-x64"},
            {"file_name": "other.exe"},
            {"version": "9.9.38"},
            {"size_bytes": 123},
        ]
        for mutation in mutations:
            with self.subTest(fields=list(mutation)):
                with self.assertRaises(ProviderProtocolError):
                    await self.provider.resolve_download(release.model_copy(update=mutation))
        with self.assertRaises(ProviderProtocolError):
            await self.provider.resolve_download({"id": release.id})
        self.assertEqual(self.sign_count, 0)

    async def test_nonzero_or_non_integer_retcode_is_rejected(self):
        release = (await self.provider.get_releases())[0]
        for code in (1, -1, "0", False, None):
            with self.subTest(code=code):
                self.sign_payload = {"retcode": code, "error": {"code": 0}, "data": {"url": f"{release.download_url}?sign=token"}}
                with self.assertRaises(ProviderProtocolError):
                    await self.provider.resolve_download(release)

    async def test_nonzero_or_malformed_error_code_is_rejected(self):
        release = (await self.provider.get_releases())[0]
        for error in ({"code": 1}, {"code": -1}, {"code": "0"}, {"code": False}, {"code": None}, {}, None, "error"):
            with self.subTest(error=error):
                self.sign_payload = {"retcode": 0, "error": error, "data": {"url": f"{release.download_url}?sign=token"}}
                with self.assertRaises(ProviderProtocolError):
                    await self.provider.resolve_download(release)

    async def test_both_trpc_success_envelopes_are_supported(self):
        release = (await self.provider.get_releases())[0]
        for success in ({"retcode": 0}, {"error": {"code": 0}}, {"retcode": 0, "error": {"code": 0}}):
            with self.subTest(success=success):
                self.sign_payload = {**success, "data": {"url": f"{release.download_url}?sign=token"}}
                self.assertEqual((await self.provider.resolve_download(release)).id, release.id)

    async def test_missing_success_code_or_download_data_is_rejected(self):
        release = (await self.provider.get_releases())[0]
        cases = [
            {"data": {"url": f"{release.download_url}?sign=token"}},
            {"retcode": 0},
            {"retcode": 0, "data": None},
            {"retcode": 0, "data": []},
            {"retcode": 0, "data": {}},
            {"retcode": 0, "data": {"url": ""}},
            {"retcode": 0, "data": {"url": 123}},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                self.sign_payload = payload
                with self.assertRaises(ProviderProtocolError):
                    await self.provider.resolve_download(release)

    async def test_signed_urls_must_preserve_the_original_host_path_and_file(self):
        release = (await self.provider.get_releases())[0]
        cases = [
            "not-a-url",
            str(release.download_url).replace("https://", "http://") + "?sign=token",
            str(release.download_url).replace("qqdl.gtimg.cn", "evil.example") + "?sign=token",
            str(release.download_url).replace("/release/test/", "/other/") + "?sign=token",
            str(release.download_url).replace(release.file_name, "different.exe") + "?sign=token",
            str(release.download_url).replace("qqdl.gtimg.cn", "user:password@qqdl.gtimg.cn") + "?sign=token",
            str(release.download_url) + "#sign=token",
        ]
        for url in cases:
            with self.subTest(url=url):
                self.sign_payload = {"retcode": 0, "error": {"code": 0}, "data": {"url": url}}
                with self.assertRaises(ProviderProtocolError):
                    await self.provider.resolve_download(release)

    async def test_unsigned_empty_or_duplicate_sign_parameters_are_rejected(self):
        release = (await self.provider.get_releases())[0]
        query_strings = [
            "",
            "?t=uninterpreted",
            "?sign",
            "?sign=",
            "?sign=&t=uninterpreted",
            "?sign=one&sign=two",
            "?sign=&sign=two",
            "?sign=one&s%69gn=two",
        ]
        for query in query_strings:
            with self.subTest(query=query):
                self.sign_payload = {
                    "retcode": 0,
                    "error": {"code": 0},
                    "data": {"url": f"{release.download_url}{query}"},
                }
                with self.assertRaises(ProviderProtocolError):
                    await self.provider.resolve_download(release)

    async def test_opaque_sign_and_other_query_parameters_are_preserved(self):
        release = (await self.provider.get_releases())[0]
        signed_url = f"{release.download_url}?other=a%2Fb&t=not-an-expiry&sign=opaque-not-hex_%2f+%3D=="
        self.sign_payload = {"retcode": 0, "error": {"code": 0}, "data": {"url": signed_url}}
        resolved = await self.provider.resolve_download(release)
        self.assertEqual(str(resolved.download_url), signed_url)
        self.assertEqual(resolved.file_name, release.file_name)
        self.assertEqual(str((await self.provider.get_releases())[0].download_url), str(release.download_url))

    async def test_http_errors_are_provider_errors(self):
        self.config_status = 502
        with self.assertRaises(ProviderUnavailableError) as context:
            await self.provider.get_releases()
        self.assertIsInstance(context.exception, ProviderError)
        self.config_status = 200
        release = (await self.provider.get_releases())[0]
        self.sign_status = 403
        with self.assertRaises(ProviderUnavailableError):
            await self.provider.resolve_download(release)

    async def test_timeout_errors_are_distinct_and_do_not_expose_upstream_text(self):
        token = "SENSITIVE_SIGN_TOKEN"
        self.config_exception = httpx2.ReadTimeout(token)
        with self.assertRaises(ProviderTimeoutError) as context:
            await self.provider.get_releases()
        self.assertNotIn(token, "".join(traceback.format_exception(context.exception)))
        self.config_exception = None
        release = (await self.provider.get_releases())[0]
        self.sign_exception = httpx2.ReadTimeout(token)
        with self.assertRaises(ProviderTimeoutError) as context:
            await self.provider.resolve_download(release)
        self.assertNotIn(token, "".join(traceback.format_exception(context.exception)))

    async def test_transport_errors_are_provider_errors(self):
        self.config_exception = httpx2.ConnectError("upstream connection failed")
        with self.assertRaises(ProviderUnavailableError):
            await self.provider.get_releases()
        self.config_exception = None
        release = (await self.provider.get_releases())[0]
        self.sign_exception = httpx2.ConnectError("upstream connection failed")
        with self.assertRaises(ProviderUnavailableError):
            await self.provider.resolve_download(release)

    async def test_invalid_json_or_non_object_responses_are_protocol_errors(self):
        for body in (b"{", b"<html>error</html>", b"[]", b"null"):
            with self.subTest(operation="config", body=body):
                self.config_raw = body
                with self.assertRaises(ProviderProtocolError):
                    await self.provider.get_releases(force_refresh=True)
        self.config_raw = None
        release = (await self.provider.get_releases())[0]
        for body in (b"{", b"<html>error</html>", b"[]", b"null"):
            with self.subTest(operation="sign", body=body):
                self.sign_raw = body
                with self.assertRaises(ProviderProtocolError):
                    await self.provider.resolve_download(release)

    async def test_protocol_errors_do_not_expose_signed_tokens(self):
        release = (await self.provider.get_releases())[0]
        token = "SENSITIVE_SIGN_TOKEN"
        self.sign_payload = {
            "retcode": 0,
            "error": {"code": 0},
            "data": {"url": f"https://evil.example/{release.file_name}?sign={token}"},
        }
        with self.assertRaises(ProviderProtocolError) as context:
            await self.provider.resolve_download(release)
        self.assertNotIn(token, "".join(traceback.format_exception(context.exception)))
        self.sign_payload = {"retcode": 1, "error": {"code": 1, "message": token}}
        with self.assertRaises(ProviderProtocolError) as context:
            await self.provider.resolve_download(release)
        self.assertNotIn(token, "".join(traceback.format_exception(context.exception)))

    async def test_cache_ttl_requires_a_valid_duration(self):
        for ttl in (-1, float("nan"), float("inf")):
            with self.subTest(ttl=ttl):
                with self.assertRaises(ValueError):
                    QQReleaseProvider(self.client, cache_ttl=ttl)


class QQProviderImportTests(unittest.TestCase):
    def test_direct_backend_script_import_path(self):
        script = (
            "from providers import QQReleaseProvider; "
            "from providers.qq import Release as ProviderRelease; "
            "from models import Release; "
            "assert ProviderRelease is Release; assert QQReleaseProvider.id == 'qq'"
        )
        result = subprocess.run([sys.executable, "-c", script], cwd=BACKEND_DIR, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
