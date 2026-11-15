"""统一软件源 API 的规范化模型（契约见 ``docs/source-api.md``）。

所有来源（联想应用商店、360 软件管家、内置精选以及将来新增的来源）都通过
``/api/v1/sources/{source_id}/...`` 暴露**同一套** JSON 字段；本模块是这套字段的唯一权威
定义，客户端只依赖它，不再包含任何来源 ID、名称、接口路径或字段分支。

序列化约定与联想/360 源一致：``extra="forbid"``、``strict=True``、snake_case。与来源私有
模型的差异：

- ``id`` 是**来源内的应用 ID**（联想 ``softID`` / 360 ``soft_id`` / 内置精选的目录 id）。
  客户端缓存主键是 ``source_id + ":" + id``，不自己拼接前缀；统一路由的
  ``/apps/{app_id}`` 原样接收这个值。
- ``version`` 可以为 ``null``：360 的目录/搜索接口不返回版本，详情接口才有；客户端按
  “版本未知”展示。
- ``icon_url`` 是可选 CDN 绝对 http(s) 地址；内置精选没有独立 CDN，返回 ``null``，
  客户端改用 ``icon_proxy_url``（后端相对路径，客户端只按返回值请求，不构造路径）。
- 截图的 ``url`` 与详情的 ``cover_url`` 只放**安全的公网绝对地址**（不安全时适配器置
  ``null``），后端代理路径单独放在 ``proxy_url`` / ``cover_proxy_url``；
  ``video_proxy_url`` 只在视频截图返回，客户端直接播放该 URL。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

if "." in (__package__ or ""):
    from ..models import ApiModel, Architecture, RequiredText
else:
    from models import ApiModel, Architecture, RequiredText

# 来源 id：小写、点/短横线/下划线，与 ``ResourceId`` 同构但允许更长的自定义来源。
SourceId = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")]
# 来源内的应用 ID：联想是数字或 ``L`` 前缀、360 是数字字符串、内置精选用目录 id
# （小写字母开头）。这里不强制小写，因为联想 ``L105867`` 必须保留原始大小写。
SourceAppId = Annotated[
    str, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
]
# 后端相对代理路径：客户端只按返回值请求，因此必须是同源相对路径（不能写外部地址）。
BackendPath = Annotated[str, Field(min_length=2, max_length=512, pattern=r"^/[^\s]*$")]
# 客户端直连的 CDN 绝对 http(s) 地址。
IconUrl = Annotated[str, Field(min_length=8, max_length=1024, pattern=r"^https?://")]
MinimumWindows = Annotated[str, Field(pattern=r"^(7|8|8\.1|10|11)$")]
IconBackground = Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$")]


def _validate_absolute_http_url(value: str) -> str:
    """校验图片直连地址：必须是 host 合法的绝对 http(s) URL，且不含 userinfo/控制字符。"""

    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError:
        raise ValueError("图片地址必须是有效的绝对 http(s) 地址") from None
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError("图片地址必须是有效的绝对 http(s) 地址")
    return value


def _validate_backend_path(value: str) -> str:
    """校验后端相对代理路径：必须以 ``/`` 开头、无空白/控制字符。"""

    if not value.startswith("/") or any(ord(character) < 32 or character.isspace() for character in value):
        raise ValueError("代理地址必须是以后端根路径开头的相对 URL")
    return value


class SourceCapabilities(ApiModel):
    """一个软件源支持的能力；客户端据此决定是否请求对应接口，不再按来源 ID 分支。

    缺省全部为 ``false``：来源适配器必须显式声明自己支持什么。``catalog`` / ``details`` /
    ``download`` 是任何可浏览来源的必备能力，客户端可以假定它们为真，但服务端仍如实返回。
    """

    categories: bool = False
    catalog: bool = False
    search: bool = False
    details: bool = False
    download: bool = False
    screenshots: bool = False
    cover: bool = False
    video: bool = False
    refresh: bool = False


class SourceAppSummary(ApiModel):
    """统一目录 / 搜索 / 详情条目共用的应用字段（字段含义见契约第 3 节）。"""

    id: SourceAppId
    name: RequiredText
    description: RequiredText
    category: RequiredText
    category_id: str | None = None
    group: Literal["software", "game"] | None = None
    # 360 的目录/搜索不返回版本，详情才有；因此允许 null。
    version: str | None = None
    size_display: str | None = None
    size_bytes: Annotated[int | None, Field(ge=1)] = None
    publisher: str | None = None
    platforms: str | None = None
    architectures: Annotated[list[Architecture], Field(min_length=1)] | None = None
    minimum_windows: MinimumWindows | None = None
    icon_url: IconUrl | None = None
    icon_proxy_url: BackendPath | None = None
    icon_text: Annotated[str, Field(min_length=1, max_length=4)] | None = None
    icon_background: IconBackground | None = None
    tags: list[RequiredText] = Field(default_factory=list)
    score: Annotated[int | None, Field(ge=0, le=100)] = None
    download_count: str | None = None
    license: str | None = None
    is_free: bool | None = None
    # 这两个是“展示/排序”用的布尔值：为 null 时部分严格客户端无法反序列化，因此来源必须给出
    # 明确值（不知道时用 false），不随其它可选字段一起省略。
    is_recommended: bool = False
    is_beta: bool = False
    sort_order: Annotated[int, Field(ge=0)] = 0
    installation_notes: str | None = None
    # 内置精选来自目录的官网/下载页；联想与 360 不提供时为 null。
    website_url: Annotated[str, Field(min_length=8, max_length=1024)] | None = None
    download_page_url: Annotated[str, Field(min_length=8, max_length=1024)] | None = None

    @field_validator("icon_url")
    @classmethod
    def validate_icon_url(cls, value: str | None) -> str | None:
        return None if value is None else _validate_absolute_http_url(value)

    @field_validator("website_url", "download_page_url")
    @classmethod
    def validate_web_url(cls, value: str | None) -> str | None:
        return None if value is None else _validate_absolute_http_url(value)

    @field_validator("icon_proxy_url")
    @classmethod
    def validate_icon_proxy_url(cls, value: str | None) -> str | None:
        return None if value is None else _validate_backend_path(value)

    @model_validator(mode="after")
    def validate_architectures(self) -> "SourceAppSummary":
        if self.architectures is not None and len(self.architectures) != len(set(self.architectures)):
            raise ValueError(f"应用 {self.id} 重复声明了同一架构")
        return self


class SourceScreenshot(ApiModel):
    """详情页截图；``index`` 是来源原始截图列表的顺序，客户端直接用它请求代理。"""

    index: Annotated[int, Field(ge=0)]
    kind: Literal["image", "video"] = "image"
    # 可选的 CDN 绝对地址（客户端直连）；不安全或来源不提供时为空。
    url: Annotated[str, Field(min_length=8, max_length=1024)] | None = None
    # 可选的同源后端代理路径，客户端直连失败时兜底。
    proxy_url: BackendPath | None = None
    # 仅视频截图返回：客户端直接播放这个 URL（Range/206）。
    video_proxy_url: BackendPath | None = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        return None if value is None else _validate_absolute_http_url(value)

    @field_validator("proxy_url", "video_proxy_url")
    @classmethod
    def validate_proxy_url(cls, value: str | None) -> str | None:
        return None if value is None else _validate_backend_path(value)

    @model_validator(mode="after")
    def validate_video_proxy(self) -> "SourceScreenshot":
        if self.video_proxy_url is not None and self.kind != "video":
            raise ValueError("只有视频截图可以返回 video_proxy_url")
        return self


class SourceDetailTag(ApiModel):
    name: RequiredText
    tips: str | None = None


class SourceAppDetails(SourceAppSummary):
    """应用详情：summary 全部字段 + 详情页专属字段（契约第 6 节）。"""

    long_description: str = ""
    release_notes: str = ""
    screenshots: list[SourceScreenshot] = Field(default_factory=list)
    cover_url: Annotated[str, Field(min_length=8, max_length=1024)] | None = None
    cover_proxy_url: BackendPath | None = None
    labels: list[RequiredText] = Field(default_factory=list)
    ranking: str | None = None
    detail_tags: list[SourceDetailTag] = Field(default_factory=list)

    @field_validator("cover_url")
    @classmethod
    def validate_cover_url(cls, value: str | None) -> str | None:
        return None if value is None else _validate_absolute_http_url(value)

    @field_validator("cover_proxy_url")
    @classmethod
    def validate_cover_proxy_url(cls, value: str | None) -> str | None:
        return None if value is None else _validate_backend_path(value)


class SourceCategory(ApiModel):
    id: RequiredText
    name: RequiredText
    count: Annotated[int, Field(ge=0)] = 0
    # 可选分组（例如联想的 software/game）；客户端按 categories 顺序展示左边栏。
    group: Literal["software", "game"] | None = None


class SourceCategoriesResponse(ApiModel):
    source: SourceId
    updated_at: datetime | None = None
    categories: list[SourceCategory] = Field(default_factory=list)


class SourceCatalog(ApiModel):
    """完整目录：一次返回当前快照里的全部应用，客户端本地筛选/分页。"""

    source: SourceId
    source_name: RequiredText
    updated_at: datetime | None = None
    stale: bool = False
    total: Annotated[int, Field(ge=0)]
    items: list[SourceAppSummary] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_total(self) -> "SourceCatalog":
        if self.total != len(self.items):
            raise ValueError("统一目录的 total 必须等于 items 数量")
        return self


class SourceSearchResponse(ApiModel):
    """服务端搜索结果（``search_mode=api`` 的来源使用），结构与目录相同并附带 ``query``。"""

    source: SourceId
    source_name: RequiredText
    updated_at: datetime | None = None
    stale: bool = False
    query: str
    total: Annotated[int, Field(ge=0)]
    items: list[SourceAppSummary] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_total(self) -> "SourceSearchResponse":
        if self.total != len(self.items):
            raise ValueError("统一搜索结果的 total 必须等于 items 数量")
        return self


class SourceInfo(ApiModel):
    """``GET /api/v1/sources`` 的单个来源描述。"""

    id: SourceId
    name: RequiredText
    description: RequiredText
    enabled: bool = True
    # ``catalog`` = 目录是完整列表、客户端本地搜索；``api`` = 目录是子集、必须服务端搜索。
    search_mode: Literal["catalog", "api"] = "catalog"
    # 旧客户端兼容字段（新客户端优先 search_mode）。
    online_search: bool = False
    app_count: Annotated[int | None, Field(ge=0)] = None
    is_default: bool = False
    updated_at: datetime | None = None
    capabilities: SourceCapabilities = Field(default_factory=SourceCapabilities)


class SourcesResponse(ApiModel):
    items: list[SourceInfo] = Field(default_factory=list)
    total: Annotated[int, Field(ge=0)] = 0

    @model_validator(mode="after")
    def validate_total(self) -> "SourcesResponse":
        if self.total != len(self.items):
            raise ValueError("软件源列表的 total 必须等于 items 数量")
        return self


__all__ = [
    "BackendPath",
    "IconUrl",
    "MinimumWindows",
    "SourceAppDetails",
    "SourceAppId",
    "SourceAppSummary",
    "SourceCapabilities",
    "SourceCatalog",
    "SourceCategoriesResponse",
    "SourceCategory",
    "SourceDetailTag",
    "SourceId",
    "SourceInfo",
    "SourceScreenshot",
    "SourceSearchResponse",
    "SourcesResponse",
]
