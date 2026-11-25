import type { CodeStatus, SkuKey, WarrantyStatus } from "../types/api";
import {
  getCodeStatusMeta,
  getOrderDisplayStatusMeta,
  getWarrantyStatusMeta,
  type Tone,
} from "../utils/format";

export function ToneBadge({
  label,
  tone,
  pulse = false,
  shimmer = false,
}: {
  label: string;
  tone: Tone;
  pulse?: boolean;
  shimmer?: boolean;
}) {
  return (
    <span className={`badge badge-${tone}${shimmer ? " badge-shimmer" : ""}`}>
      <span className={pulse ? "badge-dot badge-dot-pulse" : "badge-dot"} aria-hidden="true" />
      <span>{label}</span>
    </span>
  );
}

export function CodeStatusBadge({ status }: { status: CodeStatus }) {
  const meta = getCodeStatusMeta(status);
  const pulse =
    status === "waiting_fulfillment" || status === "validating" || status === "purchasing";
  return <ToneBadge label={meta.label} tone={meta.tone} pulse={pulse} />;
}

export function WarrantyStatusBadge({ status }: { status: WarrantyStatus | null }) {
  const meta = getWarrantyStatusMeta(status);
  return <ToneBadge label={meta.label} tone={meta.tone} />;
}

export function OrderDisplayStatusBadge({
  skuKey,
  status,
  warrantyStatus,
}: {
  skuKey: SkuKey;
  status: CodeStatus;
  warrantyStatus?: WarrantyStatus | null;
}) {
  const meta = getOrderDisplayStatusMeta({
    sku_key: skuKey,
    status,
    warranty_status: warrantyStatus,
  });
  const pulse = status === "waiting_fulfillment" || status === "validating" || status === "purchasing";
  return <ToneBadge label={meta.label} tone={meta.tone} pulse={pulse} />;
}
