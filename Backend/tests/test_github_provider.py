"""Generic GitHub Releases provider tests: parsing, auth header, error mapping, validation."""

from hashlib import sha256
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

import httpx2

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR.parent))

from Backend.models import GithubProviderSpec, Release
from Backend.providers._github_cache import GithubResponseCache
from Backend.providers.base import (
    ProviderProtocolError,
    ProviderReleaseChangedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from Backend.providers.github import GithubReleaseProvider

SPEC = GithubProviderSpec.model_validate({
    "repo": "example/tool",
    "tag_prefix": "v",
    "assets": [
        {"pattern": "tool-{version}-win64\\.exe", "architecture": "x64"},
        {"pattern": "tool-{version}-win32\\.exe", "architecture": "x86"},
    ],
})
TOKEN = "ghp_" + "a" * 36


def release_payload(version="1.2.3", tag="v1.2.3", **overrides):
    payload = {
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "assets": [
            {
                "name": f"tool-{version}-win64.exe",
                "state": "uploaded",
                "browser_download_url": f"https://github.com/example/tool/releases/download/{tag}/tool-{version}-win64.exe",
                "size": 1000,
                "digest": "sha256:" + "a" * 64,
            },
            {
                "name": f"tool-{version}-win32.exe",
                "state": "uploaded",
                "browser_download_url": f"https://github.com/example/tool/releases/download/{tag}/tool-{version}-win32.exe",
                "size": 900,
            },
        ],
    }
    payload.update(overrides)
    return payload


class RecordingTransport:
    def __init__(self, payload=None, status=200, headers=None, raw=None):
        self.payload = payload if payload is not None else release_payload()
        self.status = status
        self.headers = headers or {}
        self.raw = raw
        self.requests = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        content = self.raw if self.raw is not None else json.dumps(self.payload).encode()
        return httpx2.Response(self.status, content=content, headers=self.headers)


class GithubProviderTests(unittest.IsolatedAsyncioTestCase):
    def provider(self, transport, *, token=""):
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        self.addAsyncCleanup(client.aclose)
        return GithubReleaseProvider(client, SPEC, provider_id="exampletool", display_name="示例工具", token=token)

    async def test_parses_releases_and_sends_the_token_only_when_configured(self):
        with_token = RecordingTransport()
        provider = self.provider(with_token, token=TOKEN)
        releases = await provider.get_releases()

        self.assertEqual([release.id for release in releases], ["1.2.3-windows-x64", "1.2.3-windows-x86"])
        self.assertEqual(releases[0].version, "1.2.3")
        self.assertEqual(releases[0].file_name, "tool-1.2.3-win64.exe")
        self.assertEqual(releases[0].size_bytes, 1000)
        self.assertEqual(releases[0].sha256, "a" * 64)
        self.assertIsNone(releases[1].sha256)

        request = with_token.requests[0]
        self.assertEqual(str(request.url), "https://api.github.com/repos/example/tool/releases/latest")
        self.assertEqual(request.headers["authorization"], f"Bearer {TOKEN}")
        self.assertEqual(request.headers["accept"], "application/vnd.github+json")

        anonymous = RecordingTransport()
        await self.provider(anonymous).get_releases()
        self.assertNotIn("authorization", anonymous.requests[0].headers)

    async def test_error_statuses_map_to_provider_errors(self):
        cases = (
            (401, {}, ProviderUnavailableError, "令牌"),
            (403, {"x-ratelimit-remaining": "0"}, ProviderUnavailableError, "额度"),
            (403, {}, ProviderUnavailableError, "拒绝"),
            (429, {}, ProviderUnavailableError, "额度"),
            (404, {}, ProviderUnavailableError, "还没有正式发布"),
            (500, {}, ProviderUnavailableError, "HTTP 500"),
        )
        for status, headers, expected, fragment in cases:
            with self.subTest(status=status, headers=headers):
                transport = RecordingTransport(status=status, headers=headers)
                with self.assertRaises(expected) as context:
                    await self.provider(transport).get_releases()
                self.assertIn(fragment, str(context.exception))

    async def test_token_never_appears_in_error_messages(self):
        transport = RecordingTransport(status=401)
        with self.assertRaises(ProviderUnavailableError) as context:
            await self.provider(transport, token=TOKEN).get_releases()
        self.assertNotIn(TOKEN, str(context.exception))

    async def test_timeout_and_transport_errors_are_typed(self):
        def timeout_handler(request):
            raise httpx2.ReadTimeout("slow", request=request)

        client = httpx2.AsyncClient(transport=httpx2.MockTransport(timeout_handler))
        self.addAsyncCleanup(client.aclose)
        provider = GithubReleaseProvider(client, SPEC, provider_id="exampletool")
        with self.assertRaises(ProviderTimeoutError):
            await provider.get_releases()

    async def test_protocol_errors_are_reported(self):
        cases = {
            "missing asset": release_payload(assets=[]),
            "duplicate asset": release_payload(assets=release_payload()["assets"] * 2),
            "draft": release_payload(draft=True),
            "prerelease": release_payload(prerelease=True),
            "no tag": release_payload(tag_name=None),
            "bad size": release_payload(assets=[{**release_payload()["assets"][0], "size": -5}]),
            "bad digest": release_payload(assets=[{**release_payload()["assets"][0], "digest": "sha256:xyz"}]),
            "foreign host": release_payload(assets=[
                {**release_payload()["assets"][0], "browser_download_url": "https://evil.example/tool.exe"},
            ]),
        }
        for label, payload in cases.items():
            with self.subTest(case=label):
                transport = RecordingTransport(payload=payload)
                with self.assertRaises(ProviderProtocolError):
                    await self.provider(transport).get_releases()

    async def test_invalid_json_is_a_protocol_error(self):
        transport = RecordingTransport(raw=b"{not json")
        with self.assertRaises(ProviderProtocolError):
            await self.provider(transport).get_releases()

    async def test_resolve_download_checks_the_release_identity(self):
        provider = self.provider(RecordingTransport())
        releases = await provider.get_releases()
        resolved = await provider.resolve_download(releases[0])
        self.assertEqual(resolved.model_dump(), releases[0].model_dump())
        self.assertIsNot(resolved, releases[0])

        wrong_architecture = releases[0].model_copy(update={"id": "1.2.3-windows-arm64x"})
        with self.assertRaises(ProviderReleaseChangedError):
            await provider.resolve_download(wrong_architecture)

        foreign = Release.model_validate({
            **releases[0].model_dump(),
            "download_url": "https://mirror.example/tool.exe",
        })
        with self.assertRaises(ProviderProtocolError):
            await provider.resolve_download(foreign)

    async def test_repository_casing_does_not_break_download_urls(self):
        """GitHub 用 canonical 大小写（VSCodium/vscodium）拼下载路径，目录里的写法可以不同。"""

        spec = GithubProviderSpec.model_validate({
            "repo": "Example/Tool",
            "tag_prefix": "v",
            "assets": [
                {"pattern": "tool-{version}-win64\\.exe", "architecture": "x64"},
                {"pattern": "tool-{version}-win32\\.exe", "architecture": "x86"},
            ],
        })
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(RecordingTransport()))
        self.addAsyncCleanup(client.aclose)
        provider = GithubReleaseProvider(client, spec, provider_id="exampletool")
        releases = await provider.get_releases()
        self.assertEqual([release.architecture for release in releases], ["x64", "x86"])

    async def test_invalid_constructor_arguments_are_rejected(self):
        with self.assertRaises(ValueError):
            GithubReleaseProvider(httpx2.AsyncClient(), SPEC, provider_id="")
        with self.assertRaises(ValueError):
            GithubReleaseProvider(httpx2.AsyncClient(), SPEC, provider_id="x", timeout_seconds=0)
        with self.assertRaises(ValueError):
            GithubReleaseProvider(httpx2.AsyncClient(), SPEC, provider_id="x", refresh_interval_seconds=-1)
        with self.assertRaises(ValueError):
            GithubReleaseProvider(httpx2.AsyncClient(), SPEC, provider_id="x", refresh_interval_seconds=float("inf"))
        with self.assertRaises(ValueError):
            GithubReleaseProvider(httpx2.AsyncClient(), SPEC, provider_id="x", response_cache=object())


class MutableClock:
    """可手动推进的时钟，用来断言间隔门控与 304 后的 ``fetched_at`` 刷新。"""

    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class ConditionalTransport:
    """带校验头的模拟上游：条件命中时返回 304，并记录每一个请求。"""

    def __init__(
        self,
        payload=None,
        *,
        etag='"etag-1"',
        last_modified="Wed, 21 Oct 2015 07:28:00 GMT",
        status=200,
        headers=None,
    ):
        self.payload = payload if payload is not None else release_payload()
        self.etag = etag
        self.last_modified = last_modified
        self.status = status
        self.headers = headers or {}
        self.requests = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx2.Response(self.status, headers=self.headers)
        headers = dict(self.headers)
        if self.etag:
            headers["ETag"] = self.etag
        if self.last_modified:
            headers["Last-Modified"] = self.last_modified
        if self.etag and request.headers.get("if-none-match") == self.etag:
            return httpx2.Response(304, headers=headers)
        return httpx2.Response(200, content=json.dumps(self.payload).encode("utf-8"), headers=headers)


class GithubConditionalRequestTests(unittest.IsolatedAsyncioTestCase):
    """磁盘响应缓存 + 条件请求 + 启动间隔门控。"""

    def temp_directory(self) -> Path:
        directory = tempfile.TemporaryDirectory(prefix="wam-github-provider-")
        self.addCleanup(directory.cleanup)
        return Path(directory.name)

    def cache(self, directory, *, clock=None) -> GithubResponseCache:
        return GithubResponseCache(directory, **({} if clock is None else {"clock": clock}))

    def provider(self, transport, *, cache=None, refresh_interval_seconds=0.0, token=""):
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
        self.addAsyncCleanup(client.aclose)
        return GithubReleaseProvider(
            client,
            SPEC,
            provider_id="exampletool",
            display_name="示例工具",
            token=token,
            response_cache=cache,
            refresh_interval_seconds=refresh_interval_seconds,
        )

    async def test_second_request_is_conditional_and_a_304_reuses_the_cached_payload(self):
        directory = self.temp_directory()
        clock = MutableClock(1000.0)
        cache = self.cache(directory, clock=clock)
        transport = ConditionalTransport()
        provider = self.provider(transport, cache=cache)

        first = await provider.get_releases(force_refresh=True)
        self.assertEqual(len(transport.requests), 1)
        self.assertNotIn("if-none-match", transport.requests[0].headers)
        self.assertNotIn("if-modified-since", transport.requests[0].headers)
        cached = await cache.load("example/tool")
        self.assertEqual(cached.etag, '"etag-1"')
        self.assertEqual(cached.last_modified, "Wed, 21 Oct 2015 07:28:00 GMT")
        self.assertEqual(cached.fetched_at, 1000.0)

        clock.now = 5000.0
        second = await provider.get_releases(force_refresh=True)
        self.assertEqual(len(transport.requests), 2)
        conditional = transport.requests[1]
        self.assertEqual(conditional.headers["if-none-match"], '"etag-1"')
        self.assertEqual(conditional.headers["if-modified-since"], "Wed, 21 Oct 2015 07:28:00 GMT")
        self.assertEqual([release.model_dump() for release in second], [release.model_dump() for release in first])
        # 304 后 fetched_at 被刷新，间隔门控从本次探测重新计时。
        self.assertEqual((await cache.load("example/tool")).fetched_at, 5000.0)

    async def test_refresh_interval_reuses_the_disk_cache_without_any_request(self):
        directory = self.temp_directory()
        cache = self.cache(directory)
        transport = ConditionalTransport()
        provider = self.provider(transport, cache=cache, refresh_interval_seconds=3600.0)

        first = await provider.get_releases(force_refresh=True)
        self.assertEqual(len(transport.requests), 1)
        second = await provider.get_releases(force_refresh=True)
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual([release.model_dump() for release in second], [release.model_dump() for release in first])
        # 非强制读取同样不发请求。
        await provider.get_releases()
        await provider.get_releases(force_refresh=False)
        self.assertEqual(len(transport.requests), 1)

    async def test_an_expired_interval_falls_back_to_a_conditional_request(self):
        directory = self.temp_directory()
        old_clock = MutableClock(time.time() - 7200.0)
        await self.cache(directory, clock=old_clock).save(
            "example/tool",
            release_payload(),
            etag='"etag-1"',
            last_modified="Wed, 21 Oct 2015 07:28:00 GMT",
        )

        transport = ConditionalTransport()
        provider = self.provider(transport, cache=self.cache(directory), refresh_interval_seconds=3600.0)
        releases = await provider.get_releases(force_refresh=True)
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(transport.requests[0].headers["if-none-match"], '"etag-1"')
        self.assertEqual(releases[0].version, "1.2.3")

    async def test_a_restarted_provider_reuses_the_disk_cache_without_a_request(self):
        directory = self.temp_directory()
        first_transport = ConditionalTransport()
        await self.provider(first_transport, cache=self.cache(directory)).get_releases(force_refresh=True)
        self.assertEqual(len(first_transport.requests), 1)

        second_transport = ConditionalTransport()
        restarted = self.provider(second_transport, cache=self.cache(directory))
        releases = await restarted.get_releases()
        self.assertEqual(second_transport.requests, [])
        self.assertEqual([release.id for release in releases], ["1.2.3-windows-x64", "1.2.3-windows-x86"])

    async def test_interval_gating_also_survives_a_restart(self):
        directory = self.temp_directory()
        first_transport = ConditionalTransport()
        await self.provider(
            first_transport, cache=self.cache(directory), refresh_interval_seconds=3600.0
        ).get_releases(force_refresh=True)
        self.assertEqual(len(first_transport.requests), 1)

        second_transport = ConditionalTransport()
        await self.provider(
            second_transport, cache=self.cache(directory), refresh_interval_seconds=3600.0
        ).get_releases(force_refresh=True)
        self.assertEqual(second_transport.requests, [])

    async def test_corrupt_or_incomplete_cache_files_are_ignored(self):
        directory = self.temp_directory()
        cache = self.cache(directory)
        await cache.save("example/tool", release_payload(), etag='"etag-1"', last_modified=None)
        for path in directory.glob("*.json"):
            path.write_text(json.dumps({"schema_version": 1, "repo": "example/tool"}), encoding="utf-8")

        transport = ConditionalTransport()
        provider = self.provider(transport, cache=self.cache(directory))
        releases = await provider.get_releases()
        self.assertEqual(len(transport.requests), 1)
        self.assertNotIn("if-none-match", transport.requests[0].headers)
        self.assertEqual([release.id for release in releases], ["1.2.3-windows-x64", "1.2.3-windows-x86"])

    async def test_without_a_response_cache_every_call_requests_the_api(self):
        transport = ConditionalTransport()
        provider = self.provider(transport, cache=None)
        await provider.get_releases(force_refresh=True)
        await provider.get_releases(force_refresh=True)
        await provider.get_releases()
        self.assertEqual(len(transport.requests), 3)
        for request in transport.requests:
            self.assertNotIn("if-none-match", request.headers)
            self.assertNotIn("if-modified-since", request.headers)

    async def test_a_304_without_a_cached_payload_is_a_protocol_error(self):
        def always_304(request):
            return httpx2.Response(304)

        provider = self.provider(always_304, cache=self.cache(self.temp_directory()))
        with self.assertRaises(ProviderProtocolError):
            await provider.get_releases(force_refresh=True)

    async def test_the_cache_file_never_contains_the_token_and_keeps_the_payload(self):
        directory = self.temp_directory()
        cache = self.cache(directory)
        transport = ConditionalTransport()
        provider = self.provider(transport, cache=cache, token=TOKEN)
        await provider.get_releases(force_refresh=True)

        self.assertEqual(transport.requests[0].headers["authorization"], f"Bearer {TOKEN}")
        files = list(directory.glob("*.json"))
        self.assertEqual(len(files), 1)
        expected_name = sha256(b"example/tool").hexdigest()[:32] + ".json"
        self.assertEqual(files[0].name, expected_name)
        text = files[0].read_text(encoding="utf-8")
        self.assertNotIn(TOKEN, text)
        document = json.loads(text)
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(document["repo"], "example/tool")
        self.assertEqual(document["payload"], release_payload())

    async def test_rate_limit_and_token_errors_are_reported_without_leaking_the_token(self):
        directory = self.temp_directory()
        cache = self.cache(directory)
        reset_at = str(int(time.time()) + 120)
        transport = ConditionalTransport(
            status=403,
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset_at},
        )
        provider = self.provider(transport, cache=cache, token=TOKEN)
        with self.assertRaises(ProviderUnavailableError) as context:
            await provider.get_releases(force_refresh=True)
        message = str(context.exception)
        self.assertIn("github.token", message)
        self.assertIn("重置", message)
        self.assertNotIn(TOKEN, message)
        self.assertEqual(list(directory.glob("*.json")), [])

        unauthorized = ConditionalTransport(status=401)
        provider = self.provider(unauthorized, cache=self.cache(self.temp_directory()), token=TOKEN)
        with self.assertRaises(ProviderUnavailableError) as context:
            await provider.get_releases(force_refresh=True)
        self.assertIn("令牌", str(context.exception))
        self.assertNotIn(TOKEN, str(context.exception))

    async def test_failed_responses_do_not_overwrite_a_valid_cache(self):
        directory = self.temp_directory()
        cache = self.cache(directory)
        await self.provider(ConditionalTransport(), cache=cache).get_releases(force_refresh=True)
        before = (await cache.load("example/tool")).fetched_at

        failing = ConditionalTransport(status=500)
        provider = self.provider(failing, cache=cache)
        with self.assertRaises(ProviderUnavailableError):
            await provider.get_releases(force_refresh=True)
        self.assertEqual(len(failing.requests), 1)
        self.assertEqual(failing.requests[0].headers["if-none-match"], '"etag-1"')
        self.assertEqual((await cache.load("example/tool")).fetched_at, before)


class GithubSpecValidationTests(unittest.TestCase):
    def test_pattern_without_the_version_placeholder_is_allowed(self):
        spec = GithubProviderSpec.model_validate({
            "repo": "example/tool",
            "assets": [{"pattern": r"tool-[0-9.]+-x64\.exe", "architecture": "x64"}],
        })
        self.assertEqual(spec.assets[0].architecture, "x64")

    def test_invalid_regex_is_rejected(self):
        with self.assertRaises(ValueError):
            GithubProviderSpec.model_validate({
                "repo": "example/tool",
                "assets": [{"pattern": "tool-(unclosed", "architecture": "x64"}],
            })

    def test_repository_must_be_owner_slash_name(self):
        for repo in ("example", "example/tool/extra", "https://github.com/example/tool"):
            with self.subTest(repo=repo), self.assertRaises(ValueError):
                GithubProviderSpec.model_validate({
                    "repo": repo,
                    "assets": [{"pattern": "tool-{version}.exe", "architecture": "x64"}],
                })

    def test_duplicate_architectures_are_rejected(self):
        with self.assertRaises(ValueError):
            GithubProviderSpec.model_validate({
                "repo": "example/tool",
                "assets": [
                    {"pattern": "tool-{version}.exe", "architecture": "x64"},
                    {"pattern": "tool-{version}-alt.exe", "architecture": "x64"},
                ],
            })


if __name__ == "__main__":
    unittest.main()
