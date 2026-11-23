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
import { BuyerWallpaper } from "../components/BuyerWallpaper";
import { CopyButton } from "../components/CopyButton";
import { GmailDoubleInput } from "../components/GmailDoubleInput";
import { ToneBadge } from "../components/StatusBadge";
import { useToast } from "../components/ToastProvider";
import type { CodeStatus, ProductType, PublicCodeResponse, WarrantyStatus } from "../types/api";
import {
  formatDateTime,
  formatElapsedMinutes,
  getChannelLabel,
  getSkuShortLabel,
  type Tone,
} from "../utils/format";
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

function getBuyerStatusMeta(record: PublicCodeResponse): { label: string; tone: Tone; pulse?: boolean } {
  if (record.product_type === "account") {
    if (record.status === "unused") {
      return { label: "待领商店邮箱", tone: "neutral" };
    }
    if (record.status === "validating" || record.status === "purchasing") {
      return { label: "正在出货", tone: "info", pulse: true };
    }
    if (record.status === "delivered_pending_warranty") {
      return { label: "已发商店邮箱 · 待交保修", tone: "info" };
    }
    if (record.status === "completed") {
      if (record.warranty?.status === "registered_upstream") {
        return { label: "已完成 · 已向官方报备", tone: "success" };
      }
      if (record.warranty?.status === "submitted") {
        return { label: "已交保修 · 等商家报备", tone: "warning" };
      }
      return { label: "已完成", tone: "success" };
    }
  }
  switch (record.status) {
    case "unused":
      return { label: "待填邮箱", tone: "neutral" };
    case "validating":
      return { label: "核对中", tone: "info", pulse: true };
    case "purchasing":
      return { label: "正在下单", tone: "info", pulse: true };
    case "waiting_fulfillment":
      return { label: "排队拉人中", tone: "warning", pulse: true };
    case "delivered_pending_warranty":
      return { label: "已发商店邮箱 · 待交保修", tone: "info" };
    case "completed":
      return { label: "邀请已发出", tone: "success" };
    case "needs_manual_review":
      return { label: "人工处理中", tone: "warning" };
    case "revoked":
      return { label: "已作废", tone: "neutral" };
  }
}

function getBuyerWarrantyBadgeMeta(status: WarrantyStatus | undefined): { label: string; tone: Tone } {
  switch (status) {
    case "submitted":
      return { label: "已交保修 · 等商家报备", tone: "warning" };
    case "registered_upstream":
      return { label: "商家已向官方报备", tone: "success" };
    default:
      return { label: "待交保修", tone: "warning" };
  }
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
  const [submittingRedeem, setSubmittingRedeem] = useState(false);
  const [redeemError, setRedeemError] = useState<string | null>(null);

  const [refreshing, setRefreshing] = useState(false);
  const [cooldownLeft, setCooldownLeft] = useState(0);

  const [editingWarranty, setEditingWarranty] = useState(false);
  const [adminEmail, setAdminEmail] = useState("");
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
          setNotFoundMessage(err.message || "没找到这个兑换码，请核对一下拼写。");
          setRecord(null);
        } else if (!silent) {
          setLoadError(err instanceof Error ? err.message : "订单加载失败，请稍后再试。");
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
  const preflightReady = checkFamily && checkPayment;

  const handleSlotSubmit = async (event: FormEvent) => {
    event.preventDefault();
    setRedeemError(null);
    if (!preflightReady) {
      setRedeemError("请先勾选上面的两项提货前确认。");
      return;
    }
    if (!slotEmailCheck.valid) {
      setRedeemError(slotEmailCheck.error || "请检查一下 Gmail 拼写对不对。");
      return;
    }
    setSubmittingRedeem(true);
    try {
      const updated = await redeemPublicCode(code, {
        buyer_email: slotEmailCheck.normalized,
        preflight_confirmed: true,
      });
      setRecord(updated);
      if (updated.status === "needs_manual_review") {
        pushToast("已转人工处理，收藏本页随时看进度", "info");
      } else {
        pushToast("提交成功，已经开始排队拉人", "success");
      }
    } catch (err) {
      if (err instanceof ApiError && err.errorCode === "CODE_ALREADY_REDEEMED") {
        pushToast("这个码已经提过货了，已帮你刷新进度", "info");
        await fetchRecord(false);
      } else {
        const message = err instanceof Error ? err.message : "提交没成功，请稍后再试。";
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
      setRedeemError("请先勾选上面的两项提货前确认。");
      return;
    }
    setSubmittingRedeem(true);
    try {
      const updated = await redeemPublicCode(code, {
        preflight_confirmed: true,
      });
      setRecord(updated);
      if (updated.status === "needs_manual_review") {
        pushToast("已转人工处理，收藏本页随时看进度", "info");
      } else {
        pushToast("商店套餐邮箱领好了，按下面步骤建组就行", "success");
      }
    } catch (err) {
      if (err instanceof ApiError && err.errorCode === "CODE_ALREADY_REDEEMED") {
        await fetchRecord(false);
      } else {
        const message = err instanceof Error ? err.message : "领取没成功，请稍后再试。";
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
      setCooldownLeft(updated.cooldown_seconds ?? 15);
      if (updated.status === "completed") {
        pushToast("家庭组邀请发出来了，快去 Gmail 查收", "success");
      } else {
        pushToast("进度已刷新", "info");
      }
    } catch (err) {
      pushToast(err instanceof Error ? err.message : "刷新没成功，稍后再试", "error");
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
  const isExclusive = record?.product_type === "account";
  const storeEmail =
    record && isExclusive && record.delivery
      ? record.delivery.account_text ||
        record.delivery.credentials?.find((item) => item.value)?.value ||
        ""
      : "";
  const warrantyUsesStoreEmail =
    Boolean(storeEmail) &&
    adminEmail.trim().length > 0 &&
    adminEmail.trim().toLowerCase() === storeEmail.trim().toLowerCase();

  const handleWarrantySubmit = async (event: FormEvent) => {
    event.preventDefault();
    setWarrantyError(null);
    if (warrantyUsesStoreEmail) {
      setWarrantyError("你填成了商店套餐邮箱，这里要填你自己建家庭组用的主力 Gmail。");
      return;
    }
    if (!warrantyEmailCheck.valid) {
      setWarrantyError(warrantyEmailCheck.error || "请检查一下主力 Gmail 拼写对不对。");
      return;
    }
    const fileCheck = validateWarrantyScreenshot(screenshotFile);
    if (!fileCheck.valid || !screenshotFile) {
      setWarrantyError(fileCheck.error || "请先上传家庭组邀请截图。");
      return;
    }

    setSubmittingWarranty(true);
    try {
      const formData = new FormData();
      formData.append("admin_email", warrantyEmailCheck.normalized);
      formData.append("screenshot", screenshotFile);
      if (warrantyNote.trim()) {
        formData.append("note", warrantyNote.trim());
      }
      const updated = await submitPublicWarranty(code, formData);
      setRecord(updated);
      setEditingWarranty(false);
      clearScreenshot();
      pushToast("保修交好了，已通知商家去官方报备", "success");
    } catch (err) {
      const message = err instanceof Error ? err.message : "保修提交没成功，请稍后再试。";
      setWarrantyError(message);
      pushToast(message, "error");
    } finally {
      setSubmittingWarranty(false);
    }
  };

  const warrantySubmitted =
    record?.warranty?.status === "submitted" || record?.warranty?.status === "registered_upstream";
  const warrantyLocked = record?.warranty?.status === "registered_upstream";
  const activeStep = record ? resolveStepIndex(record.product_type, record.status, warrantySubmitted) : 0;
  const steps = isExclusive
    ? ["领邮箱", "建组邀请", "交保修"]
    : ["填邮箱", "排队拉人", "收邀请"];

  const accountCredentials =
    record && isExclusive && record.delivery
      ? record.delivery.credentials && record.delivery.credentials.length > 0
        ? record.delivery.credentials
        : record.delivery.account_text
        ? [{ label: "商店套餐邮箱", value: record.delivery.account_text }]
        : []
      : [];
  const extraCredentials = accountCredentials.filter(
    (item) => item.value.trim().toLowerCase() !== storeEmail.trim().toLowerCase()
  );
  const buyerStatusMeta = record ? getBuyerStatusMeta(record) : null;
  const buyerWarrantyBadge = getBuyerWarrantyBadgeMeta(record?.warranty?.status);

  return (
    <div className="buyer-page">
      <BuyerWallpaper variant="detail" />
      <div className="buyer-container">
        <header className="buyer-topbar">
          <Link to="/" className="brand-lockup">
            <span className="brand-mark" aria-hidden="true">
              <TicketCheck size={18} />
            </span>
            <span>Google AI Ultra 20x 自助提货</span>
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
                    <span
                      className={`sku-tag ${
                        isExclusive ? "sku-tag-exclusive" : "sku-tag-shared"
                      }`}
                    >
                      {getSkuShortLabel(record.sku_key)}
                    </span>
                    {buyerStatusMeta ? (
                      <ToneBadge
                        label={buyerStatusMeta.label}
                        tone={buyerStatusMeta.tone}
                        pulse={Boolean(buyerStatusMeta.pulse)}
                      />
                    ) : null}
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
                <ol className="stepper" aria-label="提货进度">
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
                    <strong>这个兑换码已经作废了</strong>
                    <div style={{ marginTop: 4 }}>
                      {record.buyer_notice || "这个码已失效或订单已关闭，不能再用了。"}
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
                    <strong>客服正在人工处理这单</strong>
                    <div style={{ marginTop: 4 }}>
                      {record.buyer_notice ||
                        "你的提货信息已经记下来并通知客服跟进了，收藏本页链接，处理完这里会自动更新。"}
                    </div>
                  </div>
                </div>
                <div className="kv-grid">
                  <div className="kv-item">
                    <div className="kv-label">当前状态</div>
                    <div className="kv-value">人工跟进中</div>
                  </div>
                  <div className="kv-item">
                    <div className="kv-label">最近更新</div>
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
                    <span>刷一下状态</span>
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
                  <h2 className="card-title">填你收家庭组邀请的 Gmail</h2>
                  <p className="card-subtitle">
                    先勾选下面两项确认，再填你的 Gmail。提交后一般 1~6 小时拉人，最晚 24 小时。
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
                      <strong>这个 Gmail 现在没在别的 Google 家庭组里</strong>，而且近 12 个月没频繁换过组（可点{" "}
                      <a href="https://g.co/yourfamily" target="_blank" rel="noreferrer">
                        g.co/yourfamily <ExternalLink size={12} style={{ verticalAlign: "-1px" }} />
                      </a>{" "}
                      看一眼）。
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
                      <strong>账号地区没问题</strong>（如果以前绑过其他地区付款资料，可点{" "}
                      <a href="https://payments.google.com" target="_blank" rel="noreferrer">
                        payments.google.com <ExternalLink size={12} style={{ verticalAlign: "-1px" }} />
                      </a>{" "}
                      检查一下）。
                    </div>
                  </label>
                </div>

                <form onSubmit={(event) => void handleSlotSubmit(event)} className="stack">
                  <GmailDoubleInput
                    email={buyerEmail}
                    onEmailChange={setBuyerEmail}
                    disabled={submittingRedeem}
                    primaryLabel="收家庭组邀请的 Gmail"
                    warningText="别输错邮箱！提交后会立刻开始安排拉人，改不了邮箱，点提交前看清拼写。"
                  />

                  <div className="presubmit-card" aria-live="polite">
                    <div className="presubmit-title">提交前看一眼</div>
                    <ul className="presubmit-list">
                      <li className={`presubmit-item ${preflightReady ? "is-pass" : ""}`}>
                        {preflightReady ? <CheckCircle2 size={14} /> : <Circle size={14} />}
                        <span>两项确认已勾选</span>
                      </li>
                      <li className={`presubmit-item ${slotEmailCheck.valid ? "is-pass" : ""}`}>
                        {slotEmailCheck.valid ? <CheckCircle2 size={14} /> : <Circle size={14} />}
                        <span>Gmail 拼写格式没问题</span>
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
                    disabled={!preflightReady || !slotEmailCheck.valid || submittingRedeem}
                    aria-busy={submittingRedeem}
                  >
                    {submittingRedeem ? (
                      <>
                        <Loader2 size={17} className="spin" />
                        <span>正在提交...</span>
                      </>
                    ) : (
                      <span>核对好了，提交提货</span>
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
                    <strong>已经排上队了，等发邀请就行</strong>
                    <div style={{ marginTop: 4 }}>
                      一般 1~6 小时内拉人，最晚 24 小时。页面会自动刷新进度，建议收藏链接随时回来看。
                    </div>
                  </div>
                </div>

                <div className="kv-grid">
                  <div className="kv-item">
                    <div className="kv-label">收邀请的 Gmail</div>
                    <div className="kv-value mono">{record.buyer_email_masked || "-"}</div>
                  </div>
                  <div className="kv-item">
                    <div className="kv-label">已经等了</div>
                    <div className="kv-value tabular">{formatElapsedMinutes(record.fulfillment.elapsed_minutes)}</div>
                  </div>
                  <div className="kv-item">
                    <div className="kv-label">提交时间</div>
                    <div className="kv-value tabular">{formatDateTime(record.fulfillment.submitted_at)}</div>
                  </div>
                  <div className="kv-item">
                    <div className="kv-label">上次查进度</div>
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
                        ? "正在查..."
                        : cooldownLeft > 0
                        ? `稍后再查 (${cooldownLeft}s)`
                        : "手动查一下进度"}
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
                    <strong>家庭组邀请发出来了，快去 Gmail 查收</strong>
                    <div style={{ marginTop: 4 }}>
                      打开 <span className="mono">{record.buyer_email_masked || "你填的 Gmail"}</span>，找来自 Google 的家庭组邀请邮件（收件箱没看到的话翻一下垃圾箱或推广分类）。
                    </div>
                  </div>
                </div>

                <div className="kv-grid">
                  <div className="kv-item">
                    <div className="kv-label">收邀请的 Gmail</div>
                    <div className="kv-value mono">{record.buyer_email_masked || "-"}</div>
                  </div>
                  <div className="kv-item">
                    <div className="kv-label">发邀请时间</div>
                    <div className="kv-value tabular">{formatDateTime(record.fulfillment.completed_at)}</div>
                  </div>
                </div>

                <div className="inset-panel stack-sm">
                  <strong style={{ fontSize: 13.5 }}>怎么点接受邀请</strong>
                  <ol className="guide-steps">
                    <li>打开 Gmail，找到标题带 “Google 家庭组” 或 “Join family group” 的邮件。</li>
                    <li>点里面的 <strong>接受邀请</strong>，按提示点确认加入就生效了。</li>
                    <li>如果弹出地区或家庭组限制提示，直接截图找下单店铺客服帮你处理。</li>
                  </ol>
                </div>

                <div className="row">
                  <a
                    href="https://mail.google.com"
                    target="_blank"
                    rel="noreferrer"
                    className="btn btn-primary btn-block"
                  >
                    <span>去 Gmail 收邀请</span>
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
                  <h2 className="card-title">先领商店套餐邮箱</h2>
                  <p className="card-subtitle">
                    独享整组是你自己当管理员：先点下面领一个商店套餐邮箱，再用你的主力 Gmail 建家庭组把它拉进去，最后传截图交保修。
                  </p>
                </div>

                <div className="exclusive-steps-card">
                  <strong style={{ fontSize: 13.5 }}>一共就 3 步，很快</strong>
                  <ol className="guide-steps">
                    <li>点下面按钮，秒出你的专属<strong>商店套餐邮箱</strong>。</li>
                    <li>用你自己的<strong>主力 Gmail</strong> 建 Google 家庭组，把这个商店邮箱拉进组。</li>
                    <li>截一张「已发出邀请」的图，回到本页填你的主力 Gmail 和截图交保修。</li>
                  </ol>
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
                      <strong>我准备好了当管理员的主力 Gmail</strong>，而且它现在没在别的家庭组里（点{" "}
                      <a href="https://g.co/yourfamily" target="_blank" rel="noreferrer">
                        g.co/yourfamily <ExternalLink size={12} style={{ verticalAlign: "-1px" }} />
                      </a>{" "}
                      可以看）。
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
                      <strong>我知道把商店邮箱拉进家庭组后别踢出组</strong>，发完邀请回来传截图交保修就行。
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
                        <span>正在领商店邮箱...</span>
                      </>
                    ) : (
                      <>
                        <ShieldCheck size={18} />
                        <span>准备好了，马上领商店邮箱</span>
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
                    <h2 className="card-title">第 1 步：把商店套餐邮箱拉进家庭组</h2>
                    {record.delivery?.delivered_at ? (
                      <span className="field-hint tabular">
                        领号时间：{formatDateTime(record.delivery.delivered_at)}
                      </span>
                    ) : null}
                  </div>

                  {storeEmail ? (
                    <div className="store-email-box">
                      <div className="stack-sm grow">
                        <div className="email-line">
                          <span className="email-role-tag email-role-tag-store">商店邮箱</span>
                          <span className="field-hint">用来拉进你的家庭组，别填到下面保修框里</span>
                        </div>
                        <div className="credential-value">{storeEmail}</div>
                      </div>
                      <CopyButton
                        text={storeEmail}
                        label="复制商店邮箱"
                        toastMessage="商店套餐邮箱已复制"
                        variant="primary"
                      />
                    </div>
                  ) : (
                    <div className="callout callout-info">
                      <ShieldCheck size={17} className="callout-icon" />
                      <div>商店邮箱已发出，如果没显示出来请刷新页面或联系客服。</div>
                    </div>
                  )}

                  {extraCredentials.length > 0 ? (
                    <div className="stack-sm">
                      {extraCredentials.map((cred) => (
                        <div key={`${cred.label}-${cred.value}`} className="credential-box">
                          <div className="grow">
                            <div className="credential-label">{cred.label}</div>
                            <div className="credential-value">{cred.value}</div>
                          </div>
                          <CopyButton
                            text={cred.value}
                            label="复制"
                            toastMessage={`已复制${cred.label}`}
                            variant="secondary"
                          />
                        </div>
                      ))}
                    </div>
                  ) : null}

                  <div className="exclusive-steps-card">
                    <strong style={{ fontSize: 13.5 }}>第 2 步：建家庭组并邀请商店邮箱</strong>
                    <ol className="guide-steps">
                      <li>
                        用你自己的<strong>主力 Gmail</strong> 打开{" "}
                        <a href="https://g.co/yourfamily" target="_blank" rel="noreferrer">
                          g.co/yourfamily <ExternalLink size={12} style={{ verticalAlign: "-1px" }} />
                        </a>{" "}
                        建好 Google 家庭组。
                      </li>
                      <li>在家庭组里点「发送邀请」，把上面复制的<strong>商店邮箱</strong>贴进去发邀请。</li>
                      <li>截一张「已发出邀请」的页面图，回到下面填<strong>你自己的主力 Gmail</strong> 并传图交保修。</li>
                    </ol>
                  </div>
                </section>

                {/* Warranty Section */}
                <section className="card stack">
                  <div className="row-between">
                    <div style={{ flex: "1 1 240px", minWidth: 0 }}>
                      <h2 className="card-title">第 3 步：提交保修信息</h2>
                      <p className="card-subtitle">
                        发完邀请回来把主力 Gmail 和截图交上去，商家会帮你在官方做好全保报备。
                      </p>
                    </div>
                    <ToneBadge label={buyerWarrantyBadge.label} tone={buyerWarrantyBadge.tone} />
                  </div>

                  {warrantySubmitted && !editingWarranty ? (
                    <div className="stack">
                      <div className={`callout ${warrantyLocked ? "callout-success" : "callout-info"}`}>
                        <CheckCircle2 size={18} className="callout-icon" />
                        <div>
                          <strong>
                            {warrantyLocked
                              ? "商家已向官方报备完成，全保已生效"
                              : "已交保修，等商家向官方报备"}
                          </strong>
                          <div style={{ marginTop: 4 }}>
                            你提交的主力 Gmail：<span className="mono">{record.warranty?.admin_email_masked || "-"}</span>
                          </div>
                        </div>
                      </div>

                      <div className="kv-grid">
                        <div className="kv-item">
                          <div className="kv-label">你的主力 Gmail</div>
                          <div className="kv-value mono">{record.warranty?.admin_email_masked || "-"}</div>
                        </div>
                        <div className="kv-item">
                          <div className="kv-label">商店套餐邮箱</div>
                          <div className="kv-value mono">{storeEmail || "-"}</div>
                        </div>
                        <div className="kv-item">
                          <div className="kv-label">提交时间</div>
                          <div className="kv-value tabular">{formatDateTime(record.warranty?.submitted_at)}</div>
                        </div>
                        <div className="kv-item">
                          <div className="kv-label">邀请截图</div>
                          <div className="kv-value">{record.warranty?.has_screenshot ? "已上传" : "未上传"}</div>
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
                            改邮箱 / 重传截图
                          </button>
                        </div>
                      ) : null}
                    </div>
                  ) : (
                    <form onSubmit={handleWarrantySubmit} className="stack">
                      <GmailDoubleInput
                        email={adminEmail}
                        onEmailChange={setAdminEmail}
                        primaryLabel="你的主力 Gmail（建家庭组那个号）"
                        warningText="这里填你自己建家庭组的主力 Gmail，别填成上面复制的商店邮箱，点提交前看清拼写。"
                        disabled={submittingWarranty}
                      />

                      {warrantyUsesStoreEmail ? (
                        <div className="callout callout-danger" role="alert">
                          <AlertCircle size={17} className="callout-icon" />
                          <div>你填成了上面的商店套餐邮箱，请改成你自己的主力 Gmail。</div>
                        </div>
                      ) : null}

                      <div className="field-group">
                        <label className="field-label" htmlFor="warranty-screenshot-input">
                          <span>家庭组邀请截图</span>
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
                                点这里上传家庭组邀请截图
                              </span>
                              <span className="field-hint">截图里能看清「已邀请商店邮箱」就行</span>
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
                          placeholder="比如：已经发邀请了，麻烦帮忙报备"
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
                          disabled={
                            !warrantyEmailCheck.valid ||
                            warrantyUsesStoreEmail ||
                            !screenshotFile ||
                            submittingWarranty
                          }
                          aria-busy={submittingWarranty}
                        >
                          {submittingWarranty ? (
                            <>
                              <Loader2 size={17} className="spin" />
                              <span>正在上传...</span>
                            </>
                          ) : (
                            <>
                              <UploadCloud size={18} />
                              <span>提交保修信息</span>
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
          <span>Google AI Ultra 20x 自助提货</span>
          <span aria-hidden="true">·</span>
          <Link to="/">返回首页</Link>
        </footer>
      </div>
    </div>
  );
}
