import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import ValidationError

from Backend.config import BackendSettings, CacheConfig, RefreshConfig
from Backend.main import DATA_FILE, ICON_DIR, create_app
from Backend.models import AppMetadata, Release
from Backend.tests.test_api import release_fixture


class FixtureProvider:
    """Deterministic provider that serves the requested architectures."""

    def __init__(self, provider_id: str, architectures: tuple[str, ...] = ("x64",)):
        self.id = provider_id
        self.architectures = list(architectures)

    async def get_releases(self, *, force_refresh=False):
        return [Release.model_validate(release_fixture(architecture)) for architecture in self.architectures]

    async def resolve_download(self, release):
        return release


def build_catalog(declarations: dict[str, list[str]]) -> Path:
    """复制生产目录里的前几个 app，只保留给定 id 并按声明写入 architectures。"""

    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    apps = []
    for app in data["apps"]:
        if app["id"] not in declarations:
            continue
        apps.append({**app, "architectures": declarations[app["id"]]})
    directory = tempfile.TemporaryDirectory()
    path = Path(directory.name) / "apps.json"
    path.write_text(json.dumps({"schema_version": 1, "apps": apps}, ensure_ascii=False), encoding="utf-8")
    directory.cleanup()  # 文件已写入，目录由对象生命周期外的调用方重新创建
    Path(directory.name).mkdir(parents=True, exist_ok=True)
    return path


class PlatformMetadataTests(unittest.TestCase):
    def test_architectures_default_to_unknown_which_supports_everything(self):
        metadata = AppMetadata.model_validate({
            "id": "demo", "name": "Demo", "description": "d", "long_description": "d",
            "category": "实用工具", "publisher": "p", "website_url": "https://example.com",
            "download_page_url": "https://example.com/download", "icon_text": "D",
            "icon_background": "#0067C0", "installation_notes": "n",
            "features": ["a"], "tags": ["b"],
        })
        self.assertIsNone(metadata.architectures)
        self.assertIsNone(metadata.minimum_windows)
        self.assertTrue(metadata.supports_architecture("x86"))
        self.assertTrue(metadata.supports_architecture("arm64"))

    def test_declared_architectures_filter_the_supported_set(self):
        metadata = AppMetadata.model_validate({
            "id": "demo", "name": "Demo", "description": "d", "long_description": "d",
            "category": "实用工具", "publisher": "p", "website_url": "https://example.com",
            "download_page_url": "https://example.com/download", "icon_text": "D",
            "icon_background": "#0067C0", "installation_notes": "n",
            "features": ["a"], "tags": ["b"],
            "architectures": ["x64", "x86"], "minimum_windows": "7",
        })
        self.assertTrue(metadata.supports_architecture("x64"))
        self.assertTrue(metadata.supports_architecture("x86"))
        self.assertFalse(metadata.supports_architecture("arm64"))
        self.assertEqual(metadata.minimum_windows, "7")

    def test_duplicate_architectures_are_rejected(self):
        with self.assertRaises(ValidationError):
            AppMetadata.model_validate({
                "id": "demo", "name": "Demo", "description": "d", "long_description": "d",
                "category": "实用工具", "publisher": "p", "website_url": "https://example.com",
                "download_page_url": "https://example.com/download", "icon_text": "D",
                "icon_background": "#0067C0", "installation_notes": "n",
                "features": ["a"], "tags": ["b"],
                "architectures": ["x64", "x64"],
            })

    def test_unknown_architecture_and_bad_minimum_windows_are_rejected(self):
        with self.assertRaises(ValidationError):
            AppMetadata.model_validate({
                "id": "demo", "name": "Demo", "description": "d", "long_description": "d",
                "category": "实用工具", "publisher": "p", "website_url": "https://example.com",
                "download_page_url": "https://example.com/download", "icon_text": "D",
                "icon_background": "#0067C0", "installation_notes": "n",
                "features": ["a"], "tags": ["b"],
                "architectures": ["sparc"],
            })
        with self.assertRaises(ValidationError):
            AppMetadata.model_validate({
                "id": "demo", "name": "Demo", "description": "d", "long_description": "d",
                "category": "实用工具", "publisher": "p", "website_url": "https://example.com",
                "download_page_url": "https://example.com/download", "icon_text": "D",
                "icon_background": "#0067C0", "installation_notes": "n",
                "features": ["a"], "tags": ["b"],
                "minimum_windows": "Windows 10",
            })


class ArchitectureFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        first_two = data["apps"][:2]
        directory = tempfile.mkdtemp()
        catalog_path = Path(directory) / "apps.json"
        apps = [
            {**first_two[0], "architectures": ["x64"], "minimum_windows": "10"},
            {**first_two[1], "architectures": ["x64", "x86"], "minimum_windows": "7"},
        ]
        catalog_path.write_text(
            json.dumps({"schema_version": 1, "apps": apps}, ensure_ascii=False), encoding="utf-8"
        )
        empty_directory = Path(directory) / "apps.d"
        cls.providers = {
            first_two[0]["id"]: FixtureProvider(first_two[0]["id"], ("x64", "x86")),
            first_two[1]["id"]: FixtureProvider(first_two[1]["id"], ("x64", "x86")),
        }
        cls.settings = BackendSettings(
            cache=CacheConfig(persist=False),
            refresh=RefreshConfig(on_startup=False, interval_seconds=0),
        )
        cls.client = TestClient(
            create_app(
                data_file=catalog_path,
                icon_directory=ICON_DIR,
                release_providers=cls.providers,
                settings=cls.settings,
                extra_directory=empty_directory,
            )
        )
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def test_list_only_returns_apps_that_support_the_requested_architecture(self):
        x86 = self.client.get("/api/v1/apps", params={"architecture": "x86", "sort": "name"})
        self.assertEqual(x86.status_code, 200)
        self.assertEqual({item["id"] for item in x86.json()["items"]}, {"wechat"})
        self.assertEqual(x86.json()["total"], 1)

        x64 = self.client.get("/api/v1/apps", params={"architecture": "x64", "sort": "name"})
        self.assertEqual({item["id"] for item in x64.json()["items"]}, {"qq", "wechat"})

    def test_response_exposes_machine_readable_compatibility_fields(self):
        payload = self.client.get("/api/v1/apps", params={"architecture": "x64"}).json()
        by_id = {item["id"]: item for item in payload["items"]}
        self.assertEqual(by_id["qq"]["architectures"], ["x64"])
        self.assertEqual(by_id["qq"]["minimum_windows"], "10")
        self.assertEqual(by_id["wechat"]["architectures"], ["x64", "x86"])
        self.assertEqual(by_id["wechat"]["minimum_windows"], "7")

    def test_detail_stays_reachable_for_unsupported_architecture(self):
        response = self.client.get("/api/v1/apps/qq", params={"architecture": "x86"})
        self.assertEqual(response.status_code, 200)
        # 详情不做架构过滤：列表按架构隐藏后，直链仍可打开；能否下载取决于 provider 是否提供该架构安装包。
        release = response.json()["latest_release"]
        self.assertIsNotNone(release)
        self.assertEqual(release["architecture"], "x86")


if __name__ == "__main__":
    unittest.main()
