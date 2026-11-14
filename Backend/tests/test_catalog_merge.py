"""Catalog loading: the curated apps.json plus the per-group files in ``apps.d``."""

import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR.parent))

from Backend.catalog import CatalogRepository


def app(app_id, **overrides):
    payload = {
        "id": app_id,
        "name": app_id.upper(),
        "description": "简短说明",
        "long_description": "详细说明",
        "category": "测试分类",
        "publisher": "示例厂商",
        "website_url": "https://example.com/",
        "download_page_url": "https://example.com/download",
        "icon_text": "X",
        "icon_background": "#123456",
        "installation_notes": "按向导安装。",
        "features": ["特性"],
        "tags": ["标签"],
    }
    payload.update(overrides)
    return payload


class CatalogMergeTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="wam-catalog-"))
        self.icons = self.directory / "icons"
        self.icons.mkdir()
        self.base = self.directory / "apps.json"

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    def write(self, path, apps):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": 1, "apps": apps}, ensure_ascii=False), encoding="utf-8")

    def repository(self, **kwargs):
        return CatalogRepository(self.base, self.icons, **kwargs)

    def test_derived_apps_directory_is_merged_after_the_base_file(self):
        self.write(self.base, [app("alpha")])
        self.write(self.base.parent / "apps.d" / "b.json", [app("bravo"), app("bravo2")])
        self.write(self.base.parent / "apps.d" / "a.json", [app("charlie")])
        repository = self.repository()
        self.assertEqual([item.id for item in repository.apps], ["alpha", "charlie", "bravo", "bravo2"])
        self.assertIsNotNone(repository.get_app("charlie"))

    def test_missing_extension_directory_keeps_the_base_catalog(self):
        self.write(self.base, [app("alpha")])
        self.assertEqual([item.id for item in self.repository().apps], ["alpha"])

    def test_duplicate_ids_across_files_are_rejected(self):
        self.write(self.base, [app("alpha")])
        self.write(self.base.parent / "apps.d" / "dup.json", [app("alpha")])
        with self.assertRaises(ValueError) as context:
            self.repository()
        self.assertIn("alpha", str(context.exception))

    def test_extra_directory_can_be_injected(self):
        self.write(self.base, [app("alpha")])
        other = self.directory / "elsewhere"
        self.write(other / "x.json", [app("delta")])
        self.write(self.base.parent / "apps.d" / "y.json", [app("echo")])
        repository = self.repository(extra_directory=other)
        self.assertEqual([item.id for item in repository.apps], ["alpha", "delta"])

    def test_extension_entries_are_validated_like_the_base_catalog(self):
        self.write(self.base, [app("alpha")])
        self.write(self.base.parent / "apps.d" / "bad.json", [app("broken", icon_file="missing.png")])
        with self.assertRaises(ValueError) as context:
            self.repository()
        self.assertIn("broken", str(context.exception))

    def test_extension_file_with_invalid_schema_is_rejected(self):
        self.write(self.base, [app("alpha")])
        (self.base.parent / "apps.d").mkdir()
        (self.base.parent / "apps.d" / "broken.json").write_text('{"schema_version": 2, "apps": []}', encoding="utf-8")
        with self.assertRaises(ValueError):
            self.repository()


if __name__ == "__main__":
    unittest.main()
