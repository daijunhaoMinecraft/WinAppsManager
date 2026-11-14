"""360 软件详情缓存与下载地址解析。

- 详情：内存 LRU + TTL，磁盘 ``<cache>/soft360/details/<sha256(soft_id)>.json``。详情接口
  ``getinfo`` 同时给出 ``edition_info.default_edition``（版本、系统、下载信息）与 ``snapshot``
  截图列表；缓存里只保存规范化后的展示字段，**绝不保存下载地址**（下载地址每次实时解析）。
- 截图：``snapshot[]`` 里的图片地址统一规范化为 https，模型里只暴露后端代理路径
  ``/api/v1/store/360/apps/{soft_id}/screenshots/{index}``；360 没有视频。
- 下载：``resolve_download`` 每次都实时请求 ``getinfo``（不读也不写详情缓存），从
  ``edition_info.default_edition.down.default`` 的 ``p2p`` / ``multi_urls`` / ``multi_down``
  里提取 http(s) 地址，优先 https，扩展名只允许 ``.exe/.msi/.msix/.appx``；文件名用
  ``down.default.name`` 清洗，缺失或不安全时回退 ``{安全应用名}_{版本}.exe``；大小用
  ``down.default.size`` 或 ``soft_size``；``md5`` **不是** sha256，因此 ``sha256`` 一律为空。
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
import logging
import math
import os
from hashlib import sha256
from pathlib import Path
import re
import tempfile
import time
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from pydantic import ValidationError

if "." in (__package__ or ""):
    from ..models import Release
else:
    from models import Release

from .catalog import (
    build_architectures,
    minimum_windows_from_versions,
    normalize_image_url,
    parse_score,
    parse_size_bytes,
    truncate_description,
)
from .client import Soft360RawClient
from .errors import (
    Soft360Error,
    Soft360NotFoundError,
    Soft360ProtocolError,
    Soft360UnavailableError,
)
from .models import Soft360AppDetails, Soft360AppSummary, Soft360Screenshot

logger = logging.getLogger("winappsmanager.soft360")

DETAILS_SCHEMA_VERSION = 1
DEFAULT_DETAILS_TTL_SECONDS = 900.0
DEFAULT_DETAILS_PERSISTENT_TTL_SECONDS = 21600.0
DEFAULT_DETAILS_MAX_ENTRIES = 256
ALLOWED_INSTALLER_SUFFIXES = (".exe", ".msi", ".msix", ".appx")
# 下载地址候选的字段顺序：p2p 是 360 主推的多线路列表，multi_urls / multi_down 作为补充。
DOWNLOAD_URL_FIELDS: tuple[str, ...] = ("p2p", "multi_urls", "multi_down")
DEFAULT_VERSION = "以官网为准"
LATEST_VERSION = "latest"
# p2p 字段既有 ``|||`` 分隔的多条地址，也有 ``pdown://`` 描述串里用 ``|`` 拼进来的
# ``https://cdn-download.soft.360.cn/...exe`` 兜底地址，因此按 URL 正则整体提取。
_URL_PATTERN = re.compile(r"https?://[^\s\"'<>|]+")
_ILLEGAL_FILE_CHARACTERS = frozenset('<>:"/\\|?*')
_RESERVED_FILE_STEMS = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


@dataclass(frozen=True)
class Soft360ScreenshotSource:
    """截图来源：``source_url`` 是 360 图片 CDN 的原图地址。"""

    index: int
    source_url: str


@dataclass(frozen=True)
class Soft360DetailsFields:
    """详情页专属字段（不含从目录 summary 继承的基础字段）。"""

    name: str
    bname: str | None
    publisher: str | None
    version: str
    brief: str
    long_description: str
    size_bytes: int | None
    score: int | None
    category: str | None
    category_id: str | None
    architectures: tuple[str, ...]
    minimum_windows: str | None
    screenshots: tuple[Soft360ScreenshotSource, ...]


@dataclass(frozen=True)
class _MemoryEntry:
    fields: Soft360DetailsFields
    expires_at: float


class Soft360DetailsProvider:
    """详情抓取 + 内存/磁盘缓存 + 下载地址实时解析。

    ``directory`` 是 360 缓存根目录（``<cache_dir>/soft360``），详情写在其中的 ``details/`` 下。
    """

    def __init__(
        self,
        client: Soft360RawClient,
        directory: Path | str | None = None,
        *,
        ttl_seconds: float = DEFAULT_DETAILS_TTL_SECONDS,
        persistent_ttl_seconds: float = DEFAULT_DETAILS_PERSISTENT_TTL_SECONDS,
        max_entries: int = DEFAULT_DETAILS_MAX_ENTRIES,
        cache_enabled: bool = True,
        cache_persist: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(client, Soft360RawClient):
            raise ValueError("client must be a Soft360RawClient")
        for name, value in (
            ("ttl_seconds", ttl_seconds),
            ("persistent_ttl_seconds", persistent_ttl_seconds),
        ):
            if type(value) not in (int, float) or not math.isfinite(float(value)) or value < 0:
                raise ValueError(f"{name} must be a finite non-negative value")
        if type(max_entries) is not int or max_entries <= 0:
            raise ValueError("max_entries must be a positive integer")
        if type(cache_enabled) is not bool or type(cache_persist) is not bool:
            raise ValueError("cache_enabled / cache_persist must be booleans")
        if not callable(clock):
            raise ValueError("clock must be callable")
        self._client = client
        self._ttl_seconds = float(ttl_seconds)
        self._persistent_ttl_seconds = float(persistent_ttl_seconds)
        self._max_entries = max_entries
        self._cache_enabled = cache_enabled
        self._cache_persist = cache_persist
        self._clock = clock
        self._directory = _prepare_directory(directory)
        self._memory: OrderedDict[str, _MemoryEntry] = OrderedDict()
        self._hits = 0
        self._misses = 0
        self._stale_hits = 0

    # -- 公开接口 ---------------------------------------------------------

    @property
    def counters(self) -> dict[str, int]:
        return {"hits": self._hits, "misses": self._misses, "stale_hits": self._stale_hits}

    async def get_fields(self, soft_id: str, *, force_refresh: bool = False) -> Soft360DetailsFields:
        """读取详情字段；失败时尽量回退到陈旧缓存（不存在/下架除外）。"""

        if not isinstance(soft_id, str) or not soft_id:
            raise ValueError("soft_id must be a non-empty string")
        if not self._cache_enabled:
            return await self._fetch(soft_id)

        now = self._clock()
        entry = self._memory.get(soft_id)
        if entry is not None and not force_refresh and now < entry.expires_at:
            self._hits += 1
            self._memory.move_to_end(soft_id)
            return entry.fields
        stale = entry.fields if entry is not None else None

        if not force_refresh and self._cache_persist:
            loaded = await self._load_disk(soft_id)
            if loaded is not None:
                fields, fresh = loaded
                if fresh:
                    self._hits += 1
                    self._remember(soft_id, fields)
                    return fields
                stale = stale or fields

        self._misses += 1
        try:
            fields = await self._fetch(soft_id)
        except Soft360NotFoundError:
            # 不存在/下架是确定结论，不回退陈旧详情。
            raise
        except Soft360Error:
            fallback = stale
            if fallback is None and self._cache_persist:
                expired = await self._load_disk(soft_id, allow_expired=True)
                fallback = expired[0] if expired is not None else None
            if fallback is None:
                raise
            self._stale_hits += 1
            logger.warning("360 软件 %s 详情抓取失败，回退到陈旧详情。", soft_id)
            return fallback

        self._remember(soft_id, fields)
        if self._cache_persist:
            await self._save_disk(soft_id, fields)
        return fields

    def build_details(
        self, summary: Soft360AppSummary, fields: Soft360DetailsFields
    ) -> Soft360AppDetails:
        """把目录 summary 与详情字段合并成 :class:`Soft360AppDetails`。"""

        encoded_soft_id = quote(summary.soft_id, safe="")
        screenshots = [
            Soft360Screenshot(
                index=screenshot.index,
                url=f"/api/v1/store/360/apps/{encoded_soft_id}/screenshots/{screenshot.index}",
            )
            for screenshot in fields.screenshots
        ]
        brief = fields.brief or summary.description
        data = summary.model_dump()
        data.update(
            name=fields.name or summary.name,
            bname=fields.bname if fields.bname is not None else summary.bname,
            description=truncate_description(brief, summary.name),
            category=fields.category or summary.category,
            category_id=fields.category_id or summary.category_id,
            size_bytes=fields.size_bytes if fields.size_bytes is not None else summary.size_bytes,
            score=fields.score if fields.score is not None else summary.score,
            architectures=list(fields.architectures) or list(summary.architectures),
            minimum_windows=fields.minimum_windows or summary.minimum_windows,
            version=fields.version or DEFAULT_VERSION,
            publisher=fields.publisher,
            long_description=fields.long_description or brief or summary.description,
            screenshots=screenshots,
        )
        return Soft360AppDetails(**data)

    async def resolve_download(
        self,
        soft_id: str,
        summary: Soft360AppSummary,
        architecture: str = "x64",
    ) -> Release:
        """实时解析当前可用的下载地址；结果绝不缓存。"""

        if not isinstance(summary, Soft360AppSummary):
            raise Soft360ProtocolError("360 软件目录条目类型无效。")
        # 下载地址不缓存：每次都重新请求详情（也不写入详情缓存）。
        data = await self._client.get_info([soft_id])
        raw = _pick_info(data, soft_id)
        if raw is None:
            raise Soft360NotFoundError(f"360 软件源中找不到 {soft_id}。")
        edition = _default_edition(raw)
        if edition is None:
            raise Soft360UnavailableError(f"360 软件源暂时没有 {summary.name} 的安装包信息。")
        down = _download_entry(edition)
        if down is None:
            raise Soft360UnavailableError(f"360 软件源暂时没有 {summary.name} 的下载信息。")
        selected = _select_download_url(down)
        if selected is None:
            raise Soft360UnavailableError(f"360 软件源暂时没有 {summary.name} 的可用下载地址。")
        version = _text(edition.get("version")) or LATEST_VERSION
        file_name = _safe_file_name(down.get("name"), summary, version)
        size_bytes = parse_size_bytes(down.get("size")) or summary.size_bytes
        chosen_architecture = _choose_architecture(raw, summary, architecture)
        try:
            return Release(
                id=_release_id(soft_id, version),
                version=version,
                platform="windows",
                architecture=chosen_architecture,
                download_url=selected,
                file_name=file_name,
                size_bytes=size_bytes,
                sha256=None,
            )
        except ValidationError:
            raise Soft360ProtocolError("360 下载地址响应无法映射成有效的版本资源。") from None

    async def clear(self) -> int:
        """清空内存与磁盘详情缓存，返回删除的磁盘文件数。"""

        removed = len(self._memory)
        self._memory.clear()
        if not self._cache_persist or self._directory is None:
            return 0
        details_directory = self._directory / "details"
        try:
            deleted = await asyncio.to_thread(_delete_json_files, details_directory)
        except OSError:
            return 0
        return max(removed, deleted)

    # -- 抓取与映射 -------------------------------------------------------

    async def _fetch(self, soft_id: str) -> Soft360DetailsFields:
        data = await self._client.get_info([soft_id])
        raw = _pick_info(data, soft_id)
        if raw is None:
            raise Soft360NotFoundError(f"360 软件源中找不到 {soft_id}。")
        return _parse_fields(raw, soft_id)

    # -- 内存缓存 ---------------------------------------------------------

    def _remember(self, soft_id: str, fields: Soft360DetailsFields) -> None:
        self._memory[soft_id] = _MemoryEntry(
            fields=fields, expires_at=self._clock() + self._ttl_seconds
        )
        self._memory.move_to_end(soft_id)
        while len(self._memory) > self._max_entries:
            self._memory.popitem(last=False)

    # -- 磁盘缓存 ---------------------------------------------------------

    def _path_for(self, soft_id: str) -> Path | None:
        if self._directory is None:
            return None
        digest = sha256(soft_id.encode("utf-8")).hexdigest()
        return self._directory / "details" / f"{digest}.json"

    async def _load_disk(
        self, soft_id: str, *, allow_expired: bool = False
    ) -> tuple[Soft360DetailsFields, bool] | None:
        path = self._path_for(soft_id)
        if path is None:
            return None
        try:
            loaded = await asyncio.to_thread(self._read_disk_sync, path, soft_id)
        except Exception:  # 缓存读取永远不能中断 API 请求。
            logger.warning("读取 360 详情缓存失败: %s", path, exc_info=True)
            loaded = None
        if loaded is None:
            await self._discard(path)
            return None
        fields, fetched_at = loaded
        fresh = self._clock() - fetched_at <= self._persistent_ttl_seconds
        if not fresh and not allow_expired:
            return None
        return fields, fresh

    @staticmethod
    def _read_disk_sync(path: Path, soft_id: str) -> tuple[Soft360DetailsFields, float] | None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict) or raw.get("schema_version") != DETAILS_SCHEMA_VERSION:
            return None
        if raw.get("soft_id") != soft_id:
            return None
        fetched_at = raw.get("fetched_at")
        if type(fetched_at) not in (int, float) or not math.isfinite(float(fetched_at)):
            return None
        screenshots_raw = raw.get("screenshots")
        if not isinstance(screenshots_raw, list):
            return None
        screenshots: list[Soft360ScreenshotSource] = []
        for item in screenshots_raw:
            if not isinstance(item, Mapping):
                return None
            index = item.get("index")
            source_url = item.get("source_url")
            if type(index) is not int or index < 0 or not isinstance(source_url, str) or not source_url:
                return None
            screenshots.append(Soft360ScreenshotSource(index=index, source_url=source_url))
        architectures_raw = raw.get("architectures")
        if not isinstance(architectures_raw, list) or not all(
            isinstance(item, str) for item in architectures_raw
        ):
            return None
        minimum_windows = raw.get("minimum_windows")
        if minimum_windows is not None and not isinstance(minimum_windows, str):
            return None
        strings: dict[str, str | None] = {}
        for name in ("name", "version", "brief", "long_description"):
            value = raw.get(name)
            if not isinstance(value, str):
                return None
            strings[name] = value
        for name in ("bname", "publisher", "category", "category_id"):
            value = raw.get(name)
            if value is not None and not isinstance(value, str):
                return None
            strings[name] = value
        size_bytes = raw.get("size_bytes")
        if size_bytes is not None and (type(size_bytes) is not int or size_bytes < 1):
            return None
        score = raw.get("score")
        if score is not None and (type(score) is not int or not 0 <= score <= 100):
            return None
        return (
            Soft360DetailsFields(
                name=strings["name"] or soft_id,
                bname=strings["bname"],
                publisher=strings["publisher"],
                version=strings["version"],
                brief=strings["brief"] or "",
                long_description=strings["long_description"] or "",
                size_bytes=size_bytes,
                score=score,
                category=strings["category"],
                category_id=strings["category_id"],
                architectures=tuple(architectures_raw),
                minimum_windows=minimum_windows,
                screenshots=tuple(screenshots),
            ),
            float(fetched_at),
        )

    async def _save_disk(self, soft_id: str, fields: Soft360DetailsFields) -> None:
        path = self._path_for(soft_id)
        if path is None:
            return
        document = {
            "schema_version": DETAILS_SCHEMA_VERSION,
            "soft_id": soft_id,
            "fetched_at": float(self._clock()),
            # 注意：下载地址绝不写入缓存，这里只有展示字段与截图原图地址。
            "name": fields.name,
            "bname": fields.bname,
            "publisher": fields.publisher,
            "version": fields.version,
            "brief": fields.brief,
            "long_description": fields.long_description,
            "size_bytes": fields.size_bytes,
            "score": fields.score,
            "category": fields.category,
            "category_id": fields.category_id,
            "architectures": list(fields.architectures),
            "minimum_windows": fields.minimum_windows,
            "screenshots": [
                {"index": item.index, "source_url": item.source_url}
                for item in fields.screenshots
            ],
        }
        try:
            await asyncio.to_thread(_write_json_file, path, document)
        except (OSError, TypeError, ValueError):
            logger.warning("写入 360 详情缓存失败: %s", path, exc_info=True)

    @staticmethod
    async def _discard(path: Path) -> None:
        try:
            await asyncio.to_thread(path.unlink, True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 字段映射辅助函数
# ---------------------------------------------------------------------------


def _pick_info(data: Mapping[str, Any], soft_id: str) -> Mapping[str, Any] | None:
    """从 ``{soft_id: {...}}`` 里取出目标详情（兼容大小写/数字键）。"""

    value = data.get(soft_id)
    if isinstance(value, Mapping):
        return value
    folded = soft_id.casefold()
    for key, candidate in data.items():
        if isinstance(key, str) and key.casefold() == folded and isinstance(candidate, Mapping):
            return candidate
    return None


def _default_edition(raw: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """取 ``edition_info.default_edition``；缺失时兼容直接给 ``edition_info`` 的写法。"""

    edition_info = raw.get("edition_info")
    if not isinstance(edition_info, Mapping):
        return None
    edition = edition_info.get("default_edition")
    if isinstance(edition, Mapping):
        return edition
    if any(key in edition_info for key in ("version", "down", "os")):
        return edition_info
    return None


def _download_entry(edition: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """取 ``down.default``；缺失时兼容 ``down`` 本身就是默认项。"""

    down = edition.get("down")
    if not isinstance(down, Mapping):
        return None
    default = down.get("default")
    if isinstance(default, Mapping):
        return default
    if any(key in down for key in ("p2p", "multi_urls", "name", "size")):
        return down
    return None


def _parse_fields(raw: Mapping[str, Any], soft_id: str) -> Soft360DetailsFields:
    edition = _default_edition(raw) or {}
    os_info = edition.get("os")
    bits = os_info.get("bit") if isinstance(os_info, Mapping) else None
    versions = os_info.get("ver") if isinstance(os_info, Mapping) else None
    architectures: tuple[str, ...] = ()
    if isinstance(bits, list) and bits:
        architectures = tuple(build_architectures(bits))
    baoku = raw.get("baoku_cate")
    category = category_id = None
    if isinstance(baoku, Mapping):
        category = _text(baoku.get("name")) or None
        # 实测 baoku_cate.id 是整数（149），这里兼容整数/字符串。
        category_id = _string(baoku.get("id")) or None
    return Soft360DetailsFields(
        name=_text(raw.get("name")) or soft_id,
        bname=_text(raw.get("bname")) or None,
        publisher=_text(raw.get("corp")) or None,
        version=_text(edition.get("version")),
        brief=_text(raw.get("brief")),
        long_description=_text(raw.get("desc")),
        size_bytes=parse_size_bytes(raw.get("soft_size")),
        score=parse_score(raw.get("soft_score")),
        category=category,
        category_id=category_id,
        architectures=architectures,
        minimum_windows=minimum_windows_from_versions(versions),
        screenshots=_parse_screenshots(raw.get("snapshot")),
    )


def _parse_screenshots(value: Any) -> tuple[Soft360ScreenshotSource, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise Soft360ProtocolError("360 软件详情截图列表结构无效。")
    screenshots: list[Soft360ScreenshotSource] = []
    for index, item in enumerate(value):
        source = _screenshot_url(item)
        if source is None:
            continue
        screenshots.append(Soft360ScreenshotSource(index=index, source_url=source))
    return tuple(screenshots)


def _screenshot_url(value: Any) -> str | None:
    if isinstance(value, str):
        return normalize_image_url(value)
    if isinstance(value, Mapping):
        for key in ("url", "img", "src", "image", "snapshot"):
            candidate = normalize_image_url(value.get(key))
            if candidate is not None:
                return candidate
    return None


def _iter_url_strings(value: Any):
    """递归收集 ``p2p`` / ``multi_urls`` 等字段里的字符串候选。"""

    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _iter_url_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_url_strings(item)


def _select_download_url(down: Mapping[str, Any]) -> str | None:
    """按 p2p → multi_urls → multi_down 顺序收集候选，https 优先，只接受安装包后缀。"""

    https_candidates: list[str] = []
    http_candidates: list[str] = []
    seen: set[str] = set()
    for field_name in DOWNLOAD_URL_FIELDS:
        for text in _iter_url_strings(down.get(field_name)):
            for candidate in _URL_PATTERN.findall(text):
                normalized = _normalize_download_url(candidate)
                if normalized is None or normalized in seen:
                    continue
                seen.add(normalized)
                if normalized.casefold().startswith("https://"):
                    https_candidates.append(normalized)
                else:
                    http_candidates.append(normalized)
    return (https_candidates or http_candidates or [None])[0]


def _normalize_download_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 4096 or any(ord(character) < 32 for character in text):
        return None
    try:
        parsed = urlsplit(text)
        parsed.port
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    path = parsed.path.casefold()
    if not path.endswith(ALLOWED_INSTALLER_SUFFIXES):
        return None
    return text


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _string(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if type(value) is int:
        return str(value)
    return ""


def _safe_file_name(raw: Any, summary: Soft360AppSummary, version: str) -> str:
    fallback = _fallback_file_name(summary, version)
    if not isinstance(raw, str) or not raw.strip():
        return fallback
    try:
        decoded = unquote(raw.strip(), encoding="utf-8", errors="strict")
    except (UnicodeError, ValueError):
        return fallback
    # 带路径分隔符的“文件名”不可信：不做清洗重解释，直接用安全回退名。
    if "/" in decoded or "\\" in decoded or decoded in (".", ".."):
        return fallback
    cleaned = _clean_file_name(decoded)
    if not cleaned:
        return fallback
    if not cleaned.casefold().endswith(ALLOWED_INSTALLER_SUFFIXES):
        return fallback
    stem = cleaned.split(".", 1)[0].upper()
    if stem in _RESERVED_FILE_STEMS or cleaned.endswith((".", " ")):
        return fallback
    return cleaned


def _fallback_file_name(summary: Soft360AppSummary, version: str) -> str:
    name = _safe_file_component(summary.name) or _safe_file_component(summary.soft_id) or "installer"
    version_text = _safe_file_component(version) or LATEST_VERSION
    return f"{name}_{version_text}.exe"


def _safe_file_component(value: str, *, max_length: int = 60) -> str:
    if not isinstance(value, str):
        return ""
    characters = [
        character
        for character in value.strip()
        if character not in _ILLEGAL_FILE_CHARACTERS and ord(character) >= 32
    ]
    text = "".join(characters).strip().strip(".")
    return text[:max_length].strip()


def _clean_file_name(value: str) -> str:
    characters = [
        character
        for character in value.strip()
        if character not in _ILLEGAL_FILE_CHARACTERS and ord(character) >= 32
    ]
    cleaned = "".join(characters).strip().strip(".")
    if len(cleaned) > 180:
        stem, dot, suffix = cleaned.rpartition(".")
        cleaned = (stem[: max(1, 180 - len(suffix) - 1)] + dot + suffix) if dot else cleaned[:180]
    return cleaned


def _choose_architecture(raw: Mapping[str, Any], summary: Soft360AppSummary, requested: str) -> str:
    supported = list(summary.architectures)
    edition = _default_edition(raw) or {}
    os_info = edition.get("os")
    bits = os_info.get("bit") if isinstance(os_info, Mapping) else None
    if isinstance(bits, list) and bits:
        supported = build_architectures(bits)
    if requested in supported and requested in ("x64", "x86"):
        return requested
    if "x64" in supported:
        return "x64"
    if "x86" in supported:
        return "x86"
    return requested if requested in ("x64", "x86", "arm64") else "x64"


def _release_id(soft_id: str, version: str) -> str:
    raw = f"soft360-{soft_id}-{version}".casefold()
    sanitized = re.sub(r"[^a-z0-9.-]+", "-", raw).strip("-.")
    return (sanitized or "soft360-app")[:100].rstrip("-.") or "soft360-app"


def _prepare_directory(directory: Path | str | None) -> Path | None:
    if directory is None:
        return None
    try:
        text = os.fspath(directory)
    except TypeError as error:
        raise ValueError("directory must be a filesystem path") from error
    if not str(text).strip():
        raise ValueError("directory must be a non-empty path")
    return Path(text).expanduser().resolve()


def _write_json_file(
    path: Path, document: Mapping[str, Any], *, attempts: int = 3, delay: float = 0.05
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    last_error: OSError | None = None
    for attempt in range(1, attempts + 1):
        try:
            _write_once(path, text)
            return
        except OSError as error:
            last_error = error
            if attempt < attempts:
                time.sleep(delay * attempt)
    if last_error is not None:
        raise last_error


def _write_once(path: Path, text: str) -> None:
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


def _delete_json_files(directory: Path) -> int:
    if not directory.is_dir():
        return 0
    deleted = 0
    for path in directory.glob("*.json"):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            continue
        deleted += 1
    return deleted


__all__ = [
    "ALLOWED_INSTALLER_SUFFIXES",
    "DOWNLOAD_URL_FIELDS",
    "DEFAULT_DETAILS_PERSISTENT_TTL_SECONDS",
    "DEFAULT_DETAILS_TTL_SECONDS",
    "Soft360DetailsFields",
    "Soft360DetailsProvider",
    "Soft360ScreenshotSource",
]
