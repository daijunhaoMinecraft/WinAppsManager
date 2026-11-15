"""360 软件源规范化的 API 模型（后端对客户端暴露的契约）。

与联想源（``Backend/lenovo/models.py``）保持同一套序列化约定：所有模型 ``extra="forbid"``、
``strict=True``，字段名一律 snake_case，并复用 ``Backend/models.py`` 的严格模式基类与
:class:`Release`。

与联想源的差异：

- 360 的目录/详情接口是**明文 JSON、无需 token/签名**，列表项自带 ``logo`` 地址；搜索走
  加密的 ``enc_client/v2/search/search``（AES + RSA + MD5 签名，见
  :mod:`Backend.soft360.search`），结果条目字段与目录条目类似；
- ``logo_url`` 是 **360 图片 CDN 的绝对 https URL**（qhimg.com / mediav.com 等），客户端可
  直连取图；``icon_proxy_url`` 是后端代理路径，直连失败或需要统一缓存时使用；
- 截图 ``url`` **优先 360 图片 CDN 的绝对 https 地址**（客户端直连取图），不满足 https +
  CDN 白名单时回退后端相对代理路径（``/api/v1/store/360/...``）；``proxy_url`` 始终是代理
  路径，作为直连失败时的兜底；
- 360 没有视频，:class:`Soft360Screenshot` 只有图片；
- 目录列表接口不返回架构/系统信息，所以目录条目默认 ``architectures=["x64", "x86"]``、
  ``minimum_windows="7"``；详情与搜索接口的 ``os.bit`` / ``os.ver`` 会覆盖这两个字段。
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

# 安装说明统一文案：360 只提供安装包，不提供静默安装参数。
INSTALLATION_NOTES = "安装包由 360 软件管家提供，下载完成后按安装向导操作。"

# 360 soft_id：目录/详情接口里都是数字字符串（例如 1010），保留原始大小写用于接口调用；
# 同时兼容极少数带字母的旧 ID，正则与联想源一致。
SoftId = Annotated[str, Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")]
# 相对图片代理路径；只允许本后端 /api/v1/store/360/ 下，防止把外部地址写进契约。
StoreRelativePath = Annotated[str, Field(min_length=1, max_length=512, pattern=r"^/api/v1/store/360/")]
# 客户端直连的图标地址：360 图片 CDN 的绝对 http(s) URL（响应里统一规范化为 https）。
IconUrl = Annotated[str, Field(min_length=8, max_length=1024, pattern=r"^https?://")]
MinimumWindows = Annotated[str, Field(pattern=r"^(7|8|8\.1|10|11)$")]

# 详情截图直连 CDN 白名单：只允许 https 的 360 图片 CDN 子域（与图片代理使用的是同一组 CDN）。
ALLOWED_IMAGE_DOMAIN_SUFFIXES: tuple[str, ...] = (
    ".qhimg.com",
    ".mediav.com",
    ".qhres2.com",
    ".qhres.com",
)
# 详情图片地址：优先 360 图片 CDN 绝对 https 地址，否则回退本后端 /api/v1/store/360/ 相对代理路径。
ImageUrl = Annotated[str, Field(min_length=1, max_length=1024)]
_STORE_RELATIVE_PREFIX = "/api/v1/store/360/"


def cdn_image_url(value: Any) -> str | None:
    """把 360 图片 CDN 地址规范化为可直连的 https 绝对地址；不满足安全条件返回 ``None``。

    必须同时满足：``https`` scheme、有 host、无用户信息、无空白/控制字符，且 host 落在
    :data:`ALLOWED_IMAGE_DOMAIN_SUFFIXES`（``*.qhimg.com`` / ``*.mediav.com`` /
    ``*.qhres2.com`` / ``*.qhres.com``）的子域内。详情映射在读入时已经把 http 统一转成
    https，因此这里不做 http→https 升级：不合格地址由调用方回退后端代理路径。
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
    """校验详情截图地址：CDN 绝对 https 地址，或本后端 360 相对代理路径。"""

    if value.startswith(_STORE_RELATIVE_PREFIX):
        if len(value) <= 512 and not any(
            ord(character) < 32 or character.isspace() for character in value
        ):
            return value
        raise ValueError("图片代理地址必须是 /api/v1/store/360/ 下的相对路径")
    if cdn_image_url(value) is not None:
        return value
    raise ValueError(
        "图片地址必须是 360 图片 CDN 绝对 https 地址，或 /api/v1/store/360/ 相对代理路径"
    )


class Soft360AppSummary(ApiModel):
    """360 目录 / 搜索结果里的单款软件（客户端列表卡片的数据源）。"""

    # 客户端本地主键：``soft360-{soft_id}``。
    id: ResourceId
    soft_id: SoftId
    name: RequiredText
    bname: str | None = None
    description: RequiredText
    category: RequiredText
    category_id: RequiredText
    size_bytes: Annotated[int | None, Field(ge=1)] = None
    score: Annotated[int | None, Field(ge=0, le=100)] = None
    # 客户端列表直接取图的 360 图片 CDN 绝对地址；icon_proxy_url 是后端代理兜底。
    icon_url: IconUrl
    icon_proxy_url: StoreRelativePath
    architectures: Annotated[list[Architecture], Field(min_length=1)]
    minimum_windows: MinimumWindows = "7"
    tags: list[RequiredText] = Field(default_factory=list)
    license: RequiredText = "免费"
    is_free: bool = True
    sort_order: Annotated[int, Field(ge=0)] = 0
    installation_notes: RequiredText = INSTALLATION_NOTES

    @field_validator("icon_url")
    @classmethod
    def validate_icon_url(cls, value: str) -> str:
        """360 原始 logo 必须是可直连的绝对 http(s) URL。"""

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
    def validate_architectures(self) -> "Soft360AppSummary":
        if len(self.architectures) != len(set(self.architectures)):
            raise ValueError(f"360 软件 {self.id} 重复声明了同一架构")
        return self


class Soft360Screenshot(ApiModel):
    """详情页截图；``index`` 是 360 ``snapshot`` 列表的原始顺序。

    ``url`` 优先 360 图片 CDN 的绝对 https 原图，客户端直连取图；CDN 地址不合格时回退
    ``proxy_url`` 的后端代理路径。``proxy_url`` 始终是 ``/api/v1/store/360/...`` 相对路径。
    """

    index: Annotated[int, Field(ge=0)]
    kind: Literal["image"] = "image"
    url: ImageUrl
    proxy_url: StoreRelativePath | None = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return _validate_image_url(value)


class Soft360AppDetails(Soft360AppSummary):
    """软件详情：summary 全部字段 + 详情页专属字段。"""

    version: RequiredText
    publisher: str | None = None
    long_description: str = ""
    screenshots: list[Soft360Screenshot] = Field(default_factory=list)


class Soft360Category(ApiModel):
    id: RequiredText
    name: RequiredText
    count: Annotated[int, Field(ge=0)] = 0


class Soft360CategoriesResponse(ApiModel):
    source: Literal["soft360"] = "soft360"
    updated_at: datetime | None = None
    total: Annotated[int, Field(ge=0)] = 0
    categories: list[Soft360Category] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_total(self) -> "Soft360CategoriesResponse":
        if self.total != len(self.categories):
            raise ValueError("360 分类列表的 total 必须等于 categories 数量")
        return self


class Soft360Catalog(ApiModel):
    """完整目录：一次返回当前快照里的全部软件（各分类热门子集），客户端本地筛选。"""

    source: Literal["soft360"] = "soft360"
    source_name: RequiredText = "360软件管家"
    updated_at: datetime | None = None
    stale: bool = False
    total: Annotated[int, Field(ge=0)]
    items: list[Soft360AppSummary] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_total(self) -> "Soft360Catalog":
        if self.total != len(self.items):
            raise ValueError("360 目录的 total 必须等于 items 数量")
        return self


class Soft360SearchResponse(ApiModel):
    """搜索结果：优先来自 360 全量目录的加密搜索接口，失败时回退缓存目录本地匹配。

    条目可能是目录外软件（不在 ``GET /store/360/apps`` 的热门子集里），客户端照常用
    ``soft_id`` 打开详情/下载（后端会实时抓 ``getinfo``）。
    """

    source: Literal["soft360"] = "soft360"
    query: str
    total: Annotated[int, Field(ge=0)]
    items: list[Soft360AppSummary] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_total(self) -> "Soft360SearchResponse":
        if self.total != len(self.items):
            raise ValueError("360 搜索结果的 total 必须等于 items 数量")
        return self


__all__ = [
    "ALLOWED_IMAGE_DOMAIN_SUFFIXES",
    "INSTALLATION_NOTES",
    "Soft360AppDetails",
    "Soft360AppSummary",
    "Soft360Catalog",
    "Soft360CategoriesResponse",
    "Soft360Category",
    "Soft360Screenshot",
    "Soft360SearchResponse",
    "cdn_image_url",
]
