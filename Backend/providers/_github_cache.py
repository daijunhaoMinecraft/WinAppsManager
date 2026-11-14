"""GitHub API 响应的磁盘缓存（条件请求的 ETag / Last-Modified 载体）。

启动预热和定时刷新都会对每个 GitHub 来源请求一次 ``/releases/latest``。GitHub 支持
条件请求：带上上次响应的 ``ETag``（``If-None-Match``）或 ``Last-Modified``
（``If-Modified-Since``），数据没有变化时返回 ``304 Not Modified``，而且 **304 不计入
速率限制配额**。本模块把上一次的响应体与校验头保存在 ``<cache.directory>/github`` 下，
让重启后的后端可以直接复用资料，或者只发一次廉价的条件请求。

每个仓库一个 JSON 文件，文件名是 ``sha256(repo)[:32] + ".json"``（不把 ``owner/name``
拼进路径，避免路径穿越）。写入使用临时文件加 ``os.replace`` 的原子替换；读取和写入都
在线程里执行，单个文件损坏、版本不符或缺少字段时会被忽略并删除。任何缓存故障只记录
日志，不会让 API 请求失败。缓存里只保存 GitHub 的公开响应体和校验头，绝不写入
``github.token``。
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


logger = logging.getLogger("winappsmanager.githubcache")

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class CachedGithubResponse:
    """一次成功（200）或未变化（304）的 GitHub 响应。"""

    payload: dict
    etag: str | None
    last_modified: str | None
    fetched_at: float


class GithubResponseCache:
    """按仓库保存 GitHub 响应体和条件请求校验头。"""

    def __init__(
        self,
        directory: Path | str,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not callable(clock):
            raise ValueError("clock must be callable")
        try:
            text = os.fspath(directory)
        except TypeError as error:
            raise ValueError("directory must be a filesystem path") from error
        if not str(text).strip():
            raise ValueError("directory must be a non-empty path")
        self.directory = Path(text).expanduser().resolve()
        self._clock = clock
        self._entries: set[str] = set()
        self._hits = 0
        self._misses = 0
        self._writes = 0
        self._errors = 0
        self._scan()

    def _scan(self) -> None:
        """把已有文件里可用的仓库名读进条目表；损坏的文件直接删除。"""

        try:
            paths = list(self.directory.glob("*.json"))
        except OSError as error:
            logger.warning("无法读取 GitHub 响应缓存目录 %s: %s", self.directory, error)
            self._errors += 1
            return
        for path in paths:
            raw = self._read_raw(path)
            repo = raw.get("repo") if raw is not None else None
            if not isinstance(repo, str) or not repo or self._decode(raw) is None:
                self._errors += 1
                self._discard(path)
                continue
            self._entries.add(repo)

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
    def _decode(raw: dict[str, Any]) -> CachedGithubResponse | None:
        payload = raw.get("payload")
        if not isinstance(payload, dict):
            return None
        etag = raw.get("etag")
        if etag is not None and not isinstance(etag, str):
            return None
        last_modified = raw.get("last_modified")
        if last_modified is not None and not isinstance(last_modified, str):
            return None
        fetched_at = raw.get("fetched_at")
        if type(fetched_at) not in (int, float) or not math.isfinite(fetched_at):
            return None
        return CachedGithubResponse(payload, etag or None, last_modified or None, float(fetched_at))

    @staticmethod
    def _discard(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def _path(self, repo: str) -> Path:
        digest = sha256(repo.encode("utf-8")).hexdigest()[:32]
        return self.directory / f"{digest}.json"

    def _load_entry(self, repo: str, path: Path) -> CachedGithubResponse | None:
        raw = self._read_raw(path)
        if raw is None:
            self._discard(path)
            return None
        if raw.get("repo") != repo:
            # 摘要碰撞或目录被其他配置复用：不能把别的仓库资料当作本仓库的缓存。
            self._discard(path)
            return None
        cached = self._decode(raw)
        if cached is None:
            self._discard(path)
            return None
        return cached

    def _write_entry(self, path: Path, text: str, *, attempts: int = 3, delay: float = 0.05) -> None:
        """原子写入一个缓存文件，并对 Windows 上常见的短暂文件占用重试。"""

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

    async def load(self, repo: str) -> CachedGithubResponse | None:
        """读取仓库缓存；没有可用条目（包括文件损坏）时返回 ``None``。"""

        if not isinstance(repo, str) or not repo:
            raise ValueError("repo must be a non-empty string")
        path = self._path(repo)
        try:
            cached = await asyncio.to_thread(self._load_entry, repo, path)
        except Exception:  # 缓存读取永远不能中断 API 请求。
            logger.warning("读取 GitHub 响应缓存失败: %s", path, exc_info=True)
            self._errors += 1
            cached = None
        if cached is None:
            self._misses += 1
            self._entries.discard(repo)
            return None
        self._hits += 1
        self._entries.add(repo)
        return cached

    async def save(
        self,
        repo: str,
        payload: dict,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> None:
        """保存响应体与校验头；写盘失败只记录日志，不抛出。"""

        if not isinstance(repo, str) or not repo:
            raise ValueError("repo must be a non-empty string")
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
        if etag is not None and not isinstance(etag, str):
            raise ValueError("etag must be a string or None")
        if last_modified is not None and not isinstance(last_modified, str):
            raise ValueError("last_modified must be a string or None")
        document = {
            "schema_version": SCHEMA_VERSION,
            "repo": repo,
            "etag": etag or None,
            "last_modified": last_modified or None,
            "fetched_at": float(self._clock()),
            "payload": payload,
        }
        path = self._path(repo)
        try:
            text = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
            await asyncio.to_thread(self._write_entry, path, text)
        except (OSError, TypeError, ValueError):
            logger.warning("写入 GitHub 响应缓存失败: %s", path, exc_info=True)
            self._errors += 1
            return
        self._writes += 1
        self._entries.add(repo)

    def info(self) -> dict:
        """缓存状态与计数器，供诊断和测试断言使用。"""

        return {
            "enabled": True,
            "directory": str(self.directory),
            "entries": len(self._entries),
            "hits": self._hits,
            "misses": self._misses,
            "writes": self._writes,
            "errors": self._errors,
        }
