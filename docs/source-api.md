# 软件源统一 API 契约

目标：客户端不包含任何软件源 ID、名称、接口路径或字段分支。新增一个软件源只需在服务端：

1. 在 `GET /api/v1/sources` 增加来源描述；
2. 实现该来源的 adapter（目录/详情/搜索/下载）；
3. 可选地提供截图/封面/视频能力。

客户端只读取 `/sources`，所有来源使用同一组 `/api/v1/sources/{source_id}/...` 接口和同一套 JSON 字段。

> 实现状态（已落地）：`Backend/unified/` 用注册表 + 适配器实现本契约，联想应用商店、360 软件管家
> 与内置精选三个来源已接入；旧的 `/api/v1/store/lenovo/*`、`/api/v1/store/360/*`、`/api/v1/apps*`
> 路由保持不变（内部复用同一份服务/缓存），客户端可逐步迁移。

## 1. 来源描述

`GET /api/v1/sources` 返回：

```json
{
  "items": [
    {
      "id": "lenovo",
      "name": "联想应用商店",
      "description": "……",
      "enabled": true,
      "search_mode": "catalog",
      "online_search": false,
      "app_count": 5111,
      "is_default": true,
      "updated_at": "2026-11-15T00:00:00Z",
      "capabilities": {
        "categories": true,
        "catalog": true,
        "search": true,
        "details": true,
        "download": true,
        "screenshots": true,
        "cover": true,
        "video": true,
        "refresh": false
      }
    }
  ],
  "total": 1
}
```

说明：

- `capabilities` 缺省全部为 `false`（除 `catalog/details/download` 客户端可假定为 true）。客户端只根据它决定是否请求可选接口，不按来源 ID 判断。
- `search_mode` 仍为 `catalog` 或 `api`：`catalog` = 目录是完整列表，客户端在本地筛选/搜索；`api` = 目录只是子集，必须调用 `/search`（即 `online_search=true`）。`online_search` 是给旧客户端的兼容字段，新客户端优先 `search_mode`。
- `app_count` 只反映服务端**已缓存**的数据（不会为了统计触发抓取），目录尚未加载时为 `null`；`updated_at` 同理。
- `enabled=false` 的来源仍会列在这里（便于客户端展示/切换），但它的所有 `/api/v1/sources/{source_id}/...` 接口返回 404。
- 顺序 = 服务端注册顺序（当前为 `lenovo`、`builtin`、`soft360`）；`is_default` 当前只有 `lenovo=true`，客户端首次启动或当前来源不可用时回退到它。
- 响应 `Cache-Control: no-store`。

当前能力矩阵：

| 来源 | categories | catalog | search | details | download | screenshots | cover | video | refresh |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `lenovo` 联想应用商店 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ |
| `soft360` 360软件管家 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ |
| `builtin` 内置精选 | ✅ | ✅ | ❌ | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ |

`refresh` 当前在所有来源均为 `false`：手动刷新接口已禁用（返回 404），目录只由启动预热与各来源的后台定时线程更新，避免公网调用重复抓取上游。

## 2. 统一接口

所有路径前缀：`/api/v1/sources/{source_id}`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/categories` | 分类列表 |
| GET | `/apps` | 完整目录（客户端本地筛选/分页） |
| GET | `/search?q=&limit=` | 服务端搜索（`search_mode=api` 时使用；`q` 1–100 字符，`limit` 1–50，默认 30） |
| GET | `/apps/{app_id}` | 详情 |
| GET | `/apps/{app_id}/download?architecture=x64` | 下载地址（`Release`，`architecture` 可选，默认 `x64`） |
| GET | `/apps/{app_id}/icon` | 图标代理（可选，支持 `ETag`/304） |
| GET | `/apps/{app_id}/screenshots/{index}` | 截图代理（可选，支持 `ETag`/304） |
| GET | `/apps/{app_id}/cover` | 封面代理（可选，支持 `ETag`/304） |
| GET | `/apps/{app_id}/screenshots/{index}/video` | 视频播放代理（可选，Range/206 流式转发，`HEAD` 只返回响应头） |

约束：

- `app_id` 是**来源内的应用 ID**（联想 `softID`，如 `L105867`/`11488`；360 `soft_id`，如 `1010`；内置精选目录 ID，如 `qq`），保留原始大小写，由目录/搜索返回的 `id` **原样**使用，不做客户端拼接。客户端缓存时用 `source_id + ":" + app_id` 作为本地主键。
- 目录/分类/详情/搜索/下载/视频（含 `HEAD`）响应都是 `Cache-Control: no-store`；下载地址每次实时解析，绝不进入任何中间缓存，也不写日志。手动刷新接口（`POST /refresh`）已禁用并返回 404。
- 图片（图标/截图/封面）响应为 `Cache-Control: public, max-age=86400` + `ETag`，`If-None-Match` 命中（兼容引号、`W/` 前缀与逗号列表）返回 304。
- 来源被禁用或 `source_id` 未知：404 `{"detail":"软件源不存在或未启用"}`；来源不支持的能力：404 `{"detail":"该软件源不支持此能力。"}`（客户端本应根据 `capabilities` 不请求）。
- 上游协议/网络错误沿用全局 `ProviderError` 映射：502；超时：504；来源内资源不存在：404；来源被禁用：404（统一 API 不复用旧路由的 503）。

## 3. 统一应用字段

目录/搜索条目、详情共用的字段：

```json
{
  "id": "11488",
  "name": "软件名",
  "description": "一句话简介",
  "category": "聊天工具",
  "category_id": "chat",
  "group": "software",
  "version": "1.2.3",
  "size_display": "123 MB",
  "size_bytes": 128974848,
  "publisher": "发布者",
  "platforms": "Windows",
  "architectures": ["x64", "x86"],
  "minimum_windows": "7",
  "icon_url": "https://cdn.example/icon.png",
  "icon_proxy_url": "/api/v1/sources/lenovo/apps/11488/icon",
  "icon_text": "软",
  "icon_background": "#0067C0",
  "tags": ["安全认证"],
  "score": 88,
  "download_count": "1.2亿次",
  "license": "免费",
  "is_free": true,
  "is_recommended": false,
  "is_beta": false,
  "sort_order": 0,
  "installation_notes": "……",
  "website_url": null,
  "download_page_url": null
}
```

核心字段：`id`、`name`、`description`、`category`、`category_id`、`version`、`icon_url`。其余字段可为空/缺省：

- `architectures` 缺省时客户端视为全兼容；`minimum_windows` 为 `"7"`/`"8"`/`"8.1"`/`"10"`/`"11"` 或 `null`。
- `version` 可以为 `null`：360 的目录/搜索接口不返回版本（客户端显示“版本未知/以官网为准”），详情接口返回真实版本。
- `icon_url` 是可选 CDN 绝对 http(s) 地址；内置精选没有独立 CDN，返回 `null`，客户端改用 `icon_proxy_url`。
- `icon_proxy_url` 可以是任意**后端相对 URL**（以 `/` 开头）；客户端只按返回值请求，不构造路径。内置精选取 `/api/v1/sources/builtin/apps/{id}/icon`（直接复用同一份图标文件与 ETag/304 逻辑，不递归调用旧 `/api/v1/apps/{id}/icon` 路由）。
- `icon_text`/`icon_background` 缺省时客户端从名称自动生成。
- `size_bytes` 为 `null` 时不显示大小（客户端可回退 `size_display`）。
- `is_recommended` / `is_beta` **恒为布尔值**：来源没有该信息时给 `false`，不返回 `null`（部分严格客户端无法把 JSON `null` 反序列化成非空布尔字段）。
- `website_url` / `download_page_url` 为可选展示字段：内置精选来自目录并返回真实地址；联想与 360 没有官网/下载页字段，返回 `null`（客户端隐藏对应按钮）。
- 已知来源的字段差异：
  - 联想：`group` 为 `software`/`game`，带 `size_display`/`download_count`/`score`/`is_free` 等完整信息；
  - 360：目录/搜索无 `version`、`size_display`、`platforms`、`publisher`，详情补齐 `version`/`publisher`/`long_description`；
  - 内置精选：`category_id` 等于分类名（目录没有独立分类 ID），`icon_url` 为 `null`，`website_url`/`download_page_url` 有值。

## 4. 分类响应

```json
{
  "source": "lenovo",
  "updated_at": "2026-11-15T00:00:00Z",
  "categories": [
    {"id": "chat", "name": "聊天工具", "count": 32, "group": "software"}
  ]
}
```

- `group` 可选（联想分类树按 `software`/`game` 分组，统一响应按原顺序展平并带上 `group`；360 与内置精选为 `null`）。
- 客户端左边栏按 `categories` 顺序展示。若来源没有分类，返回空数组，客户端显示“全部”。
- `count` 是该来源当前快照内的条目数（不触发抓取）。

## 5. 目录 / 搜索响应

```json
{
  "source": "lenovo",
  "source_name": "联想应用商店",
  "updated_at": "2026-11-15T00:00:00Z",
  "stale": false,
  "total": 1,
  "items": [ { "id": "11488", "name": "软件名", "...": "..." } ]
}
```

`/search` 额外带 `query`，其余相同。`total` 必须等于 `items` 数量。`stale=true` 表示服务端快照已超过来源的 TTL（客户端可提示“目录偏旧”），不代表请求失败。

## 6. 详情响应

在应用字段基础上增加：

```json
{
  "long_description": "……",
  "release_notes": "……",
  "screenshots": [
    {
      "index": 0,
      "kind": "image",
      "url": "https://cdn.example/shot.png",
      "proxy_url": "/api/v1/sources/lenovo/apps/11488/screenshots/0",
      "video_proxy_url": null
    },
    {
      "index": 1,
      "kind": "video",
      "url": "https://cdn.example/video-thumb.jpg",
      "proxy_url": "/api/v1/sources/lenovo/apps/11488/screenshots/1",
      "video_proxy_url": "/api/v1/sources/lenovo/apps/11488/screenshots/1/video"
    }
  ],
  "cover_url": "https://cdn.example/cover.jpg",
  "cover_proxy_url": "/api/v1/sources/lenovo/apps/11488/cover",
  "labels": ["安全认证"],
  "ranking": "……",
  "detail_tags": [{"name": "安全认证", "tips": "……"}]
}
```

- `screenshots` 可为空；`kind` 为 `image` 或 `video`。视频截图的 `url` 是缩略图。
- `proxy_url` / `cover_proxy_url` / `video_proxy_url` 都是**统一前缀下的后端相对路径，可直接请求**（联想与 360 的图片代理复用原 `/store/...` 的同一份缓存，但对外路径是统一路径）。
- `video_proxy_url` 仅视频截图返回；客户端直接播放该 URL（Range/206），不再自己拼接来源路径。
- `cover_url` 可为空；`cover_proxy_url` 可为任意后端相对 URL。联想与 360 没有封面能力时两者都为 `null`。
- 所有图片 `url` 为可选 CDN 绝对地址（只放安全的公网地址；不安全或来源不提供时置 `null`），`proxy_url` 为可选后端相对兜底。
- 内置精选没有截图/封面/视频，三个字段为空数组或 `null`。

## 7. 下载响应

沿用现有 `Release` 结构：

```json
{
  "id": "lenovo-11488-x64-installer",
  "version": "1.2.3",
  "platform": "windows",
  "architecture": "x64",
  "download_url": "https://...",
  "file_name": "setup.exe",
  "size_bytes": 123456,
  "sha256": null
}
```

响应使用 `Cache-Control: no-store`，下载地址不缓存、不写日志。内置精选的下载地址与旧 `/api/v1/apps/{id}/releases/latest` 使用同一条解析/签名逻辑（QQ 等插件会重新签名）。

## 8. 错误

- 未知/禁用的 `source_id`：404，`{"detail":"软件源不存在或未启用"}`。
- 应用不存在：404。
- 不支持的能力（如内置源截图/搜索/刷新）：404，`{"detail":"该软件源不支持此能力。"}`；客户端应根据 `capabilities` 不请求。
- 上游失败：沿用现有错误映射（502/504），客户端显示可读错误；来源返回的 4xx 已经由适配器区分成 404（资源不存在）或 502（上游拒绝），不会把上游 5xx 直接透传。

## 9. 客户端要求

- 不再定义 `AppSource` 枚举、来源 ID 常量、来源名称映射或按来源 ID 的 `if/switch`。
- 当前来源保存为 `source_id` 字符串；旧 `settings.json` 的 `Lenovo`/`BuiltIn`/`Soft360` 只需在迁移时映射一次。
- 来源列表、默认源、名称、`search_mode`、能力全部来自 `/sources`。
- 客户端缓存主键用 `source_id + ":" + app_id`；图片/视频地址只使用响应里的原值（相对代理路径按当前后端地址解析成绝对 URL）。
- 新增来源只改后端；客户端自动显示、切换、搜索、详情、下载。

## 10. 服务端实现（`Backend/unified/`）

```text
Backend/unified/
  models.py            统一响应模型（SourceAppSummary / SourceAppDetails / SourceCatalog /
                       SourceCategoriesResponse / SourceSearchResponse / SourcesResponse / capabilities）
  adapters.py          SourceAdapter 基类 + SourceRegistry 注册表
  factory.py            create_source_registry()：新增来源的唯一注册点
  routes.py             /api/v1/sources 与 /api/v1/sources/{source_id}/... 路由
  media.py             图片 ETag/304、视频流响应的共享辅助
  lenovo_adapter.py    联想适配器（转发 LenovoStoreService，全能力）
  soft360_adapter.py   360 适配器（转发 Soft360StoreService，无封面/视频）
  builtin_adapter.py   内置精选适配器（CatalogRepository + ReleaseService + IconService，无搜索/媒体/刷新）
```

三个适配器只做**模型转换 + 错误翻译**，不复制抓取逻辑：联想/360 复用各自的 service（同一份目录/详情/图片缓存），内置精选直接复用 `ReleaseService`/`CatalogRepository`/`IconService`（**不通过 HTTP 递归调用统一路由**，也不调用旧路由）。

### 新增一个软件源（仅服务端）

1. 新建 `Backend/unified/<source>_adapter.py`，继承 `SourceAdapter`，声明 `source_id`/`source_name`/`source_description`/`search_mode`/`is_default`/`capabilities`，覆盖需要的 `list_categories`/`list_apps`/`search`/`get_app`/`get_download`/`icon_response`/`screenshot_response`/`cover_response`/`open_video`（手动刷新接口已禁用，不需要实现 `refresh`）；
2. 在 `Backend/unified/factory.py` 的 `create_source_registry()` 里注册一个实例（需要新服务/配置时再按联想/360 的方式在 `Backend/main.py` lifespan 中创建该服务并传入）；
3. 需要开关/参数时在 `Backend/config.py` 增加配置；
4. 客户端不需要任何改动：`/sources` 自动出现新来源，统一路由自动可用（未实现的能力按 `capabilities` 返回 404）。

适配器约定：来源内“找不到”翻译成 `SourceNotFoundError`（404）、不支持的能力抛 `SourceUnsupportedError`（404）、来源禁用/不可用抛 `SourceUnavailableError`（503）；上游 502/504 类异常（`ProviderError` 子类）原样抛出，由 `Backend/main.py` 的全局处理器映射。

### 测试

`Backend/tests/test_unified_sources.py`（全部离线，注入假服务/假 provider）覆盖：

- 三源的 `/sources` 描述与能力矩阵、未启用/未知来源 404、内置精选禁用；
- 三源统一 `categories`/`apps`/`search`/详情/`download`/图标/截图/封面/视频/刷新的形状、`Cache-Control`、404/502/504；
- 旧路由（`/api/v1/apps`、`/store/lenovo/*`、`/store/360/*`）行为不变；
- 新增来源只需 `SourceAdapter` + 注册：自定义 adapter 注册后统一路由与 `/sources` 自动可用；
- 统一模型的严格校验（必填字段、禁止多余字段、`total` 等于条目数、`video_proxy_url` 只属于视频截图、代理路径必须是后端相对路径）。
