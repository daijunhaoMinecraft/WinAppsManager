"""联想应用详情缓存、视频地址解析与下载地址解析。

- 详情：内存 LRU + TTL，磁盘 ``<cache>/lenovo/details/<sha256(soft_id)>.json``。联想详情里的
  ``captureFileList[].type=1`` 是带 ``auth_key`` 的**临时视频地址**，长期缓存时只保留
  ``videoImg`` 缩略图、丢掉视频 url；``capture`` 列表顺序就是客户端截图索引。
- 详情映射：截图与横版封面优先返回联想 CDN 的**绝对 https 地址**（客户端直连，降低后端图片
  带宽），地址不合格时回退 ``/api/v1/store/lenovo/...`` 相对代理路径；``proxy_url`` /
  ``cover_proxy_url`` 始终是代理路径，作为直连失败时的兜底。
- 视频：:meth:`LenovoDetailsProvider.resolve_screenshot_video_url` 每次都实时请求联想详情
  （不读也不写缓存），只返回通过 https + 联想视频 CDN 白名单校验的临时地址，供路由流式代理；
  该 URL 绝不写日志、缓存或磁盘。
- 下载：``getPcSoftDownloadUrlList`` 的地址有时效，**绝不缓存**。按
  ``hs2 > bs2 > amsHttpsDownloadUrl > firstOldDownloadUrl > secondOldDownloadUrl`` 选第一个
  https 地址，``fileName`` 先 URL 解码再按 Windows 文件名规则清洗，扩展名只允许
  ``.exe/.msi/.msix/.appx``，否则回退 ``{安全应用名}_{版本}.exe``，最后映射成
  :class:`Backend.models.Release`。
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable, Mapping
import json
import logging
import math
import os
from dataclasses import dataclass
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

from .client import LenovoRawClient
from .errors import (
    LenovoError,
    LenovoNotFoundError,
    LenovoProtocolError,
    LenovoUnavailableError,
)
from .models import (
    StoreAppDetails,
    StoreAppSummary,
    StoreDetailTag,
    StoreScreenshot,
    cdn_image_url,
)

logger = logging.getLogger("winappsmanager.lenovo")

DETAILS_SCHEMA_VERSION = 1
DEFAULT_DETAILS_TTL_SECONDS = 900.0
DEFAULT_DETAILS_PERSISTENT_TTL_SECONDS = 21600.0
DEFAULT_DETAILS_MAX_ENTRIES = 256

# 下载地址 code 的优先级：https 的 pc-lsw7 优先，其次 pc-lsw6、AMS、旧 CDN。
DOWNLOAD_PRIORITY: tuple[str, ...] = (
    "hs2",
    "bs2",
    "amsHttpsDownloadUrl",
    "firstOldDownloadUrl",
    "secondOldDownloadUrl",
)
ALLOWED_INSTALLER_SUFFIXES = (".exe", ".msi", ".msix", ".appx")
# 视频代理只允许联想视频 CDN：显式列出已知主机，并放宽到 *.lenovomm.cn（大小写不敏感）。
ALLOWED_VIDEO_HOSTS = frozenset({"pc-store-video.lenovomm.cn"})
ALLOWED_VIDEO_DOMAIN_SUFFIXES = (".lenovomm.cn",)
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
class ScreenshotSource:
    """截图来源：``source_url`` 是原图或视频缩略图地址（视频地址本身不保存）。"""

    index: int
    kind: str
    source_url: str


@dataclass(frozen=True)
class DetailsFields:
    """详情页专属字段（不含从目录 summary 继承的基础字段）。"""

    long_description: str
    release_notes: str
    screenshots: tuple[ScreenshotSource, ...]
    cover_source: str | None
    labels: tuple[str, ...]
    ranking: str | None
    detail_tags: tuple[tuple[str, str | None], ...]


@dataclass(frozen=True)
class _MemoryEntry:
    fields: DetailsFields
    expires_at: float


class LenovoDetailsProvider:
    """详情抓取 + 内存/磁盘缓存 + 下载地址解析。

    ``directory`` 是联想缓存根目录（``<cache_dir>/lenovo``），详情写在其中的 ``details/`` 下。
    """

    def __init__(
        self,
        client: LenovoRawClient,
        directory: Path | str | None = None,
        *,
        ttl_seconds: float = DEFAULT_DETAILS_TTL_SECONDS,
        persistent_ttl_seconds: float = DEFAULT_DETAILS_PERSISTENT_TTL_SECONDS,
        max_entries: int = DEFAULT_DETAILS_MAX_ENTRIES,
        cache_enabled: bool = True,
        cache_persist: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(client, LenovoRawClient):
            raise ValueError("client must be a LenovoRawClient")
        for name, value in (
            ("ttl_seconds", ttl_seconds),
            ("persistent_ttl_seconds", persistent_ttl_seconds),
        ):
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
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

    async def get_fields(self, soft_id: str, *, force_refresh: bool = False) -> DetailsFields:
        """读取详情专属字段；失败时尽量回退到陈旧缓存（下架除外）。"""

        if not isinstance(soft_id, str) or not soft_id:
            raise ValueError("soft_id must be a non-empty string")
        if not self._cache_enabled:
            return await self._fetch(soft_id)

        now = self._clock()
        entry = self._memory.get(soft_id)
        if entry is not None and not force_refresh:
            if now < entry.expires_at:
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
        except LenovoNotFoundError:
            # 已下架是确定结论，不回退陈旧详情。
            raise
        except LenovoError:
            fallback = stale
            if fallback is None and self._cache_persist:
                expired = await self._load_disk(soft_id, allow_expired=True)
                fallback = expired[0] if expired is not None else None
            if fallback is None:
                raise
            self._stale_hits += 1
            logger.warning("联想应用 %s 详情抓取失败，回退到陈旧详情。", soft_id)
            return fallback

        self._remember(soft_id, fields)
        if self._cache_persist:
            await self._save_disk(soft_id, fields)
        return fields

    def build_details(self, summary: StoreAppSummary, fields: DetailsFields) -> StoreAppDetails:
        """把目录 summary 与详情字段合并成 :class:`StoreAppDetails`。

        ``icon_url``（联想 CDN 绝对地址）与 ``icon_proxy_url``（后端代理兜底）随 summary
        原样继承，客户端详情页同样优先直连。

        截图与横版封面优先返回联想 CDN 的绝对 https 地址（客户端直连，降低后端图片带宽）；
        地址不是 https 或 host 不在联想 CDN 白名单时回退后端相对代理路径。``proxy_url`` /
        ``cover_proxy_url`` 始终是代理路径，供客户端直连失败时兜底。
        """

        soft_id = summary.soft_id
        encoded_soft_id = quote(soft_id, safe="")
        screenshots = [
            _build_screenshot(encoded_soft_id, screenshot) for screenshot in fields.screenshots
        ]
        cover_proxy_url = (
            f"/api/v1/store/lenovo/apps/{encoded_soft_id}/cover"
            if fields.cover_source is not None
            else None
        )
        cover_url = (
            cdn_image_url(fields.cover_source) if fields.cover_source is not None else None
        )
        if cover_url is None:
            cover_url = cover_proxy_url
        return StoreAppDetails(
            **summary.model_dump(),
            long_description=fields.long_description,
            release_notes=fields.release_notes,
            screenshots=screenshots,
            cover_url=cover_url,
            cover_proxy_url=cover_proxy_url,
            labels=list(fields.labels),
            ranking=fields.ranking,
            detail_tags=[
                StoreDetailTag(name=name, tips=tips) for name, tips in fields.detail_tags
            ],
        )

    async def resolve_screenshot_video_url(self, soft_id: str, index: int) -> str:
        """实时解析视频截图的临时播放地址（带 ``auth_key``，绝不缓存、绝不记录日志）。

        每次都重新请求联想详情接口：既不读内存/磁盘详情缓存（缓存里本来就没有视频 url），
        也不写任何缓存。返回的地址只用于本次代理转发，调用方不得持久化或写日志。

        - ``index`` 越界 / 对应条目不是视频（``type != "1"``）/ 视频地址缺失 →
          :class:`LenovoNotFoundError`（路由映射 404）；
        - 视频地址不是 https、含用户信息或主机不在联想视频 CDN 白名单 →
          :class:`LenovoProtocolError`（映射 502），避免把任意地址当视频源转发。
        """

        if not isinstance(soft_id, str) or not soft_id:
            raise ValueError("soft_id must be a non-empty string")
        if type(index) is not int or index < 0:
            raise LenovoNotFoundError("联想应用截图索引无效。")

        # 不走 get_fields()：强制实时抓取且不写缓存，拿到的视频地址不进入任何缓存结构。
        data = await self._client.app_details(soft_id)
        if not isinstance(data, Mapping):
            raise LenovoProtocolError("联想应用详情响应结构无效。")
        _ensure_detail_identity(data, soft_id)

        capture_list = data.get("captureFileList")
        if capture_list is None:
            raise LenovoNotFoundError(f"联想应用商店中找不到 {soft_id} 的视频截图。")
        if not isinstance(capture_list, list):
            raise LenovoProtocolError("联想应用详情截图列表结构无效。")
        if index >= len(capture_list):
            raise LenovoNotFoundError(f"联想应用商店中找不到 {soft_id} 的截图 {index}。")
        item = capture_list[index]
        if not isinstance(item, Mapping):
            raise LenovoProtocolError("联想应用详情截图条目结构无效。")
        if _text(item.get("type")) != "1":
            raise LenovoNotFoundError(f"联想应用商店中 {soft_id} 的截图 {index} 不是视频。")
        video_url = item.get("url")
        if not isinstance(video_url, str) or not video_url.strip():
            raise LenovoNotFoundError(f"联想应用商店中找不到 {soft_id} 的视频地址。")
        return _normalized_video_url(video_url)

    async def resolve_download(
        self,
        soft_id: str,
        summary: StoreAppSummary,
        architecture: str = "x64",
    ) -> Release:
        """解析当前可用的下载地址；结果绝不缓存。"""

        if not isinstance(summary, StoreAppSummary):
            raise LenovoProtocolError("联想应用目录条目类型无效。")
        data = await self._client.download_url_list(soft_id)
        if not isinstance(data, Mapping):
            raise LenovoProtocolError("联想下载地址响应结构无效。")

        selected = _select_download_url(data.get("downloadUrls"))
        if selected is None:
            raise LenovoUnavailableError(
                f"联想应用商店暂时没有 {summary.name} 的可用下载地址。"
            )
        sha256_value = _normalize_sha256(data.get("sha256"))
        file_name = _safe_file_name(data.get("fileName"), summary)
        try:
            return Release(
                id=_release_id(summary),
                version=summary.version,
                platform="windows",
                architecture=_choose_architecture(summary, architecture),
                download_url=selected,
                file_name=file_name,
                size_bytes=summary.size_bytes,
                sha256=sha256_value,
            )
        except ValidationError:
            raise LenovoProtocolError("联想下载地址响应无法映射成有效的版本资源。") from None

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

    async def _fetch(self, soft_id: str) -> DetailsFields:
        data = await self._client.app_details(soft_id)
        if not isinstance(data, Mapping):
            raise LenovoProtocolError("联想应用详情响应结构无效。")
        _ensure_detail_identity(data, soft_id)

        screenshots = _parse_screenshots(data.get("captureFileList"))
        cover = data.get("cover")
        cover_source = None
        if isinstance(cover, Mapping):
            cover_source = _safe_image_url(cover.get("coverImg"))

        labels: list[str] = []
        label_items = data.get("appLabelList")
        if isinstance(label_items, list):
            for item in label_items:
                if not isinstance(item, Mapping):
                    continue
                label = _text(item.get("labelName"))
                if label and label not in labels:
                    labels.append(label)

        ranking = None
        ranking_info = data.get("classRankingDesc")
        if isinstance(ranking_info, Mapping):
            ranking = _text(ranking_info.get("targetDesc")) or None

        detail_tags: list[tuple[str, str | None]] = []
        tag_items = data.get("detailsTag")
        if isinstance(tag_items, list):
            for item in tag_items:
                if not isinstance(item, Mapping):
                    continue
                name = _text(item.get("name"))
                if not name or any(existing == name for existing, _ in detail_tags):
                    continue
                tips = _text(item.get("tips")) or None
                detail_tags.append((name, tips))

        return DetailsFields(
            long_description=_text(data.get("detailInfo")),
            release_notes=_text(data.get("whatNew")),
            screenshots=screenshots,
            cover_source=cover_source,
            labels=tuple(labels),
            ranking=ranking,
            detail_tags=tuple(detail_tags),
        )

    # -- 内存缓存 ---------------------------------------------------------

    def _remember(self, soft_id: str, fields: DetailsFields) -> None:
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
    ) -> tuple[DetailsFields, bool] | None:
        path = self._path_for(soft_id)
        if path is None:
            return None
        try:
            loaded = await asyncio.to_thread(self._read_disk_sync, path, soft_id)
        except Exception:  # 缓存读取永远不能中断 API 请求。
            logger.warning("读取联想详情缓存失败: %s", path, exc_info=True)
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
    def _read_disk_sync(path: Path, soft_id: str) -> tuple[DetailsFields, float] | None:
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
        screenshots: list[ScreenshotSource] = []
        for item in screenshots_raw:
            if not isinstance(item, Mapping):
                return None
            index = item.get("index")
            kind = item.get("kind")
            source_url = item.get("source_url")
            if type(index) is not int or index < 0:
                return None
            if kind not in ("image", "video") or not isinstance(source_url, str) or not source_url:
                return None
            screenshots.append(ScreenshotSource(index=index, kind=kind, source_url=source_url))
        labels = raw.get("labels")
        labels_tuple = tuple(item for item in labels if isinstance(item, str)) if isinstance(labels, list) else ()
        detail_tags_raw = raw.get("detail_tags")
        detail_tags: list[tuple[str, str | None]] = []
        if isinstance(detail_tags_raw, list):
            for item in detail_tags_raw:
                if not isinstance(item, Mapping):
                    return None
                name = item.get("name")
                tips = item.get("tips")
                if not isinstance(name, str) or not name:
                    return None
                if tips is not None and not isinstance(tips, str):
                    return None
                detail_tags.append((name, tips))
        long_description = raw.get("long_description")
        release_notes = raw.get("release_notes")
        cover_source = raw.get("cover_source")
        ranking = raw.get("ranking")
        if not isinstance(long_description, str) or not isinstance(release_notes, str):
            return None
        if cover_source is not None and not isinstance(cover_source, str):
            return None
        if ranking is not None and not isinstance(ranking, str):
            return None
        return (
            DetailsFields(
                long_description=long_description,
                release_notes=release_notes,
                screenshots=tuple(screenshots),
                cover_source=cover_source,
                labels=labels_tuple,
                ranking=ranking,
                detail_tags=tuple(detail_tags),
            ),
            float(fetched_at),
        )

    async def _save_disk(self, soft_id: str, fields: DetailsFields) -> None:
        path = self._path_for(soft_id)
        if path is None:
            return
        document = {
            "schema_version": DETAILS_SCHEMA_VERSION,
            "soft_id": soft_id,
            "fetched_at": float(self._clock()),
            "long_description": fields.long_description,
            "release_notes": fields.release_notes,
            # 注意：视频 url 带 auth_key，有有效期；这里只保存截图/缩略图地址。
            "screenshots": [
                {"index": item.index, "kind": item.kind, "source_url": item.source_url}
                for item in fields.screenshots
            ],
            "cover_source": fields.cover_source,
            "labels": list(fields.labels),
            "ranking": fields.ranking,
            "detail_tags": [{"name": name, "tips": tips} for name, tips in fields.detail_tags],
        }
        try:
            await asyncio.to_thread(_write_json_file, path, document)
        except (OSError, TypeError, ValueError):
            logger.warning("写入联想详情缓存失败: %s", path, exc_info=True)

    @staticmethod
    async def _discard(path: Path) -> None:
        try:
            await asyncio.to_thread(path.unlink, True)
        except OSError:
            pass


def _build_screenshot(encoded_soft_id: str, screenshot: ScreenshotSource) -> StoreScreenshot:
    """构造截图契约：CDN 绝对 https 优先，否则回退后端相对代理路径。"""

    proxy_url = (
        f"/api/v1/store/lenovo/apps/{encoded_soft_id}/screenshots/{screenshot.index}"
    )
    return StoreScreenshot(
        index=screenshot.index,
        kind="video" if screenshot.kind == "video" else "image",
        url=cdn_image_url(screenshot.source_url) or proxy_url,
        proxy_url=proxy_url,
    )


def _ensure_detail_identity(data: Mapping[str, Any], soft_id: str) -> None:
    """详情响应的 ``softID`` 非空时必须与请求的 soft_id 一致（大小写不敏感）。"""

    detail_soft_id = data.get("softID")
    if isinstance(detail_soft_id, str) and detail_soft_id.strip():
        if detail_soft_id.strip().casefold() != soft_id.casefold():
            raise LenovoProtocolError("联想应用详情返回了不同的应用。")


def _normalized_video_url(value: str) -> str:
    """校验视频地址：https、无用户信息、主机属于联想视频 CDN 白名单。

    失败信息绝不包含 URL 本身（可能带 ``auth_key``），只说明校验原因。
    """

    text = value.strip()
    if not text or any(ord(character) < 32 for character in text):
        raise LenovoProtocolError("联想视频地址无效。")
    try:
        parsed = urlsplit(text)
        parsed.port
    except ValueError:
        raise LenovoProtocolError("联想视频地址无效。") from None
    if parsed.scheme.casefold() != "https" or not parsed.hostname:
        raise LenovoProtocolError("联想视频地址必须是 https URL。")
    if parsed.username is not None or parsed.password is not None:
        raise LenovoProtocolError("联想视频地址不允许包含用户信息。")
    if not _video_host_allowed(parsed.hostname):
        raise LenovoProtocolError("联想视频地址不在允许的域名内。")
    return text


def _video_host_allowed(hostname: str) -> bool:
    host = hostname.casefold().rstrip(".")
    if not host:
        return False
    if host in ALLOWED_VIDEO_HOSTS:
        return True
    return any(
        host.endswith(suffix) and len(host) > len(suffix)
        for suffix in ALLOWED_VIDEO_DOMAIN_SUFFIXES
    )


def _parse_screenshots(value: Any) -> tuple[ScreenshotSource, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise LenovoProtocolError("联想应用详情截图列表结构无效。")
    screenshots: list[ScreenshotSource] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            continue
        kind_value = _text(item.get("type"))
        if kind_value == "1":
            # 视频：只保留 videoImg 缩略图，url（带 auth_key）不进入任何缓存。
            source = _safe_image_url(item.get("videoImg"))
            kind = "video"
        elif kind_value == "0":
            source = _safe_image_url(item.get("url"))
            kind = "image"
        else:
            continue
        if source is None:
            continue
        screenshots.append(ScreenshotSource(index=index, kind=kind, source_url=source))
    return tuple(screenshots)


def _safe_image_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or any(ord(character) < 32 for character in text):
        return None
    try:
        parsed = urlsplit(text)
        parsed.port
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    return text


def _select_download_url(value: Any) -> str | None:
    if not isinstance(value, list):
        return None
    by_code: dict[str, list[str]] = {}
    for item in value:
        if not isinstance(item, Mapping):
            continue
        code = _text(item.get("downloadCode"))
        url = item.get("downLoadUrl")
        if code and isinstance(url, str):
            by_code.setdefault(code, []).append(url)
    for code in DOWNLOAD_PRIORITY:
        for candidate in by_code.get(code, ()):
            normalized = _normalize_download_url(candidate)
            if normalized is not None:
                return normalized
    return None


def _normalize_download_url(value: str) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or any(ord(character) < 32 for character in text):
        return None
    try:
        parsed = urlsplit(text)
        parsed.port
    except ValueError:
        return None
    if parsed.scheme.casefold() != "https" or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    return text


def _normalize_sha256(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) != 64 or any(character not in "0123456789abcdefABCDEF" for character in text):
        return None
    return text.lower()


def _safe_file_name(raw: Any, summary: StoreAppSummary) -> str:
    fallback = _fallback_file_name(summary)
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


def _fallback_file_name(summary: StoreAppSummary) -> str:
    name = _safe_file_component(summary.name) or _safe_file_component(summary.soft_id) or "installer"
    version = _safe_file_component(summary.version) or "latest"
    return f"{name}_{version}.exe"


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


def _choose_architecture(summary: StoreAppSummary, requested: str) -> str:
    supported = {architecture for architecture in summary.architectures if architecture in ("x64", "x86")}
    if requested in supported:
        return requested
    if "x64" in supported:
        return "x64"
    if "x86" in supported:
        return "x86"
    return requested if requested in ("x64", "x86", "arm64") else "x64"


def _release_id(summary: StoreAppSummary) -> str:
    raw = f"lenovo-{summary.soft_id}-{summary.version}".casefold()
    sanitized = re.sub(r"[^a-z0-9.-]+", "-", raw).strip("-.")
    return (sanitized or "lenovo-app")[:100].rstrip("-.") or "lenovo-app"


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


def _write_json_file(path: Path, document: Mapping[str, Any], *, attempts: int = 3, delay: float = 0.05) -> None:
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


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


__all__ = [
    "ALLOWED_INSTALLER_SUFFIXES",
    "ALLOWED_VIDEO_DOMAIN_SUFFIXES",
    "ALLOWED_VIDEO_HOSTS",
    "DEFAULT_DETAILS_PERSISTENT_TTL_SECONDS",
    "DEFAULT_DETAILS_TTL_SECONDS",
    "DOWNLOAD_PRIORITY",
    "DetailsFields",
    "LenovoDetailsProvider",
    "ScreenshotSource",
]
