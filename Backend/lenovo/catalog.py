"""联想应用商店目录：逐分类抓取合并（builder）+ 内存/磁盘快照（store）。

接口调研里的关键结论：``id=-1`` 的“全部”列表并不等于所有分类的并集（微信、QQ 等只出现在
具体分类里），所以完整目录必须**逐分类抓取**后按 ``softID`` 合并去重。本模块：

- :class:`LenovoCatalogBuilder`：先取分类树（``soft`` / ``game`` 两组的 ``type=pc`` 分类），
  跳过 ``classId=-1``；再对每个分类 ``limit=1000`` 抓取，``count`` 更大时按 ``skip`` 分页
  （单分类硬上限 5000 条）；并发上限可配置，单个分类失败重试一次后整体失败，绝不返回半成品。
- 归属规则：应用可能出现在多个分类里，优先使用列表项自带的 ``classID`` 命中的分类，否则用
  实际抓到的第一个分类；分类内前 10 名标记为推荐，``sort_order`` 是分类内位置。
- 图标：列表项的 ``logoFile`` 原样作为 ``icon_url``（联想 CDN 绝对 http(s) 地址，客户端直连），
  同时给出后端代理路径 ``icon_proxy_url`` 作为兜底；``logoFile`` 缺失或不是绝对 http(s) 地址的
  条目会被跳过并记 warning，绝不产生无图标或非法 URL 的模型。
- :class:`LenovoCatalogStore`：内存快照 + ``<cache_dir>/lenovo/catalog.json`` 磁盘快照，
  原子写、损坏忽略。请求路径**只读缓存**：内存快照直接返回（``stale`` 由快照年龄与
  ``ttl_seconds`` 决定）、内存没有时读磁盘快照并载入内存，两者都没有的冷启动才同步抓取
  一次并落盘。``force_refresh=True`` 有快照时只触发后台单飞刷新并立即返回当前快照（不等待
  联网）；``refresh_now()`` 同步强制刷新，供 ``POST /store/lenovo/refresh`` 与
  :class:`~Backend.lenovo.service.LenovoStoreService` 的独立后台刷新线程使用。线程的创建/
  停止由服务层负责（见 ``service.start_catalog_refresher``）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import json
import logging
import math
import os
from dataclasses import dataclass, field, replace
from hashlib import sha256
from pathlib import Path
import re
import tempfile
import time
from typing import Any
from urllib.parse import quote, urlsplit

from pydantic import ValidationError

from .client import LenovoRawClient
from .errors import LenovoError, LenovoProtocolError, LenovoUnavailableError
from .models import (
    INSTALLATION_NOTES,
    StoreAppSummary,
    StoreCategory,
    StoreCategoryGroup,
)

logger = logging.getLogger("winappsmanager.lenovo")

CATALOG_SCHEMA_VERSION = 1
CATALOG_FILE_NAME = "catalog.json"
DEFAULT_CATALOG_TTL_SECONDS = 21600.0
DEFAULT_CATALOG_REFRESH_INTERVAL_SECONDS = 300.0
DEFAULT_CATALOG_MAX_STALE_SECONDS = 604800.0
DEFAULT_CONCURRENCY = 3
DEFAULT_PAGE_SIZE = 1000
DEFAULT_MAX_APPS_PER_CLASS = 5000
MAX_RECOMMENDED = 10

# 预设主题色：由应用名称稳定哈希选中，保证同一款应用每次都是同一个颜色。
ICON_BACKGROUNDS: tuple[str, ...] = (
    "#1679C8",
    "#0F7B6C",
    "#B45309",
    "#7C3AED",
    "#BE123C",
    "#0369A1",
    "#15803D",
    "#A16207",
    "#9333EA",
    "#C2410C",
    "#0E7490",
    "#4D7C0F",
)

_SOFT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
_ID_SANITIZER = re.compile(r"[^a-z0-9.-]+")
_RESERVED_FILE_STEMS = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
# 联想分组 code -> (对客户端的 group 值, 分组默认中文名)
_GROUP_TYPES: dict[str, tuple[str, str]] = {
    "soft": ("software", "软件"),
    "game": ("game", "游戏"),
}
# 分类树里的平台代号 -> 面向客户端的 Windows 版本。
_PLATFORM_VERSIONS: dict[str, str] = {
    "win11": "11",
    "win10": "10",
    "win8_1": "8.1",
    "win81": "8.1",
    "win8.1": "8.1",
    "win8": "8",
    "win7": "7",
}
_PLATFORM_RANK = {"7": 7.0, "8": 8.0, "8.1": 8.1, "10": 10.0, "11": 11.0}
_SIZE_PATTERN = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*([KMGTP]?)B?$", re.IGNORECASE)
_SIZE_MULTIPLIERS = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}


# ---------------------------------------------------------------------------
# 规范化辅助函数（供 builder / 搜索 / 测试复用）
# ---------------------------------------------------------------------------


def parse_size_bytes(value: Any) -> int | None:
    """解析联想 ``installFileSize`` 文案（``318.95 MB`` / ``525KB`` / ``1,000.7 MB``）。

    去空格与千分位逗号，允许小数与 ``KB/MB/GB/TB``；解析失败或小于 1 字节时返回 ``None``。
    """

    if not isinstance(value, str):
        return None
    text = value.strip().replace(",", "").replace("，", "")
    if not text:
        return None
    match = _SIZE_PATTERN.fullmatch(text)
    if match is None:
        return None
    try:
        number = float(match.group(1))
    except ValueError:
        return None
    multiplier = _SIZE_MULTIPLIERS[match.group(2).upper()]
    if not math.isfinite(number):
        return None
    size = int(round(number * multiplier))
    return size if size >= 1 else None


def build_architectures(support_osbit: Any) -> list[str]:
    """由 ``supportOsbit`` 推断安装包架构；无有效值时按 x64 处理。"""

    values = _string_list(support_osbit)
    architectures: list[str] = []
    if any("32" in value for value in values):
        architectures.append("x86")
    if any("64" in value for value in values):
        architectures.append("x64")
    return architectures or ["x64"]


def minimum_windows_from_platforms(support_platform: Any) -> str | None:
    """从 ``supportPlatform`` 取最低支持的 Windows 版本（7/8/8.1/10/11）。"""

    versions: list[str] = []
    for value in _string_list(support_platform):
        normalized = value.strip().casefold().replace(" ", "")
        version = _PLATFORM_VERSIONS.get(normalized)
        if version is not None and version not in versions:
            versions.append(version)
    if not versions:
        return None
    return min(versions, key=lambda item: _PLATFORM_RANK[item])


def format_platforms(support_osbit: Any, support_platform: Any) -> str:
    """生成展示文案，例如 ``Windows 10 / 11 · 64 位``。"""

    versions: list[str] = []
    for value in _string_list(support_platform):
        normalized = value.strip().casefold().replace(" ", "")
        version = _PLATFORM_VERSIONS.get(normalized)
        if version is not None and version not in versions:
            versions.append(version)
    versions.sort(key=lambda item: _PLATFORM_RANK[item])
    bits = _string_list(support_osbit)
    has_x86 = any("32" in value for value in bits)
    has_x64 = any("64" in value for value in bits)
    if has_x86 and has_x64:
        bit_text = "32 / 64 位"
    elif has_x64:
        bit_text = "64 位"
    elif has_x86:
        bit_text = "32 位"
    else:
        bit_text = ""
    if versions:
        base = "Windows " + " / ".join(versions)
        return f"{base} · {bit_text}" if bit_text else base
    return f"Windows {bit_text}".strip()


def is_beta_stable(stable: Any) -> bool:
    """``stable`` 字段为 ``beta`` 时标注 Beta 徽章。"""

    return isinstance(stable, str) and stable.strip().casefold() == "beta"


def parse_score(value: Any) -> int | None:
    if type(value) is int:
        score = value
    elif isinstance(value, str) and value.strip().isdigit():
        score = int(value.strip())
    else:
        return None
    return score if 0 <= score <= 100 else None


def icon_text_for(name: str) -> str:
    """图标文字：中文取 1 个字，英文取前 2 个字母/数字并大写。"""

    text = name.strip()
    if not text:
        return "?"
    if ord(text[0]) > 127:
        return text[0]
    letters = []
    for character in text[:2]:
        if character.isascii() and character.isalnum():
            letters.append(character.upper())
        else:
            break
    return "".join(letters) or text[0].upper()


def icon_background_for(name: str) -> str:
    """用应用名称的稳定哈希映射到预设主题色。"""

    digest = sha256(name.casefold().encode("utf-8")).digest()
    return ICON_BACKGROUNDS[int.from_bytes(digest[:4], "big") % len(ICON_BACKGROUNDS)]


def app_id_for(soft_id: str) -> str:
    """客户端本地主键：``lenovo-`` + 小写 softID，且满足 ResourceId 正则。"""

    sanitized = _ID_SANITIZER.sub("-", soft_id.casefold()).strip("-.")
    return f"lenovo-{sanitized or 'app'}"


def download_count_for(raw: Mapping[str, Any]) -> str | None:
    text = _text(raw.get("downloadCount")) or _text(raw.get("downloadCountDesc"))
    return text or None


def build_search_summary(raw: Mapping[str, Any], position: int) -> StoreAppSummary | None:
    """把搜索结果里的 AppItem 规范化成 summary。

    缺少 ID / 名称 / 可直连的绝对 logo 地址时返回 ``None``（调用方跳过该条）；搜索结果同样
    把联想原始 ``logoFile`` 作为 ``icon_url``，并给后端代理路径作为兜底。
    """

    soft_id = _string(raw.get("softID"))
    if not soft_id or _SOFT_ID_PATTERN.fullmatch(soft_id) is None:
        return None
    name = _text(raw.get("softName"))
    if not name:
        return None
    logo_url = _absolute_logo_url(raw.get("logoFile"))
    if logo_url is None:
        return None
    category = (
        _text(raw.get("className"))
        or _text(raw.get("classifyName"))
        or _text(raw.get("categoryName"))
        or "其他应用"
    )
    category_id = _string(raw.get("classID")) or _string(raw.get("classId")) or "0"
    group = "game" if _text(raw.get("softType")).casefold() == "game" else "software"
    return _make_summary(
        raw,
        soft_id=soft_id,
        name=name,
        logo_url=logo_url,
        category=category,
        category_id=category_id,
        group=group,
        position=position,
        recommended=False,
    )


def normalize_soft_id(soft_id: Any) -> str | None:
    """校验并返回联想 ``softID``；非法时返回 ``None``。"""

    value = _string(soft_id)
    if not value or _SOFT_ID_PATTERN.fullmatch(value) is None:
        return None
    return value


# ---------------------------------------------------------------------------
# 目录快照与 builder
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CatalogSnapshot:
    """一次完整抓取（或磁盘载入）的目录快照。"""

    apps: tuple[StoreAppSummary, ...]
    groups: tuple[StoreCategoryGroup, ...]
    logo_urls: Mapping[str, str]
    fetched_at: float
    stale: bool = False

    def with_stale(self, stale: bool) -> "CatalogSnapshot":
        return replace(self, stale=stale)


@dataclass(frozen=True)
class _ClassSpec:
    raw_code: str
    group_code: str
    group_name: str
    class_id: str
    class_name: str


@dataclass(frozen=True)
class _FetchedClass:
    spec: _ClassSpec
    apps: tuple[Mapping[str, Any], ...]
    total_count: int | None


@dataclass(frozen=True)
class _Assignment:
    spec: _ClassSpec
    position: int


@dataclass
class _Record:
    raw: Mapping[str, Any]
    # 客户端直连的联想 CDN logo 绝对地址（首个带有效 logoFile 的条目决定）。
    logo_url: str
    assignments: list[_Assignment] = field(default_factory=list)


class LenovoCatalogBuilder:
    """逐分类抓取联想商店并合并成完整目录。"""

    def __init__(
        self,
        client: LenovoRawClient,
        *,
        concurrency: int = DEFAULT_CONCURRENCY,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_apps_per_class: int = DEFAULT_MAX_APPS_PER_CLASS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(client, LenovoRawClient):
            raise ValueError("client must be a LenovoRawClient")
        if type(concurrency) is not int or not 1 <= concurrency <= 16:
            raise ValueError("concurrency must be an integer between 1 and 16")
        if type(page_size) is not int or not 1 <= page_size <= 1000:
            raise ValueError("page_size must be an integer between 1 and 1000")
        if type(max_apps_per_class) is not int or max_apps_per_class <= 0:
            raise ValueError("max_apps_per_class must be a positive integer")
        if not callable(clock):
            raise ValueError("clock must be callable")
        self._client = client
        self._concurrency = concurrency
        self._page_size = page_size
        self._max_apps_per_class = max_apps_per_class
        self._clock = clock

    async def build(self) -> CatalogSnapshot:
        tree = await self._client.composite_class_list()
        specs = self._parse_tree(tree)
        semaphore = asyncio.Semaphore(self._concurrency)

        async def fetch(spec: _ClassSpec) -> _FetchedClass:
            async with semaphore:
                return await self._fetch_class(spec)

        results = await asyncio.gather(*(fetch(spec) for spec in specs), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                # 单个分类失败会让整轮目录抓取失败：调用方保留旧快照，绝不发布半成品。
                raise result
        fetched = [item for item in results if isinstance(item, _FetchedClass)]
        apps, logo_urls = self._merge(fetched)
        groups = self._build_groups(fetched)
        return CatalogSnapshot(
            apps=tuple(apps),
            groups=tuple(groups),
            logo_urls=logo_urls,
            fetched_at=float(self._clock()),
        )

    def _parse_tree(self, tree: Any) -> list[_ClassSpec]:
        if not isinstance(tree, list) or not tree:
            raise LenovoProtocolError("联想分类树为空。")
        specs: list[_ClassSpec] = []
        seen: set[tuple[str, str]] = set()
        for group in tree:
            if not isinstance(group, Mapping):
                continue
            raw_code = _string(group.get("code"))
            mapping = _GROUP_TYPES.get(raw_code)
            if mapping is None:
                continue
            group_code, default_name = mapping
            group_name = _text(group.get("name")) or default_name
            categories = group.get("categories")
            if not isinstance(categories, list):
                continue
            for category in categories:
                if not isinstance(category, Mapping):
                    continue
                if _string(category.get("type")).casefold() != "pc":
                    continue
                app_classes = category.get("appClassList")
                if not isinstance(app_classes, list):
                    continue
                for app_class in app_classes:
                    if not isinstance(app_class, Mapping):
                        continue
                    class_id = _string(app_class.get("classId"))
                    class_name = _text(app_class.get("className"))
                    if not class_id or not class_name or class_id == "-1":
                        continue
                    key = (raw_code, class_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    specs.append(_ClassSpec(raw_code, group_code, group_name, class_id, class_name))
        if not specs:
            raise LenovoProtocolError("联想分类树中没有可用的 PC 分类。")
        return specs

    async def _fetch_class(self, spec: _ClassSpec) -> _FetchedClass:
        """抓取一个分类；失败重试一次，仍失败则抛出（由 build 让整轮失败）。"""

        last_error: LenovoError | None = None
        for attempt in range(2):
            try:
                return await self._fetch_class_once(spec)
            except LenovoError as error:
                last_error = error
                if attempt == 0:
                    logger.warning("联想分类“%s”抓取失败，重试一次：%s", spec.class_name, error)
        assert last_error is not None
        raise last_error

    async def _fetch_class_once(self, spec: _ClassSpec) -> _FetchedClass:
        apps: list[Mapping[str, Any]] = []
        total_count: int | None = None
        skip = 0
        while True:
            data = await self._client.class_app_list(
                spec.raw_code, spec.class_id, skip=skip, limit=self._page_size
            )
            raw_items = data.get("apps")
            if raw_items is None:
                raw_items = []
            if not isinstance(raw_items, list):
                raise LenovoProtocolError(f"联想分类“{spec.class_name}”应用列表结构无效。")
            count = _coerce_int(data.get("count"))
            if count is not None:
                total_count = count
            for item in raw_items:
                if isinstance(item, Mapping):
                    apps.append(item)
            if len(apps) >= self._max_apps_per_class:
                apps = apps[: self._max_apps_per_class]
                break
            if total_count is not None and len(apps) >= total_count:
                break
            if not raw_items:
                break
            next_skip = skip + len(raw_items)
            if next_skip <= skip:
                break
            if len(raw_items) < self._page_size and total_count is None:
                break
            skip = next_skip
        return _FetchedClass(spec=spec, apps=tuple(apps), total_count=total_count)

    def _merge(
        self, fetched: list[_FetchedClass]
    ) -> tuple[list[StoreAppSummary], dict[str, str]]:
        records: dict[str, _Record] = {}
        order: list[str] = []
        skipped = 0
        skipped_logo = 0
        for item in fetched:
            for position, raw in enumerate(item.apps):
                soft_id = _string(raw.get("softID"))
                if not soft_id or _SOFT_ID_PATTERN.fullmatch(soft_id) is None:
                    skipped += 1
                    continue
                if not _text(raw.get("softName")):
                    skipped += 1
                    continue
                record = records.get(soft_id)
                if record is None:
                    # 客户端列表直连 CDN 取图标：没有绝对 logo 地址的条目无法满足契约，
                    # 直接跳过（实测 5111 条都有 logo，不应静默产生非法模型）。
                    logo_url = _absolute_logo_url(raw.get("logoFile"))
                    if logo_url is None:
                        skipped_logo += 1
                        continue
                    record = _Record(raw=raw, logo_url=logo_url)
                    records[soft_id] = record
                    order.append(soft_id)
                record.assignments.append(_Assignment(item.spec, position))
        if skipped:
            logger.debug("联想目录跳过了 %s 条缺少 softID / 名称的条目。", skipped)
        if skipped_logo:
            logger.warning(
                "联想目录跳过了 %s 条缺少有效 logoFile（绝对 http(s) 图标地址）的条目。",
                skipped_logo,
            )
        if not order:
            raise LenovoProtocolError("联想目录没有可用应用。")

        apps: list[StoreAppSummary] = []
        logo_urls: dict[str, str] = {}
        for soft_id in order:
            record = records[soft_id]
            assignment = _primary_assignment(record)
            apps.append(
                _make_summary(
                    record.raw,
                    soft_id=soft_id,
                    name=_text(record.raw.get("softName")),
                    logo_url=record.logo_url,
                    category=assignment.spec.class_name,
                    category_id=assignment.spec.class_id,
                    group=assignment.spec.group_code,
                    position=assignment.position,
                    recommended=assignment.position < MAX_RECOMMENDED,
                )
            )
            logo_urls[soft_id] = record.logo_url
        return apps, logo_urls

    @staticmethod
    def _build_groups(fetched: list[_FetchedClass]) -> list[StoreCategoryGroup]:
        groups: dict[str, StoreCategoryGroup] = {}
        for item in fetched:
            spec = item.spec
            fetched_count = len(item.apps)
            count = item.total_count if item.total_count is not None else fetched_count
            count = max(count, fetched_count)
            group = groups.get(spec.group_code)
            if group is None:
                group = StoreCategoryGroup(code=spec.group_code, name=spec.group_name, categories=[])
                groups[spec.group_code] = group
            group.categories.append(StoreCategory(id=spec.class_id, name=spec.class_name, count=count))
        return list(groups.values())


def _primary_assignment(record: _Record) -> _Assignment:
    primary_class_id = _string(record.raw.get("classID"))
    if primary_class_id:
        for assignment in record.assignments:
            if assignment.spec.class_id == primary_class_id:
                return assignment
    return record.assignments[0]


def _make_summary(
    raw: Mapping[str, Any],
    *,
    soft_id: str,
    name: str,
    logo_url: str,
    category: str,
    category_id: str,
    group: str,
    position: int,
    recommended: bool,
) -> StoreAppSummary:
    size_display = _text(raw.get("installFileSize"))
    encoded_soft_id = quote(soft_id, safe="")
    return StoreAppSummary(
        id=app_id_for(soft_id),
        soft_id=soft_id,
        name=name,
        description=_text(raw.get("introduction")) or name,
        category=category or "其他应用",
        category_id=category_id or "0",
        group=group if group in ("software", "game") else "software",
        version=_text(raw.get("version")) or "未知版本",
        size_display=size_display or "未知大小",
        size_bytes=parse_size_bytes(size_display),
        publisher=None,
        platforms=format_platforms(raw.get("supportOsbit"), raw.get("supportPlatform")),
        architectures=build_architectures(raw.get("supportOsbit")),
        minimum_windows=minimum_windows_from_platforms(raw.get("supportPlatform")),
        # 客户端列表按行直连联想 CDN；代理路径只作为兜底。
        icon_url=logo_url,
        icon_proxy_url=f"/api/v1/store/lenovo/apps/{encoded_soft_id}/icon",
        icon_text=icon_text_for(name),
        icon_background=icon_background_for(name),
        tags=[category or "其他应用"],
        score=parse_score(raw.get("score")),
        download_count=download_count_for(raw),
        license="免费",
        is_free=True,
        is_recommended=recommended,
        is_beta=is_beta_stable(raw.get("stable")),
        sort_order=position,
        installation_notes=INSTALLATION_NOTES,
    )


# ---------------------------------------------------------------------------
# 目录缓存（内存 + 磁盘快照；请求只读；后台定时/单飞刷新）
# ---------------------------------------------------------------------------


class LenovoCatalogStore:
    """联想目录快照缓存（请求只读缓存 + 后台定时刷新）。

    ``directory`` 是联想缓存根目录（即 ``<cache_dir>/lenovo``），磁盘文件为其中的
    ``catalog.json``。``cache_enabled=False`` 时不使用任何缓存（每次同步抓取），
    ``cache_persist=False`` 时只保留内存快照。

    请求路径（:meth:`get`）语义：

    - 内存快照存在：直接返回，绝不调度刷新、绝不联网；``stale`` 只由 ``ttl_seconds`` 与该
      快照的实际年龄决定（超过即 ``stale=True``，用于提示客户端目录偏旧）；
    - 内存没有但磁盘有：读取 ``catalog.json`` 载入内存并返回，不联网；
    - 两边都没有：同步抓取一次并写盘（冷启动兜底，唯一允许在请求路径联网的情况）。

    ``refresh_now()`` 是同步强制刷新（``POST /store/lenovo/refresh`` 与后台刷新线程使用）；
    后台定时刷新本身由 :class:`~Backend.lenovo.service.LenovoStoreService` 用一个独立的
    daemon 线程负责（线程只把 ``refresh_now()``/``refresh_catalog()`` 协程投递回主事件循环），
    store 不创建也不持有线程；:meth:`shutdown` 只取消进程内的后台单飞刷新任务。
    """

    def __init__(
        self,
        builder: LenovoCatalogBuilder,
        directory: Path | str | None = None,
        *,
        ttl_seconds: float = DEFAULT_CATALOG_TTL_SECONDS,
        max_stale_seconds: float = DEFAULT_CATALOG_MAX_STALE_SECONDS,
        cache_enabled: bool = True,
        cache_persist: bool = True,
        refresh_interval_seconds: float = 0.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(builder, LenovoCatalogBuilder):
            raise ValueError("builder must be a LenovoCatalogBuilder")
        for name, value in (("ttl_seconds", ttl_seconds), ("max_stale_seconds", max_stale_seconds)):
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
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
        self._index: dict[str, StoreAppSummary] = {}
        self._casefold_index: dict[str, StoreAppSummary] = {}
        self._logo_urls: dict[str, str] = {}
        self._logo_casefold: dict[str, str] = {}
        self._disk_loaded = False
        self._closed = False
        self._generation = 0
        self._lock = asyncio.Lock()
        self._refresh_task: asyncio.Task[None] | None = None
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._refresh_interval = float(refresh_interval_seconds)
        self._last_refresh_at: float | None = None
        self._last_error: str | None = None
        self._refreshes = 0
        self._background_refreshes = 0
        self._stale_served = 0

    # -- 查询 -------------------------------------------------------------

    @property
    def snapshot(self) -> CatalogSnapshot | None:
        return self._snapshot

    @property
    def counters(self) -> dict[str, int]:
        return {
            "refreshes": self._refreshes,
            "background_refreshes": self._background_refreshes,
            "stale_served": self._stale_served,
        }

    def cached_app_count(self) -> int | None:
        return len(self._snapshot.apps) if self._snapshot is not None else None

    def find(self, soft_id: str) -> StoreAppSummary | None:
        if not isinstance(soft_id, str) or not soft_id:
            return None
        found = self._index.get(soft_id)
        if found is not None:
            return found
        return self._casefold_index.get(soft_id.casefold())

    def logo_source(self, soft_id: str) -> str | None:
        if not isinstance(soft_id, str) or not soft_id:
            return None
        value = self._logo_urls.get(soft_id)
        if value is not None:
            return value
        return self._logo_casefold.get(soft_id.casefold())

    # -- 预加载与读取（请求路径只读缓存）-----------------------------------

    async def preload(self) -> CatalogSnapshot | None:
        """只从 ``catalog.json`` 载入内存快照（不联网），供启动阶段预加载。

        返回载入的快照；磁盘没有可用快照（文件缺失/损坏/过旧）时返回 ``None``。读取失败
        绝不抛出：预加载失败不能阻止后端启动，第一次请求会按冷启动兜底抓取。
        """

        if self._closed or not self._cache_enabled or not self._cache_persist:
            return None
        if self._snapshot is not None:
            return self._snapshot
        started = time.monotonic()
        snapshot = await self._load_disk_snapshot()
        if snapshot is None:
            logger.info("联想目录预加载：没有可用的磁盘快照（%s），首次请求时按需抓取。", self._path)
            return None
        self._set_snapshot(snapshot)
        self._last_refresh_at = snapshot.fetched_at
        logger.info(
            "联想目录预加载完成：%s 款，抓取时间 %s，用时 %.3f 秒。",
            len(snapshot.apps),
            _format_timestamp(snapshot.fetched_at),
            max(0.0, time.monotonic() - started),
        )
        return self._snapshot

    async def get(self, *, force_refresh: bool = False) -> CatalogSnapshot:
        """读取目录快照；请求路径不联网（冷启动兜底除外）。

        - ``force_refresh=False``：内存快照 → 直接返回；否则磁盘快照 → 载入内存返回；都没有
          才同步抓取一次（冷启动）。
        - ``force_refresh=True``：有快照（内存或磁盘）时只触发后台单飞刷新并立即返回当前
          快照；没有任何快照时同步抓取一次。``stale`` 由快照年龄与 ``ttl_seconds`` 计算。
        """

        if self._closed:
            raise LenovoUnavailableError("联想应用商店源已关闭。")
        if not self._cache_enabled:
            # 显式关闭缓存：没有快照可读，只能每次同步抓取（非默认配置）。
            return (await self._builder.build()).with_stale(False)
        if self._snapshot is None:
            loaded = await self._load_disk_snapshot()
            if loaded is not None:
                self._set_snapshot(loaded)
        if self._snapshot is not None:
            if force_refresh:
                # 快速返回：目录由后台定时刷新维护，刷新请求只负责触发一轮单飞后台刷新。
                self._schedule_background_refresh()
            return self._response_snapshot(self._snapshot)
        # 冷启动兜底：内存与磁盘都没有快照时才同步抓取一次并落盘。
        return await self._cold_start_refresh()

    async def refresh_now(self) -> CatalogSnapshot:
        """同步强制刷新目录并写盘（``POST /store/lenovo/refresh`` 与定时刷新使用）。

        成功后返回新快照；失败抛 :class:`LenovoError`，旧快照保持不变。
        """

        if self._closed:
            raise LenovoUnavailableError("联想应用商店源已关闭。")
        if not self._cache_enabled:
            return (await self._builder.build()).with_stale(False)
        try:
            return await self._refresh(force=True, requested_at=self._clock())
        except LenovoError as error:
            self._last_error = str(error)
            raise

    async def _cold_start_refresh(self) -> CatalogSnapshot:
        """冷启动兜底：内存与磁盘都没有快照时同步抓取一次（唯一的请求路径联网）。"""

        try:
            return await self._refresh(force=True, requested_at=self._clock())
        except LenovoError as error:
            raise LenovoUnavailableError("联想应用商店目录暂时无法获取，请稍后重试。") from error

    async def _refresh(self, *, force: bool, requested_at: float) -> CatalogSnapshot:
        async with self._lock:
            if self._closed:
                raise LenovoUnavailableError("联想应用商店源已关闭。")
            if self._snapshot is not None:
                if self._snapshot.fetched_at > requested_at:
                    # 等待锁期间已经有别的请求完成了刷新，直接复用这一轮结果。
                    return self._snapshot
                if not force and self._is_fresh(self._snapshot, self._clock()):
                    return self._snapshot
            generation = self._generation
            snapshot = await self._builder.build()
            if self._closed:
                # 关闭期间完成的抓取不能发布，也不能回填磁盘。
                raise LenovoUnavailableError("联想应用商店源已关闭。")
            if generation != self._generation:
                # 抓取期间缓存被清理过：把结果返回给本次调用方，但不回填缓存。
                return snapshot.with_stale(False)
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
        casefold: dict[str, StoreAppSummary] = {}
        for app in snapshot.apps:
            casefold.setdefault(app.soft_id.casefold(), app)
        self._casefold_index = casefold
        self._logo_urls = dict(snapshot.logo_urls)
        logo_casefold: dict[str, str] = {}
        for key, url in self._logo_urls.items():
            logo_casefold.setdefault(key.casefold(), url)
        self._logo_casefold = logo_casefold

    # -- 后台刷新（单飞）------------------------------------------------

    def _schedule_background_refresh(self) -> None:
        if self._closed:
            return
        task = self._refresh_task
        if task is not None and not task.done():
            return
        task = asyncio.create_task(self._background_refresh(), name="lenovo-catalog-refresh")
        self._refresh_task = task
        self._background_tasks.add(task)
        task.add_done_callback(self._background_done)

    async def _background_refresh(self) -> None:
        try:
            await self._refresh(force=True, requested_at=self._clock())
            self._background_refreshes += 1
        except LenovoError as error:
            self._last_error = str(error)
            logger.warning("联想目录后台刷新失败，继续提供当前快照：%s", error)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # 后台任务不能把异常留成“未取回”的告警。
            self._last_error = f"{type(error).__name__}: {error}"
            logger.exception("联想目录后台刷新出现未预期错误。")

    def _background_done(self, task: asyncio.Task[None]) -> None:
        self._background_tasks.discard(task)
        if self._refresh_task is task:
            self._refresh_task = None
        if not task.cancelled():
            task.exception()

    async def wait_for_pending_refresh(self) -> None:
        """等待在途的后台刷新（测试与诊断用）。"""

        while True:
            task = self._refresh_task
            if task is None or task.done():
                return
            await asyncio.gather(task, return_exceptions=True)

    # -- 状态（供 /api/v1/cache 诊断）-------------------------------------

    def catalog_status(self) -> dict[str, Any]:
        """目录缓存状态；只读，不触发任何抓取。

        ``refresher_running`` / ``next_refresh_at`` 由 :class:`~Backend.lenovo.service.
        LenovoStoreService` 的独立刷新线程提供（``service.catalog_status()`` 会覆盖这两个字段，
        ``/api/v1/cache`` 使用的是服务层版本）；store 本身不拥有线程，因此这里固定为
        ``False`` / ``None``。
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
            "fetched_at": fetched_at,
            "age_seconds": age_seconds,
            "refresh_interval_seconds": self._refresh_interval,
            "refresher_running": False,
            "next_refresh_at": None,
            "last_refresh_at": last_refresh_at,
            "last_error": self._last_error,
            "refreshes": self._refreshes,
            "background_refreshes": self._background_refreshes,
            "stale_served": self._stale_served,
        }

    # -- 磁盘快照 ---------------------------------------------------------

    async def _load_disk_snapshot(self) -> CatalogSnapshot | None:
        if not self._cache_persist or self._path is None:
            return None
        return await self._load_disk()

    async def _load_disk(self) -> CatalogSnapshot | None:
        if self._disk_loaded or self._path is None:
            return None
        self._disk_loaded = True
        try:
            snapshot = await asyncio.to_thread(self._read_disk_sync)
        except Exception:  # 缓存读取永远不能中断 API 请求。
            logger.warning("读取联想目录磁盘快照失败: %s", self._path, exc_info=True)
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
        groups_raw = raw.get("groups")
        logo_urls_raw = raw.get("logo_urls")
        if not isinstance(apps_raw, list) or not isinstance(groups_raw, list):
            return None
        if logo_urls_raw is not None and not isinstance(logo_urls_raw, dict):
            return None
        try:
            apps = tuple(StoreAppSummary.model_validate(item) for item in apps_raw)
            groups = tuple(StoreCategoryGroup.model_validate(item) for item in groups_raw)
        except (ValidationError, TypeError, ValueError):
            return None
        if not apps:
            return None
        logo_urls: dict[str, str] = {}
        if isinstance(logo_urls_raw, dict):
            for key, value in logo_urls_raw.items():
                if isinstance(key, str) and isinstance(value, str) and value:
                    logo_urls[key] = value
        return CatalogSnapshot(
            apps=apps,
            groups=groups,
            logo_urls=logo_urls,
            fetched_at=fetched_at,
        )

    async def _save_disk(self, snapshot: CatalogSnapshot) -> None:
        if self._path is None:
            return
        try:
            await asyncio.to_thread(self._write_disk_sync, snapshot)
        except (OSError, TypeError, ValueError):
            logger.warning("写入联想目录磁盘快照失败: %s", self._path, exc_info=True)

    def _write_disk_sync(self, snapshot: CatalogSnapshot) -> None:
        assert self._path is not None
        document = {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "fetched_at": snapshot.fetched_at,
            "apps": [app.model_dump(mode="json") for app in snapshot.apps],
            "logo_urls": dict(snapshot.logo_urls),
            "groups": [group.model_dump(mode="json") for group in snapshot.groups],
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
        self._generation += 1
        apps = len(self._snapshot.apps) if self._snapshot is not None else 0
        self._snapshot = None
        self._index.clear()
        self._casefold_index.clear()
        self._logo_urls.clear()
        self._logo_casefold.clear()
        self._disk_loaded = False
        # cache_persist=False 时本 store 不拥有磁盘快照（可能是别的配置写的），不能删。
        disk_cleared = False
        if self._cache_persist:
            if self._path is not None and self._path.exists():
                disk_cleared = True
            await self._discard_disk_file()
        return {"cleared": apps > 0 or disk_cleared, "apps": apps, "disk_cleared": disk_cleared}

    async def shutdown(self) -> None:
        # 关闭后完成的抓取不会发布也不会回填磁盘；后台单飞刷新任务一并取消。
        self._closed = True
        self._generation += 1
        pending = [task for task in self._background_tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._background_tasks.clear()
        self._refresh_task = None


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
    if type(value) is str and value.strip().isdigit():
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _string(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if type(value) is int:
        return str(value)
    return ""


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _absolute_logo_url(value: Any) -> str | None:
    """校验列表项的 ``logoFile``：客户端直连图标要求绝对 http(s) 地址。

    返回清洗后的 URL；缺失、超长、带控制字符、非 http(s)、没有 host 或带用户信息时返回
    ``None``（调用方跳过该条目，避免产生无法直连的 ``icon_url``）。
    """

    text = value.strip() if isinstance(value, str) else ""
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
    return text


__all__ = [
    "CATALOG_SCHEMA_VERSION",
    "CatalogSnapshot",
    "DEFAULT_CATALOG_MAX_STALE_SECONDS",
    "DEFAULT_CATALOG_REFRESH_INTERVAL_SECONDS",
    "DEFAULT_CATALOG_TTL_SECONDS",
    "ICON_BACKGROUNDS",
    "LenovoCatalogBuilder",
    "LenovoCatalogStore",
    "app_id_for",
    "build_architectures",
    "build_search_summary",
    "download_count_for",
    "format_platforms",
    "icon_background_for",
    "icon_text_for",
    "is_beta_stable",
    "minimum_windows_from_platforms",
    "normalize_soft_id",
    "parse_score",
    "parse_size_bytes",
]
