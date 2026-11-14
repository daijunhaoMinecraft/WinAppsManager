# 实用工具 / 系统 / 多媒体 / 网络类软件图标来源

以下图标于 2026-11-14 获取，配合 `Backend/data/apps.d/utils.json` 使用。目录只记录本地 `icon_file` 文件名，后端运行时读取本目录中的文件，不再为这些软件在线获取图标。

PNG 均保留下载得到的原始字节，未调整图案、颜色、尺寸或透明度。优先取各项目仓库中官方发布的品牌图标；mpv.net 与 Obsidian 的仓库内没有符合体积要求的 PNG，改用 GitHub 组织头像（官方品牌图）。本机访问 `raw.githubusercontent.com` 时连接失败，因此官方文件通过 GitHub Contents API 的 raw 响应取得（`https://api.github.com/repos/<owner>/<repo>/contents/<path>?ref=<branch>`，`Accept: application/vnd.github.raw`），表中给出对应文件的 GitHub 页面地址。

| 软件 | 本地文件 | 格式及尺寸 | 字节数 | 来源 URL |
| --- | --- | --- | ---: | --- |
| Microsoft PowerToys | `powertoys.png` | PNG，256×256 | 6,246 | <https://github.com/microsoft/PowerToys/blob/main/doc/images/icons/PowerToys%20icon/PNG/PowerToysAppList.targetsize-256.png> |
| ShareX | `sharex.png` | PNG，256×256 | 25,356 | <https://github.com/ShareX/ShareX/blob/develop/ShareX.HelpersLib/Resources/ShareX_Logo.png> |
| Rufus | `rufus.png` | PNG，256×256 | 34,971 | <https://github.com/pbatard/rufus/blob/master/res/icons/rufus-256.png> |
| KeePassXC | `keepassxc.png` | PNG，256×256 | 32,938 | <https://github.com/keepassxreboot/keepassxc/blob/develop/share/icons/application/256x256/apps/keepassxc.png> |
| mpv.net | `mpvnet.png` | PNG，128×128 | 25,040 | <https://github.com/mpvnet-player.png?size=128> |
| Clash Verge Rev | `clashverge.png` | PNG，512×512 | 76,027 | <https://github.com/clash-verge-rev/clash-verge-rev/blob/dev/src-tauri/icons/icon.png> |
| LocalSend | `localsend.png` | PNG，512×512 | 4,688 | <https://github.com/localsend/localsend/blob/main/app/assets/img/logo-512.png> |
| QuickLook | `quicklook.png` | PNG，512×512 | 40,421 | <https://github.com/QL-Win/QuickLook/blob/master/QuickLook/Resources/app.png> |
| Motrix | `motrix.png` | PNG，512×512 | 200,794 | <https://github.com/agalwood/Motrix/blob/main/public/app-icon.png> |
| Obsidian | `obsidian.png` | PNG，128×128 | 7,083 | <https://github.com/obsidianmd.png?size=128> |

## SHA256

| 文件 | SHA256 |
| --- | --- |
| `powertoys.png` | `3e4efab616a246c93d0fe43eb1486fdd60e05c6c7aa05e47240832b0058b5dca` |
| `sharex.png` | `59ab47af2f7c733c3eb9e865491aa833839c33046bc2dc8c79ee7388e4e87820` |
| `rufus.png` | `209dfca832c43898a5018edd9eb5b304d320609c466bb9a1464e8c80528c4dd5` |
| `keepassxc.png` | `517e021ae33591d41139ac187d84b3d6a64564de4942858bf8d789f25c17f9c5` |
| `mpvnet.png` | `9942b20b2f64fb763f3c683db67eb825233696d28cbff37c6a64cede8d459799` |
| `clashverge.png` | `ee04739a049564e34c7fee4fcc649f6dd195598c645bf061c1696e3099d67b08` |
| `localsend.png` | `d7e6016dd11ff9ce48ed6f1f24d8090b85d70982a450f1f1e51943739e166a48` |
| `quicklook.png` | `02a0c1dfb67cfbdb57b37dde72c7a36838e68020088b596fff1d183690a7ac17` |
| `motrix.png` | `574e1dfc03f87210511380c2ccf932374182752c0b2cf43759bafaf6e2991915` |
| `obsidian.png` | `f5f620d62882dbbe9c6320d8dd960e2904ce97f4fb9317e8664dc33b4a96b02c` |

全部文件通过 PNG 签名（`89 50 4E 47 0D 0A 1A 0A`）检查，字节数位于 2 KB–300 KB 之间，并已逐个目视确认图案与对应软件一致。
