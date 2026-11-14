import json
from pathlib import Path
import sys
import tempfile
import unittest

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR.parent))

from Backend.config import (
    BASE_DIR,
    DEFAULT_CONFIG_FILE,
    LOCAL_CONFIG_FILE_NAME,
    BackendSettings,
    CacheConfig,
    default_cache_directory,
)


def write_config(directory, payload):
    path = Path(directory) / "config.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


class ConfigTests(unittest.TestCase):
    def test_defaults_are_used_without_a_configuration_file(self):
        settings = BackendSettings()
        self.assertTrue(settings.cache.enabled)
        self.assertTrue(settings.cache.persist)
        self.assertEqual(settings.cache.directory, default_cache_directory())
        self.assertEqual(settings.cache.directory, BASE_DIR / ".cache")
        self.assertTrue(settings.cache.directory.is_relative_to(BASE_DIR))
        self.assertEqual(settings.cache.ttl_seconds, 300.0)
        self.assertEqual(settings.cache.persistent_ttl_seconds, 3600.0)
        self.assertEqual(settings.cache.max_entries, 64)
        self.assertEqual(settings.providers.cache_ttl_seconds, 600.0)
        self.assertEqual(settings.providers.timeout_seconds, 10.0)
        self.assertEqual(settings.http.timeout_seconds, 5.0)
        self.assertEqual(settings.http.connect_timeout_seconds, 3.0)

    def test_committed_config_file_is_valid(self):
        settings = BackendSettings.load(environ={})
        self.assertEqual(settings.cache.ttl_seconds, 300.0)
        self.assertEqual(settings.cache.persistent_ttl_seconds, 3600.0)
        # The committed configuration keeps the cache inside the working directory.
        self.assertEqual(settings.cache.directory, BASE_DIR / ".cache")

    def test_json_file_overrides_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(directory, {
                "cache": {
                    "enabled": False,
                    "persist": False,
                    "ttl_seconds": 12.5,
                    "persistent_ttl_seconds": 90,
                    "max_entries": 3,
                },
                "providers": {"cache_ttl_seconds": 0, "timeout_seconds": 4},
                "http": {"timeout_seconds": 2, "connect_timeout_seconds": 1},
            })
            settings = BackendSettings.load(path, environ={})
        self.assertFalse(settings.cache.enabled)
        self.assertFalse(settings.cache.persist)
        self.assertEqual(settings.cache.ttl_seconds, 12.5)
        self.assertEqual(settings.cache.persistent_ttl_seconds, 90.0)
        self.assertEqual(settings.cache.max_entries, 3)
        self.assertEqual(settings.providers.cache_ttl_seconds, 0.0)
        self.assertEqual(settings.providers.timeout_seconds, 4.0)
        self.assertEqual(settings.http.connect_timeout_seconds, 1.0)

    def test_relative_directory_resolves_against_the_backend_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(directory, {"cache": {"directory": "runtime-cache"}})
            settings = BackendSettings.load(path, environ={})
        self.assertEqual(settings.cache.directory, BASE_DIR / "runtime-cache")

    def test_null_directory_uses_the_workspace_default(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(directory, {"cache": {"directory": None}})
            settings = BackendSettings.load(path, environ={})
        self.assertEqual(settings.cache.directory, default_cache_directory())
        self.assertEqual(settings.cache.directory, BASE_DIR / ".cache")

    def test_environment_overrides_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(directory, {"cache": {"ttl_seconds": 10, "persist": True}})
            settings = BackendSettings.load(path, environ={
                "WINAPPSMANAGER_CACHE_ENABLED": "false",
                "WINAPPSMANAGER_CACHE_PERSIST": "no",
                "WINAPPSMANAGER_CACHE_TTL_SECONDS": "45",
                "WINAPPSMANAGER_CACHE_PERSISTENT_TTL_SECONDS": "900",
                "WINAPPSMANAGER_CACHE_MAX_ENTRIES": "9",
                "WINAPPSMANAGER_PROVIDER_CACHE_TTL_SECONDS": "5",
                "WINAPPSMANAGER_PROVIDER_TIMEOUT_SECONDS": "6",
                "WINAPPSMANAGER_HTTP_TIMEOUT_SECONDS": "7",
                "WINAPPSMANAGER_HTTP_CONNECT_TIMEOUT_SECONDS": "2",
            })
        self.assertFalse(settings.cache.enabled)
        self.assertFalse(settings.cache.persist)
        self.assertEqual(settings.cache.ttl_seconds, 45.0)
        self.assertEqual(settings.cache.persistent_ttl_seconds, 900.0)
        self.assertEqual(settings.cache.max_entries, 9)
        self.assertEqual(settings.providers.cache_ttl_seconds, 5.0)
        self.assertEqual(settings.providers.timeout_seconds, 6.0)
        self.assertEqual(settings.http.timeout_seconds, 7.0)
        self.assertEqual(settings.http.connect_timeout_seconds, 2.0)

    def test_config_path_environment_variable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(directory, {"cache": {"ttl_seconds": 77}})
            settings = BackendSettings.load(environ={"WINAPPSMANAGER_CONFIG": str(path)})
        self.assertEqual(settings.cache.ttl_seconds, 77.0)

    def test_explicit_missing_config_file_is_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                BackendSettings.load(Path(directory) / "missing.json", environ={})
            with self.assertRaises(ValueError):
                BackendSettings.load(environ={"WINAPPSMANAGER_CONFIG": str(Path(directory) / "missing.json")})

    def test_invalid_files_and_values_are_rejected(self):
        invalid_payloads = (
            {"cache": {"ttl_seconds": -1}},
            {"cache": {"persistent_ttl_seconds": -1}},
            {"cache": {"ttl_seconds": float("inf")}},
            {"cache": {"max_entries": 0}},
            {"cache": {"enabled": "yes"}},
            {"cache": {"unknown": True}},
            {"providers": {"timeout_seconds": 0}},
            {"http": {"connect_timeout_seconds": 9, "timeout_seconds": 5}},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as directory:
                path = write_config(directory, payload)
                with self.assertRaises(ValueError):
                    BackendSettings.load(path, environ={})

    def test_malformed_json_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text("{", encoding="utf-8")
            with self.assertRaises(ValueError):
                BackendSettings.load(path, environ={})

    def test_invalid_environment_values_are_rejected(self):
        invalid = (
            {"WINAPPSMANAGER_CACHE_ENABLED": "maybe"},
            {"WINAPPSMANAGER_CACHE_TTL_SECONDS": "soon"},
            {"WINAPPSMANAGER_CACHE_MAX_ENTRIES": "many"},
        )
        for environ in invalid:
            with self.subTest(environ=environ), tempfile.TemporaryDirectory() as directory:
                path = write_config(directory, {})
                with self.assertRaises(ValueError):
                    BackendSettings.load(path, environ=environ)

    def test_direct_construction_does_not_read_files_or_environment(self):
        settings = BackendSettings(cache=CacheConfig(persist=False, ttl_seconds=1))
        self.assertFalse(settings.cache.persist)
        self.assertEqual(settings.cache.ttl_seconds, 1.0)
        self.assertEqual(settings.cache.max_entries, 64)


class GithubConfigTests(unittest.TestCase):
    def test_defaults_disable_the_token(self):
        settings = BackendSettings()
        self.assertEqual(settings.github.token, "")
        self.assertEqual(settings.github.api_base, "https://api.github.com")
        self.assertEqual(settings.github.timeout_seconds, 10.0)
        self.assertEqual(settings.github.refresh_interval_seconds, 900.0)

    def test_the_committed_configuration_never_contains_a_token(self):
        committed = json.loads(DEFAULT_CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertEqual(committed["github"]["token"], "")
        self.assertIn("config.local.json", LOCAL_CONFIG_FILE_NAME)

    def test_the_committed_configuration_sets_the_github_refresh_interval(self):
        settings = BackendSettings.load(environ={})
        self.assertEqual(settings.github.refresh_interval_seconds, 900.0)
        committed = json.loads(DEFAULT_CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertEqual(committed["github"]["refresh_interval_seconds"], 900)

    def test_github_refresh_interval_can_be_overridden_by_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(directory, {"github": {"refresh_interval_seconds": 0}})
            settings = BackendSettings.load(path, environ={})
            self.assertEqual(settings.github.refresh_interval_seconds, 0.0)

            path = write_config(directory, {"github": {"refresh_interval_seconds": 12.5}})
            settings = BackendSettings.load(path, environ={})
            self.assertEqual(settings.github.refresh_interval_seconds, 12.5)

    def test_github_refresh_interval_environment_override(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(directory, {"github": {"refresh_interval_seconds": 900}})
            settings = BackendSettings.load(
                path, environ={"WINAPPSMANAGER_GITHUB_REFRESH_INTERVAL_SECONDS": "0"}
            )
            self.assertEqual(settings.github.refresh_interval_seconds, 0.0)

            settings = BackendSettings.load(
                path, environ={"WINAPPSMANAGER_GITHUB_REFRESH_INTERVAL_SECONDS": "45.5"}
            )
            self.assertEqual(settings.github.refresh_interval_seconds, 45.5)

    def test_invalid_github_refresh_interval_values_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            for value in (-1, -0.5, float("inf"), float("nan")):
                with self.subTest(value=value):
                    path = write_config(directory, {"github": {"refresh_interval_seconds": value}})
                    with self.assertRaises(ValueError):
                        BackendSettings.load(path, environ={})

            path = write_config(directory, {"github": {}})
            for value in ("soon", "NaN", "inf"):
                with self.subTest(environment=value):
                    with self.assertRaises(ValueError):
                        BackendSettings.load(
                            path,
                            environ={"WINAPPSMANAGER_GITHUB_REFRESH_INTERVAL_SECONDS": value},
                        )

    def test_local_config_file_overrides_the_committed_one(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(directory, {"cache": {"ttl_seconds": 10}, "github": {"token": ""}})
            (Path(directory) / "config.local.json").write_text(
                json.dumps({"github": {"token": "local-token"}, "cache": {"max_entries": 9}}),
                encoding="utf-8",
            )
            settings = BackendSettings.load(path, environ={})
        self.assertEqual(settings.github.token, "local-token")
        self.assertEqual(settings.cache.max_entries, 9)
        self.assertEqual(settings.cache.ttl_seconds, 10.0)

    def test_environment_token_overrides_the_local_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(directory, {"cache": {}})
            (Path(directory) / "config.local.json").write_text(
                json.dumps({"github": {"token": "local-token"}}), encoding="utf-8"
            )
            settings = BackendSettings.load(path, environ={"WINAPPSMANAGER_GITHUB_TOKEN": "env-token"})
        self.assertEqual(settings.github.token, "env-token")

    def test_github_environment_overrides(self):
        settings = BackendSettings.load(environ={
            "WINAPPSMANAGER_GITHUB_TOKEN": " token ",
            "WINAPPSMANAGER_GITHUB_API_BASE": "https://ghe.example/api/v3/",
            "WINAPPSMANAGER_GITHUB_TIMEOUT_SECONDS": "4",
        })
        self.assertEqual(settings.github.token, "token")
        self.assertEqual(settings.github.api_base, "https://ghe.example/api/v3")
        self.assertEqual(settings.github.timeout_seconds, 4.0)

    def test_invalid_github_values_are_rejected(self):
        invalid = (
            {"github": {"token": "has space"}},
            {"github": {"token": "line\nbreak"}},
            {"github": {"api_base": "http://api.github.com"}},
            {"github": {"api_base": "https://api github.com"}},
            {"github": {"timeout_seconds": 0}},
            {"github": {"unknown": True}},
        )
        for payload in invalid:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as directory:
                path = write_config(directory, payload)
                with self.assertRaises(ValueError):
                    BackendSettings.load(path, environ={})


if __name__ == "__main__":
    unittest.main()
