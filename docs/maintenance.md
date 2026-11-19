# 维护手册

这份文档面向继续开发、排障和发布 MultiStore AI Ops 的维护者。先看本页，再进入具体模块，能减少直接改动数据或核算规则带来的风险。

## 维护原则

1. 先保存来源事实，再生成计算结果。
2. 任何影响利润的人工结论都要带有来源、时间和关联订单。
3. AI 只能识别意图、选择白名单查询工具和整理已验证结果。
4. 修改导入、退款、成本或删除逻辑后，必须运行完整测试。
5. 真实订单、采购文件、账号、Cookie、模型密钥和本地数据库不进入 Git。

## 目录地图

| 位置 | 责任 |
| --- | --- |
| `app/main.py` | FastAPI 路由、会话、导入入口、订单删除入口 |
| `app/models.py` | SQLAlchemy 模型和关系 |
| `app/importer.py` | CSV、XLSX、复制文本解析、字段归一化和批次预览 |
| `app/accounting.py` | 订单、退款、成本、利润和待核对状态 |
| `app/card_reconciliation.py` | 卡网采购去重、类目、候选匹配和确认 |
| `app/payment_basis_review.py` | 退款前实付与退款后净额的口径核对 |
| `app/refund_review.py` | 历史退款补录和完整性核对 |
| `app/agent.py` | AI 意图识别、工具白名单和结果约束 |
| `app/qianniu_chats.py` | 千牛聊天导入、脱敏和会话分析 |
| `app/static/index.html` | 页面结构和可见文案 |
| `app/static/style.css` | 视觉系统、响应式布局和交互状态 |
| `app/static/app.js` | 页面状态、API 调用和交互行为 |
| `migrations/versions/` | Alembic 数据库迁移 |
| `tests/` | 业务规则、接口和回归测试 |
| `scripts/start-desktop.ps1` | Windows 桌面启动入口 |

## 本机启动

环境要求是 PowerShell 7、Python 3.12 和 uv。

```powershell
uv sync --locked
uv run alembic upgrade head
uv run python -m app.cli init --username owner --store '我的店铺' --generate-password
uv run uvicorn app.main:app --host 127.0.0.1 --port 8018 --no-access-log
```

打开 `http://127.0.0.1:8018`。初始化密码写入被 Git 忽略的 `private/initial-login.txt`。

Windows 桌面启动使用：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-desktop.ps1
```

停止本机服务：

```powershell
Get-NetTCPConnection -LocalPort 8018 -State Listen |
  Select-Object -ExpandProperty OwningProcess |
  ForEach-Object { Stop-Process -Id $_ -Force }
```

如果端口已被占用，先确认占用进程确实属于本项目，再停止进程。不要用不加端口限定的批量终止命令。

## 数据库与迁移

查看当前迁移状态：

```powershell
uv run alembic current
uv run alembic history
uv run alembic check
```

新增字段或表时遵循以下顺序：

1. 修改 `app/models.py`。
2. 创建一份新的 Alembic migration。
3. 在迁移中处理旧数据的默认值、索引和可重复执行性。
4. 为新增规则补测试。
5. 执行 `uv run alembic upgrade head` 和完整测试。

不要直接删除线上字段，也不要通过手工修改 SQLite 文件替代迁移。需要修复数据时，优先写可审计、可重复执行的脚本，并先备份数据库。

## 订单与采购核对规则

### 订单导入

千牛订单入口对应“已卖出宝贝”的导出文件。导入流程是预览、识别、去重、确认和入库。订单号、子订单号、商品编号、数量、退款状态、付款时间和履约状态是核算链路中的关键字段。

导入异常先看导入结果里的缺失字段，再检查原始表头映射。不要为了让导入通过而把未知数量、未知退款或未知成本改成零。

### 卡网采购

卡网采购按真实外部订单号去重。当前类目是：

- 成品号
- 激活
- 家庭组

采购匹配只提供候选排序。标题、规格、数量、金额和时间用于计算候选分数，人工确认后才会进入利润核算。重复导入应保持幂等，删除卡网订单时要同步更新采购关联和相关核对状态。

### 利润与退款

- 未发货且全额退款的订单，实际成本按零处理，利润不进入有效利润统计。
- 已发货但发生退款的订单，利润按收入扣除退款和实际成本后的结果计算。
- 未确认成本的订单不能通过默认零成本进入有效利润。
- 退款历史与退款快照分开保存。来源不完整时进入待核对状态。
- 付款前实付和退款后净额是两种不同金额口径。来源发生变化时，必须重新核对口径。

修改这些规则时，优先查看：

- `tests/test_accounting.py`
- `tests/test_refund_completeness.py`
- `tests/test_cashflow_refund_review.py`
- `tests/test_payment_basis_review.py`
- `tests/test_card_reconciliation.py`
- `tests/test_cost_invalidation.py`

## AI 维护边界

AI 相关变更必须同时检查三层：

1. 意图解析是否仍限制在固定业务意图。
2. 工具调用是否仍经过白名单、店铺范围和时间范围校验。
3. 最终数字是否来自服务端计算结果，而不是模型自由生成。

模型不可用时，订单导入、采购核对、退款核对和利润计算必须继续可用。涉及 AI 的修改至少运行：

```powershell
uv run pytest tests/test_api_agent.py -q
uv run python -X utf8 scripts/evaluate_agent.py --output .\private\agent-evaluation.json
```

评测结果只能写入 `private/` 或其他已忽略目录。不要把真实聊天、邮箱、订单号、卡密或模型响应提交到仓库。

## 前端维护约定

- 页面结构改动优先编辑 `app/static/index.html`。
- 交互和 API 状态改动编辑 `app/static/app.js`。
- 颜色、间距、布局和响应式规则编辑 `app/static/style.css`。
- 每个操作页面只保留完成任务所需的标签、状态、结果和错误。
- 动态数据使用 `aria-live` 时，只播报状态变化，不重复播报整页内容。
- 表格保留语义化表头、键盘滚动容器和可见焦点状态。
- 前端静态资源有版本查询参数时，结构、样式或脚本更新后同步递增版本。

前端改动后的最小检查：

```powershell
node --check app/static/app.js
git diff --check
```

然后打开工作台、导入订单、成本核对、已入库订单、千牛聊天和问账六个页面，至少确认导航、空状态、错误状态和主要按钮正常。

## 测试与提交

完整验证：

```powershell
uv run pytest -q
uv run alembic check
node --check app/static/app.js
git diff --check
```

提交前检查：

```powershell
git status --short
git diff --stat
git diff --name-only
```

提交信息使用简短的动词开头，例如：

```text
fix: preserve refund review evidence
feat: add card purchase category filter
refine: simplify reconciliation interface
```

不要提交 `.env`、`data/`、`private/`、`evidence/`、`outputs/`、数据库文件、原始订单文件或真实截图。

## 发布与回滚

推送前先确认工作树干净、测试通过、迁移可执行。发布后检查首页、导入预览、订单筛选、采购删除和利润结果。

发现回归时，先保留日志和复现输入，再回退到上一个已验证提交。涉及数据库迁移时，先确认是否已经执行过迁移，再决定是否需要补偿迁移。不要直接强制重写远端分支历史。

## 常见问题

### 页面打不开

确认服务监听 `127.0.0.1:8018`，查看启动终端的异常，并确认迁移已执行。修改静态资源后先硬刷新浏览器，避免旧缓存遮蔽结果。

### 导入提示缺少字段

先确认导出页面是千牛“已卖出宝贝”，再检查实际表头和字段映射。重点查看 `item_id`、`quantity`、`refund_status` 以及订单号、付款时间等字段是否被识别。

### 利润和预期不一致

依次检查付款日期范围、退款状态、付款金额口径、采购成本确认状态和是否存在待核对记录。不要先改结果，先定位来源和计算层的差异。

### 桌面启动后立即关闭

从 PowerShell 直接运行 `scripts/start-desktop.ps1` 查看错误。常见原因是 Python 环境、uv 同步、端口占用或初始化文件缺失。

## 变更前检查清单

- [ ] 明确变更影响的来源字段、业务规则和页面入口
- [ ] 检查是否需要数据库迁移
- [ ] 增加或更新对应测试
- [ ] 验证导入幂等、删除一致性和租户范围
- [ ] 手动检查相关页面和空状态
- [ ] 确认没有真实数据进入 Git
- [ ] 运行完整测试并记录结果
- [ ] 提交后再推送，不直接修改远端历史
