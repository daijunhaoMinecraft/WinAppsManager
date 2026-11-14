# 社交通讯类软件图标来源

以下图标在 2026-11-14 一次性获取并保存到本目录，供 `Backend/data/apps.d/chat.json` 中的社交通讯类软件使用。后端运行时读取本地 `icon_file`，不再向官网请求图标。

PNG 均保留下载得到的原始字节，没有调整图案、颜色、尺寸或透明度。仓库内文件通过 GitHub contents API 按默认分支（`HEAD`）读取后原样保存。

| 软件 | 本地文件 | 格式及尺寸 | 字节数 | 来源 URL | SHA256 |
| --- | --- | --- | ---: | --- | --- |
| Telegram Desktop | `telegram.png` | PNG，128×128 | 11,449 | <https://telegram.org/img/t_logo.png> | `88afe4caad9d3ef9d7b4a5301d1b2b4378b54d233038079f0145e2f387f4eed9` |
| Jitsi Meet | `jitsi.png` | PNG，512×512 | 64,504 | <https://github.com/jitsi/jitsi-meet-electron/blob/HEAD/resources/icons/512x512.png> | `60d86a34a5f42b02f27ce0a676de5e375ff3d1b9448221b78bdcde11533fdac2` |
| Mattermost | `mattermost.png` | PNG，512×525 | 52,186 | <https://github.com/mattermost/desktop/blob/HEAD/src/assets/linux/app_icon.png> | `9538f52ecdbce2fe5b87cabf0fb47d8dcc5b51180e420d89a62369d95985754e` |
| Zulip | `zulip.png` | PNG，512×512 | 13,462 | <https://github.com/zulip/zulip-desktop/blob/HEAD/app/resources/zulip.png> | `bb77091dd0d082bdb1c115dbcf155d8b5d4991fc7fc6d85c5ceae2e600868ec8` |
| Rocket.Chat | `rocketchat.png` | PNG，512×512 | 24,346 | <https://github.com/RocketChat/Rocket.Chat.Electron/blob/HEAD/build/icons/512x512.png> | `435d7f49d3c081335007adf0b0714060d0402911f7804439e5549755579ed9c1` |
| Ferdium | `ferdium.png` | PNG，512×512 | 26,924 | <https://github.com/ferdium/ferdium-app/blob/HEAD/build-helpers/images/icons/512x512.png> | `bcc5795c808ebb3600156bc792f91962de9dd67ff6ad67a65572e88a05415ef9` |
| Revolt | `revolt.png` | PNG，512×512 | 21,011 | <https://github.com/revoltchat/desktop/blob/HEAD/build/icons/512x512.png> | `db64c2f3ab6714771638d06f3c0156e42c82ff6cbd041370b23aa691ab7fdeec` |
| Session | `session.png` | PNG，512×512 | 40,583 | <https://github.com/oxen-io/session-desktop/blob/HEAD/build/icons/icon_512x512.png> | `d85abe3a5393f76046031e4ddd8df42ed191de3c5b7e6670ea906e6382c3dc44` |

所有文件都通过 PNG 字节标记（`89 50 4E 47 0D 0A 1A 0A`）、2 KB–300 KB 大小范围检查，并已逐个目视确认图案与软件一致。

## 未收录的候选

核对 GitHub 最新正式版资产后，以下候选无法满足“GitHub Releases 通用 provider + 恰好匹配一个 Windows 安装包”的要求，因此未收录：

| 候选 | 仓库 | 原因 |
| --- | --- | --- |
| Signal Desktop | `signalapp/Signal-Desktop` | 最新正式版只有标签、没有任何发布资产（安装包由 signal.org 分发）。 |
| Element Desktop | `element-hq/element-desktop` | 最新正式版只附带 `v1.12.13.tar.gz.asc`，没有 Windows 安装包。 |
| Wire | `wireapp/wire-desktop` | 最新正式版标签为 `windows/3.46.5382`，且没有发布资产。 |
