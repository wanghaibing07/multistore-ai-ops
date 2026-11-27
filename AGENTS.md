# 仓库级工程与 Git 管理规范（ai-commerce-sourcing-ledger / multistore-ai-ops）

本仓库采用严格的 Git 工程管理，所有子模块统一纳入版本控制、自动化测试与防泄密门禁。

## 仓库结构

- 根目录（`multistore-ai-ops`）：店铺账本与 AI 受限查询工作台（FastAPI + SQLAlchemy + Alembic，依赖由 `pyproject.toml` 与 `uv.lock` 锁定）。
- `plan1_auto_restock/`：16688 自营卡密秒发 + Canboso 低水位自动补库守护进程（`pyproject.toml`、`plan1_auto_restock/`、`tests/`）。
- `plan2_redeem_service/`：自助提货/兑换码与售后履约服务（后端 `backend/` + `tests/` + `pyproject.toml`；前端 `frontend/` 为 React + Vite，构建产物 `frontend/dist/` 由后端同源托管并随源码同步版本化）。

## 云端生产环境与默认运维入口（强制默认）

- `plan2_redeem_service` 的固定生产服务运行在云端主机（SSH 别名 `plan2-prod`，由本机 `~/.ssh/config` 解析；严禁将真实 IP、私钥、`.env` 或管理员 Token 写入任何版本化文件）。
- 云端生产拓扑：
  - 应用目录：`/opt/plan2_redeem_service`（非 Git 仓库，属主 `plan2:plan2`）
  - 虚拟环境：`/opt/plan2_redeem_service/.venv/bin/python`
  - 生产数据库与私有目录：`/opt/plan2_redeem_service/data/redeem.sqlite3`、`/opt/plan2_redeem_service/data/private`
  - 环境文件：`/opt/plan2_redeem_service/.env`（排查时不得打印或导出密钥明文）
  - Systemd 服务：`plan2-redeem.service`（监听 `127.0.0.1:8000`，由 `caddy.service` 反向代理对外提供 HTTPS）
- **默认操作路由**：
  - 凡涉及 `plan2_redeem_service` 的线上报错（如 HTTP 500、批量制码、提货、16688、阿奇索 webhook/履约异常）、服务状态、日志、真实订单/兑换码数据库排查、重启或发布验证，**默认直接通过 `plan2-prod` 在云端排查和操作**，不要误查本地 `127.0.0.1` 或本地 `data/redeem.sqlite3`（仅在用户明确要求本地 `dry_run` 调试时使用本地实例）。
  - **本地与云端职责分工**：本地工作区负责改代码、跑 `pytest` / 前端 `typecheck` & `build`、Git 提交与推送；改动验证通过后，统一使用 `.\scripts\plan2-cloud.ps1` 在云端运维（`-Action status | health | logs | backup | deploy | rollback`，支持 `-KeepBackups`、`-DrainWaitSeconds`、`-BackupName`、`-Force`；`deploy`/`rollback` 会先做 `purchasing` 排空检查，回滚仅恢复代码与 `frontend/dist` 而不覆盖生产库）。日常库存补卡、阿奇索/16688 渠道排障与 16688 OpenAPI 降级待补验人工核验统一从管理后台「渠道自动化」（`/admin/automation`）与订单抽屉处理，详见 `docs/maintenance.md`。
  - 严禁在本地另起连接真实上游的 `live` 实例，避免与云端后台轮询和 webhook 冲突。

## 严格 Git 工程流（强制执行）

1. **先启用仓库内建钩子**：在新环境或重新克隆后立即执行：
   ```powershell
   git config core.hooksPath .githooks
   ```
   严禁使用 `--no-verify` 绕过 `.githooks/pre-commit`。
2. **每次改动的标准闭环**：
   - 修改代码/配置后，必须运行所有受影响模块的测试、类型检查与构建门禁。
   - 若修改了 `plan2_redeem_service/frontend/` 下源码、HTML 或 `public/` 资源，提交前必须重新执行 `npm run build`，将更新后的 `frontend/dist/` 一并提交，避免前后端产物脱节。
   - 提交前核对 `git status -sb` 与 `git diff --cached --stat`，确保工作区无遗漏未追踪源码、无临时脚本/截图、无敏感文件。
   - 使用 Conventional Commits 提交，且单次提交只包含单一清晰主题：
     `<type>(<scope>): <summary>`
     - `type`：`feat` | `fix` | `refactor` | `chore` | `docs` | `test` | `ci`
     - `scope`：`root` | `plan1` | `plan2` | `git` | `ci`（按实际受影响模块填写）
   - **提交后直接一并推送远端**：完成提交后，在同一轮直接执行 `git push origin <当前分支>`（默认 `origin master`），最后确认 `git status -sb` 干净且与远端同步。
3. **历史与分支安全**：
   - 严禁对共享分支（`master`/`main`）执行 `git push --force`、`git reset --hard` 等改写已推送历史的操作。
   - 生产环境前端与对外接口不得保留 demo / mock / 演练快捷入口或测试后门（脚本级安全护栏 `dry_run` 与单测 Mock 仅限受控测试与运维防误扣费使用）。

## 敏感数据与防泄密红线

- 以下内容**严禁**进入任何版本化文件（包括 `README.md`、示例配置、源码和测试用例）：
  - 真实 API Key / Secret（16688 `APP_ID`/`SECRET`、Canboso `tgb_*`、大模型密钥等）
  - SMTP 授权码、管理员 Token、私钥/证书（`*.pem`/`*.key`）
  - 真实个人邮箱、商户号、真实订单号/卡密/钱包余额明细
  - 运行期数据库与导出文件（`.env`、`*.local.yaml`、`*.db`、`*.sqlite*`、`data/`、`.local_data/`、`exports/`、`private/`、`outputs/`）
- 真实凭证仅存放在本地被 `.gitignore` 忽略的 `.env` 和 `config.local.yaml` 中；版本库只保留脱敏后的 `.env.example` 与 `config.example.yaml`。

## 本地验证门禁命令

- **根目录（`multistore-ai-ops`）**：
  ```powershell
  uv run pytest -q
  uv run alembic upgrade head
  uv run alembic check
  ```
- **方案 1（`plan1_auto_restock/`）**：
  ```powershell
  uv run --no-project --with-editable ./plan1_auto_restock --with pytest pytest plan1_auto_restock/tests -q
  ```
- **方案 2 后端（`plan2_redeem_service/`）**：
  ```powershell
  .\plan2_redeem_service\.venv\Scripts\python.exe -X utf8 -m pytest plan2_redeem_service\tests -q
  ```
- **方案 2 前端（`plan2_redeem_service/frontend/`）**：
  ```powershell
  npm --prefix plan2_redeem_service/frontend run typecheck
  npm --prefix plan2_redeem_service/frontend run build
  ```
