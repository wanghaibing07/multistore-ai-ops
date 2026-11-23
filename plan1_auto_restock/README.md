# 方案 1：16688 自营卡密秒发 + Canboso 低水位自动补库

本模块专门承接 **Canboso `productType=account` 成品号** 自动补库到 16688 自营卡密商品（`source=local`、`delivery_method=1`）。家庭组车位（`slot`）和代充（`upgrade_account`）会被代码硬拦截，留给方案 2（自助提货/兑换码链路）。

## 快速开始

1. 复制配置与环境变量模板（这两个本地文件已在 `.gitignore` 中排除，不会进版本库）：
   - `.env.example` → `.env`，填入 `PLATFORM_16688_APP_ID`、`PLATFORM_16688_SECRET`、`CANBOSO_KEY_PRIMARY`（及可选 `CANBOSO_KEY_SECONDARY`、`ALERT_WEBHOOK_URL`）。
   - `config.example.yaml` → `config.local.yaml`。
2. 查 Canboso 现货成品号列表（只读）：
   ```powershell
   python -X utf8 -m plan1_auto_restock -c config.local.yaml catalog --in-stock
   ```
3. 在 16688 建好自营卡密商品（或用命令先预览、加 `--execute` 创建）：
   ```powershell
   python -X utf8 -m plan1_auto_restock -c config.local.yaml bootstrap-goods --name "测试成品号" --price 15.9 --category-no C365194
   ```
4. 将 16688 `goods_no` 和 Canboso `product_id` 填入 `config.local.yaml` 并把对应 SKU 设为 `enabled: true`，先做只读巡检和演练：
   ```powershell
   python -X utf8 -m plan1_auto_restock -c config.local.yaml check
   python -X utf8 -m plan1_auto_restock -c config.local.yaml sync-once --dry-run
   ```
5. 确认演练计划无误后，再开启单轮或常驻实盘（双锁防误扣费）：
   ```powershell
   python -X utf8 -m plan1_auto_restock -c config.local.yaml sync-once --live --i-confirm-real-spend
   python -X utf8 -m plan1_auto_restock -c config.local.yaml daemon --live --i-confirm-real-spend
   ```
6. 导出兼容 `multistore-ai-ops` 的对账 CSV（零卡密明文）：
   ```powershell
   python -X utf8 -m plan1_auto_restock -c config.local.yaml export-csv
   ```
7. 验证告警通道（发送测试邮件到 QQ 邮箱，零扣费）：
   ```powershell
   python -X utf8 -m plan1_auto_restock -c config.local.yaml test-alert
   ```

## 内置安全护栏
- **默认演练**：`dry_run: true`，实盘必须显式传 `--live --i-confirm-real-spend`。
- **双库隔离**：普通对账库 `ledger.db` 不存任何明文账号/密码/卡密；原始交付卡密单独原子落盘至 `secrets_vault.db`。
- **零丢卡与防重扣**：先写 `PENDING` 意图并绑定 `Idempotency-Key` → 下单 → 先落保险库 → 再上传 16688；超时复用原幂等键恢复，上传失败直接从保险库补传不重复买单。
- **风控熔断**：硬拦 `quantity <= 0` 与非 `account` 商品、限制单次件数与单日美元预算、余额预警、冷却时间、上游涨价超阈值自动熔断下架、断货归零自动下架、16688 投诉去重告警、Canboso 401/403 鉴权熔断。

## 凭证管理与本地配置约定

- **严禁明文入库**：真实 16688 `APP_ID`/`SECRET`、Canboso `tgb_*` Key、SMTP 授权码、商户号与真实业务 SKU 映射一律只放在本地 `.env` 和 `config.local.yaml` 中（已在 `.gitignore` 忽略），不要写入文档、源码或测试用例。
- **告警策略**：支持 Webhook 与 SMTP（如 QQ 邮箱 SSL 465）双通道；默认仅推送 `WARNING`/`ERROR`/`CRITICAL`，同类事件 30 分钟冷却防刷屏；`COMPLAINT_16688` 可通过 `exclude_event_types` 排除邮件重复推送，仅落本地账本。
- **上线与停售切换**：`runtime.dry_run` 默认保持 `true`，实盘补库需显式传 `--live --i-confirm-real-spend`；若某个 16688 商品在后台临时下架，应同步将其在 `config.local.yaml` 中的 SKU 置为 `enabled: false`。

## 已验证的 16688 开放接口行为

- `/openApi/order/list` 真实返回的金额与付款时间字段为 `total_amount`（及 `goods_total_amount`）和 `success_time`，而非 `pay_price` / `pay_time`；已在 `plan1_auto_restock/storage.py` 兼容处理。
- `/openApi/order/list` 与 `/openApi/order/info` 均实际返回 `contact_email`（官方文档未列出）。
- `/openApi/order/list` 按 `card_no` 反查为**全字符串精确匹配**（仅传邮箱子串查不到，传完整卡密字符串才能命中）。
- `/openApi/stockSecret/list` 出参 `status` 中 `0=未售`、`2=已售`，售出后会自动回填 `trade_no`。
- 已下架商品（`status=0`）调用 `/openApi/goods/detail` 会返回“商品不存在”；停售商品应同步把配置中的 SKU 置为 `enabled: false`。
