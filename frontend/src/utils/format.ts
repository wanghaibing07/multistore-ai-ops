import type { Channel, CodeStatus, SkuKey, WarrantyStatus } from "../types/api";

export type Tone = "neutral" | "info" | "warning" | "success" | "danger";

export interface StatusMeta {
  label: string;
  tone: Tone;
}

export function getCodeStatusMeta(status: CodeStatus): StatusMeta {
  switch (status) {
    case "unused":
      return { label: "待核销", tone: "neutral" };
    case "validating":
      return { label: "校验中", tone: "info" };
    case "purchasing":
      return { label: "提交处理中", tone: "info" };
    case "waiting_fulfillment":
      return { label: "排单拉人中", tone: "warning" };
    case "delivered_pending_warranty":
      return { label: "已发号·待登记保修", tone: "info" };
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
      return { label: "待提交保修", tone: "warning" };
    case "submitted":
      return { label: "已提交·待报备", tone: "info" };
    case "registered_upstream":
      return { label: "已向官方报备", tone: "success" };
    default:
      return { label: "-", tone: "neutral" };
  }
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
