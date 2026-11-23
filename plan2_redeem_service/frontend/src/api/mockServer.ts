import type {
  AdminBatchCodeRequest,
  AdminBatchCodeResponse,
  AdminCodeDetail,
  AdminCodeListResponse,
  AdminCodeSummary,
  AdminIssueCodeRequest,
  AdminIssueCodeResponse,
  AdminOverviewResponse,
  AdminSkuStatus,
  AdminTestEmailResponse,
  AdminUpdateSkuRequest,
  Channel,
  PublicCodeResponse,
  RedeemExclusiveRequest,
  RedeemSlotRequest,
  SkuKey,
} from "../types/api";
import { validateGmailInput } from "../utils/validation";

export class MockApiError extends Error {
  status: number;
  errorCode: string;

  constructor(status: number, errorCode: string, message: string) {
    super(message);
    this.status = status;
    this.errorCode = errorCode;
  }
}

interface MockRecord {
  detail: AdminCodeDetail;
  screenshotDataUrl: string | null;
  buyerNotice: string | null;
  submittedAt: string | null;
  completedAt: string | null;
  lastPolledAt: string | null;
  pollCount: number;
}

function nowIso(offsetMinutes = 0): string {
  const date = new Date(Date.now() + offsetMinutes * 60_000);
  return date.toISOString();
}

function maskEmail(email: string): string {
  const [local, domain] = email.split("@");
  if (!local || !domain) return "***";
  const prefix = local.slice(0, Math.min(2, local.length));
  return `${prefix}***@${domain}`;
}

function randomSegment(length: number): string {
  const alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  let out = "";
  for (let idx = 0; idx < length; idx += 1) {
    out += alphabet[Math.floor(Math.random() * alphabet.length)];
  }
  return out;
}

function makeDemoSvgDataUrl(
  adminEmail = "buyer.main@gmail.com",
  storeEmail = "store-ultra@example.com"
): string {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360" viewBox="0 0 640 360">
    <rect width="640" height="360" fill="#f8fafc"/>
    <rect x="32" y="32" width="576" height="296" rx="16" fill="#ffffff" stroke="#cbd5e1" stroke-width="2"/>
    <text x="64" y="96" font-family="sans-serif" font-size="22" font-weight="700" fill="#0f172a">Google Family Invitation Sent (Demo Screenshot)</text>
    <text x="64" y="142" font-family="monospace" font-size="16" fill="#334155">Admin: ${adminEmail}</text>
    <text x="64" y="176" font-family="monospace" font-size="16" fill="#059669">Invited Member: ${storeEmail}</text>
    <rect x="64" y="216" width="220" height="44" rx="10" fill="#dcfce7"/>
    <text x="86" y="244" font-family="sans-serif" font-size="15" font-weight="600" fill="#166534">Status: Invitation Pending</text>
  </svg>`;
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}

function formatWarrantyForwardTemplate(orderCode: string, adminEmail: string): string {
  return [
    "Chào shop ạ, em gửi thông tin đăng ký bảo hành gói Family:",
    `- Mã đơn: ${orderCode}`,
    `- Gmail chủ nhóm: ${adminEmail}`,
    "Em đã mời tài khoản shop vào nhóm và gửi kèm ảnh xác nhận, nhờ shop ghi nhận bảo hành giúp em ạ. Cảm ơn shop!",
  ].join("\n");
}

const SKU_META: Record<SkuKey, AdminSkuStatus> = {
  ultra_shared_slot: {
    sku_key: "ultra_shared_slot",
    title: "VEO3 Ultra x20 共享车位（1个月·0信用全保）",
    product_type: "slot",
    canboso_product_id: "6ac26aaeec73a42d6430a837",
    goods_no: "G88001001",
    current_price_usd: 13.47,
    max_cost_usd: 16.0,
    available: null,
    is_available: true,
    price_exceeded: false,
    unused_codes_taobao: 0,
    unused_codes_16688: 0,
  },
  ultra_exclusive_admin: {
    sku_key: "ultra_exclusive_admin",
    title: "VEO3 Ultra x20 独享整组·管理员版（1个月全保）",
    product_type: "account",
    canboso_product_id: "6ae03419c0080f9e15f695a8",
    goods_no: "G88001002",
    current_price_usd: 67.5,
    max_cost_usd: 72.0,
    available: 224,
    is_available: true,
    price_exceeded: false,
    unused_codes_taobao: 0,
    unused_codes_16688: 0,
  },
};

function buildOrigin(): string {
  if (typeof window !== "undefined" && window.location.origin) {
    return window.location.origin;
  }
  return "https://redeem.example.com";
}

function buildWangwangMessage(skuKey: SkuKey, code: string, redeemUrl: string): string {
  if (skuKey === "ultra_exclusive_admin") {
    return [
      "你好，感谢购买 Google AI Ultra 20x 独享整组（1个月全保）！",
      "点下面链接先领商店套餐邮箱，再用你自己的主力 Gmail 建家庭组把商店邮箱拉进去：",
      redeemUrl,
      `兑换码：${code}`,
      "拉进组后回到这个页面，填你的主力 Gmail 并传一张邀请截图交保修。",
    ].join("\n");
  }
  return [
    "你好，感谢购买 Google AI Ultra 20x 共享车位（1个月·0信用全保）！",
    "点下面链接填你的 Gmail 等拉人，一般 1~6 小时、最晚 24 小时，随时可回本页看进度：",
    redeemUrl,
    `兑换码：${code}`,
    "提交前看清 Gmail 拼写，提交后改不了。",
  ].join("\n");
}

class DemoMockStore {
  records = new Map<string, MockRecord>();
  balanceUsd = 17.3;
  lowBalanceThresholdUsd = 80.0;
  checkedAt = nowIso(-2);
  skus: Record<SkuKey, AdminSkuStatus> = {
    ultra_shared_slot: { ...SKU_META.ultra_shared_slot },
    ultra_exclusive_admin: { ...SKU_META.ultra_exclusive_admin },
  };

  constructor() {
    this.seed();
  }

  private seed(): void {
    const origin = buildOrigin();
    this.records.set("DEMO-SLOT-NEW", {
      detail: {
        code: "DEMO-SLOT-NEW",
        sku_key: "ultra_shared_slot",
        sku_title: this.skus.ultra_shared_slot.title,
        product_type: "slot",
        channel: "taobao",
        goods_no: null,
        external_order_id: "TB202611230001",
        remark: "淘宝测试单，待提货",
        status: "unused",
        buyer_email: null,
        buyer_email_masked: null,
        store_email: null,
        upstream_order_code: null,
        upstream_status: null,
        cost_usd: null,
        warranty_status: null,
        last_error_code: null,
        last_error_message: null,
        is_overdue_24h: false,
        elapsed_minutes: null,
        created_at: nowIso(-25),
        updated_at: nowIso(-25),
        redeem_url: `${origin}/r/DEMO-SLOT-NEW`,
        batch_id: null,
        idempotency_key: null,
        delivery: null,
        warranty: null,
        audit_events: [
          {
            event_id: 1,
            from_status: null,
            to_status: "unused",
            reason: "商家在淘宝手动发了兑换码",
            created_at: nowIso(-25),
          },
        ],
      },
      screenshotDataUrl: null,
      buyerNotice: null,
      submittedAt: null,
      completedAt: null,
      lastPolledAt: null,
      pollCount: 0,
    });

    this.records.set("DEMO-SLOT-WAIT", {
      detail: {
        code: "DEMO-SLOT-WAIT",
        sku_key: "ultra_shared_slot",
        sku_title: this.skus.ultra_shared_slot.title,
        product_type: "slot",
        channel: "16688",
        goods_no: "G88001001",
        external_order_id: "16688-ORD-99102",
        remark: "16688 自动发码订单",
        status: "waiting_fulfillment",
        buyer_email: "alice.demo@gmail.com",
        buyer_email_masked: "al***@gmail.com",
        store_email: null,
        upstream_order_code: "CNB-SLOT-88412",
        upstream_status: "paid",
        cost_usd: 13.47,
        warranty_status: null,
        last_error_code: null,
        last_error_message: null,
        is_overdue_24h: false,
        elapsed_minutes: 78,
        created_at: nowIso(-90),
        updated_at: nowIso(-5),
        redeem_url: `${origin}/r/DEMO-SLOT-WAIT`,
        batch_id: "B20261123040001",
        idempotency_key: "idem_demo_slot_wait_001",
        delivery: null,
        warranty: null,
        audit_events: [
          {
            event_id: 1,
            from_status: null,
            to_status: "unused",
            reason: "批量生成 16688 兑换码",
            created_at: nowIso(-90),
          },
          {
            event_id: 2,
            from_status: "unused",
            to_status: "waiting_fulfillment",
            reason: "买家填了 Gmail，16688 订单核对通过，已让上游排队拉人",
            created_at: nowIso(-78),
          },
        ],
      },
      screenshotDataUrl: null,
      buyerNotice: null,
      submittedAt: nowIso(-78),
      completedAt: null,
      lastPolledAt: nowIso(-3),
      pollCount: 0,
    });

    this.records.set("DEMO-SLOT-DONE", {
      detail: {
        code: "DEMO-SLOT-DONE",
        sku_key: "ultra_shared_slot",
        sku_title: this.skus.ultra_shared_slot.title,
        product_type: "slot",
        channel: "taobao",
        goods_no: null,
        external_order_id: "TB202611220881",
        remark: "已完成样例",
        status: "completed",
        buyer_email: "bob.ultra@gmail.com",
        buyer_email_masked: "bo***@gmail.com",
        store_email: null,
        upstream_order_code: "CNB-SLOT-88301",
        upstream_status: "completed",
        cost_usd: 13.47,
        warranty_status: null,
        last_error_code: null,
        last_error_message: null,
        is_overdue_24h: false,
        elapsed_minutes: 142,
        created_at: nowIso(-260),
        updated_at: nowIso(-118),
        redeem_url: `${origin}/r/DEMO-SLOT-DONE`,
        batch_id: null,
        idempotency_key: "idem_demo_slot_done_001",
        delivery: null,
        warranty: null,
        audit_events: [
          {
            event_id: 1,
            from_status: null,
            to_status: "unused",
            reason: "商家发码",
            created_at: nowIso(-260),
          },
          {
            event_id: 2,
            from_status: "unused",
            to_status: "waiting_fulfillment",
            reason: "已通知上游开始排队拉人",
            created_at: nowIso(-240),
          },
          {
            event_id: 3,
            from_status: "waiting_fulfillment",
            to_status: "completed",
            reason: "上游已发出家庭组邀请，并邮件通知商家",
            created_at: nowIso(-118),
          },
        ],
      },
      screenshotDataUrl: null,
      buyerNotice: null,
      submittedAt: nowIso(-240),
      completedAt: nowIso(-118),
      lastPolledAt: nowIso(-118),
      pollCount: 2,
    });

    this.records.set("DEMO-EXCL-NEW", {
      detail: {
        code: "DEMO-EXCL-NEW",
        sku_key: "ultra_exclusive_admin",
        sku_title: this.skus.ultra_exclusive_admin.title,
        product_type: "account",
        channel: "taobao",
        goods_no: null,
        external_order_id: "TB202611230009",
        remark: "独享整组，待领商店邮箱",
        status: "unused",
        buyer_email: null,
        buyer_email_masked: null,
        store_email: null,
        upstream_order_code: null,
        upstream_status: null,
        cost_usd: null,
        warranty_status: null,
        last_error_code: null,
        last_error_message: null,
        is_overdue_24h: false,
        elapsed_minutes: null,
        created_at: nowIso(-18),
        updated_at: nowIso(-18),
        redeem_url: `${origin}/r/DEMO-EXCL-NEW`,
        batch_id: null,
        idempotency_key: null,
        delivery: null,
        warranty: null,
        audit_events: [
          {
            event_id: 1,
            from_status: null,
            to_status: "unused",
            reason: "商家在淘宝手动发了兑换码",
            created_at: nowIso(-18),
          },
        ],
      },
      screenshotDataUrl: null,
      buyerNotice: null,
      submittedAt: null,
      completedAt: null,
      lastPolledAt: null,
      pollCount: 0,
    });

    this.records.set("DEMO-EXCL-PENDING", {
      detail: {
        code: "DEMO-EXCL-PENDING",
        sku_key: "ultra_exclusive_admin",
        sku_title: this.skus.ultra_exclusive_admin.title,
        product_type: "account",
        channel: "16688",
        goods_no: "G88001002",
        external_order_id: "16688-ORD-99218",
        remark: "独享整组，已发商店邮箱待交保修",
        status: "delivered_pending_warranty",
        buyer_email: null,
        buyer_email_masked: null,
        store_email: "store-ultra-pending@example.com",
        upstream_order_code: "CNB-ACC-55210",
        upstream_status: "completed",
        cost_usd: 67.5,
        warranty_status: "pending",
        last_error_code: null,
        last_error_message: null,
        is_overdue_24h: false,
        elapsed_minutes: 8,
        created_at: nowIso(-35),
        updated_at: nowIso(-8),
        redeem_url: `${origin}/r/DEMO-EXCL-PENDING`,
        batch_id: "B20261123040002",
        idempotency_key: "idem_demo_excl_pending_001",
        delivery: {
          account_text: "store-ultra-pending@example.com",
          credentials: [{ label: "商店套餐邮箱", value: "store-ultra-pending@example.com" }],
          raw_lines: ["store-ultra-pending@example.com"],
          delivered_at: nowIso(-8),
        },
        warranty: {
          status: "pending",
          admin_email: null,
          submitted_at: null,
          registered_at: null,
          note: null,
          has_screenshot: false,
          screenshot_url: null,
          forward_template: null,
        },
        audit_events: [
          {
            event_id: 1,
            from_status: null,
            to_status: "unused",
            reason: "批量生成 16688 兑换码",
            created_at: nowIso(-35),
          },
          {
            event_id: 2,
            from_status: "unused",
            to_status: "delivered_pending_warranty",
            reason: "已发出商店套餐邮箱，等买家建家庭组并交保修",
            created_at: nowIso(-8),
          },
        ],
      },
      screenshotDataUrl: null,
      buyerNotice: null,
      submittedAt: nowIso(-8),
      completedAt: null,
      lastPolledAt: nowIso(-8),
      pollCount: 0,
    });

    this.records.set("DEMO-EXCL-WARRANTY", {
      detail: {
        code: "DEMO-EXCL-WARRANTY",
        sku_key: "ultra_exclusive_admin",
        sku_title: this.skus.ultra_exclusive_admin.title,
        product_type: "account",
        channel: "taobao",
        goods_no: null,
        external_order_id: "TB202611230012",
        remark: "独享整组，已交保修等商家报备",
        status: "completed",
        buyer_email: "buyer.main@gmail.com",
        buyer_email_masked: "bu***@gmail.com",
        store_email: "store-ultra@example.com",
        upstream_order_code: "CNB-ACC-55190",
        upstream_status: "completed",
        cost_usd: 67.5,
        warranty_status: "submitted",
        last_error_code: null,
        last_error_message: null,
        is_overdue_24h: false,
        elapsed_minutes: 12,
        created_at: nowIso(-45),
        updated_at: nowIso(-10),
        redeem_url: `${origin}/r/DEMO-EXCL-WARRANTY`,
        batch_id: null,
        idempotency_key: "idem_demo_excl_002",
        delivery: {
          account_text: "store-ultra@example.com",
          credentials: [{ label: "商店套餐邮箱", value: "store-ultra@example.com" }],
          raw_lines: ["store-ultra@example.com"],
          delivered_at: nowIso(-22),
        },
        warranty: {
          status: "submitted",
          admin_email: "buyer.main@gmail.com",
          submitted_at: nowIso(-10),
          registered_at: null,
          note: "已经把商店邮箱拉进家庭组了",
          has_screenshot: true,
          screenshot_url: "/api/v1/admin/codes/DEMO-EXCL-WARRANTY/warranty-screenshot",
          forward_template: formatWarrantyForwardTemplate("CNB-ACC-55190", "buyer.main@gmail.com"),
        },
        audit_events: [
          {
            event_id: 1,
            from_status: null,
            to_status: "unused",
            reason: "商家发码",
            created_at: nowIso(-45),
          },
          {
            event_id: 2,
            from_status: "unused",
            to_status: "delivered_pending_warranty",
            reason: "已发出商店套餐邮箱，等买家交保修",
            created_at: nowIso(-22),
          },
          {
            event_id: 3,
            from_status: "delivered_pending_warranty",
            to_status: "completed",
            reason: "买家提交了主力 Gmail 和邀请截图，已邮件通知商家",
            created_at: nowIso(-10),
          },
        ],
      },
      screenshotDataUrl: makeDemoSvgDataUrl("buyer.main@gmail.com", "store-ultra@example.com"),
      buyerNotice: null,
      submittedAt: nowIso(-22),
      completedAt: nowIso(-10),
      lastPolledAt: nowIso(-22),
      pollCount: 0,
    });

    this.records.set("DEMO-EXCL-DONE", {
      detail: {
        code: "DEMO-EXCL-DONE",
        sku_key: "ultra_exclusive_admin",
        sku_title: this.skus.ultra_exclusive_admin.title,
        product_type: "account",
        channel: "taobao",
        goods_no: null,
        external_order_id: "TB202611220918",
        remark: "独享整组，已向官方报备完成",
        status: "completed",
        buyer_email: "buyer.done@gmail.com",
        buyer_email_masked: "bu***@gmail.com",
        store_email: "store-ultra-done@example.com",
        upstream_order_code: "CNB-ACC-55120",
        upstream_status: "completed",
        cost_usd: 67.5,
        warranty_status: "registered_upstream",
        last_error_code: null,
        last_error_message: null,
        is_overdue_24h: false,
        elapsed_minutes: 16,
        created_at: nowIso(-180),
        updated_at: nowIso(-60),
        redeem_url: `${origin}/r/DEMO-EXCL-DONE`,
        batch_id: null,
        idempotency_key: "idem_demo_excl_done_003",
        delivery: {
          account_text: "store-ultra-done@example.com",
          credentials: [{ label: "商店套餐邮箱", value: "store-ultra-done@example.com" }],
          raw_lines: ["store-ultra-done@example.com"],
          delivered_at: nowIso(-150),
        },
        warranty: {
          status: "registered_upstream",
          admin_email: "buyer.done@gmail.com",
          submitted_at: nowIso(-134),
          registered_at: nowIso(-60),
          note: "已发给官方登记全保",
          has_screenshot: true,
          screenshot_url: "/api/v1/admin/codes/DEMO-EXCL-DONE/warranty-screenshot",
          forward_template: formatWarrantyForwardTemplate("CNB-ACC-55120", "buyer.done@gmail.com"),
        },
        audit_events: [
          {
            event_id: 1,
            from_status: null,
            to_status: "unused",
            reason: "商家发码",
            created_at: nowIso(-180),
          },
          {
            event_id: 2,
            from_status: "unused",
            to_status: "delivered_pending_warranty",
            reason: "已发出商店套餐邮箱，等买家交保修",
            created_at: nowIso(-150),
          },
          {
            event_id: 3,
            from_status: "delivered_pending_warranty",
            to_status: "completed",
            reason: "买家提交了主力 Gmail 和邀请截图",
            created_at: nowIso(-134),
          },
          {
            event_id: 4,
            from_status: "completed",
            to_status: "completed",
            reason: "商家已向官方完成保修报备",
            created_at: nowIso(-60),
          },
        ],
      },
      screenshotDataUrl: makeDemoSvgDataUrl("buyer.done@gmail.com", "store-ultra-done@example.com"),
      buyerNotice: null,
      submittedAt: nowIso(-150),
      completedAt: nowIso(-134),
      lastPolledAt: nowIso(-150),
      pollCount: 0,
    });

    this.records.set("DEMO-REVIEW", {
      detail: {
        code: "DEMO-REVIEW",
        sku_key: "ultra_shared_slot",
        sku_title: this.skus.ultra_shared_slot.title,
        product_type: "slot",
        channel: "16688",
        goods_no: "G88001001",
        external_order_id: "16688-ORD-99155",
        remark: "模拟进价超上限暂停",
        status: "needs_manual_review",
        buyer_email: "charlie.demo@gmail.com",
        buyer_email_masked: "ch***@gmail.com",
        store_email: null,
        upstream_order_code: null,
        upstream_status: null,
        cost_usd: null,
        warranty_status: null,
        last_error_code: "PRICE_LIMIT_EXCEEDED",
        last_error_message: "上游实时进价 $19.80 超过设定的上限 $16.00，已自动暂停下单",
        is_overdue_24h: false,
        elapsed_minutes: 15,
        created_at: nowIso(-30),
        updated_at: nowIso(-15),
        redeem_url: `${origin}/r/DEMO-REVIEW`,
        batch_id: "B20261123040001",
        idempotency_key: null,
        delivery: null,
        warranty: null,
        audit_events: [
          {
            event_id: 1,
            from_status: null,
            to_status: "unused",
            reason: "批量制码",
            created_at: nowIso(-30),
          },
          {
            event_id: 2,
            from_status: "unused",
            to_status: "needs_manual_review",
            reason: "进价超上限（$19.80 > $16.00），已暂停下单并邮件提醒商家",
            created_at: nowIso(-15),
          },
        ],
      },
      screenshotDataUrl: null,
      buyerNotice: "已经收到你的提货请求，正在人工核对安排处理。收藏好这个页面，处理完会自动更新，不用重复提交。",
      submittedAt: nowIso(-15),
      completedAt: null,
      lastPolledAt: null,
      pollCount: 0,
    });

    this.records.set("DEMO-REVOKED", {
      detail: {
        code: "DEMO-REVOKED",
        sku_key: "ultra_shared_slot",
        sku_title: this.skus.ultra_shared_slot.title,
        product_type: "slot",
        channel: "taobao",
        goods_no: null,
        external_order_id: "TB202611220330",
        remark: "买家申请退款已作废",
        status: "revoked",
        buyer_email: null,
        buyer_email_masked: null,
        store_email: null,
        upstream_order_code: null,
        upstream_status: null,
        cost_usd: null,
        warranty_status: null,
        last_error_code: null,
        last_error_message: null,
        is_overdue_24h: false,
        elapsed_minutes: null,
        created_at: nowIso(-320),
        updated_at: nowIso(-300),
        redeem_url: `${origin}/r/DEMO-REVOKED`,
        batch_id: null,
        idempotency_key: null,
        delivery: null,
        warranty: null,
        audit_events: [
          {
            event_id: 1,
            from_status: null,
            to_status: "unused",
            reason: "商家发码",
            created_at: nowIso(-320),
          },
          {
            event_id: 2,
            from_status: "unused",
            to_status: "revoked",
            reason: "买家退款，商家手动作废",
            created_at: nowIso(-300),
          },
        ],
      },
      screenshotDataUrl: null,
      buyerNotice: null,
      submittedAt: null,
      completedAt: null,
      lastPolledAt: null,
      pollCount: 0,
    });
  }

  private toPublic(rec: MockRecord): PublicCodeResponse {
    const d = rec.detail;
    return {
      code: d.code,
      sku_key: d.sku_key,
      sku_title: d.sku_title,
      product_type: d.product_type,
      channel: d.channel,
      status: d.status,
      buyer_email_masked: d.buyer_email_masked,
      delivery: d.delivery,
      warranty: d.warranty
        ? {
            status: d.warranty.status,
            admin_email_masked: d.warranty.admin_email
              ? maskEmail(d.warranty.admin_email)
              : null,
            submitted_at: d.warranty.submitted_at,
            has_screenshot: d.warranty.has_screenshot,
            note: d.warranty.note,
          }
        : null,
      fulfillment: {
        eta_text:
          d.product_type === "slot"
            ? "一般 1~6 小时内发家庭组邀请，最晚 24 小时"
            : null,
        submitted_at: rec.submittedAt,
        completed_at: rec.completedAt,
        last_polled_at: rec.lastPolledAt,
        elapsed_minutes: d.elapsed_minutes,
      },
      buyer_notice: rec.buyerNotice,
      support_hint: "有问题请把订单号和兑换码发给客服。",
      created_at: d.created_at,
      updated_at: d.updated_at,
    };
  }

  private toSummary(d: AdminCodeDetail): AdminCodeSummary {
    return {
      code: d.code,
      sku_key: d.sku_key,
      sku_title: d.sku_title,
      product_type: d.product_type,
      channel: d.channel,
      goods_no: d.goods_no,
      external_order_id: d.external_order_id,
      remark: d.remark,
      status: d.status,
      buyer_email: d.buyer_email,
      buyer_email_masked: d.buyer_email_masked,
      store_email: d.store_email,
      upstream_order_code: d.upstream_order_code,
      upstream_status: d.upstream_status,
      cost_usd: d.cost_usd,
      warranty_status: d.warranty_status,
      last_error_code: d.last_error_code,
      last_error_message: d.last_error_message,
      is_overdue_24h: d.is_overdue_24h,
      elapsed_minutes: d.elapsed_minutes,
      created_at: d.created_at,
      updated_at: d.updated_at,
    };
  }

  private requireToken(token: string): void {
    if (!token || token.trim().length < 4) {
      throw new MockApiError(401, "UNAUTHORIZED", "登录口令不对（演练模式随便输 4 位以上字符就能进）");
    }
  }

  getPublicCode(code: string): PublicCodeResponse {
    const rec = this.records.get(code.toUpperCase());
    if (!rec) {
      throw new MockApiError(404, "CODE_NOT_FOUND", "没找到这个兑换码，核对一下有没有输错。");
    }
    return this.toPublic(rec);
  }

  redeemCode(
    code: string,
    payload: RedeemSlotRequest | RedeemExclusiveRequest
  ): PublicCodeResponse {
    const rec = this.records.get(code.toUpperCase());
    if (!rec) {
      throw new MockApiError(404, "CODE_NOT_FOUND", "没找到这个兑换码，核对一下有没有输错。");
    }
    if (!payload.preflight_confirmed) {
      throw new MockApiError(422, "PREFLIGHT_REQUIRED", "请先勾选确认上面的提示再提交。");
    }
    if (rec.detail.status === "revoked") {
      throw new MockApiError(409, "CODE_REVOKED", "这个兑换码已经作废了，不能再用。");
    }
    if (rec.detail.status !== "unused") {
      throw new MockApiError(409, "CODE_ALREADY_REDEEMED", "这个兑换码已经提交过了，直接看当前进度就行。");
    }

    const now = nowIso();
    if (rec.detail.product_type === "slot") {
      const slotPayload = payload as RedeemSlotRequest;
      const emailCheck = validateGmailInput(slotPayload.buyer_email || "");
      if (!emailCheck.valid) {
        throw new MockApiError(422, "INVALID_EMAIL", emailCheck.error || "邮箱格式不对，核对一下再试。");
      }
      const confirmRaw = (slotPayload.buyer_email_confirm || "").trim();
      if (confirmRaw && emailCheck.normalized.toLowerCase() !== confirmRaw.toLowerCase()) {
        throw new MockApiError(422, "EMAIL_MISMATCH", "两次填的 Gmail 邮箱不一样。");
      }
      rec.detail.buyer_email = emailCheck.normalized;
      rec.detail.buyer_email_masked = maskEmail(emailCheck.normalized);
      rec.detail.status = "waiting_fulfillment";
      rec.detail.upstream_order_code = `CNB-SLOT-${randomSegment(6)}`;
      rec.detail.upstream_status = "paid";
      rec.detail.cost_usd = this.skus.ultra_shared_slot.current_price_usd;
      rec.detail.idempotency_key = `idem_${randomSegment(12)}`;
      rec.detail.elapsed_minutes = 0;
      rec.detail.updated_at = now;
      rec.submittedAt = now;
      rec.lastPolledAt = now;
      rec.detail.audit_events.push({
        event_id: rec.detail.audit_events.length + 1,
        from_status: "unused",
        to_status: "waiting_fulfillment",
        reason: "演练模式：已模拟提交共享车位排队拉人（不扣钱）",
        created_at: now,
      });
      return this.toPublic(rec);
    }

    const storeEmail = `store-ultra-${randomSegment(4).toLowerCase()}@example.com`;
    const upstreamOrder = `CNB-ACC-${randomSegment(6)}`;
    rec.detail.status = "delivered_pending_warranty";
    rec.detail.store_email = storeEmail;
    rec.detail.upstream_order_code = upstreamOrder;
    rec.detail.upstream_status = "completed";
    rec.detail.cost_usd = this.skus.ultra_exclusive_admin.current_price_usd;
    rec.detail.idempotency_key = `idem_${randomSegment(12)}`;
    rec.detail.warranty_status = "pending";
    rec.detail.updated_at = now;
    rec.submittedAt = now;
    rec.detail.delivery = {
      account_text: storeEmail,
      credentials: [{ label: "商店套餐邮箱", value: storeEmail }],
      raw_lines: [storeEmail],
      delivered_at: now,
    };
    rec.detail.warranty = {
      status: "pending",
      admin_email: null,
      submitted_at: null,
      registered_at: null,
      note: null,
      has_screenshot: false,
      screenshot_url: null,
      forward_template: null,
    };
    rec.detail.audit_events.push({
      event_id: rec.detail.audit_events.length + 1,
      from_status: "unused",
      to_status: "delivered_pending_warranty",
      reason: "演练模式：已模拟发出独享整组商店邮箱（不扣钱）",
      created_at: now,
    });
    return this.toPublic(rec);
  }

  refreshPublicCode(code: string): PublicCodeResponse {
    const rec = this.records.get(code.toUpperCase());
    if (!rec) {
      throw new MockApiError(404, "CODE_NOT_FOUND", "没找到这个兑换码。");
    }
    if (rec.detail.status === "waiting_fulfillment") {
      rec.pollCount += 1;
      rec.lastPolledAt = nowIso();
      rec.detail.updated_at = rec.lastPolledAt;
      if (rec.pollCount >= 2) {
        rec.detail.status = "completed";
        rec.detail.upstream_status = "completed";
        rec.completedAt = rec.lastPolledAt;
        rec.detail.audit_events.push({
          event_id: rec.detail.audit_events.length + 1,
          from_status: "waiting_fulfillment",
          to_status: "completed",
          reason: "演练模式：上游已拉人完成，已邮件通知商家",
          created_at: rec.lastPolledAt,
        });
      }
    }
    return { ...this.toPublic(rec), cooldown_seconds: 15 };
  }

  async submitWarranty(code: string, formData: FormData): Promise<PublicCodeResponse> {
    const rec = this.records.get(code.toUpperCase());
    if (!rec) {
      throw new MockApiError(404, "CODE_NOT_FOUND", "没找到这个兑换码。");
    }
    if (rec.detail.product_type !== "account" || !rec.detail.delivery) {
      throw new MockApiError(409, "INVALID_STATE", "这笔订单现在还不能交保修。");
    }
    if (rec.detail.warranty?.status === "registered_upstream") {
      throw new MockApiError(409, "WARRANTY_LOCKED", "商家已经向官方报备完成了，不能再改。");
    }

    const adminEmail = String(formData.get("admin_email") || "");
    const adminEmailConfirm = String(formData.get("admin_email_confirm") || "").trim();
    const note = String(formData.get("note") || "").trim();
    const file = formData.get("screenshot");

    const check = validateGmailInput(adminEmail);
    if (!check.valid) {
      throw new MockApiError(422, "INVALID_EMAIL", check.error || "主力 Gmail 格式不对。");
    }
    if (adminEmailConfirm && check.normalized.toLowerCase() !== adminEmailConfirm.toLowerCase()) {
      throw new MockApiError(422, "EMAIL_MISMATCH", "两次填的主力 Gmail 不一样。");
    }
    if (!(file instanceof File)) {
      throw new MockApiError(422, "SCREENSHOT_REQUIRED", "请先选一张家庭组邀请确认截图。");
    }

    const storeEmailForSvg = rec.detail.store_email || rec.detail.delivery.account_text;
    const dataUrl = await new Promise<string>((resolve) => {
      const reader = new FileReader();
      reader.onload = () =>
        resolve(String(reader.result || makeDemoSvgDataUrl(check.normalized, storeEmailForSvg)));
      reader.onerror = () => resolve(makeDemoSvgDataUrl(check.normalized, storeEmailForSvg));
      reader.readAsDataURL(file);
    });

    const now = nowIso();
    const prevStatus = rec.detail.status;
    rec.detail.status = "completed";
    rec.detail.buyer_email = check.normalized;
    rec.detail.buyer_email_masked = maskEmail(check.normalized);
    rec.detail.warranty_status = "submitted";
    rec.detail.updated_at = now;
    rec.completedAt = now;
    rec.screenshotDataUrl = dataUrl;
    rec.detail.warranty = {
      status: "submitted",
      admin_email: check.normalized,
      submitted_at: now,
      registered_at: null,
      note: note || null,
      has_screenshot: true,
      screenshot_url: `/api/v1/admin/codes/${rec.detail.code}/warranty-screenshot`,
      forward_template: formatWarrantyForwardTemplate(
        rec.detail.upstream_order_code || "",
        check.normalized
      ),
    };
    rec.detail.audit_events.push({
      event_id: rec.detail.audit_events.length + 1,
      from_status: prevStatus,
      to_status: "completed",
      reason: "买家提交了主力 Gmail 和邀请截图，已邮件通知商家",
      created_at: now,
    });
    return this.toPublic(rec);
  }

  getOverview(token: string): AdminOverviewResponse {
    this.requireToken(token);
    const counts = {
      total: 0,
      unused: 0,
      waiting_fulfillment: 0,
      delivered_pending_warranty: 0,
      completed: 0,
      needs_manual_review: 0,
      revoked: 0,
      overdue_24h: 0,
    };
    const unusedCounters: Record<SkuKey, Record<Channel, number>> = {
      ultra_shared_slot: { taobao: 0, "16688": 0 },
      ultra_exclusive_admin: { taobao: 0, "16688": 0 },
    };
    for (const rec of this.records.values()) {
      const d = rec.detail;
      counts.total += 1;
      if (d.status === "unused") {
        counts.unused += 1;
        unusedCounters[d.sku_key][d.channel] += 1;
      } else if (d.status === "waiting_fulfillment") {
        counts.waiting_fulfillment += 1;
      } else if (d.status === "delivered_pending_warranty") {
        counts.delivered_pending_warranty += 1;
      } else if (d.status === "completed") {
        counts.completed += 1;
      } else if (d.status === "needs_manual_review") {
        counts.needs_manual_review += 1;
      } else if (d.status === "revoked") {
        counts.revoked += 1;
      }
      if (d.is_overdue_24h) counts.overdue_24h += 1;
    }

    const skus: AdminSkuStatus[] = (["ultra_shared_slot", "ultra_exclusive_admin"] as SkuKey[]).map(
      (key) => {
        const s = this.skus[key];
        return {
          ...s,
          price_exceeded: s.current_price_usd !== null && s.current_price_usd > s.max_cost_usd,
          unused_codes_taobao: unusedCounters[key].taobao,
          unused_codes_16688: unusedCounters[key]["16688"],
        };
      }
    );

    return {
      execution_mode: "dry_run",
      public_base_url: buildOrigin(),
      canboso: {
        balance_usd: this.balanceUsd,
        low_balance_threshold_usd: this.lowBalanceThresholdUsd,
        is_low_balance: this.balanceUsd < this.lowBalanceThresholdUsd,
        checked_at: this.checkedAt,
        error: null,
      },
      skus,
      counts,
    };
  }

  refreshUpstream(token: string): AdminOverviewResponse {
    this.requireToken(token);
    this.checkedAt = nowIso();
    return this.getOverview(token);
  }

  issueCode(token: string, req: AdminIssueCodeRequest): AdminIssueCodeResponse {
    this.requireToken(token);
    const prefix = req.sku_key === "ultra_exclusive_admin" ? "U20E" : "U20S";
    const code = `${prefix}-${randomSegment(4)}-${randomSegment(4)}`;
    const origin = buildOrigin();
    const redeemUrl = `${origin}/r/${code}`;
    const now = nowIso();
    const sku = this.skus[req.sku_key];

    const detail: AdminCodeDetail = {
      code,
      sku_key: req.sku_key,
      sku_title: sku.title,
      product_type: sku.product_type,
      channel: req.channel,
      goods_no: req.channel === "16688" ? sku.goods_no : null,
      external_order_id: req.external_order_id.trim() || null,
      remark: req.remark?.trim() || null,
      status: "unused",
      buyer_email: null,
      buyer_email_masked: null,
      store_email: null,
      upstream_order_code: null,
      upstream_status: null,
      cost_usd: null,
      warranty_status: null,
      last_error_code: null,
      last_error_message: null,
      is_overdue_24h: false,
      elapsed_minutes: null,
      created_at: now,
      updated_at: now,
      redeem_url: redeemUrl,
      batch_id: null,
      idempotency_key: null,
      delivery: null,
      warranty: null,
      audit_events: [
        {
          event_id: 1,
          from_status: null,
          to_status: "unused",
          reason: `商家单笔发码（${req.channel}）`,
          created_at: now,
        },
      ],
    };

    this.records.set(code, {
      detail,
      screenshotDataUrl: null,
      buyerNotice: null,
      submittedAt: null,
      completedAt: null,
      lastPolledAt: null,
      pollCount: 0,
    });

    return {
      code,
      redeem_url: redeemUrl,
      wangwang_message: buildWangwangMessage(req.sku_key, code, redeemUrl),
      item: this.toSummary(detail),
    };
  }

  batchCodes(token: string, req: AdminBatchCodeRequest): AdminBatchCodeResponse {
    this.requireToken(token);
    const count = Math.min(500, Math.max(1, Math.floor(req.count)));
    const prefix = req.sku_key === "ultra_exclusive_admin" ? "U20E" : "U20S";
    const batchId = `B${new Date().toISOString().replace(/\D/g, "").slice(0, 14)}`;
    const origin = buildOrigin();
    const now = nowIso();
    const sku = this.skus[req.sku_key];
    const codes: string[] = [];

    for (let idx = 0; idx < count; idx += 1) {
      const code = `${prefix}-${randomSegment(4)}-${randomSegment(4)}`;
      codes.push(code);
      const redeemUrl = `${origin}/r/${code}`;
      this.records.set(code, {
        detail: {
          code,
          sku_key: req.sku_key,
          sku_title: sku.title,
          product_type: sku.product_type,
          channel: req.channel,
          goods_no: req.goods_no?.trim() || (req.channel === "16688" ? sku.goods_no : null),
          external_order_id: null,
          remark: req.batch_label?.trim() || null,
          status: "unused",
          buyer_email: null,
          buyer_email_masked: null,
          store_email: null,
          upstream_order_code: null,
          upstream_status: null,
          cost_usd: null,
          warranty_status: null,
          last_error_code: null,
          last_error_message: null,
          is_overdue_24h: false,
          elapsed_minutes: null,
          created_at: now,
          updated_at: now,
          redeem_url: redeemUrl,
          batch_id: batchId,
          idempotency_key: null,
          delivery: null,
          warranty: null,
          audit_events: [
            {
              event_id: 1,
              from_status: null,
              to_status: "unused",
              reason: `批量制码 ${batchId}`,
              created_at: now,
            },
          ],
        },
        screenshotDataUrl: null,
        buyerNotice: null,
        submittedAt: null,
        completedAt: null,
        lastPolledAt: null,
        pollCount: 0,
      });
    }

    return {
      batch_id: batchId,
      sku_key: req.sku_key,
      channel: req.channel,
      count,
      codes,
      export_lines_code_only: codes.join("\n"),
      export_lines_with_url: codes
        .map((c) => `提货链接：${origin}/r/${c} | 兑换码：${c}`)
        .join("\n"),
    };
  }

  listCodes(
    token: string,
    params: {
      status?: string;
      channel?: string;
      sku_key?: string;
      q?: string;
      page?: number;
      page_size?: number;
    }
  ): AdminCodeListResponse {
    this.requireToken(token);
    const q = (params.q || "").trim().toLowerCase();
    const filtered = Array.from(this.records.values())
      .map((r) => r.detail)
      .filter((d) => {
        if (params.status && d.status !== params.status) return false;
        if (params.channel && d.channel !== params.channel) return false;
        if (params.sku_key && d.sku_key !== params.sku_key) return false;
        if (q) {
          const hay = [
            d.code,
            d.external_order_id || "",
            d.upstream_order_code || "",
            d.remark || "",
            d.buyer_email || "",
            d.buyer_email_masked || "",
            d.store_email || "",
            d.warranty?.admin_email || "",
            d.delivery?.account_text || "",
          ]
            .join(" ")
            .toLowerCase();
          if (!hay.includes(q)) return false;
        }
        return true;
      })
      .sort((a, b) => b.updated_at.localeCompare(a.updated_at));

    const page = Math.max(1, params.page || 1);
    const pageSize = Math.min(100, Math.max(1, params.page_size || 20));
    const start = (page - 1) * pageSize;
    return {
      items: filtered.slice(start, start + pageSize).map((d) => this.toSummary(d)),
      total: filtered.length,
      page,
      page_size: pageSize,
    };
  }

  getCodeDetail(token: string, code: string): AdminCodeDetail {
    this.requireToken(token);
    const rec = this.records.get(code.toUpperCase());
    if (!rec) {
      throw new MockApiError(404, "CODE_NOT_FOUND", "没找到这个兑换码");
    }
    return { ...rec.detail };
  }

  getWarrantyScreenshotUrl(token: string, code: string): string {
    this.requireToken(token);
    const rec = this.records.get(code.toUpperCase());
    if (!rec || !rec.screenshotDataUrl) {
      throw new MockApiError(404, "SCREENSHOT_NOT_FOUND", "没找到这张保修截图");
    }
    return rec.screenshotDataUrl;
  }

  pollCode(token: string, code: string): AdminCodeDetail {
    this.requireToken(token);
    const rec = this.records.get(code.toUpperCase());
    if (!rec) throw new MockApiError(404, "CODE_NOT_FOUND", "没找到这个兑换码");
    const now = nowIso();
    if (rec.detail.status === "waiting_fulfillment") {
      rec.detail.status = "completed";
      rec.detail.upstream_status = "completed";
      rec.detail.updated_at = now;
      rec.completedAt = now;
      rec.lastPolledAt = now;
      rec.detail.audit_events.push({
        event_id: rec.detail.audit_events.length + 1,
        from_status: "waiting_fulfillment",
        to_status: "completed",
        reason: "商家手动查了进度：上游已经完成拉人",
        created_at: now,
      });
    }
    return { ...rec.detail };
  }

  revokeCode(token: string, code: string, reason: string): AdminCodeDetail {
    this.requireToken(token);
    const rec = this.records.get(code.toUpperCase());
    if (!rec) throw new MockApiError(404, "CODE_NOT_FOUND", "没找到这个兑换码");
    if (rec.detail.status === "completed") {
      throw new MockApiError(409, "CANNOT_REVOKE_COMPLETED", "已经完成的订单不能作废");
    }
    const now = nowIso();
    const prev = rec.detail.status;
    rec.detail.status = "revoked";
    rec.detail.updated_at = now;
    rec.detail.audit_events.push({
      event_id: rec.detail.audit_events.length + 1,
      from_status: prev,
      to_status: "revoked",
      reason: reason.trim() || "商家手动作废",
      created_at: now,
    });
    return { ...rec.detail };
  }

  retryCode(
    token: string,
    code: string,
    payload: { override_price_limit?: boolean; note?: string }
  ): AdminCodeDetail {
    this.requireToken(token);
    const rec = this.records.get(code.toUpperCase());
    if (!rec) throw new MockApiError(404, "CODE_NOT_FOUND", "没找到这个兑换码");
    if (rec.detail.status !== "needs_manual_review") {
      throw new MockApiError(409, "INVALID_STATE", "只有人工处理中的订单才能点重试");
    }
    const now = nowIso();
    const isExclusive = rec.detail.product_type === "account";
    if (isExclusive) {
      const storeEmail = `store-ultra-${randomSegment(4).toLowerCase()}@example.com`;
      rec.detail.status = "delivered_pending_warranty";
      rec.detail.store_email = storeEmail;
      rec.detail.upstream_order_code = `CNB-ACC-${randomSegment(6)}`;
      rec.detail.upstream_status = "completed";
      rec.detail.warranty_status = "pending";
      rec.detail.delivery = {
        account_text: storeEmail,
        credentials: [{ label: "商店套餐邮箱", value: storeEmail }],
        raw_lines: [storeEmail],
        delivered_at: now,
      };
      rec.detail.warranty = {
        status: "pending",
        admin_email: null,
        submitted_at: null,
        registered_at: null,
        note: null,
        has_screenshot: false,
        screenshot_url: null,
        forward_template: null,
      };
    } else {
      rec.detail.status = "waiting_fulfillment";
      rec.detail.upstream_order_code = `CNB-RETRY-${randomSegment(5)}`;
      rec.detail.upstream_status = "paid";
    }
    rec.detail.cost_usd = this.skus[rec.detail.sku_key].current_price_usd;
    rec.detail.last_error_code = null;
    rec.detail.last_error_message = null;
    rec.detail.idempotency_key = rec.detail.idempotency_key || `idem_${randomSegment(12)}`;
    rec.detail.updated_at = now;
    rec.buyerNotice = null;
    rec.detail.audit_events.push({
      event_id: rec.detail.audit_events.length + 1,
      from_status: "needs_manual_review",
      to_status: rec.detail.status,
      reason: `演练模式重试成功${payload.override_price_limit ? "（本次跳过进价上限）" : ""}${
        payload.note ? `：${payload.note}` : ""
      }`,
      created_at: now,
    });
    return { ...rec.detail };
  }

  ackWarranty(token: string, code: string, note: string): AdminCodeDetail {
    this.requireToken(token);
    const rec = this.records.get(code.toUpperCase());
    if (!rec || !rec.detail.warranty) {
      throw new MockApiError(404, "WARRANTY_NOT_FOUND", "这笔订单还没有保修记录");
    }
    const now = nowIso();
    rec.detail.warranty.status = "registered_upstream";
    rec.detail.warranty.registered_at = now;
    rec.detail.warranty_status = "registered_upstream";
    if (note.trim()) {
      rec.detail.warranty.note = note.trim();
    }
    rec.detail.updated_at = now;
    rec.detail.audit_events.push({
      event_id: rec.detail.audit_events.length + 1,
      from_status: rec.detail.status,
      to_status: rec.detail.status,
      reason: note.trim() || "商家已向官方完成保修报备",
      created_at: now,
    });
    return { ...rec.detail };
  }

  updateSku(token: string, skuKey: SkuKey, payload: AdminUpdateSkuRequest): AdminSkuStatus {
    this.requireToken(token);
    const sku = this.skus[skuKey];
    if (!sku) throw new MockApiError(404, "SKU_NOT_FOUND", "没找到这个商品规格");
    if (typeof payload.max_cost_usd === "number" && payload.max_cost_usd > 0) {
      sku.max_cost_usd = payload.max_cost_usd;
    }
    if (payload.goods_no !== undefined) {
      sku.goods_no = payload.goods_no ? payload.goods_no.trim() : null;
    }
    sku.price_exceeded = sku.current_price_usd !== null && sku.current_price_usd > sku.max_cost_usd;
    return { ...sku };
  }

  sendTestEmail(token: string): AdminTestEmailResponse {
    this.requireToken(token);
    return {
      ok: true,
      message: "演练模式：已模拟发了一封测试提醒邮件到 merchant***@example.com",
    };
  }
}

export const demoMockStore = new DemoMockStore();
