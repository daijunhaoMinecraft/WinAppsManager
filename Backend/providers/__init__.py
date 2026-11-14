from .base import ProviderError, ProviderProtocolError, ProviderReleaseChangedError, ProviderTimeoutError, ProviderUnavailableError, ReleaseProvider
from ._github_cache import CachedGithubResponse, GithubResponseCache
from .github import GithubReleaseProvider
from .qq import QQReleaseProvider
from .wechat import WeChatReleaseProvider
from .vscode import VSCodeReleaseProvider
from .firefox import FirefoxReleaseProvider
from .sevenzip import SevenZipReleaseProvider
from .notepadplusplus import NotepadPlusPlusReleaseProvider
from .vlc import VLCReleaseProvider
from .obs import OBSReleaseProvider
from .workbuddy import WorkBuddyReleaseProvider
from .deepseek_harness import DeepSeekHarnessReleaseProvider
from .doubao import DoubaoReleaseProvider


def create_github_providers(
    client,
    catalog,
    *,
    token: str = "",
    api_base: str = "https://api.github.com",
    timeout_seconds: float = 10.0,
    response_cache: GithubResponseCache | None = None,
    refresh_interval_seconds: float = 0.0,
) -> dict[str, ReleaseProvider]:
    """Create one generic GitHub provider per catalog app that declares ``provider_spec``.

    The provider id is the app's ``download_provider``; rejecting duplicates here keeps
    ``ReleaseService``'s provider table unambiguous.

    ``response_cache`` is shared by every provider so conditional requests (``ETag`` /
    ``Last-Modified``) and their 304 responses survive restarts. ``refresh_interval_seconds``
    makes a forced refresh reuse the cached response without any request while it is fresh
    (``0`` = always send a conditional request).
    """

    providers: dict[str, ReleaseProvider] = {}
    for app in catalog.apps:
        spec = app.provider_spec
        if spec is None:
            continue
        provider_id = app.download_provider
        if provider_id is None:
            raise ValueError(f"app {app.id} declares provider_spec without a download_provider")
        if provider_id in providers:
            raise ValueError(f"duplicate GitHub download_provider: {provider_id}")
        providers[provider_id] = GithubReleaseProvider(
            client,
            spec,
            provider_id=provider_id,
            display_name=app.name,
            token=token,
            api_base=api_base,
            timeout_seconds=timeout_seconds,
            response_cache=response_cache,
            refresh_interval_seconds=refresh_interval_seconds,
        )
    return providers


def create_release_providers(
    client,
    direct_client=None,
    *,
    cache_ttl: float = 600.0,
    catalog=None,
    github_token: str = "",
    github_api_base: str = "https://api.github.com",
    github_timeout_seconds: float = 10.0,
    github_response_cache: GithubResponseCache | None = None,
    github_refresh_interval_seconds: float = 0.0,
) -> dict[str, ReleaseProvider]:
    direct_client = direct_client if direct_client is not None else client
    providers: dict[str, ReleaseProvider] = {
        "qq": QQReleaseProvider(direct_client, cache_ttl=cache_ttl),
        "wechat": WeChatReleaseProvider(direct_client, cache_ttl=cache_ttl),
        "vscode": VSCodeReleaseProvider(direct_client, cache_ttl=cache_ttl),
        "7zip": SevenZipReleaseProvider(direct_client, cache_ttl=cache_ttl),
        "vlc": VLCReleaseProvider(direct_client, cache_ttl=cache_ttl),
        "firefox": FirefoxReleaseProvider(direct_client, cache_ttl=cache_ttl),
        "obs": OBSReleaseProvider(direct_client, cache_ttl=cache_ttl),
        "notepadplusplus": NotepadPlusPlusReleaseProvider(client, cache_ttl=cache_ttl),
        # 国内官方下载站来源（无 GitHub Releases），只有 x64 安装包。
        "workbuddy": WorkBuddyReleaseProvider(direct_client, cache_ttl=cache_ttl),
        "deepseekharness": DeepSeekHarnessReleaseProvider(direct_client, cache_ttl=cache_ttl),
        "doubao": DoubaoReleaseProvider(direct_client, cache_ttl=cache_ttl),
    }
    if catalog is not None:
        github = create_github_providers(
            client,
            catalog,
            token=github_token,
            api_base=github_api_base,
            timeout_seconds=github_timeout_seconds,
            response_cache=github_response_cache,
            refresh_interval_seconds=github_refresh_interval_seconds,
        )
        for provider_id, provider in github.items():
            if provider_id in providers:
                raise ValueError(f"download_provider already registered by another provider: {provider_id}")
        providers.update(github)
    return providers


__all__ = [
    "ReleaseProvider",
    "ProviderError",
    "ProviderTimeoutError",
    "ProviderProtocolError",
    "ProviderReleaseChangedError",
    "ProviderUnavailableError",
    "GithubReleaseProvider",
    "CachedGithubResponse",
    "GithubResponseCache",
    "QQReleaseProvider",
    "WeChatReleaseProvider",
    "VSCodeReleaseProvider",
    "FirefoxReleaseProvider",
    "SevenZipReleaseProvider",
    "NotepadPlusPlusReleaseProvider",
    "VLCReleaseProvider",
    "OBSReleaseProvider",
    "WorkBuddyReleaseProvider",
    "DeepSeekHarnessReleaseProvider",
    "DoubaoReleaseProvider",
    "create_github_providers",
    "create_release_providers",
]
