# 部署 WinAppsManager API

本文档说明如何把 `Backend/` 部署成可被 WPF 客户端访问的 HTTPS API 服务。
仓库当前为**私有**，这里提供的是部署准备；真正公网部署前请先完成“安全清单”。

> 注意：本项目采用非商业源码许可（见 [LICENSE](../LICENSE)），公网部署也要遵守：
> 禁止商业用途、倒卖、销售或付费分发。联想商店的名称、数据与图标归联想所有，
> 本项目与联想无隶属或背书关系。

## 0. 前置条件

- 一台可运行 Python 3.11+ 的 Linux 服务器（推荐 Ubuntu 24.04 + Python 3.12；`httpx2 2.16` 要求 3.11+）。
- 一个域名（例如 `api.example.com`）及可用的 80/443 端口。
- 服务器能访问 GitHub API、联想 PC 商店与联想图片/视频 CDN。
- 客户端通过设置页“服务端地址”填写 `https://api.example.com/`，因此必须配置 TLS；
  不要把无鉴权的 HTTP 服务直接暴露在公网。

## 1. 方式 A：Docker Compose（推荐）

```bash
git clone https://github.com/daijunhaoMinecraft/WinAppsManager.git
cd WinAppsManager
# 可选：按需修改 Backend/config.json（日志级别、缓存目录、联想参数等）
docker compose up -d --build
curl http://127.0.0.1:8080/api/v1/health
```

- 容器只把 `8080` 绑定在 `127.0.0.1`，由 Caddy/Nginx 反向代理并终止 HTTPS。
- 缓存目录挂载到命名卷 `winappsmanager-cache`（联想目录快照、图标、token、日志）。
- 首次启动会预加载 `catalog.json`；没有快照时第一次目录请求会同步抓取一次联想目录
  （约 5111 款，1–3 秒），之后由后台线程每 5 分钟刷新。
- 常用命令：`docker compose logs -f api`、`docker compose restart api`、`docker compose down`。

## 2. 方式 B：systemd + Caddy（无 Docker）

```bash
sudo mkdir -p /opt/winappsmanager
sudo git clone https://github.com/daijunhaoMinecraft/WinAppsManager.git /opt/winappsmanager
cd /opt/winappsmanager/Backend
sudo python3.12 -m venv .venv
sudo .venv/bin/python -m pip install -r requirements.txt
sudo mkdir -p .cache
sudo chown -R www-data:www-data /opt/winappsmanager/Backend/.cache
```

复制并启用 systemd 单元（仓库内模板见 [`deploy/winappsmanager-api.service`](../deploy/winappsmanager-api.service)）：

```bash
sudo cp /opt/winappsmanager/deploy/winappsmanager-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now winappsmanager-api
sudo systemctl status winappsmanager-api --no-pager
```

Caddy 反向代理示例见 [`deploy/Caddyfile.example`](../deploy/Caddyfile.example)：

```bash
sudo cp /opt/winappsmanager/deploy/Caddyfile.example /etc/caddy/Caddyfile
# 编辑域名、保存后：
sudo systemctl reload caddy
```

没有 Caddy 时也可用 Nginx + certbot；关键是：

1. 终止 TLS，转发到 `127.0.0.1:8080`；
2. 屏蔽 `POST /api/v1/refresh`、`POST /api/v1/store/lenovo/refresh`、`/api/v1/cache*`；
3. 按需限制 `/docs` 公网访问；
4. 设置合理的请求体大小与超时（视频代理是流式转发，读取超时不要太短）。

## 2.5 方式 C：局域网 systemd 直连 8020

如果服务器只在内网使用，不需要域名和 HTTPS 反向代理，可以直接让 systemd 监听 `0.0.0.0:8020`。仓库模板见 [`deploy/winappsmanager-lan.service`](../deploy/winappsmanager-lan.service)。

```bash
# 1. 准备代码和 Python 3.12 虚拟环境（httpx2 2.16 要求 Python >= 3.11）
cd /home/daijunhao/ServerProject/Python/WinAppsManagerBackend
/usr/bin/python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. 安装并启用 systemd 单元（模板位于仓库根目录 deploy/，以下用占位路径）
sudo cp /path/to/WinAppsManager/deploy/winappsmanager-lan.service /etc/systemd/system/winappsmanager-backend.service
sudo systemctl daemon-reload
sudo systemctl enable --now winappsmanager-backend.service

# 3. 检查
systemctl status winappsmanager-backend.service --no-pager
curl http://127.0.0.1:8020/api/v1/health
```

该单元使用 `WINAPPSMANAGER_HOST=0.0.0.0`、`WINAPPSMANAGER_PORT=8020`，日志同时写入 journal 和 `Backend/.cache/logs/backend.log`：

```bash
journalctl -u winappsmanager-backend.service -f
tail -f .cache/logs/backend.log
```

首次启动如果还没有目录快照，可主动刷新一次目录（否则联想/360 会等待各自的定时刷新间隔）：

```bash
curl -X POST http://127.0.0.1:8020/api/v1/sources/lenovo/refresh
curl -X POST http://127.0.0.1:8020/api/v1/sources/soft360/refresh
curl http://127.0.0.1:8020/api/v1/sources
```

**安全提醒**：后端没有用户认证，`0.0.0.0:8020` 只能用于可信内网；不要直接把该端口映射到公网。需要公网访问时请使用第 1/2 节的 HTTPS 反向代理方案，并只在代理层暴露必要路径。
## 3. 客户端接入

1. 客户端 → 设置 → 服务端地址：填写 `https://api.example.com/`，点击“应用”/“测试连接”。
2. 软件源会从 `GET /api/v1/sources` 动态读取；联想源不可用时客户端会自动回退到可用源。
3. 已经开始的下载任务不受服务端地址切换影响；切换后重新打开“软件列表”即可。

## 4. 安全清单（公网部署前逐项确认）

- [ ] 只用 HTTPS 对外，HTTP 自动跳转；不要暴露 `127.0.0.1:8080` 原始端口。
- [ ] 在反向代理或防火墙层屏蔽 `POST /api/v1/refresh`、`POST /api/v1/store/lenovo/refresh`、
      `/api/v1/cache*`（客户端不需要这些接口；`/cache` 会暴露本机路径等诊断信息）。
- [ ] 如无必要，限制 `/docs` 的访问来源，或只在内网开启。
- [ ] 后端本身**没有用户认证**；不要把它当作多租户服务，也不要直接放在公网裸奔。
- [ ] 建议在 Caddy/Nginx 或云防火墙加 IP 限速（目录/搜索/视频代理都是公开读接口）。
- [ ] `Backend/config.local.json`、`Backend/.cache/`（含联想 token 与日志）绝不能提交到仓库。
- [ ] 日志中不包含签名下载地址/视频地址、GitHub 令牌或代理认证；对外反馈前先检查日志内容。
- [ ] 定期备份 `Backend/.cache/`（可加速冷启动）并留意磁盘占用（联想图片缓存上限 256 MB）。
- [ ] 监控 `/api/v1/health` 与磁盘、内存；视频代理会占用服务器出网带宽。
- [ ] 部署完成后重新阅读 [LICENSE](../LICENSE)，确认用途符合非商业许可。

## 5. 转为公开仓库（部署完成后）

仓库目前是私有的。完成公网部署与安全审查后，由仓库所有者执行：

```bash
gh repo edit daijunhaoMinecraft/WinAppsManager \
  --visibility public --accept-visibility-change-consequences
```

转公开前再检查一次：

1. `git status` 干净，提交历史里没有密钥/令牌/签名 URL（`gitleaks` 或 `git grep` 扫描）。
2. `Backend/config.local.json`、`.cache/`、用户 `%LOCALAPPDATA%` 数据没有进入仓库。
3. README 的截图、部署说明、许可证与实际状态一致。
4. 决定是否公开 `/docs`、是否继续保留私有 `AGENTS.md`（当前已随仓库提供）。

## 6. 常见问题

**Q：为什么客户端不直连联想 CDN？**
图标由客户端直连联想图片 CDN（带浏览器 UA）；目录、详情、截图、视频全部经后端代理，
目录只读本地缓存，视频/图片代理不缓存签名地址，避免密钥/时效 URL 泄漏。

**Q：视频播放需要服务器做什么？**
需要能访问 `pc-store-video.lenovomm.cn`；后端按 Range 流式转发，支持拖动进度，
不落盘。服务器出网带宽与并发需要留余量。

**Q：如何修改刷新间隔？**
`Backend/config.json` 的 `lenovo.catalog_refresh_interval_seconds`（默认 300 秒），
或环境变量 `WINAPPSMANAGER_LENOVO_CATALOG_REFRESH_INTERVAL_SECONDS`，修改后重启。

**Q：后端必须装 Windows 吗？**
不需要，FastAPI 后端可运行在 Linux；WPF 客户端才需要 Windows（Win7 SP1+ / Win10 2004+）。
