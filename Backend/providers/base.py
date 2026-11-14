from abc import ABC, abstractmethod

if "." in __package__:
    from ..models import Release
else:
    from models import Release


class ProviderError(RuntimeError):
    """An upstream provider could not supply a usable release."""


class ProviderTimeoutError(ProviderError):
    """An upstream request timed out."""


class ProviderProtocolError(ProviderError):
    """The upstream response does not follow the expected protocol."""


class ProviderReleaseChangedError(ProviderProtocolError):
    """A selected release disappeared when authoritative metadata refreshed."""


class ProviderUnavailableError(ProviderError):
    """The upstream service returned an HTTP or transport error."""


class ReleaseProvider(ABC):
    id: str

    @abstractmethod
    async def get_releases(self, *, force_refresh: bool = False) -> list[Release]:
        """Return release metadata, without short-lived download signatures."""

    @abstractmethod
    async def resolve_download(self, release: Release) -> Release:
        """Resolve a current download URL without caching its signature."""
