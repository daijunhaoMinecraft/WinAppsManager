"""联想应用商店（lenovo）软件源。

公开入口是 :class:`LenovoStoreService` 与 :func:`create_lenovo_store_service`；异常与规范化模型
也在这里导出，路由与测试只依赖本包。

典型接线（``Backend/main.py``）::

    service = create_lenovo_store_service(client, settings.cache.directory, settings,
                                          cache_enabled=settings.cache.enabled,
                                          cache_persist=settings.cache.persist)
    catalog = await service.get_catalog()
    release = await service.resolve_download("L105867", architecture="x64")
"""

from __future__ import annotations

from .errors import (
    LenovoError,
    LenovoNotFoundError,
    LenovoProtocolError,
    LenovoTimeoutError,
    LenovoUnavailableError,
)
from .media import ImagePayload
from .models import (
    INSTALLATION_NOTES,
    StoreAppDetails,
    StoreAppSummary,
    StoreCatalog,
    StoreCategoriesResponse,
    StoreCategory,
    StoreCategoryGroup,
    StoreDetailTag,
    StoreScreenshot,
    StoreSearchResponse,
    StoreSourceInfo,
    StoreSourcesResponse,
)
from .service import (
    SOURCE_DESCRIPTION,
    SOURCE_ID,
    SOURCE_NAME,
    LenovoStoreService,
    create_lenovo_store_service,
)

__all__ = [
    "INSTALLATION_NOTES",
    "ImagePayload",
    "LenovoError",
    "LenovoNotFoundError",
    "LenovoProtocolError",
    "LenovoStoreService",
    "LenovoTimeoutError",
    "LenovoUnavailableError",
    "SOURCE_DESCRIPTION",
    "SOURCE_ID",
    "SOURCE_NAME",
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
    "create_lenovo_store_service",
]
