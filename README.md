# WinAppsManager

Windows 软件管理器：像应用商店一样查找、下载和卸载 Windows 软件。软件源由服务端统一下发，默认包含联想应用商店、360 软件管家与内置精选，新增来源只需在服务端添加。

[![Build](https://github.com/daijunhaoMinecraft/WinAppsManager/actions/workflows/build.yml/badge.svg)](https://github.com/daijunhaoMinecraft/WinAppsManager/actions/workflows/build.yml) [![Latest Release](https://img.shields.io/github/v/release/daijunhaoMinecraft/WinAppsManager)](https://github.com/daijunhaoMinecraft/WinAppsManager/releases/latest)

![软件列表](docs/screenshots/01-apps-list.png)

## 功能

- **软件商店**：软件源由服务端统一下发（默认联想应用商店、360 软件管家、内置精选），随时切换；新增来源无需升级客户端。分类导航、排序、搜索、分页。
- **软件详情**：安装包大小、评分、开发者、版本、应用截图；下载链接可复制或打开。
- **下载中心**：分段并发下载、实时进度和速度、取消/重试/打开安装包；支持 `.exe` / `.msi` / `.msix` / `.appx` / `.msu`（Windows 补丁包）。下载完成后显示安装包数字签名的签发公司、证书有效期和有效性（仅提示，不阻止打开）；服务端上报的 Size 不再参与下载校验，可选 SHA256 仍会校验。
- **卸载与强制卸载**：读取 Windows 卸载注册表和 Microsoft Store/MSIX 应用，分类、搜索、批量卸载；卸载结束后刷新已安装列表判断软件是否还存在，确认移除后自动打开“强制卸载”窗口，由用户逐项选择残留（安全项默认勾选，安装目录/AppData/快捷方式/MSI 等危险项默认不勾选）。
- **主题与设置**：浅色/深色/跟随系统，自定义下载目录、线程数、代理和软件源。
- **系统兼容**：单一 .NET 6 构建支持 Windows 7 SP1 / 8 / 8.1 / 10 / 11，按系统版本自动启用商店应用；内置 Fluent System Icons 图标字体，Win7 未安装 Segoe Fluent Icons 也不会出现方框图标。

## 截图

| 软件列表 | 软件详情 |
| --- | --- |
| ![软件列表](docs/screenshots/01-apps-list.png) | ![软件详情](docs/screenshots/02-app-details.png) |

| 下载中心 | 设置与软件源 |
| --- | --- |
| ![下载中心](docs/screenshots/03-download-center.png) | ![设置](docs/screenshots/04-settings.png) |

| 已安装软件 | 商店应用 |
| --- | --- |
| ![已安装软件](docs/screenshots/12-uninstall.png) | ![商店应用](docs/screenshots/14-store-apps.png) |

| 强制卸载（清理残留） | 360 软件源 |
| --- | --- |
| ![强制卸载残留](docs/screenshots/09-deep-clean.png) | ![360 软件源](docs/screenshots/07-360-source.png) |

## 下载与运行

### 1. 下载自包含版本

从 [GitHub Releases](https://github.com/daijunhaoMinecraft/WinAppsManager/releases/latest) 下载自包含版本：

- `WinAppsManager-*-win-x64-selfcontained.zip`：64 位，适用于 Windows 7 SP1–11
- `WinAppsManager-*-win-x86-selfcontained.zip`：32 位，适用于 Windows 7 SP1–11

压缩包已内置 .NET 6 Desktop Runtime，**目标机器无需安装 .NET Runtime**；解压后运行 `WinAppsManager.exe` 即可。GitHub Actions 每次推送都会构建客户端并测试后端；发布版本由手动触发 `Release` workflow 构建并上传。

### 2. 可选：本地启动后端

要求 Python 3.11+（推荐 3.12；`httpx2 2.16` 要求 Python 3.11+）。

```powershell
python -m venv Backend\.venv
Backend\.venv\Scripts\python.exe -m pip install -r Backend\requirements.txt
Backend\.venv\Scripts\python.exe Backend\main.py
```

后端默认监听 `http://127.0.0.1:8080`，接口文档 <http://127.0.0.1:8080/docs>。客户端默认连接在线服务端 `https://winapps.theconsole.top/`，本地运行时无需额外启动后端；需要本地开发或自建服务时可在设置中切换服务端地址。

### 3. 启动客户端

要求 .NET SDK 8+；运行客户端需要 .NET 6 Desktop Runtime。

```powershell
dotnet build WinAppsManager\WinAppsManager.sln -c Release
dotnet run --project WinAppsManager\WinAppsManager\WinAppsManager.csproj
```

只有一套 `net6.0-windows7.0` 构建：Windows 7 SP1–11 使用同一个客户端，启动时检查系统版本；Windows 10 / 11 自动启用 Microsoft Store / MSIX 应用管理，Windows 7 / 8 只显示桌面软件。需要独立安装包时使用 `Properties/PublishProfiles` 中的 x64/x86 配置发布。

## 使用方法

1. 启动客户端，默认连接在线服务端 `https://winapps.theconsole.top/`；本地开发可启动 `Backend` 后在设置中切换为 `http://127.0.0.1:8080/`。
2. 在**设置 → 软件源**选择需要的软件源；列表和服务端地址均可修改，来源能力（分类、搜索方式、截图/视频等）由服务端下发。
3. 在**软件列表**搜索或按分类浏览，打开详情查看截图并点击“开始下载”。
4. 下载任务进入**下载中心**，完成后可查看安装包数字签名（签发公司、证书有效期、是否有效），再打开安装包或下载文件夹。
5. 在**卸载软件**勾选要卸载的程序，点击“卸载所选”；卸载结束后会刷新安装列表核对结果，确认软件已不存在后自动打开“强制卸载”窗口，由你手动选择要清理的残留项。
6. 也可选中任意软件点“强制卸载”手动扫描残留；安装目录、AppData、快捷方式等危险项默认不勾选，确认后再点“删除选中”。

搜索方式由服务端 `search_mode` 决定：目录完整时客户端本地搜索，否则自动调用服务端搜索 API；切换源后目录和左侧分类会自动更新。卸载结果以刷新后的已安装列表为准，不再以卸载程序退出码为准；确认已卸载后会自动打开“强制卸载”窗口，由你手动选择残留项。

## 常见问题

**Win7 能运行吗？** 可以，同一套客户端支持 Win7 SP1–11；Windows 7/8 没有商店应用，Windows 10/11 会自动显示商店应用。

**需要单独安装数据库或服务吗？** 不需要。后端是单进程 FastAPI 服务，默认只监听本机，数据和缓存保存在 `Backend\.cache`。

**卸载不掉的软件怎么办？** 先尝试正常卸载；如果卸载程序失败或留下残留，可用“强制卸载”扫描并清理残留文件和注册表项。它不会强制结束正在运行的程序。

**软件图标/截图不显示？** 图片优先从软件源 CDN 加载，失败会自动回退到后端代理；请检查网络，或在设置中切换服务端。

## License

WinAppsManager is licensed under the **PolyForm Noncommercial License 1.0.0** with additional source-sharing and non-resale terms. Commercial use, resale, paid distribution, and closed-source distribution are not permitted. See [LICENSE](LICENSE).

Copyright (c) 2026 daijunhaoMinecraft

Lenovo, 360, and their respective product names, data, icons, and trademarks belong to their owners; this project is not affiliated with or endorsed by them.
The bundled Fluent System Icons font comes from Microsoft `fluentui-system-icons` and is used under the MIT License (see `WinAppsManager/Assets/Fonts/LICENSE.txt`).

## 开发文档

- 后端接口与配置：[Backend/README.md](Backend/README.md)
- 软件源统一接口契约：[docs/source-api.md](docs/source-api.md)
- 软件源与设计说明：[docs/store-source-design.md](docs/store-source-design.md)
- 联想接口调研：[docs/lenovo-store-api.md](docs/lenovo-store-api.md)
- 360 接口调研：[docs/soft360-store-api.md](docs/soft360-store-api.md)、[docs/soft360-search-api.md](docs/soft360-search-api.md)
- 部署与安全：[docs/deploy.md](docs/deploy.md)
