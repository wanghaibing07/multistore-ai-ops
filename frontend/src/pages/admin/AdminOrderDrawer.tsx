import { useCallback, useEffect, useId, useState } from "react";
import {
  AlertTriangle,
  Ban,
  CheckCircle2,
  Image as ImageIcon,
  Loader2,
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
import { CodeStatusBadge, WarrantyStatusBadge } from "../../components/StatusBadge";
import { useToast } from "../../components/ToastProvider";
import type { AdminCodeDetail } from "../../types/api";
import {
  formatDateTime,
  formatElapsedMinutes,
  formatUsd,
  getChannelLabel,
  getSkuShortLabel,
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
      if (event.key === "Escape" && !retryConfirmOpen && !revokeConfirmOpen) {
        onClose();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [code, onClose, retryConfirmOpen, revokeConfirmOpen]);

  if (!code) {
    return null;
  }

  const handlePoll = async () => {
    setPolling(true);
    try {
      const updated = await pollAdminCode(token, code);
      setDetail(updated);
      onMutated();
      pushToast("已同步最新上游状态", "success");
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401) {
        onUnauthorized();
        return;
      }
      pushToast(caught instanceof ApiError ? caught.message : "上游轮询失败", "error");
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
      pushToast("已标记向 Canboso 完成保修报备", "success");
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
      pushToast("已触发人工重试", "success");
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401) {
        onUnauthorized();
        return;
      }
      pushToast(caught instanceof ApiError ? caught.message : "重试失败，请检查日志", "error");
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

  const canPoll =
    detail &&
    (detail.status === "waiting_fulfillment" || Boolean(detail.upstream_order_code));
  const canRetry = detail?.status === "needs_manual_review";
  const canRevoke =
    detail && detail.status !== "completed" && detail.status !== "revoked";

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
            <div className="row" style={{ gap: 10, minWidth: 0 }}>
              <h2 id={titleId} className="card-title mono" style={{ fontSize: 16 }}>
                {code}
              </h2>
              {detail && <CodeStatusBadge status={detail.status} />}
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
                      <strong>最近异常（{detail.last_error_code || "ERROR"}）：</strong>
                      {detail.last_error_message || "上游或风控拦截，请人工核查后处理"}
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
                      <span className="kv-value">{getSkuShortLabel(detail.sku_key)}</span>
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
                    <div className="kv-item">
                      <span className="kv-label">买家邮箱</span>
                      <span className="kv-value mono">
                        {detail.buyer_email || detail.buyer_email_masked || "-"}
                      </span>
                    </div>
                    <div className="kv-item">
                      <span className="kv-label">上游订单与成本</span>
                      <span className="kv-value mono">
                        {detail.upstream_order_code || "-"} · {formatUsd(detail.cost_usd)}
                      </span>
                    </div>
                    <div className="kv-item">
                      <span className="kv-label">上游状态 / 耗时</span>
                      <span className="kv-value">
                        {detail.upstream_status || "-"} · {formatElapsedMinutes(detail.elapsed_minutes)}
                      </span>
                    </div>
                    <div className="kv-item">
                      <span className="kv-label">幂等键</span>
                      <span className="kv-value mono">{detail.idempotency_key || "-"}</span>
                    </div>
                    <div className="kv-item">
                      <span className="kv-label">创建时间</span>
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

                {detail.delivery && (() => {
                  const credentials = Array.isArray(detail.delivery.credentials)
                    ? detail.delivery.credentials
                    : [];
                  const fallbackText =
                    typeof detail.delivery.account_text === "string" &&
                    detail.delivery.account_text.trim().length > 0
                      ? detail.delivery.account_text
                      : JSON.stringify(detail.delivery);
                  return (
                  <section className="stack-sm" aria-label="交付凭据">
                    <div className="row-between">
                      <strong style={{ fontSize: 14 }}>已下发交付信息</strong>
                      <CopyButton
                        text={fallbackText}
                        label="复制交付文本"
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
                  <section className="inset-panel stack-sm" aria-label="独享整组保修登记">
                    <div className="row-between">
                      <div className="row" style={{ gap: 8 }}>
                        <ShieldCheck size={16} style={{ color: "var(--accent)" }} aria-hidden="true" />
                        <strong style={{ fontSize: 14 }}>保修登记与官方报备</strong>
                      </div>
                      <WarrantyStatusBadge status={detail.warranty.status} />
                    </div>

                    <div className="kv-grid">
                      <div className="kv-item">
                        <span className="kv-label">买家管理员 Gmail</span>
                        <span className="kv-value mono">{detail.warranty.admin_email || "-"}</span>
                      </div>
                      <div className="kv-item">
                        <span className="kv-label">提交 / 报备时间</span>
                        <span className="kv-value mono">
                          {formatDateTime(detail.warranty.submitted_at)} /{" "}
                          {formatDateTime(detail.warranty.registered_at)}
                        </span>
                      </div>
                    </div>

                    {detail.warranty.note && (
                      <div style={{ fontSize: 13, color: "var(--ink-2)" }}>
                        <strong>买家备注：</strong>
                        {detail.warranty.note}
                      </div>
                    )}

                    {detail.warranty.has_screenshot && (
                      <div className="stack-sm">
                        <div className="row" style={{ gap: 6, fontSize: 13, color: "var(--ink-3)" }}>
                          <ImageIcon size={14} aria-hidden="true" />
                          <span>买家邀请确认截图（需管理员鉴权加载）</span>
                        </div>
                        {screenshotLoading ? (
                          <div className="skeleton" style={{ height: 140, width: "100%" }} />
                        ) : screenshotError ? (
                          <p className="field-error" role="alert">
                            {screenshotError}
                          </p>
                        ) : screenshotUrl ? (
                          <img
                            src={screenshotUrl}
                            alt="买家上传的 Google 家庭组邀请确认截图"
                            className="upload-preview-img"
                            style={{ maxHeight: 240 }}
                          />
                        ) : null}
                      </div>
                    )}

                    {detail.warranty.forward_template && (
                      <div className="stack-sm">
                        <div className="row-between">
                          <span style={{ fontSize: 13, fontWeight: 600 }}>Canboso 管理员报备模板</span>
                          <CopyButton
                            text={detail.warranty.forward_template}
                            label="一键复制报备文本"
                            toastMessage="已复制保修报备文本，可直接发给 Canboso 客服"
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
                              <span>更新中...</span>
                            </>
                          ) : (
                            <>
                              <CheckCircle2 size={15} />
                              <span>标记已向 Canboso 完成报备</span>
                            </>
                          )}
                        </button>
                      </div>
                    )}
                  </section>
                )}

                <section className="stack-sm" aria-label="人工干预操作">
                  <strong style={{ fontSize: 14 }}>人工履约操作</strong>

                  <div className="row">
                    {canPoll && (
                      <button
                        type="button"
                        className={`btn btn-secondary ${polling ? "is-loading" : ""}`}
                        disabled={polling}
                        aria-busy={polling}
                        onClick={() => void handlePoll()}
                      >
                        {polling ? <Loader2 size={15} className="spin" /> : <RefreshCw size={15} />}
                        <span>立即轮询上游状态</span>
                      </button>
                    )}

                    {canRevoke && (
                      <button
                        type="button"
                        className="btn btn-danger"
                        onClick={() => setRevokeConfirmOpen(true)}
                      >
                        <Ban size={15} />
                        <span>作废兑换码</span>
                      </button>
                    )}
                  </div>

                  {canRetry && (
                    <div className="inset-panel stack-sm">
                      <strong style={{ fontSize: 13.5 }}>人工重试上游下单</strong>
                      <label className={`check-card ${overridePriceLimit ? "is-checked" : ""}`}>
                        <input
                          type="checkbox"
                          checked={overridePriceLimit}
                          onChange={(event) => setOverridePriceLimit(event.target.checked)}
                        />
                        <span className="check-card-body">
                          <strong>本次放行价格熔断上限（override_price_limit）</strong>
                          <span>仅在确认上游涨价仍可接受时勾选，否则保持按当前上限风控校验。</span>
                        </span>
                      </label>
                      <input
                        type="text"
                        className="input"
                        placeholder="操作备注（可选，例如：已补余额重新下单）"
                        value={retryNote}
                        onChange={(event) => setRetryNote(event.target.value)}
                      />
                      <button
                        type="button"
                        className="btn btn-warm btn-block"
                        onClick={() => setRetryConfirmOpen(true)}
                      >
                        <RotateCcw size={15} />
                        <span>重试上游履约</span>
                      </button>
                    </div>
                  )}
                </section>

                <section className="stack-sm" aria-label="状态流转审计日志">
                  <strong style={{ fontSize: 14 }}>审计时间线</strong>
                  {detail.audit_events.length === 0 ? (
                    <p className="field-hint">暂无状态流转记录</p>
                  ) : (
                    <ol className="timeline">
                      {detail.audit_events.map((auditItem) => (
                        <li key={auditItem.event_id} className="timeline-item">
                          <span className="timeline-dot" aria-hidden="true" />
                          <div className="stack-sm" style={{ gap: 2 }}>
                            <div className="row-between">
                              <strong style={{ fontSize: 13, color: "var(--ink-1)" }}>
                                {auditItem.from_status
                                  ? `${auditItem.from_status} -> ${auditItem.to_status}`
                                  : auditItem.to_status}
                              </strong>
                              <span className="mono" style={{ fontSize: 12, color: "var(--ink-3)" }}>
                                {formatDateTime(auditItem.created_at)}
                              </span>
                            </div>
                            <span style={{ color: "var(--ink-2)" }}>{auditItem.reason}</span>
                          </div>
                        </li>
                      ))}
                    </ol>
                  )}
                </section>
              </>
            ) : null}
          </div>
        </aside>
      </div>

      <ConfirmDialog
        open={retryConfirmOpen}
        title="确认重新触发上游履约"
        confirmLabel="确认重试"
        busy={retrying}
        onConfirm={() => void handleRetryConfirm()}
        onClose={() => setRetryConfirmOpen(false)}
      >
        <p>
          即将对兑换码 <strong className="mono">{code}</strong> 重新执行履约流程。
          {overridePriceLimit
            ? "当前已勾选放行价格熔断上限，若后端处于 LIVE 模式会按上游实时价格下单。"
            : "系统会复用原有幂等键并继续校验价格熔断上限。"}
        </p>
      </ConfirmDialog>

      <ConfirmDialog
        open={revokeConfirmOpen}
        title="确认作废该兑换码"
        confirmLabel="确认作废"
        confirmVariant="danger"
        busy={revoking}
        onConfirm={() => void handleRevokeConfirm()}
        onClose={() => setRevokeConfirmOpen(false)}
      >
        <p>
          作废后买家将无法继续核销 <strong className="mono">{code}</strong>，通常用于买家退款或取消订单后防二次提货。
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
