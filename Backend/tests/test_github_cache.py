"""``GithubResponseCache``：GitHub 响应（ETag/Last-Modified + 响应体）的磁盘缓存测试。

只用临时目录，不访问网络；损坏、版本不符和字段缺失的文件必须被忽略并删除。
"""

from hashlib import sha256
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR.parent))

from Backend.config import BackendSettings, CacheConfig, RefreshConfig
from Backend.main import create_app
from Backend.providers._github_cache import CachedGithubResponse, GithubResponseCache


REPO = "example/tool"
OTHER_REPO = "example/other"
PAYLOAD = {
    "tag_name": "v1.2.3",
    "draft": False,
    "prerelease": False,
    "assets": [{"name": "tool-1.2.3-win64.exe", "size": 1000}],
}
LAST_MODIFIED = "Wed, 21 Oct 2015 07:28:00 GMT"
DOCUMENT_FIELDS = {"schema_version", "repo", "etag", "last_modified", "fetched_at", "payload"}


class MutableClock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class GithubResponseCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="wam-github-cache-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def cache(self, *, clock=None) -> GithubResponseCache:
        return GithubResponseCache(self.directory, **({} if clock is None else {"clock": clock}))

    def files(self) -> list[Path]:
        return sorted(self.directory.glob("*.json"))

    def write_document(self, document: dict, *, repo: str = REPO) -> Path:
        path = self.directory / f"{sha256(repo.encode('utf-8')).hexdigest()[:32]}.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    async def test_round_trip_persists_the_payload_and_validators(self):
        clock = MutableClock(1000.0)
        await self.cache(clock=clock).save(REPO, PAYLOAD, etag='"etag-1"', last_modified=LAST_MODIFIED)

        # 新实例模拟后端重启：同一个目录里能读回上一次响应。
        cached = await self.cache().load(REPO)
        self.assertIsInstance(cached, CachedGithubResponse)
        self.assertEqual(cached.payload, PAYLOAD)
        self.assertEqual(cached.etag, '"etag-1"')
        self.assertEqual(cached.last_modified, LAST_MODIFIED)
        self.assertEqual(cached.fetched_at, 1000.0)

    async def test_null_validators_round_trip(self):
        await self.cache().save(REPO, PAYLOAD, etag=None, last_modified=None)
        cached = await self.cache().load(REPO)
        self.assertIsNone(cached.etag)
        self.assertIsNone(cached.last_modified)

    async def test_the_file_name_is_the_hashed_repository(self):
        await self.cache().save("../../evil/repo", PAYLOAD, etag=None, last_modified=None)
        files = self.files()
        self.assertEqual(len(files), 1)
        expected = sha256(b"../../evil/repo").hexdigest()[:32] + ".json"
        self.assertEqual(files[0].name, expected)
        self.assertNotIn("evil", files[0].name)
        self.assertEqual(files[0].parent, self.directory.resolve())

    async def test_the_document_uses_the_documented_schema(self):
        clock = MutableClock(1234.5)
        await self.cache(clock=clock).save(REPO, PAYLOAD, etag='"etag-1"', last_modified=LAST_MODIFIED)
        document = json.loads(self.files()[0].read_text(encoding="utf-8"))
        self.assertEqual(set(document), DOCUMENT_FIELDS)
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(document["repo"], REPO)
        self.assertEqual(document["etag"], '"etag-1"')
        self.assertEqual(document["last_modified"], LAST_MODIFIED)
        self.assertEqual(document["fetched_at"], 1234.5)
        self.assertEqual(document["payload"], PAYLOAD)

    async def test_writes_are_atomic_and_leave_no_temporary_files(self):
        cache = self.cache()
        await cache.save(REPO, PAYLOAD, etag="e", last_modified=None)
        self.assertEqual(list(self.directory.glob("*.tmp")), [])
        self.assertEqual(cache.info()["writes"], 1)

    async def test_corrupt_files_are_removed_and_ignored(self):
        await self.cache().save(REPO, PAYLOAD, etag="e", last_modified=None)
        for path in self.files():
            path.write_text("{not json", encoding="utf-8")

        restarted = self.cache()
        self.assertIsNone(await restarted.load(REPO))
        self.assertEqual(self.files(), [])
        self.assertEqual(restarted.info()["entries"], 0)
        self.assertEqual(restarted.info()["errors"], 1)

    async def test_schema_version_mismatch_is_removed_and_ignored(self):
        await self.cache().save(REPO, PAYLOAD, etag="e", last_modified=None)
        for path in self.files():
            document = json.loads(path.read_text(encoding="utf-8"))
            document["schema_version"] = 99
            path.write_text(json.dumps(document), encoding="utf-8")

        restarted = self.cache()
        self.assertIsNone(await restarted.load(REPO))
        self.assertEqual(self.files(), [])
        self.assertEqual(restarted.info()["errors"], 1)

    async def test_missing_fields_are_removed_and_ignored(self):
        path = self.write_document({"schema_version": 1, "repo": REPO, "etag": None, "last_modified": None, "fetched_at": 1.0})
        cache = self.cache()
        self.assertIsNone(await cache.load(REPO))
        self.assertFalse(path.exists())

        path = self.write_document({"schema_version": 1, "repo": REPO, "payload": PAYLOAD, "fetched_at": "soon"})
        self.assertIsNone(await self.cache().load(REPO))
        self.assertFalse(path.exists())

    async def test_a_file_for_another_repository_is_not_reused(self):
        path = self.write_document(
            {
                "schema_version": 1,
                "repo": OTHER_REPO,
                "etag": '"etag-1"',
                "last_modified": None,
                "fetched_at": 1.0,
                "payload": PAYLOAD,
            }
        )
        self.assertIsNone(await self.cache().load(REPO))
        self.assertFalse(path.exists())

    async def test_counters_track_hits_misses_writes_and_entries(self):
        cache = self.cache()
        self.assertEqual(cache.info()["entries"], 0)
        await cache.save(REPO, PAYLOAD, etag="e", last_modified=None)
        await cache.save(OTHER_REPO, PAYLOAD, etag=None, last_modified=None)
        self.assertEqual(cache.info()["writes"], 2)
        self.assertEqual(cache.info()["entries"], 2)

        self.assertIsNotNone(await cache.load(REPO))
        self.assertIsNone(await cache.load("example/missing"))
        info = cache.info()
        self.assertEqual(info["hits"], 1)
        self.assertEqual(info["misses"], 1)
        self.assertEqual(info["errors"], 0)

        restarted = self.cache()
        self.assertEqual(restarted.info()["entries"], 2)

    async def test_write_failures_are_logged_but_never_raised(self):
        blocked = self.directory / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")
        cache = GithubResponseCache(blocked)
        with self.assertLogs("winappsmanager.githubcache", level="WARNING") as captured:
            await cache.save(REPO, PAYLOAD, etag="e", last_modified=None)
        self.assertTrue(any("写入 GitHub 响应缓存失败" in line for line in captured.output))
        self.assertEqual(cache.info()["writes"], 0)
        self.assertEqual(cache.info()["errors"], 1)
        self.assertIsNone(await cache.load(REPO))

    async def test_invalid_arguments_are_rejected(self):
        with self.assertRaises(ValueError):
            GithubResponseCache("")
        with self.assertRaises(ValueError):
            GithubResponseCache(self.directory, clock=None)
        cache = self.cache()
        with self.assertRaises(ValueError):
            await cache.load("")
        with self.assertRaises(ValueError):
            await cache.save("", PAYLOAD, etag=None, last_modified=None)
        with self.assertRaises(ValueError):
            await cache.save(REPO, ["not", "an", "object"], etag=None, last_modified=None)
        with self.assertRaises(ValueError):
            await cache.save(REPO, PAYLOAD, etag=1, last_modified=None)


class _StubProvider:
    """占位 provider：``ReleaseService`` 要求目录里每个 provider 都已注册。"""

    def __init__(self, provider_id: str) -> None:
        self.id = provider_id

    async def get_releases(self, *, force_refresh: bool = False):
        return []

    async def resolve_download(self, release):
        return release


class GithubCacheWiringTests(unittest.TestCase):
    """``main.create_app`` 把缓存目录与间隔门控接到通用 GitHub provider 工厂上。"""

    def create_with_fake_factory(self, settings):
        captured = {}

        def fake_factory(client, direct_client=None, **kwargs):
            captured.update(kwargs)
            catalog = kwargs["catalog"]
            return {
                app.download_provider: _StubProvider(app.download_provider)
                for app in catalog.apps
                if app.download_provider is not None
            }

        with mock.patch("Backend.main.create_release_providers", side_effect=fake_factory):
            app = create_app(settings=settings)
            with TestClient(app) as client:
                self.assertEqual(client.get("/api/v1/health").status_code, 200)
        return captured

    def test_a_persistent_cache_is_created_under_the_cache_directory(self):
        with tempfile.TemporaryDirectory(prefix="wam-github-wiring-") as directory:
            settings = BackendSettings(
                cache=CacheConfig(directory=Path(directory)),
                refresh=RefreshConfig(on_startup=False, interval_seconds=0),
            )
            captured = self.create_with_fake_factory(settings)

        cache = captured["github_response_cache"]
        self.assertIsInstance(cache, GithubResponseCache)
        self.assertEqual(cache.directory, (Path(directory) / "github").resolve())
        self.assertEqual(cache.info()["entries"], 0)
        self.assertEqual(captured["github_refresh_interval_seconds"], 900.0)

    def test_the_configured_interval_is_forwarded(self):
        with tempfile.TemporaryDirectory(prefix="wam-github-wiring-") as directory:
            settings = BackendSettings(
                cache=CacheConfig(directory=Path(directory)),
                github={"refresh_interval_seconds": 120.0},
                refresh=RefreshConfig(on_startup=False, interval_seconds=0),
            )
            captured = self.create_with_fake_factory(settings)
        self.assertEqual(captured["github_refresh_interval_seconds"], 120.0)

    def test_disabled_or_non_persistent_caches_pass_none(self):
        for cache_config in (CacheConfig(enabled=False), CacheConfig(persist=False)):
            with self.subTest(cache=cache_config):
                with tempfile.TemporaryDirectory(prefix="wam-github-wiring-") as directory:
                    cache_config = cache_config.model_copy(update={"directory": Path(directory)})
                    settings = BackendSettings(
                        cache=cache_config,
                        refresh=RefreshConfig(on_startup=False, interval_seconds=0),
                    )
                    captured = self.create_with_fake_factory(settings)
                self.assertIsNone(captured["github_response_cache"])


if __name__ == "__main__":
    unittest.main()
