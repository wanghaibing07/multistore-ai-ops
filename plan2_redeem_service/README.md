# Plan 2 自助提货与履约服务

后端使用 Python 3.12、FastAPI、Uvicorn、SQLite WAL、httpx、Telethon 和 SMTP，接口契约见同目录的 `API_CONTRACT.md`。

## 本机开发

```powershell
cd D:\Codex-Projects\ai-commerce-sourcing-ledger\plan2_redeem_service
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
$env:PLAN2_EXECUTION_MODE = "dry_run"
$env:PLAN2_ADMIN_TOKEN = "dev-only-token"
uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
```

打开 `http://127.0.0.1:8000/`。前端构建产物存在于 `frontend/dist/` 时，FastAPI 会同源托管静态资源和 SPA 路由。

后台默认每 20 分钟（1200 秒）自动轮询一次 Canboso 共享车位订单。可通过 `PLAN2_POLL_INTERVAL_SECONDS` 调整，服务端会保留至少 10 秒的下限。买家端查询自己订单的 30 秒刷新节奏不受此配置影响。

## 单台云服务器启动

当前生产服务默认固定运行在云端主机（SSH 别名 `plan2-prod`，应用目录 `/opt/plan2_redeem_service`，服务单元 `plan2-redeem.service` + `caddy.service`）。日常排障、看日志、查线上状态与发布同步统一优先使用仓库根目录脚本：

```powershell
pwsh -File .\scripts\plan2-cloud.ps1 -Action status
pwsh -File .\scripts\plan2-cloud.ps1 -Action logs -Lines 80
pwsh -File .\scripts\plan2-cloud.ps1 -Action deploy
```

首次初始化云端主机时的手工启动步骤如下：

```bash
cd /opt/plan2_redeem_service
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
cp .env.example .env
# 在进程管理器中注入 .env 后执行
uvicorn backend.main:app --host 0.0.0.0 --port 8000 --workers 1
```

生产配置必须显式设置 `PLAN2_EXECUTION_MODE=live`，并配置 Canboso、16688、管理员 Token 和 SMTP 密钥。默认值为 `dry_run`。SQLite 数据库与私有截图目录应放在持久化磁盘，并限制私有目录只有服务进程可读。

## 配置项

`PLAN2_CONFIG_FILE` 可选，指向 YAML 配置文件。环境变量会覆盖 YAML 中的同名字段。`PLAN2_DB_PATH` 指向 SQLite 文件，`PLAN2_STORAGE_DIR` 保存不公开的保修截图，`PLAN2_PUBLIC_BASE_URL` 用于生成提货链接。`PLAN2_ADMIN_TOKEN` 保护所有 `/api/v1/admin/*` 接口。`PLAN2_CANBOSO_API_SECRET` 填 Canboso buyer key，`PLAN2_CANBOSO_BASE_URL` 默认使用 `https://canboso.com`。16688 的 `PLAN2_CHANNEL16688_API_KEY` 对应 Open API 的 `app_id`。SMTP 配置控制商家告警和履约通知。`PLAN2_POLL_INTERVAL_SECONDS` 控制后台上游轮询间隔，默认 1200 秒，代码下限为 10 秒。

### Telegram 保修自动报备配置（可选）

独享整组（`ultra_exclusive_admin`）支持免截图提交保修邮箱后，通过 Telethon 自动私聊向 Canboso 管理员报备；失败时自动邮件告警并可在管理后台重试。相关环境变量：

- `PLAN2_TG_WARRANTY_ENABLED`：`true` / `false`（默认 `false`）
- `PLAN2_TG_API_ID`、`PLAN2_TG_API_HASH`：Telegram 应用凭据（留空不影响服务启动）
- `PLAN2_TG_SESSION`：Telethon `StringSession`（严格保存在 `.env`，严禁写入仓库或日志）
- `PLAN2_TG_ADMIN_TARGET`：管理员目标（支持纯数字 `peerId` 或 `@username`）
- `PLAN2_TG_TIMEOUT_SECONDS`：发送超时秒数（默认 `20`）

获取 `StringSession` 与只读校验目标的辅助命令（不会发送测试消息）：

```powershell
python -m backend.tg_login
python -m backend.tg_login --verify-only
```

## 验证

```powershell
python -m pytest
```

测试使用 Mock 网关和 `dry_run`，不会发起真实扣款、真实 16688 收费或真实邮件发送。

