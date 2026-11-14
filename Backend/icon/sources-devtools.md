# 开发工具类软件图标来源

以下图标在 2026-11-14 一次性获取并保存到本目录。`data/apps.d/devtools.json` 通过本地 `icon_file` 文件名引用这些文件，后端运行时读取本目录，不再向官网获取图标。

PNG 均保留下载得到的原始字节，没有调整图案、颜色、尺寸或透明度。全部文件已通过 PNG 文件头（`89 50 4E 47 0D 0A 1A 0A`）校验，字节数在 2 KB 到 300 KB 之间。

仓库内的图标经 GitHub Contents API（`/repos/{repo}/contents/{path}?ref={branch}`，`Accept: application/vnd.github.raw`）按原始字节下载：本机到 `raw.githubusercontent.com` 的连接不可用，而该 API 返回的内容与仓库中对应文件一致。表中记录的是仓库内的原始路径。VSCodium 官网只提供 SVG 标志，GitHub CLI 没有独立的 PNG 品牌图，两者改用组织头像。

| 软件 | 本地文件 | 格式及尺寸 | 字节数 | 官方来源 |
| --- | --- | --- | ---: | --- |
| Git for Windows | `git.png` | PNG，383×383 | 2,548 | [git-scm.com 官方 Git 图标](https://git-scm.com/images/logos/downloads/Git-Icon-1788C.png) |
| PowerShell 7 | `powershell.png` | PNG，150×150 | 10,843 | [PowerShell 仓库 `assets/Square150x150Logo.png`](https://github.com/PowerShell/PowerShell/blob/master/assets/Square150x150Logo.png) |
| Neovim | `neovim.png` | PNG，1202×1472 | 51,483 | [neovim.io 官方站标](https://neovim.io/logos/neovim-mark-flat.png) |
| VSCodium | `vscodium.png` | PNG，128×128 | 35,393 | [VSCodium 组织头像](https://github.com/VSCodium.png?size=128) |
| DBeaver Community | `dbeaver.png` | PNG，128×128 | 7,565 | [dbeaver.io 官方站标](https://dbeaver.io/wp-content/uploads/2015/09/beaver-head.png) |
| GitHub Desktop | `ghdesktop.png` | PNG，128×128 | 51,939 | [GitHub Desktop 仓库 `app/static/common/logo-64x64@2x.png`](https://github.com/desktop/desktop/blob/development/app/static/common/logo-64x64@2x.png) |
| Zed | `zed.png` | PNG，512×512 | 168,173 | [Zed 仓库 `crates/zed/resources/app-icon.png`](https://github.com/zed-industries/zed/blob/main/crates/zed/resources/app-icon.png) |
| Bruno | `bruno.png` | PNG，256×256 | 25,531 | [Bruno 仓库 `packages/bruno-electron/resources/icons/png/256x256.png`](https://github.com/usebruno/bruno/blob/main/packages/bruno-electron/resources/icons/png/256x256.png) |
| GitHub CLI | `ghcli.png` | PNG，128×128 | 4,595 | [GitHub CLI 组织头像](https://github.com/cli.png?size=128) |

## SHA256

| 文件 | SHA256 |
| --- | --- |
| `git.png` | `3585b2ebe81259786804764bdf0777d34c1f40264f9826f8835ffd1063be4959` |
| `powershell.png` | `ab09c050a04360ad1d8c4ca38ba50533bc569e61a0dc5870129b9a97b87c729f` |
| `neovim.png` | `5283a7552844f3fe4be114f0e459da5a8b645b216573df52b416a67d2bd95ab0` |
| `vscodium.png` | `66aa6506334cfba4e89fdb1a1b7a1926a4f8aaf72689e4e4c6f19889c5f1bfd6` |
| `dbeaver.png` | `02c2e2507f0c4309d8d684071e6b0c808a2d09a585afcee8172daf2422440103` |
| `ghdesktop.png` | `e46ed2e5d094f4393ecbe4979efe9da6c13caf47e1c5a8d1e5f5df9afd136892` |
| `zed.png` | `77453d98d46494b3b34b94aa863b686ea299e0190030a5c7ca5377b238332522` |
| `bruno.png` | `f40e589ebb1df0c4153d064c5655820cb5aa10516ca36d45b3b54a88b97f6578` |
| `ghcli.png` | `aee7b26fd2ea690ac35d6fa652a47fcb7b84c1b3d5ab93e5440895697c13b273` |

图标已逐个目视确认，为对应软件的品牌标志；后端不需要额外的图像处理依赖。
