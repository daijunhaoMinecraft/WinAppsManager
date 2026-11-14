"""360 软件源（soft360）后端模块。

公开入口是 :class:`Soft360StoreService` 与 :func:`create_soft360_store_service`；异常与规范化
模型也在这里导出，路由、``main.py`` 与测试只依赖本包。

典型接线（``Backend/main.py``）::

    service = create_soft360_store_service(client, settings.cache.directory, settings,
                                           cache_enabled=settings.cache.enabled,
                                           cache_persist=settings.cache.persist)
    await service.preload_catalog()          # 只读 soft360/catalog.json，不联网
    await service.start_catalog_refresher()  # 独立 daemon 线程每 1800 秒刷新
    catalog = await service.get_catalog()    # 请求路径只读缓存
    results = await service.search("微信")   # 加密搜索接口，覆盖 360 全量目录
    release = await service.resolve_download("1010", architecture="x64")

搜索的加密/签名实现（AES-128-CBC、RSA PKCS#1 v1.5、MD5 签名）在
:mod:`Backend.soft360.search`，只依赖 Python 标准库，不新增第三方依赖。
"""

from __future__ import annotations

from .errors import (
    Soft360Error,
    Soft360NotFoundError,
    Soft360ProtocolError,
    Soft360TimeoutError,
    Soft360UnavailableError,
)
from .media import ImagePayload
from .models import (
    INSTALLATION_NOTES,
    Soft360AppDetails,
    Soft360AppSummary,
    Soft360Catalog,
    Soft360CategoriesResponse,
    Soft360Category,
    Soft360Screenshot,
    Soft360SearchResponse,
)
from .search import Soft360SearchClient
from .service import (
    SOURCE_DESCRIPTION,
    SOURCE_ID,
    SOURCE_NAME,
    Soft360StoreService,
    create_soft360_store_service,
)

__all__ = [
    "INSTALLATION_NOTES",
    "ImagePayload",
    "SOURCE_DESCRIPTION",
    "SOURCE_ID",
    "SOURCE_NAME",
    "Soft360AppDetails",
    "Soft360AppSummary",
    "Soft360Catalog",
    "Soft360CategoriesResponse",
    "Soft360Category",
    "Soft360Error",
    "Soft360NotFoundError",
    "Soft360ProtocolError",
    "Soft360Screenshot",
    "Soft360SearchClient",
    "Soft360SearchResponse",
    "Soft360StoreService",
    "Soft360TimeoutError",
    "Soft360UnavailableError",
    "create_soft360_store_service",
]
