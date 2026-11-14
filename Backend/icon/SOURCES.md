# 固定软件图标来源

以下图标在 2026-11-09 一次性获取并保存到本目录。软件目录使用本地 `icon_file` 文件名，后端运行时读取这些文件，不再向官网获取图标。

PNG 和 ICO 均保留下载得到的原始字节，没有调整图案、颜色、尺寸或透明度。ICO 原生保留多尺寸图层。QQ 是项目已有资源，本次保留原文件；其最初下载 URL 未记录。

| 软件 | 本地文件 | 格式及尺寸 | 字节数 | 官方来源 |
| --- | --- | --- | ---: | --- |
| QQ | `qq.png` | PNG，250×300 | 24,556 | 项目已有资源，未重新下载 |
| 微信 | `wechat.png` | PNG，180×180 | 3,247 | [微信官网 apple-touch-icon](https://res.wx.qq.com/a/wx_fed/assets/res/OTE0YTAw.png) |
| Visual Studio Code | `vscode.png` | PNG，1024×1024 | 114,937 | [VS Code 官网稳定版品牌图标](https://code.visualstudio.com/assets/branding/code-stable.png) |
| 7-Zip | `7zip.ico` | ICO，16×16、32×32、48×48 | 4,846 | [7-Zip 官方源码中的 File Manager 图标](https://raw.githubusercontent.com/ip7z/7zip/main/CPP/7zip/UI/FileManager/FM.ico) |
| VLC | `vlc.png` | PNG，256×256 | 54,221 | [VideoLAN 官方源码](https://raw.githubusercontent.com/videolan/vlc/master/share/icons/256x256/vlc.png) |
| Firefox | `firefox.png` | PNG，256×256 | 32,441 | [Mozilla 官方 Firefox branding](https://raw.githubusercontent.com/mozilla-firefox/firefox/main/browser/branding/official/default256.png) |
| OBS Studio | `obs.png` | PNG，256×256 | 34,924 | [OBS 官网图标](https://obsproject.com/assets/images/new_icon_small-r.png) |
| Notepad++ | `notepadplusplus.ico` | ICO，16×16、32×32、48×48、64×64、128×128、256×256 | 186,712 | [Notepad++ 官方源码](https://raw.githubusercontent.com/notepad-plus-plus/notepad-plus-plus/master/PowerEditor/src/icons/npp.ico) |

## SHA256

| 文件 | SHA256 |
| --- | --- |
| `qq.png` | `af70ba09c8ba100cdfe806d8d94a0eedf3502e351b89a640b474eeb0b4aaa624` |
| `wechat.png` | `058bf1b3661dac713bb1807fbadbe1e6c2b1f5513d93ecae61c986ebb76b1edf` |
| `vscode.png` | `7ac29e00cd66e549acc2ea6a99a813f92cb23a6d165293be6959845670f0c5a4` |
| `7zip.ico` | `649f8e81cce9d8e3ab9a0d06db31788f5ced3c6ed99a78304a87dba35ecf06cc` |
| `vlc.png` | `a1ccd4a5006713dc65ad84f41281ce732c23533555debc4e71f6457bb26b8fa1` |
| `firefox.png` | `395e0e25367f7f8b277213ea18c16c89adb12321a402ca4ba503c41fe1e2fc6f` |
| `obs.png` | `cc4b94efae24b4a571131526361352787a9193c01ffe09ce5bb018710ce666c9` |
| `notepadplusplus.ico` | `7f00c181277bddfe06f51e7712c713e24e29a167b20033c43829136cdadc655d` |

文件通过 PNG/ICO 字节标记检查和离线图片解码检查，并已逐个目视确认。无需为这些固定资源添加运行时图像处理依赖。
