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

export interface AdminCanbosoStatus {
  balance_usd: number | null;
  low_balance_threshold_usd: number;
  is_low_balance: boolean;
  checked_at: string | null;
  error: string | null;
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

export interface AdminOverviewResponse {
  public_base_url: string;
  canboso: AdminCanbosoStatus;
  skus: AdminSkuStatus[];
  counts: AdminOverviewCounts;
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
  warranty_status: WarrantyStatus | null;
  last_error_code: string | null;
  last_error_message: string | null;
  is_overdue_24h: boolean;
  elapsed_minutes: number | null;
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
