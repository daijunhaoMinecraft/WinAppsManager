"""360 软件源（``soft360`` 配置段）的配置测试。

覆盖默认值、提交的 ``config.json``、文件/本地文件/环境变量覆盖，以及非法值（负数 TTL、
非法刷新间隔、分页/并发越界、图片缓存上限非正、非法布尔等）让配置加载失败。
"""

import json
from pathlib import Path
import sys
import tempfile
import unittest

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR.parent))

from Backend.config import (
    DEFAULT_CONFIG_FILE,
    BackendSettings,
    Soft360Config,
    default_cache_directory,
)


DEFAULT_SOFT360 = {
    "enabled": True,
    "catalog_pages_per_category": 3,
    "catalog_ttl_seconds": 21600.0,
    "catalog_max_stale_seconds": 604800.0,
    "catalog_refresh_interval_seconds": 1800.0,
    "details_ttl_seconds": 900.0,
    "details_persistent_ttl_seconds": 21600.0,
    "timeout_seconds": 20.0,
    "catalog_concurrency": 4,
    "image_cache_ttl_seconds": 2592000.0,
    "image_cache_max_bytes": 268435456,
}

ALL_ENVIRONMENT = {
    "WINAPPSMANAGER_SOFT360_ENABLED": "false",
    "WINAPPSMANAGER_SOFT360_CATALOG_PAGES_PER_CATEGORY": "5",
    "WINAPPSMANAGER_SOFT360_CATALOG_TTL_SECONDS": "120",
    "WINAPPSMANAGER_SOFT360_CATALOG_MAX_STALE_SECONDS": "600",
    "WINAPPSMANAGER_SOFT360_CATALOG_REFRESH_INTERVAL_SECONDS": "90",
    "WINAPPSMANAGER_SOFT360_DETAILS_TTL_SECONDS": "30.5",
    "WINAPPSMANAGER_SOFT360_DETAILS_PERSISTENT_TTL_SECONDS": "45",
    "WINAPPSMANAGER_SOFT360_TIMEOUT_SECONDS": "8",
    "WINAPPSMANAGER_SOFT360_CATALOG_CONCURRENCY": "1",
    "WINAPPSMANAGER_SOFT360_IMAGE_CACHE_TTL_SECONDS": "3600",
    "WINAPPSMANAGER_SOFT360_IMAGE_CACHE_MAX_BYTES": "1024",
}


def write_config(directory, payload):
    path = Path(directory) / "config.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


class Soft360ConfigTests(unittest.TestCase):
    def test_defaults_match_the_design(self):
        settings = BackendSettings()
        self.assertEqual(settings.soft360.model_dump(), DEFAULT_SOFT360)
        self.assertTrue(settings.soft360.enabled)
        self.assertEqual(settings.soft360.catalog_pages_per_category, 3)
        self.assertEqual(settings.soft360.catalog_refresh_interval_seconds, 1800.0)
        self.assertEqual(settings.soft360.timeout_seconds, 20.0)
        self.assertEqual(settings.soft360.catalog_concurrency, 4)

    def test_the_committed_configuration_contains_the_soft360_section(self):
        committed = json.loads(DEFAULT_CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertIn("soft360", committed)
        self.assertEqual(committed["soft360"], DEFAULT_SOFT360)

    def test_loading_the_committed_configuration_keeps_the_defaults(self):
        settings = BackendSettings.load(environ={})
        self.assertEqual(settings.soft360.model_dump(), DEFAULT_SOFT360)
        self.assertEqual(settings.cache.directory, default_cache_directory())

    def test_json_file_overrides_every_soft360_field(self):
        payload = {
            "enabled": False,
            "catalog_pages_per_category": 20,
            "catalog_ttl_seconds": 1,
            "catalog_max_stale_seconds": 2,
            "catalog_refresh_interval_seconds": 2.5,
            "details_ttl_seconds": 3,
            "details_persistent_ttl_seconds": 4,
            "timeout_seconds": 6,
            "catalog_concurrency": 16,
            "image_cache_ttl_seconds": 7,
            "image_cache_max_bytes": 8,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(directory, {"soft360": payload})
            settings = BackendSettings.load(path, environ={})
        self.assertEqual(
            settings.soft360.model_dump(),
            {
                **payload,
                "catalog_ttl_seconds": 1.0,
                "catalog_max_stale_seconds": 2.0,
                "catalog_refresh_interval_seconds": 2.5,
                "details_ttl_seconds": 3.0,
                "details_persistent_ttl_seconds": 4.0,
                "timeout_seconds": 6.0,
                "image_cache_ttl_seconds": 7.0,
            },
        )

    def test_partial_soft360_section_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(
                directory, {"soft360": {"enabled": False, "catalog_pages_per_category": 1}}
            )
            settings = BackendSettings.load(path, environ={})
        self.assertFalse(settings.soft360.enabled)
        self.assertEqual(settings.soft360.catalog_pages_per_category, 1)
        self.assertEqual(settings.soft360.catalog_refresh_interval_seconds, 1800.0)
        self.assertEqual(settings.soft360.catalog_concurrency, 4)
        self.assertEqual(settings.cache.ttl_seconds, 300.0)

    def test_local_config_file_overrides_the_committed_soft360_section(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(directory, {"soft360": {"catalog_pages_per_category": 3}})
            (Path(directory) / "config.local.json").write_text(
                json.dumps({"soft360": {"catalog_pages_per_category": 2, "catalog_concurrency": 2}}),
                encoding="utf-8",
            )
            settings = BackendSettings.load(path, environ={})
        self.assertEqual(settings.soft360.catalog_pages_per_category, 2)
        self.assertEqual(settings.soft360.catalog_concurrency, 2)
        self.assertEqual(settings.soft360.catalog_ttl_seconds, 21600.0)

    def test_environment_overrides_every_soft360_field(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(directory, {"soft360": {}})
            settings = BackendSettings.load(path, environ=ALL_ENVIRONMENT)
        self.assertFalse(settings.soft360.enabled)
        self.assertEqual(settings.soft360.catalog_pages_per_category, 5)
        self.assertEqual(settings.soft360.catalog_ttl_seconds, 120.0)
        self.assertEqual(settings.soft360.catalog_max_stale_seconds, 600.0)
        self.assertEqual(settings.soft360.catalog_refresh_interval_seconds, 90.0)
        self.assertEqual(settings.soft360.details_ttl_seconds, 30.5)
        self.assertEqual(settings.soft360.details_persistent_ttl_seconds, 45.0)
        self.assertEqual(settings.soft360.timeout_seconds, 8.0)
        self.assertEqual(settings.soft360.catalog_concurrency, 1)
        self.assertEqual(settings.soft360.image_cache_ttl_seconds, 3600.0)
        self.assertEqual(settings.soft360.image_cache_max_bytes, 1024)

    def test_catalog_refresh_interval_accepts_zero_to_disable_the_refresher(self):
        settings = Soft360Config(catalog_refresh_interval_seconds=0)
        self.assertEqual(settings.catalog_refresh_interval_seconds, 0.0)
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(directory, {"soft360": {"catalog_refresh_interval_seconds": 0}})
            loaded = BackendSettings.load(path, environ={})
        self.assertEqual(loaded.soft360.catalog_refresh_interval_seconds, 0.0)
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(directory, {"soft360": {}})
            from_environment = BackendSettings.load(
                path,
                environ={"WINAPPSMANAGER_SOFT360_CATALOG_REFRESH_INTERVAL_SECONDS": "0"},
            )
        self.assertEqual(from_environment.soft360.catalog_refresh_interval_seconds, 0.0)

    def test_environment_boolean_forms(self):
        for value, expected in (
            ("true", True),
            ("ON", True),
            ("1", True),
            ("false", False),
            ("no", False),
            ("0", False),
        ):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                path = write_config(directory, {"soft360": {}})
                settings = BackendSettings.load(
                    path, environ={"WINAPPSMANAGER_SOFT360_ENABLED": value}
                )
                self.assertEqual(settings.soft360.enabled, expected)

    def test_empty_environment_values_fall_back_to_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(directory, {"soft360": {"catalog_concurrency": 2}})
            settings = BackendSettings.load(
                path, environ={"WINAPPSMANAGER_SOFT360_CATALOG_CONCURRENCY": "   "}
            )
        self.assertEqual(settings.soft360.catalog_concurrency, 2)

    def test_invalid_file_values_are_rejected(self):
        invalid_payloads = (
            {"soft360": {"catalog_pages_per_category": 0}},
            {"soft360": {"catalog_pages_per_category": 21}},
            {"soft360": {"catalog_pages_per_category": 1.5}},
            {"soft360": {"catalog_pages_per_category": True}},
            {"soft360": {"catalog_ttl_seconds": -1}},
            {"soft360": {"catalog_ttl_seconds": float("inf")}},
            {"soft360": {"catalog_max_stale_seconds": -0.5}},
            {"soft360": {"catalog_refresh_interval_seconds": -1}},
            {"soft360": {"catalog_refresh_interval_seconds": float("inf")}},
            {"soft360": {"catalog_refresh_interval_seconds": "300"}},
            {"soft360": {"details_ttl_seconds": -1}},
            {"soft360": {"details_persistent_ttl_seconds": -1}},
            {"soft360": {"image_cache_ttl_seconds": -1}},
            {"soft360": {"timeout_seconds": 0}},
            {"soft360": {"catalog_concurrency": 0}},
            {"soft360": {"catalog_concurrency": 17}},
            {"soft360": {"catalog_concurrency": 1.5}},
            {"soft360": {"catalog_concurrency": True}},
            {"soft360": {"image_cache_max_bytes": 0}},
            {"soft360": {"image_cache_max_bytes": -1}},
            {"soft360": {"enabled": "yes"}},
            {"soft360": {"enabled": 1}},
            {"soft360": {"unknown": True}},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as directory:
                path = write_config(directory, payload)
                with self.assertRaises(ValueError):
                    BackendSettings.load(path, environ={})

    def test_invalid_environment_values_are_rejected(self):
        invalid_environments = (
            {"WINAPPSMANAGER_SOFT360_ENABLED": "maybe"},
            {"WINAPPSMANAGER_SOFT360_TIMEOUT_SECONDS": "soon"},
            {"WINAPPSMANAGER_SOFT360_TIMEOUT_SECONDS": "0"},
            {"WINAPPSMANAGER_SOFT360_CATALOG_TTL_SECONDS": "NaN"},
            {"WINAPPSMANAGER_SOFT360_CATALOG_REFRESH_INTERVAL_SECONDS": "soon"},
            {"WINAPPSMANAGER_SOFT360_CATALOG_REFRESH_INTERVAL_SECONDS": "-1"},
            {"WINAPPSMANAGER_SOFT360_CATALOG_REFRESH_INTERVAL_SECONDS": "NaN"},
            {"WINAPPSMANAGER_SOFT360_CATALOG_PAGES_PER_CATEGORY": "many"},
            {"WINAPPSMANAGER_SOFT360_CATALOG_PAGES_PER_CATEGORY": "21"},
            {"WINAPPSMANAGER_SOFT360_CATALOG_PAGES_PER_CATEGORY": "0"},
            {"WINAPPSMANAGER_SOFT360_CATALOG_CONCURRENCY": "many"},
            {"WINAPPSMANAGER_SOFT360_CATALOG_CONCURRENCY": "3.5"},
            {"WINAPPSMANAGER_SOFT360_CATALOG_CONCURRENCY": "17"},
            {"WINAPPSMANAGER_SOFT360_IMAGE_CACHE_MAX_BYTES": "0"},
            {"WINAPPSMANAGER_SOFT360_IMAGE_CACHE_MAX_BYTES": "many"},
        )
        for environ in invalid_environments:
            with self.subTest(environ=environ), tempfile.TemporaryDirectory() as directory:
                path = write_config(directory, {"soft360": {}})
                with self.assertRaises(ValueError):
                    BackendSettings.load(path, environ=environ)

    def test_strict_validation_applies_to_direct_construction_and_assignment(self):
        with self.assertRaises(ValueError):
            Soft360Config(enabled="yes")
        with self.assertRaises(ValueError):
            Soft360Config(catalog_pages_per_category=0)
        with self.assertRaises(ValueError):
            Soft360Config(catalog_concurrency=17)
        with self.assertRaises(ValueError):
            Soft360Config(catalog_refresh_interval_seconds=-0.5)
        settings = BackendSettings()
        with self.assertRaises(ValueError):
            settings.soft360.catalog_pages_per_category = 21
        with self.assertRaises(ValueError):
            settings.soft360.image_cache_max_bytes = 0
        with self.assertRaises(ValueError):
            settings.soft360.catalog_refresh_interval_seconds = -1


if __name__ == "__main__":
    unittest.main()
