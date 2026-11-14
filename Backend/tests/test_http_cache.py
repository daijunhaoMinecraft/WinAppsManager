from hashlib import sha256
import json
from pathlib import Path
import sys
import tempfile
import unittest

from fastapi import Request
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from Backend.icons import ICON_CACHE_CONTROL, IconService
from Backend.main import DATA_FILE, ICON_DIR
from Backend.tests.test_api import FakeQQProvider, create_app


def request(headers=None):
    return Request({"type": "http", "method": "GET", "path": "/icon.png", "headers":
                    [(name.lower().encode(), value.encode()) for name, value in (headers or {}).items()]})


class HttpCacheTests(unittest.TestCase):
    def test_cache_resource_counts_hits_and_clears_without_modifying_icons(self):
        provider = FakeQQProvider()
        qq_bytes = (ICON_DIR / "qq.png").read_bytes()
        with TestClient(create_app(release_providers={"qq": provider})) as client:
            self.assertEqual(client.get("/api/v1/cache").json()["entries"], 0)
            client.get("/api/v1/apps/qq")
            first = client.get("/api/v1/cache").json()
            self.assertEqual(first["entries"], 1)
            self.assertEqual(first["misses"], 1)
            client.get("/api/v1/apps/qq")
            self.assertEqual(client.get("/api/v1/cache").json()["hits"], 1)
            self.assertEqual(provider.force_refreshes, 1)
            self.assertEqual(provider.sign_calls, 2)
            cleared = client.delete("/api/v1/cache")
            self.assertEqual(cleared.status_code, 200)
            self.assertEqual(cleared.json()["cleared"], 1)
            self.assertEqual(cleared.json()["entries"], 0)
            self.assertEqual(cleared.headers["cache-control"], "no-store")
            client.get("/api/v1/apps/qq")
            self.assertEqual(provider.force_refreshes, 2)
            self.assertEqual((ICON_DIR / "qq.png").read_bytes(), qq_bytes)

    def test_manual_refresh_and_latest_bypass_version_cache(self):
        provider = FakeQQProvider()
        with TestClient(create_app(release_providers={"qq": provider})) as client:
            self.assertEqual(client.get("/api/v1/apps/qq").json()["version"], "9.9.37")
            provider.version = "9.9.38"
            self.assertEqual(client.get("/api/v1/apps/qq").json()["version"], "9.9.37")
            self.assertEqual(client.get("/api/v1/apps/qq?refresh=true").json()["version"], "9.9.38")
            provider.version = "9.9.39"
            self.assertEqual(client.get("/api/v1/apps?search=QQ&refresh=true").json()["items"][0]["version"], "9.9.39")
            provider.version = "9.9.40"
            self.assertEqual(client.get("/api/v1/apps/qq/releases?refresh=true").json()["items"][0]["version"], "9.9.40")
            provider.version = "9.9.41"
            self.assertEqual(client.get("/api/v1/apps/qq/releases/latest").json()["version"], "9.9.41")

    def test_fixed_icon_returns_week_cache_and_matching_etag_is_304(self):
        with TestClient(create_app()) as client:
            response = client.get("/api/v1/apps/qq/icon")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], ICON_CACHE_CONTROL)
            self.assertEqual(response.headers["etag"], '"' + sha256(response.content).hexdigest() + '"')
            for candidate in (response.headers["etag"], "W/" + response.headers["etag"], "*", '"different", ' + response.headers["etag"]):
                with self.subTest(candidate=candidate):
                    cached = client.get("/api/v1/apps/qq/icon", headers={"If-None-Match": candidate})
                    self.assertEqual(cached.status_code, 304)
                    self.assertEqual(cached.content, b"")
                    self.assertEqual(cached.headers["etag"], response.headers["etag"])

    def test_last_modified_and_etag_precedence(self):
        with TestClient(create_app()) as client:
            response = client.get("/api/v1/apps/qq/icon")
            self.assertEqual(client.get("/api/v1/apps/qq/icon", headers={"If-Modified-Since": response.headers["last-modified"]}).status_code, 304)
            self.assertEqual(client.get("/api/v1/apps/qq/icon", headers={"If-None-Match": '"not-matching"', "If-Modified-Since": response.headers["last-modified"]}).status_code, 200)
            self.assertEqual(client.get("/api/v1/apps/qq/icon", headers={"If-Modified-Since": "invalid date"}).status_code, 200)
            self.assertEqual(client.get("/icon/qq.png").headers["cache-control"], ICON_CACHE_CONTROL)

    def test_asset_update_changes_the_etag(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "icon.png"
            service = IconService()
            path.write_bytes(b"first asset")
            first = service.response(request(), path)
            path.write_bytes(b"new longer asset")
            second = service.response(request({"If-None-Match": first.headers["etag"]}), path)
            self.assertEqual(second.status_code, 200)
            self.assertNotEqual(first.headers["etag"], second.headers["etag"])

    def test_all_eight_fixed_icons_are_served_from_catalog_files(self):
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        with TestClient(create_app()) as client:
            self.assertEqual(len(data["apps"]), 8)
            for app in data["apps"]:
                with self.subTest(app_id=app["id"]):
                    path = ICON_DIR / app["icon_file"]
                    response = client.get(f'/api/v1/apps/{app["id"]}/icon')
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.content, path.read_bytes())
                    self.assertEqual(response.headers["cache-control"], ICON_CACHE_CONTROL)
                    self.assertTrue(response.content.startswith(b"\x89PNG\r\n\x1a\n") or response.content.startswith(b"\x00\x00\x01\x00"))

    def test_unconfigured_icon_is_still_404(self):
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        data["apps"][1]["icon_file"] = None
        with tempfile.TemporaryDirectory() as directory:
            data_file = Path(directory) / "apps.json"
            data_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            with TestClient(create_app(data_file=data_file)) as client:
                self.assertEqual(client.get("/api/v1/apps/wechat/icon").status_code, 404)
                self.assertIsNone(client.get("/api/v1/apps/wechat").json()["icon_url"])

    def test_provider_cannot_mutate_release_identity_in_place(self):
        class MutatingProvider(FakeQQProvider):
            async def resolve_download(self, release):
                release.version = "100.0.0"
                release.file_name = "different.exe"
                return release

        with TestClient(create_app(release_providers={"qq": MutatingProvider()})) as client:
            self.assertEqual(client.get("/api/v1/apps/qq/releases/latest").status_code, 502)


if __name__ == "__main__":
    unittest.main()
