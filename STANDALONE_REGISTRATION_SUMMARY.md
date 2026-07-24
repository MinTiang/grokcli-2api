# 独立注册服务 - 完成总结

## 已完成

从 grokcli-2api 抽出 **完全独立的注册服务**，目录：`standalone-registration/`。

### 功能对照需求

| 需求 | 实现 |
|------|------|
| 协议自动注册 | 复用 `grok2api` + `grok-build-auth` + 本地 Turnstile |
| Docker 部署 | `Dockerfile` + `docker-compose.yml` |
| Web 页面 | `static/index.html`（登录 / 配置 / 进度 / 本地账号） |
| 可配置邮箱与各类信息 | 页面 + `data/config.json` 持久化 |
| 推送到指定 grokcli-2api | `POST /admin/api/accounts/import` 自动推送 |

### 目录

```
standalone-registration/
├── app/
│   ├── main.py            # FastAPI 入口
│   ├── config_store.py    # JSON 配置持久化
│   ├── push.py            # 远端推送
│   └── hooks.py           # import 成功 → 推送
├── static/index.html      # Web UI
├── Dockerfile
├── docker-compose.yml
├── entrypoint.sh
├── requirements.txt
├── .env.example
├── setup.sh
├── README.md
└── QUICKSTART.md
```

### 使用

```bash
cd standalone-registration
cp .env.example .env
# 编辑 REG_UI_PASSWORD / GROKCLI2API_* / 邮箱 Key
docker compose up -d --build
# 打开 http://host:8080
```

构建上下文为**仓库根目录**（compose 已配置 `context: ..`），以便打入协议栈源码。

### 数据持久化

- Volume `registration_data` → `/app/data`
- `config.json`：页面保存的配置
- `auth.json`：本机注册结果缓存（主库在远端）

### 说明

- 本服务 **不依赖** PostgreSQL；`GROK2API_STORE_BACKEND=file`
- 注册成功后 hook 拦截 `import_auth_payload`，先写本地文件再推远端
- 推送失败不影响注册本身，进度区可看到 `push=ok/fail`
- 主项目行为未改动，本目录可独立演进
