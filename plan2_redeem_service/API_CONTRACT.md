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
- `waiting_fulfillment`：共享车位上游已受理（`paid` / `waiting_seller`），排队拉人中（2-6h，最晚 24h）
- `delivered_pending_warranty`：独享整组已秒发商店套餐邮箱，待买家建家庭组邀请并回填保修信息
- `completed`：履约完成（共享车位上游状态已变为 `completed`/`delivered`；独享整组买家已提交保修登记）
- `needs_manual_review`：触发异常需商家人工介入（价格熔断、余额不足、上游断货/报错、24h 超时等；对买家隐藏内部敏感原因）
- `revoked`：兑换码已作废（退款、投诉或商家手动作废）

### 2.4 保修状态 (`warranty.status`，仅 `ultra_exclusive_admin`)
- `pending`：等待买家提交主 Gmail + 邀请截图
- `submitted`：买家已提交保修信息，已发邮件通知商家
- `registered_upstream`：商家已在后台标记“已向 Canboso 管理员报备”

### 2.5 通知策略（硬性要求）
- 仅在需要商家人工介入时发送邮件通知（正常拉人完成、独享自动出货成功不发邮件打扰）：
  1. 共享车位**超过 24 小时**仍未完成（**严禁在 6 小时发送提醒邮件**）
  2. 独享整组买家提交保修信息（需人工转发上游报备，`warranty.status -> submitted`）
  3. 异常报警：单笔价格超熔断上限、Canboso 余额低于阈值、上游断货/报错、订单进入 `needs_manual_review`

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
    "eta_text": "一般 2-6 小时内发家庭组邀请，最晚 24 小时",
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
  "wangwang_message": "感谢下单 Google Ultra x20 共享车位！\n点链接自助填 Gmail 提货（不用在旺旺发邮箱）：https://redeem.example.com/r/U20S-8K3M-9P2Q\n兑换码：U20S-8K3M-9P2Q\n\n【提货前准备】\n1. 关闭付款资料：https://payments.google.com/gp/w/home/settings\n2. 退出原有家庭组：https://g.co/yourfamily\n3. 近 12 个月内没换过家庭组，不然可能进不来\n\n【发货与进度】\n提交邮箱后一般 2-6 小时内发家庭组邀请（最晚 24 小时），未超时勿催；随时可点链接看进度，收到邮件接受邀请即可。\n\n【使用须知】\n1. 质保 30 天会员权益，不质保您的 Google 账号；自行反代或违反官方条款导致封号不负责。\n2. 本款为 0 积分/点数车位，使用积分或点数会被机器人检测踢出家庭组且不退款。\n3. 确认后非商品问题不退款；买家原因填错邮箱或需换号，需补 39.9 元手续费。",
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
- 16688 渠道导入失败时返回结构化错误，不再裸抛 `500`：
  - `CHANNEL_16688_REJECTED`（`422` 或 `502`）：16688 明确拒绝（如商品非卡密/自动发货、商品不存在、连接被拒绝），本地不落库、不留孤儿兑换码。
  - `CHANNEL_16688_TIMEOUT`（`504`）或 `CHANNEL_16688_ERROR`（`502`）：请求结果不明确（超时或 5xx/断流），为防止 16688 实际已导入导致买家找不到码，本地会以「待核对库存」备注保留该批次兑换码并提示商家核对。

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

## 6. 阿奇索（Agiso）自动发货接口

### 6.1 消息推送回调（免管理员 Token，使用阿奇索 MD5 签名校验）
`POST /api/v1/webhooks/agiso?fromPlatform=TbAlds&timestamp=1700000000&aopic=2&sign=<md5>`
- 请求体：`application/x-www-form-urlencoded`，包含 `json=<JSON字符串>`（兼容按表单解析时的 `+` 空格解码，也可从 Query 兜底读取）。
- 验签规则：
  - 时间戳校验：`PLAN2_AGISO_TIMESTAMP_TOLERANCE_SECONDS > 0` 时要求 `|now - timestamp| <= tolerance`（默认 600 秒）。
  - 签名算法：`md5(app_secret + "json" + raw_json + "timestamp" + timestamp + app_secret).lower()`，使用常量时间比对。
- 文本响应（`text/plain`）：
  - `200 OK`，响应体为空串 `""`：验签成功且事件处理完成（或幂等/安全忽略）。
  - `400 Bad Request`：`"参数错误"` 或 `"缺少订单号"`（`aopic` / `json` 非法或简化推送缺 `Tid`）。
  - `403 Forbidden`：`"时间戳无效"` 或 `"验签失败"`。
  - `503 Service Unavailable`：`"服务未配置"`（未配 `PLAN2_AGISO_APP_SECRET`）、`"未配置AccessToken"`、`"订单查询失败"` 或瞬态核心发货失败（触发阿奇索重试）。
- 事件行为：
  - `fromPlatform=Open`：开放平台「模拟推送」验签探测，直接落审计事件 `simulated_verified` 并返回 `200 ""`，不生成兑换码。
  - `aopic=2`（付款推送含明细）/ `aopic=2097152`（简化付款推送，自动调用 `TradeInfo` 反查）：仅处理 `Status=WAIT_SELLER_SEND_GOODS`；严格按已启用 SKU 映射匹配（优先级 `outer_sku_id > sku_id > num_iid+sku_props_contains > outer_iid > sku_props_contains > num_iid`，未命中商品安全忽略，单子单数量超过 50 暂停并告警）。按 `delivery_key = "{tid}:{oid}:{seq}"` 原子幂等签发兑换码，合并同单所有命中子单执行 `CreateOrderResult` -> `LogisticsDummySend` -> 可选 `SendTbAppCard` / `WwMsg/Send`。自动发货成功**不发邮件**，失败或部分失败才告警。
  - `aopic=256`（申请退款）/ `aopic=65536`（退款成功）：按 `Oid` 精确匹配（避免多子单混合订单跨子单误伤）；`unused` 码自动作废且不发邮件，已使用/履约中/已完成码不作废并发送人工核对告警邮件。
  - `aopic=2048`（阿奇索卡密池自动发货成功）：从已发卡密中提取 `U20S-...` / `U20E-...` 并绑定 `external_order_id`。

### 6.2 阿奇索集成状态（管理员鉴权）
`GET /api/v1/admin/agiso/status`
- 严禁返回 `app_secret` 或 `access_token` 明文，仅返回布尔配置状态与统计。
- `200 OK`：
```json
{
  "configured": true,
  "webhook_ready": true,
  "api_ready": true,
  "app_id": "2026000000000000000",
  "has_app_secret": true,
  "has_access_token": true,
  "webhook_url": "https://redeem.example.com/api/v1/webhooks/agiso",
  "api_base_url": "https://gw-api.agiso.com",
  "execution_mode": "live",
  "auto_create_result": true,
  "auto_dummy_send": true,
  "auto_send_tb_card": true,
  "auto_send_ww_msg": false,
  "auto_revoke_on_refund": true,
  "poll_enabled": false,
  "poll_interval_seconds": 120,
  "sku_mappings_count": 1,
  "deliveries_summary": {
    "issued": 0,
    "delivering": 0,
    "delivered": 0,
    "partial": 0,
    "failed": 0,
    "revoked": 0,
    "total": 0
  },
  "events_total": 0
}
```

### 6.3 淘宝 SKU 映射规则管理（管理员鉴权）
- `GET /api/v1/admin/agiso/sku-mappings`
  - 返回 `{ "items": [ ...数据库规则... ], "env_rules": [ ...环境变量只读规则... ] }`。
- `POST /api/v1/admin/agiso/sku-mappings`（`201 Created`）
  - 请求：
```json
{
  "sku_key": "ultra_shared_slot",
  "num_iid": "1100000000000",
  "sku_id": "5000000000000",
  "outer_iid": "",
  "outer_sku_id": "",
  "sku_props_contains": "",
  "enabled": true,
  "note": "Ultra 20x 共享车位"
}
```
  - 规则：至少填写 `num_iid` / `sku_id` / `outer_iid` / `outer_sku_id` / `sku_props_contains` 之一，否则返回 `422 MATCHER_REQUIRED`；未知 `sku_key` 返回 `422 UNKNOWN_SKU`。
- `DELETE /api/v1/admin/agiso/sku-mappings/{mapping_id}`
  - 删除成功返回 `{ "ok": true, "mapping_id": 1 }`，不存在返回 `404 MAPPING_NOT_FOUND`。

### 6.4 阿奇索事件与发货跟踪日志（管理员鉴权）
`GET /api/v1/admin/agiso/events?tid=&aopic=&status=&page=1&page_size=20`
- `200 OK`：
```json
{
  "items": [
    {
      "event_id": 1,
      "event_key": null,
      "source": "webhook",
      "from_platform": "TbAlds",
      "aopic": 2,
      "tid": "TB202611250001",
      "oid": "",
      "status": "delivered",
      "detail": "自动发货完成（1 份）",
      "payload_summary": "Tid=TB202611250001 Status=WAIT_SELLER_SEND_GOODS Orders=1",
      "created_at": "2026-11-25T02:00:00+08:00"
    }
  ],
  "total": 1,
  "page": 1,
  "page_size": 20
}
```
- 当传入 `tid` 时，响应额外附带 `"deliveries": [...]` 明细列表。

### 6.5 单笔补发 / 重试（管理员鉴权）
`POST /api/v1/admin/agiso/orders/{tid}/retry`
- 请求体（可选）：`{ "fetch_from_agiso": true }`
- 行为：若本地已有 `agiso_deliveries` 记录，仅补执行未完成的步骤（不重复调用已成功的 `CreateOrderResult` / `LogisticsDummySend`）；若本地无记录且 `fetch_from_agiso=true`，先调 `TradeInfo` 拉单后走自动发货；若 `fetch_from_agiso=false` 且无记录返回 `404 ORDER_NOT_FOUND`；上游错误返回 `503 AGISO_ERROR`。

### 6.6 手动拉单补漏（管理员鉴权）
`POST /api/v1/admin/agiso/sync-orders`
- 请求体（可选）：
```json
{
  "lookback_minutes": 60,
  "page_size": 50,
  "max_pages": 5
}
```
- 按北京时间（UTC+8）分页调用 `TradesBuyerPay` 并对 `WAIT_SELLER_SEND_GOODS` 订单执行幂等自动发货。
- `200 OK`：`{ "modify_time_start", "fetched", "fulfilled", "skipped", "failed", "items" }`；上游失败返回 `503 AGISO_ERROR`。

### 6.7 拉取淘宝商品 SKU 列表（管理员鉴权）
`GET /api/v1/admin/agiso/remote-skus?num_iids=1100000000000,1100000000001`
- `num_iids` 逗号分隔，必填且最多 40 个（否则 `422 NUM_IIDS_REQUIRED` / `422 TOO_MANY_NUM_IIDS`）。
- 调用阿奇索 `Item/SkusGet`，返回 `{ "items": [...] }`；上游失败返回 `503 AGISO_ERROR`。

### 6.8 批量制码并推送到阿奇索卡密库（管理员鉴权）
`POST /api/v1/admin/agiso/push-cards`（`201 Created`）
- 请求：
```json
{
  "sku_key": "ultra_shared_slot",
  "cpc_id": 12345,
  "count": 20,
  "batch_label": "阿奇索首批20张",
  "is_use_first": false,
  "allow_replace": false
}
```
- `count` 范围 `1..500`。批量签发兑换码并调用 `CardPwd/Add`（每行 `提货链接,兑换码`）。
- 若阿奇索返回确定性业务拒绝（HTTP 200 且 `IsSuccess=false` 且非瞬态），自动作废本次新建的 `unused` 码防止孤儿库存；网络/瞬态异常则保留以便核对。
- `201 Created`：`{ "batch_id", "sku_key", "cpc_id", "count", "codes", "agiso_result" }`；校验错误 `422`，上游失败 `503 AGISO_ERROR`。

## 7. 健康检查
`GET /api/v1/health` -> `{ "status": "ok", "execution_mode": "dry_run" }`
