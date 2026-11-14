"""联想应用商店源（``lenovo`` 配置段）的配置测试。

覆盖设计文档 3.2 的默认值、提交的 ``config.json``、文件/本地文件/环境变量覆盖，以及非法值
（负数 TTL、负数刷新间隔、并发越界、图片缓存上限非正、非法布尔等）让配置加载失败。
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
    LenovoConfig,
    default_cache_directory,
)


DEFAULT_LENOVO = {
    "enabled": True,
    "catalog_ttl_seconds": 21600.0,
    "catalog_refresh_interval_seconds": 300.0,
    "catalog_max_stale_seconds": 604800.0,
    "details_ttl_seconds": 900.0,
    "details_persistent_ttl_seconds": 21600.0,
    "search_cache_ttl_seconds": 60.0,
    "timeout_seconds": 20.0,
    "catalog_concurrency": 3,
    "image_cache_ttl_seconds": 2592000.0,
    "image_cache_max_bytes": 268435456,
}

ALL_ENVIRONMENT = {
    "WINAPPSMANAGER_LENOVO_ENABLED": "false",
    "WINAPPSMANAGER_LENOVO_CATALOG_TTL_SECONDS": "120",
    "WINAPPSMANAGER_LENOVO_CATALOG_REFRESH_INTERVAL_SECONDS": "90",
    "WINAPPSMANAGER_LENOVO_CATALOG_MAX_STALE_SECONDS": "600",
    "WINAPPSMANAGER_LENOVO_DETAILS_TTL_SECONDS": "30.5",
    "WINAPPSMANAGER_LENOVO_DETAILS_PERSISTENT_TTL_SECONDS": "45",
    "WINAPPSMANAGER_LENOVO_SEARCH_CACHE_TTL_SECONDS": "5",
    "WINAPPSMANAGER_LENOVO_TIMEOUT_SECONDS": "8",
    "WINAPPSMANAGER_LENOVO_CATALOG_CONCURRENCY": "1",
    "WINAPPSMANAGER_LENOVO_IMAGE_CACHE_TTL_SECONDS": "3600",
    "WINAPPSMANAGER_LENOVO_IMAGE_CACHE_MAX_BYTES": "1024",
}


def write_config(directory, payload):
    path = Path(directory) / "config.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


class LenovoConfigTests(unittest.TestCase):
    def test_defaults_match_the_design_document(self):
        settings = BackendSettings()
        self.assertEqual(settings.lenovo.model_dump(), DEFAULT_LENOVO)
        self.assertTrue(settings.lenovo.enabled)
        self.assertEqual(settings.lenovo.timeout_seconds, 20.0)
        self.assertEqual(settings.lenovo.catalog_concurrency, 3)
        self.assertEqual(settings.lenovo.image_cache_max_bytes, 268435456)
        # 目录每 5 分钟由后台刷新一次；catalog_ttl 只决定响应里的 stale 标记。
        self.assertEqual(settings.lenovo.catalog_refresh_interval_seconds, 300.0)

    def test_the_committed_configuration_contains_the_lenovo_section(self):
        committed = json.loads(DEFAULT_CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertIn("lenovo", committed)
        self.assertEqual(committed["lenovo"], DEFAULT_LENOVO)

    def test_loading_the_committed_configuration_keeps_the_defaults(self):
        settings = BackendSettings.load(environ={})
        self.assertEqual(settings.lenovo.model_dump(), DEFAULT_LENOVO)
        self.assertEqual(settings.cache.directory, default_cache_directory())

    def test_json_file_overrides_every_lenovo_field(self):
        payload = {
            "enabled": False,
            "catalog_ttl_seconds": 1,
            "catalog_refresh_interval_seconds": 2.5,
            "catalog_max_stale_seconds": 2,
            "details_ttl_seconds": 3,
            "details_persistent_ttl_seconds": 4,
            "search_cache_ttl_seconds": 5,
            "timeout_seconds": 6,
            "catalog_concurrency": 6,
            "image_cache_ttl_seconds": 7,
            "image_cache_max_bytes": 8,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(directory, {"lenovo": payload})
            settings = BackendSettings.load(path, environ={})
        self.assertEqual(settings.lenovo.model_dump(), {
            **payload,
            "catalog_ttl_seconds": 1.0,
            "catalog_refresh_interval_seconds": 2.5,
            "catalog_max_stale_seconds": 2.0,
            "details_ttl_seconds": 3.0,
            "details_persistent_ttl_seconds": 4.0,
            "search_cache_ttl_seconds": 5.0,
            "timeout_seconds": 6.0,
            "image_cache_ttl_seconds": 7.0,
        })

    def test_partial_lenovo_section_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(directory, {"lenovo": {"enabled": False, "timeout_seconds": 5}})
            settings = BackendSettings.load(path, environ={})
        self.assertFalse(settings.lenovo.enabled)
        self.assertEqual(settings.lenovo.timeout_seconds, 5.0)
        self.assertEqual(settings.lenovo.catalog_ttl_seconds, 21600.0)
        self.assertEqual(settings.lenovo.catalog_concurrency, 3)
        self.assertEqual(settings.cache.ttl_seconds, 300.0)

    def test_local_config_file_overrides_the_committed_lenovo_section(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(directory, {"lenovo": {"timeout_seconds": 20}})
            (Path(directory) / "config.local.json").write_text(
                json.dumps({"lenovo": {"timeout_seconds": 9, "catalog_concurrency": 2}}),
                encoding="utf-8",
            )
            settings = BackendSettings.load(path, environ={})
        self.assertEqual(settings.lenovo.timeout_seconds, 9.0)
        self.assertEqual(settings.lenovo.catalog_concurrency, 2)
        self.assertEqual(settings.lenovo.details_ttl_seconds, 900.0)

    def test_environment_overrides_every_lenovo_field(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(directory, {"lenovo": {}})
            settings = BackendSettings.load(path, environ=ALL_ENVIRONMENT)
        self.assertFalse(settings.lenovo.enabled)
        self.assertEqual(settings.lenovo.catalog_ttl_seconds, 120.0)
        self.assertEqual(settings.lenovo.catalog_refresh_interval_seconds, 90.0)
        self.assertEqual(settings.lenovo.catalog_max_stale_seconds, 600.0)
        self.assertEqual(settings.lenovo.details_ttl_seconds, 30.5)
        self.assertEqual(settings.lenovo.details_persistent_ttl_seconds, 45.0)
        self.assertEqual(settings.lenovo.search_cache_ttl_seconds, 5.0)
        self.assertEqual(settings.lenovo.timeout_seconds, 8.0)
        self.assertEqual(settings.lenovo.catalog_concurrency, 1)
        self.assertEqual(settings.lenovo.image_cache_ttl_seconds, 3600.0)
        self.assertEqual(settings.lenovo.image_cache_max_bytes, 1024)

    def test_catalog_refresh_interval_accepts_zero_to_disable_the_refresher(self):
        settings = LenovoConfig(catalog_refresh_interval_seconds=0)
        self.assertEqual(settings.catalog_refresh_interval_seconds, 0.0)
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(directory, {"lenovo": {"catalog_refresh_interval_seconds": 0}})
            loaded = BackendSettings.load(path, environ={})
        self.assertEqual(loaded.lenovo.catalog_refresh_interval_seconds, 0.0)
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(directory, {"lenovo": {}})
            from_environment = BackendSettings.load(
                path,
                environ={"WINAPPSMANAGER_LENOVO_CATALOG_REFRESH_INTERVAL_SECONDS": "0"},
            )
        self.assertEqual(from_environment.lenovo.catalog_refresh_interval_seconds, 0.0)

    def test_environment_boolean_forms(self):
        for value, expected in (("true", True), ("ON", True), ("1", True),
                                ("false", False), ("no", False), ("0", False)):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                path = write_config(directory, {"lenovo": {}})
                settings = BackendSettings.load(
                    path, environ={"WINAPPSMANAGER_LENOVO_ENABLED": value}
                )
                self.assertEqual(settings.lenovo.enabled, expected)

    def test_empty_environment_values_fall_back_to_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(directory, {"lenovo": {"timeout_seconds": 7}})
            settings = BackendSettings.load(
                path, environ={"WINAPPSMANAGER_LENOVO_TIMEOUT_SECONDS": "   "}
            )
        self.assertEqual(settings.lenovo.timeout_seconds, 7.0)

    def test_invalid_file_values_are_rejected(self):
        invalid_payloads = (
            {"lenovo": {"catalog_ttl_seconds": -1}},
            {"lenovo": {"catalog_refresh_interval_seconds": -1}},
            {"lenovo": {"catalog_refresh_interval_seconds": float("inf")}},
            {"lenovo": {"catalog_refresh_interval_seconds": "300"}},
            {"lenovo": {"catalog_max_stale_seconds": -0.5}},
            {"lenovo": {"details_ttl_seconds": -1}},
            {"lenovo": {"details_persistent_ttl_seconds": -1}},
            {"lenovo": {"search_cache_ttl_seconds": -1}},
            {"lenovo": {"image_cache_ttl_seconds": -1}},
            {"lenovo": {"catalog_ttl_seconds": float("inf")}},
            {"lenovo": {"timeout_seconds": 0}},
            {"lenovo": {"catalog_concurrency": 0}},
            {"lenovo": {"catalog_concurrency": 7}},
            {"lenovo": {"catalog_concurrency": 1.5}},
            {"lenovo": {"catalog_concurrency": True}},
            {"lenovo": {"image_cache_max_bytes": 0}},
            {"lenovo": {"image_cache_max_bytes": -1}},
            {"lenovo": {"enabled": "yes"}},
            {"lenovo": {"enabled": 1}},
            {"lenovo": {"unknown": True}},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as directory:
                path = write_config(directory, payload)
                with self.assertRaises(ValueError):
                    BackendSettings.load(path, environ={})

    def test_invalid_environment_values_are_rejected(self):
        invalid_environments = (
            {"WINAPPSMANAGER_LENOVO_ENABLED": "maybe"},
            {"WINAPPSMANAGER_LENOVO_TIMEOUT_SECONDS": "soon"},
            {"WINAPPSMANAGER_LENOVO_TIMEOUT_SECONDS": "0"},
            {"WINAPPSMANAGER_LENOVO_CATALOG_TTL_SECONDS": "NaN"},
            {"WINAPPSMANAGER_LENOVO_CATALOG_REFRESH_INTERVAL_SECONDS": "soon"},
            {"WINAPPSMANAGER_LENOVO_CATALOG_REFRESH_INTERVAL_SECONDS": "-1"},
            {"WINAPPSMANAGER_LENOVO_CATALOG_REFRESH_INTERVAL_SECONDS": "NaN"},
            {"WINAPPSMANAGER_LENOVO_CATALOG_CONCURRENCY": "many"},
            {"WINAPPSMANAGER_LENOVO_CATALOG_CONCURRENCY": "3.5"},
            {"WINAPPSMANAGER_LENOVO_CATALOG_CONCURRENCY": "7"},
            {"WINAPPSMANAGER_LENOVO_IMAGE_CACHE_MAX_BYTES": "0"},
            {"WINAPPSMANAGER_LENOVO_IMAGE_CACHE_MAX_BYTES": "many"},
        )
        for environ in invalid_environments:
            with self.subTest(environ=environ), tempfile.TemporaryDirectory() as directory:
                path = write_config(directory, {"lenovo": {}})
                with self.assertRaises(ValueError):
                    BackendSettings.load(path, environ=environ)

    def test_strict_validation_applies_to_direct_construction_and_assignment(self):
        with self.assertRaises(ValueError):
            LenovoConfig(enabled="yes")
        with self.assertRaises(ValueError):
            LenovoConfig(catalog_concurrency=7)
        with self.assertRaises(ValueError):
            LenovoConfig(catalog_refresh_interval_seconds=-0.5)
        settings = BackendSettings()
        with self.assertRaises(ValueError):
            settings.lenovo.catalog_concurrency = 9
        with self.assertRaises(ValueError):
            settings.lenovo.image_cache_max_bytes = 0
        with self.assertRaises(ValueError):
            settings.lenovo.catalog_refresh_interval_seconds = -1


if __name__ == "__main__":
    unittest.main()
