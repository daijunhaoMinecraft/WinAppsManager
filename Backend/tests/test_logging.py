"""后端日志实现（``Backend/logging_config.py`` 与 ``logging`` 配置段）测试。

覆盖 ``logging`` 段的默认值、提交的 ``config.json``、文件/``config.local.json``/环境变量
覆盖与非法值启动失败，以及 ``configure_logging`` 的写盘格式、幂等、禁用、降级、轮转、
``shutdown_logging``、``get_log_files`` 和 ``GET /api/v1/cache`` 的 ``logging`` 摘要。
全部测试使用临时目录，不访问真实网络、不写真实 ``Backend/.cache``。
"""

import contextlib
import io
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR.parent))

from Backend.config import (
    BASE_DIR,
    DEFAULT_CONFIG_FILE,
    BackendSettings,
    BuiltinConfig,
    CacheConfig,
    GithubConfig,
    LenovoConfig,
    LoggingConfig,
    RefreshConfig,
    default_cache_directory,
    default_log_directory,
)
from Backend.logging_config import (
    LOG_FILE_NAME,
    configure_logging,
    current_log_path,
    get_log_files,
    shutdown_logging,
)
from Backend.main import create_app


DEFAULT_LOGGING = {
    "enabled": True,
    "directory": ".cache/logs",
    "level": "INFO",
    "max_bytes": 5242880,
    "backup_count": 5,
    "console": True,
}
LOG_LINE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} (DEBUG|INFO|WARNING|ERROR) ")
MANAGED_ATTRIBUTE = "_winappsmanager_managed"


def write_config(directory, payload):
    path = Path(directory) / "config.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def managed_handlers(logger):
    return [handler for handler in logger.handlers if getattr(handler, MANAGED_ATTRIBUTE, False)]


class LoggingConfigTests(unittest.TestCase):
    def test_defaults_match_the_documented_values(self):
        settings = BackendSettings()
        logging_settings = settings.logging
        self.assertTrue(logging_settings.enabled)
        self.assertEqual(logging_settings.directory, default_log_directory())
        self.assertEqual(logging_settings.directory, default_cache_directory() / "logs")
        self.assertEqual(logging_settings.directory, BASE_DIR / ".cache" / "logs")
        self.assertTrue(logging_settings.directory.is_relative_to(BASE_DIR))
        self.assertEqual(logging_settings.level, "INFO")
        self.assertEqual(logging_settings.max_bytes, 5 * 1024 * 1024)
        self.assertEqual(logging_settings.backup_count, 5)
        self.assertTrue(logging_settings.console)

    def test_the_committed_configuration_contains_the_logging_section(self):
        committed = json.loads(DEFAULT_CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertIn("logging", committed)
        self.assertEqual(committed["logging"], DEFAULT_LOGGING)

    def test_loading_the_committed_configuration_keeps_the_defaults(self):
        settings = BackendSettings.load(environ={})
        self.assertEqual(settings.logging.model_dump(mode="json"), {
            **DEFAULT_LOGGING,
            "directory": str(default_log_directory()),
        })

    def test_json_file_overrides_every_field(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "runtime-logs"
            path = write_config(directory, {"logging": {
                "enabled": False,
                "directory": str(target),
                "level": "WARNING",
                "max_bytes": 4096,
                "backup_count": 2,
                "console": False,
            }})
            settings = BackendSettings.load(path, environ={})
        self.assertFalse(settings.logging.enabled)
        self.assertEqual(settings.logging.directory, target)
        self.assertEqual(settings.logging.level, "WARNING")
        self.assertEqual(settings.logging.max_bytes, 4096)
        self.assertEqual(settings.logging.backup_count, 2)
        self.assertFalse(settings.logging.console)

    def test_partial_logging_section_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(directory, {"logging": {"level": "ERROR"}})
            settings = BackendSettings.load(path, environ={})
        self.assertEqual(settings.logging.level, "ERROR")
        self.assertTrue(settings.logging.enabled)
        self.assertEqual(settings.logging.directory, default_log_directory())
        self.assertEqual(settings.logging.max_bytes, 5242880)
        self.assertTrue(settings.logging.console)

    def test_level_is_normalized_to_uppercase(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(directory, {"logging": {"level": "debug"}})
            settings = BackendSettings.load(path, environ={})
            self.assertEqual(settings.logging.level, "DEBUG")
            settings = BackendSettings.load(
                path, environ={"WINAPPSMANAGER_LOG_LEVEL": "warning "}
            )
            self.assertEqual(settings.logging.level, "WARNING")

    def test_relative_directory_resolves_against_the_backend_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(directory, {"logging": {"directory": "runtime-logs"}})
            settings = BackendSettings.load(path, environ={})
        self.assertEqual(settings.logging.directory, BASE_DIR / "runtime-logs")

    def test_null_directory_uses_the_workspace_default(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(directory, {"logging": {"directory": None}})
            settings = BackendSettings.load(path, environ={})
        self.assertEqual(settings.logging.directory, default_log_directory())

    def test_local_config_file_overrides_the_committed_section(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(directory, {"logging": {"level": "INFO", "backup_count": 5}})
            (Path(directory) / "config.local.json").write_text(
                json.dumps({"logging": {"level": "ERROR", "console": False}}),
                encoding="utf-8",
            )
            settings = BackendSettings.load(path, environ={})
        self.assertEqual(settings.logging.level, "ERROR")
        self.assertFalse(settings.logging.console)
        self.assertEqual(settings.logging.backup_count, 5)

    def test_environment_overrides_every_field(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "env-logs"
            path = write_config(directory, {"logging": {}})
            settings = BackendSettings.load(path, environ={
                "WINAPPSMANAGER_LOG_ENABLED": "false",
                "WINAPPSMANAGER_LOG_DIR": str(target),
                "WINAPPSMANAGER_LOG_LEVEL": "DEBUG",
                "WINAPPSMANAGER_LOG_MAX_BYTES": "2048",
                "WINAPPSMANAGER_LOG_BACKUP_COUNT": "3",
                "WINAPPSMANAGER_LOG_CONSOLE": "no",
            })
        self.assertFalse(settings.logging.enabled)
        self.assertEqual(settings.logging.directory, target)
        self.assertEqual(settings.logging.level, "DEBUG")
        self.assertEqual(settings.logging.max_bytes, 2048)
        self.assertEqual(settings.logging.backup_count, 3)
        self.assertFalse(settings.logging.console)

    def test_empty_environment_values_fall_back_to_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(directory, {"logging": {"level": "WARNING"}})
            settings = BackendSettings.load(
                path, environ={"WINAPPSMANAGER_LOG_LEVEL": "   ", "WINAPPSMANAGER_LOG_DIR": ""}
            )
        self.assertEqual(settings.logging.level, "WARNING")
        self.assertEqual(settings.logging.directory, default_log_directory())

    def test_invalid_file_values_are_rejected(self):
        invalid_payloads = (
            {"logging": {"level": "TRACE"}},
            {"logging": {"level": ""}},
            {"logging": {"level": 3}},
            {"logging": {"max_bytes": 1023}},
            {"logging": {"max_bytes": 0}},
            {"logging": {"max_bytes": -1}},
            {"logging": {"max_bytes": 1024.5}},
            {"logging": {"max_bytes": "1024"}},
            {"logging": {"backup_count": 0}},
            {"logging": {"backup_count": 51}},
            {"logging": {"backup_count": 1.5}},
            {"logging": {"backup_count": True}},
            {"logging": {"enabled": "yes"}},
            {"logging": {"enabled": 1}},
            {"logging": {"console": "no"}},
            {"logging": {"directory": 123}},
            {"logging": {"unknown": True}},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as directory:
                path = write_config(directory, payload)
                with self.assertRaises(ValueError):
                    BackendSettings.load(path, environ={})

    def test_invalid_environment_values_are_rejected(self):
        invalid_environments = (
            {"WINAPPSMANAGER_LOG_ENABLED": "maybe"},
            {"WINAPPSMANAGER_LOG_LEVEL": "TRACE"},
            {"WINAPPSMANAGER_LOG_MAX_BYTES": "1023"},
            {"WINAPPSMANAGER_LOG_MAX_BYTES": "many"},
            {"WINAPPSMANAGER_LOG_BACKUP_COUNT": "0"},
            {"WINAPPSMANAGER_LOG_BACKUP_COUNT": "51"},
            {"WINAPPSMANAGER_LOG_BACKUP_COUNT": "many"},
            {"WINAPPSMANAGER_LOG_CONSOLE": "maybe"},
        )
        for environ in invalid_environments:
            with self.subTest(environ=environ), tempfile.TemporaryDirectory() as directory:
                path = write_config(directory, {"logging": {}})
                with self.assertRaises(ValueError):
                    BackendSettings.load(path, environ=environ)

    def test_strict_validation_applies_to_direct_construction_and_assignment(self):
        with self.assertRaises(ValueError):
            LoggingConfig(level="TRACE")
        with self.assertRaises(ValueError):
            LoggingConfig(max_bytes=100)
        with self.assertRaises(ValueError):
            LoggingConfig(backup_count=51)
        with self.assertRaises(ValueError):
            LoggingConfig(directory="   ")
        settings = BackendSettings()
        with self.assertRaises(ValueError):
            settings.logging.backup_count = 0
        with self.assertRaises(ValueError):
            settings.logging.level = "verbose"


class LoggingConfigurationTests(unittest.TestCase):
    def setUp(self):
        shutdown_logging()
        self.temporary = tempfile.TemporaryDirectory(prefix="wam-logging-")
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(shutdown_logging)
        self.directory = Path(self.temporary.name) / "logs"
        self.log_file = self.directory / LOG_FILE_NAME

    def settings(self, **overrides):
        values = {"directory": self.directory, "console": False}
        values.update(overrides)
        return LoggingConfig(**values)

    def read_log(self, path=None):
        path = path or self.log_file
        return path.read_text(encoding="utf-8")

    def test_configure_writes_the_file_with_the_expected_format(self):
        configure_logging(self.settings(level="DEBUG"))
        logger = logging.getLogger("winappsmanager.test")
        logger.info("启动摘要：版本 %s", "1.0.0")
        logger.debug("调试信息")
        text = self.read_log()
        self.assertIn("INFO winappsmanager.test: 启动摘要：版本 1.0.0", text)
        self.assertIn("DEBUG winappsmanager.test: 调试信息", text)
        for line in text.splitlines():
            if line.strip():
                self.assertRegex(line, LOG_LINE_PATTERN)
        self.assertEqual(current_log_path(), self.log_file)
        summary = get_log_files()
        self.assertEqual(summary["file_count"], 1)
        self.assertGreater(summary["total_bytes"], 0)
        self.assertEqual(summary["current_path"], str(self.log_file))

    def test_root_level_follows_the_configuration(self):
        configure_logging(self.settings(level="WARNING"))
        logger = logging.getLogger("winappsmanager.level")
        logger.info("这条不应出现")
        logger.warning("这条应出现")
        text = self.read_log()
        self.assertNotIn("这条不应出现", text)
        self.assertIn("WARNING winappsmanager.level: 这条应出现", text)

    def test_uvicorn_loggers_share_the_same_file_and_write_each_line_once(self):
        configure_logging(self.settings())
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            logging.getLogger(name).info("uvicorn-message-%s-mark", name)
        text = self.read_log()
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            self.assertEqual(text.count(f"uvicorn-message-{name}-mark"), 1, text)
            uvicorn_logger = logging.getLogger(name)
            self.assertFalse(uvicorn_logger.propagate)
            self.assertEqual(len(managed_handlers(uvicorn_logger)), 1)

    def test_configure_is_idempotent_and_does_not_duplicate_handlers(self):
        configure_logging(self.settings())
        root = logging.getLogger()
        file_handlers = [handler for handler in root.handlers if isinstance(handler, RotatingFileHandler)]
        self.assertEqual(len(file_handlers), 1)
        self.assertEqual(len(managed_handlers(root)), 1)
        configure_logging(self.settings())
        file_handlers = [handler for handler in root.handlers if isinstance(handler, RotatingFileHandler)]
        self.assertEqual(len(file_handlers), 1)
        self.assertEqual(len(managed_handlers(root)), 1)
        logging.getLogger("winappsmanager.test").info("只应出现一次")
        self.assertEqual(self.read_log().count("只应出现一次"), 1)

    def test_disabled_logging_creates_no_directory_or_file(self):
        with contextlib.redirect_stderr(io.StringIO()):
            configure_logging(self.settings(enabled=False, console=False))
            logging.getLogger("winappsmanager.test").warning("没有 handler")
        self.assertEqual(managed_handlers(logging.getLogger()), [])
        self.assertFalse(self.directory.exists())
        self.assertFalse(self.log_file.exists())
        self.assertIsNone(current_log_path())
        summary = get_log_files(self.directory)
        self.assertEqual(summary["file_count"], 0)
        self.assertEqual(summary["total_bytes"], 0)
        self.assertIsNone(summary["current_path"])

    def test_a_broken_directory_falls_back_to_console_only(self):
        blocked = Path(self.temporary.name) / "not-a-directory"
        blocked.write_text("occupied", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()) as captured:
            configure_logging(self.settings(directory=blocked))
            logging.getLogger("winappsmanager.fallback").warning("降级后仍能输出")
        handlers = managed_handlers(logging.getLogger())
        self.assertEqual(len(handlers), 1)
        self.assertNotIsInstance(handlers[0], RotatingFileHandler)
        self.assertIsNone(current_log_path())
        self.assertIn("降级后仍能输出", captured.getvalue())
        self.assertIn("已降级为仅控制台输出", captured.getvalue())

    def test_a_degraded_setup_is_retried_by_a_later_call(self):
        blocked = Path(self.temporary.name) / "blocked"
        blocked.write_text("occupied", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            configure_logging(self.settings(directory=blocked))
        self.assertIsNone(current_log_path())
        blocked.unlink()
        configure_logging(self.settings(directory=blocked))
        self.assertEqual(current_log_path(), blocked / LOG_FILE_NAME)
        self.assertTrue((blocked / LOG_FILE_NAME).is_file())

    def test_rotation_creates_backup_files(self):
        configure_logging(self.settings(level="INFO", max_bytes=1024, backup_count=2))
        logger = logging.getLogger("winappsmanager.rotate")
        for index in range(40):
            logger.info("轮转测试消息 %02d %s", index, "x" * 100)
        self.assertTrue(self.log_file.is_file())
        self.assertTrue((self.directory / (LOG_FILE_NAME + ".1")).is_file())
        summary = get_log_files(self.directory)
        self.assertGreaterEqual(summary["file_count"], 2)
        self.assertGreater(summary["total_bytes"], 0)

    def test_reconfiguring_with_another_directory_replaces_the_handlers(self):
        configure_logging(self.settings())
        logging.getLogger("winappsmanager.first").info("first-directory")
        other = Path(self.temporary.name) / "other-logs"
        configure_logging(LoggingConfig(directory=other, console=False))
        logging.getLogger("winappsmanager.second").info("second-directory")
        self.assertIn("first-directory", self.read_log())
        self.assertNotIn("second-directory", self.read_log())
        self.assertIn("second-directory", (other / LOG_FILE_NAME).read_text(encoding="utf-8"))
        root = logging.getLogger()
        self.assertEqual(len(managed_handlers(root)), 1)
        self.assertEqual(len(managed_handlers(logging.getLogger("uvicorn.access"))), 1)

    def test_third_party_http_request_logs_are_kept_quiet(self):
        configure_logging(self.settings(level="DEBUG"))
        logger = logging.getLogger("httpx2")
        logger.info('HTTP Request: GET https://example.test/media?auth_key=secret "200 OK"')
        logger.warning("上游请求失败")
        text = self.read_log()
        self.assertNotIn("HTTP Request", text)
        self.assertNotIn("auth_key=secret", text)
        self.assertIn("WARNING httpx2: 上游请求失败", text)

    def test_shutdown_logging_removes_handlers_and_releases_the_file(self):
        configure_logging(self.settings())
        self.assertTrue(self.log_file.is_file())
        shutdown_logging()
        self.assertEqual(managed_handlers(logging.getLogger()), [])
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            self.assertEqual(managed_handlers(logging.getLogger(name)), [])
        self.assertIsNone(current_log_path())
        self.assertEqual(logging.getLogger("httpx2").level, logging.NOTSET)
        # handler 已关闭：Windows 上现在才能删除仍打开的文件。
        self.log_file.unlink()
        self.assertFalse(self.log_file.exists())

    def test_get_log_files_is_read_only_and_counts_rotated_backups(self):
        self.assertFalse(self.directory.exists())
        summary = get_log_files(self.directory)
        self.assertEqual(summary, {
            "directory": str(self.directory),
            "file_count": 0,
            "total_bytes": 0,
            "current_path": None,
        })
        self.assertFalse(self.directory.exists())

        self.directory.mkdir()
        (self.directory / LOG_FILE_NAME).write_bytes(b"abc")
        (self.directory / (LOG_FILE_NAME + ".1")).write_bytes(b"de")
        (self.directory / (LOG_FILE_NAME + ".2")).write_bytes(b"f")
        (self.directory / "unrelated.txt").write_bytes(b"ignored")
        summary = get_log_files(self.directory)
        self.assertEqual(summary["file_count"], 3)
        self.assertEqual(summary["total_bytes"], 6)
        self.assertEqual(summary["current_path"], str(self.log_file))

    def test_get_log_files_defaults_to_the_configured_directory(self):
        configure_logging(self.settings())
        logging.getLogger("winappsmanager.test").info("hello")
        summary = get_log_files()
        self.assertEqual(summary["directory"], str(self.directory))
        self.assertEqual(summary["file_count"], 1)


class CacheLoggingSummaryTests(unittest.TestCase):
    def setUp(self):
        shutdown_logging()
        self.temporary = tempfile.TemporaryDirectory(prefix="wam-logging-api-")
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(shutdown_logging)
        self.directory = Path(self.temporary.name) / "logs"

    def make_settings(self, *, logging_settings=None):
        return BackendSettings(
            cache=CacheConfig(persist=False),
            refresh=RefreshConfig(on_startup=False, interval_seconds=0),
            builtin=BuiltinConfig(enabled=False),
            lenovo=LenovoConfig(enabled=False),
            logging=logging_settings
            or LoggingConfig(directory=self.directory, level="INFO", console=False),
        )

    def test_cache_endpoint_reports_the_logging_summary_without_side_effects(self):
        secret = "ghp_" + "l" * 36
        settings = self.make_settings()
        settings.github = GithubConfig(token=secret)
        with TestClient(create_app(settings=settings)) as client:
            first = client.get("/api/v1/cache")
            self.assertEqual(first.status_code, 200)
            payload = first.json()
            summary = payload["logging"]
            self.assertTrue(summary["enabled"])
            self.assertEqual(summary["level"], "INFO")
            self.assertFalse(summary["console"])
            self.assertEqual(summary["directory"], str(self.directory))
            self.assertEqual(summary["file_count"], 1)
            self.assertGreater(summary["total_bytes"], 0)
            self.assertTrue(summary["current_path"].endswith(LOG_FILE_NAME))
            # 令牌仍然脱敏，也不会出现在诊断响应里。
            self.assertEqual(payload["settings"]["github"]["token"], "***")
            self.assertNotIn(secret, first.text)
            # 第二轮读取仍然只是统计：不新建文件、不轮转、不追加日志。
            second = client.get("/api/v1/cache")
            self.assertEqual(second.json()["logging"], summary)
            self.assertEqual(
                sorted(path.name for path in self.directory.iterdir()),
                [LOG_FILE_NAME],
            )
        text = (self.directory / LOG_FILE_NAME).read_text(encoding="utf-8")
        self.assertIn("后端启动", text)
        self.assertIn("后端已关闭", text)
        self.assertNotIn(secret, text)

    def test_cache_endpoint_reports_disabled_file_logging_without_creating_it(self):
        settings = self.make_settings(
            logging_settings=LoggingConfig(enabled=False, directory=self.directory, console=False)
        )
        with TestClient(create_app(settings=settings)) as client:
            summary = client.get("/api/v1/cache").json()["logging"]
        self.assertFalse(summary["enabled"])
        self.assertEqual(summary["file_count"], 0)
        self.assertEqual(summary["total_bytes"], 0)
        self.assertIsNone(summary["current_path"])
        self.assertFalse(self.directory.exists())

    def test_a_failing_logging_setup_does_not_prevent_the_service_from_starting(self):
        settings = self.make_settings()
        with mock.patch(
            "Backend.main.configure_logging", side_effect=RuntimeError("日志初始化失败")
        ), contextlib.redirect_stderr(io.StringIO()):
            with TestClient(create_app(settings=settings)) as client:
                self.assertEqual(client.get("/api/v1/health").status_code, 200)
                summary = client.get("/api/v1/cache").json()["logging"]
        self.assertTrue(summary["enabled"])
        self.assertEqual(summary["file_count"], 0)
        self.assertFalse(self.directory.exists())


if __name__ == "__main__":
    unittest.main()
