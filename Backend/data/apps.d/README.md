# `data/apps.d` 扩展软件目录

本目录下的每个 `*.json` 都是软件目录的扩展文件，与 `data/apps.json` 使用同一套 schema。后端启动时先读取 `data/apps.json`，再按文件名顺序合并本目录的 `*.json`；当前有 4 个文件、37 款软件：

| 文件 | 软件数 | 分类 |
| --- | ---: | --- |
| `devtools.json` | 9 | 开发工具 |
| `ai.json` | 10 | AI 助手 |
| `chat.json` | 8 | 社交通讯 |
| `utils.json` | 10 | 实用工具 / 系统工具 / 网络与安全 / 影音工具 / 办公效率 |

合并规则由 `Backend/catalog.py` 的 `CatalogRepository` 实现：

- 每个文件都是 `{"schema_version": 1, "apps": [...]}`，逐项按 Pydantic 严格模式校验（`extra="forbid"`、`strict=True`、字符串去首尾空白）。未知字段、类型不对、非法 URL、重复架构、非法 `minimum_windows` 等都会让后端启动失败。
- 文件名决定合并顺序（`sorted(glob("*.json"))`）；**跨文件重复的 app id 是启动错误**，错误信息为 `duplicate app id across catalog files`。
- 每个 app 的 `icon_file` 文件必须真实存在于 `Backend/icon/`，否则启动失败。
- `create_app(..., extra_directory=None)` 和 `CatalogRepository(data_file, icon_directory, extra_directory)` 都能覆盖本目录。测试用这个方法注入一个不存在的目录来隔离生产案例（`tests/test_api.py` 指向 `Backend/tests/no-extra-apps.d`，保持原有 8 款软件的断言稳定）；**不要**为了测试而改动本目录或 `apps.json`。

## 文件格式

```json
{
  "schema_version": 1,
  "apps": [
    {
      "id": "sharex",
      "name": "ShareX",
      "description": "开源的截图与录屏工具，可自动上传并复制分享链接。",
      "long_description": "……",
      "category": "实用工具",
      "publisher": "ShareX Team",
      "version": "以官网为准",
      "size_display": "以官网为准",
      "license": "免费 / 开源",
      "platforms": "Windows 10 / 11 · x64",
      "minimum_windows": "10",
      "architectures": ["x64"],
      "website_url": "https://getsharex.com/",
      "download_page_url": "https://github.com/ShareX/ShareX/releases",
      "icon_file": "sharex.png",
      "icon_text": "SX",
      "icon_background": "#2E9E4F",
      "installation_notes": "……",
      "features": ["区域、窗口与滚动截图", "屏幕录制"],
      "tags": ["截图", "录屏", "开源"],
      "is_recommended": false,
      "sort_order": 401,
      "download_provider": "sharex",
      "provider_spec": {
        "repo": "ShareX/ShareX",
        "tag_prefix": "v",
        "assets": [
          {"pattern": "ShareX-{version}-setup-x64\\.exe", "architecture": "x64"}
        ]
      },
      "latest_release_id": null,
      "releases": []
    }
  ]
}
```

`apps` 是数组（实际使用中至少 1 项）；`schema_version` 固定为 `1`。

## 字段

| 字段 | 必填 | 类型 / 约束 | 说明 |
| --- | --- | --- | --- |
| `id` | 是 | 字符串，`^[a-z0-9][a-z0-9.-]*$`，1–100 字符 | 全仓库唯一。同时用作 API 路径 `/api/v1/apps/{id}`、缓存键和默认 provider id |
| `name` | 是 | 非空字符串 | 客户端显示名 |
| `description` | 是 | 非空字符串 | 列表里的简介 |
| `long_description` | 是 | 非空字符串 | 详情页的长说明 |
| `category` | 是 | 非空字符串 | 见下面的分类词表；客户端按它动态生成左侧分类 |
| `publisher` | 是 | 非空字符串 | 发布者 |
| `version` | 否 | 字符串，默认 `以官网为准` | provider 抓取成功后，API 响应里的 `version` 会被最新版本覆盖 |
| `size_display` | 否 | 字符串，默认 `以官网为准` | 官网展示用的体积文字，精确字节数来自版本资源 |
| `license` | 否 | 字符串，默认 `免费` | 许可证文字 |
| `platforms` | 否 | 字符串，默认 `Windows 10 / 11` | 给人看的展示文案，建议统一写成「系统版本 · 架构」 |
| `minimum_windows` | 否 | 字符串，`7` / `8` / `8.1` / `10` / `11` | 官方系统要求的最低 Windows 版本；客户端据此隐藏不兼容软件 |
| `architectures` | 否 | 字符串数组，取值 `x64` / `x86` / `arm64`，至少 1 项、不可重复 | 该软件 provider 实际能提供的安装包架构；`/apps?architecture=` 据此过滤 |
| `website_url` | 是 | http(s) URL | 官网 |
| `download_page_url` | 是 | http(s) URL | 官网下载页；没有安装包直链时客户端仍展示它 |
| `icon_file` | 否 | 裸文件名，扩展名 `.png` / `.jpg` / `.jpeg` / `.webp` / `.ico` | 必须是 `Backend/icon/` 内真实存在的文件；不能带路径分隔符 |
| `icon_text` | 是 | 非空字符串 | 图标缺失或加载失败时的字母徽章 |
| `icon_background` | 是 | `^#[0-9a-fA-F]{6}$` | 字母徽章背景色 |
| `installation_notes` | 是 | 非空字符串 | 安装说明，展示在详情页 |
| `features` | 是 | 字符串数组，每项非空 | 功能要点 |
| `tags` | 是 | 字符串数组，每项非空 | 搜索标签 |
| `is_recommended` | 否 | 布尔，默认 `false` | 推荐软件在 `sort=recommended` 时排在前面 |
| `sort_order` | 否 | 非负整数，默认 `0` | 同分类内的目录顺序，见下面的区间约定 |
| `download_provider` | 否 | 字符串，规则同 `id` | 指向 `providers/__init__.py` 注册的 provider；设置后**不能**再写静态 `releases` 或 `latest_release_id` |
| `provider_spec` | 否 | 对象 | 仅当 `download_provider` 非空时可用；通用 GitHub provider 靠它自动注册 |
| `latest_release_id` | 否 | `null` | provider 管理的软件保持 `null` |
| `releases` | 否 | 数组 | provider 管理的软件保持空数组 `[]` |

严格模式的后果：布尔必须写 JSON 的 `true`/`false`，数字必须写数字（不能写字符串），未知字段（例如拼错的 `tag_prefx`）会直接报错。API 响应不会返回 `icon_file`、`download_provider`、`latest_release_id`、`releases` 和 `provider_spec`。

## 适用系统与架构

`minimum_windows` 与 `architectures` 是客户端判断“这款软件能不能装”的机器可读字段；`platforms` 只是展示文案。三个字段都要基于**官方系统要求**和**provider 实际能提供的安装包**核实，不能照抄宣传语。

| 字段 | 取值 | 核实方式 |
| --- | --- | --- |
| `minimum_windows` | `7` / `8` / `8.1` / `10` / `11`，字符串 | 查官网“系统要求 / System requirements”页；例如微信官方 4.2.0 支持 Windows 7 及以上，Mattermost 官方要求 Windows 11，Ollama 要求 Windows 10 22H2 及以上 |
| `architectures` | `x64` / `x86` / `arm64` 数组，至少 1 项且不可重复 | 以该软件 provider 实际能解析出的安装包为准（GitHub 来源就是 `provider_spec.assets` 声明的架构；专用 provider 看代码里返回的架构），不要写官网宣传但拿不到安装包的架构 |
| `platforms` | 字符串 | 统一写成「系统版本 · 架构」，例：`Windows 7 / 8 / 8.1 / 10 / 11 · x64 / x86 / arm64`、`Windows 10（22H2 及以上）/ 11 · x64`、`Windows 11 · x64 / arm64` |

补充规则：

- 不确定或官方未明确声明时可以省略两个字段；省略表示“兼容所有架构 / 不限制 Windows 版本”，客户端和后端都按兼容处理。
- `architectures` 必须覆盖 provider 实际会返回的每一个架构（GitHub 来源由 `provider_spec.assets` 决定）；漏声明会让该架构被列表过滤，多声明则可能让用户看到装不了的软件（对应架构的 `releases/latest` 返回 404）。
- `platforms` 要与两个机器可读字段保持一致，避免出现“文案写支持 Win7、实际 `minimum_windows` 是 10”的矛盾。
- 本次已按上述规则回填 45 款；核实记录在 gitignore 的 `artifacts/platform-matrix/` 下，供复查。

## `provider_spec`

GitHub Releases 软件的唯一来源配置。约定 `download_provider` 直接等于 app id（一个软件一个 provider 实例）：

```json
{
  "download_provider": "powertoys",
  "provider_spec": {
    "repo": "microsoft/PowerToys",
    "tag_prefix": "v",
    "assets": [
      {"pattern": "PowerToysUserSetup-[0-9.]+-x64\\.exe", "architecture": "x64"},
      {"pattern": "PowerToysUserSetup-[0-9.]+-arm64\\.exe", "architecture": "arm64"}
    ]
  },
  "latest_release_id": null,
  "releases": []
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `repo` | 是 | `owner/name` 形式，不能写成完整 URL、不能带末尾斜杠；尽量使用仓库的 canonical 大小写（下载地址比较不区分大小写，但目录里写对更易维护） |
| `tag_prefix` | 否 | 默认 `v`。从 tag 得到版本号时去掉这个前缀；没有前缀写 `""`（如 VSCodium），GitHub Desktop 这类 tag 用 `release-` |
| `assets` | 是 | 至少 1 项；每项 `{pattern, architecture}`，`architecture` 只能是 `x64` / `x86` / `arm64`，同一架构只能声明一次 |

`pattern` 规则：

- 是 Python 正则，`re.fullmatch` 匹配完整资产名。JSON 里的反斜杠要双写（`\\.exe`）。
- 可选 `{version}` 占位符：匹配前会先替换成 tag 去掉 `tag_prefix` 后的版本号，并**自动做正则转义**；不写占位符也合法。
- 每个架构必须**恰好匹配一个**资产，0 个或多个都会报 `ProviderProtocolError`，软件在本次刷新里不可用。
- 同一个资产可以同时服务多个架构（PowerShell 7 的 `.msixbundle` 同时声明 x64 / x86 / arm64）。
- 只接受 `.exe` / `.msi` / `.msix` / `.msixbundle` 结尾的资产；不提供便携版 ZIP、`.appx` 或源码包。客户端允许 `.appx`，但通用 GitHub provider 不会产出它。

provider 运行时行为（`Backend/providers/github.py`）：

- 只读 `GET /repos/{repo}/releases/latest`；`draft` 或 `prerelease` 为真、缺失 tag、资产重复、缺资产、大小非正、digest 非法、下载域名不是 `github.com` 时都按协议错误处理。
- 只接受 `state=uploaded` 的资产；资产 `size` → `size_bytes`，`digest`（`sha256:...`）→ `sha256`，缺失时保持 `null`。
- 下载地址必须位于 `https://github.com/<repo>/releases/download/<tag>/` 下，仓库路径比较**不区分大小写**：例如目录里写 `VSCodium/vscodium`，GitHub 返回的 canonical 路径是 `VSCodium/vscodium`，两者都能通过校验，下载地址以 API 返回的资产 URL 为准。
- 版本 ID 固定为 `<version>-windows-<architecture>`；`resolve_download()` 只校验身份并返回副本，不访问网络（GitHub 发布资产地址稳定，不需要签名）。
- 错误映射：401 → 令牌无效提示；403/429 且配额耗尽 → 提示配置 token；404 → 仓库没有正式发布；其他 ≥400 → 通用 HTTP 错误；超时 → `ProviderTimeoutError`（API 返回 504）。

### 正则示例

| 场景 | pattern | 说明 |
| --- | --- | --- |
| 资产名直接带 tag 版本 | `ShareX-{version}-setup-x64\\.exe` | tag `v1.2.3` → 版本 `1.2.3`，匹配 `ShareX-1.2.3-setup-x64.exe` |
| **资产版本与 tag 不一致** | `PowerToysUserSetup-[0-9.]+-x64\\.exe` | PowerToys tag 是 `v0.102.3101`，资产是 `PowerToysUserSetup-0.102.3101.0-x64.exe`；写 `{version}` 只得到 `0.102.3101`，匹配不到，所以用纯正则 |
| 同一发布区分架构 | `rufus-{version}\\.exe`、`rufus-{version}_x86\\.exe` | 分别对应 x64 / x86；不要用 `rufus-.*` 这类过宽规则 |
| 无前缀 tag | `VSCodiumUserSetup-x64-{version}\\.exe` + `"tag_prefix": ""` | tag 直接就是版本号时把 `tag_prefix` 设为空串 |
| 多个架构共用一个安装包 | 三项 `assets` 都写 `PowerShell-{version}\\.msixbundle` | 同一资产服务三个架构，架构由条目声明而不是文件名决定 |

## 非 GitHub 来源条目

官网 JSON、CDN 最新别名或在线安装器这类**没有 GitHub Releases** 的软件，条目字段相同，但来源协议由专用 provider 实现：

```json
{
  "id": "workbuddy",
  "download_provider": "workbuddy",
  "latest_release_id": null,
  "releases": []
}
```

- **不要写 `provider_spec`**：它只用于通用 GitHub provider；写了却没有对应仓库配置会在启动校验或解析时失败。
- `download_provider` 必须指向 `providers/__init__.py` 的 `create_release_providers` 静态注册表里的 provider id（约定等于 app id）。`ai.json` 里的 `workbuddy`、`deepseekharness`、`doubao` 就分别对应 `providers/workbuddy.py`、`deepseek_harness.py`、`doubao.py`。
- 版本、架构、文件名、大小和校验值全部由 provider 返回；条目保持 `version: "以官网为准"`、`latest_release_id: null`、`releases: []`，不要手写静态版本。
- 新增来源需要实现 `ReleaseProvider`（优先复用 `_utility.py` 的 `UtilityReleaseProvider` 和 `official_executable_url`），在注册表里登记，补 `Backend/tests/` 下基于 `httpx2.MockTransport` 的测试，并像 GitHub 来源一样补图标和来源记录。完整步骤见 `Backend/README.md` 的“添加软件提供器”和根 `AGENTS.md` 5.5。
- `ai.json` 现有 10 条：`gpt4all` 等 7 条走 GitHub Releases，`workbuddy`、`deepseekharness`、`doubao` 3 条是非 GitHub 来源（版本分别来自官网更新 JSON、安装包 `Content-Disposition` 文件名、在线安装器构建号），且都只有 x64。

## 图标要求

- 图标固定为本地文件，共 45 个：43 个 PNG，外加原有的 `7zip.ico` 和 `notepadplusplus.ico`。运行时只读取 `Backend/icon/`，不向官网下载。
- `icon_file` 只写文件名（如 `powertoys.png`），不能写 `icon/powertoys.png`、URL 或 `..` 路径。
- 图标应当一次性从**官方**来源取得（官网品牌页、官方仓库的 branding/资源目录或官方组织头像），保留原始字节，不重绘、不裁切、不改色；没有官方 PNG 时可用官方 ICO 或组织头像，并在来源文件中说明原因。
- 提交前应确认 PNG / ICO 通过文件头校验（PNG：`89 50 4E 47 0D 0A 1A 0A`；ICO：`00 00 01 00`），并能在图片查看器中正常打开。
- 来源必须记录到 `Backend/icon/sources-<group>.md`（原有 8 款在 `Backend/icon/SOURCES.md`）：软件、本地文件、格式及尺寸、字节数、官方来源 URL、SHA256。当前对应关系是 `devtools.json` → `sources-devtools.md`、`ai.json` → `sources-ai.md`、`chat.json` → `sources-chat.md`、`utils.json` → `sources-utils.md`。新增分组时新建同名来源文件。
- `icon_text` 和 `icon_background` 仍要填：图标文件缺失时客户端用它们显示字母徽章，后端图标接口固定返回本地文件并支持 ETag/304 与一周缓存。

## 分类词表

分类是精确匹配的字符串，客户端按后端返回的分类动态生成左侧导航。当前使用以下 9 个分类，不要随意造同义词（例如“开发工具”不要再写“编程工具”）：

| 分类 | 当前数量 | `sort_order` 段 | 所在文件 |
| --- | ---: | --- | --- |
| 开发工具 | 11 | 3、8、100–108 | `apps.json` + `devtools.json` |
| 社交通讯 | 10 | 1、2、300–307 | `apps.json` + `chat.json` |
| AI 助手 | 10 | 200–209 | `ai.json` |
| 实用工具 | 5 | 4、401、402、406、408 | `apps.json` + `utils.json` |
| 影音工具 | 3 | 5、7、404 | `apps.json` + `utils.json` |
| 系统工具 | 2 | 400、407 | `utils.json` |
| 网络与安全 | 2 | 403、405 | `utils.json` |
| 办公效率 | 1 | 409 | `utils.json` |
| 网络浏览 | 1 | 6 | `apps.json` |

新增分类前先确认现有词表不能覆盖，并同步更新本文件、`Backend/README.md` 和根 `README.md` 的数量统计；客户端不需要改代码。

## `sort_order` 约定

- `apps.json` 的原有 8 款使用 `1`–`8`。
- 扩展文件按分组预留区间：`devtools.json` 100–108、`ai.json` 200–209、`chat.json` 300–307、`utils.json` 400–409。
- 新条目取当前文件最大值 +1（例如 `devtools.json` 下一个用 `109`），不要复用已有值或插到中间重排；`sort=recommended` 先按 `is_recommended` 再按 `sort_order` 排序，分段后顺序稳定。
- 值不要求连续，但同一文件内保持递增，方便以后插入。

## 验证方法

### 1. 目录与图标校验（不联网）

在仓库根目录运行，能同时验证 schema、合并顺序、重复 id 和图标存在性：

```powershell
Backend\.venv\Scripts\python.exe -c "from pathlib import Path; from Backend.catalog import CatalogRepository; repo = CatalogRepository(Path('Backend/data/apps.json'), Path('Backend/icon')); print(len(repo.apps), repo.get_app('newapp'))"
```

### 2. 用真实 GitHub API 跑 provider（新增软件必做）

正则、tag 前缀和资产命名只有访问真实 API 才能确认。用下面的临时脚本读取本机配置（`Backend/config.local.json` 或 `WINAPPSMANAGER_GITHUB_TOKEN`），跑一次 `get_releases(force_refresh=True)`；脚本不打印令牌，运行完删除：

```python
import asyncio
from pathlib import Path

import httpx2

from Backend.catalog import CatalogRepository
from Backend.config import BackendSettings
from Backend.providers import create_github_providers

APP_ID = "newapp"

async def main() -> None:
    settings = BackendSettings.load()
    catalog = CatalogRepository(Path("Backend/data/apps.json"), Path("Backend/icon"))
    async with httpx2.AsyncClient(timeout=settings.github.timeout_seconds) as client:
        providers = create_github_providers(
            client,
            catalog,
            token=settings.github.token,
            api_base=settings.github.api_base,
            timeout_seconds=settings.github.timeout_seconds,
        )
        for release in await providers[APP_ID].get_releases(force_refresh=True):
            print(release.architecture, release.version, release.file_name, release.size_bytes, release.sha256)

asyncio.run(main())
```

```powershell
Backend\.venv\Scripts\python.exe Backend\check_new_app.py
Remove-Item Backend\check_new_app.py
```

脚本里的 `APP_ID` 是 provider 的注册键，必须和该条目的 `download_provider` 一致（约定等于 app id）。输出里每个声明的架构都应恰好出现一次，`file_name` 必须是官方的 `*.exe` / `*.msi` / `*.msix(bundle)`。令牌要求与优先级：

- 未认证的 GitHub API 只有 **60 次/小时**，带令牌是 **5000 次/小时**。软件越多、刷新越频繁越需要令牌。
- 令牌只能放在机器本地：`Backend/config.local.json` 的 `github.token`（已加入 `Backend/.gitignore`，禁止提交），或环境变量 `WINAPPSMANAGER_GITHUB_TOKEN`（优先级最高）。
- 提交的 `Backend/config.json` 里 `github.token` 必须保持空字符串。不要把令牌写进 `apps.d/*.json`、测试、脚本、命令行参数、日志或文档；上面的脚本只读取 `settings.github.token`，不会打印它。
- 没配置令牌且配额耗尽时，接口会提示“GitHub API 请求额度已用尽”；配置后重启后端生效。

### 3. 通过运行中的后端检查

启动后端后验证 API 与诊断状态（不要占用其他进程的 8080）：

```powershell
Invoke-RestMethod 'http://127.0.0.1:8080/api/v1/apps/newapp/releases/latest?architecture=x64'
Invoke-RestMethod 'http://127.0.0.1:8080/api/v1/status'
```

`GET /api/v1/apps?category=分类名&refresh=true` 能看到新卡片；`/status` 的 `warmup` 段对于新软件应为 `ok`。`GET /api/v1/cache` 的 `settings.github.token` 只会显示 `***` 或空字符串，并带 `token_configured` 布尔值。

再用 `GET /api/v1/apps?architecture=x86`（或 `arm64`）确认新条目只在声明的架构下出现；详情接口不按架构过滤，仍可打开。

### 4. 自动化测试

- 新的 pattern 形态（无 `{version}`、多架构共用资产、特殊 tag 前缀等）应在 `Backend/tests/test_github_provider.py` 增加基于 `httpx2.MockTransport` 的用例；目录合并、跨文件重复 id 与平台字段校验由 `Backend/tests/test_catalog_merge.py`、`Backend/tests/test_platform_compatibility.py` 覆盖。
- 测试禁止访问真实网络、禁止使用真实令牌。改动后由维护者运行后端完整 unittest，文档不要写死通过数量。
- 纯目录改动（只加 JSON 和图标）不需要新增 provider 代码；只有新增非 GitHub 来源时才需要实现 `ReleaseProvider`（见 `Backend/README.md` 的“添加软件提供器”和 `AGENTS.md` 5.5）。

### 5. 文档与清单

新增/修改条目时必须同时填写 `minimum_windows` 与 `architectures`，`platforms` 用「系统版本 · 架构」格式；更新 `Backend/README.md` 的软件清单、根 `README.md` 的分类概览；软件总数变化时同步 `AGENTS.md` 第 1 节、5.1、10 节和 `Backend/README.md` 的“45 款”描述，以及本文件开头的分类数量。

## 不要做

- 不要为了加软件去改 `Backend/catalog.py`、`providers/github.py` 或客户端代码；目录条目已经能表达 GitHub Releases 软件。
- 不要把 GitHub 令牌、`config.local.json` 或任何机器本地配置提交进仓库。
- 不要收录只有 draft / prerelease 的仓库、只有便携 ZIP 的软件，或声明 provider 实际不提供的架构（`architectures` 必须与 `provider_spec.assets` / provider 解析出的安装包一致）。
- 不要用第三方镜像、网盘或非 `github.com` 的下载地址；provider 会直接拒绝。
- 不要在运行时下载或改写图标；图标是仓库内的固定资产，来源必须记录。
