# Plan 2 自助提货与履约服务 — 前后端接口契约 (v1)

> 共享约定文件。前端 (`plan2_redeem_service/frontend/`) 与后端 (`plan2_redeem_service/backend/`) 严格以此契约对接。

## 1. 目录与部署约定

- 根目录：`D:\Codex-Projects\ai-commerce-sourcing-ledger\plan2_redeem_service\`
- 前端目录：`plan2_redeem_service/frontend/`（React + TypeScript + Vite，构建产物输出到 `frontend/dist/`）
- 后端目录：`plan2_redeem_service/backend/`（Python 3.12 + FastAPI + Uvicorn + SQLite WAL + httpx + SMTP）
- 测试目录：`plan2_redeem_service/tests/`（后端单元测试与集成测试）
- 同源部署：
  - 开发期：Vite dev server 将 `/api` 代理到 `http://127.0.0.1:8000`。
  - 生产期：FastAPI 挂载 `/api/v1/*`，并在 `frontend/dist/` 存在时托管静态资源与 SPA fallback（`/`, `/r/{code}`, `/admin`, `/admin/*` 返回 `frontend/dist/index.html`）。

## 2. 业务常量与枚举

### 2.1 SKU 定义 (`sku_key`)

| `sku_key` | 展示名称 (`sku_title`) | `product_type` | Canboso `productId` | 参考进价 | 默认熔断上限 `max_cost_usd` |
|---|---|---|---|---:|---:|
| `ultra_shared_slot` | Google AI Ultra 20x 共享车位（1个月） | `slot` | `6ac26aaeec73a42d6430a837` | `$13.47` | `$16.00` |
| `ultra_exclusive_admin` | Google AI Ultra 20x 独享整组·管理员版（1个月） | `account` | `6ae03419c0080f9e15f695a8` | `$67.50` | `$72.00` |

### 2.2 渠道 (`channel`)
- `taobao`：淘宝订单（当前手动单笔发码；也支持批量制码导出供阿奇索/91卡券导入）
- `16688`：16688 自营卡密单（核销前后端必须实时调用 16688 Open API 校验已付款/未退款/无退款中投诉）

### 2.3 兑换码/订单生命周期状态 (`status`)
- `unused`：未使用，等待买家提交核销
- `validating`：正在校验订单与上游状态（瞬态）
- `purchasing`：正在调用 Canboso 下单（`Idempotency-Key` 已落库，防双花）
- `waiting_fulfillment`：共享车位上游已受理（`paid` / `waiting_seller`），排队拉人中（1~6h，最晚 24h）
- `delivered_pending_warranty`：独享整组已秒发商店套餐邮箱，待买家建家庭组邀请并回填保修信息
- `completed`：履约完成（共享车位上游状态已变为 `completed`/`delivered`；独享整组买家已提交保修登记）
- `needs_manual_review`：触发异常需商家人工介入（价格熔断、余额不足、上游断货/报错、24h 超时等；对买家隐藏内部敏感原因）
- `revoked`：兑换码已作废（退款、投诉或商家手动作废）

### 2.4 保修状态 (`warranty.status`，仅 `ultra_exclusive_admin`)
- `pending`：等待买家提交主 Gmail + 邀请截图
- `submitted`：买家已提交保修信息，已发邮件通知商家
- `registered_upstream`：商家已在后台标记“已向 Canboso 管理员报备”

### 2.5 通知策略（硬性要求）
- 邮件通知仅在以下事件发送给商家：
  1. 共享车位拉人完成 (`waiting_fulfillment -> completed`)
  2. 共享车位**超过 24 小时**仍未完成（**严禁在 6 小时发送提醒邮件**）
  3. 独享整组出货成功 (`delivered_pending_warranty`)
  4. 独享整组买家提交保修信息 (`warranty.status -> submitted`)
  5. 异常报警：单笔价格超熔断上限、Canboso 余额低于阈值、上游断货/报错、订单进入 `needs_manual_review`

## 3. 通用约定

- 所有时间字段使用带时区的 ISO 8601 字符串，例如 `2026-11-23T06:00:00+08:00`。
- 错误统一返回 JSON：
  ```json
  {
    "error_code": "ERROR_CODE_HERE",
    "message": "面向当前调用方的中文说明"
  }
  ```
- 安全隔离：
  - 所有 `/api/v1/public/*` 接口严禁泄露 Canboso 余额、USD 成本、API Key、内部堆栈或未脱敏的敏感配置。
  - 所有 `/api/v1/admin/*` 接口必须校验请求头 `Authorization: Bearer <ADMIN_TOKEN>`，失败返回 `401`（`error_code: "UNAUTHORIZED"`）。

## 4. 买家公开接口 (`/api/v1/public/*`)

### 4.1 查询兑换码状态与进度
`GET /api/v1/public/codes/{code}`

- `200 OK` 返回 `PublicCodeResponse`：
```json
{
  "code": "U20S-8K3M-9P2Q",
  "sku_key": "ultra_shared_slot",
  "sku_title": "Google AI Ultra 20x 共享车位（1个月）",
  "product_type": "slot",
  "channel": "taobao",
  "status": "unused",
  "buyer_email_masked": null,
  "delivery": null,
  "warranty": null,
  "fulfillment": {
    "eta_text": "预计 1~6 小时内发出家庭组邀请，最晚不超过 24 小时",
    "submitted_at": null,
    "completed_at": null,
    "last_polled_at": null,
    "elapsed_minutes": null
  },
  "buyer_notice": null,
  "support_hint": "如有疑问，请携带订单号与兑换码联系店铺客服处理。",
  "created_at": "2026-11-23T06:00:00+08:00",
  "updated_at": "2026-11-23T06:00:00+08:00"
}
```
- 独享整组已出货时，`delivery` 与 `warranty` 结构：
```json
{
  "delivery": {
    "account_text": "store-ultra-01@gmail.com",
    "credentials": [
      { "label": "商店套餐邮箱", "value": "store-ultra-01@gmail.com" }
    ],
    "raw_lines": ["store-ultra-01@gmail.com"],
    "delivered_at": "2026-11-23T06:05:12+08:00"
  },
  "warranty": {
    "status": "pending",
    "admin_email_masked": null,
    "submitted_at": null,
    "has_screenshot": false,
    "note": null
  }
}
```
- `404 Not Found`：`error_code = "CODE_NOT_FOUND"`

### 4.2 买家提交核销
`POST /api/v1/public/codes/{code}/redeem`

- 请求体（`ultra_shared_slot`）：
```json
{
  "buyer_email": "alice@gmail.com",
  "preflight_confirmed": true
}
```
- 说明：`buyer_email_confirm` 为选填兼容字段（`string | null`）；不传或传空字符串时跳过二次邮箱比对，仅在显式传了非空值且与 `buyer_email` 不一致时返回 `422 EMAIL_MISMATCH`。
- 请求体（`ultra_exclusive_admin`）：
```json
{
  "preflight_confirmed": true
}
```
- `200 OK`：返回最新 `PublicCodeResponse`（共享车位进入 `waiting_fulfillment`，独享整组进入 `delivered_pending_warranty`，若触发熔断/余额不足/上游异常则进入 `needs_manual_review` 并返回友好 `buyer_notice`）。
- 常见错误码：
  - `404` `CODE_NOT_FOUND`
  - `409` `CODE_REVOKED` / `CODE_ALREADY_REDEEMED` / `ORDER_NOT_ELIGIBLE` / `CONCURRENT_IN_PROGRESS`
  - `422` `INVALID_EMAIL` / `EMAIL_MISMATCH` / `PREFLIGHT_REQUIRED`
  - `503` `TEMPORARILY_UNAVAILABLE`

### 4.3 买家手动刷新上游进度
`POST /api/v1/public/codes/{code}/refresh`

- 仅对 `waiting_fulfillment` 实际触发一次限流后的 Canboso 订单查询（单码冷却 15 秒，冷却期内直接返回当前库内状态）。
- `200 OK`：返回 `PublicCodeResponse`（可附加 `cooldown_seconds: int`）。

### 4.4 独享整组保修登记提交
`POST /api/v1/public/codes/{code}/warranty`

- `Content-Type: multipart/form-data`
  - `admin_email`：必填，合法 Gmail（提交后会同步写入 `codes.buyer_email` 与 `codes.buyer_email_masked`；公网接口仍只返回脱敏值）
  - `admin_email_confirm`：选填（向后兼容）；不传或传空时跳过二次比对，仅在显式传了非空值且与 `admin_email` 不一致时返回 `422 EMAIL_MISMATCH`
  - `screenshot`：必填，图片文件（`image/jpeg`, `image/png`, `image/webp`，最大 5MB）
  - `note`：选填，最多 300 字
- 允许在 `delivered_pending_warranty`（首次提交）或 `completed` 且 `warranty.status == "submitted"`（报备前修改重传）时提交；若已 `registered_upstream` 返回 `409 WARRANTY_LOCKED`。
- `200 OK`：返回更新后的 `PublicCodeResponse`（`status = "completed"`, `warranty.status = "submitted"`）。
- 隐私约束：所有 `/api/v1/public/*` 响应严禁返回明文 `buyer_email`、`admin_email`、`forward_template` 或 `upstream_order_code`。

## 5. 商家后台接口 (`/api/v1/admin/*`)

所有接口均需 Header：`Authorization: Bearer <ADMIN_TOKEN>`。

### 5.1 后台概览与上游状态
`GET /api/v1/admin/overview`

```json
{
  "execution_mode": "dry_run",
  "public_base_url": "https://redeem.example.com",
  "canboso": {
    "balance_usd": 17.30,
    "low_balance_threshold_usd": 80.0,
    "is_low_balance": true,
    "checked_at": "2026-11-23T06:00:00+08:00",
    "error": null
  },
  "skus": [
    {
      "sku_key": "ultra_shared_slot",
      "title": "Google AI Ultra 20x 共享车位（1个月）",
      "product_type": "slot",
      "canboso_product_id": "6ac26aaeec73a42d6430a837",
      "goods_no": null,
      "current_price_usd": 13.47,
      "max_cost_usd": 16.0,
      "available": null,
      "is_available": true,
      "price_exceeded": false,
      "unused_codes_taobao": 0,
      "unused_codes_16688": 0
    }
  ],
  "counts": {
    "total": 0,
    "unused": 0,
    "waiting_fulfillment": 0,
    "delivered_pending_warranty": 0,
    "completed": 0,
    "needs_manual_review": 0,
    "revoked": 0,
    "overdue_24h": 0
  }
}
```

### 5.2 强制刷新上游余额与 SKU 行情
`POST /api/v1/admin/upstream/refresh`
- 返回最新 `overview` 结构。

### 5.3 单笔快速发码（淘宝手动发货专用）
`POST /api/v1/admin/codes/issue`
- 请求：
```json
{
  "sku_key": "ultra_shared_slot",
  "channel": "taobao",
  "external_order_id": "TB202611230001",
  "remark": "旺旺ID可选"
}
```
- `201 Created` 响应：
```json
{
  "code": "U20S-8K3M-9P2Q",
  "redeem_url": "https://redeem.example.com/r/U20S-8K3M-9P2Q",
  "wangwang_message": "感谢下单 Google Ultra x20 共享车位！\n点链接填您的 Gmail 提货：https://redeem.example.com/r/U20S-8K3M-9P2Q\n兑换码：U20S-8K3M-9P2Q\n提交后一般 1~6 小时内发家庭组邀请（最晚 24 小时），随时能在链接里看进度。",
  "item": { "...AdminCodeSummary..." }
}
```

### 5.4 批量制码（16688 卡密导入 / 淘宝阿奇索码池）
`POST /api/v1/admin/codes/batch`
- 请求：
```json
{
  "sku_key": "ultra_shared_slot",
  "channel": "16688",
  "count": 20,
  "goods_no": "G12345678",
  "batch_label": "16688首批20张"
}
```
- `count` 范围 `1..500`。
- `201 Created` 响应：
```json
{
  "batch_id": "B20261123060501",
  "sku_key": "ultra_shared_slot",
  "channel": "16688",
  "count": 20,
  "codes": ["U20S-...."],
  "export_lines_code_only": "U20S-....\n...",
  "export_lines_with_url": "提货链接：https://redeem.example.com/r/U20S-.... | 兑换码：U20S-....\n..."
}
```

### 5.5 订单与兑换码列表
`GET /api/v1/admin/codes?status=&channel=&sku_key=&q=&page=1&page_size=20`
- 搜索参数 `q` 模糊匹配：`code`、`external_order_id`、`buyer_email`、`upstream_order_code`、`remark`、`warranty_json`、`delivery_json`（确保新旧独享单均可通过买家主邮箱或商店套餐邮箱 `store_email` 搜索命中）。
- `200 OK`：
```json
{
  "items": [
    {
      "code": "U20S-8K3M-9P2Q",
      "sku_key": "ultra_shared_slot",
      "sku_title": "Google AI Ultra 20x 共享车位（1个月）",
      "product_type": "slot",
      "channel": "taobao",
      "goods_no": null,
      "external_order_id": "TB202611230001",
      "remark": "",
      "status": "waiting_fulfillment",
      "buyer_email": "alice@gmail.com",
      "buyer_email_masked": "al***@gmail.com",
      "store_email": null,
      "upstream_order_code": "ORD-12345",
      "upstream_status": "paid",
      "cost_usd": 13.47,
      "warranty_status": null,
      "last_error_code": null,
      "last_error_message": null,
      "is_overdue_24h": false,
      "elapsed_minutes": 45,
      "created_at": "2026-11-23T06:00:00+08:00",
      "updated_at": "2026-11-23T06:45:00+08:00"
    }
  ],
  "total": 1,
  "page": 1,
  "page_size": 20
}
```
- 独享单适配说明：
  - `buyer_email` 优先取 `codes.buyer_email`，历史单兜底取 `warranty.admin_email`；`buyer_email_masked` 同步兜底脱敏展示。
  - `store_email` 从 `delivery_json.account_text` 提取，独享单出货后展示商店套餐邮箱，共享车位或未出货订单为 `null`。

### 5.6 兑换码详情（含完整事件流、交付凭据与保修信息）
`GET /api/v1/admin/codes/{code}`
- 在 `AdminCodeSummary` 字段基础上增加：
  - `redeem_url`: string
  - `batch_id`: string | null
  - `buyer_email`: string | null（沿用 `AdminCodeSummary` 的兜底解析逻辑，兼容历史独享单；日志严禁打印明文）
  - `idempotency_key`: string | null
  - `delivery`: 同公开结构或 null
  - `warranty`: `{ "status", "admin_email", "submitted_at", "registered_at", "note", "has_screenshot", "screenshot_url", "forward_template" }` 或 null（只要含 `admin_email`，后端每次返回时都会动态按最新越南语模板生成 `forward_template`，兼容历史订单无需改库）
  - `audit_events`: `[{ "event_id", "from_status", "to_status", "reason", "created_at" }]`

### 5.7 获取保修确认截图（需鉴权）
`GET /api/v1/admin/codes/{code}/warranty-screenshot`
- 返回图片二进制流与对应 `Content-Type`（前端带 Bearer Token 拉取 Blob 展示，不公开暴露静态 URL）。

### 5.8 后台操作动作
- `POST /api/v1/admin/codes/{code}/poll`：立即查一次 Canboso 订单状态，返回最新 `AdminCodeDetail`。
- `POST /api/v1/admin/codes/{code}/revoke`：请求 `{ "reason": "买家退款" }`，作废兑换码，返回最新 `AdminCodeDetail`。
- `POST /api/v1/admin/codes/{code}/retry`：请求 `{ "override_price_limit": false, "note": "" }`，对 `needs_manual_review` 订单安全重试（复用已落库 `Idempotency-Key` 防重复扣款），返回最新 `AdminCodeDetail`。
- `POST /api/v1/admin/codes/{code}/warranty-ack`：请求 `{ "note": "已发Canboso管理员" }`，将独享保修状态标记为 `registered_upstream`，返回最新 `AdminCodeDetail`。

### 5.9 风控配置与邮件测试
- `PATCH /api/v1/admin/settings/skus/{sku_key}`：请求 `{ "max_cost_usd": 16.0, "goods_no": "G12345678" }`，返回更新后的 SKU 对象。
- `POST /api/v1/admin/notifications/test-email`：发送一封测试邮件到商家邮箱，返回 `{ "ok": true, "message": "..." }`。

## 6. 健康检查
`GET /api/v1/health` -> `{ "status": "ok", "execution_mode": "dry_run" }`
