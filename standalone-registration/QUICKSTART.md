# 5 分钟快速开始

## 1. 配置

```bash
cd standalone-registration
cp .env.example .env
```

编辑 `.env`：

```env
REG_UI_PASSWORD=请改成强密码

GROKCLI2API_ADMIN_URL=http://中心实例IP:3000
GROKCLI2API_ADMIN_TOKEN=中心管理台的Token
GROKCLI2API_AUTO_PUSH=true

GROK2API_MAIL_PROVIDER=yyds
GROK2API_YYDS_KEY=你的YYDS密钥
```

## 2. 启动

在 **本目录** 执行（compose 的 build context 指向上级仓库根）：

```bash
docker compose up -d --build
docker compose logs -f registration
```

看到类似：

```
[standalone-registration] UI http://0.0.0.0:8080
[standalone-registration] api_token=...
[entrypoint] turnstile-solver ready
```

## 3. 使用页面

1. 打开 `http://服务器IP:8080`
2. 输入 `REG_UI_PASSWORD` 登录
3. 确认「推送目标」URL/Token，点 **测试连通**
4. 填写邮箱 Key（若 .env 已写可跳过），点 **开始注册**
5. 到中心 grokcli-2api 管理台账号列表，应出现 `source=standalone-registration` 的新账号

## 4. 停止 / 更新

```bash
docker compose down
# 更新代码后
docker compose up -d --build
```

数据在 volume `registration_data`（配置 + 本机账号缓存），不会因重建镜像丢失。
