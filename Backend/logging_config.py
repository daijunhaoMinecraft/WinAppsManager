"""后端轮转文件日志的配置与生命周期管理。

``Backend/main.py`` 在 lifespan 最开始时调用 :func:`configure_logging`，把 ``logging``
根 logger 配置成“轮转文件 + 控制台”双通道：

* 文件默认写到 ``Backend/.cache/logs/backend.log``（UTF-8），目录不存在时自动创建；
* 单文件写满 ``logging.max_bytes``（默认 5 MB）后改名为 ``backend.log.1``，最多保留
  ``logging.backup_count``（默认 5）个备份；
* 格式统一为 ``时间 级别 logger: 消息``，root 级别取 ``logging.level``；
* 写盘失败（目录不可创建、磁盘只读、文件被占用等）只记录一条错误并降级为“仅控制台”，
  绝不让日志初始化影响服务启动。

uvicorn 的 ``uvicorn`` / ``uvicorn.error`` / ``uvicorn.access`` 三个 logger 默认
``propagate=False``，并且各自装着自己的 handler（``uvicorn.access`` 直接写 stdout，
``uvicorn.error`` 的启动/关闭消息写 stderr）。本模块统一接管这三个 logger：移除它们原有
的 handler、改装本模块的同一组 handler 并保持 ``propagate=False``。这样 uvicorn 的访问
日志和错误日志会写进同一个 ``backend.log``，并且每条消息只落一次文件、只打印一次控制台。
调用方用 ``uvicorn.run(..., log_config=None)`` 启动时（``python main.py`` 走的就是这条路）
uvicorn 不会再装自己的 handler，本模块是唯一输出通道。

本模块只做显式调用：import 不创建目录、不添加 handler。测试与进程退出用
:func:`shutdown_logging` 关闭 handler，诊断用 :func:`get_log_files` 统计落盘文件（只读，
不产生副作用）。
"""

from __future__ import annotations

import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 仅为类型检查导入，避免把 config 拉进模块导入链
    if __package__:
        from .config import LoggingConfig
    else:
        from config import LoggingConfig


#: 当前日志文件名（轮转后的备份依次是 ``backend.log.1``、``backend.log.2`` …）。
LOG_FILE_NAME = "backend.log"
#: 日志行格式：时间、级别、logger 名、消息。
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
#: 与 LOG_FORMAT 配套的时间格式。
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
#: uvicorn 自己维护、默认 propagate=False 的 logger；本模块统一接管它们的 handler。
UVICORN_LOGGER_NAMES = ("uvicorn", "uvicorn.error", "uvicorn.access")
#: 第三方 HTTP 客户端的逐请求 INFO 日志（httpx2 会打印完整上游 URL）对本地后端价值不大，
#: 还可能把带临时参数的媒体地址写进日志；统一压到 WARNING，异常仍然记录。
QUIET_LOGGER_NAMES = ("httpx", "httpx2", "httpcore")

_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}
_MANAGED_ATTRIBUTE = "_winappsmanager_managed"

# 全局状态：配置签名用于幂等判断，handlers 记录本模块添加的 handler 以便关闭/替换。
_lock = threading.RLock()
_configured = False
_degraded = False
_signature: tuple[Any, ...] | None = None
_directory: Path | None = None
_log_file: Path | None = None
_handlers: list[logging.Handler] = []


def configure_logging(settings: LoggingConfig) -> None:
    """按 ``logging`` 配置段初始化根 logger 与 uvicorn logger。

    幂等：同一组配置重复调用不会重复添加 handler，只会重新应用一次级别（uvicorn 启动时
    会用 ``--log-level`` 改写 ``uvicorn.*`` 的级别，需要这里再覆盖回去）。配置变化时先
    关闭旧 handler 再按新配置重建，因此测试可以多次传入不同的临时目录。

    参数是 ``Backend/config.py`` 的 ``LoggingConfig``（已由配置模型校验过取值范围）；
    本函数自身不抛异常——文件日志不可用时降级为仅控制台，服务照常启动。
    """

    global _configured, _degraded, _signature, _directory, _log_file, _handlers

    level_name = str(settings.level).strip().upper()
    level = _LEVELS.get(level_name, logging.INFO)
    enabled = bool(settings.enabled)
    directory = Path(settings.directory)
    max_bytes = int(settings.max_bytes)
    backup_count = int(settings.backup_count)
    console = bool(settings.console)
    signature: tuple[Any, ...] = (
        enabled,
        str(directory),
        level_name,
        max_bytes,
        backup_count,
        console,
    )

    with _lock:
        root = logging.getLogger()
        if _configured and _signature == signature and not _degraded:
            # 重复调用：handler 已经就绪，只重新对齐级别（见 docstring）。上一次若因目录
            # 不可写而降级，则继续走下面的重建流程再试一次（例如启动后磁盘恢复可写）。
            _apply_levels(level)
            return

        _remove_handlers_locked()

        _signature = signature
        _directory = directory
        _log_file = None

        formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
        handlers: list[logging.Handler] = []
        file_handler: logging.Handler | None = None
        file_error: BaseException | None = None

        if enabled:
            path = directory / LOG_FILE_NAME
            try:
                directory.mkdir(parents=True, exist_ok=True)
                file_handler = RotatingFileHandler(
                    path,
                    maxBytes=max_bytes,
                    backupCount=backup_count,
                    encoding="utf-8",
                )
                file_handler.setFormatter(formatter)
                _log_file = path
            except Exception as error:  # noqa: BLE001 - 日志初始化失败不能阻止服务启动
                file_error = error
                file_handler = None

        console_handler: logging.Handler | None = None
        # 文件日志不可用时，无论 console 配置如何都保留控制台输出，避免日志彻底消失。
        if console or (enabled and file_handler is None):
            console_handler = logging.StreamHandler(sys.stderr)
            console_handler.setFormatter(formatter)

        for handler in (file_handler, console_handler):
            if handler is None:
                continue
            setattr(handler, _MANAGED_ATTRIBUTE, True)
            handlers.append(handler)
        for handler in handlers:
            handler.setLevel(level)
            root.addHandler(handler)
        root.setLevel(level)
        _handlers = handlers
        _configured = True
        _degraded = file_error is not None
        _configure_uvicorn_loggers(handlers, level)
        _quiet_noisy_loggers()

        if file_error is not None:
            logging.getLogger("winappsmanager.logging").error(
                "无法初始化轮转文件日志 %s，已降级为仅控制台输出。",
                directory / LOG_FILE_NAME,
                exc_info=file_error,
            )


def shutdown_logging() -> None:
    """移除并关闭本模块添加的 handler（测试与进程退出用；幂等）。"""

    global _directory, _log_file
    with _lock:
        _remove_handlers_locked()
        _directory = None
        _log_file = None
        # 恢复第三方 logger 与 root 的默认级别，避免进程后续输出被之前的配置放大。
        for name in QUIET_LOGGER_NAMES:
            logging.getLogger(name).setLevel(logging.NOTSET)
        for name in UVICORN_LOGGER_NAMES:
            uvicorn_logger = logging.getLogger(name)
            uvicorn_logger.setLevel(logging.NOTSET)
            uvicorn_logger.propagate = True
        logging.getLogger().setLevel(logging.WARNING)


def current_log_path() -> Path | None:
    """返回当前生效的日志文件路径；文件日志未启用或初始化失败时返回 ``None``。"""

    with _lock:
        path = _log_file
    if path is None or not path.is_file():
        return None
    return path


def get_log_files(directory: Path | str | None = None) -> dict[str, object]:
    """只读统计日志目录：文件数、总字节数与当前日志文件路径，供诊断接口使用。

    ``directory`` 省略时使用最近一次 :func:`configure_logging` 的目录。不创建目录、不写
    文件；目录不存在或不可读时返回 0 计数（``current_path`` 为 ``None``）。
    """

    if directory is None:
        with _lock:
            base = _directory
    else:
        base = Path(directory)

    summary: dict[str, object] = {
        "directory": str(base) if base is not None else None,
        "file_count": 0,
        "total_bytes": 0,
        "current_path": None,
    }
    if base is None:
        return summary
    try:
        if not base.is_dir():
            return summary
        files = sorted(
            path
            for path in base.iterdir()
            if path.is_file()
            and (path.name == LOG_FILE_NAME or path.name.startswith(LOG_FILE_NAME + "."))
        )
        total_bytes = 0
        for path in files:
            try:
                total_bytes += path.stat().st_size
            except OSError:
                continue
    except OSError:
        return summary
    summary["file_count"] = len(files)
    summary["total_bytes"] = total_bytes
    current = base / LOG_FILE_NAME
    if current.is_file():
        summary["current_path"] = str(current)
    return summary


def _apply_levels(level: int) -> None:
    """把配置级别重新应用到 root 与 uvicorn logger（handler 不动）。"""

    logging.getLogger().setLevel(level)
    _quiet_noisy_loggers()
    for name in UVICORN_LOGGER_NAMES:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.setLevel(level)
        uvicorn_logger.propagate = False


def _quiet_noisy_loggers() -> None:
    """压掉第三方 HTTP 客户端的逐请求日志，避免噪声和上游 URL 落盘。"""

    for name in QUIET_LOGGER_NAMES:
        logging.getLogger(name).setLevel(logging.WARNING)


def _configure_uvicorn_loggers(handlers: list[logging.Handler], level: int) -> None:
    """用同一组 handler 接管 uvicorn logger，并阻止它们再冒泡到 root。

    uvicorn 默认在 ``uvicorn`` / ``uvicorn.access`` 上装自己的控制台 handler，保留它们会让
    同一行既被 uvicorn 打印、又被本模块打印；``uvicorn.error`` 没有 handler，靠
    ``propagate`` 找到 ``uvicorn`` 的 handler，如果这里只加 handler 不改 propagate，同一条
    记录会被父 logger 的 handler 再写一次文件。所以统一做法是：移除 uvicorn 的旧 handler、
    装上本模块的 handler、``propagate=False``。
    """

    for name in UVICORN_LOGGER_NAMES:
        uvicorn_logger = logging.getLogger(name)
        for existing in list(uvicorn_logger.handlers):
            if not getattr(existing, _MANAGED_ATTRIBUTE, False):
                uvicorn_logger.removeHandler(existing)
        for handler in handlers:
            uvicorn_logger.addHandler(handler)
        uvicorn_logger.setLevel(level)
        uvicorn_logger.propagate = False


def _remove_handlers_locked() -> None:
    """从 root/uvicorn logger 上摘掉并关闭本模块添加的全部 handler。"""

    global _configured, _degraded, _signature, _handlers

    root = logging.getLogger()
    loggers = [root, *(logging.getLogger(name) for name in UVICORN_LOGGER_NAMES)]
    for logger in loggers:
        for handler in list(logger.handlers):
            if getattr(handler, _MANAGED_ATTRIBUTE, False):
                logger.removeHandler(handler)
    for handler in _handlers:
        try:
            handler.close()
        except Exception:  # noqa: BLE001 - 关闭旧 handler 失败不应影响重新配置
            pass
    _handlers = []
    _signature = None
    _configured = False
    _degraded = False
