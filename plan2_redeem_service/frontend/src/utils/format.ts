import type { AuditEventItem, Channel, CodeStatus, SkuKey, WarrantyStatus } from "../types/api";

export type Tone = "neutral" | "info" | "warning" | "success" | "danger";

export interface StatusMeta {
  label: string;
  tone: Tone;
}

export function getCodeStatusMeta(status: CodeStatus): StatusMeta {
  switch (status) {
    case "unused":
      return { label: "待提货", tone: "neutral" };
    case "validating":
      return { label: "核对中", tone: "info" };
    case "purchasing":
      return { label: "正在下单", tone: "info" };
    case "waiting_fulfillment":
      return { label: "排队拉人中", tone: "warning" };
    case "delivered_pending_warranty":
      return { label: "已发商店邮箱 · 待交保修", tone: "info" };
    case "completed":
      return { label: "已完成", tone: "success" };
    case "needs_manual_review":
      return { label: "需人工处理", tone: "danger" };
    case "revoked":
      return { label: "已作废", tone: "neutral" };
  }
}

export function getWarrantyStatusMeta(status: WarrantyStatus | null): StatusMeta {
  switch (status) {
    case "pending":
      return { label: "待交保修", tone: "warning" };
    case "submitted":
      return { label: "已交保修 · 待你报备", tone: "warning" };
    case "registered_upstream":
      return { label: "已向官方报备", tone: "success" };
    default:
      return { label: "-", tone: "neutral" };
  }
}

export function getOrderDisplayStatusMeta(item: {
  sku_key: SkuKey;
  status: CodeStatus;
  warranty_status?: WarrantyStatus | null;
}): StatusMeta {
  if (item.sku_key === "ultra_exclusive_admin") {
    if (item.status === "unused") {
      return { label: "待领商店邮箱", tone: "neutral" };
    }
    if (item.status === "validating" || item.status === "purchasing") {
      return { label: "正在出货", tone: "info" };
    }
    if (item.status === "delivered_pending_warranty") {
      return { label: "已发商店邮箱 · 待交保修", tone: "info" };
    }
    if (item.status === "completed") {
      if (item.warranty_status === "registered_upstream") {
        return { label: "已完成 · 已报备", tone: "success" };
      }
      if (item.warranty_status === "submitted") {
        return { label: "买家已交保修 · 待你报备", tone: "warning" };
      }
      return { label: "已完成", tone: "success" };
    }
  }
  return getCodeStatusMeta(item.status);
}

export function getUpstreamStatusLabel(status: string | null | undefined): string {
  if (!status) return "未下单";
  switch (status.trim().toLowerCase()) {
    case "paid":
      return "上游已收款";
    case "waiting_seller":
    case "pending":
    case "processing":
      return "等上游拉人";
    case "delivered":
      return "上游已发货";
    case "completed":
      return "上游已完成";
    case "cancelled":
    case "canceled":
      return "上游已取消";
    case "refunded":
      return "上游已退款";
    default:
      return status;
  }
}

const STATUS_LABELS: Record<string, string> = {
  unused: "待提货",
  validating: "核对中",
  purchasing: "正在下单",
  waiting_fulfillment: "排队拉人中",
  delivered_pending_warranty: "已发商店邮箱 · 待交保修",
  completed: "已完成",
  needs_manual_review: "需人工处理",
  revoked: "已作废",
};

export function getRawStatusLabel(status: string | null | undefined): string {
  if (!status) return "";
  return STATUS_LABELS[status] || status;
}

function translateAuditReason(rawReason: string): { title?: string; detail: string } {
  const reason = (rawReason || "").trim();
  if (!reason) return { detail: "状态已更新" };

  if (reason.startsWith("admin_retry:")) {
    const note = reason.slice("admin_retry:".length).trim();
    return { title: "商家手动重试", detail: note ? `重新向上游下单（备注：${note}）` : "重新向上游下单" };
  }
  if (reason.startsWith("admin_retry_failed:")) {
    const sub = reason.slice("admin_retry_failed:".length).trim();
    const mapped = translateAuditReason(sub).detail;
    return { title: "重试没通过", detail: `重新下单被拦下：${mapped}` };
  }

  switch (reason) {
    case "issued":
      return { title: "生成兑换码", detail: "已建好兑换码，等买家打开链接提货" };
    case "redeem_claimed":
      return { title: "买家发起提货", detail: "买家点了确认提货，系统开始向上游下单" };
    case "exclusive_delivered":
      return { title: "发出商店邮箱", detail: "上游已秒发商店套餐邮箱，等买家建家庭组并交保修" };
    case "shared_order_accepted":
      return { title: "上游已接单", detail: "开始排队拉买家进家庭组（一般 1~6 小时）" };
    case "shared_poll":
      return { title: "刷新过一次进度", detail: "上游还在排队拉人中，继续等就行" };
    case "shared_fulfillment_completed":
      return { title: "拉人完成", detail: "上游确认已发出家庭组邀请，这单完成了" };
    case "warranty_submitted":
      return { title: "买家交了保修", detail: "已收到买家主力 Gmail 和家庭组邀请截图" };
    case "warranty_ack":
      return { title: "已向官方报备", detail: "你已把买家保修信息发给上游并标记完成" };
    case "16688_goods_missing":
      return { title: "缺 16688 商品号", detail: "还没填 16688 商品编号，已退回待提货" };
    case "16688_unavailable":
      return { title: "16688 连不上", detail: "校验订单时 16688 接口异常，先转人工处理" };
    case "16688_order_not_eligible":
      return { title: "订单不能提货", detail: "16688 订单还没付款、已退款或有退款投诉，已拦下" };
    case "PRICE_LIMIT_EXCEEDED":
      return { title: "进价超上限了", detail: "上游涨价超过你设的最高成本，已自动暂停下单" };
    case "LOW_BALANCE":
    case "INSUFFICIENT_BALANCE":
      return { title: "上游余额不够", detail: "Canboso 账户余额不足，请充值后再点重试" };
    case "OUT_OF_STOCK":
    case "UPSTREAM_OUT_OF_STOCK":
      return { title: "上游暂时没货", detail: "Canboso 当前断货，已转人工等补货" };
    case "upstream_timeout":
    case "UPSTREAM_TIMEOUT":
      return { title: "上游响应超时", detail: "下单时没收到回执，请先去上游核实别重复扣款" };
    case "upstream_error":
    case "UPSTREAM_ERROR":
      return { title: "上游接口报错", detail: "调上游接口出错，已转人工核查" };
    case "poll_upstream_error":
      return { title: "查进度暂时失败", detail: "本次查上游出错，稍后会自动再查" };
    case "fulfillment_over_24h":
    case "FULFILLMENT_TIMEOUT":
      return { title: "超 24 小时未完成", detail: "排队超 24 小时还没拉人，已转人工跟进" };
    case "admin_retry":
      return { title: "商家手动重试", detail: "重新向上游发起下单" };
    default:
      return { detail: reason };
  }
}

export function formatAuditEvent(event: AuditEventItem): { title: string; detail: string } {
  const translated = translateAuditReason(event.reason);
  if (translated.title) {
    return { title: translated.title, detail: translated.detail };
  }
  if (event.to_status === "revoked") {
    return { title: "已作废兑换码", detail: translated.detail };
  }
  const fromLabel = getRawStatusLabel(event.from_status);
  const toLabel = getRawStatusLabel(event.to_status);
  if (!fromLabel || fromLabel === toLabel) {
    return { title: toLabel || "状态更新", detail: translated.detail };
  }
  return { title: `${fromLabel} → ${toLabel}`, detail: translated.detail };
}

export function formatOrderErrorMessage(
  errorCode: string | null | undefined,
  errorMessage: string | null | undefined
): string {
  const message = (errorMessage || "").trim();
  const code = (errorCode || "").trim();
  if (message) {
    const translated = translateAuditReason(message);
    if (translated.title) {
      return translated.detail;
    }
    return message;
  }
  if (code) {
    const translated = translateAuditReason(code);
    if (translated.title) {
      return translated.detail;
    }
  }
  return "这笔订单遇到问题暂停了，请核对后再处理";
}

export function getChannelLabel(channel: Channel): string {
  return channel === "16688" ? "16688" : "淘宝";
}

export function getSkuShortLabel(skuKey: SkuKey): string {
  return skuKey === "ultra_exclusive_admin" ? "独享整组" : "共享车位";
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "-";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours()
  )}:${pad(date.getMinutes())}`;
}

export function formatElapsedMinutes(minutes: number | null | undefined): string {
  if (minutes === null || minutes === undefined || minutes < 0) return "-";
  if (minutes < 1) return "不到 1 分钟";
  if (minutes < 60) return `${Math.floor(minutes)} 分钟`;
  const hours = Math.floor(minutes / 60);
  const remain = Math.floor(minutes % 60);
  return remain > 0 ? `${hours} 小时 ${remain} 分钟` : `${hours} 小时`;
}

export function formatUsd(amount: number | null | undefined): string {
  if (amount === null || amount === undefined || Number.isNaN(amount)) return "-";
  return `$${amount.toFixed(2)}`;
}

export async function copyToClipboard(text: string): Promise<boolean> {
  if (!text) return false;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // Fallback below for mobile WebViews or HTTP local preview
  }

  try {
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.setAttribute("readonly", "true");
    textarea.style.position = "fixed";
    textarea.style.left = "-9999px";
    textarea.style.top = "0";
    document.body.appendChild(textarea);
    textarea.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(textarea);
    return ok;
  } catch {
    return false;
  }
}

export function downloadTextFile(filename: string, content: string): void {
  const blob = new Blob([content], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  URL.revokeObjectURL(url);
}
