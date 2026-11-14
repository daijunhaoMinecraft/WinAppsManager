"""磁盘上的版本元数据快照，使后端重启后仍能复用上次成功获取的资料。

只保存提供器 ``get_releases()`` 返回的未签名资料；``resolve_download()`` 生成的
临时下载签名从不写入磁盘。写入使用临时文件加 ``os.replace``，单个损坏的文件会被
忽略并删除，缓存故障不会让 API 请求失败。

快照在 TTL 过期后不会立刻变得一文不值：只要过期时间不超过 ``max_stale_seconds``，
就可以用 ``load(key, allow_expired=True)`` 当作“陈旧但可用”的兜底数据，用于官网抓取
失败时继续提供上一次成功的版本信息；超出该时限的文件会被删除。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
import json
import logging
import math
import os
from pathlib import Path
import tempfile
import time
from typing import Any

from pydantic import ValidationError

if __package__:
    from .models import Release
else:
    from models import Release


logger = logging.getLogger("winappsmanager.cache")

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class StoredMetadata:
    releases: tuple[Release, ...]
    expires_at: float
    # 已过期时为负数，表示距离过期已经过去多久。
    remaining_seconds: float
    saved_at: float
    expired: bool


@dataclass(frozen=True)
class _IndexEntry:
    path: Path
    expires_at: float


class PersistentMetadataStore:
    """JSON-file cache for release metadata snapshots."""

    def __init__(
        self,
        directory: Path | str,
        *,
        ttl_seconds: float = 3600.0,
        max_entries: int = 64,
        max_stale_seconds: float = 604800.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if type(ttl_seconds) not in (int, float) or not math.isfinite(ttl_seconds) or ttl_seconds < 0:
            raise ValueError("ttl_seconds must be finite and non-negative")
        if type(max_entries) is not int or max_entries <= 0:
            raise ValueError("max_entries must be a positive integer")
        if (
            type(max_stale_seconds) not in (int, float)
            or not math.isfinite(max_stale_seconds)
            or max_stale_seconds < 0
        ):
            raise ValueError("max_stale_seconds must be finite and non-negative")
        if not callable(clock):
            raise ValueError("clock must be callable")
        try:
            text = os.fspath(directory)
        except TypeError as error:
            raise ValueError("directory must be a filesystem path") from error
        if not str(text).strip():
            raise ValueError("directory must be a non-empty path")
        self.directory = Path(text).expanduser().resolve()
        self.ttl_seconds = float(ttl_seconds)
        self.max_entries = max_entries
        self.max_stale_seconds = float(max_stale_seconds)
        self._clock = clock
        self._index: dict[str, _IndexEntry] = {}
        self._generation = 0
        self._closed = False
        self._hits = 0
        self._misses = 0
        self._writes = 0
        self._errors = 0
        self._cleared_last = 0
        self._pruned = 0
        self._scan()

    def _scan(self) -> None:
        try:
            paths = list(self.directory.glob("*.json"))
        except OSError as error:
            logger.warning("无法读取持久缓存目录 %s: %s", self.directory, error)
            self._errors += 1
            return
        now = self._clock()
        for path in paths:
            raw = self._read_raw(path)
            if raw is None:
                self._errors += 1
                self._discard(path)
                continue
            key = raw.get("key")
            expires_at = raw.get("expires_at")
            if (
                not isinstance(key, str)
                or not key
                or type(expires_at) not in (int, float)
                or not math.isfinite(expires_at)
            ):
                self._errors += 1
                self._discard(path)
                continue
            expires_at = float(expires_at)
            # 过期但仍在陈旧时限内的快照要保留在索引里，否则重启后就取不到兜底数据；
            # 超出时限的文件与损坏文件一样直接丢弃。
            if expires_at <= now and now - expires_at > self.max_stale_seconds:
                self._discard(path)
                continue
            self._index[key] = _IndexEntry(path, expires_at)

    @staticmethod
    def _read_raw(path: Path) -> dict[str, Any] | None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
            return None
        return raw

    @staticmethod
    def _discard(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def _path(self, key: str) -> Path:
        digest = sha256(key.encode("utf-8")).hexdigest()[:32]
        return self.directory / f"{digest}.json"

    def _read_entry(self, key: str, path: Path, *, allow_expired: bool) -> StoredMetadata | None:
        raw = self._read_raw(path)
        if raw is None:
            return None
        if raw.get("key") != key:
            self._discard(path)
            return None
        expires_at = raw.get("expires_at")
        if type(expires_at) not in (int, float) or not math.isfinite(expires_at):
            self._discard(path)
            return None
        expires_at = float(expires_at)
        saved_at_raw = raw.get("saved_at")
        saved_at = (
            float(saved_at_raw)
            if type(saved_at_raw) in (int, float) and math.isfinite(saved_at_raw)
            else expires_at
        )
        remaining = expires_at - self._clock()
        expired = remaining <= 0
        if expired:
            # 过期仍在陈旧时限内且调用方允许时保留文件，作为抓取失败时的兜底。
            if not allow_expired or -remaining > self.max_stale_seconds:
                self._discard(path)
                return None
        releases_raw = raw.get("releases")
        if not isinstance(releases_raw, list):
            self._discard(path)
            return None
        try:
            releases = tuple(Release.model_validate(item) for item in releases_raw)
        except (ValidationError, TypeError, ValueError):
            self._discard(path)
            return None
        return StoredMetadata(releases, expires_at, remaining, saved_at, expired)

    def _write_entry(self, path: Path, text: str, *, attempts: int = 3, delay: float = 0.05) -> None:
        """Write one snapshot atomically, retrying transient Windows file locks.

        杀毒软件或索引服务会短暂占用刚创建的临时文件，在 Windows 上表现为
        ``PermissionError``/``sharing violation``；这种情况重试一次通常就能成功，而
        真正不可写的目录会在用尽重试后抛出，由 :meth:`save` 记录并降级。
        """

        last_error: OSError | None = None
        for attempt in range(1, attempts + 1):
            try:
                self._write_once(path, text)
                return
            except OSError as error:
                last_error = error
                if attempt < attempts:
                    time.sleep(delay * attempt)
        if last_error is not None:
            raise last_error

    @staticmethod
    def _write_once(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            except OSError:
                pass

    def _prune_entries(self, entries: dict[str, tuple[Path, float]], keep: int) -> list[str]:
        removed: list[str] = []
        ordered = sorted(entries.items(), key=lambda item: (item[1][1], item[0]))
        for key, (path, _) in ordered[: max(0, len(ordered) - keep)]:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                continue
            removed.append(key)
        return removed

    def _delete_files(self) -> int:
        removed = 0
        for pattern in ("*.json", "*.tmp"):
            try:
                paths = list(self.directory.glob(pattern))
            except OSError:
                continue
            for path in paths:
                try:
                    path.unlink()
                except FileNotFoundError:
                    continue
                except OSError:
                    continue
                removed += 1
        return removed

    async def load(self, key: str, *, allow_expired: bool = False) -> StoredMetadata | None:
        """读取快照；``allow_expired`` 为真时允许返回 ``max_stale_seconds`` 内的陈旧快照。"""

        if self._closed:
            return None
        path = self._path(key)
        try:
            stored = await asyncio.to_thread(self._read_entry, key, path, allow_expired=allow_expired)
        except Exception:  # A cache read must never break an API request.
            logger.warning("读取持久缓存失败: %s", path, exc_info=True)
            self._errors += 1
            stored = None
        if stored is None:
            self._misses += 1
            self._index.pop(key, None)
            return None
        self._hits += 1
        self._index[key] = _IndexEntry(path, stored.expires_at)
        return stored

    async def save(self, key: str, releases: list[Release] | tuple[Release, ...]) -> bool:
        if self._closed:
            return False
        snapshot = tuple(releases)
        saved_at = self._clock()
        expires_at = saved_at + self.ttl_seconds
        generation = self._generation
        path = self._path(key)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "key": key,
            "saved_at": saved_at,
            "expires_at": expires_at,
            "releases": [release.model_dump(mode="json") for release in snapshot],
        }
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        try:
            await asyncio.to_thread(self._write_entry, path, text)
        except OSError:
            logger.warning("写入持久缓存失败: %s", path, exc_info=True)
            self._errors += 1
            return False
        self._writes += 1
        if generation != self._generation or self._closed:
            await asyncio.to_thread(self._discard, path)
            return False
        self._index[key] = _IndexEntry(path, expires_at)
        await self._prune()
        return True

    async def _prune(self) -> None:
        snapshot = {key: (entry.path, entry.expires_at) for key, entry in self._index.items()}
        if len(snapshot) <= self.max_entries:
            return
        removed = await asyncio.to_thread(self._prune_entries, snapshot, self.max_entries)
        for key in removed:
            self._index.pop(key, None)
        self._pruned += len(removed)

    async def clear(self) -> int:
        self._generation += 1
        self._index.clear()
        removed = await asyncio.to_thread(self._delete_files)
        self._cleared_last = removed
        self._hits = self._misses = self._writes = self._errors = self._pruned = 0
        return removed

    def close(self) -> None:
        self._closed = True
        self._index.clear()

    def info(self) -> dict:
        now = self._clock()
        entries = 0
        stale_entries = 0
        for key, entry in list(self._index.items()):
            if entry.expires_at > now:
                entries += 1
            elif now - entry.expires_at <= self.max_stale_seconds:
                # 过期但仍在陈旧时限内，可作为兜底快照。
                stale_entries += 1
            else:
                self._index.pop(key, None)
        return {
            "enabled": True,
            "directory": str(self.directory),
            "ttl_seconds": self.ttl_seconds,
            "max_entries": self.max_entries,
            "entries": entries,
            "stale_entries": stale_entries,
            "max_stale_seconds": self.max_stale_seconds,
            "hits": self._hits,
            "misses": self._misses,
            "writes": self._writes,
            "errors": self._errors,
            "pruned": self._pruned,
            "last_cleared": self._cleared_last,
            "closed": self._closed,
        }
