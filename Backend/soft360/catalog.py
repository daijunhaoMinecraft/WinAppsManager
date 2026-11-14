"""360 软件源目录：逐分类分页抓取（builder）+ 内存/磁盘快照（store）。

接口结论（详见任务说明与 ``Backend/README.md`` 的“360 软件源”章节）：

- 360 应用中心共有 24 个可见分类，分类 ID/名称是固定清单（接口不提供分类树）；
- 每个分类的软件列表接口是 ``soft/list?cate_id=...&page_size=100&page=N``，
  ``page_size`` 上限 100，``data`` 为空数组表示没有更多页；
- 360 目录总量约 43731 款，全量抓取不现实。本项目按**各分类前 N 页**抓取，默认每分类 3 页
  （约 7200 条原始记录，按 ``soft_id`` 去重后约 5940 条），与联想源 5111 条规模相当；
  ``catalog_pages_per_category`` 可配置 1–20。客户端与文档必须写明这是“热门子集”。

本模块职责：

- :class:`Soft360CatalogBuilder`：按 24 个分类并发抓取（并发上限可配置），某分类某页失败
  重试一次后整轮失败（绝不发布半成品）；按 ``soft_id`` 去重，**首个出现的分类**作为归属分类，
  ``sort_order`` 是该分类内的位置；列表项缺少 ``soft_id`` / ``name`` / 合法 logo 地址时跳过。
- :class:`Soft360CatalogStore`：内存快照 + ``<cache_dir>/soft360/catalog.json`` 磁盘快照，
  原子写、损坏忽略。**请求路径只读缓存、绝不联网**：内存快照直接返回，内存没有时读磁盘快照，
  两者都没有时返回 ``None``（服务层据此返回空目录），目录更新完全交给后台刷新线程与
  ``POST /store/360/refresh``（见 ``service.py``）。
- :class:`CatalogSnapshot`：抓取时间、应用列表与 24 个分类计数；``stale`` 由快照年龄与
  ``catalog_ttl_seconds`` 计算，只用于提示客户端目录偏旧。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any
from urllib.parse import quote, urlsplit

from pydantic import ValidationError

from .client import DEFAULT_PAGE_SIZE, Soft360RawClient
from .errors import Soft360Error, Soft360ProtocolError, Soft360UnavailableError
from .models import (
    INSTALLATION_NOTES,
    Soft360AppSummary,
    Soft360Category,
)

logger = logging.getLogger("winappsmanager.soft360")

SOURCE_ID = "soft360"
CATALOG_SCHEMA_VERSION = 1
CATALOG_FILE_NAME = "catalog.json"
DEFAULT_CATALOG_TTL_SECONDS = 21600.0
DEFAULT_CATALOG_REFRESH_INTERVAL_SECONDS = 1800.0
DEFAULT_CATALOG_MAX_STALE_SECONDS = 604800.0
DEFAULT_PAGES_PER_CATEGORY = 3
DEFAULT_CONCURRENCY = 4
MAX_PAGES_PER_CATEGORY = 20
MAX_DESCRIPTION_LENGTH = 300

# 24 个分类的固定清单（360 页面左侧分类）。顺序即抓取与展示顺序；同一软件出现在多个分类时，
# 以此处的第一个分类作为归属分类。
SOFT360_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("149", "聊天工具"),
    ("150", "视频软件"),
    ("157", "浏览器"),
    ("159", "办公软件"),
    ("156", "输入法"),
    ("151", "音乐软件"),
    ("153", "游戏娱乐"),
    ("100000409", "网络游戏"),
    ("1900000002", "手游电脑版"),
    ("152", "下载工具"),
    ("155", "安全杀毒"),
    ("154", "图形图像"),
    ("162", "系统工具"),
    ("165", "手机数码"),
    ("158", "股票网银"),
    ("161", "压缩刻录"),
    ("100000186", "阅读翻译"),
    ("168", "网络应用"),
    ("100000376", "主题壁纸"),
    ("167", "教育学习"),
    ("163", "编程开发"),
    ("169", "其它软件"),
    ("1900000001", "XP专区"),
    ("1900000010", "win7专区"),
)

# os.ver 是 360 客户端的 Windows 版本序号；接口没有公开编码文档，这里按官方客户端常见的
# 系统枚举顺序 1..8 映射到 Windows 2000 → 10。低于 7 的版本在本项目中按最低支持 7 处理。
_WINDOWS_BY_OS_VERSION: dict[int, str] = {
    1: "7",  # Windows 2000（低于 7，按下限）
    2: "7",  # Windows XP（低于 7，按下限）
    3: "7",  # Windows 2003（低于 7，按下限）
    4: "7",  # Windows Vista（低于 7，按下限）
    5: "7",
    6: "8",
    7: "8.1",
    8: "10",
}
_WINDOWS_RANK = {"7": 7.0, "8": 8.0, "8.1": 8.1, "10": 10.0, "11": 11.0}

_SOFT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
_ID_SANITIZER = re.compile(r"[^a-z0-9.-]+")
_APP_ID_PREFIX = "soft360-"


# ---------------------------------------------------------------------------
# 规范化辅助函数（供 builder / 服务层 / 测试复用）
# ---------------------------------------------------------------------------


def app_id_for(soft_id: str) -> str:
    """客户端本地主键：``soft360-`` + 小写 soft_id，且满足 ResourceId 正则。"""

    sanitized = _ID_SANITIZER.sub("-", soft_id.casefold()).strip("-.")
    return f"{_APP_ID_PREFIX}{sanitized or 'app'}"


def normalize_soft_id(value: Any) -> str | None:
    """校验并返回 360 ``soft_id``；同时接受客户端主键 ``soft360-{soft_id}``。"""

    text = _string(value)
    if text.casefold().startswith(_APP_ID_PREFIX):
        text = text[len(_APP_ID_PREFIX) :]
    if not text or _SOFT_ID_PATTERN.fullmatch(text) is None:
        return None
    return text


def normalize_image_url(value: Any) -> str | None:
    """校验图片地址并优先规范化为 https（360 图片 CDN 同时支持 http/https）。"""

    text = _text(value)
    if not text or len(text) > 1024 or any(ord(character) < 32 for character in text):
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
    if parsed.scheme == "http":
        return "https" + text[len("http") :]
    return text


def logo_url_from(value: Any) -> str | None:
    """从列表项的 ``logo{32,48,48_trans,72}`` 里挑一个可直连的地址（优先大图）。"""

    if isinstance(value, Mapping):
        for key in ("72", "48_trans", "48", "96", "64", "32"):
            candidate = normalize_image_url(value.get(key))
            if candidate is not None:
                return candidate
        for candidate in value.values():
            normalized = normalize_image_url(candidate)
            if normalized is not None:
                return normalized
        return None
    return normalize_image_url(value)


def parse_size_bytes(value: Any) -> int | None:
    """解析安装包大小（360 ``soft_size`` / ``down.default.size`` 是字节整数或数字字符串）。"""

    if type(value) is int:
        return value if value >= 1 else None
    if isinstance(value, str) and value.strip().isdigit():
        try:
            parsed = int(value.strip())
        except ValueError:
            return None
        return parsed if parsed >= 1 else None
    return None


def parse_score(value: Any) -> int | None:
    """解析 ``soft_score``（0–100 的整数）；超出范围或非法时返回 ``None``。"""

    score = _coerce_int(value)
    return score if score is not None and 0 <= score <= 100 else None


def truncate_description(value: Any, fallback: str) -> str:
    """把 ``brief`` / ``desc`` 截断到 300 字以内；空值时回退软件名。"""

    text = _text(value)
    if not text:
        return fallback
    return text[:MAX_DESCRIPTION_LENGTH]


def build_architectures(bits: Any) -> list[str]:
    """由 ``os.bit``（``32`` / ``64``）推断架构；无有效值时默认同时支持 x86 与 x64。"""

    values = _int_list(bits)
    architectures: list[str] = []
    if 32 in values:
        architectures.append("x86")
    if 64 in values:
        architectures.append("x64")
    return architectures or ["x86", "x64"]


def minimum_windows_from_versions(versions: Any) -> str | None:
    """由 ``os.ver`` 取最低支持的 Windows 版本（7/8/8.1/10）；无有效值返回 ``None``。"""

    supported: list[str] = []
    for value in _int_list(versions):
        mapped = _WINDOWS_BY_OS_VERSION.get(value)
        if mapped is not None and mapped not in supported:
            supported.append(mapped)
    if not supported:
        return None
    return min(supported, key=lambda item: _WINDOWS_RANK[item])


def build_summary(
    raw: Mapping[str, Any],
    *,
    category_id: str,
    category_name: str,
    position: int,
) -> Soft360AppSummary | None:
    """把 360 列表项规范化成 :class:`Soft360AppSummary`。

    ``soft_id`` / ``name`` / 合法 logo 地址任一缺失时返回 ``None``（调用方跳过并计数）。
    """

    soft_id = _string(raw.get("soft_id"))
    if not soft_id or _SOFT_ID_PATTERN.fullmatch(soft_id) is None:
        return None
    name = _text(raw.get("name"))
    if not name:
        return None
    logo_url = logo_url_from(raw.get("logo"))
    if logo_url is None:
        return None
    bname = _text(raw.get("bname")) or None
    encoded_soft_id = quote(soft_id, safe="")
    return Soft360AppSummary(
        id=app_id_for(soft_id),
        soft_id=soft_id,
        name=name,
        bname=bname,
        description=truncate_description(raw.get("brief") or raw.get("desc"), name),
        category=category_name,
        category_id=category_id,
        size_bytes=parse_size_bytes(raw.get("soft_size")),
        score=parse_score(raw.get("soft_score")),
        icon_url=logo_url,
        icon_proxy_url=f"/api/v1/store/360/apps/{encoded_soft_id}/icon",
        architectures=["x86", "x64"],
        minimum_windows="7",
        tags=[category_name],
        license="免费",
        is_free=True,
        sort_order=position,
        installation_notes=INSTALLATION_NOTES,
    )


# ---------------------------------------------------------------------------
# 目录快照与 builder
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CategorySpec:
    """一个 360 分类的固定定义。"""

    id: str
    name: str


@dataclass(frozen=True)
class CatalogSnapshot:
    """一次完整抓取（或磁盘载入）的目录快照。"""

    apps: tuple[Soft360AppSummary, ...]
    categories: tuple[Soft360Category, ...]
    fetched_at: float
    stale: bool = False

    def with_stale(self, stale: bool) -> "CatalogSnapshot":
        return replace(self, stale=stale)


@dataclass(frozen=True)
class _FetchedCategory:
    spec: CategorySpec
    items: tuple[Mapping[str, Any], ...]


@dataclass
class _Record:
    raw: Mapping[str, Any]
    spec: CategorySpec
    position: int


def category_specs() -> tuple[CategorySpec, ...]:
    """24 个固定分类（id/name），顺序即抓取与展示顺序。"""

    return tuple(CategorySpec(category_id, name) for category_id, name in SOFT360_CATEGORIES)


class Soft360CatalogBuilder:
    """按分类分页抓取 360 软件目录并合并去重。"""

    def __init__(
        self,
        client: Soft360RawClient,
        *,
        pages_per_category: int = DEFAULT_PAGES_PER_CATEGORY,
        page_size: int = DEFAULT_PAGE_SIZE,
        concurrency: int = DEFAULT_CONCURRENCY,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(client, Soft360RawClient):
            raise ValueError("client must be a Soft360RawClient")
        if type(pages_per_category) is not int or not 1 <= pages_per_category <= MAX_PAGES_PER_CATEGORY:
            raise ValueError(
                f"pages_per_category must be an integer between 1 and {MAX_PAGES_PER_CATEGORY}"
            )
        if type(page_size) is not int or not 1 <= page_size <= DEFAULT_PAGE_SIZE:
            raise ValueError(f"page_size must be an integer between 1 and {DEFAULT_PAGE_SIZE}")
        if type(concurrency) is not int or not 1 <= concurrency <= 16:
            raise ValueError("concurrency must be an integer between 1 and 16")
        if not callable(clock):
            raise ValueError("clock must be callable")
        self._client = client
        self._pages_per_category = pages_per_category
        self._page_size = page_size
        self._concurrency = concurrency
        self._clock = clock

    async def build(self) -> CatalogSnapshot:
        """抓取全部 24 个分类并返回快照；任一分类最终失败则整轮失败。"""

        specs = category_specs()
        semaphore = asyncio.Semaphore(self._concurrency)

        async def fetch(spec: CategorySpec) -> _FetchedCategory:
            async with semaphore:
                return await self._fetch_category(spec)

        results = await asyncio.gather(*(fetch(spec) for spec in specs), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                # 单个分类失败会让整轮目录抓取失败：调用方保留旧快照，绝不发布半成品。
                raise result
        fetched = [item for item in results if isinstance(item, _FetchedCategory)]
        apps = self._merge(fetched)
        return CatalogSnapshot(
            apps=tuple(apps),
            categories=tuple(self._category_counts(apps)),
            fetched_at=float(self._clock()),
        )

    async def _fetch_category(self, spec: CategorySpec) -> _FetchedCategory:
        """抓取一个分类的前 N 页；失败重试一次，仍失败则抛出。"""

        last_error: Soft360Error | None = None
        for attempt in range(2):
            try:
                return await self._fetch_category_once(spec)
            except Soft360Error as error:
                last_error = error
                if attempt == 0:
                    logger.warning("360 分类“%s”抓取失败，重试一次：%s", spec.name, error)
        assert last_error is not None
        raise last_error

    async def _fetch_category_once(self, spec: CategorySpec) -> _FetchedCategory:
        items: list[Mapping[str, Any]] = []
        for page in range(1, self._pages_per_category + 1):
            data = await self._client.list_softs(spec.id, page=page, page_size=self._page_size)
            raw_items = [item for item in data if isinstance(item, Mapping)]
            items.extend(raw_items)
            if not data:
                # 空数组表示该分类已经没有更多页。
                break
        return _FetchedCategory(spec=spec, items=tuple(items))

    def _merge(self, fetched: list[_FetchedCategory]) -> list[Soft360AppSummary]:
        records: dict[str, _Record] = {}
        order: list[str] = []
        skipped = 0
        skipped_logo = 0
        for item in fetched:
            for position, raw in enumerate(item.items):
                soft_id = _string(raw.get("soft_id"))
                if not soft_id or _SOFT_ID_PATTERN.fullmatch(soft_id) is None:
                    skipped += 1
                    continue
                if not _text(raw.get("name")):
                    skipped += 1
                    continue
                if soft_id in records:
                    continue
                if logo_url_from(raw.get("logo")) is None:
                    skipped_logo += 1
                    continue
                records[soft_id] = _Record(raw=raw, spec=item.spec, position=position)
                order.append(soft_id)
        if skipped:
            logger.debug("360 目录跳过了 %s 条缺少 soft_id / name 的条目。", skipped)
        if skipped_logo:
            logger.warning("360 目录跳过了 %s 条缺少有效 logo 地址的条目。", skipped_logo)
        if not order:
            raise Soft360ProtocolError("360 软件目录没有可用软件。")

        apps: list[Soft360AppSummary] = []
        for soft_id in order:
            record = records[soft_id]
            summary = build_summary(
                record.raw,
                category_id=record.spec.id,
                category_name=record.spec.name,
                position=record.position,
            )
            if summary is None:  # pragma: no cover - 前面已做同样的校验
                continue
            apps.append(summary)
        return apps

    @staticmethod
    def _category_counts(apps: list[Soft360AppSummary]) -> list[Soft360Category]:
        counts: dict[str, int] = {}
        for app in apps:
            counts[app.category_id] = counts.get(app.category_id, 0) + 1
        return [
            Soft360Category(id=spec.id, name=spec.name, count=counts.get(spec.id, 0))
            for spec in category_specs()
        ]


# ---------------------------------------------------------------------------
# 目录缓存（内存 + 磁盘快照；请求只读，绝不在请求路径联网）
# ---------------------------------------------------------------------------


class Soft360CatalogStore:
    """360 目录快照缓存（请求只读缓存 + 后台定时刷新）。

    ``directory`` 是 360 缓存根目录（即 ``<cache_dir>/soft360``），磁盘文件为其中的
    ``catalog.json``。``cache_enabled=False`` 时不使用任何缓存，``cache_persist=False`` 时只
    保留内存快照。

    请求路径（:meth:`get`）语义：

    - 内存快照存在：直接返回，绝不联网；
    - 内存没有但磁盘有：读取 ``catalog.json`` 载入内存并返回，不联网；
    - 两边都没有：返回 ``None``（服务层据此返回空目录）；**绝不**在请求路径抓取 360。

    目录刷新只由 :meth:`refresh_now` 完成（``POST /store/360/refresh`` 与后台刷新线程）。
    """

    def __init__(
        self,
        builder: Soft360CatalogBuilder,
        directory: Path | str | None = None,
        *,
        ttl_seconds: float = DEFAULT_CATALOG_TTL_SECONDS,
        max_stale_seconds: float = DEFAULT_CATALOG_MAX_STALE_SECONDS,
        cache_enabled: bool = True,
        cache_persist: bool = True,
        refresh_interval_seconds: float = 0.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(builder, Soft360CatalogBuilder):
            raise ValueError("builder must be a Soft360CatalogBuilder")
        for name, value in (("ttl_seconds", ttl_seconds), ("max_stale_seconds", max_stale_seconds)):
            if type(value) not in (int, float) or not math.isfinite(float(value)) or value < 0:
                raise ValueError(f"{name} must be a finite non-negative value")
        if (
            type(refresh_interval_seconds) not in (int, float)
            or not math.isfinite(float(refresh_interval_seconds))
            or refresh_interval_seconds < 0
        ):
            raise ValueError("refresh_interval_seconds must be a finite non-negative value")
        if type(cache_enabled) is not bool or type(cache_persist) is not bool:
            raise ValueError("cache_enabled / cache_persist must be booleans")
        if not callable(clock):
            raise ValueError("clock must be callable")
        self._builder = builder
        self._ttl_seconds = float(ttl_seconds)
        self._max_stale_seconds = float(max_stale_seconds)
        self._cache_enabled = cache_enabled
        self._cache_persist = cache_persist
        self._clock = clock
        self._directory = _prepare_directory(directory)
        self._path = self._directory / CATALOG_FILE_NAME if self._directory is not None else None
        self._snapshot: CatalogSnapshot | None = None
        self._index: dict[str, Soft360AppSummary] = {}
        self._casefold_index: dict[str, Soft360AppSummary] = {}
        self._disk_loaded = False
        self._closed = False
        self._generation = 0
        self._lock = asyncio.Lock()
        self._refresh_interval = float(refresh_interval_seconds)
        self._last_refresh_at: float | None = None
        self._last_error: str | None = None
        self._refreshes = 0
        self._stale_served = 0

    # -- 查询 -------------------------------------------------------------

    @property
    def snapshot(self) -> CatalogSnapshot | None:
        return self._snapshot

    @property
    def counters(self) -> dict[str, int]:
        return {"refreshes": self._refreshes, "stale_served": self._stale_served}

    def cached_app_count(self) -> int | None:
        return len(self._snapshot.apps) if self._snapshot is not None else None

    def find(self, soft_id: str) -> Soft360AppSummary | None:
        """按 ``soft_id``（也接受客户端主键 ``soft360-{soft_id}``）查缓存目录。"""

        normalized = normalize_soft_id(soft_id)
        if normalized is None:
            return None
        found = self._index.get(normalized)
        if found is not None:
            return found
        return self._casefold_index.get(normalized.casefold())

    # -- 预加载与读取（请求路径只读缓存）-----------------------------------

    async def preload(self) -> CatalogSnapshot | None:
        """只从 ``catalog.json`` 载入内存快照（不联网），供启动阶段预加载。

        返回载入的快照；磁盘没有可用快照（文件缺失/损坏/过旧）时返回 ``None``。读取失败
        绝不抛出：预加载失败不能阻止后端启动。
        """

        if self._closed or not self._cache_enabled or not self._cache_persist:
            return None
        if self._snapshot is not None:
            return self._snapshot
        started = time.monotonic()
        snapshot = await self._load_disk_snapshot()
        if snapshot is None:
            logger.info("360 目录预加载：没有可用的磁盘快照（%s）。", self._path)
            return None
        self._set_snapshot(snapshot)
        self._last_refresh_at = snapshot.fetched_at
        logger.info(
            "360 目录预加载完成：%s 款，抓取时间 %s，用时 %.3f 秒。",
            len(snapshot.apps),
            _format_timestamp(snapshot.fetched_at),
            max(0.0, time.monotonic() - started),
        )
        return self._snapshot

    async def get(self) -> CatalogSnapshot | None:
        """读取目录快照；只读内存/磁盘缓存，**绝不联网**。

        返回的 ``stale`` 由快照年龄与 ``ttl_seconds`` 计算；没有任何快照时返回 ``None``。
        """

        if self._closed:
            raise Soft360UnavailableError("360 软件源已关闭。")
        if not self._cache_enabled:
            # 显式关闭缓存：没有快照可读，也不在请求路径抓取（后台刷新线程仍可写内存快照）。
            return self._snapshot
        if self._snapshot is None:
            loaded = await self._load_disk_snapshot()
            if loaded is not None:
                self._set_snapshot(loaded)
        if self._snapshot is None:
            return None
        return self._response_snapshot(self._snapshot)

    async def refresh_now(self) -> CatalogSnapshot:
        """同步强制刷新目录并写盘（``POST /store/360/refresh`` 与定时刷新使用）。

        成功后返回新快照；失败抛 :class:`Soft360Error`，旧快照保持不变。
        """

        if self._closed:
            raise Soft360UnavailableError("360 软件源已关闭。")
        async with self._lock:
            if self._closed:
                raise Soft360UnavailableError("360 软件源已关闭。")
            generation = self._generation
            try:
                snapshot = await self._builder.build()
            except Soft360Error as error:
                self._last_error = str(error)
                raise
            if self._closed:
                # 关闭期间完成的抓取不能发布，也不能回填磁盘。
                raise Soft360UnavailableError("360 软件源已关闭。")
            if generation != self._generation:
                # 抓取期间缓存被清理过：把结果返回给本次调用方，但不回填缓存。
                return snapshot
            self._refreshes += 1
            self._last_refresh_at = self._clock()
            self._last_error = None
            self._set_snapshot(snapshot)
            if self._cache_persist:
                await self._save_disk(snapshot)
            return self._snapshot  # type: ignore[return-value]

    def _is_fresh(self, snapshot: CatalogSnapshot, now: float) -> bool:
        return now - snapshot.fetched_at <= self._ttl_seconds

    def _response_snapshot(self, snapshot: CatalogSnapshot) -> CatalogSnapshot:
        """按当前时钟计算响应里的 ``stale`` 标记；不修改内存里的基础快照。"""

        stale = not self._is_fresh(snapshot, self._clock())
        if stale:
            self._stale_served += 1
        return snapshot.with_stale(stale)

    def _set_snapshot(self, snapshot: CatalogSnapshot) -> None:
        self._snapshot = snapshot.with_stale(False)
        self._index = {app.soft_id: app for app in snapshot.apps}
        casefold: dict[str, Soft360AppSummary] = {}
        for app in snapshot.apps:
            casefold.setdefault(app.soft_id.casefold(), app)
        self._casefold_index = casefold

    # -- 状态（供 /api/v1/cache 诊断）-------------------------------------

    def catalog_status(self) -> dict[str, Any]:
        """目录缓存状态；只读，不触发任何抓取。

        ``refresher_running`` / ``next_refresh_at`` 由
        :class:`~Backend.soft360.service.Soft360StoreService` 的独立刷新线程提供
        （``service.catalog_status()`` 会覆盖这两个字段）；store 本身不拥有线程。
        """

        snapshot = self._snapshot
        now = self._clock()
        if snapshot is None:
            apps = age_seconds = fetched_at = last_refresh_at = None
        else:
            apps = len(snapshot.apps)
            age_seconds = round(max(0.0, now - snapshot.fetched_at), 2)
            fetched_at = _format_timestamp(snapshot.fetched_at)
            last_refresh_at = (
                _format_timestamp(self._last_refresh_at)
                if self._last_refresh_at is not None
                else fetched_at
            )
        return {
            "apps": apps,
            "categories": len(snapshot.categories) if snapshot is not None else None,
            "fetched_at": fetched_at,
            "age_seconds": age_seconds,
            "refresh_interval_seconds": self._refresh_interval,
            "refresher_running": False,
            "next_refresh_at": None,
            "last_refresh_at": last_refresh_at,
            "last_error": self._last_error,
            "refreshes": self._refreshes,
            "stale_served": self._stale_served,
        }

    # -- 磁盘快照 ---------------------------------------------------------

    async def _load_disk_snapshot(self) -> CatalogSnapshot | None:
        if not self._cache_persist or self._path is None:
            return None
        if self._disk_loaded:
            return None
        self._disk_loaded = True
        try:
            snapshot = await asyncio.to_thread(self._read_disk_sync)
        except Exception:  # 缓存读取永远不能中断 API 请求。
            logger.warning("读取 360 目录磁盘快照失败: %s", self._path, exc_info=True)
            snapshot = None
        if snapshot is None:
            await self._discard_disk_file()
            return None
        return snapshot

    def _read_disk_sync(self) -> CatalogSnapshot | None:
        assert self._path is not None
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict) or raw.get("schema_version") != CATALOG_SCHEMA_VERSION:
            return None
        fetched_at = raw.get("fetched_at")
        if type(fetched_at) not in (int, float) or not math.isfinite(float(fetched_at)):
            return None
        fetched_at = float(fetched_at)
        if self._clock() - fetched_at > self._max_stale_seconds:
            return None
        apps_raw = raw.get("apps")
        categories_raw = raw.get("categories")
        if not isinstance(apps_raw, list) or not isinstance(categories_raw, list):
            return None
        try:
            apps = tuple(Soft360AppSummary.model_validate(item) for item in apps_raw)
            categories = tuple(Soft360Category.model_validate(item) for item in categories_raw)
        except (ValidationError, TypeError, ValueError):
            return None
        if not apps or not categories:
            return None
        return CatalogSnapshot(apps=apps, categories=categories, fetched_at=fetched_at)

    async def _save_disk(self, snapshot: CatalogSnapshot) -> None:
        if self._path is None:
            return
        try:
            await asyncio.to_thread(self._write_disk_sync, snapshot)
        except (OSError, TypeError, ValueError):
            logger.warning("写入 360 目录磁盘快照失败: %s", self._path, exc_info=True)

    def _write_disk_sync(self, snapshot: CatalogSnapshot) -> None:
        assert self._path is not None
        document = {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "fetched_at": snapshot.fetched_at,
            "apps": [app.model_dump(mode="json") for app in snapshot.apps],
            "categories": [category.model_dump(mode="json") for category in snapshot.categories],
        }
        text = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
        _atomic_write_text(self._path, text)

    async def _discard_disk_file(self) -> None:
        if self._path is None:
            return
        try:
            await asyncio.to_thread(self._path.unlink, True)
        except OSError:
            pass

    # -- 清理与关闭 -------------------------------------------------------

    async def clear(self) -> dict[str, Any]:
        """清空内存与磁盘目录缓存，返回清理统计。"""

        self._generation += 1
        apps = len(self._snapshot.apps) if self._snapshot is not None else 0
        self._snapshot = None
        self._index.clear()
        self._casefold_index.clear()
        self._disk_loaded = False
        # cache_persist=False 时本 store 不拥有磁盘快照（可能是别的配置写的），不能删。
        disk_cleared = False
        if self._cache_persist:
            if self._path is not None and self._path.exists():
                disk_cleared = True
            await self._discard_disk_file()
        return {"cleared": apps > 0 or disk_cleared, "apps": apps, "disk_cleared": disk_cleared}

    async def shutdown(self) -> None:
        # 关闭后完成的抓取不会发布也不会回填磁盘。
        self._closed = True
        self._generation += 1


def _format_timestamp(timestamp: float | None) -> str | None:
    """把 epoch 秒格式化成 UTC ISO 字符串（``/cache`` 诊断用）。"""

    if timestamp is None:
        return None
    try:
        moment = datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


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


def _atomic_write_text(path: Path, text: str, *, attempts: int = 3, delay: float = 0.05) -> None:
    """原子写文本，并对 Windows 上常见的短暂文件占用重试。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    last_error: OSError | None = None
    for attempt in range(1, attempts + 1):
        try:
            descriptor, temporary = tempfile.mkstemp(
                dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
            )
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
            return
        except OSError as error:
            last_error = error
            if attempt < attempts:
                time.sleep(delay * attempt)
    if last_error is not None:
        raise last_error


def _coerce_int(value: Any) -> int | None:
    if type(value) is int:
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _int_list(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    values: list[int] = []
    for item in value:
        number = _coerce_int(item)
        if number is not None:
            values.append(number)
    return values


def _string(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if type(value) is int:
        return str(value)
    return ""


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


__all__ = [
    "CATALOG_SCHEMA_VERSION",
    "CatalogSnapshot",
    "CategorySpec",
    "DEFAULT_CATALOG_MAX_STALE_SECONDS",
    "DEFAULT_CATALOG_REFRESH_INTERVAL_SECONDS",
    "DEFAULT_CATALOG_TTL_SECONDS",
    "DEFAULT_CONCURRENCY",
    "DEFAULT_PAGES_PER_CATEGORY",
    "MAX_PAGES_PER_CATEGORY",
    "SOFT360_CATEGORIES",
    "Soft360CatalogBuilder",
    "Soft360CatalogStore",
    "app_id_for",
    "build_architectures",
    "build_summary",
    "category_specs",
    "logo_url_from",
    "minimum_windows_from_versions",
    "normalize_image_url",
    "normalize_soft_id",
    "parse_score",
    "parse_size_bytes",
    "truncate_description",
]
