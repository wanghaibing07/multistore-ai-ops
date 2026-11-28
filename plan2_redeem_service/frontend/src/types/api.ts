export type SkuKey = "ultra_shared_slot" | "ultra_exclusive_admin";
export type ProductType = "slot" | "account";
export type Channel = "taobao" | "16688";

export type CodeStatus =
  | "unused"
  | "validating"
  | "purchasing"
  | "waiting_fulfillment"
  | "delivered_pending_warranty"
  | "completed"
  | "needs_manual_review"
  | "revoked";

export type AdminOrderStatusFilter =
  | CodeStatus
  | "in_stock"
  | "dispatched_unused"
  | "manual_attention"
  | "pending_warranty_ack";

export type WarrantyStatus = "pending" | "submitted" | "registered_upstream";

export type TgForwardStatus = "disabled" | "pending" | "sent" | "failed";

export interface ApiErrorEnvelope {
  error_code: string;
  message: string;
}

export interface DeliveryCredential {
  label: string;
  value: string;
}

export interface DeliveryPayload {
  account_text: string;
  credentials: DeliveryCredential[];
  raw_lines: string[];
  delivered_at: string;
}

export interface PublicWarrantyInfo {
  status: WarrantyStatus;
  admin_email_masked: string | null;
  submitted_at: string | null;
  has_screenshot: boolean;
  note: string | null;
}

export interface PublicFulfillmentInfo {
  eta_text: string | null;
  submitted_at: string | null;
  completed_at: string | null;
  last_polled_at: string | null;
  elapsed_minutes: number | null;
}

export interface PublicCodeResponse {
  code: string;
  sku_key: SkuKey;
  sku_title: string;
  product_type: ProductType;
  channel: Channel;
  status: CodeStatus;
  buyer_email_masked: string | null;
  delivery: DeliveryPayload | null;
  warranty: PublicWarrantyInfo | null;
  fulfillment: PublicFulfillmentInfo;
  buyer_notice: string | null;
  support_hint: string;
  created_at: string;
  updated_at: string;
  cooldown_seconds?: number;
}

export interface RedeemSlotRequest {
  buyer_email: string;
  buyer_email_confirm?: string;
  preflight_confirmed: true;
}

export interface RedeemExclusiveRequest {
  preflight_confirmed: true;
}

export interface SubmitPublicWarrantyRequest {
  admin_email: string;
  admin_email_confirm?: string;
  note?: string;
}

export interface AdminCanbosoStatus {
  balance_usd: number | null;
  low_balance_threshold_usd: number;
  is_low_balance: boolean;
  checked_at: string | null;
  error: string | null;
  today_purchases?: {
    order_count: number;
    gross_usd: number;
    cancelled_count: number;
    cancelled_gross_usd: number;
  };
}

export interface AdminSkuStatus {
  sku_key: SkuKey;
  title: string;
  product_type: ProductType;
  canboso_product_id: string;
  goods_no: string | null;
  current_price_usd: number | null;
  max_cost_usd: number;
  available: number | null;
  is_available: boolean;
  price_exceeded: boolean;
  unused_codes_taobao: number;
  unused_codes_16688: number;
  in_stock_codes_taobao?: number;
  dispatched_unused_codes_taobao?: number;
  in_stock_codes_16688?: number;
  dispatched_unused_codes_16688?: number;
}

export interface AdminOverviewCounts {
  total: number;
  unused: number;
  in_stock: number;
  dispatched_unused: number;
  waiting_fulfillment: number;
  delivered_pending_warranty: number;
  pending_warranty_ack: number;
  manual_attention: number;
  completed: number;
  needs_manual_review: number;
  revoked: number;
  overdue_24h: number;
}

export interface AdminTelegramWarrantyStatus {
  enabled: boolean;
  ready: boolean;
  target_configured: boolean;
}

export interface AdminOverviewResponse {
  public_base_url: string;
  canboso: AdminCanbosoStatus;
  skus: AdminSkuStatus[];
  counts: AdminOverviewCounts;
  telegram_warranty?: AdminTelegramWarrantyStatus;
}

export interface AdminCodeSummary {
  code: string;
  sku_key: SkuKey;
  sku_title: string;
  product_type: ProductType;
  channel: Channel;
  goods_no: string | null;
  external_order_id: string | null;
  remark: string | null;
  status: CodeStatus;
  display_status?: AdminOrderStatusFilter;
  is_dispatched?: boolean;
  buyer_email: string | null;
  buyer_email_masked: string | null;
  store_email: string | null;
  upstream_order_code: string | null;
  upstream_status: string | null;
  cost_usd: number | null;
  upstream_refund_usd: number | null;
  upstream_refund_note: string | null;
  warranty_status: WarrantyStatus | null;
  last_error_code: string | null;
  last_error_message: string | null;
  is_overdue_24h: boolean;
  elapsed_minutes: number | null;
  can_poll_upstream?: boolean;
  can_reorder?: boolean;
  can_reset_unused?: boolean;
  submitted_at: string | null;
  completed_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface AdminWarrantyDetail {
  status: WarrantyStatus;
  admin_email: string | null;
  submitted_at: string | null;
  registered_at: string | null;
  note: string | null;
  has_screenshot: boolean;
  screenshot_url: string | null;
  forward_template: string | null;
  auto_forwarded?: boolean;
  tg_forward_status?: TgForwardStatus | null;
  tg_message_id?: number | string | null;
  tg_sent_at?: string | null;
  tg_error?: string | null;
}

export interface AuditEventItem {
  event_id: number;
  from_status: string | null;
  to_status: string;
  reason: string;
  created_at: string;
}

export interface AdminCodeDetail extends AdminCodeSummary {
  redeem_url: string;
  batch_id: string | null;
  buyer_email: string | null;
  idempotency_key: string | null;
  delivery: DeliveryPayload | null;
  warranty: AdminWarrantyDetail | null;
  audit_events: AuditEventItem[];
}

export interface AdminCodeListResponse {
  items: AdminCodeSummary[];
  total: number;
  page: number;
  page_size: number;
}

export interface AdminIssueCodeRequest {
  sku_key: SkuKey;
  channel: Channel;
  external_order_id: string;
  remark?: string;
}

export interface AdminIssueCodeResponse {
  code: string;
  redeem_url: string;
  wangwang_message: string;
  item: AdminCodeSummary;
}

export interface AdminBatchCodeRequest {
  sku_key: SkuKey;
  channel: Channel;
  count: number;
  goods_no?: string;
  batch_label?: string;
}

export interface AdminBatchCodeResponse {
  batch_id: string;
  sku_key: SkuKey;
  channel: Channel;
  count: number;
  codes: string[];
  export_lines_code_only: string;
  export_lines_with_url: string;
}

export interface AdminUpdateSkuRequest {
  max_cost_usd: number;
  goods_no?: string | null;
}

export interface AdminTestEmailResponse {
  ok: boolean;
  message: string;
}

export interface AdminUpstreamRefundRequest {
  upstream_refund_usd: number;
  note?: string;
}

export interface AdminAgisoStatus {
  configured: boolean;
  webhook_ready: boolean;
  api_ready: boolean;
  app_id: string;
  has_app_secret: boolean;
  has_access_token: boolean;
  webhook_url: string;
  api_base_url: string;
  execution_mode: string;
  auto_create_result: boolean;
  auto_dummy_send: boolean;
  auto_send_tb_card: boolean;
  auto_send_ww_msg: boolean;
  auto_revoke_on_refund: boolean;
  auto_restore_on_refund_close: boolean;
  poll_enabled: boolean;
  poll_interval_seconds: number;
  restock_enabled: boolean;
  restock_interval_seconds: number;
  restock_rules_count: number;
  sku_mappings_count: number;
  deliveries_summary: Record<string, number>;
  events_total: number;
}

export interface AdminAgisoSkuMapping {
  mapping_id?: number;
  sku_key: SkuKey;
  num_iid: string;
  sku_id: string;
  outer_iid: string;
  outer_sku_id: string;
  sku_props_contains: string;
  enabled: boolean;
  note: string;
  source: "db" | "env";
  created_at?: string;
  updated_at?: string;
}

export interface AdminAgisoSkuMappingsResponse {
  items: AdminAgisoSkuMapping[];
  env_rules: AdminAgisoSkuMapping[];
}

export interface AdminCreateAgisoSkuMappingRequest {
  sku_key: SkuKey;
  num_iid?: string;
  sku_id?: string;
  outer_iid?: string;
  outer_sku_id?: string;
  sku_props_contains?: string;
  enabled?: boolean;
  note?: string;
}

export interface AdminAgisoDelivery {
  delivery_key: string;
  tid: string;
  oid: string;
  seq: number;
  num_iid: string;
  sku_id: string;
  sku_key: SkuKey | string;
  code: string;
  buyer_nick: string;
  delivery_status: string;
  result_created: number | boolean;
  card_sent: number | boolean;
  ww_sent: number | boolean;
  dummy_sent: number | boolean;
  last_error: string | null;
  created_at: string;
  updated_at: string;
}

export interface AdminAgisoEvent {
  event_id: number;
  event_key: string | null;
  source: string;
  from_platform: string;
  aopic: number | null;
  tid: string;
  oid: string;
  status: string;
  detail: string;
  payload_summary: string;
  created_at: string;
}

export interface AdminAgisoEventsResponse {
  items: AdminAgisoEvent[];
  total: number;
  page: number;
  page_size: number;
  deliveries?: AdminAgisoDelivery[];
}

export interface AdminAgisoSyncOrdersRequest {
  lookback_minutes?: number;
  page_size?: number;
  max_pages?: number;
}

export interface AdminAgisoSyncOrdersResponse {
  modify_time_start: string;
  fetched: number;
  fulfilled: number;
  skipped: number;
  failed: number;
  items: Array<{
    tid?: string;
    status?: string;
    codes?: string[];
  }>;
}

export interface AdminAgisoRetryOrderResponse {
  status: string;
  tid?: string;
  trade_status?: string;
  codes?: string[];
  deliveries?: AdminAgisoDelivery[];
  [key: string]: unknown;
}

export interface AdminAgisoRemoteSkuItem {
  NumIid?: string | number;
  SkuId?: string | number;
  PropertiesName?: string;
  Properties?: string;
  OuterId?: string;
  Quantity?: number;
  Price?: string | number;
  [key: string]: unknown;
}

export interface AdminAgisoRemoteSkusResponse {
  items: AdminAgisoRemoteSkuItem[];
}

export interface AdminAgisoPushCardsRequest {
  sku_key: SkuKey;
  cpc_id: number;
  count: number;
  batch_label?: string;
  is_use_first?: boolean;
  allow_replace?: boolean;
}

export interface AdminAgisoPushCardsResponse {
  batch_id: string;
  sku_key: SkuKey;
  cpc_id: number;
  count: number;
  codes: string[];
  partial_failure?: boolean;
  add_success_count?: number;
  add_fail_count?: number;
  agiso_result?: Record<string, unknown>;
}

export interface AdminAgisoStockItem {
  sku_key: SkuKey;
  sku_title: string;
  cpc_id: number;
  enabled: boolean;
  min_stock: number;
  target_stock: number;
  max_push: number;
  current_qty: number | null;
  storage_found: boolean;
  needs_restock: boolean;
  planned_push: number;
  local_unused_taobao: number;
  local_in_stock_taobao: number;
  local_dispatched_unused_taobao: number;
}

export interface AdminAgisoStockResponse {
  restock_enabled: boolean;
  items: AdminAgisoStockItem[];
}

export interface AdminRestockResultItem {
  sku_key: string;
  cpc_id?: number;
  goods_no?: string;
  current_qty: number | null;
  after_qty?: number;
  min_stock: number;
  target_stock: number;
  action: string;
  planned_push?: number;
  pushed_count: number;
  batch_id?: string;
  error?: string;
}

export interface AdminAgisoRestockResponse {
  trigger: string;
  dry_check: boolean;
  checked: number;
  restocked: number;
  total_pushed: number;
  errors: string[];
  items: AdminRestockResultItem[];
}

export interface AdminChannel16688StockItem {
  sku_key: SkuKey;
  sku_title: string;
  goods_no: string;
  enabled: boolean;
  min_stock: number;
  target_stock: number;
  max_push: number;
  current_qty: number | null;
  locked_qty: number | null;
  sold_qty: number | null;
  stock_found: boolean;
  needs_restock: boolean;
  planned_push: number;
  error: string | null;
  local_unused_16688: number;
  local_in_stock_16688: number;
  local_dispatched_unused_16688: number;
}

export interface AdminChannel16688StockResponse {
  restock_enabled: boolean;
  restock_interval_seconds: number;
  items: AdminChannel16688StockItem[];
}

export interface AdminChannel16688RestockResponse {
  trigger: string;
  dry_check: boolean;
  checked: number;
  restocked: number;
  total_pushed: number;
  sync_sold?: Record<string, unknown>;
  stock_conflicts?: number;
  errors: string[];
  items: AdminRestockResultItem[];
}
