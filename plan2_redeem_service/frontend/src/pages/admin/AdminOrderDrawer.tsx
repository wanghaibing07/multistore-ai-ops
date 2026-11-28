import { useCallback, useEffect, useId, useRef, useState } from "react";
import {
  ackAdminWarranty,
  ApiError,
  fetchAdminWarrantyScreenshot,
  forwardAdminWarranty,
  getAdminCodeDetail,
  markAdminExclusiveActivated,
  pollAdminCode,
  recordAdminUpstreamRefund,
  resetAdminCodeToUnused,
  retryAdminCode,
  revokeAdminCode,
} from "../../api/client";
import { CastoriceIcon } from "../../components/CastoriceIcon";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { CopyButton } from "../../components/CopyButton";
import { OrderDisplayStatusBadge, WarrantyStatusBadge } from "../../components/StatusBadge";
import { useToast } from "../../components/ToastProvider";
import type { AdminCodeDetail } from "../../types/api";
import {
  formatAuditEvent,
  formatDateTime,
  formatElapsedMinutes,
  formatOrderErrorMessage,
  formatUsd,
  getChannelLabel,
  getOrderDisplayStatusMeta,
  getSkuShortLabel,
  getUpstreamStatusLabel,
} from "../../utils/format";
import { validateGmailInput } from "../../utils/validation";

function buildDisputeEvidenceSummary(detail: AdminCodeDetail): string {
  const isExclusive = detail.sku_key === "ultra_exclusive_admin";
  const statusLabel = getOrderDisplayStatusMeta({
    sku_key: detail.sku_key,
    status: detail.status,
    warranty_status: detail.warranty_status,
    external_order_id: detail.external_order_id,
    is_dispatched: detail.is_dispatched,
  }).label;
  const buyerEmail =
    (isExclusive ? detail.warranty?.admin_email : null) ||
    detail.buyer_email ||
    detail.buyer_email_masked ||
    "未填写";
  const storeEmail = detail.store_email || detail.delivery?.account_text?.trim() || "";
  const lines: string[] = [
    "【售后核验举证摘要】",
    `兑换码：${detail.code}`,
    `销售渠道：${getChannelLabel(detail.channel)}${detail.goods_no ? `（商品编号 ${detail.goods_no}）` : ""}`,
    `外部订单号：${detail.external_order_id || "未绑定"}`,
    `商品规格：${detail.sku_title || getSkuShortLabel(detail.sku_key)}`,
    `当前状态：${statusLabel}`,
    `买家邮箱：${buyerEmail}`,
  ];
  if (isExclusive && storeEmail) {
    lines.push(`商店套餐邮箱：${storeEmail}`);
  }
  lines.push(
    `上游单号：${detail.upstream_order_code || "未下单"}（上游状态：${getUpstreamStatusLabel(detail.upstream_status)}）`
  );
  lines.push(
    `采购成本：${formatUsd(detail.cost_usd)}${
      detail.upstream_refund_usd !== null && detail.upstream_refund_usd !== undefined
        ? ` ｜ 上游实退：${formatUsd(detail.upstream_refund_usd)}`
        : ""
    }`
  );
  lines.push(`制码时间：${formatDateTime(detail.created_at)}`);
  lines.push(`提货提交时间：${detail.submitted_at ? formatDateTime(detail.submitted_at) : "尚未使用"}`);
  if (detail.completed_at) {
    lines.push(`完成时间：${formatDateTime(detail.completed_at)}`);
  }
  if (detail.warranty) {
    lines.push(
      `质保登记：${detail.warranty.status}${
        detail.warranty.registered_at ? `（报备于 ${formatDateTime(detail.warranty.registered_at)}）` : ""
      }`
    );
    if (detail.warranty.activated_at) {
      lines.push(`独享进组生效：${formatDateTime(detail.warranty.activated_at)}`);
    }
  }
  if (detail.last_error_code || detail.last_error_message) {
    lines.push(
      `异常记录：${detail.last_error_code || "UNKNOWN"} - ${formatOrderErrorMessage(
        detail.last_error_code,
        detail.last_error_message
      )}`
    );
  }
  if (detail.audit_events.length > 0) {
    lines.push("关键节点：");
    for (const item of detail.audit_events.slice(-6)) {
      const ev = formatAuditEvent(item);
      lines.push(`- ${formatDateTime(item.created_at)} ${ev.title}${ev.detail && ev.detail !== ev.title ? `（${ev.detail}）` : ""}`);
    }
  }
  return lines.join("\n");
}

interface AdminOrderDrawerProps {
  token: string;
  code: string | null;
  onClose: () => void;
  onUnauthorized: () => void;
  onMutated: () => void;
}

export function AdminOrderDrawer({
  token,
  code,
  onClose,
  onUnauthorized,
  onMutated,
}: AdminOrderDrawerProps) {
  const { pushToast } = useToast();
  const titleId = useId();

  const [detail, setDetail] = useState<AdminCodeDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Screenshot blob state
  const [screenshotUrl, setScreenshotUrl] = useState<string | null>(null);
  const [screenshotLoading, setScreenshotLoading] = useState(false);
  const [screenshotError, setScreenshotError] = useState<string | null>(null);
  const [screenshotZoomOpen, setScreenshotZoomOpen] = useState(false);

  // Mutation states
  const [polling, setPolling] = useState(false);
  const [ackNote, setAckNote] = useState("");
  const [acking, setAcking] = useState(false);
  const [forwarding, setForwarding] = useState(false);
  const [togglingActivation, setTogglingActivation] = useState(false);
  const [activationNote, setActivationNote] = useState("");

  const [overridePriceLimit, setOverridePriceLimit] = useState(false);
  const [forceNewOrder, setForceNewOrder] = useState(false);
  const [newBuyerEmail, setNewBuyerEmail] = useState("");
  const [retryNote, setRetryNote] = useState("");
  const [merchantVerifiedOrderNo, setMerchantVerifiedOrderNo] = useState("");
  const [merchantVerifiedRecipientEmail, setMerchantVerifiedRecipientEmail] = useState("");
  const [retryConfirmOpen, setRetryConfirmOpen] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [resetNote, setResetNote] = useState("");
  const [resetConfirmOpen, setResetConfirmOpen] = useState(false);
  const [resetting, setResetting] = useState(false);

  const [revokeReason, setRevokeReason] = useState("买家退款或手动作废");
  const [revokeConfirmOpen, setRevokeConfirmOpen] = useState(false);
  const [revoking, setRevoking] = useState(false);
  const [revokeForceRequired, setRevokeForceRequired] = useState(false);
  const [revokeForceConfirmed, setRevokeForceConfirmed] = useState(false);
  const [revokeForceMessage, setRevokeForceMessage] = useState<string | null>(null);

  const [refundAmountInput, setRefundAmountInput] = useState("");
  const [refundNoteInput, setRefundNoteInput] = useState("");
  const [savingRefund, setSavingRefund] = useState(false);
  const activeCodeRef = useRef(code);
  activeCodeRef.current = code;

  const loadDetail = useCallback(async () => {
    if (!code) return;
    setLoading(true);
    setError(null);
    try {
      const loaded = await getAdminCodeDetail(token, code);
      setDetail(loaded);
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401) {
        onUnauthorized();
        return;
      }
      setError(caught instanceof ApiError ? caught.message : "加载订单详情失败");
    } finally {
      setLoading(false);
    }
  }, [token, code, onUnauthorized]);

  useEffect(() => {
    setDetail(null);
    setError(null);
    setScreenshotZoomOpen(false);
    setOverridePriceLimit(false);
    setForceNewOrder(false);
    setNewBuyerEmail("");
    setRetryNote("");
    setMerchantVerifiedOrderNo("");
    setMerchantVerifiedRecipientEmail("");
    setRetryConfirmOpen(false);
    setResetNote("");
    setResetConfirmOpen(false);
    setAckNote("");
    setActivationNote("");
    setRevokeReason("买家退款或手动作废");
    setRevokeConfirmOpen(false);
    setRevokeForceRequired(false);
    setRevokeForceConfirmed(false);
    setRevokeForceMessage(null);
    setRefundAmountInput("");
    setRefundNoteInput("");
    if (code) {
      void loadDetail();
    }
  }, [code, loadDetail]);

  useEffect(() => {
    if (!detail) return;
    setRefundAmountInput(
      detail.upstream_refund_usd !== null && detail.upstream_refund_usd !== undefined
        ? String(detail.upstream_refund_usd)
        : ""
    );
    setRefundNoteInput(detail.upstream_refund_note ?? "");
  }, [detail?.code, detail?.upstream_refund_usd, detail?.upstream_refund_note]);

  useEffect(() => {
    if (!code || !detail?.warranty?.has_screenshot) {
      setScreenshotUrl(null);
      setScreenshotError(null);
      setScreenshotZoomOpen(false);
      return;
    }
    let cancelled = false;
    let revokeObjectUrl: (() => void) | null = null;

    const loadShot = async () => {
      setScreenshotLoading(true);
      setScreenshotError(null);
      try {
        const result = await fetchAdminWarrantyScreenshot(token, code);
        if (cancelled) {
          result.revoke();
          return;
        }
        revokeObjectUrl = result.revoke;
        setScreenshotUrl(result.url);
      } catch (caught) {
        if (cancelled) return;
        if (caught instanceof ApiError && caught.status === 401) {
          onUnauthorized();
          return;
        }
        setScreenshotError(caught instanceof ApiError ? caught.message : "加载历史保修截图失败");
      } finally {
        if (!cancelled) {
          setScreenshotLoading(false);
        }
      }
    };

    void loadShot();
    return () => {
      cancelled = true;
      if (revokeObjectUrl) {
        revokeObjectUrl();
      }
    };
  }, [token, code, detail?.warranty?.has_screenshot, onUnauthorized]);

  useEffect(() => {
    if (!code) return;
    const handleKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key !== "Escape") return;
      if (screenshotZoomOpen) {
        setScreenshotZoomOpen(false);
        return;
      }
      if (!retryConfirmOpen && !revokeConfirmOpen) {
        onClose();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [code, onClose, retryConfirmOpen, revokeConfirmOpen, screenshotZoomOpen]);

  if (!code) {
    return null;
  }

  const handlePoll = async () => {
    setPolling(true);
    try {
      const updated = await pollAdminCode(token, code);
      setDetail(updated);
      onMutated();
      pushToast("已刷新上游最新进度", "success");
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401) {
        onUnauthorized();
        return;
      }
      pushToast(caught instanceof ApiError ? caught.message : "查上游进度失败，请稍后重试", "error");
    } finally {
      setPolling(false);
    }
  };

  const handleAckWarranty = async () => {
    setAcking(true);
    try {
      const updated = await ackAdminWarranty(token, code, ackNote.trim());
      setDetail(updated);
      setAckNote("");
      onMutated();
      pushToast("已标记发给官方报备", "success");
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401) {
        onUnauthorized();
        return;
      }
      pushToast(caught instanceof ApiError ? caught.message : "标记保修报备失败", "error");
    } finally {
      setAcking(false);
    }
  };

  const handleForwardWarranty = async () => {
    setForwarding(true);
    try {
      const updated = await forwardAdminWarranty(token, code);
      setDetail(updated);
      onMutated();
      if (
        updated.warranty?.status === "registered_upstream" ||
        updated.warranty?.tg_forward_status === "sent"
      ) {
        pushToast("已通过 Telegram 完成官方报备", "success");
      } else if (updated.warranty?.tg_forward_status === "failed") {
        pushToast(
          updated.warranty.tg_error
            ? `Telegram 自动报备失败：${updated.warranty.tg_error}`
            : "Telegram 自动报备失败，请查看失败原因",
          "error"
        );
      } else {
        pushToast("已重新发起 Telegram 报备", "success");
      }
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401) {
        onUnauthorized();
        return;
      }
      pushToast(caught instanceof ApiError ? caught.message : "重发 TG 报备失败", "error");
      try {
        const refreshed = await getAdminCodeDetail(token, code);
        setDetail(refreshed);
        onMutated();
      } catch (refreshError) {
        if (refreshError instanceof ApiError && refreshError.status === 401) {
          onUnauthorized();
          return;
        }
      }
    } finally {
      setForwarding(false);
    }
  };

  const handleToggleExclusiveActivated = async (activated: boolean) => {
    const targetCode = code;
    setTogglingActivation(true);
    try {
      const updated = await markAdminExclusiveActivated(
        token,
        targetCode,
        activated,
        activationNote.trim()
      );
      if (activeCodeRef.current === targetCode) {
        setDetail(updated);
        setActivationNote("");
      }
      onMutated();
      pushToast(
        activated
          ? "已标记为进组生效，买家页将退出等待状态"
          : "已撤销进组生效标记，买家页将重新显示等待引导",
        "success"
      );
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401) {
        onUnauthorized();
        return;
      }
      pushToast(
        caught instanceof ApiError ? caught.message : "更新独享进组生效状态失败",
        "error"
      );
    } finally {
      if (activeCodeRef.current === targetCode) {
        setTogglingActivation(false);
      }
    }
  };

  const handleRetryConfirm = async () => {
    if (retrying || !canRetry || !retryRecipientReady || detail?.code !== code) return;
    setRetrying(true);
    try {
      const updated = await retryAdminCode(token, code, {
        override_price_limit: overridePriceLimit,
        note: retryNote.trim() || undefined,
        merchant_verified_order_no: merchantVerifiedOrderNo.trim() || undefined,
        merchant_verified_recipient_email: needsRecipientConfirmation ? verifiedRecipient : undefined,
        force_new_order: effectiveForceNewOrder || undefined,
        new_buyer_email:
          canEditNewBuyerEmail && newBuyerEmailValidation?.valid
            ? newBuyerEmailValidation.normalized
            : undefined,
      });
      setDetail(updated);
      setRetryConfirmOpen(false);
      setOverridePriceLimit(false);
      setForceNewOrder(false);
      setNewBuyerEmail("");
      setRetryNote("");
      setMerchantVerifiedOrderNo("");
      setMerchantVerifiedRecipientEmail("");
      onMutated();
      if (updated.upstream_order_code) {
        pushToast("已取得上游订单号", "success");
      }
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401) {
        onUnauthorized();
        return;
      }
      if (caught instanceof ApiError && caught.errorCode === "MERCHANT_RECIPIENT_CONFIRMATION_REQUIRED") {
        setRetryConfirmOpen(false);
        pushToast(caught.message, "error");
        return;
      }
      void loadDetail();
      pushToast(caught instanceof ApiError ? caught.message : "重新下单失败，请稍后排查", "error");
    } finally {
      setRetrying(false);
    }
  };

  const handleResetUnusedConfirm = async () => {
    if (resetting || !canResetUnused || detail?.code !== code) return;
    setResetting(true);
    try {
      const updated = await resetAdminCodeToUnused(token, code, resetNote.trim());
      setDetail(updated);
      setResetConfirmOpen(false);
      setResetNote("");
      onMutated();
      pushToast(`兑换码 ${code} 已退回待提货，买家可用原码重新提交`, "success");
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401) {
        onUnauthorized();
        return;
      }
      setResetConfirmOpen(false);
      void loadDetail();
      pushToast(caught instanceof ApiError ? caught.message : "退回待提货失败", "error");
    } finally {
      setResetting(false);
    }
  };

  const closeRevokeDialog = () => {
    setRevokeConfirmOpen(false);
    setRevokeForceRequired(false);
    setRevokeForceConfirmed(false);
    setRevokeForceMessage(null);
  };

  const handleRevokeConfirm = async () => {
    if (revokeForceRequired && !revokeForceConfirmed) {
      return;
    }
    setRevoking(true);
    try {
      const updated = await revokeAdminCode(
        token,
        code,
        revokeReason.trim() || "商家手动作废",
        revokeForceRequired && revokeForceConfirmed
      );
      setDetail(updated);
      closeRevokeDialog();
      onMutated();
      pushToast(`兑换码 ${code} 已作废`, "success");
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401) {
        onUnauthorized();
        return;
      }
      if (caught instanceof ApiError && caught.status === 409 && caught.errorCode === "UPSTREAM_ORDER_ACTIVE") {
        setRevokeForceRequired(true);
        setRevokeForceConfirmed(false);
        setRevokeForceMessage(caught.message);
        pushToast("上游订单仍活跃，如需强制本地作废请勾选二次确认", "error");
        return;
      }
      pushToast(caught instanceof ApiError ? caught.message : "作废操作失败", "error");
    } finally {
      setRevoking(false);
    }
  };

  const handleSaveUpstreamRefund = async () => {
    const rawText = refundAmountInput.trim();
    if (!rawText) {
      pushToast("请填写上游实退金额（USD），无退款可填 0", "error");
      return;
    }
    const parsedAmount = Number(rawText);
    if (!Number.isFinite(parsedAmount) || parsedAmount < 0) {
      pushToast("实退金额必须是大于等于 0 的有效数字", "error");
      return;
    }
    setSavingRefund(true);
    try {
      const updated = await recordAdminUpstreamRefund(token, code, {
        upstream_refund_usd: parsedAmount,
        note: refundNoteInput.trim(),
      });
      setDetail(updated);
      onMutated();
      pushToast(`已登记上游实退 ${formatUsd(parsedAmount)}`, "success");
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401) {
        onUnauthorized();
        return;
      }
      pushToast(caught instanceof ApiError ? caught.message : "保存上游实退登记失败", "error");
    } finally {
      setSavingRefund(false);
    }
  };

  const isExclusive = detail?.sku_key === "ultra_exclusive_admin";
  const exclusiveBuyerEmail =
    detail?.warranty?.admin_email || detail?.buyer_email || detail?.buyer_email_masked || null;
  const exclusiveBuyerCopyText = detail?.warranty?.admin_email || detail?.buyer_email || null;
  const exclusiveStoreEmail = detail?.store_email || detail?.delivery?.account_text?.trim() || null;
  const sharedBuyerEmail = detail?.buyer_email || detail?.buyer_email_masked || null;
  const sharedBuyerCopyText = detail?.buyer_email || null;

  const canPoll = Boolean(
    detail &&
      (detail.can_poll_upstream ??
        ((detail.status === "waiting_fulfillment" || detail.status === "needs_manual_review") &&
          Boolean(detail.upstream_order_code)))
  );
  const canIdempotentRetry = Boolean(
    detail &&
      detail.status === "needs_manual_review" &&
      detail.idempotency_key &&
      !detail.upstream_order_code
  );
  const canReorder = Boolean(detail?.can_reorder);
  const canResetUnused = Boolean(detail?.can_reset_unused);
  const canRetry = canIdempotentRetry || canReorder;
  const forceNewOrderRequired = canReorder && !canIdempotentRetry;
  const effectiveForceNewOrder = forceNewOrderRequired || (canReorder && forceNewOrder);
  const canEditNewBuyerEmail = Boolean(
    detail && effectiveForceNewOrder && detail.product_type === "slot"
  );
  const trimmedNewBuyerEmail = newBuyerEmail.trim();
  const newBuyerEmailValidation =
    canEditNewBuyerEmail && trimmedNewBuyerEmail ? validateGmailInput(trimmedNewBuyerEmail) : null;
  const newBuyerEmailReady = !newBuyerEmailValidation || newBuyerEmailValidation.valid;
  const needsRecipientConfirmation = detail?.channel === "16688" && detail.product_type === "slot";
  const verifiedRecipient = merchantVerifiedRecipientEmail.trim();
  const retryRecipientReady =
    newBuyerEmailReady && (!needsRecipientConfirmation || Boolean(verifiedRecipient));
  const showActiveUpstreamGuard = Boolean(
    detail &&
      detail.status === "needs_manual_review" &&
      detail.upstream_order_code &&
      !canReorder
  );
  const canRevoke = Boolean(
    detail && detail.status !== "completed" && detail.status !== "revoked" && detail.status !== "purchasing"
  );
  const canRecordRefund = Boolean(detail && detail.upstream_order_code);
  const netLossUsd =
    detail && detail.cost_usd !== null && detail.cost_usd !== undefined
      ? Math.max(0, detail.cost_usd - (detail.upstream_refund_usd ?? 0))
      : null;
  const hasActions = canPoll || canResetUnused || canRetry || canRevoke || showActiveUpstreamGuard;

  return (
    <>
      <div
        className="drawer-backdrop"
        role="presentation"
        onClick={(event) => {
          if (event.target === event.currentTarget) {
            onClose();
          }
        }}
      >
        <aside
          className="drawer-panel"
          role="dialog"
          aria-modal="true"
          aria-labelledby={titleId}
        >
          <header className="drawer-header">
            <div className="row" style={{ gap: 8, minWidth: 0, flexWrap: "wrap" }}>
              <h2 id={titleId} className="card-title mono" style={{ fontSize: 16 }}>
                {code}
              </h2>
              {detail && (
                <>
                  <span
                    className={`sku-tag ${
                      isExclusive ? "sku-tag-exclusive" : "sku-tag-shared"
                    }`}
                  >
                    {getSkuShortLabel(detail.sku_key)}
                  </span>
                  <OrderDisplayStatusBadge
                    skuKey={detail.sku_key}
                    status={detail.status}
                    warrantyStatus={detail.warranty_status}
                    externalOrderId={detail.external_order_id}
                    isDispatched={detail.is_dispatched}
                  />
                </>
              )}
            </div>
            <button
              type="button"
              className="btn btn-ghost btn-icon"
              aria-label="关闭订单详情"
              onClick={onClose}
            >
              <CastoriceIcon name="close" size={19} />
            </button>
          </header>

          <div className="drawer-body">
            {loading && !detail ? (
              <div className="stack">
                <div className="skeleton" style={{ height: 22, width: "50%" }} />
                <div className="skeleton" style={{ height: 110, width: "100%" }} />
                <div className="skeleton" style={{ height: 140, width: "100%" }} />
              </div>
            ) : error ? (
              <div className="callout callout-danger" role="alert">
                <CastoriceIcon name="alert" size={20} className="callout-icon" />
                <div className="stack-sm">
                  <span>{error}</span>
                  <button type="button" className="btn btn-secondary btn-sm" onClick={() => void loadDetail()}>
                    重新加载
                  </button>
                </div>
              </div>
            ) : detail ? (
              <>
                {(detail.last_error_code || detail.last_error_message) && (
                  <div className="callout callout-danger" role="alert">
                    <CastoriceIcon name="admin-alert" size={24} className="callout-icon" />
                    <div>
                      <strong>异常提醒：</strong>
                      {formatOrderErrorMessage(detail.last_error_code, detail.last_error_message)}
                    </div>
                  </div>
                )}

                <section className="stack-sm" aria-label="订单基础信息">
                  <div className="row-between" style={{ flexWrap: "wrap", gap: 8 }}>
                    <strong style={{ fontSize: 14 }}>基础信息</strong>
                    <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
                      <CopyButton
                        text={buildDisputeEvidenceSummary(detail)}
                        label="复制举证摘要"
                        toastMessage="已复制售后举证摘要，可直接发给淘宝或 16688 客服"
                        variant="secondary"
                      />
                      <CopyButton text={detail.redeem_url} label="复制提货链接" variant="secondary" />
                    </div>
                  </div>
                  <div className="kv-grid">
                    <div className="kv-item">
                      <span className="kv-label">商品规格</span>
                      <span className="kv-value">
                        <span
                          className={`sku-tag ${
                            isExclusive ? "sku-tag-exclusive" : "sku-tag-shared"
                          }`}
                        >
                          {getSkuShortLabel(detail.sku_key)}
                        </span>
                      </span>
                    </div>
                    <div className="kv-item">
                      <span className="kv-label">销售渠道</span>
                      <span className="kv-value">
                        {getChannelLabel(detail.channel)}
                        {detail.goods_no ? ` · ${detail.goods_no}` : ""}
                      </span>
                    </div>
                    <div className="kv-item">
                      <span className="kv-label">外部订单号</span>
                      <span className="kv-value mono">{detail.external_order_id || "-"}</span>
                    </div>
                    <div className="kv-item">
                      <span className="kv-label">批次编号</span>
                      <span className="kv-value mono">{detail.batch_id || "-"}</span>
                    </div>
                    {isExclusive ? (
                      <>
                        <div className="kv-item">
                          <span className="kv-label">买家主邮箱</span>
                          <div className="row-between" style={{ gap: 6 }}>
                            <span
                              className={`kv-value ${exclusiveBuyerEmail ? "mono" : "muted"}`}
                              style={exclusiveBuyerEmail ? undefined : { color: "var(--ink-3)" }}
                            >
                              {exclusiveBuyerEmail || "待买家提交"}
                            </span>
                            {exclusiveBuyerCopyText && (
                              <CopyButton
                                text={exclusiveBuyerCopyText}
                                label="复制"
                                toastMessage="已复制买家主邮箱"
                                variant="ghost"
                              />
                            )}
                          </div>
                        </div>
                        <div className="kv-item">
                          <span className="kv-label">商店套餐邮箱</span>
                          <div className="row-between" style={{ gap: 6 }}>
                            <span
                              className={`kv-value ${exclusiveStoreEmail ? "mono" : "muted"}`}
                              style={exclusiveStoreEmail ? undefined : { color: "var(--ink-3)" }}
                            >
                              {exclusiveStoreEmail || "还没出货"}
                            </span>
                            {exclusiveStoreEmail && (
                              <CopyButton
                                text={exclusiveStoreEmail}
                                label="复制"
                                toastMessage="已复制商店套餐邮箱"
                                variant="ghost"
                              />
                            )}
                          </div>
                        </div>
                      </>
                    ) : (
                      <div className="kv-item">
                        <span className="kv-label">买家邮箱</span>
                        <div className="row-between" style={{ gap: 6 }}>
                          <span
                            className={`kv-value ${sharedBuyerEmail ? "mono" : "muted"}`}
                            style={sharedBuyerEmail ? undefined : { color: "var(--ink-3)" }}
                          >
                            {sharedBuyerEmail || "还没提货"}
                          </span>
                          {sharedBuyerCopyText && (
                            <CopyButton
                              text={sharedBuyerCopyText}
                              label="复制"
                              toastMessage="已复制买家邮箱"
                              variant="ghost"
                            />
                          )}
                        </div>
                      </div>
                    )}
                    <div className="kv-item">
                      <span className="kv-label">上游订单与成本</span>
                      <span className="kv-value mono">
                        {detail.upstream_order_code || "-"} · {formatUsd(detail.cost_usd)}
                      </span>
                    </div>
                    <div className="kv-item">
                      <span className="kv-label">上游状态 / 耗时</span>
                      <span className="kv-value">
                        {getUpstreamStatusLabel(detail.upstream_status)}
                        {detail.elapsed_minutes !== null && detail.elapsed_minutes !== undefined
                          ? ` · ${formatElapsedMinutes(detail.elapsed_minutes)}`
                          : ""}
                      </span>
                    </div>
                    <div className="kv-item">
                      <span className="kv-label">兑换码生成时间</span>
                      <span className="kv-value mono">{formatDateTime(detail.created_at)}</span>
                    </div>
                    <div className="kv-item">
                      <span className="kv-label">使用时间</span>
                      <span className="kv-value mono">
                        {detail.submitted_at ? formatDateTime(detail.submitted_at) : "尚未使用"}
                      </span>
                    </div>
                    <div className="kv-item">
                      <span className="kv-label">最近更新</span>
                      <span className="kv-value mono">{formatDateTime(detail.updated_at)}</span>
                    </div>
                  </div>
                  {detail.remark && (
                    <div className="inset-panel" style={{ fontSize: 13 }}>
                      <strong>商家备注：</strong>
                      {detail.remark}
                    </div>
                  )}
                </section>

                {isExclusive && exclusiveStoreEmail && (() => {
                  const extraCredentials = Array.isArray(detail.delivery?.credentials)
                    ? detail.delivery.credentials.filter(
                        (item) => item.value && item.value.trim() !== exclusiveStoreEmail
                      )
                    : [];
                  return (
                    <section className="store-email-box stack-sm" aria-label="独享商店套餐邮箱">
                      <div className="row-between" style={{ gap: 8 }}>
                        <strong style={{ fontSize: 13.5 }}>
                          商店套餐邮箱（给买家拉进家庭组用）
                        </strong>
                        <CopyButton
                          text={exclusiveStoreEmail}
                          label="复制商店邮箱"
                          toastMessage="已复制商店套餐邮箱"
                          variant="primary"
                        />
                      </div>
                      <div className="credential-value">{exclusiveStoreEmail}</div>
                      {extraCredentials.length > 0 && (
                        <div className="stack-sm" style={{ paddingTop: 4 }}>
                          {extraCredentials.map((credential) => (
                            <div key={credential.label} className="row-between">
                              <span className="credential-label">{credential.label}</span>
                              <span className="credential-value" style={{ fontSize: 13 }}>
                                {credential.value}
                              </span>
                            </div>
                          ))}
                        </div>
                      )}
                    </section>
                  );
                })()}

                {!isExclusive && detail.delivery && (() => {
                  const credentials = Array.isArray(detail.delivery.credentials)
                    ? detail.delivery.credentials
                    : [];
                  const fallbackText =
                    typeof detail.delivery.account_text === "string" &&
                    detail.delivery.account_text.trim().length > 0
                      ? detail.delivery.account_text
                      : JSON.stringify(detail.delivery);
                  return (
                    <section className="stack-sm" aria-label="已发出货信息">
                      <div className="row-between">
                        <strong style={{ fontSize: 14 }}>已发出货信息</strong>
                        <CopyButton
                          text={fallbackText}
                          label="复制出货内容"
                          variant="secondary"
                        />
                      </div>
                      <div className="credential-box">
                        {credentials.length > 0 ? (
                          credentials.map((credential) => (
                            <div key={credential.label} className="row-between">
                              <span className="credential-label">{credential.label}</span>
                              <span className="credential-value" style={{ fontSize: 13.5 }}>
                                {credential.value}
                              </span>
                            </div>
                          ))
                        ) : (
                          <div className="credential-value">{fallbackText}</div>
                        )}
                      </div>
                    </section>
                  );
                })()}

                {detail.warranty && (() => {
                  const warranty = detail.warranty;
                  const isTgAutoForwarded =
                    warranty.status === "registered_upstream" &&
                    (warranty.tg_forward_status === "sent" || warranty.auto_forwarded === true);
                  const isTgForwardFailed =
                    warranty.status !== "registered_upstream" &&
                    warranty.tg_forward_status === "failed";
                  const hasTgMessageId =
                    warranty.tg_message_id !== null &&
                    warranty.tg_message_id !== undefined &&
                    String(warranty.tg_message_id).trim() !== "";
                  const canForwardTg =
                    warranty.status !== "registered_upstream" &&
                    Boolean(warranty.admin_email && warranty.admin_email.trim());
                  return (
                  <section className="inset-panel stack-sm" aria-label="保修登记与官方报备">
                    <div className="row-between">
                      <div className="row" style={{ gap: 8 }}>
                        <CastoriceIcon name="admin-brand" size={22} />
                        <strong style={{ fontSize: 14 }}>保修登记与官方报备</strong>
                      </div>
                      <WarrantyStatusBadge status={warranty.status} />
                    </div>

                    <div className="kv-grid">
                      <div className="kv-item">
                        <span className="kv-label">买家主邮箱</span>
                        <span className="kv-value mono">
                          {warranty.admin_email || "待买家提交"}
                        </span>
                      </div>
                      <div className="kv-item">
                        <span className="kv-label">提交 / 报备时间</span>
                        <span className="kv-value mono">
                          {formatDateTime(warranty.submitted_at)} /{" "}
                          {formatDateTime(warranty.registered_at)}
                        </span>
                      </div>
                    </div>

                    {warranty.status === "registered_upstream" ? (
                      <div className="callout callout-success" role="status">
                        <CastoriceIcon name="check" size={19} className="callout-icon" />
                        <div>
                          <div>
                            已于{" "}
                            <strong className="mono">
                              {formatDateTime(
                                isTgAutoForwarded
                                  ? warranty.tg_sent_at || warranty.registered_at
                                  : warranty.registered_at
                              )}
                            </strong>{" "}
                            {isTgAutoForwarded ? (
                              <>
                                通过 <strong>TG 自动报备</strong> 完成官方报备
                              </>
                            ) : (
                              "完成官方报备"
                            )}
                          </div>
                          {isTgAutoForwarded && hasTgMessageId && (
                            <div style={{ marginTop: 4, fontSize: 12.5 }}>
                              TG 消息 ID：<span className="mono">{String(warranty.tg_message_id)}</span>
                            </div>
                          )}
                          {warranty.note && (
                            <div style={{ marginTop: 4, fontSize: 12.5 }}>
                              备注：{warranty.note}
                            </div>
                          )}
                        </div>
                      </div>
                    ) : (
                      <>
                        {isTgForwardFailed && (
                          <div className="callout callout-danger" role="alert">
                            <CastoriceIcon name="admin-alert" size={24} className="callout-icon" />
                            <div>
                              <strong>Telegram 自动报备失败</strong>
                              <div style={{ marginTop: 4, fontSize: 12.5, wordBreak: "break-word" }}>
                                {warranty.tg_error ||
                                  "未能自动发送给官方客服，可点击下方「重发 TG 报备」重试或改用人工报备。"}
                              </div>
                            </div>
                          </div>
                        )}
                        {warranty.note && (
                          <div style={{ fontSize: 13, color: "var(--ink-2)" }}>
                            <strong>买家备注：</strong>
                            {warranty.note}
                          </div>
                        )}
                      </>
                    )}

                    {warranty.has_screenshot && (
                      <div className="stack-sm">
                        <div className="row-between" style={{ fontSize: 13, color: "var(--ink-3)" }}>
                          <span className="row" style={{ gap: 6 }}>
                            <CastoriceIcon name="admin-ledger" size={18} />
                            <span>历史保修截图</span>
                          </span>
                          {screenshotUrl && (
                            <button
                              type="button"
                              className="btn btn-ghost btn-sm"
                              onClick={() => setScreenshotZoomOpen(true)}
                            >
                              <CastoriceIcon name="external" size={15} />
                              <span>点击放大</span>
                            </button>
                          )}
                        </div>
                        {screenshotLoading ? (
                          <div className="skeleton" style={{ height: 140, width: "100%" }} />
                        ) : screenshotError ? (
                          <p className="field-error" role="alert">
                            {screenshotError}
                          </p>
                        ) : screenshotUrl ? (
                          <button
                            type="button"
                            onClick={() => setScreenshotZoomOpen(true)}
                            aria-label="放大查看历史保修截图"
                            style={{
                              padding: 0,
                              border: 0,
                              background: "transparent",
                              cursor: "zoom-in",
                              width: "100%",
                              textAlign: "left",
                            }}
                          >
                            <img
                              src={screenshotUrl}
                              alt="历史订单保留的保修确认截图"
                              className="upload-preview-img"
                              style={{ maxHeight: 240 }}
                            />
                          </button>
                        ) : null}
                      </div>
                    )}

                    {warranty.forward_template && (
                      <div className="stack-sm">
                        <div className="row-between">
                          <span style={{ fontSize: 13, fontWeight: 600 }}>
                            发给官方的越南语报备话术
                          </span>
                          <CopyButton
                            text={warranty.forward_template}
                            label="复制越南语话术"
                            toastMessage="已复制越南语报备话术，可直接发给 Canboso 客服"
                            variant="primary"
                          />
                        </div>
                        <pre className="code-output">{warranty.forward_template}</pre>
                      </div>
                    )}

                    {warranty.status !== "registered_upstream" && (
                      <div className="stack-sm" style={{ paddingTop: 6 }}>
                        {canForwardTg && (
                          <button
                            type="button"
                            className={`btn ${isTgForwardFailed ? "btn-warm" : "btn-secondary"} btn-block ${
                              forwarding ? "is-loading" : ""
                            }`}
                            disabled={forwarding || acking}
                            aria-busy={forwarding}
                            onClick={() => void handleForwardWarranty()}
                          >
                            {forwarding ? (
                              <>
                                <CastoriceIcon name="refresh" size={17} spin />
                                <span>正在重发 TG 报备...</span>
                              </>
                            ) : (
                              <>
                                <CastoriceIcon name="mail" size={17} />
                                <span>重发 TG 报备</span>
                              </>
                            )}
                          </button>
                        )}
                        <input
                          type="text"
                          className="input"
                          placeholder="报备备注（可选，例如：已发 Telegram 客服确认）"
                          value={ackNote}
                          onChange={(event) => setAckNote(event.target.value)}
                          disabled={acking || forwarding}
                        />
                        <button
                          type="button"
                          className={`btn btn-primary btn-block ${acking ? "is-loading" : ""}`}
                          disabled={acking || forwarding}
                          aria-busy={acking}
                          onClick={() => void handleAckWarranty()}
                        >
                          {acking ? (
                            <>
                              <CastoriceIcon name="refresh" size={17} spin />
                              <span>保存中...</span>
                            </>
                          ) : (
                            <>
                              <CastoriceIcon name="check" size={17} />
                              <span>我已发给官方报备</span>
                            </>
                          )}
                        </button>
                      </div>
                    )}

                    {isExclusive &&
                      detail.status === "completed" &&
                      Boolean(warranty.admin_email?.trim()) && (
                        <div
                          className="stack-sm"
                          style={{
                            paddingTop: 12,
                            marginTop: 10,
                            borderTop: "1px solid var(--border)",
                          }}
                        >
                          <div className="row-between">
                            <strong style={{ fontSize: 13.5 }}>独享进组生效状态</strong>
                            <span
                              className={`badge ${
                                warranty.activated_at ? "badge-success" : "badge-warning"
                              }`}
                            >
                              {warranty.activated_at ? "已进组生效" : "买家等待进组中"}
                            </span>
                          </div>
                          <p style={{ margin: 0, fontSize: 12.5, color: "var(--ink-3)" }}>
                            {warranty.activated_at
                              ? `已于 ${formatDateTime(warranty.activated_at)} 标记进组生效，买家提货页已切换为已生效完成态。`
                              : "确认源头已登录商店邮箱同意家庭组邀请且会员生效后，点此可立即结束买家页面的等待状态。"}
                          </p>
                          <input
                            type="text"
                            className="input"
                            maxLength={200}
                            placeholder={
                              warranty.activated_at
                                ? "撤销备注（可选，例如：误点需重新核查）"
                                : "生效备注（可选，例如：源头已点同意进组）"
                            }
                            value={activationNote}
                            onChange={(event) => setActivationNote(event.target.value)}
                            disabled={togglingActivation}
                          />
                          <button
                            type="button"
                            className={`btn ${
                              warranty.activated_at ? "btn-secondary" : "btn-primary"
                            } btn-block ${togglingActivation ? "is-loading" : ""}`}
                            disabled={togglingActivation}
                            aria-busy={togglingActivation}
                            onClick={() =>
                              void handleToggleExclusiveActivated(!Boolean(warranty.activated_at))
                            }
                          >
                            {togglingActivation ? (
                              <>
                                <CastoriceIcon name="refresh" size={17} spin />
                                <span>保存中...</span>
                              </>
                            ) : warranty.activated_at ? (
                              <>
                                <CastoriceIcon name="retry" size={16} />
                                <span>撤销生效标记</span>
                              </>
                            ) : (
                              <>
                                <CastoriceIcon name="check" size={17} />
                                <span>标记已进组生效</span>
                              </>
                            )}
                          </button>
                        </div>
                      )}
                  </section>
                  );
                })()}

                {hasActions && (
                  <section className="stack-sm" aria-label="订单操作">
                    <strong style={{ fontSize: 14 }}>订单操作</strong>

                    {(canPoll || canResetUnused || canRevoke) && (
                      <div className="row" style={{ flexWrap: "wrap" }}>
                        {canPoll && (
                          <button
                            type="button"
                            className={`btn btn-secondary ${polling ? "is-loading" : ""}`}
                            disabled={polling}
                            aria-busy={polling}
                            onClick={() => void handlePoll()}
                          >
                            <CastoriceIcon name="refresh" size={17} spin={polling} />
                            <span>立即查一次上游进度</span>
                          </button>
                        )}

                        {canResetUnused && (
                          <button
                            type="button"
                            className={`btn btn-primary ${resetting ? "is-loading" : ""}`}
                            disabled={resetting}
                            aria-busy={resetting}
                            onClick={() => setResetConfirmOpen(true)}
                          >
                            <CastoriceIcon name="refresh" size={17} spin={resetting} />
                            <span>退回待提货（让买家重交）</span>
                          </button>
                        )}

                        {canRevoke && (
                          <button
                            type="button"
                            className="btn btn-danger"
                            onClick={() => {
                              setRevokeForceRequired(false);
                              setRevokeForceConfirmed(false);
                              setRevokeForceMessage(null);
                              setRevokeConfirmOpen(true);
                            }}
                          >
                            <CastoriceIcon name="admin-alert" size={19} />
                            <span>作废这个兑换码</span>
                          </button>
                        )}
                      </div>
                    )}

                    {showActiveUpstreamGuard && (
                      <div className="callout callout-warning" role="note">
                        <CastoriceIcon name="alert" size={20} className="callout-icon" />
                        <div>
                          <strong>防重复扣款保护：</strong>
                          原上游订单 <span className="mono">{detail.upstream_order_code}</span> 尚未确认取消/退款，暂不允许重下新单。请先点上方「立即查一次上游进度」同步终态。
                        </div>
                      </div>
                    )}

                    {canRetry && (
                      <form
                        className="inset-panel stack-sm"
                        onSubmit={(event) => {
                          event.preventDefault();
                          if (retryRecipientReady) setRetryConfirmOpen(true);
                        }}
                      >
                        <strong style={{ fontSize: 13.5 }}>
                          {effectiveForceNewOrder ? "重下新单（启用新幂等键）" : "重新向上游下单"}
                        </strong>
                        {forceNewOrderRequired ? (
                          <div className="callout callout-info" role="note">
                            <CastoriceIcon name="alert" size={18} className="callout-icon" />
                            <div>
                              原上游订单已取消/退款，本次将以新幂等键重新向 Canboso 下单（会按现价重新扣款）。
                            </div>
                          </div>
                        ) : canReorder ? (
                          <label className={`check-card ${forceNewOrder ? "is-checked" : ""}`}>
                            <input
                              type="checkbox"
                              checked={forceNewOrder}
                              onChange={(event) => setForceNewOrder(event.target.checked)}
                            />
                            <span className="check-card-body">
                              <strong>启用新幂等键重下新单</strong>
                              <span>
                                仅当确认原请求无法沿用或需要更换买家收件邮箱时再勾选。
                              </span>
                            </span>
                          </label>
                        ) : null}
                        {canEditNewBuyerEmail && (
                          <div className="field-group">
                            <label className="field-label" htmlFor={`${titleId}-new-buyer-email`}>
                              改发新买家邮箱（可选，留空沿用原邮箱）
                            </label>
                            <input
                              id={`${titleId}-new-buyer-email`}
                              name="new_buyer_email"
                              type="email"
                              className="input mono"
                              autoComplete="off"
                              autoCapitalize="none"
                              spellCheck={false}
                              maxLength={254}
                              placeholder={detail.buyer_email || "name@gmail.com"}
                              value={newBuyerEmail}
                              onChange={(event) => setNewBuyerEmail(event.target.value)}
                            />
                            {newBuyerEmailValidation && !newBuyerEmailValidation.valid && (
                              <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
                                <span className="field-error">{newBuyerEmailValidation.error}</span>
                                {newBuyerEmailValidation.suggestion && (
                                  <button
                                    type="button"
                                    className="btn btn-ghost btn-sm"
                                    onClick={() => setNewBuyerEmail(newBuyerEmailValidation.suggestion || "")}
                                  >
                                    改为 {newBuyerEmailValidation.suggestion}
                                  </button>
                                )}
                              </div>
                            )}
                            <small className="field-hint">
                              原单已取消/退款且买家需换邮箱重发时填写；留空则沿用原邮箱 <span className="mono">{detail.buyer_email || "-"}</span>。
                            </small>
                          </div>
                        )}
                        {detail.channel === "16688" && (
                          <label className="stack-sm">
                            <span>人工核验的 16688 订单号（OpenAPI 暂停时使用）</span>
                            <input
                              type="text"
                              className="input mono"
                              maxLength={100}
                              value={merchantVerifiedOrderNo}
                              onChange={(event) => setMerchantVerifiedOrderNo(event.target.value)}
                            />
                            <small>请先在 16688 商家后台确认该订单已付款、兑换码和商品相符，且没有退款或投诉。</small>
                          </label>
                        )}
                        {needsRecipientConfirmation && (
                          <div className="field-group">
                            <label className="field-label" htmlFor={`${titleId}-retry-recipient`}>
                              人工核对的收邀请邮箱（必填）
                            </label>
                            <input
                              id={`${titleId}-retry-recipient`}
                              name="merchant_verified_recipient_email"
                              type="email"
                              className="input mono"
                              autoComplete="off"
                              autoCapitalize="none"
                              spellCheck={false}
                              maxLength={254}
                              required
                              aria-describedby={`${titleId}-retry-recipient-hint`}
                              value={merchantVerifiedRecipientEmail}
                              onChange={(event) => setMerchantVerifiedRecipientEmail(event.target.value)}
                            />
                            <small id={`${titleId}-retry-recipient-hint`} className="field-hint">
                              {canEditNewBuyerEmail && trimmedNewBuyerEmail
                                ? "请填入与上方「新买家邮箱」完全一致的收邀请邮箱完成人工二次核对。"
                                : "请先向买家核对实际收邀请的完整邮箱，再手动填入，须与本次目标收件邮箱一致。"}
                            </small>
                          </div>
                        )}
                        <label className={`check-card ${overridePriceLimit ? "is-checked" : ""}`}>
                          <input
                            type="checkbox"
                            checked={overridePriceLimit}
                            onChange={(event) => setOverridePriceLimit(event.target.checked)}
                          />
                          <span className="check-card-body">
                            <strong>按当前进价放行并重下</strong>
                            <span>
                              确认上游涨价仍能接受时再勾选；不勾会继续按单笔最高进价检查。
                            </span>
                          </span>
                        </label>
                        <input
                          type="text"
                          className="input"
                          placeholder="操作备注（可选，例如：已充余额重新下单）"
                          value={retryNote}
                          onChange={(event) => setRetryNote(event.target.value)}
                        />
                        <button
                          type="submit"
                          className="btn btn-warm btn-block"
                          disabled={!retryRecipientReady || retrying}
                        >
                          <CastoriceIcon name="retry" size={17} />
                          <span>
                            {effectiveForceNewOrder
                              ? overridePriceLimit
                                ? "按当前进价放行并重下新单"
                                : "启用新单号重新下单"
                              : overridePriceLimit
                                ? "按当前进价放行并重下"
                                : "重新向上游下单"}
                          </span>
                        </button>
                      </form>
                    )}
                  </section>
                )}

                {canRecordRefund && (
                  <section className="inset-panel stack-sm" aria-label="上游实退登记">
                    <div className="row-between" style={{ gap: 8, flexWrap: "wrap" }}>
                      <strong style={{ fontSize: 13.5 }}>上游实退登记（USD）</strong>
                      <span className="mono" style={{ fontSize: 12, color: "var(--ink-3)" }}>
                        上游单号 {detail.upstream_order_code}
                      </span>
                    </div>
                    <div className="admin-refund-metrics">
                      <div className="kv-item">
                        <span className="kv-label">采购成本</span>
                        <span className="kv-value mono">{formatUsd(detail.cost_usd)}</span>
                      </div>
                      <div className="kv-item">
                        <span className="kv-label">已登记实退</span>
                        <span className="kv-value mono">
                          {detail.upstream_refund_usd !== null && detail.upstream_refund_usd !== undefined
                            ? formatUsd(detail.upstream_refund_usd)
                            : "未登记"}
                        </span>
                      </div>
                      <div className="kv-item">
                        <span className="kv-label">净成本 / 损耗</span>
                        <span className="kv-value mono">
                          {netLossUsd !== null ? formatUsd(netLossUsd) : "-"}
                        </span>
                      </div>
                    </div>
                    <form
                      className="stack-sm"
                      onSubmit={(event) => {
                        event.preventDefault();
                        void handleSaveUpstreamRefund();
                      }}
                    >
                      <div className="admin-refund-form-row">
                        <input
                          type="number"
                          min="0"
                          step="0.01"
                          inputMode="decimal"
                          className="input mono"
                          aria-label="上游实退金额（USD）"
                          placeholder="实退金额，如 12.50"
                          value={refundAmountInput}
                          onChange={(event) => setRefundAmountInput(event.target.value)}
                          disabled={savingRefund}
                        />
                        <input
                          type="text"
                          className="input"
                          maxLength={300}
                          aria-label="实退备注"
                          placeholder="退款备注（可选，如：Canboso 钱包已原路退回）"
                          value={refundNoteInput}
                          onChange={(event) => setRefundNoteInput(event.target.value)}
                          disabled={savingRefund}
                        />
                        <button
                          type="submit"
                          className={`btn btn-secondary ${savingRefund ? "is-loading" : ""}`}
                          disabled={savingRefund}
                          aria-busy={savingRefund}
                        >
                          <span>{savingRefund ? "保存中..." : "保存实退"}</span>
                        </button>
                      </div>
                    </form>
                  </section>
                )}

                <section className="stack-sm" aria-label="订单动态">
                  <strong style={{ fontSize: 14 }}>订单动态</strong>
                  {detail.audit_events.length === 0 ? (
                    <p className="field-hint">暂无订单动态</p>
                  ) : (
                    <ol className="timeline">
                      {detail.audit_events.map((auditItem) => {
                        const ev = formatAuditEvent(auditItem);
                        return (
                          <li key={auditItem.event_id} className="timeline-item">
                            <span className="timeline-dot" aria-hidden="true" />
                            <div className="stack-sm" style={{ gap: 2 }}>
                              <div className="row-between">
                                <strong style={{ fontSize: 13, color: "var(--ink-1)" }}>
                                  {ev.title}
                                </strong>
                                <span className="mono" style={{ fontSize: 12, color: "var(--ink-3)" }}>
                                  {formatDateTime(auditItem.created_at)}
                                </span>
                              </div>
                              {ev.detail && ev.detail !== ev.title && (
                                <span style={{ color: "var(--ink-2)" }}>{ev.detail}</span>
                              )}
                            </div>
                          </li>
                        );
                      })}
                    </ol>
                  )}
                </section>
              </>
            ) : null}
          </div>
        </aside>
      </div>

      {detail?.warranty?.has_screenshot && screenshotZoomOpen && screenshotUrl && (
        <div
          role="dialog"
          className="admin-screenshot-lightbox"
          aria-modal="true"
          aria-label="放大查看历史保修截图"
          onClick={() => setScreenshotZoomOpen(false)}
        >
          <div
            onClick={(event) => event.stopPropagation()}
            style={{
              position: "relative",
              maxWidth: "92vw",
              maxHeight: "90vh",
              display: "flex",
              flexDirection: "column",
              gap: 10,
              alignItems: "flex-end",
            }}
          >
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={() => setScreenshotZoomOpen(false)}
            >
              <CastoriceIcon name="close" size={16} />
              <span>关闭大图</span>
            </button>
            <img
              src={screenshotUrl}
              alt="历史订单保留的保修确认截图大图"
              style={{
                maxWidth: "92vw",
                maxHeight: "calc(90vh - 48px)",
                borderRadius: 12,
                background: "#fff",
                objectFit: "contain",
              }}
            />
          </div>
        </div>
      )}

      <ConfirmDialog
        open={retryConfirmOpen && canRetry && detail?.code === code}
        title={effectiveForceNewOrder ? "确认启用新单号重新下单" : "确认重新向上游下单"}
        confirmLabel={
          effectiveForceNewOrder
            ? overridePriceLimit
              ? "确认放行并重下新单"
              : "确认重下新单"
            : overridePriceLimit
              ? "确认放行并重下"
              : "确认重新下单"
        }
        busy={retrying}
        onConfirm={() => void handleRetryConfirm()}
        onClose={() => setRetryConfirmOpen(false)}
      >
        <p>
          即将为兑换码 <strong className="mono">{code}</strong> 重新向上游发起下单。
          {effectiveForceNewOrder
            ? "本次会使用新幂等键向 Canboso 创建新订单。"
            : "系统会沿用原幂等键防止重复扣款。"}
          {overridePriceLimit
            ? "你已勾选按当前进价放行，生产模式下会直接按上游现价下单。"
            : "系统会继续检查单笔最高进价限制。"}
        </p>
        {canEditNewBuyerEmail && newBuyerEmailValidation?.valid && (
          <p>
            目标收件邮箱将更新为 <strong className="mono">{newBuyerEmailValidation.normalized}</strong>。
          </p>
        )}
        {merchantVerifiedOrderNo.trim() && (
          <p>确认你已在 16688 商家后台核验订单 <strong className="mono">{merchantVerifiedOrderNo.trim()}</strong>。</p>
        )}
        {needsRecipientConfirmation && (
          <p>确认你已向买家核对收邀请邮箱 <strong className="mono">{verifiedRecipient}</strong>。</p>
        )}
      </ConfirmDialog>

      <ConfirmDialog
        open={resetConfirmOpen && canResetUnused && detail?.code === code}
        title="确认退回待提货（让买家重交）"
        confirmLabel="确认退回待提货"
        busy={resetting}
        onConfirm={() => void handleResetUnusedConfirm()}
        onClose={() => setResetConfirmOpen(false)}
      >
        <p>
          即将把兑换码 <strong className="mono">{code}</strong> 重置为未使用（待提货），并清空本次失败记录。
        </p>
        <p>
          原发码渠道和外部订单号会保留，确认 Canboso 钱包已充值后，让买家刷新原提货链接重新提交即可；本操作不会直接扣款。
        </p>
        <div className="field-group">
          <label className="field-label" htmlFor="reset-unused-note-input">
            <span>操作备注（可选）</span>
          </label>
          <input
            id="reset-unused-note-input"
            type="text"
            className="input"
            maxLength={200}
            placeholder="例如：钱包已充值，请买家重新提交"
            value={resetNote}
            onChange={(event) => setResetNote(event.target.value)}
          />
        </div>
      </ConfirmDialog>

      <ConfirmDialog
        open={revokeConfirmOpen}
        title={revokeForceRequired ? "上游订单仍活跃 · 确认强制本地作废" : "确认作废这个兑换码"}
        confirmLabel={revokeForceRequired ? "确认强制本地作废" : "确认作废"}
        confirmVariant="danger"
        busy={revoking}
        confirmDisabled={revokeForceRequired && !revokeForceConfirmed}
        onConfirm={() => void handleRevokeConfirm()}
        onClose={closeRevokeDialog}
      >
        <p>
          作废后买家就无法再用 <strong className="mono">{code}</strong> 提货，适合买家退款或取消订单后防止重复领取。
        </p>
        {revokeForceRequired && (
          <div className="stack-sm">
            <div className="callout callout-danger" role="alert">
              <CastoriceIcon name="admin-alert" size={20} className="callout-icon" />
              <div>
                <strong>上游订单仍处于活跃状态：</strong>
                {revokeForceMessage || "该兑换码已在上游扣款下单且未确认取消，强制本地作废不会自动退回上游款项。"}
              </div>
            </div>
            <label className={`check-card ${revokeForceConfirmed ? "is-checked" : ""}`}>
              <input
                type="checkbox"
                checked={revokeForceConfirmed}
                onChange={(event) => setRevokeForceConfirmed(event.target.checked)}
              />
              <span className="check-card-body">
                <strong>我已核对上游退款或已知晓扣款风险，强制本地作废</strong>
                <span>勾选后将携带 force=true 强制将此兑换码置为已作废。</span>
              </span>
            </label>
          </div>
        )}
        <div className="field-group">
          <label className="field-label" htmlFor="revoke-reason-input">
            <span>作废原因</span>
          </label>
          <input
            id="revoke-reason-input"
            type="text"
            className="input"
            value={revokeReason}
            onChange={(event) => setRevokeReason(event.target.value)}
          />
        </div>
      </ConfirmDialog>
    </>
  );
}
