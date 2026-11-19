Windows 7 SP1–11 自包含版，无需安装 .NET 6 Desktop Runtime。

## 本次更新

- 修复下载完成后下载中心卡片内容向右偏移的问题。
- 修复重启后下载中心历史任务图标缺失的问题，图标会从本地缓存自动恢复。
- GitHub Release 的版本号、标题和发布说明改由仓库内的 `release.props` 与 `Release.md` 管理，发布流程不再在 workflow 中硬编码。

## 下载

- `WinAppsManager-*-win-x64-selfcontained.zip`：64 位版本
- `WinAppsManager-*-win-x86-selfcontained.zip`：32 位版本

下载对应 zip，解压后运行 `WinAppsManager.exe` 即可。客户端默认连接在线服务端，也可以在“设置 → 服务端地址”中改为自建后端。
