import { useCallback, useEffect, useId, useState } from "react";
import {
  AlertTriangle,
  Ban,
  CheckCircle2,
  Image as ImageIcon,
  Loader2,
  Maximize2,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  X,
} from "lucide-react";
import {
  ackAdminWarranty,
  ApiError,
  fetchAdminWarrantyScreenshot,
  getAdminCodeDetail,
  pollAdminCode,
  retryAdminCode,
  revokeAdminCode,
} from "../../api/client";
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
  getSkuShortLabel,
  getUpstreamStatusLabel,
} from "../../utils/format";

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

  const [overridePriceLimit, setOverridePriceLimit] = useState(false);
  const [retryNote, setRetryNote] = useState("");
  const [retryConfirmOpen, setRetryConfirmOpen] = useState(false);
  const [retrying, setRetrying] = useState(false);

  const [revokeReason, setRevokeReason] = useState("买家退款或手动作废");
  const [revokeConfirmOpen, setRevokeConfirmOpen] = useState(false);
  const [revoking, setRevoking] = useState(false);

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
    setRetryNote("");
    setAckNote("");
    setRevokeReason("买家退款或手动作废");
    if (code) {
      void loadDetail();
    }
  }, [code, loadDetail]);

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
        setScreenshotError(caught instanceof ApiError ? caught.message : "加载保修截图失败");
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

  const handleRetryConfirm = async () => {
    setRetrying(true);
    try {
      const updated = await retryAdminCode(token, code, {
        override_price_limit: overridePriceLimit,
        note: retryNote.trim() || undefined,
      });
      setDetail(updated);
      setRetryConfirmOpen(false);
      setOverridePriceLimit(false);
      setRetryNote("");
      onMutated();
      pushToast("已重新向上游发起下单", "success");
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401) {
        onUnauthorized();
        return;
      }
      pushToast(caught instanceof ApiError ? caught.message : "重新下单失败，请稍后排查", "error");
    } finally {
      setRetrying(false);
    }
  };

  const handleRevokeConfirm = async () => {
    setRevoking(true);
    try {
      const updated = await revokeAdminCode(
        token,
        code,
        revokeReason.trim() || "商家手动作废"
      );
      setDetail(updated);
      setRevokeConfirmOpen(false);
      onMutated();
      pushToast(`兑换码 ${code} 已作废`, "success");
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401) {
        onUnauthorized();
        return;
      }
      pushToast(caught instanceof ApiError ? caught.message : "作废操作失败", "error");
    } finally {
      setRevoking(false);
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
    detail && detail.status === "waiting_fulfillment" && detail.upstream_order_code
  );
  const canRetry = Boolean(detail && detail.status === "needs_manual_review");
  const canRevoke = Boolean(
    detail && detail.status !== "completed" && detail.status !== "revoked"
  );
  const hasActions = canPoll || canRetry || canRevoke;

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
              <X size={18} />
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
                <AlertTriangle size={18} className="callout-icon" aria-hidden="true" />
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
                    <AlertTriangle size={18} className="callout-icon" aria-hidden="true" />
                    <div>
                      <strong>异常提醒：</strong>
                      {formatOrderErrorMessage(detail.last_error_code, detail.last_error_message)}
                    </div>
                  </div>
                )}

                <section className="stack-sm" aria-label="订单基础信息">
                  <div className="row-between">
                    <strong style={{ fontSize: 14 }}>基础信息</strong>
                    <CopyButton text={detail.redeem_url} label="复制提货链接" variant="secondary" />
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
                          <span className="kv-label">买家主 Gmail</span>
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
                                toastMessage="已复制买家主 Gmail"
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
                        <span className="kv-label">买家 Gmail</span>
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
                              toastMessage="已复制买家 Gmail"
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

                {detail.warranty && (
                  <section className="inset-panel stack-sm" aria-label="保修登记与官方报备">
                    <div className="row-between">
                      <div className="row" style={{ gap: 8 }}>
                        <ShieldCheck size={16} style={{ color: "var(--accent)" }} aria-hidden="true" />
                        <strong style={{ fontSize: 14 }}>保修登记与官方报备</strong>
                      </div>
                      <WarrantyStatusBadge status={detail.warranty.status} />
                    </div>

                    <div className="kv-grid">
                      <div className="kv-item">
                        <span className="kv-label">买家主 Gmail</span>
                        <span className="kv-value mono">
                          {detail.warranty.admin_email || "待买家提交"}
                        </span>
                      </div>
                      <div className="kv-item">
                        <span className="kv-label">提交 / 报备时间</span>
                        <span className="kv-value mono">
                          {formatDateTime(detail.warranty.submitted_at)} /{" "}
                          {formatDateTime(detail.warranty.registered_at)}
                        </span>
                      </div>
                    </div>

                    {detail.warranty.status === "registered_upstream" ? (
                      <div className="callout callout-success" role="status">
                        <CheckCircle2 size={17} className="callout-icon" aria-hidden="true" />
                        <div>
                          <div>
                            已于{" "}
                            <strong className="mono">
                              {formatDateTime(detail.warranty.registered_at)}
                            </strong>{" "}
                            完成官方报备
                          </div>
                          {detail.warranty.note && (
                            <div style={{ marginTop: 4, fontSize: 12.5 }}>
                              备注：{detail.warranty.note}
                            </div>
                          )}
                        </div>
                      </div>
                    ) : detail.warranty.note ? (
                      <div style={{ fontSize: 13, color: "var(--ink-2)" }}>
                        <strong>买家备注：</strong>
                        {detail.warranty.note}
                      </div>
                    ) : null}

                    {detail.warranty.has_screenshot && (
                      <div className="stack-sm">
                        <div className="row-between" style={{ fontSize: 13, color: "var(--ink-3)" }}>
                          <span className="row" style={{ gap: 6 }}>
                            <ImageIcon size={14} aria-hidden="true" />
                            <span>买家家庭组邀请截图</span>
                          </span>
                          {screenshotUrl && (
                            <button
                              type="button"
                              className="btn btn-ghost btn-sm"
                              onClick={() => setScreenshotZoomOpen(true)}
                            >
                              <Maximize2 size={13} />
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
                            aria-label="放大查看买家家庭组邀请截图"
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
                              alt="买家上传的 Google 家庭组邀请确认截图"
                              className="upload-preview-img"
                              style={{ maxHeight: 240 }}
                            />
                          </button>
                        ) : null}
                      </div>
                    )}

                    {detail.warranty.forward_template && (
                      <div className="stack-sm">
                        <div className="row-between">
                          <span style={{ fontSize: 13, fontWeight: 600 }}>
                            发给官方的越南语报备话术
                          </span>
                          <CopyButton
                            text={detail.warranty.forward_template}
                            label="复制越南语话术"
                            toastMessage="已复制越南语报备话术，可直接发给 Canboso 客服"
                            variant="primary"
                          />
                        </div>
                        <pre className="code-output">{detail.warranty.forward_template}</pre>
                      </div>
                    )}

                    {detail.warranty.status !== "registered_upstream" && (
                      <div className="stack-sm" style={{ paddingTop: 6 }}>
                        <input
                          type="text"
                          className="input"
                          placeholder="报备备注（可选，例如：已发 Telegram 客服确认）"
                          value={ackNote}
                          onChange={(event) => setAckNote(event.target.value)}
                        />
                        <button
                          type="button"
                          className={`btn btn-primary btn-block ${acking ? "is-loading" : ""}`}
                          disabled={acking}
                          aria-busy={acking}
                          onClick={() => void handleAckWarranty()}
                        >
                          {acking ? (
                            <>
                              <Loader2 size={15} className="spin" />
                              <span>保存中...</span>
                            </>
                          ) : (
                            <>
                              <CheckCircle2 size={15} />
                              <span>我已发给官方报备</span>
                            </>
                          )}
                        </button>
                      </div>
                    )}
                  </section>
                )}

                {hasActions && (
                  <section className="stack-sm" aria-label="订单操作">
                    <strong style={{ fontSize: 14 }}>订单操作</strong>

                    {(canPoll || canRevoke) && (
                      <div className="row">
                        {canPoll && (
                          <button
                            type="button"
                            className={`btn btn-secondary ${polling ? "is-loading" : ""}`}
                            disabled={polling}
                            aria-busy={polling}
                            onClick={() => void handlePoll()}
                          >
                            {polling ? (
                              <Loader2 size={15} className="spin" />
                            ) : (
                              <RefreshCw size={15} />
                            )}
                            <span>立即查一次上游进度</span>
                          </button>
                        )}

                        {canRevoke && (
                          <button
                            type="button"
                            className="btn btn-danger"
                            onClick={() => setRevokeConfirmOpen(true)}
                          >
                            <Ban size={15} />
                            <span>作废这个兑换码</span>
                          </button>
                        )}
                      </div>
                    )}

                    {canRetry && (
                      <div className="inset-panel stack-sm">
                        <strong style={{ fontSize: 13.5 }}>重新向上游下单</strong>
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
                          type="button"
                          className="btn btn-warm btn-block"
                          onClick={() => setRetryConfirmOpen(true)}
                        >
                          <RotateCcw size={15} />
                          <span>
                            {overridePriceLimit ? "按当前进价放行并重下" : "重新向上游下单"}
                          </span>
                        </button>
                      </div>
                    )}
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

      {screenshotZoomOpen && screenshotUrl && (
        <div
          role="dialog"
          className="admin-screenshot-lightbox"
          aria-modal="true"
          aria-label="放大查看保修截图"
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
              <X size={15} />
              <span>关闭大图</span>
            </button>
            <img
              src={screenshotUrl}
              alt="买家上传的 Google 家庭组邀请确认截图大图"
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
        open={retryConfirmOpen}
        title="确认重新向上游下单"
        confirmLabel={overridePriceLimit ? "确认放行并重下" : "确认重新下单"}
        busy={retrying}
        onConfirm={() => void handleRetryConfirm()}
        onClose={() => setRetryConfirmOpen(false)}
      >
        <p>
          即将为兑换码 <strong className="mono">{code}</strong> 重新向上游发起下单。
          {overridePriceLimit
            ? "你已勾选按当前进价放行，生产模式下会直接按上游现价下单。"
            : "系统会自动防止重复扣款，并继续检查单笔最高进价限制。"}
        </p>
      </ConfirmDialog>

      <ConfirmDialog
        open={revokeConfirmOpen}
        title="确认作废这个兑换码"
        confirmLabel="确认作废"
        confirmVariant="danger"
        busy={revoking}
        onConfirm={() => void handleRevokeConfirm()}
        onClose={() => setRevokeConfirmOpen(false)}
      >
        <p>
          作废后买家就无法再用 <strong className="mono">{code}</strong> 提货，适合买家退款或取消订单后防止重复领取。
        </p>
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
