import {
  useCallback,
  useEffect,
  useState,
  type CSSProperties,
  type FormEvent,
  type MouseEvent,
} from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import {
  ApiError,
  getPublicCode,
  redeemPublicCode,
  refreshPublicCode,
  submitPublicWarranty,
} from "../api/client";
import { BuyerWallpaper } from "../components/BuyerWallpaper";
import { CastoriceIcon } from "../components/CastoriceIcon";
import { CastoriceWaitingStage } from "../components/CastoriceWaitingStage";
import { ChibiSticker } from "../components/ChibiSticker";
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
import { validateGmailInput } from "../utils/validation";
import { runRedeemViewTransition } from "../utils/viewTransition";

const AUTO_POLL_INTERVAL_MS = 30_000;

interface RedeemRouteState {
  prefetchedRecord?: PublicCodeResponse;
}

function staggerStyle(index: number): CSSProperties {
  return { "--stagger-idx": index } as CSSProperties;
}

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
      return { label: "待领会员邮箱", tone: "neutral" };
    }
    if (record.status === "validating" || record.status === "purchasing") {
      return { label: "正在出货", tone: "info", pulse: true };
    }
    if (record.status === "delivered_pending_warranty") {
      return { label: "已领邮箱 · 待填主邮箱登记质保", tone: "info" };
    }
    if (record.status === "completed") {
      if (record.warranty?.status === "registered_upstream") {
        return { label: "已完成 · 质保已报备", tone: "success" };
      }
      if (record.warranty?.status === "submitted") {
        return { label: "保修邮箱已登记 · 报备中", tone: "warning" };
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
      return { label: "已领邮箱 · 待填主邮箱登记质保", tone: "info" };
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
      return { label: "邮箱已登记 · 报备中", tone: "warning" };
    case "registered_upstream":
      return { label: "30 天质保已报备", tone: "success" };
    default:
      return { label: "待填主邮箱", tone: "warning" };
  }
}

export function RedeemPage() {
  const { code: rawCode } = useParams<{ code: string }>();
  const location = useLocation();
  const navigate = useNavigate();
  const code = (rawCode || "").trim().toUpperCase();
  const { pushToast } = useToast();

  const routeState = (location.state as RedeemRouteState | null) ?? null;
  const prefetchedForCode =
    routeState?.prefetchedRecord &&
    routeState.prefetchedRecord.code.trim().toUpperCase() === code
      ? routeState.prefetchedRecord
      : null;

  const [record, setRecord] = useState<PublicCodeResponse | null>(() => prefetchedForCode);
  const [loading, setLoading] = useState(() => !prefetchedForCode);
  const [notFoundMessage, setNotFoundMessage] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [checkFamily, setCheckFamily] = useState(false);
  const [checkPayment, setCheckPayment] = useState(false);
  const [checkSlotRule, setCheckSlotRule] = useState(false);
  const [checkExclusiveRule, setCheckExclusiveRule] = useState(false);

  const [buyerEmail, setBuyerEmail] = useState("");
  const [submittingRedeem, setSubmittingRedeem] = useState(false);
  const [redeemError, setRedeemError] = useState<string | null>(null);

  const [refreshing, setRefreshing] = useState(false);
  const [cooldownLeft, setCooldownLeft] = useState(0);

  const [editingWarranty, setEditingWarranty] = useState(false);
  const [adminEmail, setAdminEmail] = useState("");
  const [warrantyNote, setWarrantyNote] = useState("");
  const [warrantyError, setWarrantyError] = useState<string | null>(null);
  const [submittingWarranty, setSubmittingWarranty] = useState(false);

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
    setCheckSlotRule(false);
    setCheckExclusiveRule(false);
    setBuyerEmail("");
    setRedeemError(null);
    setAdminEmail("");
    setWarrantyNote("");
    setWarrantyError(null);
    setEditingWarranty(false);
    if (prefetchedForCode && prefetchedForCode.code.trim().toUpperCase() === code) {
      setRecord(prefetchedForCode);
      setLoading(false);
      setLoadError(null);
      setNotFoundMessage(null);
      return;
    }
    void fetchRecord(false);
  }, [code, fetchRecord, prefetchedForCode]);

  const handleBackHome = (event: MouseEvent<HTMLAnchorElement>) => {
    if (
      event.defaultPrevented ||
      event.button !== 0 ||
      event.metaKey ||
      event.altKey ||
      event.ctrlKey ||
      event.shiftKey
    ) {
      return;
    }
    event.preventDefault();
    runRedeemViewTransition(() => navigate("/"), "back");
  };

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

  const isExclusive = record?.product_type === "account";
  const slotEmailCheck = validateGmailInput(buyerEmail);
  const preflightReady = isExclusive
    ? checkFamily && checkPayment && checkExclusiveRule
    : checkFamily && checkPayment && checkSlotRule;

  const handleSlotSubmit = async (event: FormEvent) => {
    event.preventDefault();
    setRedeemError(null);
    if (!preflightReady) {
      setRedeemError("请先勾选上面的三项提货前确认。");
      return;
    }
    if (!slotEmailCheck.valid) {
      setRedeemError(slotEmailCheck.error || "请检查一下邮箱拼写对不对。");
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
      setRedeemError("请先勾选上面的三项提货前确认。");
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
        pushToast("家庭组邀请发出来了，快去邮箱查收", "success");
      } else {
        pushToast("进度已刷新", "info");
      }
    } catch (err) {
      pushToast(err instanceof Error ? err.message : "刷新没成功，稍后再试", "error");
    } finally {
      setRefreshing(false);
    }
  };

  const warrantyEmailCheck = validateGmailInput(adminEmail);
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
      setWarrantyError("你填成了商店套餐邮箱，这里要填你自己建家庭组用的常用邮箱。");
      return;
    }
    if (!warrantyEmailCheck.valid) {
      setWarrantyError(warrantyEmailCheck.error || "请检查一下常用邮箱拼写对不对。");
      return;
    }

    setSubmittingWarranty(true);
    try {
      const trimmedNote = warrantyNote.trim();
      const updated = await submitPublicWarranty(code, {
        admin_email: warrantyEmailCheck.normalized,
        admin_email_confirm: warrantyEmailCheck.normalized,
        ...(trimmedNote ? { note: trimmedNote } : {}),
      });
      setRecord(updated);
      setEditingWarranty(false);
      if (updated.warranty?.status === "registered_upstream") {
        pushToast("30 天质保已自动完成官方报备", "success");
      } else {
        pushToast("保修邮箱已登记，系统正在帮你向官方报备", "success");
      }
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
    ? ["领邮箱", "建组邀请", "登记质保"]
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
  const isBuyerGmail =
    !record?.buyer_email_masked ||
    /@(gmail|googlemail)\.com$/i.test(record.buyer_email_masked);
  const hasMobileGmailDock = Boolean(
    record && !isExclusive && record.status === "completed" && isBuyerGmail
  );
  const exclusiveWarrantyNotice = (
    <div className="inset-panel stack-sm">
      <strong style={{ fontSize: 13.5 }}>质保和退换说明</strong>
      <ol className="guide-steps">
        <li>
          <strong>千万别把我们给的会员邮箱踢出家庭组！</strong>踢掉会立刻掉会员，而且没法保修。
        </li>
        <li>
          <strong>质保 30 天（从领到邮箱那天算起）</strong>，不是你自己原因掉会员的免费保修；领完后如果不是商品本身问题，不支持全额退款。
        </li>
        <li>如果你自己的账号被封或登不上，没法保修。</li>
        <li>
          如果因为自己的号被封、登不上、12 个月换组限制，或不小心踢掉了会员邮箱导致用不了/掉会员，补救或换号收 <strong>79.9 元手续费</strong>，且不保证一定能恢复。
        </li>
      </ol>
    </div>
  );

  return (
    <div className={`buyer-page ${hasMobileGmailDock ? "buyer-page--has-sticky-cta" : ""}`}>
      <BuyerWallpaper variant="detail" />
      <div className="buyer-container">
        <header className="buyer-topbar">
          <Link to="/" className="brand-lockup" onClick={handleBackHome}>
            <span className="brand-mark" aria-hidden="true">
              <CastoriceIcon name="emblem" size={30} />
            </span>
            <span className="brand-lockup-title">Google AI Ultra 20x 自助提货</span>
          </Link>
          <Link to="/" className="btn btn-ghost btn-sm buyer-back-btn" onClick={handleBackHome}>
            <CastoriceIcon name="arrow-left" size={15} />
            <span>换个兑换码</span>
          </Link>
        </header>

        {loading ? (
          <section className="card stack redeem-card-morph" aria-label="正在加载兑换码信息">
            <div className="row-between">
              <div className="skeleton" style={{ width: 96, height: 26 }} />
              <div className="skeleton" style={{ width: 84, height: 26 }} />
            </div>
            <div className="skeleton" style={{ width: "78%", height: 28 }} />
            <div className="skeleton" style={{ width: "100%", height: 50 }} />
            <div className="skeleton" style={{ width: "100%", height: 140 }} />
          </section>
        ) : notFoundMessage ? (
          <section
            className="card stack redeem-card-morph"
            style={{ textAlign: "center", padding: "36px 24px" }}
          >
            <CastoriceIcon name="alert" size={40} style={{ margin: "0 auto" }} />
            <div>
              <h1 className="card-title">未找到该兑换码</h1>
              <p className="card-subtitle">{notFoundMessage}</p>
            </div>
            <div className="row" style={{ justifyContent: "center" }}>
              <Link to="/" className="btn btn-primary" onClick={handleBackHome}>
                <CastoriceIcon name="arrow-left" size={16} />
                返回重新输入
              </Link>
            </div>
          </section>
        ) : loadError ? (
          <section
            className="card stack redeem-card-morph"
            style={{ textAlign: "center", padding: "36px 24px" }}
          >
            <CastoriceIcon name="alert" size={40} style={{ margin: "0 auto" }} />
            <div>
              <h1 className="card-title">暂时无法加载订单</h1>
              <p className="card-subtitle">{loadError}</p>
            </div>
            <div className="row" style={{ justifyContent: "center" }}>
              <button type="button" className="btn btn-primary" onClick={() => void fetchRecord(false)}>
                <CastoriceIcon name="refresh" size={15} />
                <span>重新加载</span>
              </button>
              <Link to="/" className="btn btn-secondary" onClick={handleBackHome}>
                返回首页
              </Link>
            </div>
          </section>
        ) : record ? (
          <>
            {/* Voucher Summary Card */}
            <section className="card stack voucher-summary-card redeem-card-morph">
              <ChibiSticker
                variant="mail"
                springEntrance
                bubbleText="收件箱没看到的话，记得翻翻垃圾箱或推广分类 ✨"
              />

              <div className="voucher-head voucher-head--with-sticker redeem-stagger-block" style={staggerStyle(0)}>
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
                        shimmer={record.status === "completed"}
                      />
                    ) : null}
                  </div>
                </div>
                <h1 className="card-title" style={{ fontSize: 20 }}>
                  {record.sku_title}
                </h1>
              </div>

              <div className="voucher-meta-bar redeem-stagger-block" style={staggerStyle(1)}>
                <div className="voucher-code-pill">
                  <span className="voucher-code-label">兑换码</span>
                  <span className="voucher-code-value">{record.code}</span>
                </div>
                <CopyButton
                  text={record.code}
                  label="复制兑换码"
                  copiedLabel="✓ 已复制"
                  toastMessage="兑换码已复制"
                />
              </div>

              {record.status !== "revoked" && record.status !== "needs_manual_review" ? (
                <ol className="stepper redeem-stagger-block" style={staggerStyle(2)} aria-label="提货进度">
                  {steps.map((label, idx) => {
                    const isDone = idx < activeStep || (record.status === "completed" && idx <= activeStep);
                    const isCurrent = !isDone && idx === activeStep;
                    const showSprite = idx === activeStep;
                    return (
                      <li
                        key={label}
                        className={`stepper-item ${isDone ? "is-done" : ""} ${showSprite ? "has-sprite" : ""}`}
                        aria-current={isCurrent ? "step" : undefined}
                      >
                        <span className="stepper-num" aria-hidden="true">
                          {isDone ? <CastoriceIcon name="check" size={13} /> : idx + 1}
                        </span>
                        <span className="stepper-label">{label}</span>
                        {showSprite ? (
                          <img
                            src="/assets/stickers/pixel-sprite.png"
                            alt=""
                            aria-hidden="true"
                            decoding="async"
                            draggable={false}
                            className="stepper-sprite"
                          />
                        ) : null}
                      </li>
                    );
                  })}
                </ol>
              ) : null}
            </section>

            <CastoriceWaitingStage
              record={record}
              autoPollIntervalMs={AUTO_POLL_INTERVAL_MS}
              refreshing={refreshing}
            />

            {/* Revoked */}
            {record.status === "revoked" ? (
              <section className="card stack redeem-stagger-block" style={staggerStyle(3)}>
                <div className="callout callout-danger">
                  <CastoriceIcon name="alert" size={19} className="callout-icon" />
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
              <section className="card stack redeem-stagger-block" style={staggerStyle(3)}>
                <div className="callout callout-warning">
                  <CastoriceIcon name="refresh" size={18} className="callout-icon" />
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
                    <CastoriceIcon name="refresh" size={15} />
                    <span>刷一下状态</span>
                  </button>
                </div>
              </section>
            ) : null}

            {/* ===============================================================
                FLOW A: SHARED SLOT (ultra_shared_slot)
               =============================================================== */}
            {!isExclusive && record.status === "unused" ? (
              <section className="card stack redeem-stagger-block" style={staggerStyle(3)}>
                <div>
                  <h2 className="card-title">填你要开通会员的邮箱</h2>
                  <p className="card-subtitle">
                    先检查并勾选下面 3 项，再填你的邮箱提交（支持 Gmail、QQ、Outlook 等任意邮箱）。提交后一般 2-6 小时发家庭组邀请，最晚 24 小时，没超过 24 小时不用催。
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
                      <strong>这个邮箱账号现在不在别的 Google 家庭组里，且最近 12 个月内没加过、没退过、也没被重拉过任何家庭组</strong>
                      （Google 规定每 12 个月只能换 1 次家庭组，哪怕只进出过 1 次也会被限制加不进新组；可以点{" "}
                      <a href="https://g.co/yourfamily" target="_blank" rel="noreferrer">
                        g.co/yourfamily <CastoriceIcon name="external" size={13} className="inline-ext-icon" />
                      </a>{" "}
                      检查并退出旧组）。
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
                      <strong>已经关掉了 Google 付款资料</strong>（先点{" "}
                      <a
                        href="https://payments.google.com/gp/w/home/settings"
                        target="_blank"
                        rel="noreferrer"
                      >
                        payments.google.com/gp/w/home/settings{" "}
                        <CastoriceIcon name="external" size={13} className="inline-ext-icon" />
                      </a>{" "}
                      把里面的付款资料关掉，不然进组时会提示“国家/地区不符”）。
                    </div>
                  </label>

                  <label className={`check-card ${checkSlotRule ? "is-checked" : ""}`}>
                    <input
                      type="checkbox"
                      checked={checkSlotRule}
                      disabled={submittingRedeem}
                      onChange={(event) => setCheckSlotRule(event.target.checked)}
                    />
                    <div className="check-card-body">
                      <strong>我已看清等待时间和退换规则</strong>：提交邮箱后就会马上占好名额开始排队（一般 2-6 小时发邀请，最晚 24 小时）；如果不是商品本身问题（比如 12 个月内换过组加不进、没退旧家庭组、没关付款资料、填错邮箱等），不支持全额退款，可补 <strong>49.9 元手续费</strong>换新邮箱重新拉人，或扣除 <strong>49.9 元名额成本</strong>后退还余款。
                    </div>
                  </label>
                </div>

                <div className="inset-panel stack-sm">
                  <strong style={{ fontSize: 13.5 }}>购买和使用须知</strong>
                  <ol className="guide-steps">
                    <li>
                      <strong>质保 30 天会员</strong>，不保你的个人 Google 账号；自己违规使用或反代导致封号没法保修。
                    </li>
                    <li>
                      本款是 <strong>0 积分/点数车位</strong>，请不要去用积分或点数，用了会被系统自动踢出家庭组且不退款。
                    </li>
                    <li>
                      提交邮箱后如果不是商品本身问题（比如 12 个月内换过组加不进、没退旧家庭组、没关付款资料、填错邮箱等），不支持全额退款；可补 <strong>49.9 元手续费</strong>换新邮箱重新拉人，或扣除 <strong>49.9 元名额成本</strong>后退还余款。
                    </li>
                  </ol>
                </div>

                <form onSubmit={(event) => void handleSlotSubmit(event)} className="stack">
                  <GmailDoubleInput
                    email={buyerEmail}
                    onEmailChange={setBuyerEmail}
                    disabled={submittingRedeem}
                    primaryLabel="收家庭组邀请的邮箱"
                    warningText="别填错邮箱！提交后会立刻占好名额排队拉人，如果因为填错邮箱或 12 个月内换过组导致加不进，换新号需要补 49.9 元手续费，点提交前请再检查一遍拼写。"
                  />

                  <div className="presubmit-card" aria-live="polite">
                    <div className="presubmit-title">提交前看一眼</div>
                    <ul className="presubmit-list">
                      <li className={`presubmit-item ${preflightReady ? "is-pass" : ""}`}>
                        {preflightReady ? (
                          <CastoriceIcon name="check" size={15} />
                        ) : (
                          <span className="presubmit-dot" aria-hidden="true" />
                        )}
                        <span>三项确认已勾选</span>
                      </li>
                      <li className={`presubmit-item ${slotEmailCheck.valid ? "is-pass" : ""}`}>
                        {slotEmailCheck.valid ? (
                          <CastoriceIcon name="check" size={15} />
                        ) : (
                          <span className="presubmit-dot" aria-hidden="true" />
                        )}
                        <span>邮箱拼写格式没问题</span>
                      </li>
                    </ul>
                  </div>

                  {redeemError ? (
                    <div className="callout callout-danger" role="alert">
                      <CastoriceIcon name="alert" size={18} className="callout-icon" />
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
                        <CastoriceIcon name="refresh" size={17} spin />
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
              <section className="card stack redeem-stagger-block" style={staggerStyle(3)}>
                <div className="callout callout-info">
                  <CastoriceIcon name="refresh" size={18} className="callout-icon" />
                  <div>
                    <strong>已经排上队了，安心等邮件就行</strong>
                    <div style={{ marginTop: 4 }}>
                      你的邮箱已经进入自动拉人队列，一般 2-6 小时内会把家庭组邀请发到你邮箱，最晚 24 小时，没超过 24 小时不用催；这个页面会自动更新进度，建议收藏链接随时回来看。
                    </div>
                  </div>
                </div>

                <div className="kv-grid">
                  <div className="kv-item">
                    <div className="kv-label">收邀请的邮箱</div>
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
                    <CastoriceIcon name="refresh" size={15} spin={refreshing} />
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
              <section className="card stack redeem-stagger-block" style={staggerStyle(3)}>
                <div className="callout callout-success">
                  <CastoriceIcon name="mail" size={20} className="callout-icon" />
                  <div>
                    <strong>家庭组邀请发出来了，快去邮箱查收</strong>
                    <div style={{ marginTop: 4 }}>
                      打开 <span className="mono">{record.buyer_email_masked || "你填的邮箱"}</span>，找来自 Google 的家庭组邀请邮件（收件箱没看到的话翻一下垃圾箱或推广分类）。
                    </div>
                  </div>
                </div>

                <div className="kv-grid">
                  <div className="kv-item">
                    <div className="kv-label">收邀请的邮箱</div>
                    <div className="kv-value mono kv-value-email">{record.buyer_email_masked || "-"}</div>
                  </div>
                  <div className="kv-item">
                    <div className="kv-label">发邀请时间</div>
                    <div className="kv-value tabular">{formatDateTime(record.fulfillment.completed_at)}</div>
                  </div>
                </div>

                <div className="inset-panel stack-sm">
                  <strong style={{ fontSize: 13.5 }}>怎么点接受邀请</strong>
                  <ol className="guide-steps">
                    <li>打开你的邮箱，找到标题带 “Google 家庭组” 或 “Join family group” 的邮件。</li>
                    <li>点里面的 <strong>接受邀请</strong>，按提示点确认加入就生效了。</li>
                    <li>
                      如果提示「12 个月只能切换一次家庭群组」或「国家/地区不同」，请完整截图发给淘宝客服；如果是你自己的账号受 12 个月换组限制或没退旧组导致加不进，可以补 <strong>49.9 元手续费</strong>换一个符合条件的新邮箱重新拉人。
                    </li>
                  </ol>
                </div>

                {isBuyerGmail ? (
                  <div className="row redeem-desktop-gmail-cta">
                    <a
                      href="https://mail.google.com"
                      target="_blank"
                      rel="noreferrer"
                      className="btn btn-primary btn-block"
                    >
                      <CastoriceIcon name="mail" size={17} />
                      <span>去 Gmail 收邀请</span>
                      <CastoriceIcon name="external" size={15} />
                    </a>
                  </div>
                ) : null}
              </section>
            ) : null}

            {/* ===============================================================
                FLOW B: EXCLUSIVE ADMIN (ultra_exclusive_admin)
               =============================================================== */}
            {isExclusive && record.status === "unused" ? (
              <section className="card stack redeem-stagger-block" style={staggerStyle(3)}>
                <div>
                  <h2 className="card-title">先领会员邮箱</h2>
                  <p className="card-subtitle">
                    独享整组是你自己当家庭组组长：先点下面领一个我们给的会员邮箱（商店邮箱），再用你自己的常用邮箱建家庭组把它拉进去，最后回来填你的主邮箱登记 30 天质保就行（免截图）。
                  </p>
                </div>

                <div className="exclusive-steps-card">
                  <strong style={{ fontSize: 13.5 }}>一共就 3 步，很快</strong>
                  <ol className="guide-steps">
                    <li>点下面按钮，立刻领到我们给的<strong>会员邮箱（商店邮箱）</strong>。</li>
                    <li>
                      用你自己的<strong>常用邮箱</strong>建一个 Google 家庭组，把这个会员邮箱拉进去；对方同意后你就开通 Ultra 会员了，<strong>自己还能再拉 4 个人</strong>一起用。
                    </li>
                    <li>
                      发完邀请后回到本页，填写<strong>你自己的常用主邮箱</strong>即可一键登记 30 天质保（免截图，系统会自动向官方报备；如需人工跟进一般 <strong>2-6 小时内完成，最晚 24 小时</strong>）。
                    </li>
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
                      <strong>我准备好了自己当组长的常用邮箱</strong>，现在不在别的家庭组里，且最近 12 个月内没加过、没退过、也没被重拉过其他家庭组（Google 规定每 12 个月只能换 1 次家庭组；点{" "}
                      <a href="https://g.co/yourfamily" target="_blank" rel="noreferrer">
                        g.co/yourfamily <CastoriceIcon name="external" size={13} className="inline-ext-icon" />
                      </a>{" "}
                      检查并退出旧组）。
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
                      <strong>已经关掉了 Google 付款资料</strong>（先点{" "}
                      <a
                        href="https://payments.google.com/gp/w/home/settings"
                        target="_blank"
                        rel="noreferrer"
                      >
                        payments.google.com/gp/w/home/settings{" "}
                        <CastoriceIcon name="external" size={13} className="inline-ext-icon" />
                      </a>{" "}
                      把里面的付款资料关掉，不然建组或拉人时可能提示地区受限）。
                    </div>
                  </label>

                  <label className={`check-card ${checkExclusiveRule ? "is-checked" : ""}`}>
                    <input
                      type="checkbox"
                      checked={checkExclusiveRule}
                      disabled={submittingRedeem}
                      onChange={(event) => setCheckExclusiveRule(event.target.checked)}
                    />
                    <div className="check-card-body">
                      <strong>我已看清使用和售后规则</strong>：把我们给的会员邮箱拉进家庭组后千万别踢掉（踢掉会立刻掉会员且没法保修），发完邀请记得回来填主邮箱登记质保；领完邮箱后，不是商品本身问题不支持全额退款，因为自己原因导致用不了/掉会员，补救或换号收 <strong>79.9 元手续费</strong>（不保证一定能恢复）。
                    </div>
                  </label>
                </div>

                {exclusiveWarrantyNotice}

                {redeemError ? (
                  <div className="callout callout-danger" role="alert">
                    <CastoriceIcon name="alert" size={18} className="callout-icon" />
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
                        <CastoriceIcon name="refresh" size={17} spin />
                        <span>正在领商店邮箱...</span>
                      </>
                    ) : (
                      <>
                        <CastoriceIcon name="check" size={18} />
                        <span>准备好了，马上领会员邮箱</span>
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
                <section className="card stack redeem-stagger-block" style={staggerStyle(3)}>
                  <div className="row-between">
                    <h2 className="card-title">第 1 步：复制我们给的会员邮箱</h2>
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
                          <span className="email-role-tag email-role-tag-store">会员邮箱（商店邮箱）</span>
                          <span className="field-hint">用来拉进你的家庭组，别填到下面保修框里</span>
                        </div>
                        <div className="credential-value">{storeEmail}</div>
                      </div>
                      <CopyButton
                        text={storeEmail}
                        label="复制会员邮箱"
                        toastMessage="会员邮箱已复制"
                        variant="primary"
                      />
                    </div>
                  ) : (
                    <div className="callout callout-info">
                      <CastoriceIcon name="check" size={18} className="callout-icon" />
                      <div>会员邮箱已发出，如果没显示出来请刷新页面或联系客服。</div>
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
                    <strong style={{ fontSize: 13.5 }}>第 2 步：用你自己的常用邮箱建家庭组并邀请它</strong>
                    <ol className="guide-steps">
                      <li>
                        用你自己的<strong>常用邮箱</strong>打开{" "}
                        <a href="https://g.co/yourfamily" target="_blank" rel="noreferrer">
                          g.co/yourfamily <CastoriceIcon name="external" size={13} className="inline-ext-icon" />
                        </a>{" "}
                        建好 Google 家庭组。
                      </li>
                      <li>
                        在家庭组里点「发送邀请」，把上面复制的<strong>会员邮箱</strong>粘进去发邀请；对方同意后你就开通 Ultra 会员了，<strong>自己还能再拉 4 个人</strong>一起用。
                      </li>
                      <li>
                        发完邀请后，在下面填写<strong>你自己的常用主邮箱</strong>登记 30 天质保即可（无需截图，系统会自动向官方报备；如需人工处理一般 <strong>2-6 小时内完成，最晚 24 小时</strong>）。
                      </li>
                    </ol>
                  </div>
                </section>

                {/* Warranty Section */}
                <section className="card stack redeem-stagger-block" style={staggerStyle(4)}>
                  <div className="row-between">
                    <div style={{ flex: "1 1 240px", minWidth: 0 }}>
                      <h2 className="card-title">第 3 步：填写主邮箱登记 30 天质保</h2>
                      <p className="card-subtitle">
                        发完邀请后，填写你建家庭组用的常用主邮箱即可（免截图），提交后系统会自动帮你向官方完成质保报备。
                      </p>
                    </div>
                    <ToneBadge label={buyerWarrantyBadge.label} tone={buyerWarrantyBadge.tone} />
                  </div>

                  {warrantySubmitted && (!editingWarranty || warrantyLocked) ? (
                    <div className="stack">
                      <div className={`callout ${warrantyLocked ? "callout-success" : "callout-info"}`}>
                        <CastoriceIcon name="check" size={19} className="callout-icon" />
                        <div>
                          <strong>
                            {warrantyLocked
                              ? "30 天质保已自动完成官方报备"
                              : "保修邮箱已登记"}
                          </strong>
                          <div style={{ marginTop: 4 }}>
                            {warrantyLocked ? (
                              <>
                                你的主邮箱{" "}
                                <span className="mono">{record.warranty?.admin_email_masked || "-"}</span>{" "}
                                已完成官方质保报备，安心使用就行。
                              </>
                            ) : (
                              <>
                                保修邮箱已登记，系统正在/已转人工帮你向官方报备质保（一般 2-6 小时内完成，最晚 24 小时）。已登记主邮箱：
                                <span className="mono">{record.warranty?.admin_email_masked || "-"}</span>
                              </>
                            )}
                          </div>
                        </div>
                      </div>

                      <div className="kv-grid">
                        <div className="kv-item">
                          <div className="kv-label">你的常用主邮箱</div>
                          <div className="kv-value mono">{record.warranty?.admin_email_masked || "-"}</div>
                        </div>
                        <div className="kv-item">
                          <div className="kv-label">会员邮箱（商店邮箱）</div>
                          <div className="kv-value mono">{storeEmail || "-"}</div>
                        </div>
                        <div className="kv-item">
                          <div className="kv-label">提交时间</div>
                          <div className="kv-value tabular">{formatDateTime(record.warranty?.submitted_at)}</div>
                        </div>
                        <div className="kv-item">
                          <div className="kv-label">报备状态</div>
                          <div className="kv-value">
                            {warrantyLocked ? "已完成官方报备" : "待官方报备完成"}
                          </div>
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
                            onClick={() => {
                              setWarrantyError(null);
                              setEditingWarranty(true);
                            }}
                          >
                            修改主邮箱
                          </button>
                        </div>
                      ) : null}
                    </div>
                  ) : (
                    <form onSubmit={handleWarrantySubmit} className="stack">
                      <GmailDoubleInput
                        email={adminEmail}
                        onEmailChange={setAdminEmail}
                        primaryLabel="你的常用邮箱（建家庭组那个号）"
                        warningText="这里填你自己建家庭组的常用邮箱，别填成上面复制的会员邮箱，点提交前看清拼写。"
                        disabled={submittingWarranty}
                      />

                      {warrantyUsesStoreEmail ? (
                        <div className="callout callout-danger" role="alert">
                          <CastoriceIcon name="alert" size={18} className="callout-icon" />
                          <div>你填成了上面的会员邮箱，请改成你自己的常用邮箱。</div>
                        </div>
                      ) : null}

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
                          placeholder="比如：已经发邀请了，麻烦帮忙登记质保"
                          value={warrantyNote}
                          onChange={(event) => setWarrantyNote(event.target.value)}
                          disabled={submittingWarranty}
                        />
                      </div>

                      {warrantyError ? (
                        <div className="callout callout-danger" role="alert">
                          <CastoriceIcon name="alert" size={18} className="callout-icon" />
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
                            submittingWarranty
                          }
                          aria-busy={submittingWarranty}
                        >
                          {submittingWarranty ? (
                            <>
                              <CastoriceIcon name="refresh" size={17} spin />
                              <span>正在提交报备...</span>
                            </>
                          ) : (
                            <>
                              <CastoriceIcon name="check" size={18} />
                              <span>提交主邮箱并登记质保</span>
                            </>
                          )}
                        </button>
                      </div>
                    </form>
                  )}

                  {exclusiveWarrantyNotice}
                </section>
              </>
            ) : null}
          </>
        ) : null}

        <footer className="footer-links">
          <span>Google AI Ultra 20x 自助提货</span>
          <span aria-hidden="true">·</span>
          <Link to="/" onClick={handleBackHome}>
            返回首页
          </Link>
        </footer>
      </div>

      {hasMobileGmailDock ? (
        <div className="redeem-mobile-sticky-dock">
          <a
            href="https://mail.google.com"
            target="_blank"
            rel="noreferrer"
            className="btn btn-primary btn-lg btn-block"
          >
            <CastoriceIcon name="mail" size={18} />
            <span>去 Gmail 收邀请</span>
            <CastoriceIcon name="external" size={15} />
          </a>
        </div>
      ) : null}
    </div>
  );
}
