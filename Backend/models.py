from pathlib import PureWindowsPath
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


ResourceId = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9.-]*$")]
RequiredText = Annotated[str, Field(min_length=1)]
Architecture = Literal["x64", "x86", "arm64"]


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)


class Release(ApiModel):
    id: ResourceId
    version: RequiredText
    platform: Literal["windows"]
    architecture: Architecture
    download_url: HttpUrl
    file_name: RequiredText
    size_bytes: Annotated[int | None, Field(ge=1)] = None
    sha256: Annotated[str | None, Field(pattern=r"^[0-9a-fA-F]{64}$")] = None

    @model_validator(mode="after")
    def validate_file_name(self) -> "Release":
        invalid_characters = '<>:"/\\|?*'
        if (
            self.file_name in {".", ".."}
            or PureWindowsPath(self.file_name).name != self.file_name
            or any(character in invalid_characters or ord(character) < 32 for character in self.file_name)
            or self.file_name.endswith((".", " "))
        ):
            raise ValueError("file_name must be a safe Windows file name")
        return self


class AppMetadata(ApiModel):
    id: ResourceId
    name: RequiredText
    description: RequiredText
    long_description: RequiredText
    category: RequiredText
    publisher: RequiredText
    version: RequiredText = "以官网为准"
    size_display: RequiredText = "以官网为准"
    license: RequiredText = "免费"
    platforms: RequiredText = "Windows 10 / 11"
    website_url: HttpUrl
    download_page_url: HttpUrl
    icon_text: RequiredText
    icon_background: Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$")]
    installation_notes: RequiredText
    features: list[RequiredText]
    tags: list[RequiredText]
    is_recommended: bool = False
    sort_order: Annotated[int, Field(ge=0)] = 0
    # 机器可读的兼容性信息：minimum_windows 取 7/8/8.1/10/11，architectures 取 x64/x86/arm64。
    # platforms 仍是给人看的展示文案；这两个字段供客户端按当前系统过滤不兼容的软件。
    minimum_windows: Annotated[str, Field(pattern=r"^(7|8|8\.1|10|11)$")] | None = None
    architectures: Annotated[list[Architecture], Field(min_length=1)] | None = None

    @model_validator(mode="after")
    def validate_architectures(self) -> "AppMetadata":
        if self.architectures is not None and len(self.architectures) != len(set(self.architectures)):
            raise ValueError(f"app {self.id} 重复声明了同一架构")
        return self

    def supports_architecture(self, architecture: Architecture) -> bool:
        """未声明 ``architectures`` 时视为支持任何架构（兼容旧目录条目）。"""
        return self.architectures is None or architecture in self.architectures


class GithubAssetSpec(ApiModel):
    """One GitHub release asset: a file-name regex plus its architecture.

    ``{version}`` is optional and, when present, is replaced by the release version (with
    regex escaping) before matching. Some projects version their assets differently from
    the release tag (PowerToys tags ``v0.102.3101`` but ships ``...-0.102.3101.0-x64.exe``),
    so a plain regex is allowed as well.
    """

    pattern: Annotated[str, Field(min_length=3, max_length=200)]
    architecture: Architecture

    @model_validator(mode="after")
    def validate_pattern(self) -> "GithubAssetSpec":
        try:
            re.compile(self.pattern.replace("{version}", "1.2.3"))
        except re.error as error:
            raise ValueError(f"GitHub 安装包匹配规则不是有效的正则表达式: {error}") from error
        return self


class GithubProviderSpec(ApiModel):
    """Configuration for the generic GitHub Releases provider of one catalog app."""

    repo: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")]
    tag_prefix: Annotated[str, Field(max_length=20)] = "v"
    assets: Annotated[list[GithubAssetSpec], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_assets(self) -> "GithubProviderSpec":
        architectures = [asset.architecture for asset in self.assets]
        if len(architectures) != len(set(architectures)):
            raise ValueError(f"GitHub 版本来源 {self.repo} 重复声明了同一架构")
        return self


class AppDefinition(AppMetadata):
    icon_file: str | None = None
    download_provider: ResourceId | None = None
    latest_release_id: ResourceId | None = None
    releases: list[Release] = Field(default_factory=list)
    provider_spec: GithubProviderSpec | None = None

    @model_validator(mode="after")
    def validate_resources(self) -> "AppDefinition":
        if self.download_provider is not None and (self.releases or self.latest_release_id is not None):
            raise ValueError(f"provider-managed app {self.id} cannot also declare static releases")
        if self.provider_spec is not None and self.download_provider is None:
            raise ValueError(f"app {self.id} declares provider_spec without a download_provider")
        release_ids = [release.id for release in self.releases]
        if len(release_ids) != len(set(release_ids)):
            raise ValueError(f"duplicate release id for app {self.id}")
        if self.latest_release_id is not None:
            latest_release = next((release for release in self.releases if release.id == self.latest_release_id), None)
            if latest_release is None:
                raise ValueError(f"latest_release_id does not exist for app {self.id}")
            if latest_release.version != self.version:
                raise ValueError(f"app version does not match its latest release for app {self.id}")
        if self.icon_file is not None:
            if (
                PureWindowsPath(self.icon_file).name != self.icon_file
                or any(character in self.icon_file for character in '/\\:')
                or not self.icon_file.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".ico"))
            ):
                raise ValueError(f"icon_file must be a local image file name for app {self.id}")
        return self


class CatalogData(ApiModel):
    schema_version: Literal[1] = 1
    apps: list[AppDefinition]

    @model_validator(mode="after")
    def validate_app_ids(self) -> "CatalogData":
        app_ids = [app.id for app in self.apps]
        if len(app_ids) != len(set(app_ids)):
            raise ValueError("duplicate app id in catalog")
        return self


class AppResponse(AppMetadata):
    icon_url: HttpUrl | None
    latest_release: Release | None


class AppCollection(ApiModel):
    items: list[AppResponse]
    total: Annotated[int, Field(ge=0)]


class ReleaseCollection(ApiModel):
    items: list[Release]
    total: Annotated[int, Field(ge=0)]


class HealthResponse(ApiModel):
    status: Literal["ok"] = "ok"
