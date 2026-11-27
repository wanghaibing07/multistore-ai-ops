# 维护手册

> 返回 [仓库首页](../README.md) · 系统架构见 [架构与数据流](architecture.md)

本手册面向日常开发、本地验证、云端排障与发布操作。按模块查对应章节，避免跨环境误操作或破坏防泄密门禁。

## 维护原则

1. **先事实后计算**：账本先落来源事实，再做计算与人工核对；任何影响利润的人工结论必须保留时间、依据和关联订单。
2. **默认安全演练**：方案 1 与方案 2 本地默认均为 `dry_run`；严禁在本地另起连接真实上游的 `live` 实例，避免与云端后台轮询和 Webhook 冲突。
3. **严格门禁**：不得使用 `--no-verify` 绕过 `.githooks`；不得向共享分支执行强推或重写历史操作。
4. **凭证不入库**：真实 `.env`、`*.local.yaml`、SQLite 数据库（`*.db`/`*.sqlite*`）、私钥/证书、`data/`、`private/`、`exports/`、`evidence/`、`outputs/` 严禁进入 Git。

## 仓库目录地图

| 位置 | 职责 |
| --- | --- |
| `app/main.py`、`app/cli.py` | 根目录 FastAPI 路由、导入/删单入口与 CLI 初始化命令 |
| `app/models.py`、`app/db.py`、`app/auth.py`、`app/backup.py` | 18 张 SQLAlchemy 表、SQLite 引擎、Argon2/会话/CSRF 鉴权与数据库热备份 |
| `app/importer.py`、`app/qianniu_orders.py`、`app/cashflow.py` | 千牛订单/退款/货款解析、去重、批次预览与 `BEGIN IMMEDIATE` 串行提交 |
| `app/accounting.py`、`app/card_reconciliation.py`、`app/payment_basis_review.py`、`app/refund_review.py` | 利润核算、采购匹配确认、金额口径核对与历史退款完整性校验 |
| `app/agent.py`、`app/card_query_ai.py`、`app/qianniu_chats.py` | 受限 AI 问账、卡网查询辅助与千牛聊天知识库 |
| `app/static/`（`index.html`、`style.css`、`app.js`） | 根目录原生前端工作台（5 个视图） |
| `migrations/versions/`、`tests/` | 根目录 12 份 Alembic 迁移与 96 项业务/API/隔离测试 |
| `plan1_auto_restock/` | 方案 1 自动补库守护进程（`cli.py`、`engine.py`、`storage.py`、`pricing.py`、`exporter.py` 等 13 个模块 + 18 项测试） |
| `plan2_redeem_service/backend/` | 方案 2 后端（`app.py`、`service.py`、`db.py`、`agiso.py`、`gateways.py`、`telegram_forwarder.py` 等 + 98 项测试） |
| `plan2_redeem_service/frontend/` | 方案 2 前端 React 19 + TypeScript 5.8 + Vite 6.3（构建产物 `frontend/dist/` 随源码同步提交） |
| `scripts/start-desktop.ps1` | 根目录账本 Windows 本机桌面启动脚本 |
| `scripts/evaluate_agent.py` | 根目录受限 AI 30 场景离线评测脚本 |
| `scripts/plan2-cloud.ps1` | 方案 2 云端主机（`plan2-prod`）状态、健康检查、日志与发布脚本 |
| `.githooks/`、`.github/workflows/ci.yml` | 本地 pre-commit/commit-msg 钩子与 5 个并行 CI Job |

## Git 钩子与提交规范

首次克隆或切换工作区后先启用仓库内建钩子：

```powershell
git config core.hooksPath .githooks
```

- **`.githooks/pre-commit`** 会自动拦截：暂存区空白错误（`git diff --cached --check`）、未解决合并冲突标记、敏感路径（`.env`、`*.local.yaml`、`*.db`/`*.sqlite*`、`*.pem`/`*.key`、`data/`、`private/`、`exports/` 等）、疑似密钥正则匹配，以及修改了 `plan2_redeem_service/frontend/` 源码却未同步提交 `frontend/dist/` 的情况。
- **`.githooks/commit-msg`** 强制校验 Conventional Commits 格式：`<type>(<scope>): <summary>`，允许的 `type` 为 `feat`、`fix`、`refactor`、`chore`、`docs`、`test`、`ci`，`scope` 按受影响模块填 `root`、`plan1`、`plan2`、`git`、`ci`。

合规提交示例：

```text
fix(root): preserve refund review evidence on partial update
feat(plan2): add refund close auto-restore guard
docs(root): align maintenance guide with git hooks
```

## 模块 1：根目录账本维护

### 本机启动与停止

```powershell
uv sync --locked
uv run alembic upgrade head
uv run python -m app.cli init --username owner --store '示例店铺' --generate-password
uv run uvicorn app.main:app --host 127.0.0.1 --port 8018 --no-access-log
```

访问 `http://127.0.0.1:8018`，初始密码保存在已忽略的 `private/initial-login.txt`。Windows 桌面快捷启动可使用 `pwsh -File .\scripts\start-desktop.ps1`。停止本机监听进程前先核对端口归属：

```powershell
Get-NetTCPConnection -LocalPort 8018 -State Listen |
  Select-Object -ExpandProperty OwningProcess |
  ForEach-Object { Stop-Process -Id $_ -Force }
```

### 数据库迁移与备份

```powershell
uv run alembic current
uv run alembic history
uv run alembic upgrade head
uv run alembic check
```

变更表结构时遵循：`app/models.py` 改动 → 新增 Alembic migration（处理旧数据默认值、索引与可重复执行）→ 补充测试 → 运行 `alembic upgrade head` 与 `alembic check`。禁止直接手工改写生产 SQLite 结构；做数据修复前先使用 `app/backup.py` 备份数据库。

### 核对与核算规则

- **千牛订单导入**：对应“已卖出宝贝”导出文件，经预览、去重、差异提示和 `changes_token` 确认后入库；不要把未知数量、未知退款或未知成本硬写成零。
- **卡网采购匹配**：按真实外部订单号去重，分 `成品号`、`激活`、`家庭组` 三类；候选评分仅辅助排序，必须经人工确认才计入利润。
- **利润与退款口径**：未发货且全额退款订单成本归零且不入有效利润；已发货退款按净收入扣成本核算；未确认成本不默认按零入利润；订单核心字段变更会使旧人工核对失效。修改相关规则时重点回归 `tests/test_accounting.py`、`tests/test_refund_completeness.py`、`tests/test_cashflow_refund_review.py`、`tests/test_payment_basis_review.py`、`tests/test_card_reconciliation.py` 和 `tests/test_cost_invalidation.py`。

### 受限 AI 与原生前端检查

- 涉及 `app/agent.py` 时，需同时确认：意图固定、3 工具零参白名单与租户/日期约束生效、最终数字由 `verified_answer()` 渲染且模型不可用时账本照常工作。验证命令：
  ```powershell
  uv run pytest tests/test_api_agent.py -q
  uv run python -X utf8 scripts/evaluate_agent.py --output .\private\agent-evaluation.json
  ```
- 根目录前端（`app/static/index.html`、`style.css`、`app.js`）改动后先执行 `node --check app/static/app.js` 与 `git diff --check`，并在浏览器检查全部 **5 个视图**：工作台（`overview`）、导入订单（`import`）、已入库订单与行内成本核对（`orders`）、千牛聊天（`chats`）、问账（`assistant`）。静态资源带版本参数时同步递增版本号。

## 模块 2：方案 1 自动补库运维

日常操作详见 [`plan1_auto_restock/README.md`](../plan1_auto_restock/README.md)。关键维护约束：

- 每次调整 `config.local.yaml` 后先跑 `check` 和 `sync-once --dry-run`，确认补库计划与调价阈值后再启用 `--live --i-confirm-real-spend`。
- 若遇网络超时或 16688 上传中断，优先执行 `recover` 从 `secrets_vault.db` 补传或按原幂等键查单，避免重复下单。
- 16688 商品在后台手动下架（`status=0`）时，必须同步将 `config.local.yaml` 对应 SKU 改为 `enabled: false`。
- 需要将方案 1 采购记录导入根目录账本时，执行 `export-csv` 导出零明文对账文件。

## 模块 3：方案 2 本地开发与云端运维

### 本地与云端分工

- **本地工作区**：负责修改前后端代码、运行 `pytest`、执行前端 `typecheck` / `build`、提交并推送 Git。本地仅允许以 `PLAN2_EXECUTION_MODE=dry_run` 调试。
- **云端生产主机（`plan2-prod`）**：应用目录 `/opt/plan2_redeem_service`（非 Git 仓库，属主 `plan2:plan2`），运行 `plan2-redeem.service`（`127.0.0.1:8000`）与 `caddy.service`。排查线上接口报错、真实订单数据库、日志或发布重启一律通过 `scripts/plan2-cloud.ps1` 操作，不查本地数据库。

```powershell
# 检查服务与数据库状态
pwsh -File .\scripts\plan2-cloud.ps1 -Action status
# 检查公网 / 本地健康接口
pwsh -File .\scripts\plan2-cloud.ps1 -Action health
# 查看最近 80 行 systemd 日志
pwsh -File .\scripts\plan2-cloud.ps1 -Action logs -Lines 80
# 同步最新代码与前端 dist 到云端并重启验证
pwsh -File .\scripts\plan2-cloud.ps1 -Action deploy
```

### 前端构建产物同步门禁

只要修改了 `plan2_redeem_service/frontend/src/`、`public/` 或 `index.html`，提交前必须执行：

```powershell
npm --prefix plan2_redeem_service/frontend run typecheck
npm --prefix plan2_redeem_service/frontend run build
```

并将更新后的 `plan2_redeem_service/frontend/dist/` 一并暂存提交，否则本地 `.githooks/pre-commit` 与 CI `Plan 2 Redeem Frontend` 会直接拦截。

## 全量本地验证门禁

每次改动至少运行受影响模块门禁；跨模块或发版前运行全部门禁：

```powershell
# 1. 根目录账本 + 迁移一致性
uv run pytest -q
uv run alembic upgrade head
uv run alembic check

# 2. 方案 1 自动补库
uv run --no-project --with-editable ./plan1_auto_restock --with pytest pytest plan1_auto_restock/tests -q

# 3. 方案 2 后端
.\plan2_redeem_service\.venv\Scripts\python.exe -X utf8 -m pytest plan2_redeem_service\tests -q

# 4. 方案 2 前端
npm --prefix plan2_redeem_service/frontend run typecheck
npm --prefix plan2_redeem_service/frontend run build

# 5. 空白与暂存状态检查
git diff --check
git status -sb
```

## 常见问题排查

- **根目录页面打不开**：确认监听 `127.0.0.1:8018` 且已执行 `alembic upgrade head`；修改静态资源后硬刷新浏览器清除缓存。
- **千牛导入提示缺少字段**：确认导出入口为“已卖出宝贝”，检查 `item_id`、`quantity`、`refund_status`、订单号与付款时间表头映射。
- **利润与预期不一致**：依次核对付款日期范围、退款状态、实付/净额口径、采购成本确认状态与待核对清单，先定位来源层和计算层差异。
- **Windows 桌面脚本闪退**：在 PowerShell 中直接运行 `.\scripts\start-desktop.ps1` 查看具体异常，常见原因为 `uv` 环境未同步、端口冲突或初始密码文件缺失。
- **方案 2 线上报备或回调异常**：先用 `plan2-cloud.ps1 -Action logs -Lines 120` 查脱敏日志；Telegram Web K 失败时核对 `PLAYWRIGHT_BROWSERS_PATH` 是否在 `/home`/`/root` 之外且 `plan2` 用户可读，并确认管理员后台是否需要点击重试报备。

## 变更与发布检查清单

- [ ] 已启用 `.githooks`（`git config --get core.hooksPath` 输出 `.githooks`）
- [ ] 明确变更影响的数据表、业务规则和页面/CLI 入口
- [ ] 涉及模型改动已补 Alembic 迁移并通过 `alembic upgrade head && alembic check`
- [ ] 修改方案 2 前端源码时已重新构建并暂存 `frontend/dist/`
- [ ] 所有受影响模块的 `pytest` / `typecheck` / `build` 全部通过
- [ ] 核对 `git status -sb` 与 `git diff --cached --stat`，确认无临时文件、真实数据或敏感配置
- [ ] 使用 Conventional Commits 提交并推送 `origin master`；涉及方案 2 线上改动再执行 `plan2-cloud.ps1 -Action deploy`
