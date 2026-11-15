"""联想应用商店规范化的 API 模型（后端对客户端暴露的契约）。

设计来源：``docs/store-source-design.md`` 3.4。这里只包含联想源自己的模型，复用
``Backend/models.py`` 的严格模式基类与 :class:`Release`，因此序列化约定与内置精选源一致：

- 所有模型 ``extra="forbid"``、``strict=True``，字段名一律 snake_case；
- ``icon_url`` 是**联想 CDN 原始 logo 的绝对 http(s) URL**，客户端列表/搜索直接据此取图，
  不再经过后端代理；``icon_proxy_url`` 是可选的相对代理路径，作为直连失败时的兜底；
- 截图/封面的 ``url`` **优先联想 CDN 绝对 https 地址**（客户端直连取图），不满足 https +
  CDN 白名单时回退后端相对代理路径（``/api/v1/store/lenovo/...``）；``proxy_url`` /
  ``cover_proxy_url`` 始终是代理路径，供客户端在直连失败时兜底；
- 详情里的视频截图 ``url`` 指向缩略图（``videoImg``），绝不返回带时效的视频地址；视频播放
  仍走后端代理 ``.../screenshots/{index}/video``；
- 下载地址复用 :class:`Backend.models.Release`，由路由负责 ``Cache-Control: no-store``。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

if "." in (__package__ or ""):
    from ..models import ApiModel, Architecture, RequiredText, ResourceId
else:
    from models import ApiModel, Architecture, RequiredText, ResourceId

# 安装说明统一文案：联想商店只提供安装包，不提供静默安装参数。
INSTALLATION_NOTES = "安装包由联想应用商店提供，下载完成后按安装向导操作。"

# 联想 softID：数字（11488）或 L 前缀（L105867），保留原始大小写用于接口调用。
SoftId = Annotated[str, Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")]
# 相对图片代理路径；只允许本后端 /api/v1/store/lenovo/ 下，防止把外部地址写进契约。
StoreRelativePath = Annotated[str, Field(min_length=1, max_length=512, pattern=r"^/api/v1/store/lenovo/")]
# 客户端直连的图标地址：联想原始 logoFile，必须是绝对 http(s) URL（CDN 地址长度远小于上限）。
IconUrl = Annotated[str, Field(min_length=8, max_length=1024, pattern=r"^https?://")]
IconBackground = Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$")]
MinimumWindows = Annotated[str, Field(pattern=r"^(7|8|8\.1|10|11)$")]

# 详情截图/封面直连 CDN 白名单：只允许 https 的联想 CDN 子域（与图片代理使用的是同一组 CDN）。
ALLOWED_IMAGE_DOMAIN_SUFFIXES: tuple[str, ...] = (".lenovo.com.cn", ".lenovomm.cn", ".gtimg.com")
# 详情图片地址：优先联想 CDN 绝对 https 地址，否则回退本后端 /api/v1/store/lenovo/ 相对代理路径。
ImageUrl = Annotated[str, Field(min_length=1, max_length=1024)]
_STORE_RELATIVE_PREFIX = "/api/v1/store/lenovo/"


def cdn_image_url(value: Any) -> str | None:
    """把联想 CDN 原图地址规范化为可直连的 https 绝对地址；不满足安全条件返回 ``None``。

    必须同时满足：``https`` scheme、有 host、无用户信息、无空白/控制字符，且 host 落在
    :data:`ALLOWED_IMAGE_DOMAIN_SUFFIXES`（``*.lenovo.com.cn`` / ``*.lenovomm.cn`` /
    ``*.gtimg.com``）的子域内。这里不做 http→https 升级：http 地址一律视为不可直连，由调用方
    回退后端代理路径（代理本身会再校验一次 host 白名单）。
    """

    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 1024 or any(ord(character) < 32 for character in text):
        return None
    if any(character.isspace() for character in text):
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
    host = parsed.hostname.casefold()
    if not host:
        return None
    if not any(
        host.endswith(suffix) and len(host) > len(suffix)
        for suffix in ALLOWED_IMAGE_DOMAIN_SUFFIXES
    ):
        return None
    return text


def _validate_image_url(value: str) -> str:
    """校验详情图片地址：CDN 绝对 https 地址，或本后端联想相对代理路径。"""

    if value.startswith(_STORE_RELATIVE_PREFIX):
        if len(value) <= 512 and not any(
            ord(character) < 32 or character.isspace() for character in value
        ):
            return value
        raise ValueError("图片代理地址必须是 /api/v1/store/lenovo/ 下的相对路径")
    if cdn_image_url(value) is not None:
        return value
    raise ValueError(
        "图片地址必须是联想 CDN 绝对 https 地址，或 /api/v1/store/lenovo/ 相对代理路径"
    )


class StoreAppSummary(ApiModel):
    """联想目录 / 搜索结果里的单款应用（客户端列表卡片的数据源）。"""

    id: ResourceId
    soft_id: SoftId
    name: RequiredText
    description: RequiredText
    category: RequiredText
    category_id: RequiredText
    group: Literal["software", "game"]
    version: RequiredText
    size_display: RequiredText
    size_bytes: Annotated[int | None, Field(ge=1)] = None
    publisher: str | None = None
    platforms: RequiredText
    architectures: Annotated[list[Architecture], Field(min_length=1)]
    minimum_windows: MinimumWindows | None = None
    # 客户端列表直接取图的联想 CDN 绝对地址；icon_proxy_url 是后端代理兜底（可选）。
    icon_url: IconUrl
    icon_proxy_url: StoreRelativePath | None = None
    icon_text: Annotated[str, Field(min_length=1, max_length=4)]
    icon_background: IconBackground
    tags: list[RequiredText] = Field(default_factory=list)
    score: Annotated[int | None, Field(ge=0, le=100)] = None
    download_count: str | None = None
    license: RequiredText = "免费"
    is_free: bool = True
    is_recommended: bool = False
    is_beta: bool = False
    sort_order: Annotated[int, Field(ge=0)] = 0
    installation_notes: RequiredText = INSTALLATION_NOTES

    @field_validator("icon_url")
    @classmethod
    def validate_icon_url(cls, value: str) -> str:
        """联想原始 logo 必须是可直连的绝对 http(s) URL。"""

        try:
            parsed = urlsplit(value)
            parsed.port
        except ValueError:
            raise ValueError("icon_url 必须是有效的绝对 http(s) 地址") from None
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or any(ord(character) < 32 for character in value)
        ):
            raise ValueError("icon_url 必须是有效的绝对 http(s) 地址")
        return value

    @model_validator(mode="after")
    def validate_architectures(self) -> "StoreAppSummary":
        if len(self.architectures) != len(set(self.architectures)):
            raise ValueError(f"联想应用 {self.id} 重复声明了同一架构")
        return self


class StoreScreenshot(ApiModel):
    """详情页截图；``index`` 是联想 ``captureFileList`` 的原始顺序。

    ``url`` 优先联想 CDN 的绝对 https 原图（视频截图则是 ``videoImg`` 缩略图），客户端直连
    取图；CDN 地址不合格时回退 ``proxy_url`` 的后端代理路径。``proxy_url`` 始终是
    ``/api/v1/store/lenovo/...`` 相对路径，供客户端在直连失败时兜底。
    """

    index: Annotated[int, Field(ge=0)]
    kind: Literal["image", "video"]
    url: ImageUrl
    proxy_url: StoreRelativePath | None = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return _validate_image_url(value)


class StoreDetailTag(ApiModel):
    name: RequiredText
    tips: str | None = None


class StoreAppDetails(StoreAppSummary):
    """应用详情：summary 全部字段 + 详情页专属字段。"""

    long_description: str
    release_notes: str = ""
    screenshots: list[StoreScreenshot] = Field(default_factory=list)
    # 横版封面：``cover_url`` 优先联想 CDN 绝对 https 地址，``cover_proxy_url`` 是代理兜底。
    cover_url: ImageUrl | None = None
    cover_proxy_url: StoreRelativePath | None = None
    labels: list[RequiredText] = Field(default_factory=list)
    ranking: str | None = None
    detail_tags: list[StoreDetailTag] = Field(default_factory=list)

    @field_validator("cover_url")
    @classmethod
    def validate_cover_url(cls, value: str | None) -> str | None:
        return None if value is None else _validate_image_url(value)


class StoreCategory(ApiModel):
    id: RequiredText
    name: RequiredText
    count: Annotated[int, Field(ge=0)] = 0


class StoreCategoryGroup(ApiModel):
    code: Literal["software", "game"]
    name: RequiredText
    categories: list[StoreCategory] = Field(default_factory=list)


class StoreCategoriesResponse(ApiModel):
    source: Literal["lenovo"] = "lenovo"
    updated_at: datetime | None = None
    groups: list[StoreCategoryGroup] = Field(default_factory=list)


class StoreCatalog(ApiModel):
    """完整目录：一次返回全部应用（约 5000+ 条），客户端本地筛选。"""

    source: Literal["lenovo"] = "lenovo"
    source_name: RequiredText = "联想应用商店"
    updated_at: datetime | None = None
    stale: bool = False
    total: Annotated[int, Field(ge=0)]
    items: list[StoreAppSummary] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_total(self) -> "StoreCatalog":
        if self.total != len(self.items):
            raise ValueError("联想目录的 total 必须等于 items 数量")
        return self


class StoreSearchResponse(ApiModel):
    source: Literal["lenovo"] = "lenovo"
    query: str
    total: Annotated[int, Field(ge=0)]
    items: list[StoreAppSummary] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_total(self) -> "StoreSearchResponse":
        if self.total != len(self.items):
            raise ValueError("联想搜索结果的 total 必须等于 items 数量")
        return self


class StoreSourceInfo(ApiModel):
    id: ResourceId
    name: RequiredText
    description: RequiredText
    enabled: bool = True
    # 该源是否支持服务端在线搜索（联想 / 360 为 true，内置精选为 false）；客户端据此决定
    # 是否显示“在线搜索”入口，与 enabled 无关。
    online_search: bool = False
    app_count: Annotated[int | None, Field(ge=0)] = None
    is_default: bool = False
    updated_at: datetime | None = None


class StoreSourcesResponse(ApiModel):
    items: list[StoreSourceInfo] = Field(default_factory=list)
    total: Annotated[int, Field(ge=0)] = 0

    @model_validator(mode="after")
    def validate_total(self) -> "StoreSourcesResponse":
        if self.total != len(self.items):
            raise ValueError("软件源列表的 total 必须等于 items 数量")
        return self


__all__ = [
    "ALLOWED_IMAGE_DOMAIN_SUFFIXES",
    "INSTALLATION_NOTES",
    "StoreAppDetails",
    "StoreAppSummary",
    "StoreCatalog",
    "StoreCategoriesResponse",
    "StoreCategory",
    "StoreCategoryGroup",
    "StoreDetailTag",
    "StoreScreenshot",
    "StoreSearchResponse",
    "StoreSourceInfo",
    "StoreSourcesResponse",
    "cdn_image_url",
]
