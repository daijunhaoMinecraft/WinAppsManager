# WinAppsManager 后端

FastAPI 为桌面前端提供软件目录、软件详情、版本资源和图标。软件目录由 `data/apps.json`（原有 8 款精选软件）和 `data/apps.d/*.json`（37 款扩展软件，同一 schema，按文件名顺序合并）组成，共 45 款；版本和安装包全部由 `providers/` 从官方来源获取：11 款使用内置专用 provider（原有 8 款 + WorkBuddy、DeepSeek Harness、豆包 3 款非 GitHub 来源），其余 34 款使用通用的 GitHub Releases provider。前端调用 `http://127.0.0.1:8080/api/v1`。

后端同时提供三个软件源：默认的**联想应用商店**（`lenovo`，后端代理联想 PC 商店接口，约 5111 款软件与游戏，`/api/v1/store/lenovo/*`）、**360软件管家**（`soft360`，后端代理 360 应用中心接口，24 分类热门子集约 5940 款，`/api/v1/store/360/*`；搜索已逆向 `enc_client` 加密接口，覆盖 360 全量目录）和原有的**内置精选**（`builtin`，仓库内 45 款，`/api/v1/apps` 等既有接口，行为不变）。内置精选默认通过 `builtin.app_ids` 只启用 Clash Verge Rev（`clashverge`）与 Telegram Desktop（`telegram`）两款（其余数据条目原样保留、不抓取），也可用 `builtin.enabled=false` 整体关闭。`GET /api/v1/sources` 列出三个源的启用状态与软件数量；联想接口的调研结论（token、分类分页、搜索、详情、下载地址、图片 CDN）见 [`docs/lenovo-store-api.md`](../docs/lenovo-store-api.md)，360 接口的结论见下文“软件源（内置精选 / 联想应用商店 / 360 软件管家）”一节，360 搜索的加密协议见 [`docs/soft360-search-api.md`](../docs/soft360-search-api.md)。

目录条目用 `minimum_windows`（`7`/`8`/`8.1`/`10`/`11`）与 `architectures`（`x64`/`x86`/`arm64`）声明兼容范围，45 款均已按官方系统要求核实回填；`platforms` 是展示文案，统一写成「系统版本 · 架构」。`/api/v1/apps` 的 `architecture` 参数按 `architectures` 过滤列表。

## 运行

在此目录中使用 Python 3.10 或更高版本。Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

服务默认监听 `127.0.0.1:8080`，接口文档位于 <http://127.0.0.1:8080/docs>（Swagger UI 的 JS/CSS 随后端放在 `static/swagger/`，不依赖外部 CDN）。也可以在此目录中运行：

```powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8080
```

配置目录、软件数据、`data/apps.d/*.json` 或图标后重启后端，使启动校验与目录数据同步更新。

公网部署（Docker/systemd + HTTPS 反向代理）与安全清单见 [`docs/deploy.md`](../docs/deploy.md)。

## 配置

`config.json` 保存提交到仓库的运行时配置，启动时读取；文件不存在时使用内置默认值。同目录的 `config.local.json` 是机器本地覆盖文件（已加入 `.gitignore`，用于 GitHub 令牌等敏感值），会深合并覆盖 `config.json`；`WINAPPSMANAGER_*` 环境变量最后覆盖两者。加载顺序为：内置默认 → `config.json` → `config.local.json` → 环境变量。修改后重启后端生效。

| 配置项 | 默认值 | 说明 | 环境变量 |
| --- | --- | --- | --- |
| `cache.enabled` | `true` | 总开关；关闭后每次都向提供器取资料，同时关闭插件内缓存 | `WINAPPSMANAGER_CACHE_ENABLED` |
| `cache.persist` | `true` | 是否把成功取得的版本资料写入磁盘 | `WINAPPSMANAGER_CACHE_PERSIST` |
| `cache.directory` | `Backend/.cache` | 持久缓存目录（运行目录内，已加入 `.gitignore`）；相对路径按后端目录解析，`null` 也回落到该默认值 | `WINAPPSMANAGER_CACHE_DIR` |
| `cache.ttl_seconds` | `300` | 内存缓存有效期（秒） | `WINAPPSMANAGER_CACHE_TTL_SECONDS` |
| `cache.persistent_ttl_seconds` | `3600` | 磁盘快照有效期（秒），独立于内存 TTL | `WINAPPSMANAGER_CACHE_PERSISTENT_TTL_SECONDS` |
| `cache.max_stale_seconds` | `604800` | 过期快照还能作为失败兜底保留多久（秒），超过即删除 | `WINAPPSMANAGER_CACHE_MAX_STALE_SECONDS` |
| `cache.max_entries` | `128` | 内存和磁盘最多保留的提供器条目数，超出按最近最少使用/最早过期清理 | `WINAPPSMANAGER_CACHE_MAX_ENTRIES` |
| `logging.enabled` | `true` | 文件日志总开关；`false` 时不写文件（`console=true` 时仍输出到控制台） | `WINAPPSMANAGER_LOG_ENABLED` |
| `logging.directory` | `Backend/.cache/logs` | 轮转日志目录（`backend.log`，位于已 `.gitignore` 的 `.cache/` 下）；相对路径按后端目录解析，`null` 回落默认值；目录不存在时自动创建 | `WINAPPSMANAGER_LOG_DIR` |
| `logging.level` | `INFO` | root logger 级别：`DEBUG` / `INFO` / `WARNING` / `ERROR`（大小写不敏感，保存为大写），非法值让后端启动失败 | `WINAPPSMANAGER_LOG_LEVEL` |
| `logging.max_bytes` | `5242880` | 单个日志文件大小上限（字节，必须 ≥1024）；写满后轮转为 `backend.log.1`、`backend.log.2` … | `WINAPPSMANAGER_LOG_MAX_BYTES` |
| `logging.backup_count` | `5` | 保留的轮转备份个数，取值 1–50 | `WINAPPSMANAGER_LOG_BACKUP_COUNT` |
| `logging.console` | `true` | 是否同时输出到 stderr；文件日志不可用时总会保留控制台降级输出 | `WINAPPSMANAGER_LOG_CONSOLE` |
| `providers.cache_ttl_seconds` | `600` | 各下载插件内部版本缓存（秒） | `WINAPPSMANAGER_PROVIDER_CACHE_TTL_SECONDS` |
| `providers.timeout_seconds` | `10` | 单次版本读取/签名总期限（秒） | `WINAPPSMANAGER_PROVIDER_TIMEOUT_SECONDS` |
| `http.timeout_seconds` | `5` | 访问官网的总超时（秒） | `WINAPPSMANAGER_HTTP_TIMEOUT_SECONDS` |
| `http.connect_timeout_seconds` | `3` | 访问官网的连接超时（秒） | `WINAPPSMANAGER_HTTP_CONNECT_TIMEOUT_SECONDS` |
| `refresh.on_startup` | `true` | 启动时先抓取一轮再对外服务 | `WINAPPSMANAGER_REFRESH_ON_STARTUP` |
| `refresh.startup_timeout_seconds` | `30` | 启动预热的整体时间预算（秒），超时先启动服务 | `WINAPPSMANAGER_REFRESH_STARTUP_TIMEOUT_SECONDS` |
| `refresh.interval_seconds` | `1800` | 定时刷新间隔（秒），`0` 关闭 | `WINAPPSMANAGER_REFRESH_INTERVAL_SECONDS` |
| `refresh.jitter_seconds` | `0` | 定时刷新随机抖动上限（秒），不能大于间隔 | `WINAPPSMANAGER_REFRESH_JITTER_SECONDS` |
| `refresh.max_concurrency` | `4` | 抓取并发上限，取值 1–16 | `WINAPPSMANAGER_REFRESH_MAX_CONCURRENCY` |
| `refresh.use_stale_cache` | `true` | 抓取失败时允许使用持久化旧快照 | `WINAPPSMANAGER_REFRESH_USE_STALE_CACHE` |
| `refresh.drop_unavailable_apps` | `true` | 无任何可用数据的软件从列表中隐藏 | `WINAPPSMANAGER_REFRESH_DROP_UNAVAILABLE_APPS` |
| `github.token` | `""` | GitHub API 令牌；未认证 60 次/小时，带令牌 5000 次/小时。提交的 `config.json` 保持空字符串，真实令牌只放 `config.local.json` 或环境变量 | `WINAPPSMANAGER_GITHUB_TOKEN` |
| `github.api_base` | `https://api.github.com` | GitHub API 根地址，必须是无空白的 https 地址 | `WINAPPSMANAGER_GITHUB_API_BASE` |
| `github.timeout_seconds` | `10` | 单次 GitHub API 请求超时（秒） | `WINAPPSMANAGER_GITHUB_TIMEOUT_SECONDS` |
| `github.refresh_interval_seconds` | `900` | 启动预热/定时刷新等强制刷新在该间隔内直接复用磁盘上的条件缓存、不发请求；`0` 表示每次都发条件请求（数据未变时返回 304，不消耗配额），必须是非负数 | `WINAPPSMANAGER_GITHUB_REFRESH_INTERVAL_SECONDS` |
| `lenovo.enabled` | `true` | 联想应用商店源总开关；`false` 时不创建服务、`/api/v1/sources` 把联想源标记为未启用、所有 `/store/lenovo/*` 路由返回 503 | `WINAPPSMANAGER_LENOVO_ENABLED` |
| `lenovo.catalog_ttl_seconds` | `21600` | 联想目录响应的 `stale` 标记阈值（秒，默认 6 小时）：快照年龄超过该值时 `stale=true`，**不再触发请求路径抓取** | `WINAPPSMANAGER_LENOVO_CATALOG_TTL_SECONDS` |
| `lenovo.catalog_refresh_interval_seconds` | `300` | 独立后台刷新线程的唤醒间隔（秒，默认每 5 分钟全量刷新一次 `lenovo/catalog.json`），`0` 关闭线程；首次刷新按快照年龄决定（见下文） | `WINAPPSMANAGER_LENOVO_CATALOG_REFRESH_INTERVAL_SECONDS` |
| `lenovo.catalog_max_stale_seconds` | `604800` | 抓取失败时允许使用的最旧联想目录快照（秒，默认 7 天） | `WINAPPSMANAGER_LENOVO_CATALOG_MAX_STALE_SECONDS` |
| `lenovo.details_ttl_seconds` | `900` | 联想应用详情内存缓存 TTL（秒） | `WINAPPSMANAGER_LENOVO_DETAILS_TTL_SECONDS` |
| `lenovo.details_persistent_ttl_seconds` | `21600` | 联想应用详情磁盘缓存 TTL（秒） | `WINAPPSMANAGER_LENOVO_DETAILS_PERSISTENT_TTL_SECONDS` |
| `lenovo.search_cache_ttl_seconds` | `60` | 联想搜索内存缓存 TTL（秒），不落盘 | `WINAPPSMANAGER_LENOVO_SEARCH_CACHE_TTL_SECONDS` |
| `lenovo.timeout_seconds` | `20` | 单次联想请求总超时（秒，连接超时固定 5 秒），必须是正数 | `WINAPPSMANAGER_LENOVO_TIMEOUT_SECONDS` |
| `lenovo.catalog_concurrency` | `3` | 联想分类列表抓取并发，取值 1–6 | `WINAPPSMANAGER_LENOVO_CATALOG_CONCURRENCY` |
| `lenovo.image_cache_ttl_seconds` | `2592000` | 联想图片磁盘缓存 TTL（秒，默认 30 天） | `WINAPPSMANAGER_LENOVO_IMAGE_CACHE_TTL_SECONDS` |
| `lenovo.image_cache_max_bytes` | `268435456` | 联想图片磁盘缓存上限（字节，默认 256 MB，超出按最旧淘汰），必须是正整数 | `WINAPPSMANAGER_LENOVO_IMAGE_CACHE_MAX_BYTES` |
| `soft360.enabled` | `true` | 360 软件源总开关；`false` 时不创建服务、`/api/v1/sources` 把 360 源标记为未启用、所有 `/store/360/*` 路由返回 503 | `WINAPPSMANAGER_SOFT360_ENABLED` |
| `soft360.catalog_pages_per_category` | `3` | 每个分类抓取的页数（100 条/页，取值 1–20）。默认 3 页 = 24 分类约 7200 条原始记录、按 `soft_id` 去重后约 5940 条；360 目录全量约 43731 条，本项目只抓**各分类热门条目子集** | `WINAPPSMANAGER_SOFT360_CATALOG_PAGES_PER_CATEGORY` |
| `soft360.catalog_ttl_seconds` | `21600` | 360 目录响应的 `stale` 标记阈值（秒，默认 6 小时）：快照年龄超过该值时 `stale=true`，**不触发请求路径抓取** | `WINAPPSMANAGER_SOFT360_CATALOG_TTL_SECONDS` |
| `soft360.catalog_max_stale_seconds` | `604800` | 360 磁盘目录快照的最旧可用年龄（秒，默认 7 天），超过视为损坏并删除 | `WINAPPSMANAGER_SOFT360_CATALOG_MAX_STALE_SECONDS` |
| `soft360.catalog_refresh_interval_seconds` | `1800` | 360 目录独立后台刷新线程的唤醒间隔（秒，默认每 30 分钟刷新一次 `soft360/catalog.json`），`0` 关闭线程；首次刷新按快照年龄决定 | `WINAPPSMANAGER_SOFT360_CATALOG_REFRESH_INTERVAL_SECONDS` |
| `soft360.details_ttl_seconds` | `900` | 360 软件详情内存缓存 TTL（秒） | `WINAPPSMANAGER_SOFT360_DETAILS_TTL_SECONDS` |
| `soft360.details_persistent_ttl_seconds` | `21600` | 360 软件详情磁盘缓存 TTL（秒） | `WINAPPSMANAGER_SOFT360_DETAILS_PERSISTENT_TTL_SECONDS` |
| `soft360.timeout_seconds` | `20` | 单次 360 请求总超时（秒，连接超时固定 5 秒），必须是正数 | `WINAPPSMANAGER_SOFT360_TIMEOUT_SECONDS` |
| `soft360.catalog_concurrency` | `4` | 360 分类分页抓取并发，取值 1–16 | `WINAPPSMANAGER_SOFT360_CATALOG_CONCURRENCY` |
| `soft360.image_cache_ttl_seconds` | `2592000` | 360 图片磁盘缓存 TTL（秒，默认 30 天） | `WINAPPSMANAGER_SOFT360_IMAGE_CACHE_TTL_SECONDS` |
| `soft360.image_cache_max_bytes` | `268435456` | 360 图片磁盘缓存上限（字节，默认 256 MB，超出按最旧淘汰），必须是正整数 | `WINAPPSMANAGER_SOFT360_IMAGE_CACHE_MAX_BYTES` |
| `builtin.enabled` | `true` | 内置精选源总开关；`false` 时不创建内置 provider、不预热、不启动定时刷新，`/api/v1/apps*` 与 `/api/v1/refresh` 返回 503 | `WINAPPSMANAGER_BUILTIN_ENABLED` |
| `builtin.app_ids` | `["clashverge","telegram"]`（提交的 `config.json`） | 内置精选源白名单，为空表示目录里的全部软件；元素必须匹配 `^[a-z0-9][a-z0-9.-]*$`、最多 200 个且不可重复。目录数据仍完整加载校验，未知 id 让启动失败 | `WINAPPSMANAGER_BUILTIN_APP_IDS`（逗号分隔，空串=空列表） |

令牌只作为 `Authorization: Bearer ...` 请求头发送，不会写入日志、缓存或任何 API 响应：`GET/DELETE /api/v1/cache` 返回的 `settings.github.token` 会被替换成 `***`（未配置时为 `""`），并附带 `token_configured` 布尔值。令牌不能包含空白字符或换行；`github.api_base` 必须是无空白的 https 地址。`config.local.json` 和 `.venv/`、`.cache/` 都不要提交。

把 `WINAPPSMANAGER_CONFIG` 指向其他 JSON 文件可改用另一份配置。`ttl_seconds` 设为 `0` 会关闭内存命中，`persistent_ttl_seconds` 设为 `0` 会关闭磁盘命中；`persist` 为 `false` 时不创建持久缓存目录。`cache.max_stale_seconds` 只影响过期快照的保留时长，超出的快照会被删除；`refresh.interval_seconds` 为 `0` 时不启动定时刷新，`refresh.jitter_seconds` 不能大于间隔，`refresh.max_concurrency` 只能是 1–16 的整数，`github.refresh_interval_seconds` 只能是非负数。

配置校验失败（未知字段、负数 TTL、非正数上限、格式错误的 JSON 等）会让后端启动失败，而不是带病运行。`lenovo.*` 段同样严格：TTL 与 `catalog_refresh_interval_seconds` 必须是非负有限数、`timeout_seconds` 必须是正数、`catalog_concurrency` 只能是 1–6 的整数、`image_cache_max_bytes` 必须是正整数，非法值同样让启动失败。`soft360.*` 段同样严格：TTL 与 `catalog_refresh_interval_seconds` 必须是非负有限数、`timeout_seconds` 必须是正数、`catalog_pages_per_category` 只能是 1–20 的整数、`catalog_concurrency` 只能是 1–16 的整数、`image_cache_max_bytes` 必须是正整数，非法值同样让启动失败。`builtin.app_ids` 的元素必须是合法 app id、最多 200 个且不可重复；白名单里出现目录中不存在的 id 时，后端启动阶段直接报 `ValueError`。`logging.*` 段同样严格：`level` 只能是 `DEBUG`/`INFO`/`WARNING`/`ERROR`，`max_bytes` 必须是不小于 1024 的整数，`backup_count` 只能是 1–50 的整数，`enabled`/`console` 必须是布尔值，非法值让启动失败。

## 日志

后端在 lifespan 最开始时初始化日志，默认同时写轮转文件和控制台（stderr）：

- 文件：`Backend/.cache/logs/backend.log`（可用 `logging.directory` / `WINAPPSMANAGER_LOG_DIR` 改到别处）。目录不存在时自动创建，UTF-8 编码；目录不可写、磁盘只读或文件被占用时自动降级为「仅控制台」，并记录一条错误，**不会阻止服务启动**。
- 轮转：单文件写满 `logging.max_bytes`（默认 5 MB）后改名为 `backend.log.1`，保留 `logging.backup_count`（默认 5）个备份，更旧的删除。
- 格式：`时间 级别 logger: 消息`，例如 `2026-11-14 19:30:38 INFO winappsmanager.main: 后端启动：…`；root 级别取 `logging.level`（默认 `INFO`）。`logging.enabled=false` 时不写文件，`logging.console=false` 时不再输出控制台。
- uvicorn 的 `uvicorn` / `uvicorn.error` / `uvicorn.access` 三个 logger 由后端统一接管，启动/关闭消息和访问日志写进同一个 `backend.log`，同时保持 `propagate=False` 并替换 uvicorn 自己的 handler，避免同一条日志重复出现。`python main.py` 启动时使用 `log_config=None`，由本模块作为唯一日志出口；用 `uvicorn main:app` 启动时也会在 lifespan 中接管这三个 logger。
- 启动与关闭各记一条 INFO 摘要：后端版本、默认监听地址（`127.0.0.1:8080`）、缓存目录、内置精选源/联想应用商店源/360 软件源的启用状态、日志文件位置与级别。**不记录** GitHub 令牌、签名下载地址、代理认证信息或任何请求头；第三方 HTTP 客户端（httpx2）的逐请求 INFO 日志压到 `WARNING`，避免把带临时参数的上游 URL 写进日志文件。
- `GET /api/v1/cache` 新增 `logging` 段：`enabled`、`level`、`console`、`directory`、`current_path`、`file_count`、`total_bytes`。该段是只读统计，不会创建目录、写文件或触发轮转。`Backend/logging_config.py` 还提供 `shutdown_logging()` 关闭 handler、`get_log_files()` 统计日志文件（供诊断与测试使用）。

## 软件源（内置精选 / 联想应用商店 / 360 软件管家）

后端提供三个软件源，客户端可在设置里切换，默认使用联想应用商店：

| 源 id | 名称 | 接口 | 说明 |
| --- | --- | --- | --- |
| `lenovo` | 联想应用商店 | `/api/v1/store/lenovo/*` | 后端代理联想 PC 商店的 token 注册、分类/目录、详情、搜索、下载地址解析与图片；目录约 5111 款（软件 + 游戏） |
| `builtin` | 内置精选 | `/api/v1/apps` 等既有接口 | 仓库内的 45 款精选软件；默认通过 `builtin.app_ids` 只启用 Clash Verge Rev（`clashverge`）与 Telegram Desktop（`telegram`） |
| `soft360` | 360软件管家 | `/api/v1/store/360/*` | 后端代理 360 应用中心的分类/目录、详情、下载地址解析与图片；目录是**各分类热门子集**，默认约 5940 款（24 分类 × 前 3 页）；**搜索已逆向 `enc_client` 加密接口，覆盖 360 全量目录**（失败回退缓存子集匹配），搜索到的目录外软件也能打开详情/下载 |

`GET /api/v1/sources` 返回三个源的 `enabled`、`online_search`、`app_count`、`is_default`、`updated_at`；联想源与 360 源目录还没加载时 `app_count` 是 `null`（统计不会触发抓取）。内置精选的 `app_count` 是 `builtin.app_ids` 白名单过滤后的数量（默认 2），`enabled` 取 `builtin.enabled`。`online_search` 表示该源是否支持**服务端在线搜索**，与 `enabled` 无关：联想源与 360 源为 `true`，内置精选为 `false`；客户端据此决定是否显示在线搜索入口。联想源未启用（`lenovo.enabled=false`）时 `/sources` 仍会列出它但 `enabled=false`，所有 `/store/lenovo/*` 路由返回 503 `{"detail":"联想应用商店源未启用。"}`；360 源未启用（`soft360.enabled=false`）时同理，`/store/360/*` 返回 503 `{"detail":"360 软件源未启用。"}`。

内置精选源的启用范围：

- `builtin.app_ids` 为空表示暴露目录里的全部 45 款；提交的 `config.json` 默认只启用 `clashverge` 与 `telegram` 两款。
- 白名单只影响“对外暴露 + 抓取”，`data/apps.json` 与 `data/apps.d/*.json` 的数据条目全部保留、照常加载与校验（图标、跨文件重复 id 等），不会删除任何目录文件。
- 只为白名单内的软件创建下载 provider；启动预热、定时刷新和 `POST /api/v1/refresh` 也只抓这几款，其余 43 款的 GitHub/官网不会被请求。
- 白名单里出现目录中不存在的 app id 时后端启动失败，避免配置写错后静默少软件。
- `builtin.enabled=false` 时不创建内置 provider、不做启动预热、不启动定时刷新；`/api/v1/apps*`（列表、详情、版本、latest、指定版本、图标）和 `POST /api/v1/refresh` 返回 503 `{"detail":"内置精选源未启用。"}`；`GET /api/v1/health`、`/status`、`/cache` 与 `DELETE /cache` 保持可用（返回空统计），`/api/v1/sources` 仍会列出内置源但 `enabled=false`。
- 环境变量：`WINAPPSMANAGER_BUILTIN_ENABLED`（true/false 等）与 `WINAPPSMANAGER_BUILTIN_APP_IDS`（逗号分隔；显式空串表示不限制，变量未设置时保留配置文件值）。

联想源的行为要点：

- **请求路径只读缓存**：启动时只把 `<cache.directory>/lenovo/catalog.json` 预加载到内存（纯磁盘读取，不联网）；`GET /store/lenovo/apps`、`/categories`、详情、图标等请求直接复用内存快照（内存没有时读一次磁盘快照），**不会因为请求或快照陈旧去访问联想接口**。只有内存和磁盘都没有快照的冷启动（例如第一次运行、`DELETE /cache` 之后的首个请求）才会同步抓取一次并写入 `catalog.json`。
- **每 5 分钟由独立后台线程全量刷新**：lifespan 启动联想服务时创建一个 daemon 线程（名称 `lenovo-catalog-refresher`，随服务启动即存在），线程只负责计时——每 `lenovo.catalog_refresh_interval_seconds`（默认 300 秒）通过 `asyncio.run_coroutine_threadsafe(service.refresh_catalog(), loop)` 把刷新协程投递回主事件循环执行并等待结果（阻塞 HTTP 只在主事件循环里跑，子线程不直接操作 async 客户端），成功后原子写回 `catalog.json` 并记 INFO（全部 N 款/耗时），失败只记 WARNING 并保留旧快照，线程不会崩。**首次刷新策略**：启动时已有快照且年龄 ≥ interval → 立即补刷一次（避免启动后 5 分钟内还是旧数据）；快照新鲜 → 等到剩余时间；**完全没有快照 → 等待完整 interval，不立即联网**（首个请求按冷启动兜底抓取，离线测试不联网）。`0` 关闭线程，`lenovo.enabled=false` 时不创建服务。关闭服务时 set Event + join（join 用 `asyncio.to_thread`，不阻塞事件循环），确保线程退出。
- 目录 `stale` 只由 `lenovo.catalog_ttl_seconds` 与快照实际年龄决定（默认 6 小时；超过即 `stale=true`，客户端可提示“目录偏旧”），不触发刷新。
- **GET 只读缓存**：联想所有 GET 路由都**不接受** `refresh` 查询参数，旧的 `GET /store/lenovo/apps?refresh=true` 会被当作未知查询参数忽略，**永远不会**触发目录抓取或后台刷新，也不会改写 `catalog.json`；详情 GET 同样不会强制刷新。手动立即刷新只有 `POST /store/lenovo/refresh`（同步抓取、写盘，返回 `refreshed`/`total`/`updated_at`/`duration_seconds`）。并发/重叠的刷新调用由 `_refresh` 的锁 + `fetched_at` 判断合并成一轮；默认只监听 `127.0.0.1`，若把后端暴露到网络，请自行给 POST 写接口加访问控制。
- 目录与分类：按“软件/游戏”两组逐一抓取全部分类后按 `softID` 去重合并（实测 5111 款）。
- 详情：`GET /store/lenovo/apps/{soft_id}` 在目录 summary 上追加详细描述、更新说明、截图/视频缩略图、封面、标签、榜单与安全标签；长期缓存不保存带 `auth_key` 的视频地址。**截图与横版封面优先返回联想 CDN 绝对 https 地址**（`screenshots[].url`、`cover_url`，客户端直连取图以降低后端图片带宽），同时给出后端代理路径 `screenshots[].proxy_url` / `cover_proxy_url` 作为兜底；CDN 地址不是 https 或 host 不在联想 CDN 白名单（`*.lenovo.com.cn` / `*.lenovomm.cn` / `*.gtimg.com`）时，`url` / `cover_url` 回退为 `/api/v1/store/lenovo/...` 相对代理路径。视频截图的 `url` 是 `videoImg` 缩略图（同样 CDN 优先），视频播放仍走 `.../screenshots/{index}/video` 代理；旧详情缓存文件不需要新字段，响应时按缓存里的原图地址重新计算。
- 搜索：`GET /store/lenovo/search?q=QQ&limit=30` 原样返回联想搜索顺序（含推广，客户端自行过滤），`limit` 取 1–50。
- 下载地址：`GET /store/lenovo/apps/{soft_id}/download?architecture=x64` 每次实时解析，返回与内置精选相同的 `Release` 结构（`id`/`version`/`architecture`/`download_url`/`file_name`/`size_bytes`/`sha256`），响应 `Cache-Control: no-store`，地址从不缓存。
- 图片：`/icon`、`/screenshots/{index}`、`/cover` 由后端代理并在磁盘缓存，响应 `Cache-Control: public, max-age=86400` 与 `ETag`，`If-None-Match` 命中返回 304；图片 host 只允许联想/CDN 白名单，避免 SSRF。列表/搜索图标的 `icon_url` 与详情截图/封面的 `url` / `cover_url` 优先是 CDN 绝对 https 地址（客户端直连），后端代理路由保留不变，作为 `icon_proxy_url` / `proxy_url` / `cover_proxy_url` 的兜底。
- 视频：`GET /store/lenovo/apps/{soft_id}/screenshots/{index}/video` 每次**实时**解析 `captureFileList[index].type=="1"` 的临时地址（`auth_key` 有时效，既不读详情缓存也不写任何缓存），再由后端用 `httpx2` 按块流式转发：透传客户端 `Range`，返回上游 `200/206/416` 状态与 `Content-Type`/`Content-Length`/`Content-Range`/`Accept-Ranges`，响应固定 `Cache-Control: no-store`，不整体读入内存、不落盘；上游 `401/403/410`（auth_key 过期）会重新解析一次地址并重试一次，其它上游错误 502、超时 504（连接 5 秒 / 读取 60 秒）、越界或非视频 404、源未启用 503，视频地址和 host 只允许联想视频 CDN 白名单（`pc-store-video.lenovomm.cn` 及 `*.lenovomm.cn`）。`HEAD` 返回相同的状态与响应头，但不下载视频体。日志只含 soft_id/index/状态码/字节数/耗时，**绝不记录**视频 URL 或 `auth_key`；视频 URL 不写入内存/磁盘缓存、不写日志。
- 错误映射：联想上游协议/网络错误 502、超时 504（沿用全局 `ProviderError` 处理器）、应用不存在或已下架 404；源未启用 503。目录/分类/详情/搜索/下载地址/刷新/视频代理响应都是 `Cache-Control: no-store`。
- 联想缓存位于 `<cache.directory>/lenovo/`（默认 `Backend/.cache/lenovo/`）：`catalog.json` 目录快照、`details/` 详情、`media/` 图片（默认 TTL 30 天、上限 256 MB 按最旧淘汰）、`token.json` 设备指纹与 token。`DELETE /api/v1/cache` 会追加清理联想目录/详情/图片/搜索缓存并在响应 `lenovo` 段报告结果，但**不清理 token**（避免频繁重新注册）；清理后下一个请求走冷启动同步抓取一次并重新写盘。
- `GET /api/v1/cache` 的 `lenovo` 段返回启用状态、已缓存数量/时间，以及 `catalog` 状态对象（见下文“缓存”一节），不会触发抓取。
- 联想目录允许 7 天陈旧快照兜底（超过 `catalog_max_stale_seconds` 的磁盘快照视为损坏并删除）；抓取并发 1–6，单次请求总超时 20 秒（连接超时 5 秒）。配置见上一节 `lenovo.*`。

联想的原始接口、设备 token、分类列表分页、搜索、详情、下载地址与图片 CDN 的调研结论见 [`docs/lenovo-store-api.md`](../docs/lenovo-store-api.md)。

360 软件源（`Backend/soft360/`）的行为要点：

- **目录/详情是明文 JSON，搜索是加密签名接口**。目录：`GET https://cdn-mgr.soft.360.cn/main/cdn_client/v1/soft/list?cate_id={id}&page_size=100&src=web_client&machine=&page={n}`，包络 `{"errno":0,"data":[...]}`，`data` 空数组表示没有更多页，`page_size` 最大 100；详情：`GET https://mgr.soft.360.cn/main/cdn_client/v1/soft/getinfo?scene=1&machine=&soft_id={id1,id2,...}`，`data` 是 `{soft_id: {...}}`，单次最多 50 个 ID；搜索：`POST https://mgr.soft.360.cn/main/enc_client/v2/search/search`（AES-128-CBC + RSA PKCS#1 v1.5 + MD5 签名，纯 Python 标准库实现，见下一节搜索说明与 [`docs/soft360-search-api.md`](../docs/soft360-search-api.md)）。图片 CDN 为 `qhimg.com` / `mediav.com` / `qhres2.com` 子域，http/https 均可访问。
- **24 个固定分类**（接口不提供分类树）：聊天工具(149)、视频软件(150)、浏览器(157)、办公软件(159)、输入法(156)、音乐软件(151)、游戏娱乐(153)、网络游戏(100000409)、手游电脑版(1900000002)、下载工具(152)、安全杀毒(155)、图形图像(154)、系统工具(162)、手机数码(165)、股票网银(158)、压缩刻录(161)、阅读翻译(100000186)、网络应用(168)、主题壁纸(100000376)、教育学习(167)、编程开发(163)、其它软件(169)、XP专区(1900000001)、win7专区(1900000010)。同一软件出现在多个分类时，按上述顺序的第一个分类作为归属分类。
- **目录是“热门子集”而不是全量**：360 目录总量约 43731 款，全量抓取不现实。默认每分类抓前 3 页（100 条/页）≈ 7200 条原始记录，按 `soft_id` 去重后 **5940 款**（与联想源 5111 款规模相当）；实测一轮抓取（24 分类 × 至多 3 页、并发 4）约 **7.1 秒**。`soft360.catalog_pages_per_category` 可调 1–20，调大后请求数与耗时近似线性增长。客户端与本文档都把这批数据表述为“各分类热门条目子集”。
- **目录请求路径只读缓存，搜索例外**：启动时 `preload` 只把 `<cache.directory>/soft360/catalog.json` 读进内存（不联网）；`GET /store/360/apps` 与 `/categories` 只读内存/磁盘快照，**任何查询参数（包括 `?refresh=true`）都不会触发目录抓取**；内存和磁盘都没有快照时返回 `total=0`、`stale=true` 的空目录，而不是同步抓取。`GET /store/360/search` 是唯一会访问 360 的 GET：它先调用加密搜索接口（覆盖全量目录），远程失败/空结果才回退到缓存目录做本地匹配。目录更新只由独立 daemon 线程 `soft360-catalog-refresher`（默认每 `soft360.catalog_refresh_interval_seconds`=1800 秒一轮，线程把刷新协程投递回主事件循环执行并原子写回 `catalog.json`，失败保留旧快照）与 `POST /store/360/refresh` 完成。首次刷新策略与联想源一致：已有陈旧快照 → 启动后立即补刷；快照新鲜 → 等到剩余间隔；**完全没有快照 → 等满一个间隔，启动阶段不联网**。
- **详情**：`GET /store/360/apps/{soft_id}` 实时请求 `getinfo` 并缓存（内存 15 分钟 / 磁盘 6 小时，失败可回退陈旧详情），把 `edition_info.default_edition` 的 `version`、`os.bit`（32→`x86`、64→`x64`）与 `os.ver`（映射最低 Windows 版本，低于 7 的一律按 7 处理）、`logo`（统一转 https）以及 `snapshot[]` 截图映射成模型；`soft360-{soft_id}` 形式的主键也可直接访问。**详情截图优先返回 360 图片 CDN 的绝对 https 地址**（`screenshots[].url`，客户端直连取图以降低后端图片带宽），同时给出 `screenshots[].proxy_url` 后端代理路径作为兜底；CDN 地址不是 https 或 host 不在 360 图片 CDN 白名单（`*.qhimg.com` / `*.mediav.com` / `*.qhres2.com` / `*.qhres.com`）时 `url` 回退为 `/api/v1/store/360/...` 相对代理路径。**目录外软件（搜索结果里出现、但不在热门子集快照里）不再 404**：`catalog` 找不到时直接用实时 `getinfo` 字段现场合成 summary 再构建详情；确实不存在（`errno`/空 `data`）仍然 404。详情缓存只保存展示字段，**不保存下载地址**（旧版缓存文件缺少 `icon_url` 时按空值兼容读取；旧缓存里的截图原图地址同样在响应时重新计算直连/回退）。
- **下载**：`GET /store/360/apps/{soft_id}/download?architecture=x64` 每轮实时请求 `getinfo`（不读也不写详情缓存），从 `down.default.p2p`（`|||` 分隔，且 `pdown://` 描述串里也内嵌 `https://cdn-download.soft.360.cn/...exe` 兜底地址）以及 `multi_urls` / `multi_down` 里提取 http(s) 地址，优先 https，扩展名只允许 `.exe/.msi/.msix/.appx`；文件名用 `down.default.name` 清洗，缺失或不安全时回退 `{软件名}_{版本}.exe`；大小用 `down.default.size` 或 `soft_size`。`md5` 不是 sha256，因此 `Release.sha256` 恒为 `null`；响应 `Cache-Control: no-store`。**目录外软件同样可下载**：不需要目录 summary，名称/大小/架构直接取自本次 `getinfo` 响应。
- **搜索（已逆向加密接口，覆盖 360 全量目录）**：`GET /store/360/search?q=&limit=` 先调用 `POST https://mgr.soft.360.cn/main/enc_client/v2/search/search`：query 参数是 `ver/os_ver/os_bit/mid/m2/timestamp/sk_type/sk_bit/rand/sign`，请求体 `{"q","skey"}`——`q` 是固定 AES-128-CBC key/iv（PKCS#7）加密的搜索 JSON，`skey` 是用 360 RSA 公钥按 PKCS#1 v1.5 加密的 `{"key","iv"}`，`sign` 是“排序、过滤空值后的基础参数 + 原始 skey + q + 固定 salt”的 MD5；响应里的 `skey` 用同一公钥做模幂还原 `{"key","iv"}`，再 AES-CBC 解 `data` 得到 `{"list":[...]}`。`mid`/`m2` 每次随机生成（空值会被服务端以 `errno=10009` 拒绝），接口每次最多返回 50 条，`limit` 取 1–50（默认 30）在本地截断并按 `soft_id` 去重。**远程失败（超时/HTTP/`errno`/解密异常）只记 WARNING 并回退缓存目录本地匹配，远程无结果或条目全部无法解析时也回退，搜索永远不会因为上游问题变成 5xx**。AES/RSA/MD5 全部用 Python 标准库实现（`Backend/soft360/search.py`），**没有新增第三方依赖**；搜索结果条目可能是目录外软件，详情/下载/图标/截图都支持。
- **图片**：`/icon`、`/screenshots/{index}` 由后端代理并在磁盘缓存（30 天、上限 256 MB，按最旧淘汰），http 统一转 https，响应 `Cache-Control: public, max-age=86400` 与 `ETag`，`If-None-Match` 命中返回 304；图片 host 只允许 `*.qhimg.com` / `*.mediav.com` / `*.qhres2.com` / `*.qhres.com` 白名单，避免 SSRF。目录/搜索图标的 `icon_url` 与详情截图的 `url` 优先是 CDN 绝对 https 地址（客户端直连），后端代理路由保留不变，作为 `icon_proxy_url` / `proxy_url` 的兜底。
- **错误映射**：目录/详情/下载/图片的上游协议/网络错误 502、超时 504（沿用全局 `ProviderError` 处理器）、软件不存在或已下架 404、源未启用 503；搜索失败**不返回 5xx**（回退缓存目录本地匹配，最差返回空列表，只记 WARNING）；目录/分类/详情/搜索/下载地址/刷新响应都是 `Cache-Control: no-store`。
- 360 缓存位于 `<cache.directory>/soft360/`（默认 `Backend/.cache/soft360/`）：`catalog.json` 目录快照、`details/` 详情、`media/` 图片。`DELETE /api/v1/cache` 会追加清理并在响应 `soft360` 段报告结果；清理后 **不会**在请求路径重新抓取，等后台线程或 `POST /store/360/refresh` 重新填充。`GET /api/v1/cache` 的 `soft360` 段返回启用状态、已缓存数量/时间与只读的 `catalog` 状态对象。
- 配置见上一节 `soft360.*`；全部测试离线（`httpx2.MockTransport` + 临时目录），不访问真实 360 接口。

## 启动预热与定时刷新

后端在 lifespan 中先加载并校验软件目录（先读 `data/apps.json`，再按文件名顺序合并同目录 `apps.d/*.json`），创建 HTTP 客户端、provider 注册表、持久缓存和 `ReleaseService`，再对每个由 provider 管理版本的内置精选软件执行一次 `get_releases(force_refresh=True)`，完成后才开始接受请求；定时刷新任务在同一阶段启动。内置精选目录共 45 款，但默认 `builtin.app_ids` 只启用 `clashverge` 与 `telegram`，因此启动预热和定时刷新只抓这两款；`builtin.enabled=false` 时跳过预热与定时刷新且不创建内置 provider。关闭时先停定时刷新，再关闭缓存并等待在途读取。

- 预热结果分三类：抓取成功记 `ok`；抓取失败但磁盘上有快照（包括已过期但未超过 `cache.max_stale_seconds` 的快照）时回退到该快照并记 `cached`；两者都没有记 `unavailable`。
- `refresh.drop_unavailable_apps` 为 `true`（默认）时，`unavailable` 的软件不再出现在 `GET /api/v1/apps` 列表里，`GET /api/v1/apps/{app_id}` 返回 503。
- 预热总预算由 `refresh.startup_timeout_seconds` 控制（默认 30 秒）。预算耗尽或预热自身异常时服务照常启动（记 warning 日志），未完成的软件退化为按需抓取。
- 完整目录的 45 个来源在默认并发 4 下整轮预热实测十余秒；默认 `builtin.app_ids` 只启用 2 款，预热基本瞬时完成。网络较慢或 GitHub API 配额紧张时可能用满 30 秒预算，此时按上一条规则先启动服务。
- GitHub 来源启用条件缓存后，强制刷新会先在 `github.refresh_interval_seconds`（默认 900 秒）内复用磁盘缓存：模拟 34 个 GitHub 来源时冷启动各请求一次，900 秒内重启 0 次请求，超过 900 秒重发 34 次条件请求且全部返回 304（不消耗配额），304 后再次重启又回到 0 次请求。
- 单次抓取受 `providers.timeout_seconds` 限制，同时进行的软件抓取数受 `refresh.max_concurrency` 限制。
- 定时刷新由 `PeriodicRefresher` 执行：按 `refresh.interval_seconds`（默认 1800 秒）加可选抖动重跑一次全量抓取，成功结果写入持久化缓存。单轮失败只记录日志与统计，不影响后续轮次；同一时间只跑一轮，重复触发会合并。`interval_seconds=0` 关闭定时刷新。
- 联想应用商店目录的刷新是**另一套机制**：由独立 daemon 线程 `lenovo-catalog-refresher`（lifespan 创建联想服务时启动，`lenovo.catalog_refresh_interval_seconds`，默认 300 秒）通过 `asyncio.run_coroutine_threadsafe` 把刷新协程投递回主事件循环执行，`POST /store/lenovo/refresh` 仍可手动同步刷新；详见“软件源（内置精选 / 联想应用商店 / 360 软件管家）”一节。
- 360 软件源目录的刷新与联想源同构但间隔不同：独立 daemon 线程 `soft360-catalog-refresher`（lifespan 创建 360 服务时启动，`soft360.catalog_refresh_interval_seconds`，默认 **1800 秒**）定时刷新 24 个分类的「前 N 页」并原子写回 `soft360/catalog.json`；默认每分类 3 页、一轮约 7 秒，`POST /store/360/refresh` 可手动同步刷新。首次刷新与无快照启动策略同联想源（启动阶段不联网）。
- 非强制刷新的列表与详情读取在 provider 暂时不可用时回退到持久化缓存中的旧快照（`refresh.use_stale_cache`）；显式 `refresh=true` 和 `/releases/latest` 仍然强制访问官网，详情与 `/releases/latest` 失败时保持 502/504。列表是唯一例外：某个软件强制刷新失败时会再按非强制刷新重试一次，避免它从列表里凭空消失。

`GET /api/v1/status` 返回诊断信息，`POST /api/v1/refresh` 可立即触发一轮刷新（并发调用合并为一轮），两者的响应结构见下节。

## 接口

所有资源接口使用 `/api/v1` 前缀：

| 方法 | 路径 | 返回内容 |
| --- | --- | --- |
| GET | `/health` | `{"status":"ok"}` |
| GET | `/sources` | 软件源列表：联想应用商店（默认）、内置精选与 360软件管家的启用状态、在线搜索能力（`online_search`：联想/360 为 `true`、内置精选为 `false`）、软件数量与更新时间（内置精选数量受 `builtin.app_ids` 过滤） |
| GET | `/status` | 诊断信息：每个软件的预热/可用性状态与定时刷新统计 |
| GET | `/cache` | 内存/磁盘缓存统计、预热与定时刷新状态、`lenovo`/`soft360` 目录缓存状态、日志文件统计和本次启动生效的配置 |
| DELETE | `/cache` | 清理内存和磁盘版本缓存，并追加清理联想与 360 源的目录/详情/图片缓存 |
| POST | `/refresh` | 立即触发一轮全量刷新；并发调用合并为一轮 |
| GET | `/apps` | 软件列表 `{"items":[...],"total":N}`（默认白名单下 2 款），逐软件隔离失败，并按 `architecture` 过滤声明的架构 |
| GET | `/apps/{app_id}` | 一款软件的完整资料；无缓存兜底时返回 503 |
| GET | `/apps/{app_id}/releases` | 软件版本集合，默认返回该软件全部可用架构 |
| GET | `/apps/{app_id}/releases/latest` | 当前架构的最新版本，强制刷新并重新解析下载地址（QQ 重新签名） |
| GET | `/apps/{app_id}/releases/{release_id}` | 指定版本资源，重新解析下载地址 |
| GET | `/apps/{app_id}/icon` | 图标文件；PNG 返回 `image/png`，原有的 .ico 返回对应图片类型 |
| GET | `/store/lenovo/categories` | 联想商店分类树（`source`/`updated_at`/`groups`），`Cache-Control: no-store` |
| GET | `/store/lenovo/apps` | 联想商店完整目录 `{"source","source_name","updated_at","stale","total","items"}`；**只读内存/磁盘 `catalog.json`**，不接受 `refresh` 参数（未知查询参数被忽略）；手动刷新只用 `POST /store/lenovo/refresh` |
| GET | `/store/lenovo/apps/{soft_id}` | 联想应用详情（在目录 summary 上追加描述、截图、封面、标签、榜单等）；截图/封面优先联想 CDN 绝对 https 地址（`screenshots[].url` / `cover_url`），并给出代理兜底路径 `screenshots[].proxy_url` / `cover_proxy_url`；只读服务端缓存，不接受 `refresh` |
| GET | `/store/lenovo/apps/{soft_id}/download` | 实时解析联想下载地址，返回 `Release`，`architecture` 可选（默认 `x64`），`Cache-Control: no-store` |
| GET | `/store/lenovo/search` | 联想在线搜索，`q` 1–100 字符、`limit` 1–50（默认 30） |
| GET | `/store/lenovo/apps/{soft_id}/icon` | 联想图标代理，带 `ETag` 与长 TTL，支持 304 |
| GET | `/store/lenovo/apps/{soft_id}/screenshots/{index}` | 联想截图/视频缩略图代理（`index` ≥ 0，详情 `screenshots[].proxy_url` 的兜底），带 `ETag`，支持 304 |
| GET / HEAD | `/store/lenovo/apps/{soft_id}/screenshots/{index}/video` | 联想视频流代理：实时解析临时地址（不缓存）并按块转发，透传 `Range`、返回上游 200/206/416 与内容头，`Cache-Control: no-store`；HEAD 只返回响应头；403 等过期状态会重新解析一次并重试一次 |
| GET | `/store/lenovo/apps/{soft_id}/cover` | 联想横版封面代理（详情 `cover_proxy_url` 的兜底），带 `ETag`，支持 304 |
| POST | `/store/lenovo/refresh` | 同步强制刷新联想目录（等待抓取完成并写 `catalog.json`），并发调用合并为一轮，返回 `refreshed`/`total`/`updated_at`/`duration_seconds` |
| GET | `/store/360/categories` | 360 软件管家 24 个分类与当前快照内的条目数（`source`/`updated_at`/`total`/`categories`），只读缓存，`Cache-Control: no-store` |
| GET | `/store/360/apps` | 360 目录 `{"source","source_name","updated_at","stale","total","items"}`（默认约 5940 款热门子集）；**只读内存/磁盘 `catalog.json`**，绝不访问 360，任何查询参数都不会触发抓取；手动刷新只用 `POST /store/360/refresh` |
| GET | `/store/360/apps/{soft_id}` | 360 软件详情（版本、发布者、长描述、截图；截图优先 360 图片 CDN 绝对 https 地址 `screenshots[].url`，并给出代理兜底 `screenshots[].proxy_url`；`soft360-` 前缀主键也接受；**目录外软件也可访问**，实时 `getinfo` 合成 summary，确实不存在才 404），详情缓存 15 分钟内存 / 6 小时磁盘 |
| GET | `/store/360/apps/{soft_id}/download` | 实时解析 360 下载地址，返回 `Release`，`architecture` 可选（默认 `x64`，按 `os.bit` 回退），`sha256` 恒为 `null`，`Cache-Control: no-store`；目录外软件同样支持 |
| GET | `/store/360/search` | **全量在线搜索**：调用逆向的加密 `enc_client` 接口（AES-128-CBC + RSA + MD5），覆盖 360 全量目录；远程失败/空结果回退缓存目录本地匹配（不返回 5xx）；`q` 1–100 字符、`limit` 1–50（默认 30） |
| GET | `/store/360/apps/{soft_id}/icon` | 360 图标代理（图片 CDN 白名单 + 磁盘缓存），带 `ETag` 与长 TTL，支持 304 |
| GET | `/store/360/apps/{soft_id}/screenshots/{index}` | 360 截图代理（`index` ≥ 0，详情 `screenshots[].proxy_url` 的兜底），带 `ETag`，支持 304 |
| POST | `/store/360/refresh` | 同步强制刷新 360 目录（等待 24 分类抓取完成并写 `soft360/catalog.json`），返回 `refreshed`/`total`/`categories`/`updated_at`/`duration_seconds` |

`builtin.enabled=false` 时 `/api/v1/apps*` 全部返回 503 `{"detail":"内置精选源未启用。"}`，`POST /api/v1/refresh` 同样返回 503；`GET /health`、`/status`、`/cache` 与 `DELETE /cache` 仍正常（返回空统计）。内置精选默认只启用 `clashverge` 与 `telegram`，列表、搜索、分类、详情和版本接口都只涉及这两款，`/sources` 的 builtin `app_count` 为过滤后的数量（默认 2）。

`GET /apps` 支持以下查询参数：

- `search`：匹配名称、简介、完整说明、分类、发布者、功能和标签，忽略英文大小写及首尾空白；最多 200 个字符。
- `category`：精确匹配分类，忽略首尾空白；最多 100 个字符。未知分类返回空列表。
- `sort`：`recommended`（默认，推荐软件优先，再按目录顺序）或 `name`（按名称排序）。其他值返回 422。
- `architecture`：`x64`（默认）、`x86` 或 `arm64`；列表按目录声明的 `architectures` 过滤（未声明视为兼容所有架构，兼容旧条目），详情与 latest 只按它选择安装包、不隐藏软件。
- `refresh`：软件列表、详情和版本集合可传 `true`，绕过版本缓存获取新资料；强制刷新失败时不会回退到旧快照（列表接口例外：失败的软件会再按非强制刷新回退一次）。latest 资源始终刷新。

版本集合 `/apps/{app_id}/releases` 默认返回全部可用架构，也可用 `architecture` 筛选。不支持的架构值返回 422，某个软件没有对应架构时，集合为空，latest 资源返回 404。`latest` 是当前版本的资源别名，响应中的 `id` 仍是实际版本 ID，例如 `9.9.37-windows-x64`。

`total` 表示本次实际返回的条目数：列表逐个软件解析，单个软件抓取失败只会把它从本次响应中略过（记 warning 日志），接口整体仍返回 200；非强制刷新的列表与详情在 provider 暂时不可用时会回退到持久化缓存中的旧快照，带 `refresh=true` 时先强制刷新，失败的软件再按非强制刷新回退一次（有旧快照就不会被略过）；详情与 `/releases/latest` 的强制刷新失败仍会报错（502/504）。预热阶段既抓取失败又没有可用快照的软件不会出现在列表里（`refresh.drop_unavailable_apps` 默认开启），其详情返回 503。缺失的软件、版本或未配置的图标返回 404，查询参数不合法返回 422。上游 HTTP 或数据协议异常返回 502，超时返回 504。旧图标地址 `/icon/qq.png` 保留兼容。联想商店使用独立前缀 `/api/v1/store/lenovo`，360 软件管家使用 `/api/v1/store/360`，源未启用时返回 503，其接口与错误映射见“软件源（内置精选 / 联想应用商店 / 360 软件管家）”一节；`/sources`、联想与 360 的目录/分类/详情/搜索/下载/刷新（以及联想视频代理）响应都是 `Cache-Control: no-store`，只有图片使用长 TTL + ETag。

`GET /api/v1/status` 的 `warmup` 段列出每个软件的抓取状态（`ok`/`cached`/`unavailable`）、错误信息和检查时间，`refresh` 段给出定时刷新统计（`runs`、`failures`、`last_run_at`、`last_error` 等）。`POST /api/v1/refresh` 立即跑一轮全量刷新；已有一轮在运行时本次调用不排队，返回 `{"started":false,"reason":...}`，否则返回 `{"started":true,...}` 和本轮汇总。

列表与详情采用相同的 snake_case 字段：

```text
id, name, description, long_description, category, publisher,
version, size_display, license, platforms, website_url,
download_page_url, icon_url, icon_text, icon_background,
installation_notes, features, tags, is_recommended, sort_order,
minimum_windows, architectures, latest_release
```

`features` 和 `tags` 为字符串数组。`icon_url` 根据当前请求地址生成绝对资源 URL；未配置图标的软件返回 `null`，前端可以使用 `icon_text` 和 `icon_background` 显示徽章。`latest_release` 是版本对象或 `null`，`download_page_url` 始终保留官网下载页。`icon_file`、`download_provider`、`latest_release_id`、`releases` 和 `provider_spec` 只用于后端存储，不会出现在任何响应中。

`minimum_windows` 与 `architectures` 是可选的机器可读兼容字段：前者取 `7`/`8`/`8.1`/`10`/`11`，后者是至少 1 项、不可重复的 `x64`/`x86`/`arm64` 数组；未声明时 `/apps` 不按架构过滤。`platforms` 仍是给人看的展示文案。

## QQ 下载插件

`QQReleaseProvider` 分两步提供版本和下载信息：

1. GET `https://qq-web.cdn-go.cn/im.qq.com_new/latest/rainbow/pcConfig.json`，从 `Windows.version` 取得版本号，再按架构读取对应的 NT 安装包字段。
2. POST `https://im.qq.com/http2rpc/gotrpc/noauth/trpc.qqntv2.urlsign.UrlSign/GetSign`，请求 JSON 为 `{"url":"安装包地址"}`，请求头 `x-oidb` 为 `{"uint32_command":"0x9b8e","uint32_service_type":1}`。成功后使用 `data.url` 中带签名的地址。

| 架构 | 官方配置字段 |
| --- | --- |
| x64 | `Windows.ntDownloadX64Url` |
| x86 | `Windows.ntDownloadUrl` |
| arm64 | `Windows.ntDownloadARMUrl` |

`Windows.downloadUrl` 是旧版 QQ 的入口，NT 插件不会用它替代缺失的架构。缺失单个架构时仅返回其他可用架构；版本、配置或签名响应无效时返回明确错误。

版本服务按提供器统一缓存成功获取的未签名版本资料，默认内存 TTL 为 300 秒、磁盘快照 TTL 为 3600 秒，并合并同时进行的读取。磁盘快照让后端重启后继续复用上次成功获取的资料；两个 TTL、缓存目录和条目上限都可以在 `config.json` 中调整。生产插件的内部缓存默认 600 秒，用于版本服务命中期间的权威资源校验。缓存 miss、手动刷新和 `releases/latest` 都会强制更新官方资料；QQ 每次解析下载资源仍重新 POST 获取签名。签名 URL 不写入软件清单、内存缓存或磁盘缓存，含下载链接的 API 响应继续使用 `Cache-Control: no-store`。整个插件操作默认保留 10 秒期限。

如果官方版本恰好在读取资料与签名之间更新，版本服务会在同一请求期限内强制刷新并重新选择一次。latest 和版本集合返回新版本；指定的旧版本 ID 已不在官方配置中时返回 404。普通签名错误不触发这类重试。

客户端用 `RuntimeInformation.OSArchitecture` 选择操作系统架构，点击下载或复制链接时调用 latest 资源获得新签名。后端返回版本元数据、校验信息和下载地址，实际 EXE 传输由 WPF 客户端直连官方 CDN 完成。权威大小或 SHA256 未提供时，版本资源中的 `size_bytes` 和 `sha256` 保留 `null`。

客户端默认使用 4 个并发连接，用户可在设置中选择 1–32，配置用于之后开始的下载。服务器支持 HTTP Range 且能保证分段一致时，多个分段共用一个总进度条和一条分段带状进度条；界面另列出各段状态、总速度和预计剩余时间。小文件、未知长度、不支持 Range 或无法保证文件一致性时会回退到单连接，后端 API 无需为不同连接数创建下载任务。

客户端校验每个分段的范围和长度，在完整文件传输结束后按容器类型验证：`.exe` 检查 `MZ` 和 `PE` 头，`.msi` 检查 OLE 复合文档签名 `D0 CF 11 E0 A1 B1 1A E1`，`.msix` / `.msixbundle` / `.appx` 检查 ZIP 头 `50 4B 03 04`；官方提供 SHA256 时再检查完整文件哈希。全部检查通过后才保存为正式安装包，由用户手动打开。未知长度时继续显示已下载字节和速度，剩余时间区域显示“文件大小未知”，不估算剩余时间。线程数、进度和临时文件都由桌面客户端管理。

安装包允许的后缀是 `.exe` / `.msi` / `.msix` / `.msixbundle` / `.appx`；后端通用 GitHub provider 只产出前四种（GitHub 发布资产常见 EXE、MSI 和 MSIX Bundle），`.appx` 仅用于其他来源的客户端兼容。

使用示例：

```powershell
Invoke-RestMethod 'http://127.0.0.1:8080/api/v1/apps/qq/releases/latest?architecture=x64'
Invoke-RestMethod 'http://127.0.0.1:8080/api/v1/apps/qq/releases/latest?architecture=arm64'
```

## 已接入的软件（45 款）

下列 8 款由专用 provider 解析，来源结构已通过 Chrome DevTools MCP 检查；版本栏是 2026-11-09 的实际读取结果，程序会按官方发布更新：

| 软件 | 提供器 | 版本来源 | 读取版本 | Windows 安装器架构 |
| --- | --- | --- | --- | --- |
| QQ | `qq.py` | 官方 `pcConfig.json` 的 `Windows.version` 和签名 RPC | 9.9.37 | x64、x86、ARM64 |
| 微信 | `wechat.py` | [Windows 官网](https://pc.weixin.qq.com/) 的下载按钮、版本文字与链接 | x64 4.2.0；x86 3.9.12 兼容版 | x64、x86、ARM64 |
| VS Code | `vscode.py` | Microsoft `api/update/{platform}/stable/latest` 的 `productVersion`、`url`、`sha256hash` | 1.145.2 | x64、ARM64 |
| 7-Zip | `sevenzip.py` | [官方下载页](https://www.7-zip.org/download.html) 的最新稳定标题及对应 EXE 链接 | 26.06 | x64、x86、ARM64 |
| VLC | `vlc.py` | [官网](https://www.videolan.org/vlc/) 内联 `PLATFORMS` JSON 的 `latestVersion`、`location` | 3.0.24 | x64、x86、ARM64 |
| Firefox | `firefox.py` | Mozilla `firefox_versions.json` 的 `LATEST_FIREFOX_VERSION`、完整包重定向和官方 `SHA256SUMS` | 159.0.1 | x64、x86、ARM64 |
| OBS Studio | `obs.py` | 官网关联的 `obsproject/obs-studio` GitHub 最新正式发布与安装器资产 | 33.0.1 | x64、x86、ARM64 |
| Notepad++ | `notepadplusplus.py` | [官网](https://notepad-plus-plus.org/downloads/) 的 `Current Version`，再查询同版本 GitHub tag | 8.9.9 | x64、x86、ARM64 |

微信的 32 位兼容包保留自己的版本号，不使用 64 位版本号；VS Code 当前没有 32 位安装器。Firefox 使用简体中文完整稳定版，排除在线 stub、ESR、Beta 与 Nightly；7-Zip 和 Notepad++ 排除便携版、MSI、调试包与源码。各软件的 `architectures` 以 provider 实际能提供的安装包为准。

### 国内 AI 客户端来源（3 款）

WorkBuddy、DeepSeek Harness 和豆包不在 GitHub 发布，由 `providers/workbuddy.py`、`deepseek_harness.py`、`doubao.py` 三个专用 provider 解析；它们都复用 `_utility.py` 的 `UtilityReleaseProvider` 与 `official_executable_url`，只提供 Windows x64 安装包，条目位于 `data/apps.d/ai.json`（该文件共 10 条，`gpt4all` 仍在）。

| 软件 | `provider_spec` | 版本来源 | 安装包 | 校验与风险 |
| --- | --- | --- | --- | --- |
| WorkBuddy | 无（非 GitHub 来源） | `GET https://www.workbuddy.cn/v2/update?platform=workbuddy-win32-x64-user` 的 JSON：`version`、`url`、`sha256hash`；`win32-ia32` 和 `arm64` 平台返回 HTTP 400 | `download.codebuddy.cn` 上的完整安装包 | 官方提供 SHA256；大小来自可选的 HEAD 请求，CDN 拒绝 HEAD 时 `size_bytes` 为空 |
| DeepSeek Harness | 无（非 GitHub 来源） | `HEAD https://download.deepseek.com/desktop/dsh-latest-windows-x64.exe` 的 `Content-Disposition` 文件名（`deepseek-harness-<版本>-win-x64.exe`）；HEAD 被拒（405/501）时用 1 字节 Range 请求取头部 | `dsh-latest-windows-x64.exe` 最新别名，`file_name` 就是 URL 最后一段 | 官方不提供 SHA256；`download.deepseek.com` CDN 最长缓存约 24 小时，版本更新可能滞后一天 |
| 豆包 | 无（非 GitHub 来源） | `POST https://99uri.com/ug/pc/landpage_click`（JSON body 含官网前端硬编码的 `surl_token`）返回 `download_url`，从其文件名取构建号（如 `2035012`） | UG 渠道 CDN 上的在线安装器（约 7.4 MB，运行后联网下载完整程序） | 无 SHA256；token 随官网前端改版可能失效，失效时抛 `ProviderUnavailableError`，软件按降级规则暂时隐藏或回退旧快照；版本号是安装器构建号，不是产品语义版本 |

### GitHub Releases 通用来源（34 款）

其余 34 款不写专用插件，统一由 `providers/github.py` 的 `GithubReleaseProvider` 读取仓库的 `/releases/latest`；每个软件的 `provider_spec`（仓库、tag 前缀、资产正则）记录在 `data/apps.d/devtools.json`、`ai.json`、`chat.json`、`utils.json`，完整规则和资产正则见 `data/apps.d/README.md`。所有条目 `download_provider` 等于 app id，安装包格式由资产决定：大部分是 EXE，PowerShell 7 是 MSIX Bundle，Neovim、GitHub CLI、Mattermost、Zulip、KeePassXC 是 MSI。

GitHub 响应条件缓存（`providers/_github_cache.py`）：`cache.enabled` 与 `cache.persist` 都开启时，每个仓库的上次响应（响应体 + `ETag` + `Last-Modified` + 获取时间）保存为 `<cache.directory>/github/<sha256(repo)[:32]>.json`，原子写、损坏或 schema 不符即删除，**绝不写入令牌**。`get_releases()` 先读磁盘缓存：非强制读取直接复用；启动预热、定时刷新和 `refresh=true` 这类强制读取在 `github.refresh_interval_seconds`（默认 900 秒）内也直接复用、不发请求；需要联网时总是带 `If-None-Match` / `If-Modified-Since`，`200` 写缓存、`304` 复用旧响应并刷新获取时间。缓存关闭时行为不变，每次都请求 GitHub。

| 软件 | 分类 | GitHub 仓库 | Windows 架构 | 安装包格式 |
| --- | --- | --- | --- | --- |
| Git for Windows | 开发工具 | `git-for-windows/git` | x64、ARM64 | EXE |
| PowerShell 7 | 开发工具 | `PowerShell/PowerShell` | x64、x86、ARM64 | MSIX Bundle |
| Neovim | 开发工具 | `neovim/neovim` | x64、ARM64 | MSI |
| VSCodium | 开发工具 | `VSCodium/vscodium` | x64、ARM64 | EXE |
| DBeaver Community | 开发工具 | `dbeaver/dbeaver` | x64、ARM64 | EXE |
| GitHub Desktop | 开发工具 | `desktop/desktop` | x64 | EXE |
| Zed | 开发工具 | `zed-industries/zed` | x64、ARM64 | EXE |
| Bruno | 开发工具 | `usebruno/bruno` | x64、ARM64 | EXE |
| GitHub CLI | 开发工具 | `cli/cli` | x64、x86、ARM64 | MSI |
| Ollama | AI 助手 | `ollama/ollama` | x64 | EXE |
| Jan | AI 助手 | `janhq/jan` | x64、ARM64 | EXE |
| Cherry Studio | AI 助手 | `CherryHQ/cherry-studio` | x64、ARM64 | EXE |
| AnythingLLM | AI 助手 | `Mintplex-Labs/anything-llm` | x64、ARM64 | EXE |
| GPT4All | AI 助手 | `nomic-ai/gpt4all` | x64、ARM64 | EXE |
| LobeHub | AI 助手 | `lobehub/lobehub` | x64 | EXE |
| Goose | AI 助手 | `aaif-goose/goose` | x64 | EXE |
| Telegram Desktop | 社交通讯 | `telegramdesktop/tdesktop` | x64、x86、ARM64 | EXE |
| Jitsi Meet | 社交通讯 | `jitsi/jitsi-meet-electron` | x64 | EXE |
| Mattermost | 社交通讯 | `mattermost/desktop` | x64、ARM64 | MSI |
| Zulip | 社交通讯 | `zulip/zulip-desktop` | x64 | MSI |
| Rocket.Chat | 社交通讯 | `RocketChat/Rocket.Chat.Electron` | x64、x86、ARM64 | EXE |
| Ferdium | 社交通讯 | `ferdium/ferdium-app` | x64、x86、ARM64 | EXE |
| Revolt | 社交通讯 | `revoltchat/desktop` | x64 | EXE |
| Session | 社交通讯 | `oxen-io/session-desktop` | x64 | EXE |
| Microsoft PowerToys | 系统工具 | `microsoft/PowerToys` | x64、ARM64 | EXE |
| ShareX | 实用工具 | `ShareX/ShareX` | x64 | EXE |
| Rufus | 实用工具 | `pbatard/rufus` | x64、x86、ARM64 | EXE |
| KeePassXC | 网络与安全 | `keepassxreboot/keepassxc` | x64、ARM64 | MSI |
| mpv.net | 影音工具 | `mpvnet-player/mpv.net` | x64 | EXE |
| Clash Verge Rev | 网络与安全 | `clash-verge-rev/clash-verge-rev` | x64、ARM64 | EXE |
| LocalSend | 实用工具 | `localsend/localsend` | x64 | EXE |
| QuickLook | 系统工具 | `QL-Win/QuickLook` | x64 | EXE |
| Motrix | 实用工具 | `agalwood/Motrix` | x64、ARM64 | EXE |
| Obsidian | 办公效率 | `obsidianmd/obsidian-releases` | x64 | EXE |

分类统计（45 款）：开发工具 11、社交通讯 10、AI 助手 10、实用工具 5、影音工具 3、系统工具 2、网络与安全 2、办公效率 1、网络浏览 1。

### 适用系统与架构（45 款）

45 款目录条目全部声明了 `minimum_windows` 与 `architectures`，核实依据是各软件官方系统要求与 provider 实际能提供的安装包（本地核实记录在已 gitignore 的 `artifacts/platform-matrix/`）。按 `minimum_windows`：Windows 7 4 款（微信、7-Zip、Telegram Desktop、VLC media player）、Windows 8 2 款（Neovim、Rufus）、Windows 8.1 1 款（Notepad++）、Windows 10 37 款、Windows 11 1 款（Mattermost）。按 `architectures`：x64 45 款；x86 13 款（QQ、微信、7-Zip、Telegram Desktop、VLC media player、Firefox、OBS Studio、Notepad++、PowerShell 7、GitHub CLI、Rocket.Chat、Ferdium、Rufus）；arm64 29 款。

`platforms` 统一写成「系统版本 · 架构」，例如 `Windows 7 / 8 / 8.1 / 10 / 11 · x64 / x86 / arm64`、`Windows 10（22H2 及以上）/ 11 · x64`、`Windows 11 · x64 / arm64`。`GET /api/v1/apps?architecture=` 当前实测：`x64` 45 款、`x86` 13 款、`arm64` 29 款；架构以 provider 实际能提供的安装包为准，不是官网宣传的最大范围。

Microsoft 与 GitHub 提供的有效 SHA256、精确文件大小会写入版本资源；GitHub 资产带 `digest`（`sha256:...`）时映射为版本资源的 `sha256`，没有时保留 `null`，资产 `size` 始终映射为 `size_bytes`。Firefox 使用官方校验清单。只有四舍五入的网页大小时不生成精确字节值，未提供权威校验值时保留 `null`。

提供器通过工厂注入 HTTP 客户端。当前环境中普通代理连接在部分官网发生间歇 TLS EOF，而 Notepad++ 官网需要环境代理；工厂为 Notepad++ 保留环境代理客户端，为其他专用来源注入直连客户端，通用 GitHub provider 使用普通客户端（`client`）访问 API 和下载资产。内置专用 provider 共 11 个（原有 8 个 + 3 个国内 AI 客户端），其余 34 个 GitHub provider 在传入 catalog 时按 `provider_spec` 动态注册。两种客户端均开启默认 TLS 验证，生命周期由 FastAPI 管理。部署到其他网络时可在 `create_release_providers` 的客户端分配中调整。

## 缓存与固定图标

`cache.py` 的共享缓存默认保存最多 128 个提供器条目，内存 TTL 为 300 秒。每次返回资料副本，失败结果不会缓存；取消单个请求不会取消其他请求共享的读取。清理使用 generation 隔离，正在结束的旧读取不会重新填回缓存；关闭服务会取消并等待在途读取。非强制刷新读取失败且允许陈旧兜底时，缓存会返回磁盘上的旧快照并计入 `stale_hits`。

`persistence.py` 在磁盘上保存每个提供器最近一次成功读取的未签名版本资料，默认目录为 后端目录下的 `Backend/.cache`（运行时生成、已加入 `.gitignore`，可用 `cache.directory` / `WINAPPSMANAGER_CACHE_DIR` 改到别处），默认有效期 3600 秒。快照使用临时文件加原子替换写入；单个 JSON 损坏或 schema 版本不符时会被忽略并删除。过期快照不会立刻删除：只要过期时间不超过 `cache.max_stale_seconds`（默认 604800 秒，即 7 天）就保留下来，作为抓取失败时的兜底（`refresh.use_stale_cache`），超过才删除。磁盘读写失败只会退化为普通缓存 miss，不影响 API 返回。内存 TTL 与磁盘 TTL 相互独立：内存过期后会先尝试磁盘快照，磁盘也过期才重新访问官网。

`GET /api/v1/cache` 返回内存统计（`entries`、`hits`、`misses`、`persistent_hits`、`stale_hits` 等）、`persistent` 子对象（目录、条目、`stale_entries`、`max_stale_seconds`、写入、错误、最近清理数量）、`warmup` 与 `refresh` 两段（结构同 `/api/v1/status`）、`logging` 段（`enabled`/`level`/`console`/`directory`/`current_path`/`file_count`/`total_bytes`，只读统计）以及 `settings` 中本次启动实际生效的配置。`settings.github.token` 会被替换成 `***`（未配置时为 `""`）并附带 `token_configured`，不会泄露令牌。`DELETE /api/v1/cache` 同时清理内存和磁盘快照，响应中的 `cleared` 是内存条目数，`persistent_cleared` 是删除的磁盘文件数。清理后的下一次读取会更新官方资料。两份缓存都只保存 `get_releases()` 返回的未签名资料，`resolve_download()` 不经过缓存，`releases/latest` 始终重新读取最新数据。GitHub 条件缓存另存于 `<cache.directory>/github`，只包含公开响应和校验头，不计入上述版本缓存统计，也不包含令牌。联想应用商店源另有自己的缓存（见“软件源（内置精选 / 联想应用商店 / 360 软件管家）”一节）：`GET /api/v1/cache` 的 `lenovo` 段给出启用状态、已缓存数量/时间，以及只读的 `catalog` 状态对象——`apps`、`fetched_at`、`age_seconds`、`refresh_interval_seconds`、`refresher_running`、`next_refresh_at`、`last_refresh_at`、`last_error`、`refreshes`、`background_refreshes`、`stale_served`（目录未加载时 `apps`/`fetched_at`/`age_seconds` 为 `null`，未配置/未启动定时刷新时 `refresh_interval_seconds` 为 `0`、`refresher_running` 为 `false`）。360 源的 `soft360` 段结构相同（少 `background_refreshes`、多 `categories`），`catalog` 未加载时为 `null`。`DELETE /api/v1/cache` 在原有 `cleared`/`persistent_cleared` 之外追加 `lenovo` 与 `soft360` 清理结果（未启用时为 `null`，清理失败时为 `{"cleared":false}`，且不影响内置精选的清理结果），同时保留联想 token；清理联想目录后下一个请求走冷启动同步抓取一次并重新写 `catalog.json`，后台定时刷新继续按间隔更新；清理 360 目录后请求路径仍不联网，由后台线程或 `POST /store/360/refresh` 重新填充。

45 款软件的固定图标保存于 `icon/`（43 个 PNG，另有原有的 `7zip.ico` 和 `notepadplusplus.ico`），`data/apps.json` 与 `data/apps.d/*.json` 使用本地 `icon_file`。图标均来自官网或官方源码，来源和文件 SHA256 见 `icon/SOURCES.md` 以及 `icon/sources-devtools.md`、`sources-ai.md`、`sources-chat.md`、`sources-utils.md`。服务运行与刷新版本时读取本地文件；图标接口返回 `Cache-Control: public, max-age=604800`、内容 ETag 和 Last-Modified，条件请求命中时返回 304。清理版本缓存不会删除图标资产。

桌面客户端另有 5 分钟内存目录缓存及磁盘快照，按后端地址与系统架构隔离。磁盘快照只保存展示资料，所有安装地址会清空；下载和复制链接始终重新调用 latest 接口。更新失败时可显示 24 小时内的离线缓存并提示失败原因。图标也缓存在本机，重复打开页面优先读本地图片；每个图标旁记录 `<key>.meta.json`（`etag` / `last_modified` / `checked_at_utc`），默认 24 小时内直接使用文件，过期后用 `If-None-Match` / `If-Modified-Since` 重新验证：304 只刷新元数据、200 替换文件、后端超时或出错时继续用旧文件。图标写盘失败时保留在进程内存（默认最多 100 个 / 16 MB，FIFO 淘汰）并继续显示，之后的读取会重试落盘；ICO 由客户端解析目录并选择最大帧显示（PNG 帧直接解码、DIB 帧重封装），不依赖 WPF 整容器选帧。

客户端在发布目录（`AppCatalogService.PublishCatalogAsync`）时按当前 Windows 版本与 CPU 架构过滤不兼容条目，记录 `HiddenIncompatibleCount` 并在列表数量旁显示「已按当前系统隐藏 N 款不兼容的软件」，磁盘目录快照仍保存完整目录，系统升级后能重新显示。

设置页的缓存卡片显示来源、更新时间、文件数量与大小，提供清理入口。清理会删除客户端管理的目录和图标缓存文件，并请求后端清理版本缓存；后端图标资产、安装包和主题设置保留。

查询其他软件：

```powershell
Invoke-RestMethod 'http://127.0.0.1:8080/api/v1/apps/wechat/releases/latest?architecture=x64'
Invoke-RestMethod 'http://127.0.0.1:8080/api/v1/apps/vscode/releases/latest?architecture=arm64'
Invoke-RestMethod 'http://127.0.0.1:8080/api/v1/apps/notepadplusplus/releases/latest?architecture=x86'
```

## 添加软件提供器

`providers/base.py` 定义 `ReleaseProvider` 接口：

- `get_releases(force_refresh=False)`：返回当前各架构的版本资料。
- `resolve_download(release)`：为该资源生成可用下载地址；需要临时签名时在这里获取。

添加软件时，在 `providers/` 下实现一个类，在 `providers/__init__.py` 的 `create_release_providers` 注册表中添加实例，再在软件条目中配置 `download_provider` 为该注册表的键。插件软件的静态 `releases` 为空、`latest_release_id` 为 `null`。之后 REST 路由和桌面下载流程可以直接复用。提供器注册由项目代码明确控制，不从配置动态执行任意脚本。

GitHub Releases 软件**不需要写 Python**：在 `data/apps.d/<group>.json` 的条目里写 `download_provider`（等于 app id）和 `provider_spec`（仓库、tag 前缀、资产正则），启动时 `create_release_providers(..., catalog=...)` 会按 `provider_spec` 自动创建并注册 `GithubReleaseProvider`。完整字段、正则示例、图标要求、分类词表和验证方法见 `data/apps.d/README.md`。GitHub API 配额有限，需要令牌时配置 `github.token`（见上文），令牌不会出现在任何响应里。

非 GitHub 来源（官网 JSON、CDN 最新别名、在线安装器等）**需要写 Python**：在 `providers/` 实现 `ReleaseProvider`（推荐复用 `_utility.py` 的 `UtilityReleaseProvider` 和 `official_executable_url`），在 `providers/__init__.py` 的静态注册表里注册唯一 id，条目中**不要写 `provider_spec`**，`download_provider` 指向该 id 并保持 `latest_release_id: null`、`releases: []`。WorkBuddy、DeepSeek Harness、豆包就是这样接入的（实现见 `workbuddy.py`、`deepseek_harness.py`、`doubao.py`），条目写法和新增步骤见 `data/apps.d/README.md` 的“非 GitHub 来源条目”。

静态安装包也继续受支持：不设置 `download_provider`，在 `releases` 中维护资源，并让 `latest_release_id` 指向当前版本。插件和静态版本不能混用。新增软件尚未配置安装包，或请求的架构无安装器时，`latest_release` 返回 `null`，前端保留官网下载页入口。

图标放入 `icon` 目录，并用 `icon_file` 指定文件名。`icon_file`、`download_provider`、`provider_spec`、`latest_release_id` 和 `releases` 只用于后端存储，不出现在软件响应中。启动会校验数据结构、重复 ID、提供器注册、版本引用、`provider_spec` 正则/架构、文件名及图标文件；启动本身不访问外部服务。

## 测试

在后端目录运行：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

API 测试使用注入的测试提供器，各软件插件测试使用 `httpx2.MockTransport`。覆盖官网结构、架构、刷新、校验、新签名、缓存命中/过期/并发/取消/清理、图标 ETag/304、上游失败和启动校验。启动预热与可用性注册表、陈旧快照兜底、定时刷新循环，以及 API 层的预热/失败回退/隐藏不可用软件/逐 app 隔离分别由 `tests/test_warmup.py`、`tests/test_stale_cache.py`、`tests/test_scheduler.py`、`tests/test_startup_refresh.py` 覆盖；`tests/test_api.py` 的测试工厂默认关闭启动预热与定时刷新（`RefreshConfig(on_startup=False, interval_seconds=0)`），以保持 provider 计数确定，并通过 `extra_directory=<不存在的目录>` 隔离生产 `apps.d` 条目、保持原有 8 款软件的断言稳定。

平台兼容性由 `tests/test_platform_compatibility.py` 覆盖：字段默认值、重复架构与非法架构/非法 `minimum_windows` 的拒绝、`/apps` 架构过滤、响应中的两个新字段，以及详情对不支持的架构仍可访问；`tests/test_api.py` 的 `APP_FIELDS` 已包含 `minimum_windows` 与 `architectures`。

新增的 GitHub 相关测试：`tests/test_github_provider.py` 覆盖资产解析（`{version}` 占位符与无占位符正则）、令牌请求头、错误映射、超时/传输错误、协议校验、下载地址仓库大小写和 `resolve_download()` 身份校验，并新增条件请求、304 复用与 `github.refresh_interval_seconds` 间隔门控；`tests/test_github_cache.py` 覆盖响应缓存的往返、文件名摘要、损坏/版本不符忽略、计数器、写盘失败降级和 `create_app` 接线；`tests/test_catalog_merge.py` 覆盖 `apps.d` 合并、缺目录、跨文件重复 id、注入目录、扩展条目 schema 与图标校验；`tests/test_config.py` 覆盖 `github` 段、`config.local.json` 深合并、环境变量优先级、非法令牌/`api_base` 以及 `github.refresh_interval_seconds` 的默认值、文件/环境变量覆盖和非法值；`tests/test_api.py` 覆盖 `/api/v1/cache` 的令牌脱敏；`tests/test_ai_providers.py` 覆盖 WorkBuddy、DeepSeek Harness、豆包 三个国内 provider 的协议解析、回退与错误映射；`tests/test_provider_registry.py` 断言内置专用 provider 为 11 个、`data/apps.json` 的 8 个是其子集，其余 GitHub provider 由 catalog 动态注册。所有测试使用假令牌与 `MockTransport`，禁止访问真实网络。测试在进程内执行，不占用 8080 端口，不访问外部服务或执行安装包，实际通过数量以运行输出为准。

联想应用商店源的测试同样全部离线：`tests/test_lenovo_api.py` 用注入的假服务覆盖 `/api/v1/sources`（启用/未启用、`online_search` 为 true/false、`app_count` 为 `null` 与非 `null`）、全部 `/store/lenovo/*` 路由（成功 JSON、GET 带 `refresh=true` 被忽略且只读缓存、目录读取失败的 502、详情/下载不受只读 `refresh` 查询影响、404、502/504、未启用 503、图片 ETag/304、下载 `Cache-Control: no-store`、`GET/DELETE /api/v1/cache` 的 `lenovo` 段含 `catalog` 状态）以及 lifespan 对注入服务只调用可选的 `preload_catalog`、预加载失败不阻止启动、不启动定时刷新、不关闭注入服务；`tests/test_lenovo_refresher.py` 覆盖磁盘快照预加载、GET 请求只读缓存（0 次网络、`catalog.json` mtime 不变、无后台刷新）、冷启动抓取落盘、`refresh_now` 同步写盘、`force_refresh` 立即返回 + 单飞后台刷新、并发 `refresh_now` 合并为一轮、独立 daemon 线程随 lifespan 启动即存在且启动不联网、0.05 秒小间隔连续刷新并写盘、陈旧快照启动立即补刷一次、快照新鲜时等待剩余间隔、完全没有快照时等待一个间隔才联网、刷新失败保留旧快照并记录 `last_error`、`shutdown` 后线程 `is_alive()==False`（join 退出）以及 `GET /api/v1/cache` 的 `lenovo.catalog` 字段；`tests/test_lenovo_config.py` 覆盖 `lenovo` 段默认值（含 `catalog_refresh_interval_seconds=300`）、提交的 `config.json` 包含该段、文件/`config.local.json`/环境变量覆盖全部字段，以及负数 TTL、负数/NaN 刷新间隔、并发 0/7、`image_cache_max_bytes` 非正、非法布尔等启动失败路径。联想核心模块（client/token/catalog/details/media/service）另由 backend-core 负责的 `tests/test_lenovo_client.py`、`test_lenovo_catalog.py`、`test_lenovo_details.py`、`test_lenovo_media.py` 覆盖（`test_lenovo_details.py` 额外覆盖截图/封面 CDN 直连优先与代理兜底回退、模型拒绝任意图片 URL），全部使用 `httpx2.MockTransport` 与临时目录，不访问真实联想接口。视频代理由 `tests/test_lenovo_video.py` 覆盖（同样只用 `MockTransport` + 临时目录）：详情层实时解析视频地址且不写缓存、index 越界/非视频 404、非 https/非白名单主机/用户信息 502、softID 不一致 502；路由层 200 流式、`Range` → 206 并透传 `Content-Range`/`Accept-Ranges`、416 透传、404（图片/越界/负索引 422）、403 auth_key 过期后重新解析一次并重试、二次 401/410 → 502、上游 5xx → 502、读取超时 → 504、`HEAD` 只返回响应头、`Cache-Control: no-store`，以及“签名 URL/auth_key 不出现在日志和任何缓存文件”的断言；`tests/test_lenovo_api.py` 的未启用源用例同时断言视频路由 GET/HEAD 返回 503。

内置精选源的启用范围由 `tests/test_builtin_source.py` 覆盖（同样注入假 provider、不联网）：白名单只暴露 `clashverge` 与 `telegram`（`/apps` 2 款、其余软件详情/版本/图标 404）、完整目录仍加载 45 款、只为白名单软件注册 provider 且 `/refresh` 只抓这两款、空白名单等于全部软件、`builtin.enabled=false` 时 `/api/v1/apps*` 与 `/refresh` 返回 503 且不创建 provider（`create_release_providers` 不被调用）、`/health`/`/status`/`/cache`/`DELETE /cache` 不崩、`/sources` 的 enabled/`online_search`/app_count、未知 app id 启动失败，以及 `builtin` 段的默认值、提交的 `config.json`、环境变量覆盖与非法值拒绝。

日志实现由 `tests/test_logging.py` 覆盖：`logging` 段的默认值、提交的 `config.json`、文件/`config.local.json`/环境变量覆盖与非法 `level`/`max_bytes`/`backup_count`/布尔值启动失败；`configure_logging` 的写盘格式（含 UTF-8 中文）、root 级别、uvicorn 三个 logger 共用同一文件且每行只写一次、幂等不重复 handler、`enabled=false` 不建目录不写文件、目录不可用时降级为仅控制台、按 `max_bytes` 轮转出 `.1` 备份、`shutdown_logging` 释放文件、`get_log_files` 只读统计；以及 `GET /api/v1/cache` 的 `logging` 段、禁用时不创建目录、「日志初始化失败不影响服务启动」。日志测试全部注入临时目录，不写真实 `Backend/.cache`。注意：其它测试工厂注入的 `BackendSettings` 若未覆盖 `logging`，lifespan 会按默认配置在 `Backend/.cache/logs` 写测试日志（该目录已 gitignore）；新增测试建议注入临时 `logging.directory` 或 `logging.enabled=false`。

360 软件源的测试同样全部离线（`httpx2.MockTransport` + 临时目录，禁止真实网络）：`tests/test_soft360_client.py` 覆盖列表 URL/查询参数、`getinfo` 逗号拼接与 50 个 ID 上限、`errno != 0`/`data=null`/非 JSON/非法包络/HTTP 错误/超时/连接错误的异常映射与参数校验；`tests/test_soft360_catalog.py` 覆盖 24 分类固定清单、分页与空数组提前结束、按 `soft_id` 去重与首分类归属、分类计数、logo http→https、描述截断、分类失败重试一次后整轮失败、磁盘 `catalog.json` 写入/`preload` 只读/损坏与过旧快照忽略/`clear`、以及独立 daemon 线程 `soft360-catalog-refresher` 按小间隔刷新写盘并在 `shutdown` 后退出；`tests/test_soft360_details.py` 覆盖 `getinfo` 字段映射（含整数 `baoku_cate.id`、`os.bit` 架构、`os.ver` 最低 Windows 与低于 7 按下限、`logo`→`icon_url` 与旧缓存兼容）、截图 https 规范化与 CDN 直连/代理回退（模型拒绝任意图片 URL）、详情内存/磁盘缓存与陈旧回退、`p2p` 的 `|||` 与 `pdown://` 内嵌 360 CDN 地址、`multi_urls` 回退、https 优先、文件名清洗与回退、大小为 `down.default.size`/`soft_size`、`sha256=None`、实时解析不写缓存以及 404/502；`tests/test_soft360_media.py` 覆盖 http→https、host 白名单与 SSRF（含重定向）、格式/大小/空响应、内存与磁盘缓存、`clear`、超时/HTTP 错误与 ETag 稳定性；`tests/test_soft360_api.py` 用注入的假服务覆盖 `/api/v1/sources`（三项、360 启用/未启用、`online_search` 与 `app_count`）、全部 `/store/360/*` 路由（成功 JSON、GET 带 `refresh=true` 被忽略且只读缓存、空目录、404、502/504、未启用 503、图片 ETag/304、下载 `no-store`、搜索本地匹配与参数校验、`POST /refresh`）、`GET/DELETE /api/v1/cache` 的 `soft360` 段以及 lifespan 对注入服务只 `preload` 不启动线程/不 shutdown；`tests/test_soft360_config.py` 覆盖 `soft360` 段默认值、提交的 `config.json`、文件/`config.local.json`/环境变量覆盖全部字段，以及分页 0/21、并发 0/17、负数/NaN TTL、非正图片上限、非法布尔等启动失败路径。新增的 `tests/test_soft360_search.py` 覆盖 AES-128（FIPS-197 已知向量）、AES-128-CBC/PKCS#7、RSA PKCS#1 v1.5（用固定测试私钥构造 type-1/type-2 padding）与 SPKI 解析、请求 `q`/`skey`/签名排序过滤规则、响应解密、`Soft360SearchClient` 的 MockTransport 端到端与错误映射（超时/HTTP/非法 JSON/errno/解密失败/空结果）、服务层搜索映射/`limit` 截断/去重/远程失败与空结果回退本地目录、目录外软件的详情/下载/图标/截图与 404，以及 `GET /store/360/search` 路由的 200、参数 422、上游失败回退（ASGITransport，不启动 lifespan、不联网）。
