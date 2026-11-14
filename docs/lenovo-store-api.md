# 联想电脑应用商店（PC Store）接口调研文档

本文记录 WinAppsManager 接入“联想软件应用商店源”所需的服务端接口。所有结论均在 2026-11-14
通过真实请求逐条验证（curl）。后端把这些请求全部封装在服务端，客户端只访问本项目后端。

- 服务根地址：`https://pc-store.lenovomm.cn`
- 协议：HTTPS，`Content-Type: application/json`（响应为 JSON，错误也常以 HTTP 200 + JSON 返回）
- 认证：除“下载地址列表”“应用详情列表页下载跳转”外，其余接口都需要请求头 `token: <JWT>`
- 网关：`istio-envoy`，响应无 `Cache-Control`/`ETag`，因此缓存必须由本项目的后端实现
- UA：接口层对 UA 不敏感（curl 默认 UA 也可以）；**图片 CDN 会拒绝 `curl/8.x` 这类 UA
  （直接 403）**，必须使用浏览器 UA 或 `WinAppsManager/1.0` 这类非 curl UA

> 响应包络（绝大多数接口）：
>
> ```json
> { "status": 0, "message": null, "data": ... }
> ```
>
> `status` 正常时为数字 `0`；失败时**可能是字符串**（例如 token 失效为
> `{"message":"token invalid","status":"502"}`，HTTP 状态仍是 200）。解析时必须同时兼容
> 数字和字符串，且先判断 `status` 再读 `data`。

---

## 1. 设备注册与 token

### 1.1 注册 token

```
POST https://pc-store.lenovomm.cn/appbase/token/register
Content-Type: application/json
```

请求体（设备指纹，可随机生成）：

```json
{
  "ch": "9.0.3400.1106",
  "cpuIdentifier": "Intel64 Family 6 Model 165 Stepping 3",
  "cpuVendor": "GenuineIntel",
  "mac": "000c2905cb33",
  "mt": "None",
  "os": "win10",
  "osBit": "64",
  "osVersion": "10.0.19045",
  "sn": "a9be331a11f77fbaf01a5df71522e2bdc8ffef43a1cc3978d660b594f3e2f8a3",
  "uuid": "0B0C4D56-85B6-C27F-6232-D0F27505CB33"
}
```

字段说明：

| 字段 | 含义 | 本项目的取值策略 |
| --- | --- | --- |
| `ch` | 客户端渠道/版本号 | 固定 `9.0.3400.1106`（已验证可用） |
| `cpuIdentifier` | CPU 型号字符串 | 固定示例值即可 |
| `cpuVendor` | CPU 厂商 | `GenuineIntel` / `AuthenticAMD` 等 |
| `mac` | 网卡 MAC，12 位小写 hex | 每台机器随机生成一次并持久化 |
| `mt` | 机器类型 | 固定 `"None"` |
| `os` | 系统代号 | `win10`（Win11 也报 win10 可以） |
| `osBit` | 位数 | `64` |
| `osVersion` | 系统版本号 | `10.0.19045` 一类 |
| `sn` | 设备序列号（64 位 hex 字符串） | 随机生成并持久化 |
| `uuid` | 设备 UUID（大写 GUID 字符串） | 随机生成并持久化 |

实测：字段可以随机；甚至只传 `{"mac": "aabbccddeeff"}` 也能拿到 token（其余字段服务端补空）。
正常请求返回：

```json
{
  "status": 0,
  "message": null,
  "data": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJvcyI6IndpbjEwIiwi... "
}
```

`data` 是 JWT（HS256）：

- Header：`{"typ":"JWT","alg":"HS256"}`
- Payload 包含注册时提交的字段 + `exp`，例如：

```json
{
  "os": "win10",
  "osVersion": "10.0.19045",
  "ch": "9.0.3400.1106",
  "cpuVendor": "GenuineIntel",
  "mt": "None",
  "osBit": "64",
  "sn": "a9be331a...f8a3",
  "exp": 1795943668,
  "uuid": "0B0C4D56-85B6-C27F-6232-D0F27505CB33",
  "mac": "000c2905cb33"
}
```

- 实测 `exp` = 注册时间 + **15 天**（约 1 296 000 秒）。本项目缓存 token 到 `exp - 1h`
  再重新注册；token 与设备指纹可一起落盘（`Backend/.cache/lenovo/token.json`），重启复用。
- 服务端并不校验 token 与后续请求“同机”，一个 token 可用于所有业务接口。
- 携带错误/缺失 token 的处理：HTTP 200 + `{"message":"token invalid","status":"502"}`。

---

## 2. 分类树

```
GET https://pc-store.lenovomm.cn/classifyservice/class/composite_class_list
Header: token: <jwt>
```

返回 `data` 是顶层分组数组（`soft` 软件、`game` 游戏），每组下按 `type`（`pc` / `web`）分
“PC / 网页”，`type=pc` 下是具体分类：

```json
{
  "status": 0,
  "message": null,
  "data": [
    {
      "code": "soft",
      "name": "软件",
      "categories": [
        {
          "type": "pc",
          "name": "PC",
          "appClassList": [
            { "classId": "-1", "className": "全部", "tags": null },
            { "classId": "1", "className": "聊天社交", "tags": null },
            { "classId": "2", "className": "输入法", "tags": null }
          ]
        },
        { "type": "web", "name": "网页", "appClassList": [ ... ] }
      ]
    },
    { "code": "game", "name": "游戏", "categories": [ ... ] }
  ]
}
```

实测 PC 分类（2026-11-14）：

- `soft`（软件）19 个：聊天社交 1、输入法 2、浏览器 3、下载工具 4、视频软件 5、音乐软件 6、
  图片图形 7、安全防护 8、解压刻录 9、系统工具 10、驱动工具 11、办公教育 12、编程软件 13、
  理财金融 14、剪辑工具 15、助手工具 17、桌面美化 18、智能生成 40、其他应用 99
- `game`（游戏）9 个：角色扮演 19、动作冒险 20、飞行射击 21、体育竞速 22、休闲益智 23、
  MOBA 24、模拟经营 25、战争策略 26、平台工具 28

> `tags` 字段在部分分类上有值（如 web 分类或游戏 MOBA 的 `{tagId:100,tagName:"MOBA"}`），PC
> 软件分类基本为 `null`；列表请求的 `tagId=-1` 表示不过滤标签。

---

## 3. 分类应用列表（分页）

```
GET https://pc-store.lenovomm.cn/classifyservice/class/composite_class_app_list
    ?code=soft            # soft | game
    &id=-1                # 分类 id；-1 = 该组“全部”
    &pageIndex=1          # 固定 1（分页由 skip 控制，pageIndex 无实际作用）
    &type=pc              # pc | web
    &tagId=-1             # -1 = 全部标签
    &refresh=true         # 建议带 true
    &skip=0               # 偏移量（0 起）
    &limit=100            # 每页条数，实测最大 1000（limit=1000 可一次取完一个分类）
Header: token: <jwt>
```

返回：

```json
{
  "status": 0,
  "message": null,
  "data": {
    "count": 967,
    "desc": "全部",
    "code": "ranklist:pc_category_2001_-1",
    "imgUrl": null,
    "bizInfo": null,
    "apps": [ { ...AppItem... } ]
  }
}
```

- `data.count` 是该分类（或该组全部）的**应用总数**，`apps` 是当前页。
- `skip` 是真实的偏移量：`skip=36` 返回第 37 条起的数据；`pageIndex=2&skip=0` 仍返回第一页，
  说明 `pageIndex` 被忽略，只需递增 `skip`。
- `limit` 实测最大 1000；一次请求 `limit=1000` 可拿完一个分类（超过 1000 条的分类目前没有）。
- 实测规模（2026-11-14）：

| 列表 | count |
| --- | --- |
| `code=soft&id=-1`（软件全部） | 967 |
| `code=game&id=-1`（游戏全部） | 530 |
| 软件 19 个分类逐一抓取、按 `softID` 去重 | **4579** |
| 游戏 9 个分类逐一抓取、按 `softID` 去重 | **532** |
| 软件 + 游戏合计去重 | **5111**（两组无交叉） |

> 关键坑：`id=-1` 的“全部”列表**并不等于所有分类的并集**。微信电脑版（`11488`）、QQ
> （`L105867`）等应用只出现在“聊天社交”等分类列表里，不在 `soft` 的“全部”967 条中；反过来
> “全部”里还有约 40 个应用不在任何分类列表里。要拿到完整、可搜索的目录，必须**逐分类抓取
> 后按 `softID` 合并去重**（可再补抓两组“全部”）。

### 3.1 列表 AppItem 字段（重要字段）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `softID` | string | 应用 ID，数字（`11488`）或 `L` 前缀（`L105867`） |
| `softName` | string | 应用名称 |
| `version` | string | 当前版本 |
| `installFileSize` | string | 安装包大小，如 `318.95 MB`、`525KB`、`26.91 GB`、`1,000.7 MB` |
| `introduction` | string | 一句话简介 |
| `logoFile` | string(url) | 图标 URL（png/jpg/gif/ico/bmp 都有，见“图片 CDN”） |
| `captureFileList` | string[] | 截图 URL（列表页给的是纯 URL 数组） |
| `score` | string | 评分 0–100（字符串数字） |
| `downloadCount` | string | 下载量文案，如 `277万次` |
| `downloadCountDesc` | string | 如 `277万次安装` |
| `supportOsbit` | string[] | `["64"]`、`["32","64"]`、`["64","32"]` |
| `supportPlatform` | string[] | `win7/win8/win8_1/win10/win11/vista/winxp/...`，顺序不固定，可能重复语义 |
| `adaptPlatform` | string[] | `X86` / `X86+ARM`（与位数无关，别拿来当架构） |
| `stable` | string | `stable` / `beta` |
| `isPlugin` | string | 实测 `""` / `"1"` / `"2"` / `"plugins"` / `"noplugins"`，含义不明，不要过滤 |
| `isAiApp` | int | 是否 AI 应用（0/1） |
| `classID` | string/null | 主分类 id（与分类树 classId 对应；极少数为 null） |
| `downloadCountDesc` | string | 下载量说明 |
| `commentUserCount` | int | 评论数 |
| `bizInfo` | string(json) | 埋点信息，可能含 `rankCode` |
| `adKey` / `isIntervene` / `itemType` | string/null | 推广/干预标记，本项目不使用 |
| `downLoadUrl` | string | 固定的 `https://pc-store.lenovomm.cn/dlservice/download?softid=...` 跳转地址（302 到旧 CDN） |

实测数据质量（5111 条）：`softID` 全唯一；`version/installFileSize/logoFile/introduction/score/
downloadCount` 均非空；`supportOsbit` 只有 `64`、`32+64`、`64+32` 三种；`supportPlatform` 除 1 条
为空外都包含 `win10`/`win11`；`stable=beta` 的极少。

---

## 4. 应用搜索

```
GET https://pc-store.lenovomm.cn/searchcontent/fusion/v2/app_search
    ?searchWord=QQ
    &searchMode=0
    &tab=0
    &skip=0
    &limit=30
    &algoBizInfo=
    &firstId=
    &androidType=0
    &type=0
    &category=0
    &serviceBizInfo=
    &wallpaperType=0
    &sort=0
Header: token: <jwt>
```

- `searchWord` 需要 URL 编码（中文、空格）。
- `tab` 必须为 `0`；`tab=1/2` 返回 `{"status":-1,...}`。`type=0`、`sort=0/1/2`、`category=0`
  在当前 PC 应用搜索下没有观察到差异，按示例原样传即可。
- 返回结构：

```json
{
  "status": 0,
  "message": null,
  "data": {
    "bizInfo": "{\"appSearchAlgo\":\"app-search-pc-v5.9\",\"resultCount\":31,...}",
    "algoBizInfo": "eyJ0eXAiOiJKV1Qi...",
    "count": { "appsCount": 31, "wallpapersCount": 0, "postsCount": 282, "coursesCount": 3 },
    "dataList": [ { ...搜索 AppItem... } ]
  }
}
```

- `data.dataList` 元素 `itemType=1` 表示应用；当前只返回应用。
- 搜索 AppItem 比列表 AppItem 多若干字段（`classRankingDesc`、`tags`、`appLevel`、`isAdvert`、
  `searchTag`、`editorRecommend`、`manual` 等），`captureFileList` 默认是空数组
  （`showCaptureFileList="0"`）。
- 返回条数上限约 31 条（`limit=30` 仍返回 31），`skip`/`limit` 基本不生效（`skip=30` 仍返回同
  一批结果）；一次搜索给出“前 31 条”，适合做结果列表，不适合分页。
- **广告**：`isAdvert="4"` 的条目是推广（例如搜索“微信”时 WorkBuddy、豆包、剪映等会被插入）；
  `isAdvert="0"` 是自然结果。本项目需要搜索时可按需过滤或标记。
- 搜索有“兜底推荐”行为：搜不到时并不返回空列表，而是返回一批推荐应用（实测乱码关键词也返回
  30 条），所以不能靠“结果为空”判断是否命中，需要按名称/简介文本自行匹配过滤。
- 搜索覆盖软件 + 游戏（搜“英雄联盟”“原神”都能命中游戏）。

---

## 5. 应用详情

```
GET https://pc-store.lenovomm.cn/appstorecontents/app/details_new?softid=L105867
Header: token: <jwt>
```

成功：

```json
{
  "status": 0,
  "message": null,
  "data": {
    "softID": "L105867",
    "softName": "QQ",
    "version": "9.9.37.54395",
    "installFileSize": "318.95 MB",
    "detailInfo": "详细描述文本…",
    "score": "60",
    "logoFile": "https://lsw-fast.lenovo.com.cn/…/logo.png",
    "captureFileList": [
      { "type": "0", "url": "https://lsw-fast.lenovo.com.cn/…/img/xxx.png", "videoImg": null, "videoTag": null },
      { "type": "1", "url": "https://pc-store-video.lenovomm.cn/…-sd.mp4?auth_key=…",
        "videoImg": "https://imgs.lenovomm.cn/…/thumb.jpg", "videoTag": null }
    ],
    "whatNew": "更新说明…",
    "createdTime": "2026-11-12 13:34:44",
    "downloadUrl": "https://pc-store.lenovomm.cn/dlservice/download?softid=L105867",
    "downloadCount": "1.01亿次",
    "detailsTag": [ { "icon": "...", "name": "安全认证", "tips": "软件通过多款主流安全软件检测…" } ],
    "warmTips": null,
    "softType": "soft",
    "cover": {
      "coverImg": "https://imgs.lenovomm.cn/…jpg?x-oss-process=…",
      "videoId": "b061…", "videoUrl": "https://pc-store-video.lenovomm.cn/…-sd.mp4?auth_key=…",
      "showVideo": "1", "coverImgBig": "https://imgs.lenovomm.cn/…jpg", "videos": null
    },
    "hardwareConfigurationList": null,
    "classRankingDesc": { "targetDesc": "聊天社交类下载榜第2名", "targetType": "11", "targetContent": "..." },
    "editorRecommend": null,
    "activityList": [ ... ],
    "hasGiftBag": null,
    "hasCollect": 0,
    "appRecommend": { "softId": "26137", "softName": "雷神加速器(官方)", "logoFile": "...",
                      "recommendDesc": "...", "confirmButtonDesc": "立即下载", "version": "..." },
    "appLabelList": [ { "labelName": "办公辅助工具", "targetType": "11", "targetContent": "..." } ],
    "lenovoClassId": "40",
    "originalPrice": null, "currentPrice": null, "payType": 0,
    "isAiApp": 1,
    "developer": {},
    "gameCdkExtInfo": null
  }
}
```

- `captureFileList[].type`：`"0"` 图片，`"1"` 视频（`url` 是带 `auth_key` 的临时视频地址，
  `videoImg` 是视频缩略图）。**视频 URL 有时效（auth_key），不要长期缓存**；本项目只使用缩略图。
- `cover.coverImg` 可能是空字符串（部分应用没有横版封面）。
- `detailsTag` 通常是“安全认证”等标签；`appLabelList` 是分类标签；`classRankingDesc.targetDesc`
  是榜单文案；`appRecommend` 是详情页的关联推荐（可能为 `null`）。
- 数据质量：`detailInfo`/`whatNew` 部分应用为空；`captureFileList` 条数 1–10，常见 3–7。
- 软件已下架时仍为 HTTP 200：

```json
{ "status": -1, "message": "软件已下架", "data": null }
```

---

## 6. 下载地址

### 6.1 下载地址列表（无需 token）

```
GET https://pc-store.lenovomm.cn/dlservice/getPcSoftDownloadUrlList?softid=L105867
```

响应：

```json
{
  "status": 0,
  "message": "",
  "data": {
    "downloadCode": 2,
    "downloadUrls": [
      { "downLoadUrl": "https://pc-lsw7.lenovomm.cn/dlserver/…?sign=…", "downloadCode": "hs2",
        "installFileSize": null, "md5": null },
      { "downLoadUrl": "http://pc-lsw7.lenovomm.cn/dlserver/…?sign=…", "downloadCode": "hs" },
      { "downLoadUrl": "https://pc-lsw6.lenovomm.cn/…", "downloadCode": "bs2" },
      { "downLoadUrl": "http://pc-lsw6.lenovomm.cn/…", "downloadCode": "bs" },
      { "downLoadUrl": "https://lsw-fast.lenovo.com.cn/appstore/apps/adp/pkg/…/QQ9.9.37.54395_lenovo_x64.exe",
        "downloadCode": "firstOldDownloadUrl" },
      { "downLoadUrl": "http://ams.lenovomm.com/fdl/api/fileaddress?…", "downloadCode": "amsHttpsDownloadUrl" },
      { "downLoadUrl": "http://lsw-fast.lenovo.com.cn/…", "downloadCode": "secondOldDownloadUrl" },
      { "downLoadUrl": "http://ams.lenovomm.com/fdl/api/fileaddress?…", "downloadCode": "amsHttpDownloadUrl" }
    ],
    "fileName": "QQ9.9.37.54395_lenovo_x64.exe",
    "hasContinue": 0,
    "netCoreInfo": { "netCoreUrl": "https://pc-lsw7…", "reportIdentifier": "L105867", "reportUrl": "..." },
    "sha256": "7019f1a55557b4260cb7385f015a521378748d06dfd9915905e14f0585f4e855",
    "needReportDownloadComplete": 0,
    "needReportInstallComplete": 0
  }
}
```

要点：

- 返回 8 个候选地址，成对出现（https/http × 两类 CDN + 旧 CDN + AMS 跳转）。
- **优先选 `hs2`（https, `pc-lsw7`）**，其次 `bs2`（https, `pc-lsw6`）、
  `amsHttpsDownloadUrl`、`firstOldDownloadUrl`；这些地址都支持 Range。
- 带签名的 `pc-lsw7`/`pc-lsw6` 地址对 UA 不敏感（空 UA / curl UA 都能 206）；
  **旧 CDN `lsw-fast.lenovo.com.cn` 会拒绝 curl UA（403），必须用非 curl UA**。
  客户端下载时统一带一个正常 UA 即可。
- **地址有时效**：URL 里有 `sign`/`ts`/`t` 参数，属于临时签名地址，不能长期缓存，使用前重新
  请求本接口获取。
- `fileName` 可能是 URL 编码的（如含 `%28` 括号），需要 `urllib.parse.unquote`；实测可能形如：
  `QQ9.9.37.54395_lenovo_x64.exe`、`QianwenPC_V5.2.0.298_pc_pf3000_(zh-cn)_release_(Build3280179-1000-x64-lx).exe`、
  `Jianying_11_7_0_14652_jianyingpro_lenovo_pay_creatortool.exe`、纯数字文件名等；统一按 Windows
  文件名规则清洗，非法时回退 `应用名_版本.exe`。
- `sha256` 通常有值，可用于下载校验；`installFileSize` 字段为 `null`，大小取列表/详情里的
  `installFileSize` 文案解析。
- 无下载地址（如已下架/无包）时：`{"status":0,"data":{"downloadCode":1,"downloadUrls":null,...}}`，
  此时要按“暂无下载地址”处理。

### 6.2 下载跳转（302，旧逻辑）

```
GET https://pc-store.lenovomm.cn/dlservice/download?softid=L105867
→ HTTP 302 Location: http://lsw-fast.lenovo.com.cn/appstore/apps/adp/pkg/…/QQ9.9.37.54395_lenovo_x64.exe
```

客户端/后端可以不用它，直接用 6.1 的地址列表（更新更全、有 https 与 sha256）。

---

## 7. 图片 CDN

图标、截图、封面分别来自：

- `lsw-fast.lenovo.com.cn`（logo、截图）
- `imgs.lenovomm.cn`（详情 cover、视频缩略图）
- `pc1.gtimg.com`（部分 logo，第三方图床）
- `pc-store-video.lenovomm.cn`（视频，仅缩略图可用）

实测：

- **必须带非 curl UA**：`curl -A "curl/8.x"` 返回 403；`curl -A "WinAppsManager/1.0"` 或浏览器 UA
  返回 200。后端代理图片时统一使用浏览器 UA。
- 实际格式：logo `png` 4375 / `jpg` 123 / `gif` 58 / `ico` 20 / `jpeg` 2 / `bmp` 1；
  截图 `png` 12159 / `jpg` 2086 / `jpeg` 18 / `gif` 2。**客户端图标缓存必须支持 png/jpg/gif/ico/bmp**。
- 图片 URL 中的文件名带时间戳，可视为内容不变（适合长 TTL 磁盘缓存 + ETag）。
- 没有 `Content-Disposition`/`ETag` 等元数据，后端缓存以 URL 为 key 即可。
- 实测尺寸：logo 约 20–40 KB，截图约 100–300 KB。

### 7.1 图标直连 CDN（客户端列表）

列表/搜索接口里的 `logoFile` 是绝对 URL，客户端可以**直接**从联想图片 CDN 取图标，不需要
每个图标都经过本项目后端代理（后端只在列表里返回原始地址，并额外给出代理路径作为兜底）。

- **UA 要求**：CDN 会拒绝 `curl/8.x` 这类 UA（HTTP 403）。客户端 HttpClient 必须带浏览器 UA
  （例如 `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)
  Chrome/120.0.0.0 Safari/537.36`）或 `WinAppsManager/1.0` 这类非 curl UA；空 UA 实测也能返回，
  但不要依赖该行为。
- **格式**：logo 实测 png 4375 / jpg 123 / gif 58 / ico 20 / jpeg 2 / bmp 1。客户端图片解码与
  磁盘缓存必须支持 png/jpg/jpeg/gif/ico/bmp；WPF 解码 ICO 时按 ICO 目录选最大帧（PNG 帧直接
  解码、DIB 帧重封装），失败再回退整个容器。
- **缓存**：logo URL 的文件名带时间戳，内容可视为不变；客户端按 URL（sha256）做磁盘缓存并设置
  长 TTL（例如 30 天）即可，CDN 不提供可靠 `ETag`/`Last-Modified`，不需要条件请求。单个 logo
  约 20–40 KB，列表按行懒加载，**不要**一次预热全部图标。
- **兜底**：直连失败（403/超时/非图片/网络被代理拦截）时再请求本项目后端
  `GET /api/v1/store/lenovo/apps/{soft_id}/icon`：后端带浏览器 UA 拉取并做磁盘缓存，响应带
  `ETag` 与 `Cache-Control: public, max-age=86400`。列表响应本身不携带图标字节。
- 截图/封面仍走后端代理（`screenshots[].url`、`cover_url`），原因同上：CDN 的 403 规则与
  客户端代理设置不受本后端控制。

---

## 8. 结论与后端接入建议

1. **目录/详情/搜索/下载地址等接口请求都在后端完成**，客户端不直接访问联想接口；唯一例外是
   列表/搜索图标：客户端按 `logoFile` 直连联想图片 CDN 并按 URL 做磁盘缓存，后端提供
   `.../apps/{soft_id}/icon` 代理作为兜底。
2. token 由后端自动注册、缓存（`exp - 1h` 前复用），设备指纹随机生成并持久化；遇到
   `token invalid` 自动重新注册并重试一次。
3. 目录 = 两组（soft/game）× 全部分类（19 + 9）逐一 `limit=1000` 抓取，按 `softID` 去重合并；
   每次刷新约 28 个请求，实测 3–6 秒可完成。建议 TTL 6 小时、允许 7 天陈旧快照兜底。
   若要更接近商店“全部”页，可再补抓两组 `id=-1`（各 10 页、6 页）。
4. 详情按 `softid` 单独请求并做内存 + 磁盘缓存（详情里有视频临时地址，长期缓存时丢弃视频
   `url`，只保留 `videoImg` 缩略图）。
5. 下载地址**不缓存**，每次下载前重新解析；优先 https 签名地址，返回 `sha256`、`fileName`、
   解析后的 `size_bytes`。
6. 搜索接口用于补充目录之外的结果（一次最多约 31 条，含广告），按名称/简介做本地过滤后再展示。
7. 截图/封面统一走后端代理 + 磁盘缓存 + ETag；图标默认由客户端直连 CDN（按 URL 磁盘缓存，
   列表按行懒加载），直连失败时回退后端 icon 代理。

### 8.1 常用 curl 验证命令

```powershell
# 1) 注册 token
$body = '{"ch":"9.0.3400.1106","cpuIdentifier":"Intel64 Family 6 Model 165 Stepping 3","cpuVendor":"GenuineIntel","mac":"000c2905cb33","mt":"None","os":"win10","osBit":"64","osVersion":"10.0.19045","sn":"a9be331a11f77fbaf01a5df71522e2bdc8ffef43a1cc3978d660b594f3e2f8a3","uuid":"0B0C4D56-85B6-C27F-6232-D0F27505CB33"}'
$t = (curl.exe -s -X POST "https://pc-store.lenovomm.cn/appbase/token/register" -H "Content-Type: application/json" -d $body | ConvertFrom-Json).data

# 2) 分类树
curl.exe -s "https://pc-store.lenovomm.cn/classifyservice/class/composite_class_list" -H "token: $t"

# 3) 分类列表（skip 分页，limit 最大 1000）
curl.exe -s "https://pc-store.lenovomm.cn/classifyservice/class/composite_class_app_list?code=soft&id=1&pageIndex=1&type=pc&tagId=-1&refresh=true&skip=0&limit=100" -H "token: $t"

# 4) 搜索
curl.exe -s "https://pc-store.lenovomm.cn/searchcontent/fusion/v2/app_search?searchWord=QQ&searchMode=0&tab=0&skip=0&limit=30&algoBizInfo=&firstId=&androidType=0&type=0&category=0&serviceBizInfo=&wallpaperType=0&sort=0" -H "token: $t"

# 5) 详情
curl.exe -s "https://pc-store.lenovomm.cn/appstorecontents/app/details_new?softid=L105867" -H "token: $t"

# 6) 下载地址（无需 token）
curl.exe -s "https://pc-store.lenovomm.cn/dlservice/getPcSoftDownloadUrlList?softid=L105867"

# 7) 图片（必须非 curl UA）
curl.exe -s -A "WinAppsManager/1.0" -o logo.png "https://lsw-fast.lenovo.com.cn/appstore/apps/adp/logo/…png"
```
