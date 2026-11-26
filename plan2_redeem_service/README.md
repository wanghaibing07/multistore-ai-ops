# Plan 2 自助提货与履约服务

> 返回 [仓库首页](../README.md) · 架构说明见 [架构与数据流](../docs/architecture.md) · 接口定义见 [API_CONTRACT.md](API_CONTRACT.md)

后端使用 Python 3.12、FastAPI、Uvicorn、SQLite WAL、httpx、Telethon（可选 Playwright Web K）和 SMTP；前端使用 React 19、TypeScript 5.8、Vite 6.3 与 React Router 7.6，构建产物 `frontend/dist/` 由 FastAPI 同源托管。

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

### 阿奇索（淘宝）与 16688 渠道联动配置（可选）

支持淘宝阿奇索 Webhook 自动发码、退款自动作废、退款关闭自动恢复，以及阿奇索卡券库和 16688 卡密库存的低水位定时补码。完整默认值见同目录 [`.env.example`](.env.example)：

- **阿奇索鉴权与防重放**：`PLAN2_AGISO_APP_ID`、`PLAN2_AGISO_APP_SECRET`、`PLAN2_AGISO_ACCESS_TOKEN`、`PLAN2_AGISO_API_BASE_URL`、`PLAN2_AGISO_TIMEOUT_SECONDS`、`PLAN2_AGISO_TIMESTAMP_TOLERANCE_SECONDS`（默认 600 秒重放窗口）。
- **自动发码与退款联动**：`PLAN2_AGISO_AUTO_CREATE_RESULT`、`PLAN2_AGISO_AUTO_DUMMY_SEND`、`PLAN2_AGISO_AUTO_SEND_TB_CARD`、`PLAN2_AGISO_AUTO_SEND_WW_MSG`、`PLAN2_AGISO_AUTO_REVOKE_ON_REFUND`、`PLAN2_AGISO_AUTO_RESTORE_ON_REFUND_CLOSE`。
- **订单补漏轮询与 SKU 规则**：`PLAN2_AGISO_POLL_ENABLED`、`PLAN2_AGISO_POLL_INTERVAL_SECONDS`、`PLAN2_AGISO_SKU_RULES_JSON`（支持后台数据库规则优先、环境变量规则兜底）。
- **双渠道低水位自动补码**：阿奇索侧由 `PLAN2_AGISO_RESTOCK_ENABLED` / `PLAN2_AGISO_RESTOCK_INTERVAL_SECONDS` / `PLAN2_AGISO_RESTOCK_RULES_JSON` 控制；16688 侧由 `PLAN2_CHANNEL16688_RESTOCK_ENABLED` / `PLAN2_CHANNEL16688_RESTOCK_INTERVAL_SECONDS` / `PLAN2_CHANNEL16688_RESTOCK_RULES_JSON` 控制。

### Telegram 保修自动报备配置（可选）

独享整组（`ultra_exclusive_admin`）支持免截图提交保修邮箱后，通过 Telegram 自动私聊向 Canboso 管理员报备（支持 `telethon` MTProto 与 `web` Playwright Telegram Web K 持久化会话两种通道）；失败时自动邮件告警并可在管理后台重试。相关环境变量：

- `PLAN2_TG_WARRANTY_ENABLED`：`true` / `false`（默认 `false`）
- `PLAN2_TG_TRANSPORT`：`auto` / `telethon` / `web`（默认 `auto`；填写 Telethon 三件套时优先 `telethon`，否则若配置了 `PLAN2_TG_WEB_PROFILE_DIR` 自动使用 `web`）
- `PLAN2_TG_API_ID`、`PLAN2_TG_API_HASH`：Telegram 应用凭据（`telethon` 模式使用，留空不影响服务启动）
- `PLAN2_TG_SESSION`：Telethon `StringSession`（严格保存在 `.env`，严禁写入仓库或日志）
- `PLAN2_TG_WEB_PROFILE_DIR`：`web` 模式使用的 Chromium 持久化 Profile 目录（如 `./data/tg_web_profile`，权限应为 `700`）
- `PLAN2_TG_WEB_URL`：`web` 模式入口（默认 `https://web.telegram.org/k/`）
- `PLAN2_TG_ADMIN_TARGET`：管理员目标（`telethon` 模式支持纯数字 `peerId` 或 `@username`；`web` 模式强制要求纯数字 `peerId` 以防错发）
- `PLAN2_TG_TIMEOUT_SECONDS`：发送超时秒数（默认 `20`，`web` 模式内部下限 `30`、建议设为 `45`）
- `PLAYWRIGHT_BROWSERS_PATH`：`web` 模式 Chromium 浏览器目录；systemd 启用 `ProtectHome=true` 时须放在 `/home`/`/root` 之外且运行用户可读的目录（如 `/opt/plan2_redeem_service/pw-browsers`），安装命令为 `pip install -e ".[tg-web]"` 与 `PLAYWRIGHT_BROWSERS_PATH=/opt/plan2_redeem_service/pw-browsers playwright install chromium`

获取 `StringSession` 与只读校验目标的辅助命令（不会发送测试消息）：

```powershell
python -m backend.tg_login
python -m backend.tg_login --verify-only
```

## 前端开发与构建同步门禁

修改 `plan2_redeem_service/frontend/src/`、`public/` 或 `index.html` 后，提交前必须在仓库根目录运行类型检查和生产构建，并将生成的 `plan2_redeem_service/frontend/dist/` 一并提交（`.githooks/pre-commit` 与 CI 会强校验前后端产物一致）：

```powershell
npm --prefix plan2_redeem_service/frontend run typecheck
npm --prefix plan2_redeem_service/frontend run build
```

## 后端测试验证

```powershell
.\plan2_redeem_service\.venv\Scripts\python.exe -X utf8 -m pytest plan2_redeem_service\tests -q
```

77 项后端测试全部使用 Mock 网关与 `dry_run`，不会发起真实扣款、真实 16688 收费、真实 Telegram 发送或真实邮件发送。

