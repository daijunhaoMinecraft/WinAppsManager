import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import TypeVar

if __package__:
    from .cache import ReleaseMetadataCache
    from .catalog import CatalogRepository
    from .models import AppDefinition, Architecture, Release
    from .providers.base import ProviderError, ProviderReleaseChangedError, ProviderTimeoutError, ReleaseProvider
else:
    from cache import ReleaseMetadataCache
    from catalog import CatalogRepository
    from models import AppDefinition, Architecture, Release
    from providers.base import ProviderError, ProviderReleaseChangedError, ProviderTimeoutError, ReleaseProvider


T = TypeVar("T")


class ReleaseService:
    def __init__(
        self,
        catalog: CatalogRepository,
        providers: Mapping[str, ReleaseProvider],
        timeout: float = 10.0,
        *,
        cache_ttl: float = 300.0,
        metadata_cache: ReleaseMetadataCache | None = None,
        allow_stale: bool = False,
    ) -> None:
        if type(allow_stale) is not bool:
            raise ValueError("allow_stale must be a boolean")
        self._providers = dict(providers)
        self._timeout = timeout
        self._allow_stale = allow_stale
        self._cache = metadata_cache if metadata_cache is not None else ReleaseMetadataCache(
            ttl_seconds=cache_ttl, load_timeout_seconds=timeout
        )
        for app in catalog.apps:
            if app.download_provider is not None and app.download_provider not in self._providers:
                raise ValueError(f"unknown download provider for app {app.id}: {app.download_provider}")

    async def _bounded(self, operation: Awaitable[T]) -> T:
        try:
            return await asyncio.wait_for(operation, timeout=self._timeout)
        except asyncio.TimeoutError as error:
            raise ProviderTimeoutError("获取软件安装包信息超时，请稍后重试。") from error

    async def _metadata(self, app: AppDefinition, *, force_refresh: bool = False) -> list[Release]:
        if app.download_provider is None:
            return list(app.releases)
        provider = self._providers[app.download_provider]

        async def loader() -> list[Release]:
            return await provider.get_releases(force_refresh=True)

        if self._allow_stale and not force_refresh:
            # A transient upstream outage should degrade to the last known snapshot
            # instead of failing the whole catalog page; an explicit refresh still fails.
            result = await self._cache.fetch(
                app.download_provider, loader, force_refresh=False, allow_stale=True
            )
            return list(result.releases)
        return await self._cache.get(app.download_provider, loader, force_refresh=force_refresh)

    def cache_info(self) -> dict:
        return self._cache.info()

    async def clear_cache(self) -> int:
        return await self._cache.clear()

    async def shutdown(self) -> None:
        await self._cache.shutdown()

    async def _retry_changed_release(self, operation: Callable[[bool], Awaitable[T]]) -> T:
        try:
            return await operation(False)
        except ProviderReleaseChangedError:
            # Re-select from refreshed metadata if an upstream version changed between lookup and signing.
            return await operation(True)

    async def _resolve(self, app: AppDefinition, release: Release) -> Release:
        if app.download_provider is None:
            return release
        identity = (release.id, release.version, release.platform, release.architecture, release.file_name)
        resolved = await self._providers[app.download_provider].resolve_download(release)
        if (resolved.id, resolved.version, resolved.platform, resolved.architecture, resolved.file_name) != identity:
            raise ProviderError("下载插件返回了不一致的安装包信息。")
        return resolved

    async def latest(self, app: AppDefinition, architecture: Architecture, *, force_refresh: bool = False) -> Release | None:
        async def operation(refresh: bool) -> Release | None:
            releases = await self._metadata(app, force_refresh=force_refresh or refresh)
            candidates = [release for release in releases if release.architecture == architecture]
            if not candidates:
                return None
            if app.download_provider is None:
                candidate = next((release for release in candidates if release.id == app.latest_release_id), None)
                candidate = candidate or next((release for release in candidates if release.version == app.version), None)
                if candidate is None:
                    return None
            else:
                candidate = candidates[0]
            return await self._resolve(app, candidate)

        return await self._bounded(self._retry_changed_release(operation))

    async def collection(
        self, app: AppDefinition, architecture: Architecture | None = None, *, force_refresh: bool = False
    ) -> list[Release]:
        async def operation(refresh: bool) -> list[Release]:
            releases = await self._metadata(app, force_refresh=force_refresh or refresh)
            if architecture is not None:
                releases = [release for release in releases if release.architecture == architecture]
            return list(await asyncio.gather(*(self._resolve(app, release) for release in releases)))

        return await self._bounded(self._retry_changed_release(operation))

    async def resource(
        self, app: AppDefinition, release_id: str, *, force_refresh: bool = False
    ) -> Release | None:
        async def operation(refresh: bool) -> Release | None:
            release = next((release for release in await self._metadata(app, force_refresh=force_refresh or refresh) if release.id == release_id), None)
            return await self._resolve(app, release) if release is not None else None

        return await self._bounded(self._retry_changed_release(operation))
