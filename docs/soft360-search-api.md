# 360 加密搜索接口调研

360 应用中心的搜索框没有公开 API，页面调用的是 `enc_client/v2/search/search` 加密接口。
本项目的 360 软件源需要“全量搜索”（目录缓存只是各分类热门子集），因此对该接口做了逆向，
在 `Backend/soft360/search.py` 中用**纯 Python**（AES-128-CBC + RSA PKCS#1 v1.5 + MD5）
实现请求加密与响应解密，不引入第三方加密依赖。

> 这是未公开接口，可能随 360 前端更新而变化；失效时 `GET /api/v1/store/360/search`
> 会自动回退到对已缓存目录的本地匹配，不会让搜索接口 500。

## 1. 请求

```http
POST https://mgr.soft.360.cn/main/enc_client/v2/search/search
    ?ver=14.0.0.0&os_ver=7&os_bit=64
    &mid={28位字母数字}&m2={43位字母数字}
    &timestamp={Unix 秒}&sk_type=1&sk_bit=2048
    &rand={1000..9999}&sign={32位小写MD5}
Content-Type: application/json

{"q": "<base64(AES(JSON payload))>", "skey": "<base64(RSA(JSON key/iv))>"}
```

`mid`/`m2` 是设备指纹，实测可每次随机生成（空字符串会返回 `errno=10009 应用参数错误`）。

### q 明文 payload

```json
{
  "kw": "微信",
  "mid": "<同 query 的 mid>",
  "m2": "<同 query 的 m2>",
  "os_bit": "64",
  "os_ver": "7",
  "machine": "",
  "src": "web_client",
  "ext_param": "schannel=",
  "source_id": "",
  "language": "zh-CN",
  "country_code": "CN"
}
```

### 加密算法

- 算法：AES-128-CBC + PKCS#7。
- 固定 AES key：`1234123412ABCDEF`（16 字节 ASCII）。
- 固定 IV：`ABCDEF1234123412`（16 字节 ASCII）。
- `q = base64( AES-CBC( UTF-8(JSON payload) ) )`。
- `skey = base64( RSA_PKCS1v15_encrypt( UTF-8(JSON {"key": base64(AES key), "iv": base64(IV)}) ) )`。
- RSA 公钥（SPKI base64，2048 位 / e=65537）：

```text
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA4RyJX41Y/K/okDxqVwzf2S3Ni2hhYO1dW887gyX6w2+EHzYoOTJ9+DwljvHU9f1Y4IR1UHRILC93yJYg9o8Opq5HTCR5bIa4IeXz1dlBU+WwFEnHLN22Ih2V3XG2kMDpPBgMdXLFYQX2u6P96YaWK192s92NvPPVhl5jX8u58KMMJOCYGHnk7iFT1LSSJCXS0jRh6GD3vkz7wMFUCTq54OHvGEH8CD3m69cRvOvL9UujXpNt3lDnr57OGTJzdF2s1g7HFLmN1rXv1MPIosIDyVRn5vrfB8wjplkzpW33PyLVX3v9nwUBxYADsKC2OTg0M5hRkWFQERMcUe8j3B1t8wIDAQAB
```

### 签名

参与签名的 query 参数（按 key 字母顺序、过滤空值）：

```text
mid, m2, os_bit, os_ver, rand, sk_bit, sk_type, timestamp, ver
```

拼接规则：

```text
material = "m2=...&mid=...&os_bit=64&os_ver=7&rand=...&sk_bit=2048&sk_type=1&timestamp=...&ver=14.0.0.0"
         + skey + q + "sjgo144ddycl3396ddumrn3qmag7eegm"
sign = md5(material).hexdigest().lower()
```

注意：`skey` 与 `q` 使用**原始 base64 字符串**参与签名，不是解码后的字节；空值参数（如 `sdk_ver=""`）不参与拼接。

## 2. 响应

```json
{"errno": 0, "errmsg": "", "time": 1794674243, "data": "<base64 密文>", "skey": "<base64>", "type": 1}
```

解密步骤：

1. 对响应 `skey` 做 RSA **public op**：`pow(int(skey), e, n)`，得到 PKCS#1 v1.5 type-1 padding
   （`00 01 FF... FF 00`），取最后一段 UTF-8 JSON：`{"key": "<base64>", "iv": "<base64>"}`；
2. 用该 key/iv 做 AES-128-CBC 解密 `data`；
3. `data` 明文形如 `{"list": [...]}`。

list 中每条（`type=1` 为软件）常用字段：

```json
{
  "type": 1,
  "soft_id": 600001358,
  "name": "微信",
  "desc": "...",
  "logo": "https://p8.qhimg.com/...",
  "type_name": "聊天工具",
  "size": 259798216,
  "score": 74,
  "soft_baoku_cate_id": 149,
  "os": {"bit": [64], "ver": [4,5,6,7,8]},
  "snapshot": ["https://p5.qhimg.com/..."],
  "free": {"flag": 2, "intro": ""},
  "imptk": ["..."],
  "clktk": ["..."]
}
```

`imptk`/`clktk`/`dltk` 是广告曝光/点击追踪地址，本项目忽略。搜索每次返回约 50 条，
`limit` 由后端截断；实测关键词 `微信`、`压缩` 均正常。

## 3. 实现与回退

- 实现：`Backend/soft360/search.py`（AES/RSA/签名/请求/解密）、`service.search()`（映射为
  `Soft360AppSummary` 列表）；`GET /api/v1/store/360/search?q=&limit=` 对外契约不变。
- 失败回退：网络错误、解密失败、`errno != 0` 时记录 warning，回退到对缓存目录的本地匹配
  （保证 360 源搜索至少覆盖热门子集，不会 500）。
- 安全：不把 `mid`/`m2`、签名材料、加密密钥写入日志；不缓存搜索结果中的广告链接；只返回
  软件条目（`type=1`）并做 URL/字段校验。
- 限制：这是未公开接口，360 改版可能失效；搜索到的目录外软件详情/下载由后端实时 getinfo
  支持，不要求软件在本地目录快照里。
