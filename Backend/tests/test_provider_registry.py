import json
from pathlib import Path
import sys
import unittest

import httpx2
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from Backend.main import DATA_FILE
from Backend.models import Release
from Backend.providers import create_release_providers
from Backend.tests.test_api import create_app


ARCHITECTURES = {
    "qq": ("x64", "x86", "arm64"), "wechat": ("x64", "x86"), "vscode": ("x64", "arm64"),
    "7zip": ("x64", "x86", "arm64"), "vlc": ("x64", "x86", "arm64"),
    "firefox": ("x64", "x86", "arm64"), "obs": ("x64",), "notepadplusplus": ("x64", "x86", "arm64"),
}

# 内置专用 provider = data/apps.json 的 8 款 + 3 款非 GitHub 来源的 AI 客户端；
# 其余 34 款由通用 GitHub provider 在传入 catalog 时动态注册。
STATIC_PROVIDER_IDS = {
    *ARCHITECTURES, "workbuddy", "deepseekharness", "doubao",
}


class FixtureProvider:
    def __init__(self, app_id):
        self.id = app_id
        self.refreshes = 0

    async def get_releases(self, *, force_refresh=False):
        self.refreshes += int(force_refresh)
        return [Release(
            id=f"1.2.3-windows-{architecture}", version="1.2.3", platform="windows", architecture=architecture,
            download_url=f"https://downloads.example/{self.id}-1.2.3-{architecture}.exe",
            file_name=f"{self.id}-1.2.3-{architecture}.exe",
        ) for architecture in ARCHITECTURES[self.id]]

    async def resolve_download(self, release):
        return release.model_copy(deep=True)


class ProviderRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_registry_matches_catalog_and_uses_the_injected_clients(self):
        async with httpx2.AsyncClient() as primary, httpx2.AsyncClient(trust_env=False) as direct:
            providers = create_release_providers(primary, direct)
            catalog_ids = {app["download_provider"] for app in json.loads(DATA_FILE.read_text(encoding="utf-8"))["apps"]}
            self.assertTrue(catalog_ids <= set(providers))
            self.assertEqual(set(providers), STATIC_PROVIDER_IDS)
            for provider_id, provider in providers.items():
                self.assertEqual(provider.id, provider_id)
                self.assertIs(provider._client, primary if provider_id == "notepadplusplus" else direct)

    async def test_all_catalog_applications_have_current_x64_releases(self):
        providers = {app_id: FixtureProvider(app_id) for app_id in ARCHITECTURES}
        with TestClient(create_app(release_providers=providers)) as client:
            response = client.get("/api/v1/apps?architecture=x64")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["total"], 8)
            for app in response.json()["items"]:
                self.assertEqual(app["version"], "1.2.3")
                self.assertEqual(app["latest_release"]["architecture"], "x64")
                latest = client.get(f'/api/v1/apps/{app["id"]}/releases/latest?architecture=x64')
                self.assertEqual(latest.status_code, 200)
                self.assertEqual(latest.json()["version"], "1.2.3")
                self.assertGreaterEqual(providers[app["id"]].refreshes, 1)

    async def test_unsupported_architectures_are_not_replaced_with_another_package(self):
        providers = {app_id: FixtureProvider(app_id) for app_id in ARCHITECTURES}
        with TestClient(create_app(release_providers=providers)) as client:
            for app_id, architecture in (("wechat", "arm64"), ("vscode", "x86"), ("obs", "x86"), ("obs", "arm64")):
                with self.subTest(app_id=app_id, architecture=architecture):
                    self.assertEqual(client.get(f"/api/v1/apps/{app_id}/releases/latest?architecture={architecture}").status_code, 404)
                    self.assertIsNone(client.get(f"/api/v1/apps/{app_id}?architecture={architecture}").json()["latest_release"])

    async def test_each_release_collection_exposes_only_its_supported_architectures(self):
        providers = {app_id: FixtureProvider(app_id) for app_id in ARCHITECTURES}
        with TestClient(create_app(release_providers=providers)) as client:
            for app_id, architectures in ARCHITECTURES.items():
                with self.subTest(app_id=app_id):
                    payload = client.get(f"/api/v1/apps/{app_id}/releases").json()
                    self.assertEqual(payload["total"], len(architectures))
                    self.assertEqual(tuple(release["architecture"] for release in payload["items"]), architectures)


if __name__ == "__main__":
    unittest.main()
