# AGENTS.md

本文件面向在此仓库中工作的 AI 编码代理和人类贡献者，说明项目结构、运行方式、约定、测试要求和已知陷阱。修改代码前请先阅读相关章节。

## 1. 项目概览

WinAppsManager 是一个 Windows 软件管理器：

- **前端**：WPF，单一目标 `net6.0-windows7.0`（Windows 7 SP1 / 8 / 8.1 / 10 / 11 共用一套构建），使用 iNKORE.UI.WPF.Modern 控件与主题。商店应用不依赖 WinRT：Windows 10/11 通过 Windows PowerShell `Get-AppxPackage` 自动启用，Windows 7/8 只显示桌面软件。软件源完全由服务端 `/api/v1/sources` 下发，客户端不硬编码来源 ID、名称、接口路径或能力分支。
- **后端**：Python 3.11+ / FastAPI，默认监听 `127.0.0.1:8080`；同时提供**联想应用商店**（默认源，目录约 5111 款，服务端代理联想 PC 商店接口并缓存）、**360 软件管家**（服务端代理 360 应用中心，默认各分类热门子集约 5940 款）与**内置精选**（`/api/v1/apps` 等既有接口）三个软件源。
- **职责划分**：后端维护软件目录、官网版本解析、下载地址签名、图标与缓存；客户端负责展示、筛选、下载任务、进度、设置和本地缓存。联想接口的调研文档见 `docs/lenovo-store-api.md`，整体设计见 `docs/store-source-design.md`。
- **客户端后端地址**：默认 `https://winapps.theconsole.top/`（在线部署服务端），可在设置页“服务端地址”中运行时修改（`AppSettings.BackendBaseUrl`，http/https 绝对地址）；旧版默认 `http://127.0.0.1:8080/` 会在读取设置时自动迁移到在线地址，切换后重建按服务端隔离的缓存。
- **当前软件**：内置精选目录 45 款（开发工具 11、社交通讯 10、AI 助手 10、实用工具 5、影音工具 3、系统工具 2、网络与安全 2、办公效率 1、网络浏览 1），默认配置只启用 Clash Verge Rev 与 Telegram 两款（`builtin.app_ids`）；QQ、微信、VS Code、7-Zip、VLC、Firefox、OBS Studio、Notepad++ 与 WorkBuddy、DeepSeek Harness、豆包用 11 个内置专用 provider，其余走通用 GitHub Releases provider。联想源提供约 5000 款软件与游戏，客户端默认使用联想源。
- **GitHub 认证**：通用 GitHub provider 支持可选的 API 令牌（未认证 60 次/小时，带令牌 5000 次/小时）；令牌只放在被 gitignore 的 `Backend/config.local.json` 或环境变量 `WINAPPSMANAGER_GITHUB_TOKEN`，绝不写日志、缓存或 API 响应。
- **下载**：客户端分段并发下载，下载任务统一显示在“下载中心”；详情页负责发起任务和显示链接。安装包默认保存到 `%LOCALAPPDATA%\WinAppsManager\Downloads`，可在设置页自定义下载目录。
- **卸载**：客户端读取 Windows 卸载注册表和当前用户 Microsoft Store/MSIX 包，支持批量选择并逐个启动卸载程序或 `Remove-AppxPackage`。
- **自启动项**：客户端枚举 `Run`/`RunOnce`（HKLM 含 32 位视图）、启动文件夹快捷方式、“登录时触发”的顶层计划任务（`\Microsoft\` 下的系统任务与任务管理器一样不列）与 Store/UWP 应用启动任务；启用/禁用走 Windows 自己的 `Explorer\StartupApproved` 审批表、任务的 Enabled 标志或 UWP 的 `State`（不删除条目，随时可还原），删除另行确认，新增支持 8 种落点（Run/RunOnce、两个启动文件夹、登录时任务）；名称取 exe 的“文件说明”并单列显示公司，报表列宽可拖动。详见 6.14。
- **设置**：主题、软件源、服务端地址、下载线程数、下载超时、下载目录、GitHub 加速镜像、代理模式/自定义代理、忽略证书校验。

用户界面和文档主要使用中文；代码标识符使用英文。

## 2. 仓库结构

```text
WinAppsManager/
  WinAppsManager.sln
  WinAppsManager/                         WPF 客户端源码
    WinAppsManager/
      App.xaml / App.xaml.cs             应用入口、共享 Wam.* 主题资源
      MainWindow.xaml / .cs              导航、分类子项、页面切换、关闭时取消下载
      Polyfills.cs                       net6 编译器标记补齐
      Models/                            API 模型、设置、下载进度、已安装软件模型（UnifiedSourceModels 统一软件源 DTO）
      Pages/
        AppsInstallPage.xaml / .cs        软件列表、搜索/排序/分类/分页、服务端搜索、按需图标
        AppDetailsPage.xaml / .cs         详情、截图相册、下载链接、发起下载
        DownloadCenterPage.xaml / .cs     全局下载任务列表与操作
        UninstallPage.xaml / .cs          已安装软件列表、搜索、批量选择
        StartupPage.xaml / .cs            自启动项列表、启用/禁用、删除与新增
        SettingsPage.xaml / .cs           主题、软件源、服务端地址、下载、网络、缓存、设置存储
      Dialogs/
        UninstallConfirmationWindow.xaml / .cs  卸载二次确认与可停止说明
        UninstallProgressWindow.xaml / .cs      顺序卸载进度、停止与结果
        AddStartupItemWindow.xaml / .cs         新增 Run 自启动项：路径、名称、参数、作用范围
        DeepCleanWindow.xaml / .cs              深度清理：残留树的勾选与删除
        PendingRebootWindow.xaml / .cs          重启删除提示：列出未能立即删除的路径，可立即重启或完成
        ScreenshotViewerWindow.xaml / .cs       商店截图/视频大图预览与视频播放（MediaElement）
      Services/
        AppCatalogService.cs              统一软件源 API 客户端、目录/详情/搜索/图标本地缓存、按系统过滤目录
        StoreMediaService.cs              联想商店截图/封面图片加载与内存/磁盘缓存
        AppLogService.cs                  文件日志（%LOCALAPPDATA%\WinAppsManager\Logs，按天分文件、保留 14 天）
        AppSettingsService.cs             设置读写、迁移、校验、主题资源更新
        DownloadCenterService.cs          全局下载任务生命周期
        InstallerDownloadService.cs       分段下载、Range 校验、可选 SHA256、容器签名校验
        AuthenticodeSignatureService.cs    安装包数字签名读取与 WinVerifyTrust 校验（仅提示）
        DownloadNetworkService.cs         GitHub 镜像、代理、证书、下载 HttpClient
        InstallerDownloadTracker.cs       总进度/速度/剩余时间聚合
        InstalledApplicationProvider.cs   卸载注册表读取、过滤、去重与排序
        StoreApplicationProvider.cs       当前用户商店/MSIX 包枚举与元数据
        PlatformCompatibility.cs          按 Windows 版本与安装包架构判断目录条目是否兼容
        BclCompat.cs                      net6 兼容层：SnakeCase、ReadExactlyAsync、SHA256、GC
        StoreApplicationUninstallCommand.cs 商店应用 Remove-AppxPackage 命令
        InstalledApplicationIconService.cs DisplayIcon 路径/索引解析与图标提取缓存
        InstallLocationResolver.cs       安装目录解析：InstallLocation / DisplayIcon / 卸载程序目录
        UninstallCommandParser.cs         卸载命令行拆分
        UninstallProcessJob.cs            作业对象：跟踪并终止卸载程序进程树
        UninstallRunner.cs                启动厂商卸载程序、跟踪进程树并等待结束
        UninstallVerifier.cs              卸载后按注册表/包核对是否真的移除
        LeftoverScanner.cs                残留扫描：注册表 + 文件夹 + 快捷方式 + MSI
        LeftoverCleaner.cs                按类型分派残留删除（注册表 / 文件）
        RegistryLeftoverCleaner.cs        勾选注册表残留的受控删除
        FileLeftoverCleaner.cs            勾选文件/文件夹/快捷方式的受控删除
        FileLockInspector.cs              检测文件共享锁、用 Restart Manager 结束占用进程
        SystemRestartService.cs           清理完成后按用户选择立即重启 Windows
        ShortcutResolver.cs               通过 WScript.Shell 在 STA 线程解析 .lnk 目标（含参数）
        StartupItemProvider.cs            自启动项枚举：Run/RunOnce、启动文件夹，读写 StartupApproved
        StartupTaskProvider.cs            登录时触发的计划任务：XML 读取状态、schtasks 切换/删除
        StartupItemService.cs             自启动项写操作：启用/禁用、删除、新增 Run 项
        FolderPickerService.cs            原生文件夹选择对话框（IFileOpenDialog，设置页下载目录用）
        ExternalLinkService.cs            打开外部 URL
  Backend/                                FastAPI 后端
    main.py                               应用入口、路由、缓存接线
    models.py                             Pydantic 模型
    catalog.py                            data/apps.json + data/apps.d/*.json 合并读取和查询
    cache.py                              内存元数据缓存
    persistence.py                        磁盘元数据快照
    config.py / config.json / config.local.json  配置读取、校验、机器本地覆盖与环境变量覆盖
    releases.py                           provider 调用、超时、刷新重试
    icons.py                              图标 ETag/304 与静态文件
    providers/                            各软件 provider（github.py 通用来源、_github_cache.py 条件缓存、3 个国内 AI）
    lenovo/                               联想应用商店源：token/client/models/catalog/details/media/service/routes
    soft360/                              360 软件管家源：client/catalog/details/media/service/routes
    data/apps.json                        精选软件目录（原有 8 款）
    data/apps.d/*.json                    扩展软件目录（37 款，按文件名合并）
    icon/                                 固定图标、来源说明与 sources-<group>.md
    tests/                                unittest 测试
  docs/
    lenovo-store-api.md                   联想 PC 商店接口调研（token/分类/列表/搜索/详情/下载/图片）
    soft360-store-api.md                  360 应用中心接口调研（分类/列表/详情/下载/图片）
    soft360-search-api.md                 360 加密搜索接口逆向（AES/RSA/MD5 签名与加解密）
    store-source-design.md                多软件源整体设计与实现契约
    source-api.md                         服务端/客户端统一软件源 API 契约（新增来源只改服务端）
  deploy/                                 systemd / Caddy 部署模板
  artifacts/ui/                           UI 截图（可选交付物）
  release.props                           发布版本 / 标签 / 标题 / 说明文件配置（客户端版本号来源）
  Release.md                              GitHub Release 发布说明
  README.md                               项目说明
  Backend/README.md                       后端详细说明
```

以下目录是生成物或本地环境，**不要编辑，也不要提交**：

- `Backend/.venv/`
- `Backend/**/__pycache__/`
- `Backend/.cache/`（后端版本缓存，运行时生成）
- `Backend/config.local.json`（机器本地配置，例如 GitHub 令牌；已加入 `Backend/.gitignore`，**禁止提交**）
- `WinAppsManager/**/bin/`、`WinAppsManager/**/obj/`
- 用户的 `%LOCALAPPDATA%\WinAppsManager\` 下的客户端设置、目录缓存、安装包（后端缓存已改为 `Backend/.cache/`）
- 任何 `*.user`、测试临时目录

## 3. 工具链与前置条件

- 开发机 Windows 10/11（后端要求 Python 3.11+，无法在 Win7 原生运行，见第 10 节）。
- .NET SDK 8 或 10。客户端只有单一目标 `net6.0-windows7.0`（.NET 6 是最后一个官方支持 Windows 7 SP1 的 .NET 版本，已 EOL，`CheckEolTargetFramework=false`）；不要改成 .NET Framework（WPF 与依赖包不支持），也不要重新拆成 net6/net8 双目标。
- 运行需 .NET 6 Desktop Runtime（Win7 SP1 可安装）。客户端随程序嵌入 `Assets/Fonts/FluentSystemIcons-Regular.ttf`（MIT，来自 Microsoft fluentui-system-icons），并通过 `App.xaml` 的 `Wam.IconFontFamily` 隐式样式替换 `ui:FontIcon` 字体；Win7 没有 Segoe Fluent Icons 时图标不会变成方框。图标数据使用 `ui:FluentSystemIcons.*`，不要改回 `SegoeFluentIcons.*`。发布配置在 `Properties/PublishProfiles`（x64/x86，框架依赖或自包含），同一套输出适用于 Windows 7–11；不再按操作系统拆包。x86 发布（64 位改用 `-r win-x64`）：
  ```powershell
  dotnet publish WinAppsManager\WinAppsManager\WinAppsManager.csproj -c Release -f net6.0-windows7.0 -r win-x86 --self-contained false -p:PublishProfile=Win7-x86
  ```
- Python 3.11 或更高版本（推荐 3.12；httpx2 2.16 要求 3.11+）；后端依赖固定在 `Backend/requirements.txt`，不要随意升级或增加依赖。
- WPF 客户端只面向 Windows，不能在没有桌面/WPF 支持的环境运行。

当前关键包版本：

| 项目 | 包/版本 |
| --- | --- |
| 后端 | FastAPI 0.141.1、Pydantic 2.13.4、uvicorn 0.52.3、httpx2 2.16.0 |
| 客户端 | iNKORE.UI.WPF 1.2.8、iNKORE.UI.WPF.Modern 0.11.0 |

## 4. 常用命令

所有命令如无特别说明，都在仓库根目录执行。

### 4.1 后端

```powershell
python -m venv Backend\.venv
Backend\.venv\Scripts\python.exe -m pip install -r Backend\requirements.txt
Backend\.venv\Scripts\python.exe Backend\main.py
```

或：

```powershell
Backend\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8080 --app-dir Backend
```

运行后端测试：

```powershell
Backend\.venv\Scripts\python.exe -m unittest discover -s Backend\tests -v
```

运行单个测试模块：

```powershell
Backend\.venv\Scripts\python.exe -m unittest Backend.tests.test_cache -v
```

API 文档：<http://127.0.0.1:8080/docs>；健康检查：`GET /api/v1/health`。

### 4.2 客户端

构建整个解决方案：

```powershell
dotnet build WinAppsManager\WinAppsManager.sln -c Release
```

运行客户端（需要后端已启动；单目标项目无需 `-f`）：

```powershell
dotnet run --project WinAppsManager\WinAppsManager\WinAppsManager.csproj
```

Rider/Visual Studio 直接运行唯一项目配置即可；Windows 10/11 会自动加载商店应用，Windows 7/8 自动只显示桌面软件。

客户端不包含自动化测试项目（原有的 5 个控制台测试项目已移除），客户端改动以 `dotnet build WinAppsManager\WinAppsManager.sln -c Release` 编译唯一目标为准，要求 0 警告、0 错误；确实需要新增测试项目时，先与用户确认，不要自行加回解决方案。

### 4.3 提交前质量门禁

至少执行：

1. `dotnet build WinAppsManager\WinAppsManager.sln -c Release`：0 警告、0 错误。
2. 若改动后端：后端完整 unittest 通过。
3. 若改动 UI：优先通过构建验证；除非用户明确要求或涉及无法自动验证的运行时行为，不要额外加入或运行冒烟测试。
4. 更新相关 README/AGENTS；不要让临时截图、下载文件或测试配置残留。

### 4.4 发布

发布信息集中在仓库根目录的两个文件：

- `release.props`：`Version`（客户端程序集版本，`WinAppsManager.csproj` 直接导入该文件）、`ReleaseTagPrefix`、`ReleaseTitle`、`ReleaseNotesFile`、`ReleaseLatest`。
- `Release.md`：GitHub Release 的正文。

发布流程：修改 `Version` 与 `Release.md` → 提交 → 推送标签 `v<Version>`（或手动运行 `Release` workflow）。workflow 读取这两个文件，构建 `win-x64` / `win-x86` 自包含 zip，创建或覆盖同名 release；标签与 `Version` 不一致会直接失败。已存在的 release 会先删除本次构建中不存在的旧资源，再覆盖 zip 与发布说明。不要在 workflow、csproj 或 README 中再硬编码一份版本号。

## 5. 后端架构

### 5.1 启动与路由

`Backend/main.py` 的 `create_app()` 支持注入 `data_file`、`icon_directory`、`release_providers`、`settings`、`extra_directory`，测试据此避免外部网络、真实缓存和生产扩展目录（不传 `extra_directory` 时用 `data_file` 同级的 `apps.d/`）。

启动时在 lifespan 中：

- 加载并校验软件目录：先读 `data/apps.json`，再按文件名顺序合并同目录 `apps.d/*.json`（同一 schema）；`icon_file` 必须存在，跨文件重复 app id、非法 `provider_spec` 都会让启动失败。
- 创建 HTTP 客户端（普通客户端 + 直连客户端；Notepad++ 使用环境代理客户端）。
- 创建 provider 注册表（`create_release_providers(...)` 会按目录 `provider_spec` 自动创建通用 GitHub provider）、持久缓存和 `ReleaseService`，再创建 `CatalogWarmup` 与 `PeriodicRefresher`。
- `refresh.on_startup=true`（默认）时先做启动预热，整体预算为 `refresh.startup_timeout_seconds`；预热结束才接受请求。预算耗尽或预热异常不阻塞启动，未完成的软件按需抓取。
- 启动定时刷新任务；关闭时先 `refresher.stop()`，再取消并等待在途读取。

预热对每个 provider 管理的软件（当前 45 个）执行一次 `get_releases(force_refresh=True)`：成功记 `ok`；失败但有磁盘快照（含未超过 `cache.max_stale_seconds` 的过期快照）记 `cached`；两者都没有记 `unavailable`，`refresh.drop_unavailable_apps=true` 时它不出现在 `GET /apps`、详情返回 503。45 个来源在默认并发 4 下整轮十余秒（GitHub 命中条件缓存后重启基本不发请求）；网络慢或配额紧张时可能用满 30 秒预算，按降级规则先启动、其余按需抓取。`GET /cache` 的 `settings.github.token` 已脱敏（见 5.4）。

`GET /apps` 必须逐 app 隔离（`asyncio.gather(..., return_exceptions=True)`）：单个软件解析失败只能略过并记 warning，**不要**让整个列表变成 504；`total` 等于实际返回条目数。列表先按 `architecture` 过滤目录（见 5.2），再按预热可用性过滤；详情/版本接口不因架构隐藏软件，只按架构选择安装包。`refresh=true` 且某软件强制刷新失败时再按非强制刷新重试一次；预热表里 `ok`/`cached`（只是签名或地址解析失败）必须保留卡片并让 `latest_release` 为 `null`，只有 `unavailable` 才略过；详情与 `releases/latest` 的强制刷新失败直接报错，不回退。

主要 API（前缀 `/api/v1`）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 服务状态 |
| GET | `/sources` | 统一软件源列表：id/name/enabled/app_count/is_default/search_mode/capabilities；客户端只读它，不硬编码来源 |
| GET | `/sources/{source_id}/categories` | 统一分类列表（能力 categories） |
| GET | `/sources/{source_id}/apps` | 统一完整目录（客户端本地筛选/分页） |
| GET | `/sources/{source_id}/search` | 统一服务端搜索（search_mode=api） |
| GET | `/sources/{source_id}/apps/{app_id}` | 统一详情 |
| GET | `/sources/{source_id}/apps/{app_id}/download` | 统一下载地址（Release，no-store） |
| GET | `/sources/{source_id}/apps/{app_id}/icon`、`/screenshots/{index}`、`/cover`、`/screenshots/{index}/video` | 统一媒体代理（能力控制，视频 Range/206） |
| GET | `/status` | 预热/可用性状态与定时刷新统计，诊断用 |
| GET | `/apps` | 软件列表，支持 `search`、`category`、`sort`、`architecture`、`refresh`（`builtin.enabled=false` 时 503） |
| GET | `/apps/{app_id}` | 软件详情，不可用且无快照时返回 503 |
| GET | `/apps/{app_id}/releases` | 版本集合 |
| GET | `/apps/{app_id}/releases/latest` | 最新版本，始终刷新并重新签名 |
| GET | `/apps/{app_id}/releases/{release_id}` | 指定版本，重新签名 |
| GET | `/apps/{app_id}/icon` | 图标，支持 ETag/304 |
| GET | `/store/lenovo/categories` | 联想分类树（software/game 两组） |
| GET | `/store/lenovo/apps` | 联想完整目录（约 5111 条）：请求只读内存/磁盘 `catalog.json`，任何 GET 查询参数都不会触发抓取；手动刷新接口已禁用 |
| GET | `/store/lenovo/apps/{soft_id}` | 联想详情，`soft_id` 保留原始大小写 |
| GET | `/store/lenovo/apps/{soft_id}/download` | 实时解析下载地址，返回 `Release` 结构，`no-store` |
| GET | `/store/lenovo/search` | 联想在线搜索，`q` 1–100、`limit` 1–50 |
| GET | `/store/lenovo/apps/{soft_id}/icon` | 图标代理（直连 CDN 失败时兜底，ETag/304） |
| GET | `/store/lenovo/apps/{soft_id}/screenshots/{index}` | 截图/视频缩略图代理 |
| GET | `/store/lenovo/apps/{soft_id}/screenshots/{index}/video` | 视频播放代理（实时解析 auth_key 地址，Range/206 流式转发，`no-store`，不缓存签名 URL） |
| GET | `/store/lenovo/apps/{soft_id}/cover` | 横版封面代理 |
| GET | `/store/360/categories` | 360 分类（24 个固定分类 + 快照计数） |
| GET | `/store/360/apps` | 360 缓存目录（约 5940 款热门子集，GET 只读缓存） |
| GET | `/store/360/apps/{soft_id}` | 360 详情 |
| GET | `/store/360/apps/{soft_id}/download` | 360 下载地址（实时解析，`no-store`） |
| GET | `/store/360/apps/{soft_id}/icon`、`/screenshots/{index}` | 360 图标/截图代理（ETag/304） |
| GET | `/store/360/search` | 360 全量在线搜索（逆向 enc_client 加密接口，失败回退缓存目录本地匹配） |
| GET | `/cache` | 内存/磁盘缓存统计、`warmup`/`refresh`/`lenovo`（含 `catalog` 缓存年龄与下次刷新）状态和生效配置 |
| DELETE | `/cache` | 清理内存和磁盘版本缓存，并清理联想目录/详情/图片缓存 |

> 自 2026-11 起，所有 `POST .../refresh` 手动刷新接口（`/api/v1/refresh`、`/api/v1/sources/{id}/refresh`、`/api/v1/store/lenovo/refresh`、`/api/v1/store/360/refresh`）已禁用并返回 404，防止公网调用重复抓取上游；`/sources` 的 `capabilities.refresh` 统一为 `false`。目录只由启动预热与各来源的后台定时线程更新。

### 5.2 数据模型

`Backend/models.py` 使用 Pydantic 严格模式（`extra="forbid"`、`strict=True`）。关键点：

- `Release.download_url` 是 `HttpUrl`，文件名必须是安全的 Windows 文件名。
- provider 管理的软件不能同时声明静态 releases；静态版本必须满足 `latest_release_id` 和版本一致性。
- `GithubProviderSpec`（`repo`、`tag_prefix`、`assets`）和 `GithubAssetSpec`（`pattern`、`architecture`）描述一个 app 的通用 GitHub 来源；`assets` 至少一项且架构不能重复，`pattern` 在模型校验阶段就会编译，非法正则启动即失败。
- `AppMetadata.minimum_windows`（`7`/`8`/`8.1`/`10`/`11`）与 `architectures`（`x64`/`x86`/`arm64` 列表，至少 1 项、不可重复）是可选机器可读兼容字段，`platforms` 仍是展示文案；未声明 `architectures` 时 `supports_architecture()` 返回 true（兼容旧目录），`/apps` 按 `architecture` 过滤声明过的条目。
- `AppDefinition.provider_spec` 可选，但声明时必须有 `download_provider`；`provider_spec` 只用于后端构建 provider，`build_response` 会把它（以及 `icon_file`、`download_provider`、`latest_release_id`、`releases`）从响应中排除。重复 app/release ID 会在启动时失败；app id 在 `apps.json` 与 `apps.d/*.json` 合并后必须唯一。

### 5.3 缓存

- `cache.py`：进程内 LRU + TTL，默认最多 128 条、内存 TTL 300 秒；合并并发读取；generation 隔离清理；失败不缓存。抓取失败且 `allow_stale` 为真时回退到磁盘旧快照并计入 `stale_hits`。
- `persistence.py`：磁盘快照，默认 TTL 3600 秒；原子写（临时文件 + `os.replace`）；损坏或 schema 不符的文件忽略删除；只保存未签名元数据。过期快照在 `cache.max_stale_seconds`（默认 604800 秒）内保留作失败兜底，超过才删除。
- 内存 TTL 与磁盘 TTL 独立；磁盘过期后才重新访问官网。
- `refresh=true` 和 `releases/latest` 绕过缓存，且**不回退**到陈旧快照；唯一例外是列表接口在强制刷新失败后会再按非强制刷新重试一次（见 5.1）。非强制刷新的列表/详情在 provider 暂时不可用时会回退到旧快照（`refresh.use_stale_cache`），`/cache` 的 `persistent` 段含 `stale_entries`、`max_stale_seconds`，内存段含 `stale_hits`。
- 签名下载地址永远不写入内存/磁盘缓存，含链接的 API 响应使用 `Cache-Control: no-store`。
- 清理使用 generation，旧读取不能回填；关闭服务时先停定时刷新，再取消并等待在途任务。

### 5.4 配置

`Backend/config.json` 是提交到仓库的运行时配置，启动时读取；同目录的 `config.local.json`（已 `.gitignore`，机器本地，存 GitHub 令牌等敏感值）会深合并覆盖 `config.json`；最后 `WINAPPSMANAGER_*` 环境变量覆盖两者。优先级为：内置默认 → `config.json` → `config.local.json` → 环境变量。修改后必须重启后端。

`BackendSettings.load()` 的实际顺序见 `config.py`：读 `config.json`（可用 `WINAPPSMANAGER_CONFIG` 改路径），再深合并同目录 `config.local.json`，最后套用环境变量。`config.json` 中的 `github.token` 提交值必须保持空字符串。

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `cache.enabled` | `true` | 缓存总开关 |
| `cache.persist` | `true` | 是否写磁盘 |
| `cache.directory` | `Backend/.cache` | 持久缓存目录；相对路径按后端目录解析，`null` 也回落该默认值 |
| `cache.ttl_seconds` | `300` | 内存 TTL |
| `cache.persistent_ttl_seconds` | `3600` | 磁盘 TTL |
| `cache.max_stale_seconds` | `604800` | 过期快照仍可作失败兜底的时长，超过即删除 |
| `cache.max_entries` | `128` | 条目上限 |
| `providers.cache_ttl_seconds` | `600` | provider 内部缓存 |
| `providers.timeout_seconds` | `10` | 单次 provider 操作总期限 |
| `http.timeout_seconds` | `5` | 官网 HTTP 总超时 |
| `http.connect_timeout_seconds` | `3` | 官网连接超时 |
| `refresh.on_startup` | `true` | 启动时先抓取一轮再对外服务 |
| `refresh.startup_timeout_seconds` | `30` | 启动预热整体预算 |
| `refresh.interval_seconds` | `1800` | 定时刷新间隔，`0` 关闭 |
| `refresh.jitter_seconds` | `0` | 定时刷新抖动上限，不能大于间隔 |
| `refresh.max_concurrency` | `4` | 抓取并发上限，1–16 |
| `refresh.use_stale_cache` | `true` | 抓取失败时允许使用旧快照 |
| `refresh.drop_unavailable_apps` | `true` | 无可用数据的软件从列表隐藏 |
| `github.token` | `""` | GitHub API 令牌；只能放 `config.local.json` 或 `WINAPPSMANAGER_GITHUB_TOKEN`，提交的 `config.json` 保持空字符串 |
| `github.api_base` | `https://api.github.com` | GitHub API 根地址，必须是 https |
| `github.timeout_seconds` | `10` | 单次 GitHub API 请求超时（秒） |
| `github.refresh_interval_seconds` | `900` | 强制刷新在这个间隔内直接复用磁盘条件缓存、不发请求；`0` = 每次都发条件请求（304 不消耗配额） |
| `lenovo.enabled` | `true` | 联想应用商店源总开关；false 时不创建服务、`/store/lenovo/*` 返回 503 |
| `lenovo.catalog_ttl_seconds` | `21600` | 联想目录快照年龄阈值：超过后响应 `stale=true`，不再触发请求路径抓取 |
| `lenovo.catalog_refresh_interval_seconds` | `300` | 后台定时刷新目录并写回 `catalog.json` 的间隔（秒，默认 5 分钟；`0` 关闭） |
| `lenovo.catalog_max_stale_seconds` | `604800` | 联想目录抓取失败时允许使用的最旧快照 |
| `lenovo.details_ttl_seconds` / `details_persistent_ttl_seconds` | `900` / `21600` | 联想详情内存/磁盘缓存 TTL |
| `lenovo.search_cache_ttl_seconds` | `60` | 联想在线搜索缓存 TTL |
| `lenovo.timeout_seconds` / `catalog_concurrency` | `20` / `3` | 单次联想请求超时（连接 5 秒）/ 分类抓取并发（1–6） |
| `lenovo.image_cache_ttl_seconds` / `image_cache_max_bytes` | `2592000` / `268435456` | 联想图片磁盘缓存 TTL / 容量上限 |
| `soft360.enabled` | `true` | 360 软件管家源总开关；false 时不创建服务、`/store/360/*` 返回 503 |
| `soft360.catalog_pages_per_category` | `3` | 每个分类抓取页数（1–20，100 条/页），决定目录规模与耗时 |
| `soft360.catalog_refresh_interval_seconds` | `1800` | 360 目录后台刷新间隔（秒，`0` 关闭） |
| `soft360.catalog_ttl_seconds` / `catalog_max_stale_seconds` | `21600` / `604800` | stale 标记阈值 / 最旧快照兜底 |
| `soft360.details_ttl_seconds` / `details_persistent_ttl_seconds` | `900` / `21600` | 详情内存/磁盘缓存 TTL |
| `soft360.timeout_seconds` / `catalog_concurrency` | `20` / `4` | 单次请求超时 / 分类抓取并发 |
| `soft360.image_cache_ttl_seconds` / `image_cache_max_bytes` | `2592000` / `268435456` | 图片磁盘缓存 TTL / 容量上限 |
| `builtin.enabled` | `true` | 内置精选源总开关；false 时不建 provider、不预热、不启动定时刷新，`/apps*` 返回 503（手动刷新接口已整体禁用） |
| `builtin.app_ids` | `["clashverge","telegram"]` | 内置源启用白名单（空列表=全部 45 款）；只对白名单软件创建 provider/预热/定时刷新，未知 id 启动失败 |
| `logging.enabled` | `true` | 后端文件日志总开关（关闭后只 console/Debug） |
| `logging.directory` | `Backend/.cache/logs` | 日志目录；相对路径按后端目录解析，`null` 回落默认 |
| `logging.level` | `INFO` | DEBUG / INFO / WARNING / ERROR（大小写不敏感，非法启动失败） |
| `logging.max_bytes` / `backup_count` | `5242880` / `5` | 单文件轮转大小（>=1024）/ 备份数量（1–50） |
| `logging.console` | `true` | 是否同时输出到控制台 |

环境变量为 `WINAPPSMANAGER_` + 配置项大写路径（如 `WINAPPSMANAGER_CACHE_MAX_STALE_SECONDS`、`WINAPPSMANAGER_GITHUB_TOKEN`），完整清单见 `Backend/README.md`。监听地址/端口由 `WINAPPSMANAGER_HOST` / `WINAPPSMANAGER_PORT` 覆盖（默认 `127.0.0.1:8080`）；内网 systemd 直连 8020 的示例见 `docs/deploy.md`，不要在没有认证/HTTPS 的情况下直接暴露公网。

GitHub 约定：未认证 60 次/小时、令牌 5000 次/小时；令牌不含空白，`github.api_base` 必须是无空白 https；令牌只作 `Authorization: Bearer ...` 请求头，不写日志/缓存/响应（`/cache` 脱敏见第 9 节）。

配置校验失败应让后端启动失败，不要静默忽略未知字段/非法值。另外：`refresh.interval_seconds=0` 必须关闭定时刷新；`refresh.jitter_seconds` 不能大于 `interval_seconds`；`refresh.max_concurrency` 只能是 1–16 的整数；`github.refresh_interval_seconds` 必须是非负数。

### 5.5 Providers

`Backend/providers/base.py` 定义：

- `get_releases(force_refresh=False)`：返回未签名版本元数据。
- `resolve_download(release)`：生成当前下载地址；QQ 在这里请求签名。

#### 通用 GitHub Releases provider

`Backend/providers/github.py` 的 `GithubReleaseProvider` 覆盖 GitHub 上的 34 款软件，**一个 app 一个实例**，provider id 等于 app 的 `download_provider`（约定也等于 app id）。它完全由目录条目里的 `provider_spec` 驱动，不需要写 Python：

- 只读 `GET {api_base}/repos/{repo}/releases/latest`（要求非 draft、非 prerelease）；tag 去掉 `tag_prefix` 得到版本。`assets` 每个架构用 Python 正则 `fullmatch` 匹配资产，`{version}` 可选且替换前转义；每个架构必须恰好匹配一个资产，否则抛 `ProviderProtocolError`；只接受 `.exe/.msi/.msix/.msixbundle`。下载地址必须在 `https://github.com/<repo>/releases/download/<tag>/` 下（仓库路径不区分大小写），`size` → `size_bytes`、`digest` → `sha256`。
- `resolve_download()` 只校验身份（release id 以 `-windows-<arch>` 结尾、地址在 github.com）并返回副本，不访问网络。错误映射：401 令牌无效、403/429 配额耗尽、404 无正式发布、其他 ≥400 HTTP 错误、超时 `ProviderTimeoutError`（504）；令牌永不写日志/缓存/异常/响应。
- 条件缓存：`response_cache` 开启时（`cache.enabled && cache.persist`）每个仓库的响应体与 `ETag`/`Last-Modified` 存 `<cache.directory>/github/`（原子写、损坏即删、**绝不写令牌**）。`get_releases()` 优先用磁盘缓存：非强制读取直接复用；`force_refresh=True` 且距上次获取不足 `github.refresh_interval_seconds`（默认 900 秒）时也复用；联网必带 `If-None-Match`/`If-Modified-Since`，200 写缓存、304 复用并刷新 `fetched_at`（304 不计配额）。

`providers/__init__.py` 的 `create_release_providers(...)` 接收 `github_response_cache` 和 `github_refresh_interval_seconds`，在传入 `catalog` 时按 `provider_spec` 自动注册 GitHub provider；`create_github_providers(...)` 可单独调用。内置专用 provider 共 11 个，与 GitHub provider id 冲突时启动失败。

3 个非 GitHub 国内来源（`workbuddy.py`、`deepseek_harness.py`、`doubao.py`，复用 `_utility.py` 的 `UtilityReleaseProvider`，只有 x64）：WorkBuddy 读 `workbuddy.cn/v2/update` 的 JSON（`version`/`sha256hash`）；DeepSeek Harness 从 `HEAD dsh-latest-windows-x64.exe` 的 `Content-Disposition` 取版本、无 SHA256；豆包 POST `99uri.com/ug/pc/landpage_click`（带官网硬编码 `surl_token`）取约 7.4 MB 在线安装器，版本为构建号、无 SHA256。三者都注册为 `direct_client`。

#### 添加软件 provider

1. 先判断来源是不是 GitHub Releases：是则**不要写代码**，按 `Backend/data/apps.d/README.md` 在 `apps.d/*.json` 增加条目、图标和来源记录即可，并更新 README 软件清单。
2. 非 GitHub 来源：实现 `ReleaseProvider`，复用 `_media.py` / `_utility.py` 的解析与缓存。
3. 在 `providers/__init__.py` 的 `create_release_providers` 注册唯一 ID；`data/apps.json` 或 `apps.d/*.json` 里对应 app 设置 `download_provider`，不要同时写静态 releases。
4. 加 provider 测试（`httpx2.MockTransport`，禁止真实网络/令牌）。
5. 更新 README 软件来源表与测试说明。

版本解析失败应抛明确的 `Provider*Error`；不要返回猜测的版本/架构/文件名。GitHub 软件的正则、tag 前缀、资产命名必须用真实 API 验证一次（方法见 `apps.d/README.md`）。

### 5.6 联想/360 应用商店源与内置源启用范围

- **联想源**在 `Backend/lenovo/`：`token.py` 随机设备指纹 + JWT 缓存、`client.py` 原始接口、`catalog.py` 逐分类（soft 19 + game 9，`limit=1000`）抓取并按 `softID` 去重（约 5111 款）与内存/磁盘快照、`details.py` 详情缓存与下载地址解析、`media.py` 图片代理缓存、`service.py` 门面、`routes.py` 暴露 `/api/v1/store/lenovo/*`。**目录缓存语义（2026-11 调整）**：启动时 `preload()` 只读 `catalog.json` 到内存（不联网）；`GET /store/lenovo/apps` 请求路径**只返回内存/磁盘快照**（唯一例外是完全没有快照时的冷启动同步抓取），不会因为请求去抓联想；由**随服务启动的独立后台线程**（`threading.Thread`，名称 `lenovo-catalog-refresher`）按 `catalog_refresh_interval_seconds`（默认 300 秒）定时抓取并原子写回 `catalog.json`，启动时快照超龄会立即补刷一次，失败保留旧快照；`catalog_ttl_seconds` 只决定响应里的 `stale` 标记；手动刷新接口已禁用，GET 的 `refresh` 参数已移除，任何查询参数都不会触发抓取。详情内存/磁盘缓存；搜索 60 秒；下载地址**从不缓存**；图片按 URL 哈希落盘（30 天、256MB 上限、ETag/304）。联想接口调研见 `docs/lenovo-store-api.md`，实现契约见 `docs/store-source-design.md`。列表/搜索条目 `icon_url` 是联想 CDN 绝对地址（客户端直连取图），`icon_proxy_url` 是后端代理兜底；详情截图与封面也优先返回任意安全公网 CDN 绝对地址（`url`/`cover_url`，不再维护固定域名白名单），并附 `proxy_url`/`cover_proxy_url` 作为客户端直连失败时的后端兜底。联想业务请求全部在后端完成，客户端不直连联想接口。
- **360 源**在 `Backend/soft360/`：`client.py` 调用明文 JSON 目录/详情接口（`cdn_client/v1/soft/list`、`soft/getinfo`），`catalog.py` 抓 24 分类各前 `soft360.catalog_pages_per_category` 页（默认 3，去重约 5940 款）并发内存/磁盘 `soft360/catalog.json`，随服务启动的 `soft360-catalog-refresher` 线程按 `soft360.catalog_refresh_interval_seconds`（默认 1800 秒）刷新；`details.py` 详情缓存与下载地址解析（从 `p2p`/`multi_urls` 提取 http(s) URL；`pdown://` 描述串只取末尾直链，普通候选优先 https；md5 不写入 sha256）；`media.py` 代理 qhimg/mediav 图片（ETag/304）；`routes.py` 暴露 `/api/v1/store/360/*`。详情截图 `url` 优先返回任意安全公网 CDN 绝对地址（含 qhimg/qhmsg 等，不再维护固定域名白名单）并附 `proxy_url` 兜底；`/sources` 的 `search_mode=api` 让客户端在输入时自动调用服务端搜索 API。GET 只读缓存；手动刷新接口已禁用；搜索已逆向 `enc_client/v2/search/search`（AES-128-CBC + RSA PKCS#1 v1.5 + MD5，实现在 `search.py`，算法见 `docs/soft360-search-api.md`），`/search` 覆盖 360 全量目录，失败回退缓存子集匹配；目录本身仍是各分类热门子集，全量 43,731 款不抓全，调研见 `docs/soft360-store-api.md`。
- **内置源**通过 `builtin.enabled` / `builtin.app_ids` 控制启用范围（默认只启用 `clashverge` 与 `telegram`）。未启用的目录条目保留在 `CatalogRepository.all_apps`，但 `list_apps`/`get_app` 不暴露、不创建 provider、不预热/不刷新；`/sources` 的 `builtin.app_count` 是过滤后的数量。改动内置源启用逻辑时同步 `Backend/tests/test_builtin_source.py`。

## 6. 客户端架构

### 6.1 导航与分类

`MainWindow.xaml` 使用 iNKORE `NavigationView`，左侧导航包括：

- 软件列表（展开后包含“全部”和动态分类）
- 下载中心
- 卸载软件（展开后包含“应用程序 / 系统组件 / 隐藏组件 / 商店应用”四个分类，没有“全部”）
- 自启动项（无子项，来源筛选在页面内的下拉框里）
- 设置

两个分类子项列表都用同一套模式：`SelectsOnInvoked = false`、父项保持选中、`Tag` 前缀分别是 `category:` 与 `uninstall-category:`、当前分类用蓝色加粗文字标识，重建时遵守下面的规避方式。

分类由 `AppsInstallPage.CategoriesChanged` 提供，`MainWindow.OnCategoriesChanged` 动态创建子项。

**重要陷阱**：iNKORE NavigationView 在“动态重建子项且正处于子项选中状态”时会因 selection model 索引越界崩溃。规避方式：分类子项 `SelectsOnInvoked = false`（只触发 `ItemInvoked` 筛选）、父项保持选中、当前分类用蓝色加粗文字（`Foreground` 用 `SetResourceReference` 绑定 `Wam.AccentBrush`/`Wam.TextPrimaryBrush`，直接赋 `Brush` 会固化浅色）、重建前把 `NavView.SelectedItem` 切回父项、分类列表不变时不重建（`_categorySignature`）。不要为“高亮分类”让分类子项成为 SelectedItem，除非确认库版本已修复该崩溃。

**紧凑导航栏与分类子项**：分类子项直接加入父项的 `MenuItems`，**不要**改成 `MenuItemsSource` + `MenuItemTemplate`——`NavigationView.MenuItemTemplate` 是全局的，模板会套到顶级项上，标题会变成类型名（如 `SoftwareNavigationItem`）。导航栏收起时先记住两个分类区的展开状态，再把它们置为 `IsExpanded = false`：紧凑模式下库会为仍展开的父项弹出子项 Flyout，而 Flyout 会复用已挂载在别处的 `NavigationViewItem` 实例，其结果是一大片空白浮层。收起状态下点击“软件列表 / 卸载软件”由 `NavigationView_OnItemInvoked` 直接把 `IsPaneOpen` 置为 true 展开导航栏（走内联分类列表，不弹 Flyout）；恢复 `IsExpanded` 必须等展开动画结束（`DispatcherTimer` 420 ms），在动画中途恢复会让库仍按紧凑模式把子项 repeater 挂到浮层上，展开后分类区只剩空白。

**窗口背景与亚克力导航栏**：窗口不写死 `SystemBackdropType`。设置页“个性化 → 窗口背景”提供 无（默认，不透明）/ 亚克力 / 云母 / 自动（跟随系统）四项（`AppSettings.BackdropStyle`，`AppSettingsService.SetBackdropStyle`），改动立即生效（`SettingsChanged` → `ApplyPreferredBackdrop`）。能力判断集中在 `Services/BackdropSupport.cs`：云母要求非高对比度且内部版本 ≥ 22523（Windows 11 22H2；更低版本 Mica 走的是未公开属性，不作为可选项），亚克力要求非高对比度且库的 `Acrylic10` 可用（Windows 10 1803 / 17063+；Windows 11 上库自动选原生 `Acrylic11`），自动 = 云母 → 亚克力 → 无。设置页只禁用当前系统不支持的选项（各带“需要 Windows …”的悬停说明），不显示额外说明文字；保存的样式在当前系统不可用时（例如在 Windows 10 上保存过“云母”）下拉框显示实际回退后的样式，`settings.json` 里的值保持不变。`MainWindow.ApplyPreferredBackdrop()` 在启动、设置变更和高对比度切换时幂等应用；`BackdropStyle.None` 不调用任何原生接口（Windows 7/8 上 `SetSystemBackdropType` 只允许 `None`，绝不能传入从未应用过的亚克力类型）。导航栏透明只靠覆盖 `NavigationViewDefaultPaneBackground` / `NavigationViewExpandedPaneBackground` 两个资源；不要再写遍历视觉树改 `SplitView.PaneBackground` 的 hack（模板重新应用后会失效，也会让 DynamicResource 不再更新）。`ui:WindowHelper.Acrylic10Color` 绑定 `Wam.Acrylic10TintColor`：iNKORE 会把这里的 alpha 再乘 0.8，默认的 `#40FFFFFF` 会让导航栏在深色窗口后面变成深灰、深色文字看不清，所以由 `AppSettingsService` 按主题下发接近不透明的主题底色。Windows 10 的旧式亚克力在拖动/缩放时会让 DWM 每帧重算整窗模糊，窗口明显发滞（实测拖动期间 DWM CPU 约翻倍、应用 CPU 多约 30%）：`MainWindow` 监听 `WM_ENTERSIZEMOVE`/`WM_EXITSIZEMOVE`，进入移动循环时移除亚克力并把窗口切成不透明底色（`Wam.MoveBackgroundBrush`，取亚克力在深色/浅色内容上的中间色），松手后恢复；Windows 11 的 Mica/Acrylic11 是 DWM 原生优化路径，不做处理。

窗口设置 `ui:TitleBar.ExtendViewIntoTitleBar="True"`，内容铺满整个窗口（标题栏按钮仍由窗口模板绘制在最上层），标题栏那块才能被系统背景或自定义壁纸的 `AcrylicPanel` 一起覆盖；`NavView.Margin` 由代码按 `TitleBar.GetHeight(this)` 下移一个标题栏高度。副作用是库在扩展模式下隐藏标题文字（`IconTitlePanel` 折叠），应用名由导航栏的 `PaneTitle` 展示。启用自定义壁纸时 `AcrylicPanel`（对 `BackdropSourceGrid` 做实时高斯模糊）负责导航栏与标题栏的磨砂效果，没有壁纸时保持 `Collapsed`，磨砂由系统背景提供。

### 6.2 页面职责

- `AppsInstallPage`：目录加载、搜索/排序/分类筛选/分页（每页 60/120/240）、按 `/sources` 的 `search_mode` 选择本地全量搜索（catalog）或自动服务端 API 搜索（api）（**不提供刷新按钮**，目录由后端独立线程/刷新策略更新，页面只在进入/源切换时读缓存）；分类来自 `/sources/{id}/categories`，失败时按目录聚合；分类选择通过 `SelectCategory(category)` 和事件通知 MainWindow；图标按行按需加载。
- `AppDetailsPage`：详情加载、下载直链显示/刷新、复制/打开链接、发起下载；截图相册（StoreMediaService 异步加载、点击打开 ScreenshotViewerWindow，视频直接使用详情返回的 `video_proxy_url`）；**不再显示下载进度面板**。已有任务时按钮为“查看下载”。
- `DownloadCenterPage`：所有下载任务的全局列表；总进度、分段带状进度、速度、剩余时间、安装包数字签名状态、取消/重试/移除/打开安装包/打开下载文件夹。
- `UninstallPage`：读取 Windows 卸载注册表和当前用户商店应用，按“应用程序 / 系统组件 / 隐藏组件 / 商店应用”四类展示，支持分类筛选、搜索、全选、逐项选择和批量卸载；不依赖后端。
- `StartupPage`：读取 Run/RunOnce、启动文件夹、登录时计划任务与 UWP 应用启动任务，支持来源筛选、搜索、排序、逐项启用/禁用、右键删除/浏览位置与新增；不依赖后端（见 6.14）。
- `SettingsPage`：主题、软件源（从服务端 `/sources` 动态加载）、服务端地址（应用/测试连接/恢复默认）、下载线程/超时/下载位置、GitHub 镜像、代理/证书、缓存、日志目录、设置存储。

### 6.3 卸载软件

当前实现中，桌面软件采用“控制面板式”的直接卸载；商店应用通过 `Remove-AppxPackage` 卸载。卸载结束后刷新已安装软件列表判断是否还存在；确认已从列表移除后自动打开 `DeepCleanWindow`（“强制卸载”），由用户手动勾选残留项，不再自动删除任何残留。

- `InstalledApplicationProvider` 读取 `HKLM` 的 64/32 位视图和 `HKCU` 的 `SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall`。**`RegistryView.Registry32` 打开 HKLM 得到的就是 `SOFTWARE\WOW6432Node\...\Uninstall`**（实测两者子键集合完全一致），因此 32 位软件的卸载项已经覆盖，不需要再单独写一条 WOW6432Node 的扫描路径；再加会重复。
- **卸载命令不能只看 `UninstallString`**：`ResolveUninstallCommand` 按 `UninstallString` → `QuietUninstallString` → 安装目录里的 `uninstall.exe` → `ModifyPath` 回退。JetBrains Toolbox 安装的 IDE 只写 `ModifyPath`（指向 toolbox），早年因此被当作“不可卸载”整条丢弃（实测漏 10 个）。回退后仍为空的条目继续丢弃。`ModifyPath` 兜底会打开厂商管理器（Toolbox），卸载窗口会等它关闭——这是这类软件唯一的卸载入口。
- **隐藏组件 = 没有 `DisplayName` 的卸载登记项**（2026-11 实测结论）：`DisplayName` 长度为空时写隐藏标志，`SystemComponent` 是另一个独立标志，两者互不替代。本项目按同一规则分类：`SystemComponent=1` → 系统组件；否则 `DisplayName` 为空 → 隐藏组件；其余 → 应用程序。**没有 `DisplayName` 的条目不再丢弃**，显示名回退到注册表键名（如 `Steam App 228980`、`{1D8E6291-...}.KB2533623`），故意不写 `DisplayName` 的安装器也能在“隐藏组件”里被看到并清理。`ParentKeyName` / `NoRemove` / `ReleaseType` 不参与分类（实测 76 条无 `DisplayName` 条目中这些值全为空），也不再读取。没有卸载命令的普通应用程序仍丢弃，系统/隐藏组件保留（仍可用于深度清理）。MSI 的 `Installer\UserData\<SID>\Products` 也是隐藏条目的来源之一，但隐藏视图的条目来自四个 `Uninstall` 位置的动态枚举，本项目不把该 MSI 来源并入已安装列表（只在残留扫描中查询 MSI 登记）。
- 以“名称 + 版本 + 卸载命令”去重；隐藏组件按“键名 + 安装路径”去重（64 位视图与 WOW6432Node 的镜像键同名同路径，合并成一条）。显示本机/当前用户、版本、发布者、大小、安装日期和安装位置，非应用程序行带“系统 / 隐藏 / 商店”徽章。分类切换在左侧导航“卸载软件”子项（`UninstallPage.UninstallCategories`，默认“应用程序”，无“全部”），`CategoriesChanged` 把四类数量发给 MainWindow；标题显示“已安装软件 · 当前分类”与各分类数量。
- **隐藏组件不提供常规卸载**：`InstalledApplication.CanUninstall` 对隐藏组件为 false；`UninstallPage` 在“隐藏组件”分类或选择包含隐藏项时隐藏工具栏“卸载所选”，行右键菜单去掉“卸载此软件”，`RunUninstallAsync` 也会拒绝隐藏项。隐藏组件只能对单个条目使用“强制卸载”，从而删除它的卸载键标识（键名，如 `{1D8E…}.KB2533623`）及相关残留。
- 排序是“字段 + 方向”：`SortCombo`（名称 / 发布者 / 大小 / 安装日期）+ `SortDirectionButton`；报表模式点列头（`ReportHeader_OnClick`，`Tag` 是字段名）等于“选字段 + 切换方向”，`UpdateSortIndicators` 同步 ▲/▼；搜索匹配名称、发布者、版本、安装位置、来源和分类名。
- 显示方式由 `DisplayModeCombo` 切换，三个模板都在 `Page.Resources`，只换 `AppsList.ItemTemplate`（和 `ReportHeader` 可见性），不重建数据：`Report`（默认）固定列宽表头（名称 / 发布者 / 版本 / 大小 / 安装日期 / 安装位置 + 官网 + 复选框），列宽须与 `ReportItemTemplate` 的 `ColumnDefinition` 一致，固定列由 `UninstallPage.Columns`（`ColumnLayout`，默认 210 / 150 / 80 / 78 / 96 / 46）统一提供，图标 30、安装位置 `*`、复选框 34 保持写死）；`List` 紧凑单行；`Card` 三行卡片；三者都显示图标（24px 或 36px），无图标回退字母徽章。拖动表头分隔条即可改列宽（见 6.14）。
- 安装日期用 `M月 d, yyyy` 格式（如 `8月 15, 2026`），无日期返回空字符串，**不要**写“安装日期未知”（`DetailsText` 自动省略）。官网取 `URLInfoAbout`，为空回退 `HelpLink`；有官网的行显示“官网”按钮，右键菜单同样提供，统一走 `ExternalLinkService`。
- 大小/日期缺失时由 `InstalledApplicationEnricher` 后台补：大小递归累加 `InstallLocation`，用 `FindFirstFileExW`/`FindNextFileW` 直接读目录项（单目录上限 8000 项或 400ms、跳过重解析点）；**猜出来的目录只用于取日期，不参与算大小**（`DisplayIcon` 可能指向共享的 Package Cache）。日期按注册表与目录时间兜底：注册表 `InstallDate` → `min(卸载键最后写入时间, 安装目录最后写入时间)`（`RegQueryInfoKey` + `GetFileAttributesExW`，取更早者）→ 留空；**补全不以 `InstallLocation` 非空为前提**，只有 `DisplayIcon` 的软件也要排队。商店应用不做文件系统探测，结果通过 `ApplyFilesystemProbe` 刷新。
- 安装目录由 `InstallLocationResolver` 解析：`InstallLocation` → `DisplayIcon` 所在目录 → 卸载程序所在目录；跳过 `msiexec` 这类相对路径命令、盘根和 Windows / Program Files / ProgramData / AppData / 用户目录等共享根。Provider 读 `InstallLocation` 时按 `InstallLocation` → `Inno Setup: App Path` → `InstallDir` 回退并展开环境变量。
- `UninstallPage` 的后台补全条件是 `InstalledApplication.NeedsFilesystemProbe`（缺大小或日期），**不要**加“安装位置非空”限制；只排队当前分类可见条目，切换分类会取消上一轮并重新排队；每条处理完 `Dispatcher.Yield(Background)`，探测结果用“所有属性已变”通知刷新行。
- 切换分类/排序时整体替换 `Apps`（只发一次 Reset），复选框只调 `RefreshSelectionSummary`；首次加载显示 `ui:ProgressRing`。列表必须用 `VirtualizedRowListStyle`（见 6.10 第 9 条）；补全按 `EnrichmentBatchSize`（8）成批应用，同一批在同一 dispatcher 回合写入；`SizeText`/`InstallDateText` 在模型缓存；行模板不放 `ToolTip` 绑定、官网入口用 `TextBlock`。
- 页面同时显示注册表桌面软件和当前用户 Microsoft Store/MSIX 应用，系统/隐藏组件默认不与普通软件混在一起。`StoreApplicationProvider` 在 Windows 10/11 调用 Windows PowerShell `Get-AppxPackage`（单一 net6 构建无 WinRT），解析 JSON 与每个包的 `AppxManifest.xml`，只保留安装位置在 `\WindowsApps\`、至少有一个带 `Id` 的 `Applications/Application` 的包，过滤 framework、resource package、bundle、stub；每个 manifest 应用入口一条记录，包仍是卸载和验证单位。Windows 7/8 返回空集合并由界面提示不支持。
- 商店应用名称优先取 `Get-StartApps` 返回的本地化入口名，再取对应 `VisualElements` 的 `DisplayName`；单入口才回退包级名称，多入口缺少入口名时显示包名和 Application Id。每行记录同包其他入口的名称/Id 与自身 `Application/@Id`，图标优先取该入口 VisualElements 的方形 Logo，并回退包级 `Properties/Logo`；安装日期优先读 `HKCR\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppModel\Repository\Families\<PackageFamilyName>\<PackageFullName>\InstallTime`，再回退安装目录创建时间。图标支持 PNG 并由统一后台加载器按需缓存。“商店应用”分类过滤出这些入口，搜索也匹配“商店应用”来源文字。
- 商店卸载命令由 `StoreApplicationUninstallCommand` 生成：按包标识名查找并用 `PackageFullName` 精确筛选，再执行 `Remove-AppxPackage -Confirm:$false`。只对当前用户生效，不用 `-AllUsers`，不保留应用数据，也不做预置包 deprovision。卸载批次按包全名去重，避免同包多入口重复执行；确认框说明同包入口会一起卸载并列出关联名称。
- 商店图标以 `AppxManifest.xml` 的 Logo 为基准，依次找同名 `.targetsize-48/44/64/32/24.png`、`.scale-200/150/125.png` 或 `_altform-unplated.png` 变体，候选缺失时继续尝试后续尺寸，最后才用 manifest 原始路径，避免小图或有效资源被前一个缺失变体挡住。
- 商店应用按包卸载，可能包含多个入口；卸载会删除该应用在本机的用户数据，确认框会说明。刷新快照和逐包查询都以 `PackageFullName` 为验证键，不能把每个入口的行 ID 当成独立安装状态。
- 用户选多个软件点“卸载所选”或行右键“卸载此软件”时先显示 `UninstallConfirmationWindow`：提示可中途停止但可能残留部分状态，列出全部软件并要求先关闭程序；右键菜单还提供打开安装位置、复制软件名称、复制卸载命令和刷新列表。
- 确认后显示模态 `UninstallProgressWindow`，按列表顺序逐个调用 `UninstallRunner.UninstallAsync(app, token)`；“停止卸载”、关闭窗口或 Alt+F4 都会先弹出 **二次确认**（默认按钮为“否”），确认后才取消当前批次、终止卸载程序及子进程，当前软件记为已取消、剩余记为已跳过；选择“否”时批次继续。
- `InstalledApplicationIconService` 读取 `DisplayIcon`（支持带引号路径、EXE/DLL 索引、`.ico`、负资源索引和环境变量），用 `PrivateExtractIcons` 提取 48px 图标，冻结后进程内缓存。图标由页面在行显示后后台加载，不阻塞注册表扫描；没有图标回退字母徽章（不加彩色底）。注册表软件图标**按需提取**（`ApplicationRow_OnLoaded` → `LoadQueuedIconsAsync`），不要批量取。
- 页面加载完成后在线程池做一次紧凑型 GC（`BclCompat.CompactCollect`：net8 用 Aggressive，net6 回退 Forced），把注册表扫描留下的短命字符串还给系统；实测省约 3 MB 私有内存，不要在滚动/排序等交互路径调用。
- `UninstallCommandParser` 把 `UninstallString` 拆成 `FileName`/`Arguments`，支持带引号路径、未加引号含空格路径、`MsiExec.exe /I{...}`、`.bat`/`.cmd` 与环境变量展开；命令行必须来自注册表，不能接受任意输入。
- `UninstallRunner` 用 `UseShellExecute=true` 启动（Windows 处理 UAC 与文件关联），把进程加入 `UninstallProcessJob`（作业对象），先等启动器退出再等作业内进程数归零，因此“启动器退出、卸载在子进程里”也会被完整等待；只有用户停止时 `TerminateJobObject`，失败回退 `Process.Kill(entireProcessTree: true)`。
- 退出码 0/3010/1641 视为结束，1602/1223 为用户取消，1603/1618/其他非零为失败。启动器退出后仍有子进程存活超过 1.2 秒时，非零退出码不判失败，按“已交接、请刷新列表确认”报告；`ExitCode` 因权限读不到时同样按“已结束”。
- 每批结束后**刷新一次已安装软件列表**（`UninstallProgressWindow` 的 `refreshInstalledApplications` 委托，桌面注册表 + 商店 PowerShell 合并快照），用 `Id`/`PackageFullName`/`RegistryPath` 身份判断每个软件是否还存在；该列表结论覆盖退出码，这是“卸载是否真的成功”的唯一依据。列表刷新失败才回退 `UninstallVerifier` 逐项核对；3010/1641 仍只提示重启。
- 工具栏“强制卸载”或行右键“强制卸载（清理残留）…”打开 `DeepCleanWindow`，结果树默认收起。`LeftoverScanner` 的静态扫描流程如下（只读）：
  - 注册表：在 HKCU 与 HKLM 的 64/32 位视图中从 `Software` 递归枚举；键名等于名称候选（显示名、去掉版本后缀的名字、安装目录名、卸载键名）时加入该键并**停止下钻**，`RegistryScanSkipList` 的 8 条系统分支（SideBySide、Fusion、Installer、App Paths、.NETFramework、Component Based Servicing、Wow6432Node、Classes）不进入；Uninstall 分支交给“卸载信息”组，避免重复列出。
  - 注册表侧另有卸载登记项、App Paths（默认值指向安装目录）、Run/RunOnce/`Explorer\StartupApproved` 值、Services；后两组是本项目扩展（常规扫描不遍历 `SYSTEM`，也不单独列启动值）。隐藏组件额外把自己卸载键的完整标识放进“卸载信息”（普通应用仍跳过自己的键），并用标识拆出的 GUID/KB 继续匹配 MSI 产品记录和 `SOFTWARE\...\Updates\...\KB…` 键。
  - 文件侧是安装目录、AppData/ProgramData/LocalLow/Programs 同名目录，以及开始菜单（含程序组）、桌面、Quick Launch 的快捷方式；命中规则是“目标在安装目录内或名称相同”。
  - MSI 侧按卸载键 ProductCode 查 `Installer\UserData\<SID>\Products`、`Installer\Products`（字节反序压缩 GUID），枚举 `UserData\<SID>\Components\<组件>`（仅组件值仍指向安装目录时加入，`MsiComponentKey`），以及 `Installer\Folders` 中与安装目录相同/前缀匹配的值。
  - 安装期监控记录与厂商自定义规则**未实现**：需要安装监控会话才有可信数据，而本项目不做安装监控，也没有驱动来源，因此只做静态扫描。
- 商店应用同样可深度清理：残留是 `HKCU\Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppModel\PolicyCache\<包族名>` 与 `...\AppModel\SystemAppData\<包族名>` 两类状态缓存，`LeftoverScanner.ScanStoreApplication` 按 `PackageFamilyName`（回退 `PackageName`）匹配，作为“商店应用注册表”分组返回（`StoreStateKey`）。
- 安装目录、AppData、开始菜单程序组递归展开成“文件夹 → 文件”树（每文件一行，带大小和完整路径）；上限 12 层、5000 条，跳过重解析点避免目录环；整树默认不勾选。
- 快捷方式优先按“目标是否落在安装目录内”匹配：`ShortcutResolver` 在专用 STA 线程用一个 `WScript.Shell` 批量解析 `.lnk`（等价 IShellLink），名称相同兜底。开始菜单程序组只有“一个链接且无其他程序”时整体给出，否则逐个列出；删链接后清理变空的上级目录。
- AppData 候选：`%LOCALAPPDATA%\<名称>`、`%APPDATA%\<名称>`、`%PROGRAMDATA%\<名称>`、`%LOCALAPPDATA%\LocalLow\<名称>`、`%LOCALAPPDATA%\Programs\<名称>`；名称候选来自显示名、去掉版本后缀的名字、安装目录名、卸载键的键名；隐藏组件额外加入键名中的 GUID 和 KB 号（普通应用仍排除纯 GUID 键名）。
- 危险分组（安装目录、用户数据目录、快捷方式、MSI 安装数据库）节点带 `IsRisky`，构造后 `IsChecked = false`，显示“谨慎”徽章并默认不勾选；工具栏“全选”会勾上它们，等于用户显式确认。注册表安全分组默认勾选。
- 删除由 `LeftoverCleaner` 分派并逐条重校验：注册表侧（`RegistryLeftoverCleaner`）只允许卸载项、App Paths、Services 单层子键、Run/RunOnce/`StartupApproved` 单个值、`Installer\Products` 或 `UserData\<SID>\Products` 单层产品键、`UserData\<SID>\Components` 单层组件键（`MsiComponentKey`）、`Installer\Folders` 单个值、`AppModel\PolicyCache|SystemAppData\<包族名>`（`StoreStateKey`，仅 HKCU），以及 `SOFTWARE` 下至少两级的软件数据键；`RegistryScanSkipList` 的 8 个系统分支（SideBySide、Fusion、Installer、App Paths、.NETFramework、Component Based Servicing、Wow6432Node、Classes）一律跳过。删除键时用 `DeleteSubKeyTree` 递归删除其子键/值，空键随之移除。文件侧（`FileLeftoverCleaner`）按“先文件、后空目录”：文件必须位于节点 `RootPath` 内且 `RootPath` 通过根目录校验；文件夹只有无子项时才删（`Directory.Delete(recursive:false)`），删文件后清理变空的上级目录，不会连带删除未勾选文件。盘根、`Windows`、`System32`、`Program Files` 本身、`%TEMP%` 和用户个人文件夹一律拒绝。
- 卸载结果文案保持简短（“已卸载 / 注册表中仍存在该项目 / 卸载失败 / 卸载已取消”），详细原因看退出码或汇总；批次结束或取消后刷新注册表并显示汇总；卸载程序弹出向导时用户需自行操作，本应用只等待。流程不做撤销、回滚或恢复点，不会自动重启，3010/1641 只提示需要重启。
- **卸载后手动深度清理**：批次结束且刷新列表确认软件不存在（`UninstallResult.IsSuccess && RegistryRemoved == true`，**不再要求退出码为 0**）时，`UninstallPage.OpenDeepCleanAfterUninstallAsync` 自动打开 `DeepCleanWindow`，不自动删除任何残留；安全项默认勾选，安装目录/AppData/快捷方式/MSI 等危险项保持默认不勾选，由用户逐项确认后点击删除。一个批次卸载多个软件时只打开一个清理窗口，按软件逐个扫描（`BusyText` 显示“正在扫描：软件名（1/N）”），把多组残留合并去重后展示；用户关闭窗口即表示跳过该批次的残留清理。
- 文件删除前由 `FileLockInspector` 用 `FileShare.None` 检查共享锁，再用 Windows Restart Manager（`rstrtmgr.dll`）找出占用进程并结束普通用户进程（System/smss/csrs/wininit/services/lsass/winlogon/svchost/dwm 等系统进程不结束），随后重试删除；仍失败才走 `MoveFileEx` 延迟删除。
- 深度清理删除文件、快捷方式或文件夹时，任何无法立即删除的项都会尝试按 Windows `MoveFileEx(path, null, MOVEFILE_DELAY_UNTIL_REBOOT)` 登记为“已安排重启后删除”，节点保留并显示该状态。清理结束后，`PendingRebootWindow` 列出所有“重启后删除”和“仍无法删除”的路径，并提供“立即重启”（`shutdown.exe /r /t 5`）和“完成”两个选项；立即删除、重启后删除和仍失败三种结果分别计数。注册表键和值无法安排重启删除，失败信息提示重启 Windows 或以管理员身份重试。
- 客户端清单 `app.manifest` 设置 `requestedExecutionLevel level="requireAdministrator"`，程序启动即请求 UAC 管理员权限；强制卸载需要结束占用进程、访问 HKLM 和 Program Files。

### 6.4 下载流程

1. 详情页点击“开始下载”：
   - 若已有该 app 的任务，直接返回/导航到下载中心。
   - 否则调用 `DownloadCenterService.Start(app)`，随后触发 `DownloadCenterRequested`，MainWindow 切到下载中心。
2. `DownloadCenterService` 在后台：用 `GetLatestReleaseAsync()` 取最新签名地址，创建 `DownloadTaskViewModel` 加入 `Downloads`，读取设置，经 `ApplyMirror` 改写 GitHub 地址、按 `AppSettingsService.ResolvedDownloadDirectory`（设置里的自定义下载目录，空 = 默认目录）建下载服务、`CreateHttpClient()` 建客户端后调用 `InstallerDownloadService.DownloadAsync()`，进度经 `UiProgress` 回 UI 线程。
3. 下载完成后任务标记完成，后台读取安装包的 Authenticode 数字签名（签发公司、证书有效期、是否通过 WinVerifyTrust），结果只展示、不阻止打开；**不会自动打开安装包**，用户在下载中心点击“打开安装包”。
4. 取消、失败、重试、移除由下载中心管理。**已结束的任务会持久化**到 `%LOCALAPPDATA%\WinAppsManager\download-history.json`（最多保留 200 条，原子写），启动时由 `DownloadCenterService` 静态构造函数恢复为可查看的历史任务；`DownloadTaskViewModel.Restore` 从 `DownloadHistoryRecord` 重建 `AppItem`/文件名/状态/签名信息。`ClearFinished` 和单项“移除记录”会同时删除历史文件中的对应记录。
5. 下载中心**每一行都能勾选**（`DownloadTaskViewModel.CanSelect` 恒为真）：顶部按钮是“全选 / 取消全选”和“删除选中 (N)”。删除前二次确认；`DeleteInstallersButton_OnClick` 先对选中的**正在下载**任务调 `DownloadCenterService.Cancel` 并 `await Task.Completion`，再调 `DownloadCenterService.DeleteInstallers` —— 该方法删掉有文件的安装包、移除记录，并清理未完成的续传分片，文件被占用时保留记录并在末尾提示。取消/失败的任务只有分片没有安装包，同样能被选中清理。可以“继续下载”的条目数显示在顶部摘要里，“重新下载/继续下载”共用一个按钮，文案来自 `RetryButtonText`。
6. 关闭窗口时 `MainWindow.OnClosing` 调用 `DownloadCenterService.CancelAllAsync()`，取消并等待活动任务后关闭。
7. 安装包默认保存到 `%LOCALAPPDATA%\WinAppsManager\Downloads`；设置页“下载位置”可改为任意绝对路径（支持 `%USERPROFILE%` 等环境变量，目录不存在时自动创建），只影响之后开始的任务；下载中心的“打开下载文件夹”打开当前设置的目录。

### 6.5 分段下载与校验

`InstallerDownloadService`：

- 支持单连接与 Range 分段；分段数受设置线程数、文件大小和 `MinimumSegmentSize` 限制，并检查 Range 支持、Content-Range、ETag/Last-Modified、Content-Length 与内容类型；无法保证分片一致、没有可靠 validator 且无 SHA256 时回退单连接。
- 每次连接/读取都使用 `InstallerDownloadOptions.Timeout` 作为超时。
- 允许的安装包后缀是 `.exe`、`.msi`、`.msix`、`.msixbundle`、`.appx`、`.msu`（`InstallerExtensions`）；后端元数据里后缀不在白名单时直接拒绝。`.msu` 是 Windows 更新独立安装程序包（CAB 容器），用于 360 等来源的系统补丁包。
- 下载完成后按容器校验，而不是一律查 PE 头：`.exe` 检查 `MZ` + `PE\0\0`；`.msi` 检查 OLE 复合文档签名 `D0 CF 11 E0 A1 B1 1A E1`；`.msu` 检查 CAB 签名 `MSCF`；`.msix/.msixbundle/.appx` 检查 ZIP 头 `50 4B 03 04`（`ValidateInstallerAsync`）。**不再校验服务端上报的 `SizeBytes`**；`Content-Length` 只用于进度与空响应保护，不要求与实际字节数一致。
- 完整传输后保留容器签名与可选 SHA256 校验，通过后才移到正式文件名；**不再比较服务端 SizeBytes/Content-Length 与实际字节数**（服务端可能算错大小），也不因大小不一致中止下载。通用 GitHub provider 只产出 `.exe/.msi/.msix/.msixbundle`，客户端另允许 `.appx` 与 360 补丁包的 `.msu`，但不要把 GitHub 资产写成 `.appx`。
- **断点续传**：未完成文件固定叫 `{安装包名}.part`（名字固定才能跨重启续传），旁边写 `{安装包名}.part.json` 记录 URL / 大小 / SHA256 / `ETag` / `Last-Modified`。**只有单连接下载的分片可以续传**：分段下载会先 `SetLength` 撑到全长（中间是空洞），这种半成品不连续，失败时必须删掉重下——`TransferState.Resumable` 就是干这个的，它在真正开始单连接读取**之前**置位，所以中途取消也留得住已下载的部分。续传走一条 `Range: bytes=<已有长度>-` + `If-Range: <validator>` 的请求并追加写入；服务器返回 200（忽略 Range）、`Content-Range` 起点或总长对不上、响应被压缩时，退回 `RestartFromScratchAsync` 删分片重下。没有 validator 也没有 SHA256 时干脆不续传。成功后移动文件并删掉 `.part.json`；`DownloadCenterService.Remove / ClearFinished / DeleteInstallers` 会调 `InstallerDownloadService.DeleteResumeFiles` 清掉分片，避免孤儿文件。
- 已有安装包不会被覆盖（重名时自动加序号）。
- 下载目录由 `InstallerDownloadService` 构造参数决定：`DownloadCenterService` 传入 `AppSettingsService.ResolvedDownloadDirectory`（自定义目录为空时用 `InstallerDownloadService.DefaultDownloadDirectory`），下载器构造时用 `Path.GetFullPath` 规整；设置页保存时校验绝对路径、展开环境变量并尝试创建目录，非法路径只回退该字段并给出 `LastError`。
- 默认线程数 4（1–32）、超时 10–300 秒（步进 10）；下载完不自动执行安装包，只有用户点“打开安装包”才打开。数字签名由 `AuthenticodeSignatureService` 在后台读取 `X509Certificate.CreateFromSignedFile` 并用 `WinVerifyTrust` 校验，展示签发公司/有效期/有效性；未签名、证书过期或校验失败只显示警告，绝不阻止打开。

### 6.6 网络设置

`DownloadNetworkService`：

- GitHub 镜像只对 `github.com`、`githubusercontent.com`、`githubassets.com` 及其子域生效。
- 地址规则：`镜像前缀 + 原始 URL`；前缀必须是 http/https，末尾自动补 `/`。
- 代理模式：`System`（系统代理）、`Direct`（直连）、`Custom`（HTTP/HTTPS 代理）。
- 自定义代理支持在 URL 中写 `user:password@host:port`，会转换为 `NetworkCredential`。
- `IgnoreCertificateErrors` 会设置 `SslOptions.RemoteCertificateValidationCallback = true`，只允许调试使用，UI 必须保留危险提示。
- 这些设置只影响之后的安装包下载任务，不影响后端 API。

### 6.7 设置持久化

`AppSettingsService` 使用 `%LOCALAPPDATA%\WinAppsManager\settings.json`，UTF-8 无 BOM，原子写入（临时文件 + Move）。

当前字段：

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `Theme` | `System` | `System` / `Light` / `Dark` |
| `BackdropStyle` | `None` | 窗口背景：`None`（不透明）/ `Acrylic` / `Mica` / `Auto`；系统不支持时按 `BackdropSupport.Resolve` 回退（自动 = 云母 → 亚克力 → 无），非法值读取时回退 `None` 并给出提示 |
| `AccentColor` | `""` | 主题色 `#RRGGBB`；空 = 默认蓝 `#2563EB`；非法值读取时只回退该字段并给出提示 |
| `BackgroundImagePath` | `""` | 自定义背景图绝对路径；必须是存在的 png/jpg/jpeg/bmp/gif/tif/tiff，非法值读取时回退纯色背景 |
| `BackgroundImageOpacity` | `0.35` | 背景图叠加不透明度，0.05–1 |
| `DownloadConnections` | `4` | 1–32 |
| `DownloadTimeoutSeconds` | `30` | 10–300 |
| `DownloadDirectory` | `""` | 空 = `%LOCALAPPDATA%\WinAppsManager\Downloads`；否则必须是绝对路径（启动时只校验不创建，保存时创建） |
| `GitHubMirror` | `""` | 空 = 直连；否则 http(s) 前缀 |
| `ProxyMode` | `System` | `System` / `Direct` / `Custom` |
| `CustomProxyUrl` | `""` | http(s) 代理地址 |
| `IgnoreCertificateErrors` | `false` | 危险，仅调试 |
| `SourceId` | `""` | 服务端 `/sources` 下发的来源 ID；空=跟随服务端默认源；旧 `AppSource`（Lenovo/BuiltIn/Soft360）仅读取时一次性迁移 |
| `BackendBaseUrl` | `https://winapps.theconsole.top/` | 后端服务地址；必须是 http/https 绝对地址、无 userinfo/query/fragment，保存时规整末尾斜杠；旧版默认 `http://127.0.0.1:8080/` 读取时自动迁移；切换后重建 HttpClient 与按服务端隔离的缓存 |
| `EnableFileLogging` | `true` | 是否写客户端文件日志；关闭后只输出 Debug/Trace，保存后立即生效 |
| `VerboseLogging` | `false` | 是否记录 Verbose 级别（分段下载、页面细节等）；保存后立即生效，`EnableFileLogging=false` 时无意义 |

修改设置模型时：

1. 更新 `Models/AppSettings.cs`。
2. 更新 `AppSettingsService` 的读取迁移、范围/URL 校验、保存方法。
3. 更新 `SettingsPage` 控件、事件和状态文字。
4. 更新 README（客户端不再有设置测试项目，改动靠构建与手动检查验证）。
5. 保持旧 `settings.json` 可迁移；单字段损坏只回退该字段并给出 `LastError`。

### 6.8 线程模型

- 所有 `ObservableCollection` 和 WPF 控件只在 UI 线程修改。`DownloadCenterService` 使用 `OnUiAsync` 把进度/终态切回 Dispatcher；`InstallerDownloadService` 内部使用 `ConfigureAwait(false)`，不要从后台线程直接触摸页面控件。异步事件处理器（`async void`）只用于 UI 事件，内部异常必须捕获或转为任务状态。取消使用 `CancellationTokenSource`；共享下载任务不能被单个请求的取消影响。

### 6.9 主题与 UI 约定

- 颜色/画刷统一使用 `Wam.*` 动态资源，禁止硬编码浅色/深色颜色。
- 新增主题资源时同时更新：
  - `App.xaml` 的默认值
  - `AppSettingsService.UpdateThemeResources` 的浅色、深色、高对比度分支
- 现有资源：`PageBackgroundBrush`、`CardBackgroundBrush`、`CardBorderBrush`、`TextPrimaryBrush`、`TextSecondaryBrush`、`AccentBrush`、`AccentSoftBrush`、`SubtleBackgroundBrush`、`SuccessBrush`、`DangerBrush`、`ProgressTrackBrush`、`IconPlateBrush`、`Acrylic10TintColor`（Color）、`MoveBackgroundBrush`、`WindowWallpaperBrush`、`PaneTintColor`（Color）、`PaneAcrylicVisibility`（Visibility）。
- 页面标题/卡片优先复用 `Wam.*` 样式。加载状态使用 `ui:ProgressRing`（`IsActive="True"`、`IsIndeterminate="True"`），不要恢复旧的横条 ProgressBar；状态色用 `SuccessBrush` / `DangerBrush` / `AccentBrush` / `TextSecondaryBrush`。

### 6.10 XAML 常见陷阱

1. **只读属性的双向绑定**：`ProgressBar.Value` 默认 TwoWay，绑定只读 ViewModel 属性必须写 `Mode=OneWay`，否则运行时抛 `XamlParseException`。
2. **ProgressRing**：需要 `IsActive`；不确定模式显式设置 `IsIndeterminate="True"`。
3. **NavigationView 动态子项**：见 6.1，不要随意选中分类子项。
4. **TextTrimming**：在 `WrapPanel`/水平 `StackPanel` 中不会自动获得宽度约束，给 `MaxWidth` 或放进 `Grid` 列。
5. **x:Static 到 Slider 数值属性**：曾导致运行时设置 `RangeBase.Minimum` 异常；滑杆范围用字面量并在构造函数里从 `InstallerDownloadOptions` 覆盖，或使用可转换的普通值。
6. **主题刷新**：`AppSettingsService.UpdateThemeResources` 在启动/主题变化/高对比度变化时替换资源；新增资源必须覆盖所有分支。
7. **图标**：使用 `CachedIconConverter` 和 `AppItem.DisplayIconSource`；图片失败时清理缓存并显示文字徽章。
8. **UI 线程**：不要把 `Task.Wait()`/`.Result` 放在 UI 线程；不要阻塞 Dispatcher。
9. **ListBox 虚拟化会被主题模板悄悄关掉**：iNKORE 的 ListBox 模板里 `ScrollViewer` 没开 `CanContentScroll`，即使设置 `VirtualizingPanel.IsVirtualizing="True"` 也会实例化**所有行**（504 条排序卡 ~1 秒）。修法是自建模板（`VirtualizedRowListStyle`：`ScrollViewer CanContentScroll="True"` + `ItemsPresenter`）并显式 `ItemsPanel` 为 `VirtualizingStackPanel`；实测 138/138 → 13/138、1,000 ms → 84 ms。**新增长列表必须验证容器数量**。
10. **ComboBox 在同行里会比其他控件高 3px**：iNKORE 的 ComboBox 默认贴顶排列，和 `Height="36"` 的 `TextBox`/`Button` 放在同一行时边框比邻居高 3px（软件列表工具栏曾如此）。修法是给 ComboBox 显式写 `VerticalAlignment="Center"`（`UninstallPage` 一直这么写，`AppsInstallPage` 之前漏了）；新增工具栏行时每个定高控件都要写。

### 6.11 客户端图标缓存

`ClientCacheStore` + `CachedIconConverter` 管理 `%LOCALAPPDATA%\WinAppsManager\Cache` 下的图标，XAML 只绑定 `AppItem.DisplayIconSource`：

- **写盘失败不丢图标**：落盘失败（磁盘、杀软、ACL）时字节保留在进程内（默认最多 100 个 / 16 MB，FIFO 淘汰），`DisplayIconSource` 用 `memory://icon/<key>`，`CachedIconConverter` 从内存解码；下次读取会再试落盘，成功后释放内存条目。
- **ETag 条件缓存**：每个图标旁写 `<key>.meta.json`（`etag`/`last_modified`/`checked_at_utc`）；`IconRevalidateInterval`（默认 24 小时）内直接读文件，过期后发 `If-None-Match`/`If-Modified-Since`：304 只刷新元数据、200 替换文件、超时或非 2xx 继续用旧文件。
- **ICO 帧选择**：`IconImageFormat` 解析 ICO 目录选最大帧，PNG 帧直接解码、DIB 帧重封装后交给 `IconBitmapDecoder`，失败才回退整个容器；7zip、notepadplusplus 实测正常显示。
- **联想/360 源图标直连 CDN**：列表/搜索的 `icon_url` 是 CDN 绝对地址（qhimg/qhmsg/qhres、联想 CDN、任意厂商 CDN 均可），`ClientCacheStore.TryIconUri` 不再维护固定域名白名单，改为安全公网 URL 校验（拒绝 localhost/私网 IP/userinfo/内网后缀），图标 HttpClient 固定 `User-Agent: WinAppsManager/1.0`（联想 CDN 拒绝 curl 类 UA）；`EnsureIconAsync` 先直连、失败再回退后端 `icon_proxy_url`；支持 png/ico/jpg/jpeg/gif/bmp。内置源图标仍走后端 `/api/v1/apps/{id}/icon` 预热。
- **按需加载**：列表行加载时调用 `EnsureIconAsync`（并发上限 6）；`StoreMediaService` 负责详情截图/封面，允许当前服务端 `/api/v1/sources/` 代理路径与任意安全公网图片地址（直连优先、失败回退 `proxy_url`/`cover_proxy_url`），内存 LRU 按解码后像素字节记账、相册缩略图按 640 宽解码，磁盘缓存 `%LOCALAPPDATA%\WinAppsManager\Cache\store-media`。**视频不经过 StoreMediaService 缓存**：`ScreenshotViewerWindow` 用 `MediaElement` 播放详情返回的 `video_proxy_url`（Range/206），播放失败显示错误并可用浏览器打开同一地址；窗口会因 MediaElement 与 `AllowsTransparency` 不兼容而在视频相册时使用不透明背景。

### 6.12 平台与架构适配

- 单目标没有编译期差异：运行时用 `OperatingSystem.IsWindowsVersionAtLeast(10)` 判断商店应用是否可用，Windows 10/11 调用 Windows PowerShell `Get-AppxPackage`，Windows 7/8 返回空列表并通过 `UnsupportedReason` 提示；不再使用 `STORE_APPS` 常量或 WinRT。
- `Services/PlatformCompatibility.cs` 按 `Environment.OSVersion.Version`（Win7=6.1、8=6.2、8.1=6.3、10=10.0、11=10.0 build≥22000）与 `RuntimeArchitecture` 判断 `minimum_windows`/`architectures`，字段缺失视为兼容。
- `AppCatalogService.PublishCatalogAsync` 过滤不兼容条目并记 `HiddenIncompatibleCount`，`AppsInstallPage.ResultsCount` 追加“（已按当前系统隐藏 N 款不兼容的软件）”；磁盘目录缓存写**完整**目录，系统升级后能重新显示。
- `UninstallPage` 在 `StoreApplicationProvider.IsSupported == false`（Windows 7/8）时跳过商店扫描并在状态栏追加 `StoreErrorSuffix`；Windows 10/11 商店包通过 `Get-AppxPackage` 正常枚举与核对。
- 兼容层：`BclCompat.cs` 收敛 net7/net8 API（`SnakeCaseLower`、`ReadExactlyAsync`、流式 SHA256、`CompactCollect` 等），`Polyfills.cs` 补 `RequiredMemberAttribute` 等编译器标记；`[GeneratedRegex]` 改为静态 `Regex` 字段。

### 6.13 界面动效

`Services/UiMotion.cs` 是客户端动效的**唯一入口**：页面切换过渡、导航分类项淡入、弹窗打开、相册换图都从这里调用；页面/弹窗里不要再各自写 `Storyboard`，时长与缓动（`FadeEasing`/`DecelerateEasing`）也集中在这里。

  全项目的动效只保留**透明度变化**：页面切换交叉淡化、导航项淡入、弹窗淡入。位移与缩放都已移除——实测它们会和内容异步加载时机打架（先动再硬切）、或让文字在动画期间重新栅格化。

  **列表行不做入场动画**：曾给软件列表/下载中心/卸载列表加过交错淡入（`PlayListRowReveal`，按 index 错开 22ms、上限 14 行）。上限之外的行不动，屏幕上就会出现“上半部分在动、下半部分静止”的分裂观感，搜索/筛选时尤其刺眼。现在三处列表都直接显示，`PlayListRowReveal` 与 `MaxRevealRows`/`RevealStaggerMilliseconds`/`ListRowDuration` 均已删除；筛选/排序/搜索只换数据，不加动画。

- **动效恒定开启，没有任何开关**：应用内的 `AppSettings.EnableAnimations` 和 Windows 的“在 Windows 中显示动画”判断都已移除。原因：`MinAnimate=0` 会让 `SystemParameters.ClientAreaAnimation` 变 false，但 Windows 设置界面不一定同步显示，用户完全查不出“动画没了”的原因。界面动效是本项目特性而非可选项，`AnimationsEnabled` 现在恒为 `true`，设置页只在 `AnimationHintText` 里说明会有什么效果。各方法仍保留禁用分支（现在走不到），保证元素不会被留在半透明或偏移的中间状态。
- **页面过渡必须基于位图**：`ShowPage` 的顺序是「`CompleteActiveTransition()` 收尾上一轮 → 抓旧页面快照 → `ShowTransitionPreview` 盖上旧页面 → 换 `FrameView.Content` + `UpdateLayout()` → 抓新页面快照 → `PlayPageTransition`」。过渡期间 `PageHost` 置 `Visibility.Hidden`，只对 `OutgoingSnapshot`/`IncomingSnapshot` 两张 `Image` 做位移与淡入淡出。
  - **不要改回“直接给实时页面做动画”**：页面里的列表实例化、图标解码和内部动画会占住 UI 线程，实测最大帧间隔接近 300 ms（每帧明显停顿）；改成位图后稳定在 10–11 ms，且页面越复杂也不会更卡。
  - 必须把**裁剪**（外层 `ClipToBounds`）与**动画目标**（内层 `PageHost`）分成两层，否则 WPF 按未变换的边界裁剪再整体平移，滑入的位图会画到左侧导航栏上。
  - 快照是同步软件渲染（1080×760 约 6–10 ms），只在真的要播过渡时抓；抓不到（首次布局、渲染失败）就退回“上移 + 淡入”。
- **只有进出详情页是层级关系**：`ResolvePageTransition` 现在只剩 `Fade` 与 `Backward` 两种结果——导航栏同级切换一律 `Fade`（交叉淡化），从详情页回列表是 `Backward` 但同样只做淡化；横向滑动整条链路（`NavForward`/`NavBackward`/`Drill*` 常量）已删除。
- **重复导航回调**：iNKORE `NavigationView` 对同一次点击可能先后触发 `SelectionChanged` 与 `ItemInvoked`，`ChangePage` 会被调用两次。用 `_pageTransitionPlayedFor` 忽略第二次，否则动画会连着播两遍，看起来像“卡一下再重播”。
- **帧率诊断**：`BeginFrameProfile`/`EndFrameProfile` 用 `CompositionTarget.Rendering` 统计帧数与最大帧间隔，只在 `VerboseLogging` 开启时生效并在日志里输出 `过渡帧率：…`；判断动画是否卡顿看这条日志，不要靠肉眼猜。注意统计必须在 `CompleteActiveTransition()` **之后**启动，否则会被上一轮的收尾立刻关掉（日志表现为“共 0 帧”）。
- 设置页的缓存统计（`ClientCacheStore.Statistics()` 要枚举数千个图标文件）已移到线程池 + `_cacheStatisticsGeneration` 代次校验：在 UI 线程同步统计会阻塞上百毫秒，正是早先“切到设置页卡一帧”的原因。

### 6.14 自启动项

`StartupPage` 列出的来源与“任务管理器 → 启动”页一致，覆盖四类：

- **注册表**：`HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Run` 与 `RunOnce`、`HKCU` 的同名两键，以及 64 位系统上 `HKLM` 的 32 位视图（`RegistryView.Registry32` 打开后就是 `WOW6432Node`，32 位程序把自启写在那里）。HKLM 的 Run/RunOnce 两个键要**分别按 64 位视图和 32 位视图各读一遍**，只读默认视图会漏掉 32 位软件。
- **启动文件夹**：`Environment.SpecialFolder.CommonStartup` 与 `.Startup` 下的 `*.lnk`，命令行由 `ShortcutResolver.ResolveTargets` 一次批量解析得到（目标 + 参数）。
- **计划任务**：`%SystemRoot%\System32\Tasks` 下的任务定义文件，要求同时具备 `LogonTrigger` 与 `Exec` 动作（与参考实现 UninstallTool 的判定一致）。读取只解析 XML，不启动进程。**`\Microsoft\` 分支一律跳过**：那里是 ClipESU、DirectXDatabaseUpdater、LicenseImdsIntegration 这类 Windows 维护任务，任务管理器同样不显示，列出来只会把真正的自启软件淹没（实测本机会因此少 19 条噪音）。任务定义里的 `RegistrationInfo/Description`（任务计划程序“常规”页的描述）会作为鼠标悬停提示。
- **应用启动任务（UWP）**：`HKCU\Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppModel\SystemAppData\<包族名>\<任务 Id>\State`，跳过 `PSR` / `Schemas` 两个包级键。`State` 是 `StartupTaskState`：0=Disabled、1=DisabledByUser、2=Enabled、3=EnabledByPolicy，**2/3 视为已启用**。这条来源补上了任务管理器里 TranslucentTB、Xbox、Windows Terminal Preview、Teams 这类商店应用（实测本机 5 条）。这类条目没有 exe：名称、发布者与图标由 `StoreApplicationProvider.ResolvePackageSummaries` 按包族名解析——开始菜单的本地化入口名（`Get-StartApps`，形如 `终端预览` / `ChatGPT` / `XBOX`）+ `AppxManifest.xml` 的 `PublisherDisplayName` 与 Logo 变体，结果按包族名缓存在进程内（首次扫描约 +0.5 s，之后直接复用）。解析时**必须跳过拆分包**：同一族会同时注册 `…_neutral_split.scale-400_…` / `…_neutral_split.language-zh-hans_…`，它们没有自己的 `AppxManifest.xml`，标记是 `_split.` 而不是 `.split.`；包目录按 `%ProgramFiles%\WindowsApps\<包全名>` 猜（注册表里没有安装路径，应用装在别的盘时只能回退包名与字母徽章）。右键对这类条目多一项“在 Microsoft Store 中查看”（`ms-windows-store://pdp/?PFN=…`，由 Store 的清单注册，`ShellExecute` 直接打开），同时隐藏“浏览程序位置…”。

**显示名与公司（`ExecutableMetadataService`）**：列表的“名称”不是登记名——取命令行里**主程序**（不是参数里提到的另一个程序）exe 的版本信息 `FileDescription`（资源管理器“详细信息”里的“文件说明”），取不到才回退注册表值名／快捷方式名／任务名；“公司”列取同一次读取的 `CompanyName`，没有就留空。“命令行”列始终显示真实命令，所以同名条目仍能分辨。实现要点：`ResolveExecutablePath` 处理带引号、不带引号（在第一个 `.exe` 处截断）与只写文件名（`rundll32.exe` 这类先在 System32、Windows 目录里找，再走 `PATH`）三种命令行；`Read` 按路径缓存 `FileVersionInfo`（同一 exe 常出现多次），并把 `TODO: <檔案說明>` 这类没填的占位符当空值。**悬浮提示**（`StartupItem.NameToolTip`）：计划任务显示任务描述，显示名与登记名不同时再补一行“登记名：…”，两者都没有时返回 `null`（不显示空气泡）。

**启用/禁用机制**（关键，别改成删了再写回）：注册表与启动文件夹项一律写 `SOFTWARE\...\Explorer\StartupApproved\{Run|Run32|StartupFolder}` 的 12 字节二进制值——首字节 `0x02` = 启用，`0x03` = 禁用，其后 4 字节偏移处是可选的“禁用时间” FILETIME（`BitConverter` 小端）。判定禁用用 `(data[0] & 1) != 0`；值不存在等价于启用。这是 msconfig 与任务管理器共用的机制，因此**不需要自己的旁路状态文件**，条目本身始终保留，随时可以还原。`Run32` 只服务于 HKLM 的 32 位视图，启动文件夹项用**含 `.lnk` 的文件名**作为值名（实测 `Ollama.lnk` 这类）。计划任务的开关是任务定义里的 `<Settings><Enabled>`，用 `schtasks /Change /ENABLE|/DISABLE` 修改、读 XML 回读；UWP 启动任务写 `State`（禁用写 1，启用写 2，`EnabledByPolicy` 拒绝修改），与 Autoruns 的做法一致，不需要 WinRT。

- 每次启用/禁用后都**回读实际状态**；回读与写入不一致按失败上报，并把界面复选框同步回真实值（`StartupPage` 的复选框是 `Mode=OneWay` 绑定，写操作失败时不会停在用户点出来的状态）。
- “删除”才真正移除登记：注册表项删值并清掉对应的 `StartupApproved` 记录（否则留下孤儿审批项），启动文件夹项删 `.lnk`，计划任务用 `schtasks /Delete`。UWP 启动任务由应用清单声明，`CanDelete=false`，只能启用/禁用。
- “添加”支持 8 种落点（`AddStartupItemWindow` → `StartupItemService.AddAsync`）：所有用户/当前用户的 `Run` 与 `RunOnce`（含 HKLM 32 位视图）、两个开始菜单启动文件夹、登录时执行的计划任务；32 位 Run 只在 64 位系统上单独列出（`StartupSourceMetadata.AddableSources`）。注册表项写成 `"路径" 参数` 的 `REG_SZ` 值；启动文件夹用 `ShortcutResolver.TryCreateLink`（WScript.Shell，STA 线程）建 `.lnk`；计划任务用 `StartupTaskProvider.Create`（`schtasks /Create /SC ONLOGON`），创建后以任务定义文件回读确认。三种落点都先查重名，并清掉同名的旧 `StartupApproved` 记录（否则新条目一出现就是禁用状态）。名称用 `Path.GetInvalidFileNameChars` 校验，反斜杠也会被拒绝（计划任务不允许在这里建目录）。
- **未覆盖**：`RunServices`、`Winlogon\Userinit`、驱动服务等更底层的自启点，以及计划任务里非登录触发（开机、空闲、定时）的任务；`LeftoverScanner` 的“启动项”分组只是按软件名匹配残留，与本页无共用代码。
- **界面约定**：报表列固定为 名称 / 来源 / 公司 / 命令行 / 状态 / 启用（状态 70、启用 60 写死，命令行 `*`）。行尾的复选框就是启用开关（勾选=启用），页面**不提供“全部启用/全部禁用”批量操作**——自启动项影响开机行为，逐个确认更安全。右键菜单固定四项：`启动时禁止运行`/`启动时允许运行`（同一个 `Tag="toggle"` 项按状态改 Header）、`从列表中删除`、`浏览程序位置…`（资源管理器 `/select`）、`浏览来源…`（注册表项先写 `HKCU\Software\Microsoft\Windows\CurrentVersion\Applets\Regedit\LastKey` 再启动 regedit；启动文件夹用资源管理器选中；计划任务见下面一段）。
- **计划任务的“浏览来源”**（`Services/TaskSchedulerNavigator.cs`）：打开 `taskschd.msc` 后把左侧目录树展开到任务所在文件夹，并在中间列表里选中任务行。**不要用 UI Automation**：任务计划程序的左树是 MMC 里的 `SysTreeView32`，它没有 UIA 提供程序，`AutomationElement` 只能拿到一个空 Pane（既没有 Tree 也没有 TreeItem，实测 `FindAll(Descendants, TreeItem)` 恒为 0），`NavigateToScheduledTask` 那套写法注定失败。改用 MSAA（`oleacc.dll` 的 `AccessibleObjectFromWindow`）：`get_accName`/`get_accState` 读节点，`accSelect` 选中、`accDoDefaultAction` 展开（节点的默认动作就是展开/折叠）。两个坑：① `IAccessible` 的声明必须补齐 IDispatch 的 4 个槽位再按 `oleacc.h` 顺序列全成员（vtable 对齐）——只声明用到的那几个方法时 `get_accState` 会返回 `null`（状态位全 0，选择/展开判断全部失效），`accSelect` 甚至直接访问越界；② MMC 的树**只显示文件夹**，任务本身是选中文件夹后中间 `SysListView32` 里的行，所以定位目标是“任务所在文件夹”，并且要用键盘（聚焦列表后 `HOME` + `↓`）选中任务行——列表的 WinForms 提供程序对 `accSelect` 只返回 `S_OK` 而不真正改变选择。切换文件夹时 MMC 会重建中间视图，必须每轮重新找窗口取 `IAccessible`，缓存旧窗口会永远读不到新行。任务计划程序每启动一次都新开一个 MMC 窗口，定位优先用本次启动的进程号对应的窗口（进程已退出或被转交给已有实例时才退回现有窗口），否则会把旧窗口展开给用户、新窗口停在根节点。
- **列宽可拖动**：表头每列右边缘放一个 6px 的 `Thumb`（`ColumnThumbStyle`），拖动时改页面上的 `ColumnLayout`；表头与行模板的 `ColumnDefinition.Width` 都绑定到同一个 `ColumnLayout` 索引器。自启动项的 名称 / 来源 / 公司 三列可拖（默认 260 / 180 / 150）。ColumnDefinition 不在可视化树里，用不了 `RelativeSource`，所以通过 `BindingProxy`（Page.Resources 里的 Freezable）取得页面对象，路径写成 `Data.Columns[n]`。`ColumnLayout` 用 `"Item[]"` 通知刷新所有索引器绑定。卸载页的报表模式复用同一套做法（6 列）。
- 图标按行按需提取（`StartupRow_OnLoaded` → `LoadQueuedIconsAsync`），路径来自 `ExecutableMetadataService.ResolveExecutablePath`（同一份解析结果也用于取名称与公司）；应用启动任务没有可执行文件，直接回退字母徽章。
- 页面用 `VirtualizedRowListStyle`（见 6.10），排序/搜索/筛选整体替换 `ObservableCollection`；单条切换后只刷新计数、且仅在按“状态”排序时才推迟一个 dispatcher 回合重建行集合（避免在 CheckBox 事件里替换 ItemsSource）。行模板只有一个 `ToolTip` 绑定（名称列的 `NameToolTip`，只有计划任务或改名条目才有非空值），其余单元格仍不放提示。

### 6.15 客户端日志

`Services/AppLogService.cs`：文件日志写到 `%LOCALAPPDATA%\WinAppsManager\Logs\app-yyyy-MM-dd.log`（UTF-8 无 BOM，按天分文件、单文件 5 MB 后追加序号、保留 14 天），API 为 `Initialize/Shutdown/ApplySettings/Info/Warn/Error/Verbose/OpenLogFolder/GetLogStatistics/CurrentLogFilePath`；`MinimumLevel` 默认 Info，`VerboseLogging` 开启后记录 Verbose；`Enabled` 表示是否写文件（关闭后只输出 Debug/Trace）。设置加载前产生的日志先最多缓存 500 条，首次 `ApplySettings` 时决定落盘或丢弃，因此关闭文件日志时首次启动不会创建日志文件。`App.OnStartup` 最先初始化并注册三个未处理异常钩子（只记录、不改崩溃行为）；目录加载/在线搜索/源与服务端切换、详情与下载地址解析、下载任务生命周期、卸载与深度清理扫描和批次结果、设置加载警告/保存失败、页面切换、窗口关闭均已接线；分页/筛选/滚动/每次进度更新不记录，避免刷屏。设置页日志卡片可查看路径、文件数与大小，并提供启用文件日志、Verbose 开关、“打开日志目录”“复制路径”。**日志禁止记录签名 URL、代理认证信息、GitHub 令牌、证书配置或安装包内容**；只记录应用名/id、阶段、字节数、耗时、路径、主机名、异常类型与消息。日志文件不要提交。\n\n设置保存和软件源切换可能在线程池线程触发 `SettingsChanged`/`SourceChanged`；UI 订阅者必须先 `Dispatcher.CheckAccess()` 后切回 UI 线程再更新控件（`SettingsPage.OnSettingsChanged` 已这样做），否则会出现“调用线程无法访问此对象”。软件源切换失败必须在 `SettingsPage.SwitchAppSourceAsync` 的 catch 中用 `AppLogService.Error` 记录来源 ID、异常类型和消息，不能只显示在状态栏。

## 7. 测试约定

### 7.1 后端

- 使用 `unittest` + `fastapi.testclient.TestClient`，注入 provider、临时目录与缓存，禁止访问真实官网/腾讯服务。
- 覆盖缓存命中/过期/并发/取消/清理、持久缓存损坏/过期、配置迁移/环境变量、API 错误码、图标 ETag/304、provider 协议异常。
- 新增缓存/配置字段必须有测试；启动校验失败路径也要测。
- 预热/可用性、陈旧快照、定时刷新、API 层预热回退与逐 app 隔离分别由 `tests/test_warmup.py`、`test_stale_cache.py`、`test_scheduler.py`、`test_startup_refresh.py` 覆盖。
- `tests/test_api.py` 的测试工厂默认关闭启动预热与定时刷新（`RefreshConfig(on_startup=False, interval_seconds=0)`），以保持 provider 计数确定；**不要**在通用 API 测试里重新打开预热。
- 通用 GitHub provider 由 `tests/test_github_provider.py` 覆盖资产正则、令牌头、错误映射、超时、协议校验、仓库大小写、`resolve_download()` 身份及条件请求/304/`refresh_interval_seconds` 门控；只用 `httpx2.MockTransport`，**禁止真实网络和令牌**。
- 条件缓存与国内 provider：`tests/test_github_cache.py` 覆盖缓存往返、损坏忽略、计数器、写盘失败降级与 `create_app` 接线；`tests/test_ai_providers.py` 覆盖三个国内 provider 的协议、回退与超时映射；`tests/test_provider_registry.py` 断言内置专用 provider 为 11 个、`apps.json` 的 8 个是其子集。
- 目录合并由 `tests/test_catalog_merge.py` 覆盖 `apps.d` 合并、缺目录、跨文件重复 id、`extra_directory` 注入、schema 与图标校验；`create_app(..., extra_directory=...)` / `CatalogRepository(..., extra_directory=...)` 是隔离手段。
- `tests/test_config.py` 覆盖 `github` 段令牌默认关闭、提交 `config.json` 不含令牌、`config.local.json` 深合并、环境变量优先级、非法令牌/`api_base`/`refresh_interval_seconds`；`tests/test_api.py` 覆盖 `/api/v1/cache` 令牌脱敏。

### 7.2 客户端

- 客户端**没有**自动化测试项目（原有 5 个控制台测试项目已移除），不要假设可以通过 `dotnet run` 运行客户端测试。
- 客户端改动至少用 `dotnet build WinAppsManager\WinAppsManager.sln -c Release` 验证唯一目标：0 警告、0 错误。
- 涉及下载、设置、卸载命令、图标路径等纯逻辑的改动，靠代码审查与必要的手动检查确认；确实需要回归测试时先与用户确认是否恢复测试项目。
- 手动验证时不要访问真实下载地址、不要执行安装包、不要真正卸载用户软件；临时目录和 `.part` 文件用完清理。

### 7.3 UI 检查

默认不启动客户端做额外冒烟测试，也不把临时截图、UI 自动化脚本提交到仓库。仅在用户明确要求或改动涉及无法自动验证的导航/XAML 运行时行为时，做一次最小手动检查并在完成后关闭客户端。

### 7.4 GitHub Actions

- `.github/workflows/build.yml`：每次 push（所有分支）和 pull request 构建 WPF 客户端，并在 Ubuntu + Python 3.12 上跑后端 unittest；客户端构建产物保留 7 天。
- `.github/workflows/release.yml`：仅手动触发，输入 `tag`（默认 `v1.0.0`），构建 `win-x64` / `win-x86` 自包含版本（内置 .NET 6 Desktop Runtime），压缩成 zip 并创建 GitHub Release。
- 发布版本不需要目标机器安装 .NET Runtime；发布 workflow 需要 `contents: write`，标签必须符合 `vX.Y.Z` 格式。
## 8. 常见任务配方

### 8.1 新增后端软件

先判断来源类型，再决定要不要写代码：

**GitHub Releases（无需写 Python）**

1. 在合适的 `Backend/data/apps.d/<group>.json` 追加条目，`download_provider` = app id，`provider_spec` 写仓库、tag 前缀和资产正则；完整字段、正则示例、`sort_order` 区间和图标要求见 `Backend/data/apps.d/README.md`。
2. 把图标放进 `Backend/icon/`，在 `Backend/icon/sources-<group>.md` 记录官方来源、格式尺寸、字节数和 SHA256。
3. 用真实 GitHub API 跑一次 `get_releases(force_refresh=True)` 验证每个架构恰好匹配一个资产；令牌只从 `config.local.json` 或环境变量读取，不打印（脚本见 `apps.d/README.md`）。
4. 新资产命名形态在 `Backend/tests/test_github_provider.py` 加 `httpx2.MockTransport` 用例；目录合并/重复 id 由 `tests/test_catalog_merge.py` 覆盖。
5. 更新 `Backend/README.md` 软件清单、根 `README.md` 分类概览与本文件 1/5.1/10 节的数量描述。

**其他官网来源（写专用 provider）**

见 5.5。不要忘记 provider 注册、目录条目、图标、测试和 README。

### 8.2 新增客户端页面/导航项

1. 在 `Pages/` 创建 `ui:Page` 与 code-behind。
2. 在 `MainWindow.xaml` 添加 `NavigationViewItem` 和 Tag。
3. 在 `MainWindow.PageType` 添加枚举值，`ChangePage` 处理并设置导航选中。
4. 页面内复用 `Wam.*` 样式、`ProgressRing`、卡片布局。
5. 需要模态确认/进度时在 `Dialogs/` 创建无边框 `Window`，设置 `Owner` 并实现关闭拦截。
6. 更新 README。

### 8.3 新增客户端设置项

见 6.7。特别注意旧配置迁移、非法值恢复、滑杆/输入校验、手动验证和文档。

### 8.4 修改下载限制/默认值

1. 修改 `Models/InstallerDownloadOptions.cs` 的常量和默认值。
2. 检查 `InstallerDownloadService.ValidateOptions` 的错误文案。
3. 更新 `SettingsPage.xaml` 的 Slider 字面量（构造函数会从常量覆盖，但 XAML 也要一致）。
4. 检查 `DownloadConnections` 1–32、`DownloadTimeoutSeconds` 10–300 的常量、XAML 字面量与校验文案保持一致。
5. 更新 README/AGENTS。

### 8.5 修改卸载功能

1. 条目解析/过滤在 `Models/InstalledApplication.cs` 的 `TryCreate`；注册表读取在 `InstalledApplicationProvider`，商店包读取在 `StoreApplicationProvider`。
2. 命令行拆分规则在 `UninstallCommandParser`，商店命令在 `StoreApplicationUninstallCommand`；新增格式必须在本地手工验证拆分结果（客户端没有自动化测试项目）。
3. 进程启动、作业对象跟踪和退出码分类在 `UninstallRunner` / `UninstallProcessJob`；不要加入静默参数或强制删除文件的逻辑，也不要绕过取消令牌自行结束进程。
4. 二次确认与可停止提示在 `UninstallConfirmationWindow`；顺序执行、停止按钮和结果列表在 `UninstallProgressWindow`。
5. 成功判定在 `UninstallVerifier`；残留扫描在 `LeftoverScanner`，删除入口 `LeftoverCleaner`（`RegistryLeftoverCleaner` / `FileLeftoverCleaner`），快捷方式解析在 `ShortcutResolver`，树节点在 `Models/LeftoverNode.cs`，界面在 `Dialogs/DeepCleanWindow`。文件树节点必须带 `RootPath`，只有“无子项”节点可删（`FolderItem`/`FileItem`/`Shortcut`）；新增命中规则必须同步 `IsDeletable` 白名单和测试，危险分组必须 `IsRisky = true` 且默认不勾选。
6. 修改后构建并做必要的手动验证；除非用户明确要求，不额外做冒烟测试，也不要真正卸载用户软件。

### 8.6 新增主题画刷

1. `App.xaml` 增加默认 `SolidColorBrush`。
2. `AppSettingsService.UpdateThemeResources` 的高对比度、深色、浅色分支都设置。
3. 新 UI 使用 `{DynamicResource Wam.XXXBrush}`，不要直接写颜色。
4. 在浅色/深色主题下各检查一次。

## 9. 安全与边界

- 后端解析的签名下载地址只存在于响应和客户端当前任务中，**不要写入磁盘缓存或软件目录快照**；客户端显示/复制的链接是官方临时链接，GitHub 镜像仅改写实际下载请求。
- 第三方 GitHub 镜像可能记录请求、限速或失效；不要把它设为默认强制项，不要移除 SHA256/PE 校验。
- “忽略证书验证”是高危选项：默认关闭，UI 必须显示警告，不要在其他地方自动开启。
- 不要记录代理认证信息、签名 URL、GitHub 令牌或用户设置到日志/仓库。
- GitHub 令牌只能放机器本地的 `Backend/config.local.json`（已 gitignore）或环境变量 `WINAPPSMANAGER_GITHUB_TOKEN`（优先级最高），提交的 `config.json` 保持空字符串；禁止把令牌写进 `apps.d/*.json`、测试、脚本、文档、命令行、异常或日志。令牌只作为 `Authorization: Bearer ...` 发给 `github.api_base`；`GET/DELETE /api/v1/cache` 只显示 `***` / 空串 + `token_configured`，不要新增返回令牌的诊断字段。
- GitHub 配额有限（未认证 60/小时、令牌 5000/小时），测试一律用 `httpx2.MockTransport` 与假令牌，禁止访问真实 API。
- GitHub 响应缓存（`providers/_github_cache.py`）只保存公开响应体与 `ETag`/`Last-Modified`，**绝不写入令牌**；新增任何缓存字段时同样不能包含令牌。
- 文件名、资源 ID、下载目录必须做路径安全校验；不要使用用户输入拼接路径；不要执行下载的安装包，除非是在真实用户交互路径中由用户点击“打开安装包”。
- 卸载命令必须只来自 `UninstallString`/注册表或本应用生成的商店包命令，不能在 UI 中接受任意命令或路径。
- 商店应用必须使用当前用户 SID 枚举，卸载只走 `Get-AppxPackage -Name <包标识名> | Remove-AppxPackage`；不要添加 `-AllUsers`、DISM deprovision、强制删除 `WindowsApps` 目录。
- 卸载前必须二次确认并说明可以中途停止、但可能残留部分卸载；只有用户点击“停止卸载”或关闭进度窗口时才终止卸载程序进程树。
- 深度清理只能删列表里通过校验的项：路径必须在允许分支内，删除前过 `LeftoverCleaner.IsDeletable`；注册表绝不删 `SOFTWARE`、`Microsoft`、`Classes`、`Policies`、`Wow6432Node`、`SYSTEM\CurrentControlSet\Services` 整棵子树；文件只允许开始菜单/桌面 `.lnk`、`RootPath` 内文件和“已无子项”的文件夹，绝不删盘根、`Windows`、`System32`、`Program Files` 本身，也不递归删除；文件分组默认不勾选。
- 不要自动卸载系统组件、更新或 `NoRemove` 条目；不要把卸载结果报告为“已彻底清理”，只报告卸载程序已结束。
- 发布版本号只写在 `release.props`，不要同时在 csproj 或 workflow 里再写一份；推送的标签必须与 `Version` 一致，发布说明只写在 `Release.md`。

## 10. 已知限制

- 后端持久缓存默认写在 `Backend/.cache/`（已 `.gitignore`）；只读目录下运行会降级为「只用内存缓存」，用 `cache.directory` 或 `WINAPPSMANAGER_CACHE_DIR` 指到可写目录。
- 下载中心任务在重启后从历史记录恢复，但只恢复终点状态（不含分段进度与活动任务）；图标在页面加载后按历史记录的远端地址从本地缓存恢复。
- 启动预热最多占 `refresh.startup_timeout_seconds`（默认 30 秒），预算耗尽时先启动、未完成软件按需抓取；内置源默认只启用 2 款（`builtin.app_ids`），预热很快；联想目录不在预热范围、首次请求按需抓取（约 1.5–2 秒）并落盘，重启后 0.2 秒级读取。
- 非强制刷新的列表/详情可能返回最长 `cache.max_stale_seconds`（默认 7 天）的旧快照，界面无法区分；需确认新鲜度时用 `GET /api/v1/status` 或 `refresh=true`。
- 预热既抓取失败又无快照的软件从列表隐藏、详情 503（`refresh.drop_unavailable_apps=true`）；定时刷新单轮失败只记日志，要等下一个间隔。
- 公共 GitHub 镜像可能失效；UI 提供自定义前缀，但无法保证第三方可用性。
- GitHub API 配额有限（未认证 60 次/小时，令牌 5000 次/小时）；条件缓存让重启与定时刷新基本只产生 304，但软件越多、`github.refresh_interval_seconds` 越小越依赖令牌。`GET /api/v1/status` 可查看每个软件的 `ok`/`cached`/`unavailable`，配额耗尽时接口会提示配置令牌。
- 部分热门软件 GitHub 上没有可用安装包资产，本轮**未收录**：Discord、Slack、WhatsApp、飞书、钉钉、Signal、Element、Chatbox、LM Studio、SumatraPDF、Bitwarden（安装器）等；收录标准是 latest release 有 `.exe/.msi/.msix/.msixbundle` 资产。
- 45 个图标是仓库内固定文件（43 个 PNG + 2 个 ICO），来源与 SHA256 在 `Backend/icon/SOURCES.md` 和 `sources-{devtools,ai,chat,utils}.md`；新增软件必须补图标和来源记录，不要在运行时抓取。
- WorkBuddy 有官方 SHA256；DeepSeek Harness / 豆包没有，客户端只能做容器/长度校验。DeepSeek Harness 用 `dsh-latest` 别名（`download_url` 与 `file_name` 都是最新地址），CDN 最长约 24 小时缓存，版本更新可能滞后一天。
- 豆包下载地址来自官网前端硬编码 `surl_token` 的接口，官网改版可能让 token 失效并抛 `ProviderUnavailableError`（按降级规则暂时隐藏或回退旧快照）；版本号是安装器构建号（如 `2035012`），不是语义版本。
- 通用 GitHub provider 只接受 `.exe/.msi/.msix/.msixbundle`；客户端还允许 `.appx`，但不要把 GitHub 发布资产写成 `.appx`，否则 provider 会拒绝。
- 商店应用卸载只注销当前用户，其他用户不受影响；不保留应用数据，因此 `%LOCALAPPDATA%\Packages\<PFN>` 会一起删除，预置应用可能在下次登录时重新部署。
- 卸载依赖厂商自己的 `UninstallString`，可能弹出交互式向导、需要管理员权限或返回非零代码；本应用不会静默卸载或强制删除。
- 深度清理的 MSI 项依赖卸载键里的 ProductCode，非 MSI 软件没有这类记录；MSI 只清理 Products 与 Installer\Folders。AppData 目录依赖“文件夹名等于软件名/安装目录名/卸载键名”，厂商用缩写命名的目录仍可能扫不到。这些分组默认不勾选，删除后同样无法回滚。
- 安装包验证按容器类型进行（`.exe` 查 MZ+PE，`.msi` 查 OLE，`.msu` 查 CAB `MSCF`，`.msix/.msixbundle/.appx` 查 ZIP），并可选校验 SHA256；没有代码签名验证。
- 内存占用下限由 WPF + iNKORE 主题决定：实测启动后私有内存约 116 MB / 工作集约 192 MB；去掉 `ui:XamlControlsResources` 可降到约 99 MB（≈17 MB 是主题字典）。再往下压只有换主题或改用 R2R/裁剪，属于产品级取舍。
- 45 款目录中声明 x86 安装包的只有 13 款（QQ、微信、7-Zip、Telegram、VLC、Firefox、OBS Studio、Notepad++、PowerShell 7、GitHub CLI、Rocket.Chat、Ferdium、Rufus）；仅声明兼容 Win7 的有 4 款（微信、7-Zip、Telegram、VLC）；32 位系统看不到只提供 x64 的软件。
- **后端无法在 Windows 7 上原生运行**：要求 Python 3.11+，而 Python 3.9 起已不支持 Win7（3.8 是最后支持版本），当前依赖栈（FastAPI / Pydantic 2 / uvicorn）都要求 Python ≥3.9；客户端默认连接在线服务端 `https://winapps.theconsole.top/`，也可在设置页改成其他服务端地址（远程后端需自行保证网络可达），因此 Win7 也能使用目录/下载功能（卸载与设置可离线使用）。
- 联想源依赖外部网络与联想接口可用性：目录首次抓取约 28 个请求（约 1.5–2 秒），接口变更/限流会让联想源退化为 502/504 或使用陈旧快照；图片直连联想 CDN 需要正常 UA，联想 CDN 对 curl UA 返回 403。内置源默认只启用 Clash Verge Rev 与 Telegram（`builtin.app_ids`），其余 43 款不抓取版本。
- 自定义服务端地址会让客户端缓存按服务端 URL 隔离（旧服务端的目录/图标缓存不会复用）；远程服务端需要自行处理鉴权/暴露风险，默认只监听 127.0.0.1。
- WPF 客户端无跨平台支持；同一 net6 构建支持 Windows 7–11，Windows 7/8 没有商店应用（界面会提示），Windows 10/11 自动可用；框架依赖构建或开发运行需要 .NET 6 Desktop Runtime；GitHub Release 提供的自包含 zip 已包含运行时，目标机器无需安装，但 net6 已 EOL、无安全更新。

## 11. 协作方式

- 修改前先用 `read`/`grep`/`glob` 定位相关文件，不要凭印象编辑。
- 保持改动范围聚焦，不做与任务无关的大规模重构。
- 用户使用中文提出需求时，界面文案保持中文。
- 不要修改生成目录、真实用户数据或运行中的用户进程。
- 修改设置/下载路径后必须恢复测试时改动的真实设置；仅清理自己创建的临时文件。
- 若需求有歧义，先提出简短澄清；若可以直接实现，优先完成并说明假设。
- 完成前运行构建（改动后端时再加后端测试），汇报实际结果；不要声称未运行的测试已通过。
