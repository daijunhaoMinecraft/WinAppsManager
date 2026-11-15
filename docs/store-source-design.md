# 软件源（联想应用商店）功能设计

本文是给实现代理的任务契约：先看完 `docs/lenovo-store-api.md`（联想的真实接口），再按本文的
文件划分、接口签名、缓存语义和 UI 行为实现。**联想相关请求只能发生在后端**，客户端不直连联想。

## 1. 产品目标

1. 后端新增“联想应用商店（lenovo）”软件源：
   - 服务端完成 token 注册、分类/应用目录、详情、搜索、下载地址解析、图标/截图代理；
   - 全部请求带缓存（内存 + 磁盘），缓存 TTL/陈旧兜底可配置，失败不拖垮后端；
   - 保持既有“内置精选（builtin）”源完全不变（现有 `/api/v1/apps` 等接口不动）。
2. 客户端设置里可以切换软件源：**默认 = 联想应用商店**，另一个选项 = 内置精选。
3. 客户端列表/详情页补充商店字段：安装大小、下载量、评分/榜单、截图相册、更新说明、标签、
   Beta/来源徽章等；列表图标改为按需加载（目录约 5000 款，不能一次预热全部图标）。

## 2. 总体数据流

```
联想接口 (pc-store.lenovomm.cn)
        │ 后端 httpx（token、UA、超时、重试）
        ▼
Backend/lenovo/*  ← 内存 + Backend/.cache/lenovo/* 磁盘缓存
        │ 本项目 API：/api/v1/sources、/api/v1/store/lenovo/*
        ▼
客户端 AppCatalogService（源感知）→ AppsInstallPage / AppDetailsPage / SettingsPage
```

目录规模实测：软件 4579 + 游戏 532 = 5111 款（按 softID 去重）。图标/截图 CDN 会拒绝 curl UA，
且不能直连（403），所以图片也由后端代理。

## 3. 后端

### 3.1 新增文件（均由 backend-core 负责）

```
Backend/lenovo/__init__.py     导出 LenovoStoreService / create_lenovo_store_service / 异常 / 模型
Backend/lenovo/errors.py       LenovoError(ProviderError)、LenovoTimeoutError(ProviderTimeoutError)、
                               LenovoUnavailableError、LenovoProtocolError、LenovoNotFoundError
Backend/lenovo/token.py        设备指纹随机生成 + token 注册/缓存（内存 + 可选磁盘）
Backend/lenovo/client.py       LenovoRawClient：5 个原始接口调用 + 状态码/重试/错误映射
Backend/lenovo/models.py       规范化后的 API 模型（见 3.4）
Backend/lenovo/catalog.py      LenovoCatalogBuilder（逐分类抓取合并）+ LenovoCatalogStore（内存/磁盘/陈旧）
Backend/lenovo/details.py      详情缓存 + 下载地址解析
Backend/lenovo/media.py        LenovoMediaCache：图片拉取 + 磁盘缓存 + ETag
Backend/lenovo/service.py      LenovoStoreService 门面（路由只依赖它）
```

`Backend/lenovo/errors.py` 里的异常必须继承 `Backend/providers/base.py` 的 `ProviderError` /
`ProviderTimeoutError`，这样 `main.py` 现有的异常处理器自动把它们映射成 502/504
（`LenovoNotFoundError` 由路由自己捕获转 404）。

### 3.2 配置（backend-api 负责，`Backend/config.py` + `Backend/config.json`）

新增 `LenovoConfig` 段（严格校验，沿用现有 `_ConfigModel` 风格）：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `lenovo.enabled` | `true` | 总开关；false 时不创建服务、store 路由返回 503、`/sources` 不含 lenovo |
| `lenovo.catalog_ttl_seconds` | `21600` | 目录内存/磁盘新鲜期（6 小时） |
| `lenovo.catalog_max_stale_seconds` | `604800` | 抓取失败时允许使用的最旧目录快照（7 天） |
| `lenovo.details_ttl_seconds` | `900` | 详情内存缓存 TTL |
| `lenovo.details_persistent_ttl_seconds` | `21600` | 详情磁盘缓存 TTL |
| `lenovo.search_cache_ttl_seconds` | `60` | 搜索缓存 TTL |
| `lenovo.timeout_seconds` | `20` | 单次联想请求超时（连接 5 秒） |
| `lenovo.catalog_concurrency` | `3` | 分类抓取并发 1–6 |
| `lenovo.image_cache_ttl_seconds` | `2592000` | 图片磁盘缓存 TTL（30 天） |
| `lenovo.image_cache_max_bytes` | `268435456` | 图片磁盘缓存上限（256 MB，超出按最旧淘汰） |

环境变量：`WINAPPSMANAGER_LENOVO_ENABLED`、`WINAPPSMANAGER_LENOVO_CATALOG_TTL_SECONDS`、
`WINAPPSMANAGER_LENOVO_CATALOG_MAX_STALE_SECONDS`、`WINAPPSMANAGER_LENOVO_DETAILS_TTL_SECONDS`、
`WINAPPSMANAGER_LENOVO_DETAILS_PERSISTENT_TTL_SECONDS`、
`WINAPPSMANAGER_LENOVO_SEARCH_CACHE_TTL_SECONDS`、`WINAPPSMANAGER_LENOVO_TIMEOUT_SECONDS`、
`WINAPPSMANAGER_LENOVO_CATALOG_CONCURRENCY`、`WINAPPSMANAGER_LENOVO_IMAGE_CACHE_TTL_SECONDS`、
`WINAPPSMANAGER_LENOVO_IMAGE_CACHE_MAX_BYTES`。需要测试（默认值、env 覆盖、非法值报错）。

### 3.3 缓存与并发语义

- **token**：`Backend/.cache/lenovo/token.json` 保存 `{fingerprint, token, expires_at}`；有效期内
  （`exp - 3600` 之前）直接复用；接口返回 `token invalid` 时重新注册并重试一次。设备指纹随机
  生成（mac 12 位小写 hex、sn 64 位 hex、uuid 大写 GUID），首次生成后持久化，不要每次重启换。
- **目录**：
  - 内存快照 + `<cache>/lenovo/catalog.json` 磁盘快照（原子写；带 `fetched_at`、`schema_version`）。
  - `get_catalog(force_refresh=False)`：内存新鲜 → 直接返回；磁盘新鲜 → 载入内存返回；陈旧/无数据
    → 返回陈旧快照并后台刷新（stale-while-revalidate），无快照则同步抓取；抓取失败且无快照抛
    `LenovoUnavailableError`；有陈旧快照时返回 `stale=true` 的快照。
  - `force_refresh=True` 只由后台定时刷新与 `POST /refresh` 使用；GET 不提供 refresh 参数（旧客户端的 `?refresh=true` 会被忽略）
    读取（与现有 `/apps` 的 refresh 回退行为一致）。
  - 刷新必须单飞（asyncio.Lock），后台任务不能在 shutdown 后继续跑。
  - 目录响应 `Cache-Control: no-store`（缓存语义由客户端自己控制）。
- **详情**：内存 LRU+TTL + 磁盘（`<cache>/lenovo/details/<sha256(soft_id)>.json`）；失败回退陈旧
  详情（不区分新鲜度，有就用）；长期缓存里**不保存视频 url**（`captureFileList[].type=1` 的
  `url` 有 auth_key 时效），只保留 `videoImg` 缩略图。
- **搜索**：按 `(query, limit)` 内存缓存 60 秒即可，不落盘；结果原样返回，客户端自行过滤广告/兜底
  推荐。
- **下载地址**：**绝不缓存**，响应 `Cache-Control: no-store`。
- **图片**：以图片 URL 的 sha256 为 key 落盘 `<cache>/lenovo/media/`，带 sidecar 元数据
  （url/content_type/fetched_at）；超过 `image_cache_max_bytes` 按最旧淘汰；内存 LRU（64 项/32MB）。
  响应 `Cache-Control: public, max-age=86400` + `ETag`（sha256 前 32 位 hex），支持 `If-None-Match` 304。
  拉图必须带浏览器 UA，只允许 http/https，且 host 必须在白名单：
  `lsw-fast.lenovo.com.cn`、`imgs.lenovomm.cn`、`pc1.gtimg.com`、`pc-store-video.lenovomm.cn`
  及 `*.lenovomm.cn`、`*.lenovo.com.cn`（大小写不敏感）；其他 host 一律 `LenovoProtocolError`。
- 所有缓存实现都要在 `cache.enabled=False` 时退化为直连（不写内存/磁盘），`cache.persist=False`
  时只保留内存缓存。

### 3.4 API 契约（backend-core 产出模型，backend-api 暴露路由）

统一前缀 `/api/v1`，snake_case。`icon_url` 是联想 CDN 的**绝对** http(s) 地址，客户端列表/搜索
直接据此取图标；`icon_proxy_url`、截图与封面的 URL 是相对代理路径，由客户端按后端 BaseUrl 解析。

`GET /sources`（backend-api 实现）

```json
{
  "items": [
    {"id": "lenovo", "name": "联想应用商店", "description": "联想官方 PC 应用商店目录，含软件与游戏。",
     "enabled": true, "app_count": 5111, "is_default": true, "updated_at": "2026-11-14T09:00:00Z"},
    {"id": "builtin", "name": "内置精选", "description": "项目内置的精选软件目录。",
     "enabled": true, "app_count": 45, "is_default": false, "updated_at": null}
  ],
  "total": 2
}
```

`app_count` 在目录还没加载时为 `null`（不能为了统计触发抓取）。

`GET /store/lenovo/categories`

```json
{
  "source": "lenovo", "updated_at": "2026-11-14T09:00:00Z",
  "groups": [
    {"code": "software", "name": "软件", "categories": [
      {"id": "1", "name": "聊天社交", "count": 228},
      {"id": "99", "name": "其他应用", "count": 228}
    ]},
    {"code": "game", "name": "游戏", "categories": [
      {"id": "19", "name": "角色扮演", "count": 205}
    ]}
  ]
}
```

`GET /store/lenovo/apps` → 完整目录（一次返回全部 5111 条，客户端本地筛选）：

```json
{
  "source": "lenovo", "source_name": "联想应用商店",
  "updated_at": "2026-11-14T09:00:00Z", "stale": false, "total": 5111,
  "items": [{
    "id": "lenovo-l105867",
    "soft_id": "L105867",
    "name": "QQ",
    "description": "QQ9全新上线，一起轻松做自己",
    "category": "聊天社交",
    "category_id": "1",
    "group": "software",
    "version": "9.9.37.54395",
    "size_display": "318.95 MB",
    "size_bytes": 334442672,
    "publisher": null,
    "platforms": "Windows 10 / 11 · 64 位",
    "architectures": ["x64"],
    "minimum_windows": "10",
    "icon_url": "https://lsw-fast.lenovo.com.cn/appstore/apps/adp/logo/xxx.png",
    "icon_proxy_url": "/api/v1/store/lenovo/apps/L105867/icon",
    "icon_text": "QQ",
    "icon_background": "#1679C8",
    "tags": ["聊天社交"],
    "score": 60,
    "download_count": "1.01亿次",
    "license": "免费",
    "is_free": true,
    "is_recommended": true,
    "is_beta": false,
    "sort_order": 0,
    "installation_notes": "安装包由联想应用商店提供，下载完成后按安装向导操作。"
  }]
}
```

字段规则：

- `id` = `"lenovo-" + soft_id.lower()`，满足 `^[a-z0-9][a-z0-9.-]*$`（客户端本地主键）。
- `soft_id` 保留原始大小写，用于所有联想接口调用；路由路径参数同样用原始 `soft_id`。
- `size_bytes` 由 `installFileSize` 文案解析（`KB/MB/GB/TB`，去空格和千分位逗号，允许小数）；
  解析失败为 `null`。`size_display` 保留原始文案。
- `architectures`：`supportOsbit` 含 `32` → 追加 `x86`，含 `64` → 追加 `x64`；无有效值时用 `["x64"]`；
  不要用 `adaptPlatform` 推断 arm64。
- `minimum_windows`：从 `supportPlatform` 取最低支持版本（11/10/8.1/8/7），全部是 win10/win11 时
  为 `"10"`，为空时为 `null`。
- `is_recommended`：该分类列表里前 10 名；`sort_order`：该分类列表内的顺序（用于“推荐优先”排序）。
- `icon_url`：联想原始 `logoFile`，必须是绝对 http(s) URL（长度 ≤1024；实测所有条目都有 logo）。
  客户端列表/搜索**直接从这里取图标**（联想图片 CDN），不再逐个请求后端代理。
- `icon_proxy_url`：可选兜底，固定为 `/api/v1/store/lenovo/apps/{soft_id}/icon`（相对后端
  BaseUrl）；直连 CDN 失败（403/超时/非图片）时客户端再回退到它。目录/搜索始终给出该字段。
- `logoFile` 缺失或不是绝对 http(s) 地址的条目会被目录/搜索直接跳过并记 warning，不会返回
  无图标或非法 `icon_url` 的模型。
- `icon_text`：名称前 1–2 个字符（中文 1 字，英文取前 2 个字母大写）；`icon_background`：由名称
  做稳定哈希映射到一组预设主题色（不要每个应用随机）。
- `tags`：分类名 + `appLabelList` 标签名去重（列表接口没有 label，用分类名即可）。
- `installation_notes` 统一：`安装包由联想应用商店提供，下载完成后按安装向导操作。`

图标相关的客户端行为：

- 列表/搜索图标按行懒加载，直接把 `icon_url`（绝对地址）交给图片加载器与磁盘缓存
  （key = sha256(URL)，TTL 建议 30 天）；**不要**在 `PublishCatalogAsync` 预热 5000+ 个图标，
  也不要为每个图标请求后端；加载并发设上限（例如 `SemaphoreSlim(6)`）。
- 直连失败时回退 `icon_proxy_url`（后端代理 + ETag，响应 `public, max-age=86400`）；兜底路径
  仍要支持 png/jpg/gif/ico/bmp。
- 详情页图标同样优先 `icon_url`；截图/封面仍走 `screenshots[].url` / `cover_url` 后端代理。

`GET /store/lenovo/apps/{soft_id}` → `StoreAppDetails`（在 summary 基础上追加）：

```json
{
  "id": "lenovo-l105867", "soft_id": "L105867", "...": "同 summary 全部字段",
  "long_description": "详细描述…",
  "release_notes": "更新说明，可能为空字符串",
  "screenshots": [
    {"index": 0, "kind": "image", "url": "/api/v1/store/lenovo/apps/L105867/screenshots/0"},
    {"index": 1, "kind": "video", "url": "/api/v1/store/lenovo/apps/L105867/screenshots/1"}
  ],
  "cover_url": "/api/v1/store/lenovo/apps/L105867/cover",
  "labels": ["办公辅助工具", "AI对话"],
  "ranking": "聊天社交类下载榜第2名",
  "detail_tags": [{"name": "安全认证", "tips": "软件通过多款主流安全软件检测…"}]
}
```

- `screenshots`：`captureFileList` 顺序，`type="0"` → `kind="image"`（url 用原图），
  `type="1"` → `kind="video"`（url 指向 videoImg 缩略图代理，不返回视频地址）；没有截图的为空数组。
- `cover_url`：`cover.coverImg` 非空时给代理地址，否则 `null`。
- `editorRecommend`/`activityList`/`appRecommend`/`hardwareConfigurationList` 不返回。

`GET /store/lenovo/apps/{soft_id}/download?architecture=x64` → 复用现有 `models.Release`：

```json
{"id": "lenovo-l105867-9.9.37.54395", "version": "9.9.37.54395", "platform": "windows",
 "architecture": "x64", "download_url": "https://pc-lsw7.lenovomm.cn/...sign...",
 "file_name": "QQ9.9.37.54395_lenovo_x64.exe", "size_bytes": 334442672,
 "sha256": "7019f1a55557b4260cb7385f015a521378748d06dfd9915905e14f0585f4e855"}
```

- 地址优先级：`hs2` > `bs2` > `amsHttpsDownloadUrl` > `firstOldDownloadUrl` > `secondOldDownloadUrl`，
  只接受 https；没有可用地址 → `LenovoUnavailableError`（502）。
- `file_name`：`urllib.parse.unquote(data.fileName)` 后按 `models.Release` 的 Windows 文件名规则清洗；
  非法/缺失时回退 `{安全应用名}_{版本}.exe`；扩展名只接受 `.exe/.msi/.msix/.appx/.msu`，否则同样回退 `.exe`。
- `architecture`：请求参数在 `supportOsbit` 支持范围内则用它；否则 x64 优先，其次 x86。
- `size_bytes` 取目录 summary 的解析值（对联想的 `installFileSize` 是安装体积，不是字节级精确值，
  仅用于展示）。
- 已下架/无包/`status != 0` → `LenovoNotFoundError` 或 `LenovoUnavailableError`，路由映射 404/502。

`GET /store/lenovo/search?q=QQ&limit=30`

```json
{"source": "lenovo", "query": "QQ", "total": 31,
 "items": [ {"id": "lenovo-l105867", "soft_id": "L105867", "...": "StoreAppSummary"} ]}
```

- 返回联想原始搜索顺序（客户端自己做广告/兜底过滤）；`limit` 1–50，默认 30（联想实际最多约 31 条）。
- 搜索响应里的 `captureFileList` 为空，没有详情字段；客户端拿到后按 summary 使用。

`GET /store/lenovo/apps/{soft_id}/icon`、`/screenshots/{index}`、`/cover` → 图片字节响应
（`Content-Type` 按实际格式，`ETag`、`Cache-Control: public, max-age=86400`，`If-None-Match` 命中 304）。
未知 soft_id / index 越界 / 没有对应图片 → 404。

`POST /store/lenovo/refresh` → `{"refreshed": true, "total": 5111, "updated_at": "...", "duration_seconds": 3.2}`；
并发调用合并为同一轮（返回同一结果）。

`DELETE /api/v1/cache` 追加清理联想缓存（目录内存/磁盘、详情、图片），响应加
`"lenovo": {"cleared": true}`；不要清理 token（避免频繁重注册）。

错误映射：`LenovoError` → 502 `{"detail": "..."}`，`LenovoTimeoutError` → 504（沿用现有全局异常
处理器），`LenovoNotFoundError` → 404。所有含下载地址的响应都加 `Cache-Control: no-store`。

### 3.5 服务门面签名（backend-api 直接调用）

```python
# Backend/lenovo/service.py
class LenovoStoreService:
    def __init__(self, client: httpx2.AsyncClient, cache_directory: Path | None, *, settings,
                 cache_enabled: bool = True, cache_persist: bool = True) -> None: ...
    async def get_catalog(self, *, force_refresh: bool = False) -> StoreCatalog: ...
    async def get_categories(self, *, force_refresh: bool = False) -> StoreCategoriesResponse: ...
    async def get_details(self, soft_id: str, *, force_refresh: bool = False) -> StoreAppDetails: ...
    async def resolve_download(self, soft_id: str, architecture: str = "x64") -> Release: ...
    async def search(self, query: str, limit: int = 30) -> StoreSearchResponse: ...
    async def get_icon(self, soft_id: str) -> ImagePayload: ...
    async def get_screenshot(self, soft_id: str, index: int) -> ImagePayload: ...
    async def get_cover(self, soft_id: str) -> ImagePayload: ...
    async def refresh_catalog(self) -> dict: ...
    async def clear_caches(self) -> dict: ...
    def cached_app_count(self) -> int | None: ...
    async def shutdown(self) -> None: ...

def create_lenovo_store_service(client: httpx2.AsyncClient, cache_directory: Path | None, settings) -> LenovoStoreService: ...
```

`ImagePayload`（`Backend/lenovo/media.py`）：`@dataclass(frozen=True) class ImagePayload: content: bytes; content_type: str; etag: str`。

`models.py` 至少导出：`StoreAppSummary`、`StoreAppDetails`、`StoreScreenshot`、`StoreDetailTag`、
`StoreCategory`、`StoreCategoryGroup`、`StoreCategoriesResponse`、`StoreCatalog`、
`StoreSearchResponse`、`StoreSourceInfo`、`StoreSourcesResponse`。

### 3.6 main.py 接线（backend-api）

- `create_app(..., lenovo_service: Any | None = None)` 新增注入参数（测试用假实现）；类型宽一些，
  避免测试必须真造 `LenovoStoreService`。
- lifespan 里：`configured.lenovo.enabled` 时，若未注入则用 `AsyncExitStack` 新建专用
  `httpx2.AsyncClient`（timeout 用 lenovo 配置、`follow_redirects=True`、
  `User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36`，
  `trust_env` 用默认 True），创建 `LenovoStoreService` 并挂到 `api.state.lenovo`；否则
  `api.state.lenovo = None`。shutdown 时 `await service.shutdown()`（只关闭自己创建的）。
- `api.state.lenovo` 为 None 时所有 `/store/lenovo/*` 路由返回 503
  `{"detail": "联想应用商店源未启用。"}`。
- 启动时**不要**同步抓取联想目录（不能拖慢启动、不能让测试联网）；目录按需加载。
- 注册路由：`api.include_router(create_lenovo_router())`（路由从 `request.app.state.lenovo` 取服务）。
- `/sources` 用 `catalog.list_apps()` 统计内置数量、`service.cached_app_count()` 统计联想数量。

### 3.7 后端测试要求

- 新测试全部用 `httpx2.MockTransport` 或注入假服务，**禁止真实网络**。
- `test_lenovo_client.py`：token 注册/缓存/过期刷新、`token invalid` 重试一次、5 个接口的 URL/头/
  查询参数、status 字符串/数字兼容、超时→`LenovoTimeoutError`、下架→`LenovoNotFoundError`、
  下载地址优先级与 fileName 清洗、sha256 传递。
- `test_lenovo_catalog.py`：分类并集去重、分类归属、size 解析（含 `1,000.7 MB`）、
  architectures/minimum_windows、磁盘快照往返与损坏忽略、TTL/陈旧、并发刷新单飞、force 刷新失败
  回退、缓存开关。
- `test_lenovo_details.py`：详情字段映射、截图索引/视频 kind、视频 url 不落盘、下载解析错误分支。
- `test_lenovo_media.py`：UA、host 白名单（拒绝非白名单/SSRF）、格式与 content-type、磁盘缓存
  往返、TTL、容量淘汰、ETag/304、cache.disabled。
- `test_lenovo_api.py`：用假服务覆盖全部路由（成功 JSON、404、502/504 映射、503 未启用、
  refresh 回退、图片 ETag/304、下载 no-store、DELETE /cache 的 lenovo 段、/sources）。
- `test_config.py`/`test_lenovo_config.py`：配置默认值、env 覆盖、非法值启动失败。
- 既有测试全部保持通过（`Backend\.venv\Scripts\python.exe -m unittest discover -s Backend\tests -v`）。

## 4. 客户端

### 4.1 模型与服务（client-service 负责）

文件：

- `Models/AppSettings.cs`：新增 `AppSourcePreference { Lenovo, BuiltIn }`（或字符串常量 + 枚举），
  `AppSettings.AppSource` 默认 `Lenovo`。
- `Services/AppSettingsService.cs`：读写、迁移（旧 settings.json 缺字段 → Lenovo）、非法值回退、
  新增 bool 时保持原子写。
- `Models/AppsInstallerModels.cs`（+ 新文件 `Models/StoreCatalogModels.cs`）：`AppItem` 增加：
  `SourceId`、`SourceAppId`、`SourceName`、`SizeBytes`、`DownloadCount`、`Score`、`IsBeta`、
  `RankingDescription`、`ReleaseNotes`、`CoverUrl`、`Screenshots`（`AppScreenshot { int Index; string Kind; string Url; }`）、
  `Labels`、`DetailTags`；图标字段沿用 `IconUrl`（联想源是 CDN 绝对地址）并新增可选的
  `IconProxyUrl`（相对后端 BaseUrl 的兜底代理）。现有字段语义不变。
- `Models/CatalogCacheModels.cs`：磁盘目录缓存文档加 `Source`（旧缓存缺省视为 `builtin`，源不匹配
  视为无缓存）。
- `Services/AppCatalogService.cs`：
  - 源感知：`CurrentSource`、`SourceDisplayName`、`SwitchSourceAsync(AppSourcePreference)`、
    `SourceChanged` 事件；src 由 `AppSettingsService` 读取并持久化。
  - 内置源逻辑保持不变；联想源 `GetAppsAsync` 调 `store/lenovo/apps`，把 summary 映射为 `AppItem`
    （相对 URL 用现有 `ResolveHttpUrl` 解析成绝对地址）；联想目录强制刷新只走 `POST /store/lenovo/refresh`，`forceRefresh` 不再影响联想 GET URL。
  - `GetAppAsync(id)` 联想源 → `store/lenovo/apps/{soft_id}`（从目录/已知应用表按 id 找 soft_id），
    映射详情 + 截图/封面/更新说明；把详情写回内存和磁盘目录（沿用现有行为）。
  - `GetLatestReleaseAsync(AppItem app)` 新重载 + 原 `(string appId)` 保留：联想源调
    `store/lenovo/apps/{soft_id}/download?architecture=...`，映射为 `AppRelease`；放宽架构校验：
    运行 x64 接受 `x86` 安装包（WOW64 可运行）。
  - `SearchStoreAsync(string query, CancellationToken)` → `store/lenovo/search`，返回 `AppItem` 列表
    并登记到“已知应用表”，保证后续详情/下载能按 id 找到 soft_id。
  - `GetCategoriesAsync` 可选（目录接口自带分类）；客户端分类仍由 `AppsInstallPage` 从目录分组，
    但顺序与 `category_id` 无关，按名称排序即可。
  - 图标：`AppItem.IconUrl` 直接使用后端返回的 `icon_url`（联想 CDN 绝对地址），
    `IconProxyUrl` 用现有 `ResolveHttpUrl` 解析成后端的兜底代理地址；`ClientCacheStore.TryIconUri`
    允许 `/api/v1/store/` 下的图片路径；`ImageExtension` / `ManagedFile` 支持 png/ico/jpg/jpeg/gif/bmp。
  - 联想目录**不要**一次性预热全部图标：`PublishCatalogAsync` 对联想源跳过 `QueueIcons` 与
    `AssignCachedIconsAsync`，新增公开 `EnsureIconAsync(AppItem, CancellationToken)` 供列表行按需
    调用（内部先查磁盘缓存，未命中直连 `IconUrl`；直连失败再请求 `IconProxyUrl` 后端代理，带并发
    上限，例如 `SemaphoreSlim(6)`）。
  - `CacheDescription`/`CacheState`/`ClearCacheAsync` 对两种源都要工作（清理时也尽量让后端清联想缓存）。
- `Services/ClientCacheStore.cs`：目录缓存 key 加入源标识（`DirectoryPath` 不变，文件 key 变）；
  上述图片格式/路径放宽；`CloneForDisk` 兼容新字段（System.Text.Json 自动）。
- `Services/StoreMediaService.cs`（新）：加载截图/封面图片 → `ImageSource`（`BitmapImage` +
  `BitmapCacheOption.OnLoad` + `Freeze`），内存缓存（如 64 张/32MB）与磁盘缓存
  （`%LOCALAPPDATA%\WinAppsManager\Cache\store-media`，key = sha256(URL)，复用 ClientCacheStore 的
  安全写盘思路）；后台加载，UI 线程只绑定；失败返回 `null` 由 UI 显示占位。
- `Services/DownloadNetworkService.cs`：下载 HttpClient 加 `User-Agent`（联想旧 CDN 拒绝 curl 类 UA），
  其他行为不变。
- `Services/DownloadCenterService.cs`：改用 `GetLatestReleaseAsync(AppItem, ...)`，联想源无需
  `LatestRelease` 预先存在；失败提示"获取下载地址失败"。

### 4.2 UI（client-ui 负责）

文件：

- `Pages/SettingsPage.xaml(.cs)`：新增“软件源”区块（ComboBox：联想应用商店 / 内置精选 + 说明 +
  当前源状态）。切换即保存设置并 `AppCatalogService.SwitchSourceAsync(...)`，提示目录会重新加载；
  失败时显示错误并保留原源。
- `Pages/AppsInstallPage.xaml(.cs)`：
  - 顶部状态行显示当前源（如“联想应用商店 · 共 5111 款应用”），保留现有搜索/排序/刷新/清空筛选。
  - 卡片补充：安装大小（`SizeDisplay`）、下载量（`DownloadCount`）、评分（`Score`，可选）、
    Beta 徽章（`IsBeta`，仅联想源有）、来源徽章（联想源显示“联想商店”，内置不显示）。
  - 联想源图标按需加载：行容器 `Loaded`/`DataContextChanged` 时调用
    `AppCatalogService.EnsureIconAsync`，优先直连 CDN 的 `IconUrl`，失败回退 `IconProxyUrl`
    后端代理；内置源维持现有预热逻辑。
  - 联想源搜索无本地结果时，空状态显示“在联想商店在线搜索”按钮 → 调 `SearchStoreAsync`，
    结果列表显示在线结果并在状态行注明“联想商店在线搜索结果”；`Reset` 清空回目录。
  - 订阅 `AppCatalogService.SourceChanged`：重新加载目录、分类复位“全部”。
- `Pages/AppDetailsPage.xaml(.cs)`：
  - 增加安装大小、下载量、评分/榜单、来源徽章、标签（Labels/Tags chips）、更新说明
    （ReleaseNotes，空则隐藏）、Beta 徽章。
  - 截图相册：横向滚动，逐张异步加载（`StoreMediaService`）；视频缩略图显示播放角标；点击打开新
    `Dialogs/ScreenshotViewerWindow.xaml(.cs)`（大图预览，左右切换、Esc 关闭）；封面
    （`CoverUrl`）可作为相册第一张或顶部横幅。
  - 联想源下载按钮：`LatestRelease` 为空也显示“开始下载”（下载时后端实时解析地址）；详情页加载
    后可选调用 `GetLatestReleaseAsync` 显示最新下载链接；提示文案改为“安装包由联想应用商店提供”。
  - 内置精选页面维持现有行为。
- `Dialogs/ScreenshotViewerWindow.xaml(.cs)`（新）：无边框/普通窗口均可，深色半透明背景大图，
  上一张/下一张/关闭，键盘 Left/Right/Esc，`Owner` 设置，不阻塞下载。
- `MainWindow.xaml(.cs)`：分类导航沿用 `CategoriesChanged` 动态重建；联想源分类较多（约 28 个），
  确认滚动与 `_categorySignature` 逻辑正常；无需新增导航项。
- 根 `README.md` 的客户端/软件源/缓存说明更新（client-ui 负责客户端部分；backend-api 负责后端
  部分，两者改不同小节）。

### 4.3 客户端验收

- `dotnet build WinAppsManager\WinAppsManager.sln -c Release` 两个目标 0 警告 0 错误。
- 不新增自动化测试项目；改动靠构建 + 必要手测。
- 切换到内置精选时行为与改动前一致；联想源在目录未加载/后端不可用时给出明确错误与重试入口，
  不白屏、不崩溃。
- 目录 5111 款时列表可滚动、搜索/分类/排序可用；不一次请求全部图标。

## 5. 任务划分与写权限（避免并发冲突）

| 任务 | 负责范围（写作用域） |
| --- | --- |
| backend-core | `Backend/lenovo/`（errors/token/client/models/catalog/details/media/service + `__init__.py`）、`Backend/tests/test_lenovo_client.py`、`test_lenovo_catalog.py`、`test_lenovo_details.py`、`test_lenovo_media.py` |
| backend-api | `Backend/lenovo/routes.py`、`Backend/config.py`、`Backend/config.json`、`Backend/main.py`、`Backend/tests/test_lenovo_api.py`、`Backend/tests/test_lenovo_config.py`、`Backend/README.md`、根 `README.md`（后端小节） |
| client-service | `WinAppsManager/WinAppsManager/Models/AppSettings.cs`、`Models/AppsInstallerModels.cs`、`Models/StoreCatalogModels.cs`（新）、`Models/CatalogCacheModels.cs`、`Models/AppRelease.cs`（如需）、`Services/AppSettingsService.cs`、`Services/AppCatalogService.cs`、`Services/ClientCacheStore.cs`、`Services/StoreMediaService.cs`（新）、`Services/DownloadCenterService.cs`、`Services/DownloadNetworkService.cs` |
| client-ui | `Pages/AppsInstallPage.xaml(.cs)`、`Pages/AppDetailsPage.xaml(.cs)`、`Pages/SettingsPage.xaml(.cs)`、`Dialogs/ScreenshotViewerWindow.xaml(.cs)`（新）、`MainWindow.xaml(.cs)`、`App.xaml`（仅在必须时）、根 `README.md`（客户端小节） |

依赖顺序：backend-core 与 client-service 可并行；backend-api 依赖 backend-core 的
`service.py`/`models.py`；client-ui 依赖 client-service 的公开 API。Lead 负责编写本文档与
`docs/lenovo-store-api.md`、最终集成、全量测试与构建验证。

## 6. 后续增量需求（已实现）

1. **内置源启用范围**：`Backend/config.json` 新增 `builtin` 段（`enabled` / `app_ids`），默认只启用 Clash Verge Rev（`clashverge`）与 Telegram（`telegram`）两款。未在白名单里的目录条目保留在 `CatalogRepository.all_apps`，但不会出现在 `/api/v1/apps*`、不会创建 provider、不参与启动预热与定时刷新；`/api/v1/sources` 的 `builtin.enabled/app_count` 反映过滤结果；未知 app id 让后端启动失败。禁用时 `/api/v1/apps*` 与 `POST /refresh` 返回 503。
2. **服务端地址可配置（客户端）**：`AppSettings.BackendBaseUrl`（默认 `http://127.0.0.1:8080/`），设置页“服务端地址”支持输入/测试连接（`GET /api/v1/health`）/恢复默认；切换后重建 HttpClient 与按服务端 URL 隔离的本地缓存，事件 `AppCatalogService.BackendChanged` 通知页面。
3. **软件源动态发现（客户端）**：设置页从 `GET /api/v1/sources` 加载源列表，不可用的源标注“（不可用）”并禁止选择；当前源在服务端不可用时自动回退到 `is_default` 的可用源。
4. **图标直连联想 CDN**：列表/搜索响应只携带图标 URL；`icon_url` 为联想 CDN 绝对地址（`lsw-fast.lenovo.com.cn` / `imgs.lenovomm.cn` / `pc1.gtimg.com`），客户端直连取图并写入本地图标缓存（请求带 `WinAppsManager/1.0` UA，联想 CDN 拒绝 curl UA），失败回退 `icon_proxy_url` 的后端代理；截图/封面仍走后端代理 + `StoreMediaService` 缓存。

## 7. 增量变更（2026-11）：联想目录请求只读缓存 + 5 分钟后台刷新

用户反馈“请求时后端直接访问联想、没有读本地缓存”，原因是旧实现里 `refresh=true` 会同步重建目录、陈旧快照会在请求路径触发后台刷新，且后端没有定时刷新。现在按以下契约实现：

1. **启动预加载**：`lifespan` 中 `preload_catalog()` 只读 `<cache>/lenovo/catalog.json` 到内存（约 0.17 秒 / 5111 条），不联网；测试注入假服务时用 `getattr` 兼容。
2. **请求路径只读缓存**：`GET /store/lenovo/apps`（任何查询参数都不会触发抓取）返回内存/磁盘快照，平均 0.1 秒内；`refresh=true` 仅触发一次后台单飞刷新；只有完全没有快照的冷启动才同步抓取一次（约 1.5 秒 / 28 个分类请求）并原子写盘。
3. **独立后台线程定时刷新**：服务启动时创建 daemon 线程 `lenovo-catalog-refresher`（`threading.Thread`），每 `lenovo.catalog_refresh_interval_seconds`（默认 300 秒，`0` 关闭）通过 `asyncio.run_coroutine_threadsafe` 把刷新投递到主事件循环执行；成功后原子写回 catalog.json，失败保留旧快照并记 WARNING。首次刷新按快照年龄决定：快照超龄则启动立即补刷，快照新鲜则等剩余时间，无快照则等一个间隔（启动不联网，冷启动由首个请求兜底）。
4. **手动刷新与客户端**：客户端已**移除刷新按钮与手动刷新链路**，目录完全由后端线程更新；服务端保留 `POST /api/v1/store/lenovo/refresh`（同步刷新）供运维/调试使用；`catalog_ttl_seconds` 只决定响应里的 `stale` 标记。
5. **诊断**：`GET /api/v1/cache` 的 `lenovo.catalog` 暴露 apps/fetched_at/age_seconds/refresh_interval_seconds/refresher_running/next_refresh_at/last_error/refreshes 等只读字段；后端日志记录预加载、定时刷新启动与每轮结果。
6. **测试隔离**：`clear()` 在 `cache.persist=false` 时不再删除磁盘快照，避免既有测试误删开发机真实 `Backend/.cache/lenovo/catalog.json`。

## 8. 增量变更（2026-11）：360 软件管家源

用户要求新增 360 软件源；用浏览器 DevTools MCP 分析 360 应用中心
（<https://cdn.soft.360.cn/view/home.html#/app/cate>）后确认其目录/详情接口为**明文 JSON、无需 token/签名**，
最终实现为 `Backend/soft360/` + 客户端 `AppSourcePreference.Soft360`。

1. **目录**：`GET https://cdn-mgr.soft.360.cn/main/cdn_client/v1/soft/list?cate_id={id}&page_size=100&src=web_client&machine=&page={n}`，
   `page_size` 最大 100，空数组结束；24 个分类 id 通过点击页面分类按钮抓取请求得到。
2. **规模**：360 全量约 43,731 款，客户端全量加载不可行；默认每分类抓 3 页（100/页），
   24 分类 ×3 页 = 7200 条原始记录，按 `soft_id` 去重后 **5940 款**（与联想 5111 款同量级），
   一轮抓取实测约 7–8 秒（并发 4）。`soft360.catalog_pages_per_category`（1–20）可调整。
3. **详情**：`GET https://mgr.soft.360.cn/main/cdn_client/v1/soft/getinfo?scene=1&machine=&soft_id={ids}`，
   单次最多约 51 个 id；字段含 version、corp、desc、snapshot、logo、baoku_cate、edition_info.default_edition
   （os.bit/os.ver、down.default.name/size/md5/p2p）。
4. **下载**：从 `p2p` 的 `pdown://...|https://...|||https://...` 字符串与 `multi_urls` 中正则提取候选 URL，
   优先 https，扩展名限 `.exe/.msi/.msix/.appx/.msu`，文件名清洗；md5 不写入 `sha256`；详情/下载 URL 不缓存。
5. **图片**：logo/snapshot 在 `*.qhimg.com`/`*.mediav.com`，后端代理缓存（ETag/304，30 天、256MB 上限），
   http 地址规范化为 https；客户端图标直连 CDN 白名单（qhimg/mediav/qhres2/qhres），失败回退后端。
6. **搜索**：页面搜索走 `enc_client/v2/search/*` 加密签名接口，本项目已在 `Backend/soft360/search.py` 逆向（AES-128-CBC + RSA PKCS#1 v1.5 + MD5，算法见 `docs/soft360-search-api.md`）；`/api/v1/store/360/search`
   只对已缓存目录做名称/描述/分类匹配，因此只覆盖热门子集。
7. **刷新**：`catalog.json`（内存/磁盘）随服务启动预加载；独立线程 `soft360-catalog-refresher`
   默认每 1800 秒抓取并原子写盘，失败保留旧快照；无快照冷启动等待一个间隔（不联网），
   `POST /api/v1/store/360/refresh` 可手动同步刷新。GET 只读缓存，任何查询参数都不会触发抓取。
8. **客户端**：`AppSourcePreference` 追加 `Soft360`（保持 Lenovo=0/BuiltIn=1 兼容旧设置）；
   目录/详情/下载/图标/截图复用既有商店数据流；设置页从 `/sources` 映射 `soft360`；缓存按源隔离。
9. **接口**：`GET /api/v1/store/360/{categories,apps,apps/{soft_id},apps/{soft_id}/download,apps/{soft_id}/icon,apps/{soft_id}/screenshots/{index},search}`，
   `POST /refresh`；`/sources` 第三项、`/cache` 的 `soft360` 段、`DELETE /cache` 清理同上。
10. **限制**：目录为热门子集而非全量（搜索已覆盖全量）；加密搜索接口为未公开接口，360 改版可能失效（失败自动回退本地匹配）；`os.ver` 编码按 1..8 推测映射（低于 7 按 7 处理）；
    部分条目 corp 为空、无 SHA256。详见 `docs/soft360-store-api.md`。

## 9. 增量变更（2026-11）：在线搜索标志与图片 CDN 直连

1. **`/api/v1/sources` 新增 `online_search`**：联想=true、360=true、内置精选=false。客户端据此显示
   “在{源}在线搜索”入口；旧服务端缺少字段时按源回退（联想/360 可用、内置不可用），不再在页面硬编码源。
2. **图片 CDN 直连**：联想详情 `screenshots[].url` / `cover_url` 与 360 `screenshots[].url` 优先返回
   对应 CDN 的 https 绝对地址（联想 `*.lenovo.com.cn`/`*.lenovomm.cn`/`*.gtimg.com`，360
   `*.qhimg.com`/`*.mediav.com`/`*.qhres2.com`/`*.qhres.com`）；无法通过白名单校验时仍返回原相对代理路径。
3. **代理兜底**：详情同时返回 `proxy_url` / `cover_proxy_url`（`/api/v1/store/...`），客户端
   `StoreMediaService` 先直连 CDN，失败再回退代理；后端 `/icon`、`/screenshots/{index}`、`/cover`
   代理路由保留，用于直连失败/调试，正常浏览不再占用后端图片带宽。
4. **客户端媒体白名单**：`StoreMediaService.TryMediaUri` 允许当前后端 `/api/v1/store/` 路径或上述
   CDN 的任意子域，拒绝 userinfo/非 http(s)/任意主机；直连失败静默回退代理。
5. **安全**：后端只暴露白名单 CDN 的 https 图片地址；联想视频缩略图虽可能带临时 `auth_key`，但视频
   播放仍走原后端代理，不暴露视频地址。

## 10. 增量变更（2026-11）：搜索模式与去域名白名单

1. **`/sources` 新增 `search_mode`**：lenovo=`catalog`（完整 5111 款目录，本地搜索，不再需要 API 搜索；`online_search=false`）、soft360=`api`（目录是热门子集，客户端输入时自动调用服务端搜索 API）、builtin=`catalog`。客户端读取该字段决定搜索方式；旧服务端缺少字段时按源回退。
2. **图片不再依赖固定域名白名单**：联想/360 详情截图与封面返回任意安全公网 http(s) CDN 绝对地址，`p15.qhmsg.com` 等新域名无需改代码；后端图片代理用 `Backend/url_safety.py` 做 SSRF 防护（拒绝 localhost/私网/回环/数字 IP 伪装/userinfo/内网后缀；重定向逐跳校验），不再使用 `qhimg/mediav/lenovo` 域名清单。
3. **代理兜底保留**：`proxy_url`/`cover_proxy_url` 始终存在，客户端直连失败时回退；`icon_url` 图标同理，未知 CDN 图标可直连，失败回退 `icon_proxy_url`。
4. **客户端图片校验**：`StoreMediaService`/`ClientCacheStore` 只做安全公网 URL 校验，不再维护 CDN 域名白名单；后端代理路径始终放行（后端可能在局域网/回环）。
5. **已知限制**：客户端不做 DNS 解析，SSRF 防护针对 URL/主机/IP 字面量；后端只监听本机，图片代理重定向已逐跳校验。
