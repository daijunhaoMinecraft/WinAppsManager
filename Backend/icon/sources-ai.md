# AI 助手类软件图标来源

以下图标于 2026-11-14 获取，配合 `Backend/data/apps.d/ai.json` 使用。软件目录只记录本地 `icon_file` 文件名，后端运行时读取本目录中的文件，不再为这些软件在线获取图标。

PNG 均保留下载得到的原始字节，未调整图案、颜色、尺寸或透明度。优先取各项目官方发布的品牌图标：Ollama 用官网 logo，其余取项目仓库内的官方应用图标。goose 仓库内的方形图标 `ui/desktop/src/images/icon-512.png` 为 308,971 字节，超过 300 KB 上限，因此改用其 GitHub 组织头像（官方品牌图，白底黑色鹅形标志）。本机访问 GitHub 时证书吊销检查离线，下载使用 `curl --ssl-no-revoke`，未改动响应内容。

| 软件 | 本地文件 | 格式及尺寸 | 字节数 | 来源 URL |
| --- | --- | --- | ---: | --- |
| Ollama | `ollama.png` | PNG，181×256 | 7,487 | <https://ollama.com/public/ollama.png> |
| Jan | `jan.png` | PNG，512×512 | 36,896 | <https://github.com/janhq/jan/blob/main/src-tauri/icons/icon.png> |
| Cherry Studio | `cherrystudio.png` | PNG，1024×1024 | 140,060 | <https://github.com/CherryHQ/cherry-studio/blob/main/build/icon.png> |
| AnythingLLM | `anythingllm.png` | PNG，1025×1024 | 204,166 | <https://github.com/Mintplex-Labs/anything-llm/blob/master/frontend/src/media/logo/anything-llm-icon.png> |
| GPT4All | `gpt4all.png` | PNG，48×48 | 2,537 | <https://github.com/nomic-ai/gpt4all/blob/main/gpt4all-chat/icons/gpt4all-48.png> |
| LobeHub | `lobehub.png` | PNG，514×514 | 178,041 | <https://github.com/lobehub/lobehub/blob/canary/apps/desktop/build/icon.png> |
| Goose | `goose.png` | PNG，256×256 | 8,443 | <https://github.com/aaif-goose.png?size=256> |
| WorkBuddy | `workbuddy.png` | PNG，104×104 | 12,594 | <https://assets.workbuddy.cn/web/workbuddy/8e036eb06f87c6b08437546b3cbd58e60af45b10/assets/workbuddy-DpjgTAHk.png> |
| DeepSeek Harness | `deepseekharness.png` | PNG，360×360 | 5,181 | <https://cdn.deepseek.com/chat/icon.png> |
| 豆包 | `doubao.png` | PNG，192×192 | 34,367 | <https://lf-flow-web-cdn.doubao.com/obj/flow-doubao/favicon/new-doubao/192x192.png> |

## SHA256

| 文件 | SHA256 |
| --- | --- |
| `ollama.png` | `5c5528504c307d34af504f39bc4e7007d2f6f31ee00dab699cc91584d1af8aca` |
| `jan.png` | `959cc9db27d57d34b516b9abef28c3240b1dc836c50f6ffafa9215beb699c81b` |
| `cherrystudio.png` | `8706b76d225c761820b18e7d79c3c8d0dd9f712b3d6a6737d90fb96e7c8fc2a4` |
| `anythingllm.png` | `4682b8d2633bea9438a1b620ae6b352fc4fde8db4468df569739868101965803` |
| `gpt4all.png` | `8358ab91341a9e46673b4e57693d6e026d829cbac103b24510c8148c22e9b694` |
| `lobehub.png` | `4cd10694c9b9e803e76a414f20826ff789e5942787ff7295017bdc33d81eced8` |
| `goose.png` | `36962e3845cc6b2df3d9acd6eb94faf0c37d7a131126432d2d1d6e58748d77d9` |
| `workbuddy.png` | `67594377193ef4b4ffb924125eae8a873883ff320941c30ff0bd863fac313700` |
| `deepseekharness.png` | `f5d63a296b1b50b77c3a3528534000a95b6b3a5ff306a4230f9b1dfef15ee88c` |
| `doubao.png` | `dabb2abd94e3a6c11e7f1a42c9343839b6ef643c086751b71afbc68f615aec38` |

## 说明

- 每个文件都通过 PNG 字节标记检查（`89 50 4E 47 0D 0A 1A 0A`）和体积检查（2 KB–300 KB），并已逐个目视确认图案与软件一致。
- Ollama 的图标取自官网站点公开的 `public/ollama.png`，竖版 181×256；其余为方形应用图标。GPT4All 仓库只提供 48×48 的 PNG 图标，尺寸偏小但为官方原图，未做放大处理。
- WorkBuddy 官网只把图标作为 SVG favicon 和 PNG 品牌图提供：`workbuddy.png` 取自官网首页 JS bundle 引用的 `assets/workbuddy-DpjgTAHk.png`（绿色圆角方块 Logo，104×104 原始尺寸），未把 SVG 或 ICO 转码。该文件路径带官网构建哈希，每次前端发布都会变化，本行记录的是本次获取时的地址。
- DeepSeek Harness 的图标取自 `download.deepseek.com` 下载页 `<link rel="apple-touch-icon">` 声明的 `https://cdn.deepseek.com/chat/icon.png`（DeepSeek 蓝色鲸鱼标志，360×360）。
- 豆包图标取自官网 favicon 的 192×192 PNG（`new-doubao/192x192.png`），即豆包桌面客户端的官方头像图案。

