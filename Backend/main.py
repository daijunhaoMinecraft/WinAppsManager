import asyncio
from collections.abc import Mapping
from contextlib import AsyncExitStack, asynccontextmanager
import logging
import os
from pathlib import Path
import time
from types import SimpleNamespace
from typing import Annotated, Any, Literal

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, Response
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
import httpx2

if __package__:
    from .cache import ReleaseMetadataCache
    from .catalog import CatalogRepository
    from .config import BackendSettings, RefreshConfig
    from .icons import CachedIconStaticFiles, IconService
    from .lenovo import create_lenovo_store_service
    from .lenovo.media import BROWSER_USER_AGENT
    from .lenovo.routes import create_lenovo_router
    from .soft360 import create_soft360_store_service
    from .soft360.media import BROWSER_USER_AGENT as SOFT360_BROWSER_USER_AGENT
    from .soft360.routes import create_soft360_router
    from .unified import create_source_registry, create_unified_router
    from .logging_config import configure_logging, current_log_path, get_log_files, shutdown_logging
    from .models import AppCollection, AppDefinition, AppResponse, Architecture, HealthResponse, Release, ReleaseCollection
    from .persistence import PersistentMetadataStore
    from .providers.base import ProviderError, ProviderTimeoutError, ReleaseProvider
    from .providers import GithubResponseCache, create_release_providers
    from .releases import ReleaseService
    from .scheduler import PeriodicRefresher
    from .warmup import CatalogWarmup
else:
    from cache import ReleaseMetadataCache
    from catalog import CatalogRepository
    from config import BackendSettings, RefreshConfig
    from icons import CachedIconStaticFiles, IconService
    from lenovo import create_lenovo_store_service
    from lenovo.media import BROWSER_USER_AGENT
    from lenovo.routes import create_lenovo_router
    from soft360 import create_soft360_store_service
    from soft360.media import BROWSER_USER_AGENT as SOFT360_BROWSER_USER_AGENT
    from soft360.routes import create_soft360_router
    from unified import create_source_registry, create_unified_router
    from logging_config import configure_logging, current_log_path, get_log_files, shutdown_logging
    from models import AppCollection, AppDefinition, AppResponse, Architecture, HealthResponse, Release, ReleaseCollection
    from persistence import PersistentMetadataStore
    from providers.base import ProviderError, ProviderTimeoutError, ReleaseProvider
    from providers import GithubResponseCache, create_release_providers
    from releases import ReleaseService
    from scheduler import PeriodicRefresher
    from warmup import CatalogWarmup


logger = logging.getLogger("winappsmanager.main")

APP_VERSION = "1.0.0"
DEFAULT_HOST = os.environ.get("WINAPPSMANAGER_HOST", "127.0.0.1").strip() or "127.0.0.1"
try:
    DEFAULT_PORT = int(os.environ.get("WINAPPSMANAGER_PORT", "8080"))
except ValueError:
    raise RuntimeError("WINAPPSMANAGER_PORT 必须是整数。") from None
if not 1 <= DEFAULT_PORT <= 65535:
    raise RuntimeError("WINAPPSMANAGER_PORT 必须在 1–65535 之间。")
BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data" / "apps.json"
ICON_DIR = BASE_DIR / "icon"

# 内置精选源（原有 /apps 接口）在 /sources 里的展示信息由 unified.builtin_adapter 提供；
# 这里只保留禁用时 /api/v1/apps* 的 503 文案。
BUILTIN_SOURCE_DISABLED_DETAIL = "内置精选源未启用。"
# 联想源的专用 HTTP 客户端连接超时（设计文档 3.6 固定 5 秒，总超时用 lenovo.timeout_seconds）。
LENOVO_CONNECT_TIMEOUT_SECONDS = 5.0
# 360 源的专用 HTTP 客户端连接超时（总超时用 soft360.timeout_seconds）。
SOFT360_CONNECT_TIMEOUT_SECONDS = 5.0


def _public_settings(settings: BackendSettings) -> dict:
    """Serialize the effective settings for diagnostics without exposing credentials."""

    data = settings.model_dump(mode="json")
    github = data.get("github")
    if isinstance(github, dict):
        token = github.get("token") or ""
        github["token"] = "***" if token else ""
        github["token_configured"] = bool(token)
    return data


def _builtin_source_log_summary(settings: BackendSettings) -> str:
    """内置精选源的启用状态，只用于启动摘要（不含任何敏感配置）。"""

    builtin = settings.builtin
    if not builtin.enabled:
        return "内置精选源未启用"
    if not builtin.app_ids:
        return "内置精选源已启用（全部软件）"
    return f"内置精选源已启用（白名单 {len(builtin.app_ids)} 款）"


def _logging_target(settings: BackendSettings) -> str:
    """启动摘要里的日志落点描述；不返回 handler/令牌等实现细节。"""

    logging_settings = settings.logging
    if not logging_settings.enabled:
        return "文件日志已禁用（仅控制台）" if logging_settings.console else "日志已禁用"
    active = current_log_path()
    if active is None:
        return f"文件日志不可写，已降级为仅控制台（级别 {logging_settings.level}）"
    return f"{active}（级别 {logging_settings.level}）"


def _logging_summary(settings: BackendSettings) -> dict[str, Any]:
    """``GET /api/v1/cache`` 的日志摘要：只读统计，不创建目录/文件、不触发任何副作用。"""

    logging_settings = settings.logging
    files = get_log_files(logging_settings.directory)
    return {
        "enabled": logging_settings.enabled,
        "level": logging_settings.level,
        "console": logging_settings.console,
        "directory": str(logging_settings.directory),
        "current_path": files["current_path"],
        "file_count": files["file_count"],
        "total_bytes": files["total_bytes"],
    }


async def _warm_catalog(warmup: CatalogWarmup, refresh_settings: RefreshConfig) -> None:
    """Scrape every provider once before the API starts accepting requests.

    A single slow upstream must not delay startup forever, hence the overall budget; a
    provider that failed leaves its app to the per-app availability table, where the
    persistent snapshot is used when possible and the app is hidden otherwise.
    """

    try:
        report = await asyncio.wait_for(
            warmup.warm_all(), timeout=refresh_settings.startup_timeout_seconds
        )
    except asyncio.TimeoutError:
        logger.warning(
            "启动预热在 %.1f 秒内未完成，先使用已获取的数据启动服务，其余软件按需抓取。",
            refresh_settings.startup_timeout_seconds,
        )
        return
    except Exception:  # A warmer bug must not prevent the API from starting.
        logger.exception("启动预热失败，先启动服务并按需抓取。")
        return
    summary = report.summary()
    logger.info(
        "启动预热完成：成功 %s，复用持久缓存 %s，不可用 %s，用时 %.2f 秒。",
        summary["ok"],
        summary["cached"],
        summary["unavailable"],
        summary["duration_seconds"],
    )


def create_app(
    data_file: Path = DATA_FILE,
    icon_directory: Path = ICON_DIR,
    release_providers: Mapping[str, ReleaseProvider] | None = None,
    settings: BackendSettings | None = None,
    extra_directory: Path | None = None,
    lenovo_service: Any | None = None,
    soft360_service: Any | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(api: FastAPI):
        configured = settings if settings is not None else BackendSettings.load()
        api.state.settings = configured
        started_at = time.monotonic()
        # 日志必须在最早期初始化：启动预热等 INFO 日志才有落点；初始化失败只降级，
        # 不能让日志问题阻止服务启动。
        try:
            configure_logging(configured.logging)
        except Exception:  # noqa: BLE001 - 日志初始化不能影响服务启动
            logger.warning("初始化文件日志失败，继续使用默认日志输出。", exc_info=True)
        logger.info(
            "后端启动：版本 %s，默认监听 %s:%d（uvicorn 启动参数可覆盖），缓存目录 %s，%s，联想应用商店源%s，360 软件源%s，日志 %s。",
            APP_VERSION,
            DEFAULT_HOST,
            DEFAULT_PORT,
            configured.cache.directory,
            _builtin_source_log_summary(configured),
            "已启用" if configured.lenovo.enabled else "未启用",
            "已启用" if configured.soft360.enabled else "未启用",
            _logging_target(configured),
        )
        # Catalog and resource references are validated before accepting requests.
        async with AsyncExitStack() as stack:
            # 内置精选源：完整目录仍会加载并校验，但 app_ids 白名单之外的软件不会暴露，
            # 也不会创建/预热它们对应的 provider（数据条目原样保留，不删除任何文件）。
            builtin_settings = configured.builtin
            builtin_enabled = builtin_settings.enabled
            allowed_app_ids = None if not builtin_settings.app_ids else tuple(builtin_settings.app_ids)
            api.state.catalog = CatalogRepository(
                data_file,
                icon_directory,
                extra_directory,
                allowed_app_ids=allowed_app_ids,
            )
            # 联想应用商店源：只在 lenovo.enabled 时创建服务。启动阶段只预加载本地
            # lenovo/catalog.json（不联网）并启动每 5 分钟的本地目录刷新；请求路径只读内存/
            # 磁盘快照，完全没有任何快照的冷启动才会同步抓取一次。
            api.state.lenovo = None
            lenovo_created = False
            if configured.lenovo.enabled:
                if lenovo_service is not None:
                    # 测试注入的假服务：生命周期由调用方管理，shutdown 时不要关闭它。
                    api.state.lenovo = lenovo_service
                else:
                    lenovo_timeout = httpx2.Timeout(
                        configured.lenovo.timeout_seconds,
                        connect=LENOVO_CONNECT_TIMEOUT_SECONDS,
                    )
                    # 联想图片 CDN 会拒绝 curl 类 UA，接口与图片统一用浏览器 UA；trust_env 保持默认
                    # （True），让系统代理/环境变量代理生效。
                    lenovo_client = await stack.enter_async_context(httpx2.AsyncClient(
                        timeout=lenovo_timeout,
                        follow_redirects=True,
                        headers={"User-Agent": BROWSER_USER_AGENT},
                    ))
                    api.state.lenovo = create_lenovo_store_service(
                        lenovo_client,
                        configured.cache.directory,
                        configured,
                        cache_enabled=configured.cache.enabled,
                        cache_persist=configured.cache.persist,
                    )
                    lenovo_created = True
                    # 启动失败（例如后续 provider/目录校验抛错）时 AsyncExitStack 也会关闭
                    # 联想服务，停止后台刷新线程，避免 daemon 线程泄漏；正常关闭时 finally
                    # 已经 shutdown 过，这里再调用是幂等的 no-op。
                    stack.push_async_callback(api.state.lenovo.shutdown)
            if api.state.lenovo is not None:
                # 启动只预加载本地 lenovo/catalog.json 到内存（不联网）；没有快照时留给首次
                # 请求按冷启动兜底抓取。注入的假服务可能没有这个方法（getattr 兼容），异常
                # 也绝不能阻止启动。
                preload_catalog = getattr(api.state.lenovo, "preload_catalog", None)
                if callable(preload_catalog):
                    try:
                        await preload_catalog()
                    except Exception:  # noqa: BLE001 - 预加载失败不阻止启动
                        logger.warning("联想目录预加载失败，继续启动并在首次请求时按需抓取。", exc_info=True)
                if lenovo_created and configured.lenovo.catalog_refresh_interval_seconds > 0:
                    # 启动独立的 daemon 刷新线程（名称 lenovo-catalog-refresher）：线程只计时，
                    # 到点把刷新协程投递回主事件循环执行。首次刷新按快照年龄决定（陈旧快照
                    # 立即补刷、新鲜快照等剩余时间、没有快照等满一个间隔），启动阶段不联网。
                    start_refresher = getattr(api.state.lenovo, "start_catalog_refresher", None)
                    if callable(start_refresher):
                        try:
                            await start_refresher()
                        except Exception:  # noqa: BLE001 - 定时刷新启动失败不阻止启动
                            logger.warning("联想目录定时刷新线程启动失败。", exc_info=True)
            # 360 软件源：只在 soft360.enabled 时创建服务。启动阶段只预加载本地
            # soft360/catalog.json（不联网）并启动每 1800 秒的目录刷新；请求路径只读内存/
            # 磁盘快照，任何 GET 参数都不会触发抓取。
            api.state.soft360 = None
            soft360_created = False
            if configured.soft360.enabled:
                if soft360_service is not None:
                    # 测试注入的假服务：生命周期由调用方管理，shutdown 时不要关闭它。
                    api.state.soft360 = soft360_service
                else:
                    soft360_timeout = httpx2.Timeout(
                        configured.soft360.timeout_seconds,
                        connect=SOFT360_CONNECT_TIMEOUT_SECONDS,
                    )
                    soft360_client = await stack.enter_async_context(httpx2.AsyncClient(
                        timeout=soft360_timeout,
                        follow_redirects=True,
                        headers={"User-Agent": SOFT360_BROWSER_USER_AGENT},
                    ))
                    api.state.soft360 = create_soft360_store_service(
                        soft360_client,
                        configured.cache.directory,
                        configured,
                        cache_enabled=configured.cache.enabled,
                        cache_persist=configured.cache.persist,
                    )
                    soft360_created = True
                    # 启动失败（例如后续 provider/目录校验抛错）时 AsyncExitStack 也会关闭
                    # 360 服务，停止后台刷新线程；正常关闭时 finally 已 shutdown，这里是幂等 no-op。
                    stack.push_async_callback(api.state.soft360.shutdown)
            if api.state.soft360 is not None:
                preload_soft360 = getattr(api.state.soft360, "preload_catalog", None)
                if callable(preload_soft360):
                    try:
                        await preload_soft360()
                    except Exception:  # noqa: BLE001 - 预加载失败不阻止启动
                        logger.warning("360 目录预加载失败，继续启动并在后台刷新后可用。", exc_info=True)
                if soft360_created and configured.soft360.catalog_refresh_interval_seconds > 0:
                    # 启动独立的 daemon 刷新线程（名称 soft360-catalog-refresher）：线程只计时，
                    # 到点把刷新协程投递回主事件循环执行。首次刷新按快照年龄决定（陈旧快照
                    # 立即补刷、新鲜快照等剩余时间、没有快照等满一个间隔），启动阶段不联网。
                    start_soft360_refresher = getattr(api.state.soft360, "start_catalog_refresher", None)
                    if callable(start_soft360_refresher):
                        try:
                            await start_soft360_refresher()
                        except Exception:  # noqa: BLE001 - 定时刷新启动失败不阻止启动
                            logger.warning("360 目录定时刷新线程启动失败。", exc_info=True)
            if not builtin_enabled:
                # 内置源禁用：不创建任何内置 provider，也就不会请求 GitHub/官网。
                providers = {}
            elif release_providers is None:
                timeout = httpx2.Timeout(
                    configured.http.timeout_seconds, connect=configured.http.connect_timeout_seconds
                )
                client = await stack.enter_async_context(httpx2.AsyncClient(
                    timeout=timeout,
                    follow_redirects=True,
                    headers={"User-Agent": "WinAppsManager/1.0"},
                ))
                direct_client = await stack.enter_async_context(httpx2.AsyncClient(
                    timeout=timeout,
                    follow_redirects=True,
                    trust_env=False,
                    headers={"User-Agent": "WinAppsManager/1.0"},
                ))
                provider_cache_ttl = (
                    configured.providers.cache_ttl_seconds if configured.cache.enabled else 0.0
                )
                # Conditional-request cache: lets a restarted backend reuse the last GitHub
                # responses, or send only a cheap 304 probe (quotas ignore 304s).
                github_response_cache = None
                if configured.cache.enabled and configured.cache.persist:
                    github_response_cache = GithubResponseCache(configured.cache.directory / "github")
                providers = create_release_providers(
                    client,
                    direct_client,
                    cache_ttl=provider_cache_ttl,
                    catalog=api.state.catalog,
                    github_token=configured.github.token,
                    github_api_base=configured.github.api_base,
                    github_timeout_seconds=configured.github.timeout_seconds,
                    github_response_cache=github_response_cache,
                    github_refresh_interval_seconds=configured.github.refresh_interval_seconds,
                )
            else:
                providers = release_providers
            if builtin_enabled:
                # 白名单生效时只保留当前暴露软件需要的 provider：启动预热、定时刷新和
                # /refresh 都只看这两三个 provider，其余软件不会再访问 GitHub/官网。
                required_providers = {
                    app.download_provider
                    for app in api.state.catalog.apps
                    if app.download_provider is not None
                }
                providers = {
                    provider_id: provider
                    for provider_id, provider in providers.items()
                    if provider_id in required_providers
                }
            cache_settings = configured.cache
            store = None
            if (
                cache_settings.enabled
                and cache_settings.persist
                and cache_settings.persistent_ttl_seconds > 0
            ):
                store = PersistentMetadataStore(
                    cache_settings.directory,
                    ttl_seconds=cache_settings.persistent_ttl_seconds,
                    max_entries=cache_settings.max_entries,
                    max_stale_seconds=cache_settings.max_stale_seconds,
                )
            metadata_cache = ReleaseMetadataCache(
                ttl_seconds=cache_settings.ttl_seconds,
                max_entries=cache_settings.max_entries,
                load_timeout_seconds=configured.providers.timeout_seconds,
                enabled=cache_settings.enabled,
                store=store,
            )
            refresh_settings = configured.refresh
            # 内置源禁用时目录不对外暴露任何软件，给 ReleaseService 一个空目录视图，
            # 跳过“每个 app 都必须有 provider”的启动校验（此时 provider 表是空的）。
            release_catalog = api.state.catalog if builtin_enabled else SimpleNamespace(apps=())
            api.state.releases = ReleaseService(
                release_catalog,
                providers,
                timeout=configured.providers.timeout_seconds,
                metadata_cache=metadata_cache,
                allow_stale=refresh_settings.use_stale_cache,
            )
            api.state.icons = IconService()
            warmup = CatalogWarmup(
                api.state.catalog,
                providers,
                metadata_cache,
                timeout_seconds=configured.providers.timeout_seconds,
                max_concurrency=refresh_settings.max_concurrency,
                use_stale_cache=refresh_settings.use_stale_cache,
            )
            api.state.warmup = warmup
            api.state.refresher = PeriodicRefresher(
                warmup,
                # 禁用内置源时不启动定时刷新（interval=0 同时让 /cache 报告 enabled=false）。
                interval_seconds=refresh_settings.interval_seconds if builtin_enabled else 0.0,
                jitter_seconds=refresh_settings.jitter_seconds,
            )
            # 统一软件源注册表：/api/v1/sources 与 /api/v1/sources/{source_id}/... 的唯一数据源。
            # 新增来源只需在这里（create_source_registry）注册一个新的适配器，客户端无需改动；
            # 未启用的来源仍会出现在 /sources 里（enabled=false），统一路由返回 404。
            api.state.source_registry = create_source_registry(
                catalog=api.state.catalog,
                releases=api.state.releases,
                warmup=warmup,
                icons=api.state.icons,
                settings=configured,
                lenovo_service=api.state.lenovo,
                soft360_service=api.state.soft360,
            )
            if builtin_enabled and refresh_settings.on_startup:
                await _warm_catalog(warmup, refresh_settings)
            if builtin_enabled:
                api.state.refresher.start()
            try:
                yield
            finally:
                await api.state.refresher.stop()
                await api.state.releases.shutdown()
                if lenovo_created and api.state.lenovo is not None:
                    await api.state.lenovo.shutdown()
                if soft360_created and api.state.soft360 is not None:
                    await api.state.soft360.shutdown()
                logger.info("后端已关闭：本次运行 %.2f 秒。", time.monotonic() - started_at)

    # /docs 使用仓库内自带的 Swagger UI 静态资源，避免依赖 cdn.jsdelivr.net（离线/国内服务器可用）。
    api = FastAPI(title="WinAppsManager API", version=APP_VERSION, lifespan=lifespan, docs_url=None, redoc_url=None)
    router = APIRouter(prefix="/api/v1")

    @api.exception_handler(ProviderError)
    async def provider_error(request: Request, error: ProviderError):
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=504 if isinstance(error, ProviderTimeoutError) else 502,
            content={"detail": str(error)},
            headers={"Cache-Control": "no-store"},
        )

    def repository(request: Request) -> CatalogRepository:
        return request.app.state.catalog

    def get_app_or_404(request: Request, app_id: str) -> AppDefinition:
        app = repository(request).get_app(app_id)
        if app is None:
            raise HTTPException(status_code=404, detail="App not found")
        return app

    def require_builtin_source(request: Request) -> None:
        """内置精选源被配置禁用时，/api/v1/apps* 与 /api/v1/refresh 返回 503。"""

        if not request.app.state.settings.builtin.enabled:
            raise HTTPException(status_code=503, detail=BUILTIN_SOURCE_DISABLED_DETAIL)

    def ensure_available(request: Request, app: AppDefinition) -> None:
        """Reject apps whose upstream scrape failed and that had no persistent snapshot."""

        if not request.app.state.settings.refresh.drop_unavailable_apps:
            return
        if not request.app.state.warmup.is_available(app.id):
            raise HTTPException(
                status_code=503,
                detail="该软件暂时无法获取官方版本信息，请稍后重试。",
            )

    def build_response(request: Request, app: AppDefinition, latest_release: Release | None) -> AppResponse:
        metadata = app.model_dump(
            exclude={"icon_file", "download_provider", "latest_release_id", "releases", "provider_spec"}
        )
        if latest_release is not None:
            metadata["version"] = latest_release.version
        return AppResponse(
            **metadata,
            icon_url=str(request.url_for("app_icon", app_id=app.id)) if app.icon_file is not None else None,
            latest_release=latest_release,
        )

    async def app_response(request: Request, app: AppDefinition, architecture: Architecture, *, refresh: bool = False) -> AppResponse:
        latest_release = await request.app.state.releases.latest(app, architecture, force_refresh=refresh)
        return build_response(request, app, latest_release)

    @router.get("/health", response_model=HealthResponse, tags=["Health"])
    def health() -> HealthResponse:
        return HealthResponse()

    @router.get("/cache", tags=["Cache"])
    async def get_cache(request: Request, response: Response) -> dict:
        response.headers["Cache-Control"] = "no-store"
        lenovo_service = getattr(request.app.state, "lenovo", None)
        # 联想目录状态：只读内存里的快照与调度信息，不会触发抓取。注入的假服务可能没有
        # catalog_status（getattr 兼容），缺失时返回 null。
        lenovo_catalog = None
        catalog_status = getattr(lenovo_service, "catalog_status", None)
        if callable(catalog_status):
            try:
                lenovo_catalog = catalog_status()
            except Exception:  # noqa: BLE001 - 诊断接口不能因为状态读取失败而 500
                logger.warning("读取联想目录状态失败，已跳过。", exc_info=True)
        # 360 目录状态：同样只读内存/线程状态，不会触发抓取。
        soft360_service = getattr(request.app.state, "soft360", None)
        soft360_catalog = None
        soft360_catalog_status = getattr(soft360_service, "catalog_status", None)
        if callable(soft360_catalog_status):
            try:
                soft360_catalog = soft360_catalog_status()
            except Exception:  # noqa: BLE001 - 诊断接口不能因为状态读取失败而 500
                logger.warning("读取 360 目录状态失败，已跳过。", exc_info=True)
        return {
            **request.app.state.releases.cache_info(),
            "warmup": request.app.state.warmup.report(),
            "refresh": request.app.state.refresher.report(),
            "lenovo": {
                "enabled": request.app.state.settings.lenovo.enabled,
                "cached_app_count": (
                    lenovo_service.cached_app_count() if lenovo_service is not None else None
                ),
                "cached_updated_at": (
                    lenovo_service.cached_updated_at() if lenovo_service is not None else None
                ),
                "catalog": lenovo_catalog,
            },
            "soft360": {
                "enabled": request.app.state.settings.soft360.enabled,
                "cached_app_count": (
                    soft360_service.cached_app_count() if soft360_service is not None else None
                ),
                "cached_updated_at": (
                    soft360_service.cached_updated_at() if soft360_service is not None else None
                ),
                "catalog": soft360_catalog,
            },
            "logging": _logging_summary(request.app.state.settings),
            "settings": _public_settings(request.app.state.settings),
        }

    @router.delete("/cache", tags=["Cache"])
    async def clear_cache(request: Request, response: Response) -> dict:
        cleared = await request.app.state.releases.clear_cache()
        response.headers["Cache-Control"] = "no-store"
        info = request.app.state.releases.cache_info()
        # 联想源的清理是附加段：失败或未启用不能影响内置精选源的清理结果。
        # token 不清理（避免频繁重新注册），只清目录/详情/图片/搜索缓存。
        lenovo_service = getattr(request.app.state, "lenovo", None)
        lenovo_section: dict | None = None
        if lenovo_service is not None:
            try:
                lenovo_results = await lenovo_service.clear_caches()
            except Exception:
                logger.warning("清理联想应用商店缓存失败，已跳过。", exc_info=True)
                lenovo_section = {"cleared": False}
            else:
                lenovo_section = {"cleared": True, **lenovo_results}
        # 360 源的清理同样是附加段：失败或未启用不能影响前面的清理结果。
        soft360_service = getattr(request.app.state, "soft360", None)
        soft360_section: dict | None = None
        if soft360_service is not None:
            try:
                soft360_results = await soft360_service.clear_caches()
            except Exception:
                logger.warning("清理 360 软件源缓存失败，已跳过。", exc_info=True)
                soft360_section = {"cleared": False}
            else:
                soft360_section = {"cleared": True, **soft360_results}
        return {
            "cleared": cleared,
            "persistent_cleared": info["persistent"]["last_cleared"],
            **info,
            "lenovo": lenovo_section,
            "soft360": soft360_section,
            "settings": _public_settings(request.app.state.settings),
        }

    @router.get("/status", tags=["Health"])
    async def get_status(request: Request, response: Response) -> dict:
        """Per-app crawl availability and the periodic refresh state, for diagnostics."""

        response.headers["Cache-Control"] = "no-store"
        return {
            "warmup": request.app.state.warmup.report(),
            "refresh": request.app.state.refresher.report(),
        }

    @router.post("/refresh", tags=["Cache"])
    async def trigger_refresh(request: Request, response: Response) -> dict:
        """Run one catalog refresh round now; concurrent calls share a single round."""

        require_builtin_source(request)
        response.headers["Cache-Control"] = "no-store"
        report = await request.app.state.refresher.refresh_now()
        if report is None:
            logger.info("手动刷新未启动：已有一轮刷新正在进行。")
            return {"started": False, "reason": "已有刷新任务正在运行"}
        summary = report.summary()
        logger.info(
            "手动刷新完成：成功 %s，复用持久缓存 %s，不可用 %s，用时 %.2f 秒。",
            summary["ok"],
            summary["cached"],
            summary["unavailable"],
            summary["duration_seconds"],
        )
        return {"started": True, **summary}

    @router.get("/apps", response_model=AppCollection, tags=["Apps"])
    async def list_apps(
        request: Request,
        response: Response,
        search: Annotated[str | None, Query(max_length=200)] = None,
        category: Annotated[str | None, Query(max_length=100)] = None,
        sort: Literal["recommended", "name"] = "recommended",
        architecture: Architecture = "x64",
        refresh: bool = False,
    ) -> AppCollection:
        require_builtin_source(request)
        apps = repository(request).list_apps(search, category, sort)
        # 按目录声明的架构过滤：32 位系统不应该看到只提供 x64 安装包的软件。
        apps = [app for app in apps if app.supports_architecture(architecture)]
        if request.app.state.settings.refresh.drop_unavailable_apps:
            warmup = request.app.state.warmup
            apps = [app for app in apps if warmup.is_available(app.id)]
        response.headers["Cache-Control"] = "no-store"
        # One unreachable upstream must not turn the whole catalog into a gateway error:
        # each app is resolved independently and a failed app is omitted from the page.
        outcomes = await asyncio.gather(
            *(app_response(request, app, architecture, refresh=refresh) for app in apps),
            return_exceptions=True,
        )
        if refresh:
            # A forced refresh that fails should still show the last known data instead of
            # dropping the app from the page, so retry the failed ones without forcing.
            retries = [
                (index, app, outcome)
                for index, (app, outcome) in enumerate(zip(apps, outcomes))
                if isinstance(outcome, BaseException) and not isinstance(outcome, asyncio.CancelledError)
            ]
            if retries:
                fallbacks = await asyncio.gather(
                    *(app_response(request, app, architecture, refresh=False) for _, app, _ in retries),
                    return_exceptions=True,
                )
                for (index, app, forced_error), fallback in zip(retries, fallbacks):
                    if isinstance(fallback, BaseException):
                        if isinstance(fallback, asyncio.CancelledError):
                            raise fallback
                        logger.warning(
                            "获取软件 %s 的版本信息失败，且没有可用缓存：%s", app.id, forced_error
                        )
                        continue
                    logger.info("软件 %s 强制刷新失败，已回退到缓存中的版本。", app.id)
                    outcomes[index] = fallback
        items: list[AppResponse] = []
        keep_unavailable = not request.app.state.settings.refresh.drop_unavailable_apps
        warmup_state = request.app.state.warmup
        for app, outcome in zip(apps, outcomes):
            if isinstance(outcome, BaseException):
                if isinstance(outcome, asyncio.CancelledError):
                    raise outcome
                availability = warmup_state.availability(app.id)
                logger.warning("获取软件 %s 的版本信息失败，已从本次列表中略过：%s", app.id, outcome)
                if availability is not None and availability.status != "unavailable":
                    # 版本元数据可用（刚抓到或来自快照），只是签名/下载地址解析失败：
                    # 软件本身仍然有效，保留卡片但不给下载链接，避免它整条消失。
                    items.append(build_response(request, app, None))
                elif keep_unavailable:
                    items.append(build_response(request, app, None))
                continue
            items.append(outcome)
        return AppCollection(items=items, total=len(items))

    @router.get("/apps/{app_id}", response_model=AppResponse, tags=["Apps"])
    async def get_app(request: Request, response: Response, app_id: str, architecture: Architecture = "x64", refresh: bool = False) -> AppResponse:
        require_builtin_source(request)
        app = get_app_or_404(request, app_id)
        ensure_available(request, app)
        response.headers["Cache-Control"] = "no-store"
        return await app_response(request, app, architecture, refresh=refresh)

    @router.get("/apps/{app_id}/releases", response_model=ReleaseCollection, tags=["Releases"])
    async def list_releases(
        request: Request, response: Response, app_id: str, architecture: Architecture | None = None, refresh: bool = False
    ) -> ReleaseCollection:
        require_builtin_source(request)
        app = get_app_or_404(request, app_id)
        response.headers["Cache-Control"] = "no-store"
        releases = await request.app.state.releases.collection(app, architecture, force_refresh=refresh)
        return ReleaseCollection(items=releases, total=len(releases))

    @router.get("/apps/{app_id}/releases/latest", response_model=Release, tags=["Releases"])
    async def get_latest_release(
        request: Request, response: Response, app_id: str, architecture: Architecture = "x64"
    ) -> Release:
        require_builtin_source(request)
        release = await request.app.state.releases.latest(
            get_app_or_404(request, app_id), architecture, force_refresh=True
        )
        if release is None:
            raise HTTPException(status_code=404, detail="No release for the requested architecture")
        response.headers["Cache-Control"] = "no-store"
        return release

    @router.get("/apps/{app_id}/releases/{release_id}", response_model=Release, tags=["Releases"])
    async def get_release(request: Request, response: Response, app_id: str, release_id: str) -> Release:
        require_builtin_source(request)
        release = await request.app.state.releases.resource(get_app_or_404(request, app_id), release_id)
        if release is None:
            raise HTTPException(status_code=404, detail="Release not found")
        response.headers["Cache-Control"] = "no-store"
        return release

    @router.get("/apps/{app_id}/icon", name="app_icon", response_class=FileResponse, tags=["Icons"])
    def get_icon(request: Request, app_id: str) -> Response:
        require_builtin_source(request)
        icon_path = repository(request).icon_path(get_app_or_404(request, app_id))
        if icon_path is None:
            raise HTTPException(status_code=404, detail="Icon not found")
        return request.app.state.icons.response(request, icon_path)

    api.include_router(router)
    # 统一软件源 API：GET /sources 与 /sources/{source_id}/... 全部由来源注册表驱动；
    # 客户端只依赖这一套接口，新增来源不需要改客户端。旧的 /store/lenovo/*、/store/360/*、
    # /apps* 路由保持不变。
    api.include_router(create_unified_router())
    # 联想应用商店源自带 /api/v1/store/lenovo 前缀，路由从 request.app.state.lenovo 取服务。
    api.include_router(create_lenovo_router())
    # 360 软件源自带 /api/v1/store/360 前缀，路由从 request.app.state.soft360 取服务。
    api.include_router(create_soft360_router())
    api.mount("/icon", CachedIconStaticFiles(directory=str(icon_directory), check_dir=False), name="legacy_icons")

    swagger_assets = Path(__file__).resolve().parent / "static" / "swagger"
    if swagger_assets.is_dir():
        api.mount("/static/swagger", StaticFiles(directory=str(swagger_assets)), name="swagger_assets")

        @api.get("/docs", include_in_schema=False)
        async def swagger_ui() -> Any:
            return get_swagger_ui_html(
                openapi_url=api.openapi_url or "/openapi.json",
                title=f"{api.title} - Swagger UI",
                swagger_js_url="/static/swagger/swagger-ui-bundle.js",
                swagger_css_url="/static/swagger/swagger-ui.css",
                swagger_favicon_url="",
            )

    return api


app = create_app()

if __name__ == "__main__":
    # 先读配置并初始化文件日志，让 uvicorn 自己的启动/关闭消息也写进 backend.log。
    # 配置非法时这里直接抛错退出（与 lifespan 的校验一致，属于“启动失败”）；只有日志
    # 初始化失败才降级为默认输出，不阻止服务启动。
    startup_settings = BackendSettings.load()
    try:
        configure_logging(startup_settings.logging)
    except Exception:  # noqa: BLE001 - 日志初始化不能影响服务启动
        logging.getLogger("winappsmanager.main").warning(
            "初始化文件日志失败，继续使用默认日志输出。", exc_info=True
        )
    try:
        # log_config=None：不让 uvicorn 再安装一套自己的 handler（否则同一行会同时写
        # uvicorn 的 stderr 和本模块的 handler）；级别与格式统一由 configure_logging 决定。
        # 热重载/多 worker 不在这里使用，所以 log_config=None 不影响其他部署方式。
        uvicorn.run(
            create_app(settings=startup_settings),
            host=DEFAULT_HOST,
            port=DEFAULT_PORT,
            log_config=None,
        )
    finally:
        shutdown_logging()
