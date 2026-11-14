"""后端运行配置。

默认值来自本目录的 ``config.json``（文件不存在时使用内置默认值），随后由
``WINAPPSMANAGER_*`` 环境变量覆盖。把 ``WINAPPSMANAGER_CONFIG`` 指向其他文件即可
使用另一份配置。配置在服务启动时读取，修改后需要重启后端。
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_FILE = BASE_DIR / "config.json"
LOCAL_CONFIG_FILE_NAME = "config.local.json"
CONFIG_PATH_ENV = "WINAPPSMANAGER_CONFIG"
ENV_PREFIX = "WINAPPSMANAGER_"
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})

# 内置精选源白名单里的 app id，规则与 Backend/models.py 的 ResourceId 一致。
BuiltinAppId = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9.-]*$")]
# logging.level 允许的级别（大小写不敏感，加载后统一存为大写）。
LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR"})


def default_cache_directory() -> Path:
    """Return the cache directory used to persist release metadata.

    The cache lives next to the backend inside the working directory: this backend is a
    local companion process, so keeping its state in the checkout keeps the install
    self-contained (and writable even when the user profile is locked down). Override it
    with ``cache.directory`` / ``WINAPPSMANAGER_CACHE_DIR`` to move it elsewhere.
    """

    return BASE_DIR / ".cache"


def default_log_directory() -> Path:
    """Return the default directory for the rotated backend log file.

    Logs live with the rest of the generated backend state under ``Backend/.cache/logs``
    (git-ignored), so a crashed backend keeps a file to inspect after the console is gone.
    Override it with ``logging.directory`` / ``WINAPPSMANAGER_LOG_DIR``.
    """

    return default_cache_directory() / "logs"


class _ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True)


class CacheConfig(_ConfigModel):
    """Metadata cache limits. ``ttl_seconds`` is memory-only; the persistent TTL is separate.

    ``max_stale_seconds`` bounds how old an expired on-disk snapshot may be while it is
    still allowed to act as a fallback when the upstream provider cannot be scraped.
    """

    enabled: bool = True
    persist: bool = True
    directory: Path = Field(default_factory=default_cache_directory)
    ttl_seconds: float = 300.0
    persistent_ttl_seconds: float = 3600.0
    max_stale_seconds: float = 604800.0
    max_entries: int = 64

    @field_validator("directory", mode="before")
    @classmethod
    def resolve_directory(cls, value: Any) -> Path:
        if value is None:
            return default_cache_directory()
        if isinstance(value, Path):
            path = value
        elif isinstance(value, str):
            text = value.strip()
            if not text:
                raise ValueError("cache.directory 不能为空")
            path = Path(text).expanduser()
        else:
            raise ValueError("cache.directory 必须是路径字符串")
        return path if path.is_absolute() else (BASE_DIR / path)

    @model_validator(mode="after")
    def validate_limits(self) -> "CacheConfig":
        if type(self.ttl_seconds) not in (int, float) or not math.isfinite(self.ttl_seconds) or self.ttl_seconds < 0:
            raise ValueError("cache.ttl_seconds 必须是有限且非负的数字")
        if (
            type(self.persistent_ttl_seconds) not in (int, float)
            or not math.isfinite(self.persistent_ttl_seconds)
            or self.persistent_ttl_seconds < 0
        ):
            raise ValueError("cache.persistent_ttl_seconds 必须是有限且非负的数字")
        if type(self.max_entries) is not int or self.max_entries <= 0:
            raise ValueError("cache.max_entries 必须是正整数")
        if (
            type(self.max_stale_seconds) not in (int, float)
            or not math.isfinite(self.max_stale_seconds)
            or self.max_stale_seconds < 0
        ):
            raise ValueError("cache.max_stale_seconds 必须是有限且非负的数字")
        return self


class LoggingConfig(_ConfigModel):
    """后端轮转文件日志（``Backend/logging_config.py``）的参数。

    ``enabled`` 控制是否写文件；``console`` 控制是否同时输出到 stderr；``directory`` 是
    ``backend.log`` 所在目录，默认 ``Backend/.cache/logs``（相对路径按后端目录解析）；
    ``level`` 是 root logger 级别，``max_bytes`` / ``backup_count`` 控制按大小轮转（写满后
    改名为 ``backend.log.1``、``.2`` …）。目录不可写时 ``configure_logging`` 自动降级为
    仅控制台，不会阻止服务启动。
    """

    enabled: bool = True
    directory: Path = Field(default_factory=default_log_directory)
    level: str = "INFO"
    max_bytes: int = 5 * 1024 * 1024
    backup_count: int = 5
    console: bool = True

    @field_validator("directory", mode="before")
    @classmethod
    def resolve_directory(cls, value: Any) -> Path:
        if value is None:
            return default_log_directory()
        if isinstance(value, Path):
            path = value
        elif isinstance(value, str):
            text = value.strip()
            if not text:
                raise ValueError("logging.directory 不能为空")
            path = Path(text).expanduser()
        else:
            raise ValueError("logging.directory 必须是路径字符串")
        return path if path.is_absolute() else (BASE_DIR / path)

    @field_validator("level", mode="before")
    @classmethod
    def normalize_level(cls, value: Any) -> str:
        if value is None:
            return "INFO"
        if not isinstance(value, str):
            raise ValueError("logging.level 必须是字符串")
        text = value.strip().upper()
        if text not in LOG_LEVELS:
            raise ValueError("logging.level 必须是 DEBUG、INFO、WARNING 或 ERROR 之一")
        return text

    @model_validator(mode="after")
    def validate_limits(self) -> "LoggingConfig":
        if type(self.max_bytes) is not int or self.max_bytes < 1024:
            raise ValueError("logging.max_bytes 必须是不小于 1024 的整数")
        if type(self.backup_count) is not int or not 1 <= self.backup_count <= 50:
            raise ValueError("logging.backup_count 必须是 1 到 50 之间的整数")
        return self


class ProvidersConfig(_ConfigModel):
    cache_ttl_seconds: float = 600.0
    timeout_seconds: float = 10.0

    @model_validator(mode="after")
    def validate_limits(self) -> "ProvidersConfig":
        if (
            type(self.cache_ttl_seconds) not in (int, float)
            or not math.isfinite(self.cache_ttl_seconds)
            or self.cache_ttl_seconds < 0
        ):
            raise ValueError("providers.cache_ttl_seconds 必须是有限且非负的数字")
        if (
            type(self.timeout_seconds) not in (int, float)
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("providers.timeout_seconds 必须是有限正数")
        return self


class HttpConfig(_ConfigModel):
    timeout_seconds: float = 5.0
    connect_timeout_seconds: float = 3.0

    @model_validator(mode="after")
    def validate_limits(self) -> "HttpConfig":
        for name, value in (("timeout_seconds", self.timeout_seconds), ("connect_timeout_seconds", self.connect_timeout_seconds)):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"http.{name} 必须是有限正数")
        if self.connect_timeout_seconds > self.timeout_seconds:
            raise ValueError("http.connect_timeout_seconds 不能大于 http.timeout_seconds")
        return self


class RefreshConfig(_ConfigModel):
    """Startup crawl and scheduled re-crawl of provider-managed releases.

    ``on_startup`` scrapes every provider once before the backend starts serving requests
    (falling back to an on-disk snapshot when a provider fails). ``interval_seconds``
    re-runs that scrape periodically; ``0`` disables the scheduled pass.
    """

    on_startup: bool = True
    startup_timeout_seconds: float = 30.0
    interval_seconds: float = 1800.0
    jitter_seconds: float = 0.0
    max_concurrency: int = 4
    use_stale_cache: bool = True
    drop_unavailable_apps: bool = True

    @model_validator(mode="after")
    def validate_limits(self) -> "RefreshConfig":
        if (
            type(self.startup_timeout_seconds) not in (int, float)
            or not math.isfinite(self.startup_timeout_seconds)
            or self.startup_timeout_seconds <= 0
        ):
            raise ValueError("refresh.startup_timeout_seconds 必须是有限正数")
        for name, value in (("interval_seconds", self.interval_seconds), ("jitter_seconds", self.jitter_seconds)):
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"refresh.{name} 必须是有限且非负的数字")
        if self.jitter_seconds > self.interval_seconds:
            raise ValueError("refresh.jitter_seconds 不能大于 refresh.interval_seconds")
        if type(self.max_concurrency) is not int or not 1 <= self.max_concurrency <= 16:
            raise ValueError("refresh.max_concurrency 必须是 1 到 16 之间的整数")
        return self


class GithubConfig(_ConfigModel):
    """GitHub API access used by the generic releases provider.

    ``token`` is a credential: keep it in ``config.local.json`` (git-ignored) or in
    ``WINAPPSMANAGER_GITHUB_TOKEN``, never in the committed ``config.json``. Unauthenticated
    requests are limited to 60 per hour, an authenticated token allows 5000 per hour.

    ``refresh_interval_seconds`` bounds how long a forced refresh (startup warmup, scheduled
    pass, explicit ``refresh=true``) may reuse the on-disk conditional-request cache without
    contacting GitHub at all. ``0`` disables the reuse and always sends a conditional request
    (which normally answers ``304 Not Modified`` and does not consume API quota).
    """

    token: str = ""
    api_base: str = "https://api.github.com"
    timeout_seconds: float = 10.0
    refresh_interval_seconds: float = 900.0

    @field_validator("token", mode="before")
    @classmethod
    def validate_token(cls, value: Any) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ValueError("github.token 必须是字符串")
        token = value.strip()
        if "\n" in token or "\r" in token or " " in token or "\t" in token:
            raise ValueError("github.token 不能包含空白字符或换行")
        return token

    @field_validator("api_base", mode="before")
    @classmethod
    def validate_api_base(cls, value: Any) -> str:
        if value is None:
            return "https://api.github.com"
        if not isinstance(value, str):
            raise ValueError("github.api_base 必须是字符串")
        text = value.strip().rstrip("/")
        if not text.startswith("https://") or any(character.isspace() for character in text):
            raise ValueError("github.api_base 必须是无空白的 https 地址")
        return text

    @model_validator(mode="after")
    def validate_limits(self) -> "GithubConfig":
        if (
            type(self.timeout_seconds) not in (int, float)
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("github.timeout_seconds 必须是有限正数")
        if (
            type(self.refresh_interval_seconds) not in (int, float)
            or not math.isfinite(self.refresh_interval_seconds)
            or self.refresh_interval_seconds < 0
        ):
            raise ValueError("github.refresh_interval_seconds 必须是有限且非负的数字")
        return self


class LenovoConfig(_ConfigModel):
    """联想应用商店（``Backend/lenovo``）软件源的抓取与缓存参数。

    ``enabled`` 为 ``false`` 时不创建联想服务，``/api/v1/store/lenovo/*`` 返回 503，
    ``/api/v1/sources`` 把联想源标记为未启用。其余字段的单位都是秒，只有
    ``catalog_concurrency`` 与 ``image_cache_max_bytes`` 是整数；``timeout_seconds`` 是单次
    联想请求的总超时，连接超时固定为 5 秒。

    ``catalog_ttl_seconds`` 只决定响应里的 ``stale`` 标记（目录年龄超过该值时 ``stale=true``），
    不再触发请求路径抓取；``catalog_refresh_interval_seconds`` 是后台定时刷新间隔（默认 300
    秒 = 每 5 分钟更新一次 ``lenovo/catalog.json``），``0`` 关闭定时刷新。请求路径只读内存/磁盘
    快照，只有完全没有任何快照的冷启动才会同步抓取一次。

    这些值由 ``LenovoStoreService`` 按鸭子类型读取，所以只影响联想源，不影响内置精选源。
    """

    enabled: bool = True
    catalog_ttl_seconds: float = 21600.0
    catalog_refresh_interval_seconds: float = 300.0
    catalog_max_stale_seconds: float = 604800.0
    details_ttl_seconds: float = 900.0
    details_persistent_ttl_seconds: float = 21600.0
    search_cache_ttl_seconds: float = 60.0
    timeout_seconds: float = 20.0
    catalog_concurrency: int = 3
    image_cache_ttl_seconds: float = 2592000.0
    image_cache_max_bytes: int = 268435456

    @model_validator(mode="after")
    def validate_limits(self) -> "LenovoConfig":
        for name, value in (
            ("catalog_ttl_seconds", self.catalog_ttl_seconds),
            ("catalog_refresh_interval_seconds", self.catalog_refresh_interval_seconds),
            ("catalog_max_stale_seconds", self.catalog_max_stale_seconds),
            ("details_ttl_seconds", self.details_ttl_seconds),
            ("details_persistent_ttl_seconds", self.details_persistent_ttl_seconds),
            ("search_cache_ttl_seconds", self.search_cache_ttl_seconds),
            ("image_cache_ttl_seconds", self.image_cache_ttl_seconds),
        ):
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"lenovo.{name} 必须是有限且非负的数字")
        if (
            type(self.timeout_seconds) not in (int, float)
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("lenovo.timeout_seconds 必须是有限正数")
        if type(self.catalog_concurrency) is not int or not 1 <= self.catalog_concurrency <= 6:
            raise ValueError("lenovo.catalog_concurrency 必须是 1 到 6 之间的整数")
        if type(self.image_cache_max_bytes) is not int or self.image_cache_max_bytes <= 0:
            raise ValueError("lenovo.image_cache_max_bytes 必须是正整数")
        return self


class BuiltinConfig(_ConfigModel):
    """内置精选源（``data/apps.json`` + ``data/apps.d/*.json``）的启用范围。

    ``enabled`` 为 ``false`` 时不创建内置源的下载 provider、不做启动预热、不启动定时刷新，
    ``/api/v1/apps*`` 返回 503，``/api/v1/sources`` 把内置源标记为未启用。

    ``app_ids`` 是可选白名单：空列表表示目录里的全部软件，非空时只暴露这些 app id（数据条目
    仍完整加载并校验，不会删除任何目录文件）。未知 id 会在后端启动时让启动失败。
    """

    enabled: bool = True
    app_ids: Annotated[list[BuiltinAppId], Field(max_length=200)] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_app_ids(self) -> "BuiltinConfig":
        if len(self.app_ids) != len(set(self.app_ids)):
            raise ValueError("builtin.app_ids 不能包含重复的 app id")
        return self


class BackendSettings(_ConfigModel):
    cache: CacheConfig = Field(default_factory=CacheConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    http: HttpConfig = Field(default_factory=HttpConfig)
    refresh: RefreshConfig = Field(default_factory=RefreshConfig)
    github: GithubConfig = Field(default_factory=GithubConfig)
    lenovo: LenovoConfig = Field(default_factory=LenovoConfig)
    builtin: BuiltinConfig = Field(default_factory=BuiltinConfig)

    @classmethod
    def load(
        cls,
        config_file: Path | str | None = None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> "BackendSettings":
        """Load defaults, then the JSON file, then environment overrides."""

        env = os.environ if environ is None else environ
        explicit_path = config_file is not None or bool(env.get(CONFIG_PATH_ENV))
        if config_file is None:
            configured = env.get(CONFIG_PATH_ENV)
            path = Path(configured).expanduser() if configured else DEFAULT_CONFIG_FILE
        else:
            path = Path(config_file).expanduser()
        raw = _read_config_file(path, required=explicit_path)
        # Machine-local secrets (for example the GitHub token) live next to the config
        # file in ``config.local.json``, which is git-ignored; it overrides config.json.
        local = path.parent / LOCAL_CONFIG_FILE_NAME
        if local.resolve() != path.resolve():
            raw = _deep_merge(raw, _read_config_file(local, required=False))
        raw = _apply_environment(raw, env)
        return cls.model_validate(raw)


def _read_config_file(path: Path, *, required: bool) -> dict[str, Any]:
    if not path.is_file():
        if required:
            raise ValueError(f"配置文件不存在: {path}")
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"配置文件格式无效: {path}: {error}") from error
    except OSError as error:
        raise ValueError(f"无法读取配置文件: {path}: {error}") from error
    if not isinstance(raw, dict):
        raise ValueError(f"配置文件必须包含 JSON 对象: {path}")
    return raw


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Merge nested configuration objects, with ``overlay`` winning on conflicts."""

    merged = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _environment_value(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(ENV_PREFIX + name)
    if value is None:
        return None
    text = value.strip()
    return text or None


def _parse_bool(value: str, name: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ValueError(f"{ENV_PREFIX}{name} 必须是 true/false、yes/no、on/off 或 1/0")


def _parse_number(value: str, name: str) -> float:
    try:
        number = float(value)
    except ValueError as error:
        raise ValueError(f"{ENV_PREFIX}{name} 必须是数字") from error
    if not math.isfinite(number):
        raise ValueError(f"{ENV_PREFIX}{name} 必须是有限数字")
    return number


def _parse_integer(value: str, name: str) -> int:
    try:
        return int(value)
    except ValueError as error:
        raise ValueError(f"{ENV_PREFIX}{name} 必须是整数") from error


def _apply_environment(raw: dict[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    cache: dict[str, Any] = {}
    logging_values: dict[str, Any] = {}
    providers: dict[str, Any] = {}
    http: dict[str, Any] = {}
    refresh: dict[str, Any] = {}
    github: dict[str, Any] = {}
    lenovo: dict[str, Any] = {}
    builtin: dict[str, Any] = {}

    value = _environment_value(env, "CACHE_ENABLED")
    if value is not None:
        cache["enabled"] = _parse_bool(value, "CACHE_ENABLED")
    value = _environment_value(env, "CACHE_PERSIST")
    if value is not None:
        cache["persist"] = _parse_bool(value, "CACHE_PERSIST")
    value = _environment_value(env, "CACHE_DIR")
    if value is not None:
        cache["directory"] = value
    value = _environment_value(env, "CACHE_TTL_SECONDS")
    if value is not None:
        cache["ttl_seconds"] = _parse_number(value, "CACHE_TTL_SECONDS")
    value = _environment_value(env, "CACHE_PERSISTENT_TTL_SECONDS")
    if value is not None:
        cache["persistent_ttl_seconds"] = _parse_number(value, "CACHE_PERSISTENT_TTL_SECONDS")
    value = _environment_value(env, "CACHE_MAX_ENTRIES")
    if value is not None:
        cache["max_entries"] = _parse_integer(value, "CACHE_MAX_ENTRIES")
    value = _environment_value(env, "CACHE_MAX_STALE_SECONDS")
    if value is not None:
        cache["max_stale_seconds"] = _parse_number(value, "CACHE_MAX_STALE_SECONDS")

    value = _environment_value(env, "LOG_ENABLED")
    if value is not None:
        logging_values["enabled"] = _parse_bool(value, "LOG_ENABLED")
    value = _environment_value(env, "LOG_DIR")
    if value is not None:
        logging_values["directory"] = value
    value = _environment_value(env, "LOG_LEVEL")
    if value is not None:
        logging_values["level"] = value
    value = _environment_value(env, "LOG_MAX_BYTES")
    if value is not None:
        logging_values["max_bytes"] = _parse_integer(value, "LOG_MAX_BYTES")
    value = _environment_value(env, "LOG_BACKUP_COUNT")
    if value is not None:
        logging_values["backup_count"] = _parse_integer(value, "LOG_BACKUP_COUNT")
    value = _environment_value(env, "LOG_CONSOLE")
    if value is not None:
        logging_values["console"] = _parse_bool(value, "LOG_CONSOLE")

    value = _environment_value(env, "PROVIDER_CACHE_TTL_SECONDS")
    if value is not None:
        providers["cache_ttl_seconds"] = _parse_number(value, "PROVIDER_CACHE_TTL_SECONDS")
    value = _environment_value(env, "PROVIDER_TIMEOUT_SECONDS")
    if value is not None:
        providers["timeout_seconds"] = _parse_number(value, "PROVIDER_TIMEOUT_SECONDS")

    value = _environment_value(env, "HTTP_TIMEOUT_SECONDS")
    if value is not None:
        http["timeout_seconds"] = _parse_number(value, "HTTP_TIMEOUT_SECONDS")
    value = _environment_value(env, "HTTP_CONNECT_TIMEOUT_SECONDS")
    if value is not None:
        http["connect_timeout_seconds"] = _parse_number(value, "HTTP_CONNECT_TIMEOUT_SECONDS")

    for name, key, parser in (
        ("REFRESH_ON_STARTUP", "on_startup", _parse_bool),
        ("REFRESH_STARTUP_TIMEOUT_SECONDS", "startup_timeout_seconds", _parse_number),
        ("REFRESH_INTERVAL_SECONDS", "interval_seconds", _parse_number),
        ("REFRESH_JITTER_SECONDS", "jitter_seconds", _parse_number),
        ("REFRESH_MAX_CONCURRENCY", "max_concurrency", _parse_integer),
        ("REFRESH_USE_STALE_CACHE", "use_stale_cache", _parse_bool),
        ("REFRESH_DROP_UNAVAILABLE_APPS", "drop_unavailable_apps", _parse_bool),
    ):
        value = _environment_value(env, name)
        if value is not None:
            refresh[key] = parser(value, name)

    value = _environment_value(env, "GITHUB_TOKEN")
    if value is not None:
        github["token"] = value
    value = _environment_value(env, "GITHUB_API_BASE")
    if value is not None:
        github["api_base"] = value
    value = _environment_value(env, "GITHUB_TIMEOUT_SECONDS")
    if value is not None:
        github["timeout_seconds"] = _parse_number(value, "GITHUB_TIMEOUT_SECONDS")
    value = _environment_value(env, "GITHUB_REFRESH_INTERVAL_SECONDS")
    if value is not None:
        github["refresh_interval_seconds"] = _parse_number(value, "GITHUB_REFRESH_INTERVAL_SECONDS")

    value = _environment_value(env, "LENOVO_ENABLED")
    if value is not None:
        lenovo["enabled"] = _parse_bool(value, "LENOVO_ENABLED")
    for name, key, parser in (
        ("LENOVO_CATALOG_TTL_SECONDS", "catalog_ttl_seconds", _parse_number),
        (
            "LENOVO_CATALOG_REFRESH_INTERVAL_SECONDS",
            "catalog_refresh_interval_seconds",
            _parse_number,
        ),
        ("LENOVO_CATALOG_MAX_STALE_SECONDS", "catalog_max_stale_seconds", _parse_number),
        ("LENOVO_DETAILS_TTL_SECONDS", "details_ttl_seconds", _parse_number),
        ("LENOVO_DETAILS_PERSISTENT_TTL_SECONDS", "details_persistent_ttl_seconds", _parse_number),
        ("LENOVO_SEARCH_CACHE_TTL_SECONDS", "search_cache_ttl_seconds", _parse_number),
        ("LENOVO_TIMEOUT_SECONDS", "timeout_seconds", _parse_number),
        ("LENOVO_CATALOG_CONCURRENCY", "catalog_concurrency", _parse_integer),
        ("LENOVO_IMAGE_CACHE_TTL_SECONDS", "image_cache_ttl_seconds", _parse_number),
        ("LENOVO_IMAGE_CACHE_MAX_BYTES", "image_cache_max_bytes", _parse_integer),
    ):
        value = _environment_value(env, name)
        if value is not None:
            lenovo[key] = parser(value, name)

    value = _environment_value(env, "BUILTIN_ENABLED")
    if value is not None:
        builtin["enabled"] = _parse_bool(value, "BUILTIN_ENABLED")
    raw_app_ids = env.get(ENV_PREFIX + "BUILTIN_APP_IDS")
    if raw_app_ids is not None:
        # 显式空串表示空列表（= 不限制，暴露目录里的全部软件）；只有变量未设置时才保留文件值。
        builtin["app_ids"] = [item.strip() for item in raw_app_ids.split(",") if item.strip()]

    merged = dict(raw)
    for section, values in (
        ("cache", cache),
        ("logging", logging_values),
        ("providers", providers),
        ("http", http),
        ("refresh", refresh),
        ("github", github),
        ("lenovo", lenovo),
        ("builtin", builtin),
    ):
        if not values:
            continue
        current = merged.get(section)
        if current is None:
            current = {}
        if not isinstance(current, dict):
            raise ValueError(f"配置项 {section} 必须是对象")
        merged[section] = {**current, **values}
    return merged
