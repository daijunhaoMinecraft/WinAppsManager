"""GitHub Releases 通用版本来源。

每个使用 GitHub 发布的软件在 `data/apps.d/*.json` 里用 ``provider_spec`` 描述仓库和安装包
文件名规则，本项目按“一个软件一个 provider 实例”的方式注册（``download_provider`` 建议
直接等于 app id）。GitHub 的 ``browser_download_url`` 是稳定的发布资产地址，不需要签名，因此
``resolve_download()`` 只做一致性校验，不再访问网络。

认证：未认证的 GitHub API 每小时只有 60 次额度，``github.token``（放在被 gitignore 的
``config.local.json`` 或 ``WINAPPSMANAGER_GITHUB_TOKEN``）可提升到每小时 5000 次。令牌
只出现在请求头里，绝不打日志、不写入缓存、不进入任何 API 响应。

配额优化：配置了 ``response_cache`` 时，上一次响应（响应体 + ``ETag`` +
``Last-Modified``）会保存到磁盘，重启后可以直接复用而不再请求；必须访问网络时也总是
发条件请求，数据未变化时 GitHub 返回 ``304 Not Modified``，304 不计入配额。
"""

from __future__ import annotations

import math
import re
import time

import httpx2

from ._github_cache import CachedGithubResponse, GithubResponseCache
from ._utility import official_executable_url
from .base import (
    ProviderProtocolError,
    ProviderReleaseChangedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    ReleaseProvider,
)

if "." in __package__:
    from ..models import GithubProviderSpec, Release
else:
    from models import GithubProviderSpec, Release


GITHUB_API_VERSION = "2022-11-28"
USER_AGENT = "WinAppsManager/1.0"
ALLOWED_DOWNLOAD_HOSTS = frozenset({"github.com"})
# 安装包后缀：exe 为主，同时接受 msi/msix(bundle)，很多 GitHub 发布只提供这些格式。
ALLOWED_INSTALLER_SUFFIXES = (".exe", ".msi", ".msix", ".msixbundle")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_RATE_LIMITED_STATUS = frozenset({403, 429})


class GithubReleaseProvider(ReleaseProvider):
    """通用 GitHub Releases 版本来源，由目录里的 ``provider_spec`` 驱动。"""

    def __init__(
        self,
        client: httpx2.AsyncClient,
        spec: GithubProviderSpec,
        *,
        provider_id: str,
        display_name: str | None = None,
        token: str = "",
        api_base: str = "https://api.github.com",
        timeout_seconds: float = 10.0,
        response_cache: GithubResponseCache | None = None,
        refresh_interval_seconds: float = 0.0,
    ) -> None:
        if not isinstance(spec, GithubProviderSpec):
            raise ValueError("spec must be a GithubProviderSpec")
        if not isinstance(provider_id, str) or not provider_id:
            raise ValueError("provider_id must be a non-empty string")
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        if (
            type(refresh_interval_seconds) not in (int, float)
            or not math.isfinite(refresh_interval_seconds)
            or refresh_interval_seconds < 0
        ):
            raise ValueError("refresh_interval_seconds must be finite and non-negative")
        if response_cache is not None and not isinstance(response_cache, GithubResponseCache):
            raise ValueError("response_cache must be a GithubResponseCache")
        self._client = client
        self._spec = spec
        self._token = token
        self._api_base = api_base.rstrip("/")
        self._timeout = float(timeout_seconds)
        self._response_cache = response_cache
        self._refresh_interval_seconds = float(refresh_interval_seconds)
        self.id = provider_id
        self.display_name = display_name or spec.repo

    @property
    def repo(self) -> str:
        return self._spec.repo

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "User-Agent": USER_AGENT,
        }
        # 令牌只放在请求头；不要记录、不要返回、不要序列化。
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def get_releases(self, *, force_refresh: bool = False) -> list[Release]:
        """读取仓库最新一次正式发布，并映射成当前架构的安装包。

        配置了 ``response_cache`` 时先读磁盘缓存：非强制读取直接复用；强制读取（启动预热、
        定时刷新、详情页刷新）在 ``refresh_interval_seconds`` 内也直接复用，短时间反复重启
        不会重复请求 GitHub。需要访问网络时总是带上上次的 ``ETag`` / ``Last-Modified``
        做条件请求，``304`` 不消耗 API 配额。
        """

        cache = self._response_cache
        cached = await cache.load(self.repo) if cache is not None else None
        if cached is not None and self._cache_is_usable(cached, force_refresh=force_refresh):
            return self._parse_releases(cached.payload)

        headers = self._headers()
        if cached is not None:
            # 条件请求头与令牌同时存在；令牌仍然只出现在请求头里，不进入缓存或日志。
            if cached.etag:
                headers["If-None-Match"] = cached.etag
            if cached.last_modified:
                headers["If-Modified-Since"] = cached.last_modified
        url = f"{self._api_base}/repos/{self.repo}/releases/latest"
        try:
            response = await self._client.get(
                url, headers=headers, timeout=self._timeout, follow_redirects=False
            )
        except httpx2.TimeoutException:
            raise ProviderTimeoutError(f"{self.display_name} 的 GitHub 版本信息请求超时。") from None
        except httpx2.HTTPError:
            raise ProviderUnavailableError(f"{self.display_name} 的 GitHub 版本信息暂时无法连接。") from None
        if response.status_code == 304:
            # 304 只在有本地缓存时才有意义；没有缓存说明上游或代理异常。
            if cached is None:
                raise ProviderProtocolError(
                    f"{self.display_name} 的 GitHub 版本资料返回了 304，但没有可复用的本地缓存。"
                )
            if cache is not None:
                # 数据未变化：只刷新 fetched_at，让间隔门控从本次探测重新计时。
                await cache.save(
                    self.repo,
                    cached.payload,
                    etag=cached.etag,
                    last_modified=cached.last_modified,
                )
            return self._parse_releases(cached.payload)
        self._raise_for_status(response)
        try:
            payload = response.json()
        except (ValueError, UnicodeError):
            raise ProviderProtocolError(f"{self.display_name} 的 GitHub 版本资料不是有效的 JSON。") from None
        # 先解析成功再写缓存，避免把格式错误的响应缓存下来拖累后续启动。
        releases = self._parse_releases(payload)
        if cache is not None and isinstance(payload, dict):
            await cache.save(
                self.repo,
                payload,
                etag=response.headers.get("etag"),
                last_modified=response.headers.get("last-modified"),
            )
        return releases

    def _cache_is_usable(self, cached: CachedGithubResponse, *, force_refresh: bool) -> bool:
        """判断磁盘缓存能否直接返回而不发请求。

        非强制读取永远复用缓存；强制读取在 ``refresh_interval_seconds`` 的间隔内也复用
        （``0`` 表示强制读取必须发条件请求）。``fetched_at`` 来自机器时钟，取非负年龄以
        容忍回拨。
        """

        if not force_refresh:
            return True
        if self._refresh_interval_seconds <= 0:
            return False
        age = max(0.0, time.time() - cached.fetched_at)
        return age < self._refresh_interval_seconds

    def _raise_for_status(self, response: httpx2.Response) -> None:
        status = response.status_code
        if status == 401:
            raise ProviderUnavailableError(
                "GitHub 令牌无效或已过期，请在 config.local.json 的 github.token 中更新。"
            )
        if status in _RATE_LIMITED_STATUS:
            remaining = response.headers.get("x-ratelimit-remaining")
            if remaining == "0" or status == 429:
                raise ProviderUnavailableError(
                    "GitHub API 请求额度已用尽，请配置有效的 github.token（未认证仅 60 次/小时）后重试。"
                    + _rate_limit_reset_hint(response)
                )
            raise ProviderUnavailableError(f"GitHub API 拒绝了本次请求（HTTP {status}）。")
        if status == 404:
            raise ProviderUnavailableError(
                f"{self.display_name} 在 GitHub 上还没有正式发布版本（{self.repo}）。"
            )
        if status >= 400:
            raise ProviderUnavailableError(f"GitHub API 返回 HTTP {status}。")

    def _parse_releases(self, payload: object) -> list[Release]:
        if not isinstance(payload, dict):
            raise ProviderProtocolError(f"{self.display_name} 的 GitHub 版本资料格式无效。")
        tag_name = payload.get("tag_name")
        if not isinstance(tag_name, str) or not tag_name or tag_name != tag_name.strip():
            raise ProviderProtocolError(f"{self.display_name} 的 GitHub 版本缺少有效的标签名。")
        if payload.get("draft") is not False or payload.get("prerelease") is not False:
            raise ProviderProtocolError(f"{self.display_name} 的 GitHub 最新版本不是正式版本。")
        assets = payload.get("assets")
        if not isinstance(assets, list):
            raise ProviderProtocolError(f"{self.display_name} 的 GitHub 版本缺少资产列表。")

        version = tag_name[len(self._spec.tag_prefix) :] if self._spec.tag_prefix and tag_name.startswith(self._spec.tag_prefix) else tag_name
        if not version or len(version) > 50:
            raise ProviderProtocolError(f"{self.display_name} 的 GitHub 版本号无效。")

        available: dict[str, dict] = {}
        for asset in assets:
            if not isinstance(asset, dict):
                raise ProviderProtocolError(f"{self.display_name} 的 GitHub 资产列表格式无效。")
            name = asset.get("name")
            if not isinstance(name, str) or not name:
                raise ProviderProtocolError(f"{self.display_name} 的 GitHub 资产缺少文件名。")
            if asset.get("state", "uploaded") != "uploaded":
                continue
            if name in available:
                raise ProviderProtocolError(f"{self.display_name} 的 GitHub 发布包含重复资产：{name}。")
            available[name] = asset

        path_prefix = f"/{self.repo}/releases/download/{tag_name}/"
        releases: list[Release] = []
        for asset_spec in self._spec.assets:
            regex = re.compile(asset_spec.pattern.replace("{version}", re.escape(version)))
            matches = sorted(name for name in available if regex.fullmatch(name))
            if not matches:
                raise ProviderProtocolError(
                    f"{self.display_name} 的 {asset_spec.architecture} 安装包不在 GitHub 最新发布中。"
                )
            if len(matches) > 1:
                raise ProviderProtocolError(
                    f"{self.display_name} 的 {asset_spec.architecture} 匹配到多个安装包：{', '.join(matches)}。"
                )
            file_name = matches[0]
            asset = available[file_name]
            download_url, resolved_name = official_executable_url(
                asset.get("browser_download_url"),
                display_name=self.display_name,
                hosts=set(ALLOWED_DOWNLOAD_HOSTS),
                path_prefixes=(path_prefix,),
                suffixes=ALLOWED_INSTALLER_SUFFIXES,
                # 仓库名大小写以 GitHub 的 canonical 形式为准（如 VSCodium/vscodium），
                # 目录里写的是否一致不应影响解析。
                case_insensitive_path=True,
            )
            if resolved_name != file_name:
                raise ProviderProtocolError(f"{self.display_name} 的安装包名称与下载地址不一致。")
            size = asset.get("size")
            if size is not None and (type(size) is not int or size <= 0):
                raise ProviderProtocolError(f"{self.display_name} 的安装包大小无效。")
            releases.append(
                Release(
                    id=f"{version}-windows-{asset_spec.architecture}",
                    version=version,
                    platform="windows",
                    architecture=asset_spec.architecture,
                    download_url=download_url,
                    file_name=file_name,
                    size_bytes=size,
                    sha256=_asset_sha256(asset, self.display_name),
                )
            )
        if len({release.id for release in releases}) != len(releases):
            raise ProviderProtocolError(f"{self.display_name} 的 GitHub 版本产生了重复的安装包标识。")
        return releases

    async def resolve_download(self, release: Release) -> Release:
        """GitHub 发布资产地址稳定，只校验资源身份后原样返回副本。"""

        if not isinstance(release, Release):
            raise ProviderProtocolError(f"{self.display_name} 版本资源类型无效。")
        suffix = f"-windows-{release.architecture}"
        if not release.id.endswith(suffix):
            raise ProviderReleaseChangedError(f"{self.display_name} 的安装包标识与架构不一致，请刷新版本资料。")
        if not release.download_url.host or release.download_url.host not in ALLOWED_DOWNLOAD_HOSTS:
            raise ProviderProtocolError(f"{self.display_name} 的安装包地址不是 GitHub 官方地址。")
        return release.model_copy(deep=True)


def _rate_limit_reset_hint(response: httpx2.Response) -> str:
    """把 ``x-ratelimit-reset``（UTC 纪元秒）翻译成剩余秒数提示；无法解析时返回空串。"""

    raw = response.headers.get("x-ratelimit-reset")
    if raw is None:
        return ""
    try:
        reset_at = float(raw)
    except ValueError:
        return ""
    if not math.isfinite(reset_at):
        return ""
    remaining = max(0, int(math.ceil(reset_at - time.time())))
    if remaining <= 0:
        return ""
    return f"配额约 {remaining} 秒后重置。"


def _asset_sha256(asset: dict, display_name: str) -> str | None:
    digest = asset.get("digest")
    if digest is None:
        return None
    if not isinstance(digest, str):
        raise ProviderProtocolError(f"{display_name} 的安装包校验值格式无效。")
    if not digest.lower().startswith("sha256:"):
        return None
    value = digest[7:]
    if _SHA256.fullmatch(value) is None:
        raise ProviderProtocolError(f"{display_name} 的安装包 SHA256 校验值无效。")
    return value.lower()
