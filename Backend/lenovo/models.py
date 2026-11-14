"""联想应用商店规范化的 API 模型（后端对客户端暴露的契约）。

设计来源：``docs/store-source-design.md`` 3.4。这里只包含联想源自己的模型，复用
``Backend/models.py`` 的严格模式基类与 :class:`Release`，因此序列化约定与内置精选源一致：

- 所有模型 ``extra="forbid"``、``strict=True``，字段名一律 snake_case；
- ``icon_url`` 是**联想 CDN 原始 logo 的绝对 http(s) URL**，客户端列表/搜索直接据此取图，
  不再经过后端代理；``icon_proxy_url`` 是可选的相对代理路径，作为直连失败时的兜底；
- 截图/封面的 ``url`` 仍是后端相对代理路径（``/api/v1/store/lenovo/...``），由客户端按
  BaseUrl 解析；
- 详情里的视频截图 ``url`` 指向缩略图代理，绝不返回带时效的视频地址；
- 下载地址复用 :class:`Backend.models.Release`，由路由负责 ``Cache-Control: no-store``。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
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
    """详情页截图；``index`` 是联想 ``captureFileList`` 的原始顺序。"""

    index: Annotated[int, Field(ge=0)]
    kind: Literal["image", "video"]
    url: StoreRelativePath


class StoreDetailTag(ApiModel):
    name: RequiredText
    tips: str | None = None


class StoreAppDetails(StoreAppSummary):
    """应用详情：summary 全部字段 + 详情页专属字段。"""

    long_description: str
    release_notes: str = ""
    screenshots: list[StoreScreenshot] = Field(default_factory=list)
    cover_url: StoreRelativePath | None = None
    labels: list[RequiredText] = Field(default_factory=list)
    ranking: str | None = None
    detail_tags: list[StoreDetailTag] = Field(default_factory=list)


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
]
