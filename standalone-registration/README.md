# 独立注册服务 (standalone-registration)

从 **grokcli-2api** 抽出的协议注册能力：临时邮箱 + 过盾 + xAI OAuth Device Flow → 本地缓存 → **自动推送到指定 grokcli-2api**。

适合：多台 VPS 只跑注册，把账号汇总到一台中心 grokcli-2api。

## 特性

- **Web 页面**：配置邮箱 / 过盾 / 代理 / 推送目标，一键批量注册，实时进度
- **配置持久化**：`data/config.json`（Docker volume），密钥可在页面填写；空值不覆盖已有密钥
- **自动推送**：`POST {GROKCLI2API_ADMIN_URL}/admin/api/accounts/import`
- **本地过盾**：同容器内联 Turnstile Solver（Camoufox）；也可切换 YesCaptcha
- **邮箱**：YYDS / MoeMail / GPTMail / Cloudflare Temp Email
- **无 PostgreSQL 依赖**：本服务用 file store；账号主库在远端 grokcli-2api

## 快速开始（Docker）

### 1. 准备环境变量

```bash
cd standalone-registration
cp .env.example .env
# 编辑 .env：至少改 REG_UI_PASSWORD、推送 URL/Token、邮箱 Key
```

必填示例：

```env
REG_UI_PASSWORD=your-strong-password

GROKCLI2API_ADMIN_URL=http://your-grokcli2api:3000
GROKCLI2API_ADMIN_TOKEN=sk-xxx          # 中心实例管理台创建的 Admin / API Token
GROKCLI2API_AUTO_PUSH=true

GROK2API_MAIL_PROVIDER=yyds
GROK2API_YYDS_KEY=your-yyds-key

# 本地过盾（默认）
GROK2API_CAPTCHA_PROVIDER=local
```

> 邮箱 Key、YesCaptcha、推送 Token 也可启动后在 **Web 页面** 填写并保存。

### 2. 构建并启动

**构建上下文必须是 grokcli-2api 仓库根目录**（Dockerfile 会复制 `grok2api` / `grok-build-auth` / `turnstile-solver` / `scripts`）。

```bash
# 在 standalone-registration 目录
docker compose up -d --build

# 或在仓库根目录
docker compose -f standalone-registration/docker-compose.yml up -d --build
```

### 3. 打开页面

浏览器访问：`http://<host>:8080`  
用 `REG_UI_PASSWORD` 登录 → 配置推送目标 → 开始注册。

日志中会打印一次 `api_token`，可用于脚本调用（`Authorization: Bearer <api_token>`）。

## 架构

```
┌──────────────────────────────────────────────┐
│  standalone-registration 容器                 │
│  ┌────────────┐  ┌─────────────────────────┐ │
│  │ Web UI:8080│→ │ registration API        │ │
│  └────────────┘  │ grok_build_adapter      │ │
│                  │ + hooks (push remote)   │ │
│  ┌────────────┐  └───────────┬─────────────┘ │
│  │turnstile   │              │ 成功后        │
│  │:5072 local │              ▼               │
│  └────────────┘     data/auth.json 缓存      │
└──────────────────────────────┬───────────────┘
                               │ POST /admin/api/accounts/import
                               ▼
                    ┌─────────────────────┐
                    │ 中心 grokcli-2api    │
                    │ 账号池 (PostgreSQL)  │
                    └─────────────────────┘
```

## 目录结构

```
standalone-registration/
├── app/
│   ├── main.py           # FastAPI：页面 API + 注册编排
│   ├── config_store.py   # data/config.json 持久化
│   ├── push.py           # 推送到 grokcli-2api
│   └── hooks.py          # 注册成功后拦截 import → 远程推送
├── static/index.html     # Web UI
├── Dockerfile
├── docker-compose.yml
├── entrypoint.sh
├── requirements.txt
├── .env.example
└── README.md
```

镜像构建时从父仓库打入：

- `grok2api/`（适配器、邮箱、SSO 转换）
- `grok-build-auth/`（协议注册客户端）
- `turnstile-solver/`（本地过盾）
- `scripts/`（`sso_to_auth_json.py` 等）

## API 摘要

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | Web UI |
| GET | `/health` | 健康检查 |
| POST | `/api/login` | 登录（cookie） |
| GET/PUT | `/api/config` | 读写配置（密钥脱敏） |
| POST | `/api/push/test` | 测试远端连通 |
| POST | `/api/register` | 开始注册 |
| GET | `/api/sessions` `/api/sessions/{id}` | 会话 |
| GET | `/api/batches/{id}` | 批次 |
| POST | `/api/stop` | 停止全部 |
| GET | `/api/accounts` | 本地缓存账号列表 |

认证：登录 cookie，或 `Authorization: Bearer <api_token|ui_password>`。

### 脚本示例

```bash
TOKEN=你的api_token   # 容器日志里有
curl -s -X POST http://127.0.0.1:8080/api/register \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: batch-$(date +%s)" \
  -d '{"count":5,"concurrency":3,"mail_provider":"yyds"}'
```

## 推送接口

远端需开启管理写入。本服务发送：

```http
POST /admin/api/accounts/import
Authorization: Bearer <GROKCLI2API_ADMIN_TOKEN>
Content-Type: application/json

{
  "merge": true,
  "payload": { "key": "...", "email": "...", "refresh_token": "...", "sso": "...", "source": "standalone-registration" }
}
```

推送失败**不会**回滚注册；进度里会显示 `push=fail` 及原因。可在页面「测试连通」排查。

## 多 VPS 部署

每台机器各自：

1. 部署本服务
2. `.env` 里 `GROKCLI2API_ADMIN_URL` / `TOKEN` 指向同一中心实例
3. `source` 可改成 `reg-tokyo` / `reg-sg` 便于区分来源

## 持久化

| 路径 | 内容 |
|------|------|
| `/app/data/config.json` | 页面保存的邮箱/推送/代理等 |
| `/app/data/auth.json` | 本机注册结果缓存（非主库） |
| `/app/data/register_sso/` | SSO 备份（适配器写出） |

Compose 默认 volume：`registration_data` → `/app/data`。

## 常见问题

**1. 构建失败 / 找不到 grok2api**  
确认 `docker compose` 的 `build.context` 为仓库根（compose 文件已设 `context: ..`）。

**2. 本地过盾一直未就绪**  
看 `docker compose logs` 与 volume `registration_solver_logs`。镜像需 `camoufox fetch` 成功；低内存机器可改 `GROK2API_CAPTCHA_PROVIDER=yescaptcha`。

**3. 推送 401**  
中心实例 Token 无效或没有 admin 写权限；页面「测试连通」看 HTTP 状态。

**4. 注册成功但远端没有账号**  
进度里看 `push=`；本机「本地账号缓存」有数据说明注册 OK、推送失败。修好 Token 后可从中心用 SSO/JSON 再导，或重跑注册。

## 与主项目关系

- **不修改** grokcli-2api 主进程行为；本目录独立部署。
- 注册协议与主项目共用 vendored 代码，主项目升级后请重新 build 本镜像。
- 主项目内嵌的「协议注册」UI 仍可用；本服务是**外置、可水平扩展**的版本。

## 开发（非 Docker）

需已克隆完整 grokcli-2api，并安装 Python 依赖：

```bash
cd standalone-registration
python -m venv .venv && source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r ../requirements.txt
export STANDALONE_VENDOR_ROOT=..
export GROK2API_DATA_DIR=./data
export GROK2API_STORE_BACKEND=file
export GROK2API_REG_SKIP_PROBE=1
export REG_UI_PASSWORD=dev
# 本地过盾另开 turnstile-solver 或设 yescaptcha
python -m app.main
```

## License

与父项目 grokcli-2api 相同。
