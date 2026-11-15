# 360 应用中心接口调研

本文记录 360 软件管家/应用中心（<https://cdn.soft.360.cn/view/home.html#/app/cate>）的公开接口，
用于 WinAppsManager 的 `soft360` 软件源实现。所有接口均为 HTTPS JSON，**不需要 token 或签名**
（页面搜索框走的是 `enc_client` 签名接口，本项目的搜索改为在后端缓存目录内本地匹配，不做逆向）。

> 调研方式：浏览器 DevTools（网络面板 + UI 操作），2026-11。
> 360 可能随时调整接口或字段，失效时以实际响应为准。

## 1. 目录列表

```http
GET https://cdn-mgr.soft.360.cn/main/cdn_client/v1/soft/list
    ?cate_id={分类ID}&page_size=100&src=web_client&machine=&page={页码}
```

- `page_size` 最大 **100**（`200`/`500` 返回 `errno=10001`）。
- `page` 从 1 开始；返回 `data` 为空数组表示该分类结束。
- 响应：

```json
{
  "errno": 0,
  "errmsg": "",
  "time": 1794670045,
  "data": [
    {
      "soft_id": 600001358,
      "logo": {
        "32": "https://p8.qhimg.com/dm/72_72_100/t11eec6e52fecc71f655bb449f0.png",
        "48": "https://p8.qhimg.com/dm/72_72_100/t11eec6e52fecc71f655bb449f0.png",
        "48_trans": "https://p8.qhimg.com/dm/72_72_100/t11eec6e52fecc71f655bb449f0.png",
        "72": "https://p8.qhimg.com/dm/72_72_100/t11eec6e52fecc71f655bb449f0.png"
      },
      "name": "微信",
      "bname": "微信",
      "brief": "一种生活方式",
      "desc": "微信是腾讯旗下的一款跨平台的通讯工具……",
      "soft_size": 259798216,
      "soft_score": 74
    }
  ]
}
```

字段说明：`soft_size` 是安装包字节数；`soft_score` 为 0–100 的整数（页面显示为除以 10 的星级）；
`logo.48`/`logo.72` 是 qhimg CDN 图标，http/https 均可用。

## 2. 分类

页面分类按钮与 `soft/list` 的 `cate_id` 对应关系（通过逐个点击分类观察网络请求得到）：

| 分类 | cate_id | 分类 | cate_id |
| --- | --- | --- | --- |
| 聊天工具 | 149 | 安全杀毒 | 155 |
| 视频软件 | 150 | 图形图像 | 154 |
| 浏览器 | 157 | 系统工具 | 162 |
| 办公软件 | 159 | 手机数码 | 165 |
| 输入法 | 156 | 股票网银 | 158 |
| 音乐软件 | 151 | 压缩刻录 | 161 |
| 游戏娱乐 | 153 | 阅读翻译 | 100000186 |
| 网络游戏 | 100000409 | 网络应用 | 168 |
| 手游电脑版 | 1900000002 | 主题壁纸 | 100000376 |
| 下载工具 | 152 | 教育学习 | 167 |
| 编程开发 | 163 | 其它软件 | 169 |
| XP专区 | 1900000001 | win7专区 | 1900000010 |

页面上“全部软件”显示约 **43,731** 款。全量抓取会使客户端目录过大，因此后端默认只抓
**各分类前 3 页（100/页）**，共 24 分类 × 3 页 = 7200 条原始记录，按 `soft_id` 去重后约
**5940 款**（与联想源的 5111 款规模相当）。可通过 `soft360.catalog_pages_per_category`
（1–20）调整；分类之间几乎不重叠（实测 3 页/分类时去重率 0.02%）。

## 3. 详情

```http
GET https://mgr.soft.360.cn/main/cdn_client/v1/soft/getinfo
    ?scene=1&machine=&soft_id={id1,id2,...}
```

- 一次最多约 **51 个** `soft_id`（100 个返回 `errno=10002`），用逗号分隔；返回 `data` 是以
  `soft_id`（字符串）为 key 的字典。
- 单条重要字段：

```json
{
  "name": "微信",
  "bname": "微信",
  "corp": "",
  "desc": "微信是腾讯旗下……",
  "brief": "一种生活方式",
  "logo": { "48": "https://p8.qhimg.com/..." },
  "snapshot": [
    "http://p0.qhimg.com/t11098f6bcd82d1f1917be4989d.png",
    "http://p0.qhimg.com/t11098f6bcd4f5a2c373b5d4c18.png",
    "http://p0.qhimg.com/t11098f6bcd345d60aee8e24690.png"
  ],
  "soft_id": 600001358,
  "soft_size": 259798216,
  "soft_score": 74,
  "static": 1,
  "baoku_cate": { "id": 149, "name": "聊天工具" },
  "edition_info": {
    "default_edition": {
      "version": "4.2.0.52",
      "os": { "bit": [64], "ver": [4, 5, 6, 7, 8] },
      "pubtime": 1794377199,
      "brief": "更新说明……",
      "down": {
        "default": {
          "name": "微信_4.2.0.52_1794377199.exe",
          "size": 259798216,
          "md5": "36b432f3a9b7c67fe09c510ee532bcfc",
          "p2p": "pdown://p2=...|b2=...|...|https://cdn-download.soft.360.cn/softmanager-installation/20261111/600001358_1677df4eee21241f4e11c48c93231786.exe|||https://dldir1v6.qq.com/weixin/Universal/Windows/WeChatWin_4.2.0.exe",
          "multi_urls": "",
          "multi_down": 0
        }
      }
    }
  }
}
```

### 下载地址

- 360 的下载 URL 内嵌在 `down.default.p2p` 的 `pdown://` 描述串里（用 `|` 拼接，后段是
  360 CDN 与厂商 URL，`|||` 分隔），另有 `multi_urls` 可能包含候选地址；应使用正则提取全部
  `http(s)://` 候选。
- 优先 `https`（`cdn-download.soft.360.cn` 等），没有 https 时退回 http；扩展名只接受
  `.exe/.msi/.msix/.appx`；文件名用 `down.default.name` 清洗；`size` 用 `down.default.size`
  或 `soft_size`；`md5` 不是 sha256，不要写入 `sha256` 字段。
- 安装包 CDN 支持 Range（实测 `Range: bytes=0-1` 返回 206，首字节 `MZ`）。

### 架构与系统

- `edition_info.default_edition.os.bit`：`[32,64]` / `[64]` / `[32]`；据此推断 x86/x64。
- `os.ver`：1–8 的枚举，推测从 Windows 2000/XP 到 Windows 10；本项目把低于 7 的值
  统一按最低 Windows 7 处理（客户端只支持 Win7+）。
- `corp` 常为空（尤其非 360 自家软件），发布者缺失时回退到 `bname`.

## 4. 图片

- logo 与 `snapshot` 都在 `*.qhimg.com`（个别在 `*.mediav.com`），http/https 均可直接访问；
  后端返回详情截图时优先给出 **https CDN 绝对地址**（客户端直连，减少后端带宽），同时附
  `proxy_url`（`/api/v1/store/360/...`）作为直连失败时的兜底；图标同理（`icon_url` + `icon_proxy_url`）。
- 不再维护固定域名白名单：`p15.qhmsg.com` 等任意安全公网 http(s) 图片域名都可直连；只拒绝 localhost、私网/回环 IP、userinfo 等不安全地址，校验失败才回退后端代理。

## 5. 搜索与限制

- 页面搜索框会跳到 `#/app/result`，实际调用 `mgr.soft.360.cn/main/enc_client/v2/search/suggest`
  与 `/search/search`（带 `sign`、`timestamp`、`rand` 等参数的 **加密签名接口**）。
- 本项目已逆向该搜索接口：AES-128-CBC、RSA PKCS#1 v1.5 与 MD5 签名的完整算法、常量、请求/响应
  字段见 [soft360-search-api.md](soft360-search-api.md)。`GET /api/v1/store/360/search` 现在覆盖
  360 全量目录（约 43,731 款），不再局限于缓存子集。
- 逆向接口失效或解密失败时，后端自动回退到对缓存目录的本地匹配（不会 500）。
- 目录本身仍是“各分类前 N 页”热门子集；要扩大目录覆盖面可调 `soft360.catalog_pages_per_category`
  （1–20，默认 3），抓取时间、目录文件与客户端内存会线性增长。

## 6. curl 示例

```bash
curl 'https://cdn-mgr.soft.360.cn/main/cdn_client/v1/soft/list?cate_id=149&page_size=100&src=web_client&machine=&page=1'
curl 'https://mgr.soft.360.cn/main/cdn_client/v1/soft/getinfo?scene=1&machine=&soft_id=600001358'
curl 'https://mgr.soft.360.cn/main/cdn_client/v1/category/list?from=softmgr_webclient' # 404：分类接口仅加密版
```
