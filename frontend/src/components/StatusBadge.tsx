import type { CodeStatus, WarrantyStatus } from "../types/api";
import { getCodeStatusMeta, getWarrantyStatusMeta, type Tone } from "../utils/format";

export function ToneBadge({
  label,
  tone,
  pulse = false,
}: {
  label: string;
  tone: Tone;
  pulse?: boolean;
}) {
  return (
    <span className={`badge badge-${tone}`}>
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
