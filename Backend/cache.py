import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
import math
from time import monotonic

if __package__:
    from .models import Release
    from .persistence import PersistentMetadataStore
    from .providers.base import ProviderProtocolError
else:
    from models import Release
    from persistence import PersistentMetadataStore
    from providers.base import ProviderProtocolError


MetadataLoader = Callable[[], Awaitable[list[Release]]]


class MetadataSource(str, Enum):
    """版本元数据的来源，供启动预热与定时刷新报告数据新鲜度。"""

    memory = "memory"
    persistent = "persistent"
    stale = "stale"
    provider = "provider"


@dataclass(frozen=True)
class MetadataResult:
    """一次元数据读取的结果；``degraded`` 只在回退到陈旧快照时为真。"""

    releases: tuple[Release, ...]
    source: MetadataSource
    degraded: bool = False


@dataclass(frozen=True)
class _MetadataEntry:
    releases: tuple[Release, ...]
    expires_at: float


@dataclass(frozen=True)
class _LoadResult:
    releases: tuple[Release, ...]
    source: MetadataSource


class ReleaseMetadataCache:
    """Bounded metadata cache, optionally backed by on-disk snapshots.

    The in-memory TTL is short and protects upstream services while the process is
    running. The persistent TTL is independent and lets a restarted backend reuse
    the last successful, unsigned metadata instead of contacting every provider.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = 300.0,
        max_entries: int = 64,
        clock: Callable[[], float] = monotonic,
        load_timeout_seconds: float = 10.0,
        enabled: bool = True,
        store: PersistentMetadataStore | None = None,
    ) -> None:
        if type(ttl_seconds) not in (int, float) or not math.isfinite(ttl_seconds) or ttl_seconds < 0:
            raise ValueError("ttl_seconds must be finite and non-negative")
        if type(max_entries) is not int or max_entries <= 0:
            raise ValueError("max_entries must be a positive integer")
        if type(load_timeout_seconds) not in (int, float) or not math.isfinite(load_timeout_seconds) or load_timeout_seconds <= 0:
            raise ValueError("load_timeout_seconds must be finite and positive")
        if not callable(clock):
            raise ValueError("clock must be callable")
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        if store is not None and not isinstance(store, PersistentMetadataStore):
            raise ValueError("store must be a PersistentMetadataStore")
        self.ttl_seconds = float(ttl_seconds)
        self.max_entries = max_entries
        self.enabled = enabled
        self._clock = clock
        self._load_timeout_seconds = float(load_timeout_seconds)
        self._store = store
        self._entries: OrderedDict[str, _MetadataEntry] = OrderedDict()
        self._inflight: dict[str, asyncio.Task[_LoadResult]] = {}
        self._pending: set[asyncio.Task[_LoadResult]] = set()
        self._generation = 0
        self._closed = False
        self._hits = 0
        self._misses = 0
        self._refreshes = 0
        self._coalesced = 0
        self._persistent_hits = 0
        self._stale_hits = 0

    def _prune_expired(self) -> None:
        now = self._clock()
        expired = [key for key, entry in self._entries.items() if entry.expires_at <= now]
        for key in expired:
            self._entries.pop(key, None)

    @staticmethod
    def _copies(snapshot: tuple[Release, ...]) -> list[Release]:
        return [release.model_copy(deep=True) for release in snapshot]

    def _remember(self, key: str, snapshot: tuple[Release, ...], *, ttl_seconds: float) -> None:
        if self._closed:
            return
        self._prune_expired()
        self._entries[key] = _MetadataEntry(snapshot, self._clock() + ttl_seconds)
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    async def fetch(
        self,
        key: str,
        loader: MetadataLoader,
        *,
        force_refresh: bool = False,
        allow_stale: bool = False,
    ) -> MetadataResult:
        """读取元数据并报告数据来源。

        ``allow_stale`` 为真时，抓取官网失败会回退到 ``max_stale_seconds`` 内的陈旧磁盘
        快照，并以 ``degraded=True`` 标记；陈旧快照不会被当成新数据写回缓存。
        """

        if self._closed:
            raise RuntimeError("The release metadata cache has been shut down")
        if not isinstance(key, str) or not key:
            raise ValueError("A provider cache key must be a non-empty string")
        self._prune_expired()
        flight = self._inflight.get(key)
        if flight is not None and flight.done():
            self._inflight.pop(key, None)
            flight = None
        if flight is not None:
            self._coalesced += 1
        elif self.enabled and not force_refresh and key in self._entries:
            self._hits += 1
            self._entries.move_to_end(key)
            return MetadataResult(self._copies(self._entries[key].releases), MetadataSource.memory)
        else:
            self._refreshes += int(force_refresh)
            generation = self._generation
            flight = asyncio.create_task(
                self._load(key, generation, loader, allow_persistent=not force_refresh),
                name=f"release-metadata:{key}",
            )
            self._inflight[key] = flight
            self._pending.add(flight)
            flight.add_done_callback(lambda task: self._load_finished(key, task))

        try:
            # A timed-out or cancelled API request must not cancel a load shared by other requests.
            loaded = await asyncio.shield(flight)
        except Exception:
            stored = None
            if allow_stale:
                stored = await self._stale_snapshot(key)
            if stored is None:
                raise
            self._stale_hits += 1
            return MetadataResult(self._copies(stored), MetadataSource.stale, degraded=True)
        return MetadataResult(self._copies(loaded.releases), loaded.source)

    async def get(self, key: str, loader: MetadataLoader, *, force_refresh: bool = False) -> list[Release]:
        """兼容旧调用：返回元数据列表，行为与统计计数保持原样。"""

        result = await self.fetch(key, loader, force_refresh=force_refresh, allow_stale=False)
        return list(result.releases)

    async def _stale_snapshot(self, key: str) -> tuple[Release, ...] | None:
        if not self.enabled or self._store is None:
            return None
        stored = await self._store.load(key, allow_expired=True)
        if stored is None:
            return None
        return tuple(release.model_copy(deep=True) for release in stored.releases)

    async def _load(
        self,
        key: str,
        generation: int,
        loader: MetadataLoader,
        *,
        allow_persistent: bool,
    ) -> _LoadResult:
        if self.enabled and allow_persistent and self._store is not None:
            # 用 allow_expired=True 探测：过期快照不能作为有效数据，但要保留给失败兜底；
            # 直接调用 load(key) 会按兼容语义删除过期文件，那样抓取失败后就无陈旧数据可用。
            stored = await self._store.load(key, allow_expired=True)
            if stored is not None and not stored.expired:
                snapshot = tuple(release.model_copy(deep=True) for release in stored.releases)
                if generation == self._generation and not self._closed:
                    self._remember(key, snapshot, ttl_seconds=min(self.ttl_seconds, stored.remaining_seconds))
                self._hits += 1
                self._persistent_hits += 1
                return _LoadResult(snapshot, MetadataSource.persistent)

        self._misses += 1
        releases = await asyncio.wait_for(loader(), timeout=self._load_timeout_seconds)
        if not isinstance(releases, (list, tuple)) or not all(isinstance(release, Release) for release in releases):
            raise ProviderProtocolError("下载提供器返回了无效的版本元数据。")
        if len({release.id for release in releases}) != len(releases):
            raise ProviderProtocolError("下载提供器返回了重复的版本元数据。")
        snapshot = tuple(release.model_copy(deep=True) for release in releases)
        if self.enabled and not self._closed and generation == self._generation:
            self._remember(key, snapshot, ttl_seconds=self.ttl_seconds)
            if self._store is not None:
                await self._store.save(key, snapshot)
        return _LoadResult(snapshot, MetadataSource.provider)

    def _load_finished(self, key: str, task: asyncio.Task[_LoadResult]) -> None:
        self._pending.discard(task)
        if self._inflight.get(key) is task:
            self._inflight.pop(key, None)
        # A load can outlive all of its shielded awaiters. Always observe a failed task.
        if not task.cancelled():
            task.exception()

    def info(self) -> dict:
        self._prune_expired()
        if self._store is None:
            persistent = {
                "enabled": False,
                "directory": None,
                "ttl_seconds": 0.0,
                "max_entries": self.max_entries,
                "entries": 0,
                "stale_entries": 0,
                "max_stale_seconds": 0.0,
                "hits": 0,
                "misses": 0,
                "writes": 0,
                "errors": 0,
                "pruned": 0,
                "last_cleared": 0,
                "closed": False,
            }
        else:
            persistent = self._store.info()
        return {
            "enabled": self.enabled,
            "ttl_seconds": self.ttl_seconds,
            "max_entries": self.max_entries,
            "entries": len(self._entries),
            "hits": self._hits,
            "misses": self._misses,
            "refreshes": self._refreshes,
            "coalesced": self._coalesced,
            "persistent_hits": self._persistent_hits,
            "stale_hits": self._stale_hits,
            "inflight": sum(not task.done() for task in self._inflight.values()),
            "pending_loads": sum(not task.done() for task in self._pending),
            "closed": self._closed,
            "persistent": persistent,
        }

    async def clear(self) -> int:
        self._prune_expired()
        count = len(self._entries)
        self._generation += 1
        self._entries.clear()
        self._inflight.clear()
        self._hits = self._misses = self._refreshes = self._coalesced = self._persistent_hits = self._stale_hits = 0
        if self._store is not None:
            await self._store.clear()
        # Existing requests may finish, but their generation cannot repopulate this cache.
        return count

    async def shutdown(self) -> None:
        self._closed = True
        self._generation += 1
        self._entries.clear()
        self._inflight.clear()
        pending = list(self._pending)
        for task in pending:
            if not task.done():
                task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._pending.clear()
        if self._store is not None:
            self._store.close()
