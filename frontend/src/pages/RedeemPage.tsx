import { useCallback, useEffect, useRef, useState, type ChangeEvent, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import {
  AlertCircle,
  ArrowLeft,
  Check,
  CheckCircle2,
  Circle,
  Clock,
  ExternalLink,
  ImagePlus,
  Loader2,
  MailCheck,
  RefreshCw,
  ShieldCheck,
  TicketCheck,
  Trash2,
  UploadCloud,
} from "lucide-react";
import {
  ApiError,
  getPublicCode,
  redeemPublicCode,
  refreshPublicCode,
  submitPublicWarranty,
} from "../api/client";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { CopyButton } from "../components/CopyButton";
import { GmailDoubleInput } from "../components/GmailDoubleInput";
import { CodeStatusBadge, WarrantyStatusBadge } from "../components/StatusBadge";
import { useToast } from "../components/ToastProvider";
import type { CodeStatus, ProductType, PublicCodeResponse } from "../types/api";
import { formatDateTime, formatElapsedMinutes, getChannelLabel } from "../utils/format";
import { validateGmailInput, validateWarrantyScreenshot } from "../utils/validation";

const AUTO_POLL_INTERVAL_MS = 30_000;

function resolveStepIndex(productType: ProductType, status: CodeStatus, warrantySubmitted: boolean): number {
  if (productType === "slot") {
    if (status === "completed") return 2;
    if (status === "waiting_fulfillment" || status === "purchasing" || status === "validating") return 1;
    return 0;
  }
  if (status === "completed" || warrantySubmitted) return 2;
  if (status === "delivered_pending_warranty") return 1;
  return 0;
}

export function RedeemPage() {
  const { code: rawCode } = useParams<{ code: string }>();
  const code = (rawCode || "").trim().toUpperCase();
  const { pushToast } = useToast();

  const [record, setRecord] = useState<PublicCodeResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [notFoundMessage, setNotFoundMessage] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [checkFamily, setCheckFamily] = useState(false);
  const [checkPayment, setCheckPayment] = useState(false);

  const [buyerEmail, setBuyerEmail] = useState("");
  const [buyerEmailConfirm, setBuyerEmailConfirm] = useState("");
  const [confirmModalOpen, setConfirmModalOpen] = useState(false);
  const [submittingRedeem, setSubmittingRedeem] = useState(false);
  const [redeemError, setRedeemError] = useState<string | null>(null);

  const [refreshing, setRefreshing] = useState(false);
  const [cooldownLeft, setCooldownLeft] = useState(0);

  const [editingWarranty, setEditingWarranty] = useState(false);
  const [adminEmail, setAdminEmail] = useState("");
  const [adminEmailConfirm, setAdminEmailConfirm] = useState("");
  const [warrantyNote, setWarrantyNote] = useState("");
  const [screenshotFile, setScreenshotFile] = useState<File | null>(null);
  const [screenshotPreview, setScreenshotPreview] = useState<string | null>(null);
  const [warrantyError, setWarrantyError] = useState<string | null>(null);
  const [submittingWarranty, setSubmittingWarranty] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const fetchRecord = useCallback(
    async (silent = false) => {
      if (!code) {
        setNotFoundMessage("兑换码为空，请返回重新输入。");
        setLoading(false);
        return;
      }
      if (!silent) {
        setLoading(true);
        setLoadError(null);
        setNotFoundMessage(null);
      }
      try {
        const data = await getPublicCode(code);
        setRecord(data);
        setLoadError(null);
        setNotFoundMessage(null);
      } catch (err) {
        if (err instanceof ApiError && err.status === 404) {
          setNotFoundMessage(err.message || "未找到该兑换码，请检查拼写是否正确。");
          setRecord(null);
        } else if (!silent) {
          setLoadError(err instanceof Error ? err.message : "加载订单信息失败，请稍后重试。");
        }
      } finally {
        if (!silent) setLoading(false);
      }
    },
    [code]
  );

  useEffect(() => {
    setCheckFamily(false);
    setCheckPayment(false);
    setBuyerEmail("");
    setBuyerEmailConfirm("");
    setRedeemError(null);
    setWarrantyError(null);
    setEditingWarranty(false);
    void fetchRecord(false);
  }, [fetchRecord]);

  useEffect(() => {
    if (
      !record ||
      (record.status !== "waiting_fulfillment" &&
        record.status !== "validating" &&
        record.status !== "purchasing")
    ) {
      return;
    }

    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") {
        void fetchRecord(true);
      }
    }, AUTO_POLL_INTERVAL_MS);

    const onVisibility = () => {
      if (document.visibilityState === "visible") {
        void fetchRecord(true);
      }
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [record, fetchRecord]);

  useEffect(() => {
    if (cooldownLeft <= 0) return;
    const timer = window.setInterval(() => {
      setCooldownLeft((prev) => (prev <= 1 ? 0 : prev - 1));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [cooldownLeft]);

  useEffect(() => {
    return () => {
      if (screenshotPreview) {
        URL.revokeObjectURL(screenshotPreview);
      }
    };
  }, [screenshotPreview]);

  const slotEmailCheck = validateGmailInput(buyerEmail);
  const slotEmailsMatch =
    slotEmailCheck.valid &&
    buyerEmailConfirm.trim().length > 0 &&
    slotEmailCheck.normalized.toLowerCase() === buyerEmailConfirm.trim().toLowerCase();
  const preflightReady = checkFamily && checkPayment;

  const handleSlotPreSubmit = (event: FormEvent) => {
    event.preventDefault();
    setRedeemError(null);
    if (!preflightReady) {
      setRedeemError("请先勾选确认已完成上方两项前置自检。");
      return;
    }
    if (!slotEmailCheck.valid) {
      setRedeemError(slotEmailCheck.error || "请检查 Gmail 邮箱格式。");
      return;
    }
    if (!slotEmailsMatch) {
      setRedeemError("两次输入的 Gmail 邮箱不一致，请逐字母核对。");
      return;
    }
    setConfirmModalOpen(true);
  };

  const executeSlotRedeem = async () => {
    setSubmittingRedeem(true);
    setRedeemError(null);
    try {
      const updated = await redeemPublicCode(code, {
        buyer_email: slotEmailCheck.normalized,
        buyer_email_confirm: buyerEmailConfirm.trim(),
        preflight_confirmed: true,
      });
      setRecord(updated);
      setConfirmModalOpen(false);
      if (updated.status === "needs_manual_review") {
        pushToast("订单已转交人工核验处理，请留意后续进度", "info");
      } else {
        pushToast("提交成功，已进入自动拉人队列", "success");
      }
    } catch (err) {
      setConfirmModalOpen(false);
      if (err instanceof ApiError && err.errorCode === "CODE_ALREADY_REDEEMED") {
        pushToast("该兑换码已提交，已为您刷新最新进度", "info");
        await fetchRecord(false);
      } else {
        const message = err instanceof Error ? err.message : "提交失败，请稍后重试。";
        setRedeemError(message);
        pushToast(message, "error");
      }
    } finally {
      setSubmittingRedeem(false);
    }
  };

  const handleExclusiveRedeem = async (event: FormEvent) => {
    event.preventDefault();
    setRedeemError(null);
    if (!preflightReady) {
      setRedeemError("请先勾选确认已完成上方两项准备检查。");
      return;
    }
    setSubmittingRedeem(true);
    try {
      const updated = await redeemPublicCode(code, {
        preflight_confirmed: true,
      });
      setRecord(updated);
      if (updated.status === "needs_manual_review") {
        pushToast("订单已转交人工核验处理，请留意后续进度", "info");
      } else {
        pushToast("专属商店套餐邮箱已发出", "success");
      }
    } catch (err) {
      if (err instanceof ApiError && err.errorCode === "CODE_ALREADY_REDEEMED") {
        await fetchRecord(false);
      } else {
        const message = err instanceof Error ? err.message : "领取失败，请稍后重试。";
        setRedeemError(message);
        pushToast(message, "error");
      }
    } finally {
      setSubmittingRedeem(false);
    }
  };

  const handleManualRefresh = async () => {
    if (refreshing || cooldownLeft > 0) return;
    setRefreshing(true);
    try {
      const updated = await refreshPublicCode(code);
      setRecord(updated);
      setCooldownLeft(updated.cooldown_seconds && updated.cooldown_seconds > 0 ? updated.cooldown_seconds : 15);
      if (updated.status === "completed") {
        pushToast("家庭组邀请已发出，请查收 Gmail", "success");
      } else {
        pushToast("已刷新最新进度", "info");
      }
    } catch (err) {
      pushToast(err instanceof Error ? err.message : "刷新失败，请稍后重试", "error");
    } finally {
      setRefreshing(false);
    }
  };

  const handleSelectScreenshot = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0] || null;
    if (!file) return;
    const check = validateWarrantyScreenshot(file);
    if (!check.valid) {
      setWarrantyError(check.error || "图片不符合要求");
      return;
    }
    setWarrantyError(null);
    if (screenshotPreview) {
      URL.revokeObjectURL(screenshotPreview);
    }
    setScreenshotFile(file);
    setScreenshotPreview(URL.createObjectURL(file));
  };

  const clearScreenshot = () => {
    if (screenshotPreview) {
      URL.revokeObjectURL(screenshotPreview);
    }
    setScreenshotFile(null);
    setScreenshotPreview(null);
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
    }
  };

  const warrantyEmailCheck = validateGmailInput(adminEmail);
  const warrantyEmailsMatch =
    warrantyEmailCheck.valid &&
    adminEmailConfirm.trim().length > 0 &&
    warrantyEmailCheck.normalized.toLowerCase() === adminEmailConfirm.trim().toLowerCase();

  const handleWarrantySubmit = async (event: FormEvent) => {
    event.preventDefault();
    setWarrantyError(null);
    if (!warrantyEmailCheck.valid) {
      setWarrantyError(warrantyEmailCheck.error || "请检查主力 Gmail 格式。");
      return;
    }
    if (!warrantyEmailsMatch) {
      setWarrantyError("两次输入的主力 Gmail 不一致，请重新核对。");
      return;
    }
    const fileCheck = validateWarrantyScreenshot(screenshotFile);
    if (!fileCheck.valid || !screenshotFile) {
      setWarrantyError(fileCheck.error || "请上传邀请确认截图。");
      return;
    }

    setSubmittingWarranty(true);
    try {
      const formData = new FormData();
      formData.append("admin_email", warrantyEmailCheck.normalized);
      formData.append("admin_email_confirm", adminEmailConfirm.trim());
      formData.append("screenshot", screenshotFile);
      if (warrantyNote.trim()) {
        formData.append("note", warrantyNote.trim());
      }
      const updated = await submitPublicWarranty(code, formData);
      setRecord(updated);
      setEditingWarranty(false);
      clearScreenshot();
      pushToast("保修登记提交成功，已通知商家报备", "success");
    } catch (err) {
      const message = err instanceof Error ? err.message : "提交保修信息失败，请稍后重试。";
      setWarrantyError(message);
      pushToast(message, "error");
    } finally {
      setSubmittingWarranty(false);
    }
  };

  const isExclusive = record?.product_type === "account";
  const warrantySubmitted =
    record?.warranty?.status === "submitted" || record?.warranty?.status === "registered_upstream";
  const warrantyLocked = record?.warranty?.status === "registered_upstream";
  const activeStep = record ? resolveStepIndex(record.product_type, record.status, warrantySubmitted) : 0;
  const steps = isExclusive
    ? ["领取邮箱", "建组邀请", "登记保修"]
    : ["前置自检", "自动拉人", "查收邀请"];

  const accountCredentials =
    record && isExclusive && record.delivery
      ? record.delivery.credentials && record.delivery.credentials.length > 0
        ? record.delivery.credentials
        : record.delivery.account_text
        ? [{ label: "商店套餐邮箱", value: record.delivery.account_text }]
        : []
      : [];

  return (
    <div className="buyer-page">
      <div className="buyer-container">
        <header className="buyer-topbar">
          <Link to="/" className="brand-lockup">
            <span className="brand-mark" aria-hidden="true">
              <TicketCheck size={18} />
            </span>
            <span>VEO3 Ultra x20 自助提货</span>
          </Link>
          <Link to="/" className="btn btn-ghost btn-sm">
            <ArrowLeft size={15} />
            <span>换个兑换码</span>
          </Link>
        </header>

        {loading ? (
          <section className="card stack" aria-label="正在加载兑换码信息">
            <div className="row-between">
              <div className="skeleton" style={{ width: 96, height: 26 }} />
              <div className="skeleton" style={{ width: 84, height: 26 }} />
            </div>
            <div className="skeleton" style={{ width: "78%", height: 28 }} />
            <div className="skeleton" style={{ width: "100%", height: 50 }} />
            <div className="skeleton" style={{ width: "100%", height: 140 }} />
          </section>
        ) : notFoundMessage ? (
          <section className="card stack" style={{ textAlign: "center", padding: "36px 24px" }}>
            <AlertCircle size={36} style={{ color: "var(--down)", margin: "0 auto" }} />
            <div>
              <h1 className="card-title">未找到该兑换码</h1>
              <p className="card-subtitle">{notFoundMessage}</p>
            </div>
            <div className="row" style={{ justifyContent: "center" }}>
              <Link to="/" className="btn btn-primary">
                返回重新输入
              </Link>
            </div>
          </section>
        ) : loadError ? (
          <section className="card stack" style={{ textAlign: "center", padding: "36px 24px" }}>
            <AlertCircle size={36} style={{ color: "var(--down)", margin: "0 auto" }} />
            <div>
              <h1 className="card-title">暂时无法加载订单</h1>
              <p className="card-subtitle">{loadError}</p>
            </div>
            <div className="row" style={{ justifyContent: "center" }}>
              <button type="button" className="btn btn-primary" onClick={() => void fetchRecord(false)}>
                <RefreshCw size={15} />
                <span>重新加载</span>
              </button>
              <Link to="/" className="btn btn-secondary">
                返回首页
              </Link>
            </div>
          </section>
        ) : record ? (
          <>
            {/* Voucher Summary Card */}
            <section className="card stack">
              <div className="voucher-head">
                <div className="row-between">
                  <div className="row" style={{ gap: 8 }}>
                    <span className="badge badge-accent">{getChannelLabel(record.channel)}订单</span>
                    <CodeStatusBadge status={record.status} />
                  </div>
                </div>
                <h1 className="card-title" style={{ fontSize: 20 }}>
                  {record.sku_title}
                </h1>
              </div>

              <div className="voucher-meta-bar">
                <div className="voucher-code-pill">
                  <span>兑换码</span>
                  <span className="voucher-code-value">{record.code}</span>
                </div>
                <CopyButton text={record.code} label="复制兑换码" toastMessage="兑换码已复制" />
              </div>

              {record.status !== "revoked" && record.status !== "needs_manual_review" ? (
                <ol className="stepper" aria-label="履约步骤进度">
                  {steps.map((label, idx) => {
                    const isDone = idx < activeStep || (record.status === "completed" && idx <= activeStep);
                    const isCurrent = !isDone && idx === activeStep;
                    return (
                      <li
                        key={label}
                        className={`stepper-item ${isDone ? "is-done" : ""}`}
                        aria-current={isCurrent ? "step" : undefined}
                      >
                        <span className="stepper-num" aria-hidden="true">
                          {isDone ? <Check size={12} /> : idx + 1}
                        </span>
                        <span className="stepper-label">{label}</span>
                      </li>
                    );
                  })}
                </ol>
              ) : null}
            </section>

            {/* Revoked */}
            {record.status === "revoked" ? (
              <section className="card stack">
                <div className="callout callout-danger">
                  <AlertCircle size={18} className="callout-icon" />
                  <div>
                    <strong>该兑换码已作废</strong>
                    <div style={{ marginTop: 4 }}>
                      {record.buyer_notice || "当前兑换码已失效或对应订单已关闭，无法继续使用。"}
                    </div>
                  </div>
                </div>
                <p style={{ fontSize: 13.5, color: "var(--ink-3)" }}>{record.support_hint}</p>
              </section>
            ) : null}

            {/* Needs Manual Review (never leaks internal USD/balance details) */}
            {record.status === "needs_manual_review" ? (
              <section className="card stack">
                <div className="callout callout-warning">
                  <Clock size={18} className="callout-icon" />
                  <div>
                    <strong>订单正在人工核验处理中</strong>
                    <div style={{ marginTop: 4 }}>
                      {record.buyer_notice ||
                        "您的提货请求已安全记录并通知客服跟进处理，请收藏本页面链接，处理完成后状态会自动更新。"}
                    </div>
                  </div>
                </div>
                <div className="kv-grid">
                  <div className="kv-item">
                    <div className="kv-label">当前状态</div>
                    <div className="kv-value">客服人工核验中</div>
                  </div>
                  <div className="kv-item">
                    <div className="kv-label">最近更新时间</div>
                    <div className="kv-value tabular">{formatDateTime(record.updated_at)}</div>
                  </div>
                </div>
                <div className="row-between">
                  <span style={{ fontSize: 13, color: "var(--ink-3)" }}>{record.support_hint}</span>
                  <button
                    type="button"
                    className="btn btn-secondary btn-sm"
                    onClick={() => void fetchRecord(false)}
                  >
                    <RefreshCw size={15} />
                    <span>刷新状态</span>
                  </button>
                </div>
              </section>
            ) : null}

            {/* ===============================================================
                FLOW A: SHARED SLOT (ultra_shared_slot)
               =============================================================== */}
            {!isExclusive && record.status === "unused" ? (
              <section className="card stack">
                <div>
                  <h2 className="card-title">填写接收家庭组邀请的 Gmail</h2>
                  <p className="card-subtitle">
                    请先完成下方两项前置自检，再填写您的 Gmail。提交后系统将自动排队发邀请，通常 1~6 小时内完成。
                  </p>
                </div>

                <div className="stack-sm">
                  <label className={`check-card ${checkFamily ? "is-checked" : ""}`}>
                    <input
                      type="checkbox"
                      checked={checkFamily}
                      disabled={submittingRedeem}
                      onChange={(event) => setCheckFamily(event.target.checked)}
                    />
                    <div className="check-card-body">
                      <strong>我已确认当前 Gmail 未加入其他 Google 家庭组</strong>，且近 12 个月内未频繁切换家庭组（可打开{" "}
                      <a href="https://g.co/yourfamily" target="_blank" rel="noreferrer">
                        g.co/yourfamily <ExternalLink size={12} style={{ verticalAlign: "-1px" }} />
                      </a>{" "}
                      核实）。
                    </div>
                  </label>

                  <label className={`check-card ${checkPayment ? "is-checked" : ""}`}>
                    <input
                      type="checkbox"
                      checked={checkPayment}
                      disabled={submittingRedeem}
                      onChange={(event) => setCheckPayment(event.target.checked)}
                    />
                    <div className="check-card-body">
                      <strong>我已确认账号满足接收邀请地区要求</strong>（如曾配置过非目标区付款资料，可在{" "}
                      <a href="https://payments.google.com" target="_blank" rel="noreferrer">
                        payments.google.com <ExternalLink size={12} style={{ verticalAlign: "-1px" }} />
                      </a>{" "}
                      核对）。
                    </div>
                  </label>
                </div>

                <form onSubmit={handleSlotPreSubmit} className="stack">
                  <GmailDoubleInput
                    email={buyerEmail}
                    confirmEmail={buyerEmailConfirm}
                    onEmailChange={setBuyerEmail}
                    onConfirmEmailChange={setBuyerEmailConfirm}
                    disabled={submittingRedeem}
                    warningText="请务必逐字母核对接收邀请的 Gmail，提交后系统将直接发单且无法自行修改邮箱。"
                  />

                  {/* Real-time PreSubmitCheck card */}
                  <div className="presubmit-card" aria-live="polite">
                    <div className="presubmit-title">提交前实时核对</div>
                    <ul className="presubmit-list">
                      <li className={`presubmit-item ${preflightReady ? "is-pass" : ""}`}>
                        {preflightReady ? <CheckCircle2 size={14} /> : <Circle size={14} />}
                        <span>两项自检已勾选</span>
                      </li>
                      <li className={`presubmit-item ${slotEmailCheck.valid ? "is-pass" : ""}`}>
                        {slotEmailCheck.valid ? <CheckCircle2 size={14} /> : <Circle size={14} />}
                        <span>Gmail 格式有效</span>
                      </li>
                      <li className={`presubmit-item ${slotEmailsMatch ? "is-pass" : ""}`}>
                        {slotEmailsMatch ? <CheckCircle2 size={14} /> : <Circle size={14} />}
                        <span>两次输入完全一致</span>
                      </li>
                    </ul>
                  </div>

                  {redeemError ? (
                    <div className="callout callout-danger" role="alert">
                      <AlertCircle size={17} className="callout-icon" />
                      <div>{redeemError}</div>
                    </div>
                  ) : null}

                  <button
                    type="submit"
                    className={`btn btn-primary btn-lg btn-block ${submittingRedeem ? "is-loading" : ""}`}
                    disabled={!preflightReady || !slotEmailsMatch || submittingRedeem}
                    aria-busy={submittingRedeem}
                  >
                    {submittingRedeem ? (
                      <>
                        <Loader2 size={17} className="spin" />
                        <span>正在提交排单...</span>
                      </>
                    ) : (
                      <span>核对无误，下一步确认提交</span>
                    )}
                  </button>
                </form>
              </section>
            ) : null}

            {/* Shared Slot: Waiting / Processing */}
            {!isExclusive &&
            (record.status === "waiting_fulfillment" ||
              record.status === "validating" ||
              record.status === "purchasing") ? (
              <section className="card stack">
                <div className="callout callout-info">
                  <Clock size={18} className="callout-icon" />
                  <div>
                    <strong>已进入自动拉人队列，请耐心等待</strong>
                    <div style={{ marginTop: 4 }}>
                      {(() => {
                        const rawEta = (
                          record.fulfillment.eta_text || "预计 1~6 小时内发出家庭组邀请，最晚不超过 24 小时"
                        ).trim();
                        const etaText = /[。！？.!?]$/.test(rawEta) ? rawEta : `${rawEta}。`;
                        return `${etaText}本页面每 30 秒自动刷新进度，您也可以收藏链接随时回来查看。`;
                      })()}
                    </div>
                  </div>
                </div>

                <div className="kv-grid">
                  <div className="kv-item">
                    <div className="kv-label">接收邀请 Gmail</div>
                    <div className="kv-value mono">{record.buyer_email_masked || "-"}</div>
                  </div>
                  <div className="kv-item">
                    <div className="kv-label">已等待时长</div>
                    <div className="kv-value tabular">{formatElapsedMinutes(record.fulfillment.elapsed_minutes)}</div>
                  </div>
                  <div className="kv-item">
                    <div className="kv-label">提交时间</div>
                    <div className="kv-value tabular">{formatDateTime(record.fulfillment.submitted_at)}</div>
                  </div>
                  <div className="kv-item">
                    <div className="kv-label">最近同步时间</div>
                    <div className="kv-value tabular">{formatDateTime(record.fulfillment.last_polled_at)}</div>
                  </div>
                </div>

                <div className="row-between">
                  <span style={{ fontSize: 13, color: "var(--ink-3)" }}>{record.support_hint}</span>
                  <button
                    type="button"
                    className={`btn btn-secondary ${refreshing ? "is-loading" : ""}`}
                    disabled={refreshing || cooldownLeft > 0}
                    aria-busy={refreshing}
                    onClick={() => void handleManualRefresh()}
                  >
                    <RefreshCw size={15} className={refreshing ? "spin" : ""} />
                    <span>
                      {refreshing
                        ? "正在同步..."
                        : cooldownLeft > 0
                        ? `刷新进度 (${cooldownLeft}s)`
                        : "立即刷新进度"}
                    </span>
                  </button>
                </div>
              </section>
            ) : null}

            {/* Shared Slot: Completed */}
            {!isExclusive && record.status === "completed" ? (
              <section className="card stack">
                <div className="callout callout-success">
                  <MailCheck size={19} className="callout-icon" />
                  <div>
                    <strong>家庭组邀请已发出，请前往 Gmail 查收</strong>
                    <div style={{ marginTop: 4 }}>
                      请登录 <span className="mono">{record.buyer_email_masked || "您提交的 Gmail"}</span>，查收来自 Google 的家庭组邀请邮件（如收件箱未见请检查垃圾箱或社交/推广分类）。
                    </div>
                  </div>
                </div>

                <div className="kv-grid">
                  <div className="kv-item">
                    <div className="kv-label">接收邮箱</div>
                    <div className="kv-value mono">{record.buyer_email_masked || "-"}</div>
                  </div>
                  <div className="kv-item">
                    <div className="kv-label">发邀完成时间</div>
                    <div className="kv-value tabular">{formatDateTime(record.fulfillment.completed_at)}</div>
                  </div>
                </div>

                <div className="inset-panel stack-sm">
                  <strong style={{ fontSize: 13.5 }}>接收邀请步骤</strong>
                  <ol className="guide-steps">
                    <li>打开 Gmail 客户端或网页版，找到标题含 “Google 家庭组” / “Join family group” 的邮件。</li>
                    <li>点击邮件里的 <strong>接受邀请</strong> 并按页面提示确认加入即可生效。</li>
                    <li>如遇到提示地区或家庭组受限，请截屏联系原下单平台客服协助处理。</li>
                  </ol>
                </div>

                <div className="row">
                  <a
                    href="https://mail.google.com"
                    target="_blank"
                    rel="noreferrer"
                    className="btn btn-primary btn-block"
                  >
                    <span>前往 Gmail 查收邀请</span>
                    <ExternalLink size={15} />
                  </a>
                </div>
              </section>
            ) : null}

            {/* ===============================================================
                FLOW B: EXCLUSIVE ADMIN (ultra_exclusive_admin)
               =============================================================== */}
            {isExclusive && record.status === "unused" ? (
              <section className="card stack">
                <div>
                  <h2 className="card-title">领取专属商店套餐邮箱</h2>
                  <p className="card-subtitle">
                    独享整组采用反向邀请方式：先点击下方领取商店套餐邮箱，再用您自己的主力 Gmail 创建家庭组并邀请该邮箱加入。
                  </p>
                </div>

                <div className="stack-sm">
                  <label className={`check-card ${checkFamily ? "is-checked" : ""}`}>
                    <input
                      type="checkbox"
                      checked={checkFamily}
                      disabled={submittingRedeem}
                      onChange={(event) => setCheckFamily(event.target.checked)}
                    />
                    <div className="check-card-body">
                      <strong>我已准备好作为家庭组管理员的主力 Gmail</strong>，且当前未加入其他家庭组（可前往{" "}
                      <a href="https://g.co/yourfamily" target="_blank" rel="noreferrer">
                        g.co/yourfamily <ExternalLink size={12} style={{ verticalAlign: "-1px" }} />
                      </a>{" "}
                      核实）。
                    </div>
                  </label>

                  <label className={`check-card ${checkPayment ? "is-checked" : ""}`}>
                    <input
                      type="checkbox"
                      checked={checkPayment}
                      disabled={submittingRedeem}
                      onChange={(event) => setCheckPayment(event.target.checked)}
                    />
                    <div className="check-card-body">
                      <strong>我已知晓邀请商店邮箱加入家庭组后</strong>，切勿将其移出家庭组，并需在下方提交主力 Gmail 与邀请确认截图以完成保修报备。
                    </div>
                  </label>
                </div>

                {redeemError ? (
                  <div className="callout callout-danger" role="alert">
                    <AlertCircle size={17} className="callout-icon" />
                    <div>{redeemError}</div>
                  </div>
                ) : null}

                <form onSubmit={handleExclusiveRedeem}>
                  <button
                    type="submit"
                    className={`btn btn-primary btn-lg btn-block ${submittingRedeem ? "is-loading" : ""}`}
                    disabled={!preflightReady || submittingRedeem}
                    aria-busy={submittingRedeem}
                  >
                    {submittingRedeem ? (
                      <>
                        <Loader2 size={17} className="spin" />
                        <span>正在为您出货...</span>
                      </>
                    ) : (
                      <>
                        <ShieldCheck size={18} />
                        <span>确认条件，立即领取商店套餐邮箱</span>
                      </>
                    )}
                  </button>
                </form>
              </section>
            ) : null}

            {/* Exclusive: Delivered (Pending Warranty or Completed) */}
            {isExclusive &&
            (record.status === "delivered_pending_warranty" || record.status === "completed") ? (
              <>
                <section className="card stack">
                  <div className="row-between">
                    <h2 className="card-title">专属商店套餐邮箱</h2>
                    {record.delivery?.delivered_at ? (
                      <span className="field-hint tabular">
                        出货时间：{formatDateTime(record.delivery.delivered_at)}
                      </span>
                    ) : null}
                  </div>

                  {accountCredentials.length > 0 ? (
                    <div className="stack-sm">
                      {accountCredentials.map((cred) => (
                        <div key={`${cred.label}-${cred.value}`} className="credential-box">
                          <div className="grow">
                            <div className="credential-label">{cred.label}</div>
                            <div className="credential-value">{cred.value}</div>
                          </div>
                          <CopyButton
                            text={cred.value}
                            label="一键复制"
                            toastMessage={`已复制${cred.label}`}
                            variant="primary"
                          />
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div className="callout callout-info">
                      <ShieldCheck size={17} className="callout-icon" />
                      <div>套餐信息已出货，如未显示请点击下方刷新或联系客服核对。</div>
                    </div>
                  )}

                  <div className="inset-panel stack-sm">
                    <strong style={{ fontSize: 13.5 }}>反向邀请建组步骤</strong>
                    <ol className="guide-steps">
                      <li>
                        用您自己的<strong>主力 Gmail</strong>打开{" "}
                        <a href="https://g.co/yourfamily" target="_blank" rel="noreferrer">
                          g.co/yourfamily <ExternalLink size={12} style={{ verticalAlign: "-1px" }} />
                        </a>{" "}
                        创建 Google 家庭组。
                      </li>
                      <li>在家庭组中点击“发送邀请”，粘贴上方复制的<strong>商店套餐邮箱</strong>并发出邀请。</li>
                      <li>截取已发出邀请的确认页面，在下方向我们提交您的<strong>主力 Gmail 与确认截图</strong>完成保修登记。</li>
                    </ol>
                  </div>
                </section>

                {/* Warranty Section */}
                <section className="card stack">
                  <div className="row-between">
                    <div>
                      <h2 className="card-title">保修登记与报备</h2>
                      <p className="card-subtitle">
                        提交后系统会自动通知商家向官方管理员报备您的主力 Gmail，确保售后全保生效。
                      </p>
                    </div>
                    <WarrantyStatusBadge status={record.warranty?.status || "pending"} />
                  </div>

                  {warrantySubmitted && !editingWarranty ? (
                    <div className="stack">
                      <div className="callout callout-success">
                        <CheckCircle2 size={18} className="callout-icon" />
                        <div>
                          <strong>
                            {warrantyLocked
                              ? "商家已向官方完成保修报备"
                              : "保修信息已提交，商家报备处理中"}
                          </strong>
                          <div style={{ marginTop: 4 }}>
                            已登记主力 Gmail：<span className="mono">{record.warranty?.admin_email_masked || "-"}</span>
                          </div>
                        </div>
                      </div>

                      <div className="kv-grid">
                        <div className="kv-item">
                          <div className="kv-label">登记时间</div>
                          <div className="kv-value tabular">{formatDateTime(record.warranty?.submitted_at)}</div>
                        </div>
                        <div className="kv-item">
                          <div className="kv-label">邀请确认截图</div>
                          <div className="kv-value">{record.warranty?.has_screenshot ? "已安全上传" : "未上传"}</div>
                        </div>
                      </div>

                      {record.warranty?.note ? (
                        <div className="inset-panel" style={{ fontSize: 13.5, color: "var(--ink-2)" }}>
                          <span style={{ color: "var(--ink-3)" }}>备注：</span>
                          {record.warranty.note}
                        </div>
                      ) : null}

                      {!warrantyLocked ? (
                        <div className="row" style={{ justifyContent: "flex-end" }}>
                          <button
                            type="button"
                            className="btn btn-secondary btn-sm"
                            onClick={() => setEditingWarranty(true)}
                          >
                            重新提交 / 更新截图
                          </button>
                        </div>
                      ) : null}
                    </div>
                  ) : (
                    <form onSubmit={handleWarrantySubmit} className="stack">
                      <GmailDoubleInput
                        email={adminEmail}
                        confirmEmail={adminEmailConfirm}
                        onEmailChange={setAdminEmail}
                        onConfirmEmailChange={setAdminEmailConfirm}
                        primaryLabel="您的主力 Gmail（家庭组管理员邮箱）"
                        confirmLabel="再次手动输入主力 Gmail 确认"
                        warningText="请填写创建家庭组的主力 Gmail，不要填成上面复制的商店套餐邮箱，并务必核对拼写无误。"
                        disabled={submittingWarranty}
                      />

                      <div className="field-group">
                        <label className="field-label" htmlFor="warranty-screenshot-input">
                          <span>邀请确认截图</span>
                          <span className="field-hint">JPG / PNG / WEBP，5MB 以内</span>
                        </label>
                        <input
                          ref={fileInputRef}
                          id="warranty-screenshot-input"
                          type="file"
                          accept="image/jpeg,image/png,image/webp"
                          className="sr-only"
                          disabled={submittingWarranty}
                          onChange={handleSelectScreenshot}
                        />
                        <div className="upload-zone">
                          {screenshotPreview ? (
                            <div>
                              <img
                                src={screenshotPreview}
                                alt="已选择的家庭组邀请确认截图预览"
                                className="upload-preview-img"
                              />
                              <div className="upload-preview-bar">
                                <span className="upload-filename">{screenshotFile?.name}</span>
                                <div className="row" style={{ gap: 6 }}>
                                  <button
                                    type="button"
                                    className="btn btn-secondary btn-sm"
                                    disabled={submittingWarranty}
                                    onClick={() => fileInputRef.current?.click()}
                                  >
                                    重选图片
                                  </button>
                                  <button
                                    type="button"
                                    className="btn btn-danger btn-sm"
                                    disabled={submittingWarranty}
                                    onClick={clearScreenshot}
                                  >
                                    <Trash2 size={14} />
                                    <span>移除</span>
                                  </button>
                                </div>
                              </div>
                            </div>
                          ) : (
                            <button
                              type="button"
                              className="upload-trigger"
                              disabled={submittingWarranty}
                              onClick={() => fileInputRef.current?.click()}
                            >
                              <ImagePlus size={26} className="upload-icon" />
                              <span style={{ fontWeight: 600, fontSize: 14.5 }}>
                                点击选择或拍摄邀请确认截图
                              </span>
                              <span className="field-hint">需清晰看到已向商店邮箱发出家庭组邀请</span>
                            </button>
                          )}
                        </div>
                      </div>

                      <div className="field-group">
                        <label className="field-label" htmlFor="warranty-note-input">
                          <span>补充备注（选填）</span>
                          <span className="field-hint tabular">{warrantyNote.length}/300</span>
                        </label>
                        <input
                          id="warranty-note-input"
                          type="text"
                          maxLength={300}
                          enterKeyHint="done"
                          className="input"
                          placeholder="例如：已发送邀请，麻烦帮忙登记保修"
                          value={warrantyNote}
                          onChange={(event) => setWarrantyNote(event.target.value)}
                          disabled={submittingWarranty}
                        />
                      </div>

                      {warrantyError ? (
                        <div className="callout callout-danger" role="alert">
                          <AlertCircle size={17} className="callout-icon" />
                          <div>{warrantyError}</div>
                        </div>
                      ) : null}

                      <div className="row">
                        {editingWarranty ? (
                          <button
                            type="button"
                            className="btn btn-secondary"
                            onClick={() => setEditingWarranty(false)}
                            disabled={submittingWarranty}
                          >
                            取消修改
                          </button>
                        ) : null}
                        <button
                          type="submit"
                          className={`btn btn-primary btn-lg grow ${submittingWarranty ? "is-loading" : ""}`}
                          disabled={!warrantyEmailsMatch || !screenshotFile || submittingWarranty}
                          aria-busy={submittingWarranty}
                        >
                          {submittingWarranty ? (
                            <>
                              <Loader2 size={17} className="spin" />
                              <span>正在上传保修信息...</span>
                            </>
                          ) : (
                            <>
                              <UploadCloud size={18} />
                              <span>提交保修登记</span>
                            </>
                          )}
                        </button>
                      </div>
                    </form>
                  )}
                </section>
              </>
            ) : null}
          </>
        ) : null}

        <footer className="footer-links">
          <span>VEO3 Ultra x20 自助提货与进度查询</span>
          <span aria-hidden="true">·</span>
          <Link to="/">返回首页</Link>
        </footer>
      </div>

      <ConfirmDialog
        open={confirmModalOpen}
        title="最后确认一次接收邮箱"
        confirmLabel="确认无误，立即提交"
        cancelLabel="返回再检查"
        busy={submittingRedeem}
        onConfirm={() => void executeSlotRedeem()}
        onClose={() => setConfirmModalOpen(false)}
      >
        <p style={{ fontSize: 13.5, color: "var(--ink-2)" }}>
          提交后系统将立即向以下 Gmail 发送家庭组拉人排单，提交后无法自行修改，请逐字母核对：
        </p>
        <div className="credential-box credential-box-warn">
          <span className="credential-value" style={{ fontSize: 17 }}>
            {slotEmailCheck.normalized}
          </span>
        </div>
      </ConfirmDialog>
    </div>
  );
}
