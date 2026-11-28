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
import { BuyerWallpaper, WallpaperMotionToggle } from "../components/BuyerWallpaper";
import { CastoriceIcon } from "../components/CastoriceIcon";
import {
  CastoriceWaitingStage,
  isExclusiveAwaitingActivation,
} from "../components/CastoriceWaitingStage";
import { ChibiSticker } from "../components/ChibiSticker";
import { CopyButton } from "../components/CopyButton";
import { GmailDoubleInput } from "../components/GmailDoubleInput";
import { ToneBadge } from "../components/StatusBadge";
import { useToast } from "../components/ToastProvider";
import type { CodeStatus, ProductType, PublicCodeResponse, WarrantyStatus } from "../types/api";
import {
  formatDateTime,
  formatElapsedMinutes,
  getSkuShortLabel,
  type Tone,
} from "../utils/format";
import { validateGmailInput } from "../utils/validation";
import { runRedeemViewTransition } from "../utils/viewTransition";

const AUTO_POLL_INTERVAL_MS = 30_000;
const MANUAL_REVIEW_POLL_INTERVAL_MS = 60_000;
const EXCLUSIVE_WAIT_POLL_INTERVAL_MS = 60_000;

interface RedeemRouteState {
  prefetchedRecord?: PublicCodeResponse;
}

function staggerStyle(index: number): CSSProperties {
  return { "--stagger-idx": index } as CSSProperties;
}

function resolveStepIndex(
  productType: ProductType,
  status: CodeStatus,
  warrantySubmitted: boolean,
  exclusiveAwaiting: boolean
): number {
  if (productType === "slot") {
    if (status === "completed") return 2;
    if (status === "waiting_fulfillment" || status === "purchasing" || status === "validating") return 1;
    return 0;
  }
  if (status === "completed") {
    return exclusiveAwaiting ? 2 : 3;
  }
  if (status === "delivered_pending_warranty") return 1;
  if (warrantySubmitted) return 2;
  return 0;
}

function resolveExclusiveElapsedMinutes(record: PublicCodeResponse): number {
  if (
    typeof record.fulfillment.warranty_elapsed_minutes === "number" &&
    Number.isFinite(record.fulfillment.warranty_elapsed_minutes)
  ) {
    return Math.max(0, Math.floor(record.fulfillment.warranty_elapsed_minutes));
  }
  const submittedIso = record.fulfillment.warranty_submitted_at ?? record.warranty?.submitted_at ?? "";
  const parsedMs = submittedIso ? Date.parse(submittedIso) : Number.NaN;
  if (Number.isNaN(parsedMs)) return 0;
  return Math.max(0, Math.floor((Date.now() - parsedMs) / 60_000));
}

function resolveExclusivePhase(elapsedMinutes: number): "golden" | "peak" | "overdue" {
  if (elapsedMinutes >= 24 * 60) return "overdue";
  if (elapsedMinutes >= 7 * 60) return "peak";
  return "golden";
}

function readExclusiveDismissedFlag(code: string): boolean {
  if (!code) return false;
  try {
    return window.localStorage.getItem(`plan2_exclusive_wait_dismissed_${code}`) === "1";
  } catch {
    return false;
  }
}

function writeExclusiveDismissedFlag(code: string, dismissed: boolean): void {
  if (!code) return;
  try {
    const storageKey = `plan2_exclusive_wait_dismissed_${code}`;
    if (dismissed) {
      window.localStorage.setItem(storageKey, "1");
    } else {
      window.localStorage.removeItem(storageKey);
    }
  } catch {
    // Ignore storage write failures in restricted browsing contexts.
  }
}

function getBuyerStatusMeta(
  record: PublicCodeResponse,
  exclusiveAwaiting: boolean,
  exclusivePhase: "golden" | "peak" | "overdue"
): { label: string; tone: Tone; pulse?: boolean } {
  if (record.product_type === "account") {
    if (record.status === "unused") {
      return { label: "待领会员邮箱", tone: "neutral" };
    }
    if (record.status === "validating" || record.status === "purchasing") {
      return { label: "正在分配邮箱", tone: "info", pulse: true };
    }
    if (record.status === "waiting_fulfillment") {
      return { label: "排队取号中", tone: "warning", pulse: true };
    }
    if (record.status === "delivered_pending_warranty") {
      return { label: "已领邮箱 · 待登记交付排队", tone: "info" };
    }
    if (record.status === "completed") {
      if (record.fulfillment.exclusive_activated_at) {
        return { label: "已进组生效", tone: "success" };
      }
      if (exclusiveAwaiting) {
        if (exclusivePhase === "overdue") {
          return { label: "等待超 24h · 建议联系客服加急", tone: "danger", pulse: true };
        }
        if (exclusivePhase === "peak") {
          return { label: "等待会员邮箱进组 · 高峰排队", tone: "warning", pulse: true };
        }
        return { label: "等待会员邮箱进组", tone: "info", pulse: true };
      }
      if (record.warranty?.status === "registered_upstream") {
        return { label: "已完成 · 排队已登记", tone: "success" };
      }
      if (record.warranty?.status === "submitted") {
        return { label: "主邮箱已登记 · 排队中", tone: "warning" };
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
      return { label: "已领邮箱 · 待登记交付排队", tone: "info" };
    case "completed":
      return { label: "邀请已发出", tone: "success" };
    case "needs_manual_review":
      return { label: "人工处理中", tone: "warning" };
    case "revoked":
      return { label: "已作废", tone: "neutral" };
  }
}

function getBuyerWarrantyBadgeMeta(
  status: WarrantyStatus | undefined,
  exclusiveActivated = false
): { label: string; tone: Tone } {
  if (exclusiveActivated) {
    return { label: "已进组生效", tone: "success" };
  }
  switch (status) {
    case "submitted":
      return { label: "已登记排队", tone: "info" };
    case "registered_upstream":
      return { label: "已登记 · 已入交付队列", tone: "success" };
    default:
      return { label: "待登记交付排队", tone: "warning" };
  }
}

export function RedeemPage() {
  const { code: rawCode } = useParams<{ code: string }>();
  const location = useLocation();
  const code = (rawCode || "").trim().toUpperCase();

  const routeState = (location.state as RedeemRouteState | null) ?? null;
  const prefetchedForCode =
    routeState?.prefetchedRecord &&
    routeState.prefetchedRecord.code.trim().toUpperCase() === code
      ? routeState.prefetchedRecord
      : null;

  return <RedeemOrder key={`${location.key}:${code}`} code={code} prefetchedForCode={prefetchedForCode} />;
}

function RedeemOrder({ code, prefetchedForCode }: { code: string; prefetchedForCode: PublicCodeResponse | null }) {
  const navigate = useNavigate();
  const { pushToast } = useToast();

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
  const [autoPollCountdown, setAutoPollCountdown] = useState(Math.round(AUTO_POLL_INTERVAL_MS / 1000));

  const [editingWarranty, setEditingWarranty] = useState(false);
  const [adminEmail, setAdminEmail] = useState("");
  const [warrantyNote, setWarrantyNote] = useState("");
  const [warrantyError, setWarrantyError] = useState<string | null>(null);
  const [submittingWarranty, setSubmittingWarranty] = useState(false);
  const [exclusiveWaitDismissed, setExclusiveWaitDismissed] = useState<boolean>(() =>
    readExclusiveDismissedFlag(code)
  );

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
    if (!prefetchedForCode) {
      void fetchRecord(false);
    }
  }, [fetchRecord, prefetchedForCode]);

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

  const isExclusive = record?.product_type === "account";
  const exclusiveAwaitingRaw = Boolean(record && isExclusiveAwaitingActivation(record, false));
  const exclusiveAwaiting = Boolean(
    record && isExclusiveAwaitingActivation(record, exclusiveWaitDismissed)
  );
  const exclusiveActivated = Boolean(
    record && isExclusive && record.fulfillment.exclusive_activated_at
  );
  const exclusiveElapsedMin = record ? resolveExclusiveElapsedMinutes(record) : 0;
  const exclusivePhase = resolveExclusivePhase(exclusiveElapsedMin);

  const handleDismissExclusiveWait = () => {
    setExclusiveWaitDismissed(true);
    writeExclusiveDismissedFlag(code, true);
    pushToast("已收起等待页，如还没生效可随时重新展开", "info");
  };

  const handleRestoreExclusiveWait = () => {
    setExclusiveWaitDismissed(false);
    writeExclusiveDismissedFlag(code, false);
    pushToast("已重新展开独享交付等待页", "info");
  };

  useEffect(() => {
    if (!record) return;
    const isWaitingState =
      record.status === "waiting_fulfillment" ||
      record.status === "validating" ||
      record.status === "purchasing";
    const isManualReview = record.status === "needs_manual_review";
    if (!isWaitingState && !isManualReview && !exclusiveAwaiting) {
      return;
    }
    const intervalMs = isManualReview
      ? MANUAL_REVIEW_POLL_INTERVAL_MS
      : exclusiveAwaiting
      ? EXCLUSIVE_WAIT_POLL_INTERVAL_MS
      : AUTO_POLL_INTERVAL_MS;
    const intervalSec = Math.round(intervalMs / 1000);
    setAutoPollCountdown(intervalSec);

    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") {
        void fetchRecord(true);
        setAutoPollCountdown(intervalSec);
      }
    }, intervalMs);

    const tickTimer = window.setInterval(() => {
      if (document.visibilityState === "visible") {
        setAutoPollCountdown((prev) => (prev <= 1 ? intervalSec : prev - 1));
      }
    }, 1000);

    const onVisibility = () => {
      if (document.visibilityState === "visible") {
        void fetchRecord(true);
        setAutoPollCountdown(intervalSec);
      }
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.clearInterval(timer);
      window.clearInterval(tickTimer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [record?.status, record?.code, exclusiveAwaiting, fetchRecord]);

  useEffect(() => {
    if (cooldownLeft <= 0) return;
    const timer = window.setInterval(() => {
      setCooldownLeft((prev) => (prev <= 1 ? 0 : prev - 1));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [cooldownLeft]);

  const is16688 = record?.channel === "16688";
  const sharedRecipientLabel = is16688 ? "收邀请的邮箱（部分隐藏）" : "收邀请的邮箱";
  const buyerStatusMeta = record
    ? getBuyerStatusMeta(record, exclusiveAwaiting, exclusivePhase)
    : null;
  const supportTicketText = record
    ? [
        "【提货排查凭证】",
        `兑换码：${record.code}`,
        `渠道：${record.channel}`,
        `商品：${record.sku_title}`,
        `当前状态：${buyerStatusMeta ? buyerStatusMeta.label : record.status} (${record.status})`,
        `邮箱：${record.buyer_email_masked || record.warranty?.admin_email_masked || "未填写"}`,
        ...(isExclusive && exclusiveAwaitingRaw
          ? [`独享已等待：${formatElapsedMinutes(exclusiveElapsedMin)}`]
          : []),
        `更新时间：${formatDateTime(record.updated_at)}`,
      ].join("\n")
    : "";
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
      } else if (updated.status === "waiting_fulfillment") {
        pushToast("已进入排队取号，一般 3-7 小时分配独立会员邮箱", "info");
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
      if (updated.product_type === "account") {
        if (updated.fulfillment.exclusive_activated_at) {
          pushToast("独享家庭组已确认生效，现在可以拉家人一起用", "success");
        } else if (updated.status === "delivered_pending_warranty") {
          pushToast("会员邮箱已分配好，按下方步骤建组即可", "success");
        } else if (isExclusiveAwaitingActivation(updated, false)) {
          pushToast("进度已同步，当前仍在 3-7 小时进组等待中", "info");
        } else {
          pushToast("进度已刷新", "info");
        }
      } else if (updated.status === "completed") {
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
      if (isExclusiveAwaitingActivation(updated, false)) {
        setExclusiveWaitDismissed(false);
        writeExclusiveDismissedFlag(code, false);
        pushToast("主邮箱已提交登记，已排入独享套餐交付队列（一般 3-7 小时，最晚 24 小时）", "success");
      } else if (updated.warranty?.status === "registered_upstream") {
        pushToast("主邮箱已完成系统登记，已排入交付队列", "success");
      } else {
        pushToast("主邮箱已提交登记，系统正在为你排入交付队列", "success");
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : "提交登记没成功，请稍后再试。";
      setWarrantyError(message);
      pushToast(message, "error");
    } finally {
      setSubmittingWarranty(false);
    }
  };

  const warrantySubmitted =
    record?.warranty?.status === "submitted" || record?.warranty?.status === "registered_upstream";
  const warrantyLocked =
    record?.warranty?.status === "registered_upstream" ||
    Boolean(record?.fulfillment?.exclusive_activated_at);
  const isFullyCompleted = Boolean(record && record.status === "completed" && !exclusiveAwaiting);
  const activeStep = record
    ? resolveStepIndex(record.product_type, record.status, warrantySubmitted, exclusiveAwaiting)
    : 0;
  const steps = isExclusive
    ? ["领邮箱", "建组登记排队", "等待会员邮箱进组", "生效完成"]
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
  const buyerWarrantyBadge = getBuyerWarrantyBadgeMeta(
    record?.warranty?.status,
    Boolean(record?.fulfillment.exclusive_activated_at)
  );
  const isBuyerGmail =
    !record?.buyer_email_masked ||
    /@(gmail|googlemail)\.com$/i.test(record.buyer_email_masked);
  const hasMobileGmailDock = Boolean(
    record && !isExclusive && record.status === "completed" && isBuyerGmail
  );
  const chibiBubbleText = isExclusive
    ? exclusiveAwaiting
      ? "一般 3-7 小时进组，关掉本页也不掉队"
      : exclusiveActivated
      ? "家庭组已生效，快去拉 4 位家人共享吧"
      : "发完家庭组邀请记得回来填你的常用主邮箱"
    : "没看到邀请邮件记得翻垃圾箱";
  const exclusiveWarrantyNotice = (
    <div className="inset-panel stack-sm">
      <strong style={{ fontSize: 13.5 }}>使用与售后须知</strong>
      <ol className="guide-steps">
        <li>
          <strong>千万别把会员邮箱踢出家庭组</strong>，踢掉立刻掉会员且不保修。
        </li>
        <li>
          <strong>质保 30 天</strong>（从领号起算），非个人原因掉会员免费保修；领号后非商品问题不支持全额退款。
        </li>
        <li>自己账号被封、登不上不保修。</li>
        <li>
          个人原因（封号、12 个月换组限制、误踢会员邮箱）导致用不了，补救或换号收{" "}
          <strong>79.9 元</strong>（不保证一定恢复）。
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
          <div className="buyer-topbar-actions">
            <WallpaperMotionToggle surface="light" />
            <Link to="/" className="btn btn-ghost btn-sm buyer-back-btn" onClick={handleBackHome}>
              <CastoriceIcon name="arrow-left" size={15} />
              <span>换个兑换码</span>
            </Link>
          </div>
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
                bubbleText={chibiBubbleText}
              />

              <div className="voucher-head voucher-head--with-sticker redeem-stagger-block" style={staggerStyle(0)}>
                <div className="row-between">
                  <div className="row" style={{ gap: 8 }}>
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
                        shimmer={isFullyCompleted}
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
                <ol
                  className={`stepper ${isExclusive ? "stepper--four" : ""} redeem-stagger-block`}
                  style={staggerStyle(2)}
                  aria-label="提货进度"
                >
                  {steps.map((label, idx) => {
                    const isDone = idx < activeStep || (isFullyCompleted && idx <= activeStep);
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
              autoPollIntervalMs={
                exclusiveAwaiting ? EXCLUSIVE_WAIT_POLL_INTERVAL_MS : AUTO_POLL_INTERVAL_MS
              }
              countdownSeconds={autoPollCountdown}
              refreshing={refreshing}
              exclusiveDismissed={exclusiveWaitDismissed}
            />

            {/* Revoked */}
            {record.status === "revoked" ? (
              <section className="card stack redeem-stagger-block" style={staggerStyle(3)}>
                <div className="callout callout-danger">
                  <CastoriceIcon name="alert" size={19} className="callout-icon" />
                  <div>
                    <strong>兑换码已作废</strong>
                    <div style={{ marginTop: 4 }}>
                      {record.buyer_notice || "该码已失效或订单已关闭。"}
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
                    <strong>客服人工处理中</strong>
                    <div style={{ marginTop: 4 }}>
                      {record.buyer_notice ||
                        "已通知客服跟进，收藏本页等自动更新。"}
                    </div>
                  </div>
                </div>
                <div className="callout callout-danger">
                  <CastoriceIcon name="alert" size={18} className="callout-icon" />
                  <div>
                    <strong>掉组或维护处理中，请勿自行退出家庭组</strong>
                    <div style={{ marginTop: 4 }}>
                      主动退组可能触发 Google 12 个月换组冷却限制，耐心等客服补发或修复即可。
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
                  <div className="kv-item">
                    <div className="kv-label">自动刷新</div>
                    <div className="kv-value tabular">{autoPollCountdown}s 后自动检查</div>
                  </div>
                </div>
                <div className="row-between" style={{ gap: 10, flexWrap: "wrap" }}>
                  <span style={{ fontSize: 13, color: "var(--ink-3)" }}>{record.support_hint}</span>
                  <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
                    <CopyButton
                      text={supportTicketText}
                      label="复制客服排查凭证"
                      copiedLabel="✓ 已复制凭证"
                      toastMessage="已复制排查凭证，直接发给客服即可快速定位"
                      variant="primary"
                      size="sm"
                    />
                    <button
                      type="button"
                      className="btn btn-secondary btn-sm"
                      onClick={() => void fetchRecord(false)}
                    >
                      <CastoriceIcon name="refresh" size={15} />
                      <span>刷一下状态</span>
                    </button>
                  </div>
                </div>
              </section>
            ) : null}

            {/* ===============================================================
                FLOW A: SHARED SLOT (ultra_shared_slot)
               =============================================================== */}
            {!isExclusive && record.status === "unused" ? (
              <section className="card stack redeem-stagger-block" style={staggerStyle(3)}>
                <div>
                  <h2 className="card-title">填开通会员的邮箱</h2>
                  <p className="card-subtitle">
                    先勾选下面 3 项再填邮箱。提交后一般 3-7 小时发邀请，最晚 24 小时，未超时不用催。
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
                      <strong>账号没在家庭组，且 12 个月内没进出过家庭组</strong>（Google 规则：<strong>12 个月内仅能换 1 次家庭组</strong>；点{" "}
                      <a href="https://g.co/yourfamily" target="_blank" rel="noreferrer">
                        g.co/yourfamily <CastoriceIcon name="external" size={13} className="inline-ext-icon" />
                      </a>{" "}
                      检查并退旧组）。
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
                      <strong>已关掉 Google 付款资料</strong>（<strong>关闭 Google 付款资料不会删除绑定的银行卡/信用卡</strong>，也不会影响账号数据；点{" "}
                      <a
                        href="https://payments.google.com/gp/w/home/settings"
                        target="_blank"
                        rel="noreferrer"
                      >
                        payments.google.com/gp/w/home/settings{" "}
                        <CastoriceIcon name="external" size={13} className="inline-ext-icon" />
                      </a>{" "}
                      关闭，不然会提示地区不符）。
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
                      <strong>已知晓 0 积分规则、排队时间与退换说明</strong>：<strong>0 积分车位禁点 Veo / Flow 等耗积分功能</strong>；提交就占名额（3-7 小时发邀请，最晚 24 小时，履约时间内不退），非商品问题换邮箱需补{" "}
                      <strong>49.9 元</strong>，或扣 <strong>49.9 元</strong>退余款。
                    </div>
                  </label>
                </div>

                <div className="inset-panel stack-sm">
                  <strong style={{ fontSize: 13.5 }}>购买和使用须知</strong>
                  <ol className="guide-steps">
                    <li>
                      <strong>质保 30 天会员</strong>，不保个人账号；违规或反代封号不保。
                    </li>
                    <li>
                      <strong>0 积分车位禁点 Veo / Flow 等耗积分功能</strong>，误耗积分或点数会被踢且不退款。
                    </li>
                    <li>
                      账号受 12 个月限制、没退旧组、没关付款资料或填错邮箱等个人原因，按{" "}
                      <strong>49.9 元</strong>换邮箱或扣 <strong>49.9 元</strong>退余款处理。
                    </li>
                  </ol>
                </div>

                <form onSubmit={(event) => void handleSlotSubmit(event)} className="stack">
                  <GmailDoubleInput
                    email={buyerEmail}
                    onEmailChange={setBuyerEmail}
                    disabled={submittingRedeem}
                    primaryLabel="收邀请的邮箱（填你自己的 Gmail）"
                    helperText="邀请将发往你下面填写的这个邮箱（不是店家提供的成品号），提交前请逐字核对拼写。"
                    warningText="别填错邮箱！提交就占名额，换邮箱需补 49.9 元手续费。"
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
                      <li className={`presubmit-item ${checkSlotRule ? "is-pass" : ""}`}>
                        {checkSlotRule ? (
                          <CastoriceIcon name="check" size={15} />
                        ) : (
                          <span className="presubmit-dot" aria-hidden="true" />
                        )}
                        <span>知晓一旦提交换邮箱需要手续费</span>
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
                    <strong>已排队，等邮件就行</strong>
                    <div style={{ marginTop: 4 }}>
                      一般 3-7 小时发邀请，最晚 24 小时，未超时不用催。收藏本页会自动更新进度。
                    </div>
                  </div>
                </div>

                <div className="kv-grid">
                  <div className="kv-item">
                    <div className="kv-label">{sharedRecipientLabel}</div>
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

                <div className="auto-refresh-pill" aria-live="polite">
                  <CastoriceIcon name="refresh" size={14} />
                  <span>
                    页面可见时每 30 秒自动查一次，下次自动刷新倒计时：
                    <strong className="tabular"> {autoPollCountdown}s</strong>
                  </span>
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
                    <strong>邀请已发出，去邮箱查收</strong>
                    <div style={{ marginTop: 4 }}>
                      打开 {is16688 ? "提货时填写的邮箱" : (
                        <span className="mono">{record.buyer_email_masked || "你填的邮箱"}</span>
                      )}，找 Google 家庭组邀请邮件（没看到就翻垃圾箱或推广）。
                    </div>
                  </div>
                </div>

                <div className="kv-grid">
                  <div className="kv-item">
                    <div className="kv-label">{sharedRecipientLabel}</div>
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
                    <li>打开邮箱，找标题带 “Google 家庭组” 或 “Join family group” 的邮件。</li>
                    <li>点 <strong>接受邀请</strong>，确认加入即可生效。</li>
                    <li>
                      提示「不在同一国家/地区」：先去付款资料设置页{" "}
                      <a
                        href="https://payments.google.com/gp/w/home/settings"
                        target="_blank"
                        rel="noreferrer"
                      >
                        payments.google.com/gp/w/home/settings{" "}
                        <CastoriceIcon name="external" size={13} className="inline-ext-icon" />
                      </a>{" "}
                      关掉付款资料，再点接受邀请。提示「12 个月内家庭组限制」：需补{" "}
                      <strong>49.9 元</strong>换邮箱，或扣 <strong>49.9 元</strong>退款。
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
                    你自己当组长：先领会员邮箱，再用常用邮箱建家庭组把它拉进去，最后回来提交主邮箱登记独享套餐交付排队（免截图）。
                  </p>
                </div>

                <div className="exclusive-steps-card">
                  <strong style={{ fontSize: 13.5 }}>3 步搞定</strong>
                  <ol className="guide-steps">
                    <li>点下面领一个<strong>会员邮箱（商店邮箱）</strong>。</li>
                    <li>
                      用你自己的<strong>常用邮箱</strong>建 Google 家庭组并拉它进组；对方同意后开通会员，<strong>还能再拉 4 个人</strong>。
                    </li>
                    <li>
                      发完邀请回来提交<strong>你的常用主邮箱</strong>，系统自动登记独享套餐交付排队（免截图；一般{" "}
                      <strong>3-7 小时，最晚 24 小时</strong>完成进组）。
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
                      <strong>已备好建组用的常用邮箱</strong>，不在别的家庭组且 12 个月内没进出过家庭组（Google 规则：<strong>12 个月内仅能换 1 次家庭组</strong>；点{" "}
                      <a href="https://g.co/yourfamily" target="_blank" rel="noreferrer">
                        g.co/yourfamily <CastoriceIcon name="external" size={13} className="inline-ext-icon" />
                      </a>{" "}
                      检查并退旧组）。
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
                      <strong>已关掉 Google 付款资料</strong>（<strong>关闭 Google 付款资料不会删除绑定的银行卡/信用卡</strong>；点{" "}
                      <a
                        href="https://payments.google.com/gp/w/home/settings"
                        target="_blank"
                        rel="noreferrer"
                      >
                        payments.google.com/gp/w/home/settings{" "}
                        <CastoriceIcon name="external" size={13} className="inline-ext-icon" />
                      </a>{" "}
                      关闭，不然可能提示地区受限）。
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
                      <strong>已知晓 0 积分与售后规则</strong>：别把会员邮箱踢出家庭组，0 积分功能慎点 Veo / Flow 等耗积分入口，发完邀请回来登记交付排队；领号后非商品问题不支持全额退款，个人原因换号或补救收{" "}
                      <strong>79.9 元</strong>（不保证一定恢复）。
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
                        <span>准备好了，领会员邮箱</span>
                      </>
                    )}
                  </button>
                </form>
              </section>
            ) : null}

            {/* Exclusive: Provisioning / Waiting Fulfillment Queue */}
            {isExclusive &&
            (record.status === "validating" ||
              record.status === "purchasing" ||
              record.status === "waiting_fulfillment") ? (
              <section className="card stack redeem-stagger-block" style={staggerStyle(3)}>
                <div className="callout callout-info">
                  <CastoriceIcon name="refresh" size={18} className="callout-icon" />
                  <div>
                    <strong>
                      {record.status === "waiting_fulfillment"
                        ? "已进入排队取号，请耐心等待会员邮箱发放"
                        : "正在为你分配独立会员邮箱"}
                    </strong>
                    <div style={{ marginTop: 4 }}>
                      {record.fulfillment.eta_text || "一般 3-7 小时完成，最晚不超过 24 小时"}
                      。拿到会员邮箱后本页会自动更新，关掉页面也不会掉队。
                    </div>
                  </div>
                </div>

                <div className="kv-grid">
                  <div className="kv-item">
                    <div className="kv-label">当前阶段</div>
                    <div className="kv-value">
                      {record.status === "waiting_fulfillment" ? "排队分配会员邮箱" : "正在出货"}
                    </div>
                  </div>
                  <div className="kv-item">
                    <div className="kv-label">已经等了</div>
                    <div className="kv-value tabular">
                      {formatElapsedMinutes(record.fulfillment.elapsed_minutes)}
                    </div>
                  </div>
                  <div className="kv-item">
                    <div className="kv-label">提交时间</div>
                    <div className="kv-value tabular">
                      {formatDateTime(record.fulfillment.submitted_at)}
                    </div>
                  </div>
                  <div className="kv-item">
                    <div className="kv-label">上次同步</div>
                    <div className="kv-value tabular">
                      {formatDateTime(record.fulfillment.last_polled_at)}
                    </div>
                  </div>
                </div>

                <div className="auto-refresh-pill" aria-live="polite">
                  <CastoriceIcon name="refresh" size={14} />
                  <span>
                    页面可见时每 30 秒自动查一次，下次自动刷新倒计时：
                    <strong className="tabular"> {autoPollCountdown}s</strong>
                  </span>
                </div>

                <div className="row-between" style={{ gap: 10, flexWrap: "wrap" }}>
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

            {/* Exclusive: Post-Warranty Delivery Waiting Action Card */}
            {isExclusive && exclusiveAwaiting ? (
              <section
                className={`card stack ewc-card ewc-card--${exclusivePhase} redeem-stagger-block`}
                style={staggerStyle(3)}
                data-testid="exclusive-waiting-action-card"
              >
                {exclusivePhase === "overdue" ? (
                  <div className="ewc-overdue-bar" role="alert">
                    <div className="ewc-overdue-text">
                      <strong>等待已超过 24 小时</strong>
                      <span>
                        请先点下方「去 Google 家庭组看进度」确认是否已入组；若仍显示待接受，请一键复制凭证发客服优先加急。
                      </span>
                    </div>
                    <CopyButton
                      text={supportTicketText}
                      label="复制客服加急凭证"
                      copiedLabel="✓ 已复制凭证"
                      toastMessage="已复制加急排查凭证，直接发给客服即可优先处理"
                      variant="primary"
                      size="sm"
                    />
                  </div>
                ) : null}

                <div className="ewc-cta-grid">
                  <a
                    href="https://g.co/yourfamily"
                    target="_blank"
                    rel="noreferrer"
                    className="btn btn-primary"
                  >
                    <CastoriceIcon name="check" size={16} />
                    <span>去 Google 家庭组看进度</span>
                    <CastoriceIcon name="external" size={14} />
                  </a>
                  <a
                    href="https://gemini.google.com"
                    target="_blank"
                    rel="noreferrer"
                    className="btn btn-secondary"
                  >
                    <CastoriceIcon name="emblem" size={16} />
                    <span>去 Gemini 看会员状态</span>
                    <CastoriceIcon name="external" size={14} />
                  </a>
                </div>

                <div className="ewc-footer-bar">
                  <span className="ewc-footer-sync" aria-live="polite">
                    <CastoriceIcon name="refresh" size={14} />
                    <span>
                      关掉页面不影响排队，系统每 60 秒自动同步
                    </span>
                  </span>
                  <div className="ewc-footer-actions">
                    <button
                      type="button"
                      className={`btn btn-secondary btn-sm ${refreshing ? "is-loading" : ""}`}
                      disabled={refreshing || cooldownLeft > 0}
                      aria-busy={refreshing}
                      onClick={() => void handleManualRefresh()}
                    >
                      <CastoriceIcon name="refresh" size={14} spin={refreshing} />
                      <span>
                        {refreshing
                          ? "同步中..."
                          : cooldownLeft > 0
                          ? `稍后再刷 (${cooldownLeft}s)`
                          : "手动刷一下"}
                      </span>
                    </button>
                    <button
                      type="button"
                      className="btn btn-ghost btn-sm"
                      data-testid="exclusive-dismiss-wait-btn"
                      onClick={handleDismissExclusiveWait}
                    >
                      我已看到进组生效，收起等待
                    </button>
                  </div>
                </div>
              </section>
            ) : null}

            {/* Exclusive: Reversible Restore Bar When Buyer Dismissed Waiting View */}
            {isExclusive && exclusiveAwaitingRaw && exclusiveWaitDismissed && !exclusiveActivated ? (
              <div
                className="ewc-restore-bar redeem-stagger-block"
                style={staggerStyle(3)}
                data-testid="exclusive-restore-wait-bar"
              >
                <span>已收起等待进度视图 · 如家庭组尚未显示生效，可随时重新展开</span>
                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  data-testid="exclusive-restore-wait-btn"
                  onClick={handleRestoreExclusiveWait}
                >
                  重新展开进度
                </button>
              </div>
            ) : null}

            {/* Exclusive: Confirmed Activated Card */}
            {isExclusive && exclusiveActivated ? (
              <section
                className="card stack ewc-activated-card redeem-stagger-block"
                style={staggerStyle(3)}
                data-testid="exclusive-activated-card"
              >
                <div className="callout callout-success">
                  <CastoriceIcon name="check" size={20} className="callout-icon" />
                  <div>
                    <strong>独享家庭组已进组生效</strong>
                    <div style={{ marginTop: 4 }}>
                      商店会员邮箱已加入你的家庭组（确认时间：
                      <span className="tabular">
                        {formatDateTime(record.fulfillment.exclusive_activated_at)}
                      </span>
                      ）。你现在可以直接使用会员，并还能再拉入最多 4 位家人共享。
                    </div>
                  </div>
                </div>
                <div className="ewc-cta-grid">
                  <a
                    href="https://g.co/yourfamily"
                    target="_blank"
                    rel="noreferrer"
                    className="btn btn-primary"
                  >
                    <CastoriceIcon name="check" size={16} />
                    <span>去家庭组管理 / 拉家人</span>
                    <CastoriceIcon name="external" size={14} />
                  </a>
                  <a
                    href="https://gemini.google.com"
                    target="_blank"
                    rel="noreferrer"
                    className="btn btn-secondary"
                  >
                    <CastoriceIcon name="emblem" size={16} />
                    <span>去打开 Gemini</span>
                    <CastoriceIcon name="external" size={14} />
                  </a>
                </div>
              </section>
            ) : null}

            {/* Exclusive: Delivered (Pending Warranty or Completed) */}
            {isExclusive && record.status === "delivered_pending_warranty" ? (
              <>
                <section className="card stack redeem-stagger-block" style={staggerStyle(3)}>
                  <div className="row-between">
                    <h2 className="card-title">第 1 步：复制会员邮箱</h2>
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
                          <span className="field-hint">用来拉进家庭组，别填到下面登记框</span>
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
                    <strong style={{ fontSize: 13.5 }}>第 2 步：用常用邮箱建组并邀请它</strong>
                    <ol className="guide-steps">
                      <li>
                        用你的<strong>常用邮箱</strong>打开{" "}
                        <a href="https://g.co/yourfamily" target="_blank" rel="noreferrer">
                          g.co/yourfamily <CastoriceIcon name="external" size={13} className="inline-ext-icon" />
                        </a>{" "}
                        建家庭组。
                      </li>
                      <li>
                        点「发送邀请」，把上面<strong>会员邮箱</strong>粘进去；对方同意后生效，<strong>还能再拉 4 个人</strong>。
                      </li>
                      <li>
                        发完邀请在下面提交<strong>你的常用主邮箱</strong>，系统登记后自动排入独享套餐交付队列（免截图；一般{" "}
                        <strong>3-7 小时，最晚 24 小时</strong>进组）。
                      </li>
                    </ol>
                  </div>
                </section>

                {/* Delivery Queue Registration Section */}
                <section className="card stack redeem-stagger-block" style={staggerStyle(4)}>
                  <div className="row-between">
                    <div style={{ flex: "1 1 240px", minWidth: 0 }}>
                      <h2 className="card-title">第 3 步：提交主邮箱，登记交付排队</h2>
                      <p className="card-subtitle">
                        填你建家庭组的常用主邮箱（免截图），提交后自动登记独享套餐交付排队。
                      </p>
                    </div>
                    <ToneBadge label={buyerWarrantyBadge.label} tone={buyerWarrantyBadge.tone} />
                  </div>

                  <form onSubmit={handleWarrantySubmit} className="stack">
                    <GmailDoubleInput
                      email={adminEmail}
                      onEmailChange={setAdminEmail}
                      primaryLabel="建家庭组的常用主邮箱"
                      warningText="填你自己建组的常用邮箱，别填成上面的会员邮箱。"
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
                        placeholder="已发家庭组邀请，请帮忙安排进组"
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

                    <button
                      type="submit"
                      className={`btn btn-primary btn-lg btn-block ${submittingWarranty ? "is-loading" : ""}`}
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
                          <span>正在提交登记...</span>
                        </>
                      ) : (
                        <>
                          <CastoriceIcon name="check" size={18} />
                          <span>提交主邮箱，进入交付排队</span>
                        </>
                      )}
                    </button>
                  </form>

                  {exclusiveWarrantyNotice}
                </section>
              </>
            ) : null}

            {/* Exclusive: Compact Registered Delivery Summary (Completed / Waiting / Activated) */}
            {isExclusive && record.status === "completed" ? (
              <section
                className="card stack redeem-stagger-block"
                style={staggerStyle(4)}
                data-testid="exclusive-delivery-summary"
              >
                <div className="row-between">
                  <h2 className="card-title">已登记的交付信息</h2>
                  <ToneBadge label={buyerWarrantyBadge.label} tone={buyerWarrantyBadge.tone} />
                </div>

                {storeEmail ? (
                  <div className="store-email-box">
                    <div className="stack-sm grow">
                      <div className="email-line">
                        <span className="email-role-tag email-role-tag-store">会员邮箱（商店邮箱）</span>
                        <span className="field-hint">已发放的会员邮箱，可随时核对家庭组成员</span>
                      </div>
                      <div className="credential-value">{storeEmail}</div>
                    </div>
                    <CopyButton
                      text={storeEmail}
                      label="复制会员邮箱"
                      toastMessage="会员邮箱已复制"
                      variant="secondary"
                    />
                  </div>
                ) : null}

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

                {warrantySubmitted && (!editingWarranty || warrantyLocked) ? (
                  <div className="stack-sm">
                    <div className="kv-grid">
                      <div className="kv-item">
                        <div className="kv-label">你的常用主邮箱</div>
                        <div className="kv-value mono">{record.warranty?.admin_email_masked || "-"}</div>
                      </div>
                      <div className="kv-item">
                        <div className="kv-label">登记状态</div>
                        <div className="kv-value">
                          {exclusiveActivated
                            ? "已进组生效"
                            : warrantyLocked
                            ? "已完成系统登记"
                            : "已进入交付排队"}
                        </div>
                      </div>
                      <div className="kv-item">
                        <div className="kv-label">提交时间</div>
                        <div className="kv-value tabular">{formatDateTime(record.warranty?.submitted_at)}</div>
                      </div>
                      <div className="kv-item">
                        <div className="kv-label">领号时间</div>
                        <div className="kv-value tabular">{formatDateTime(record.delivery?.delivered_at)}</div>
                      </div>
                    </div>

                    {record.warranty?.note ? (
                      <div className="inset-panel" style={{ fontSize: 13.5, color: "var(--ink-2)" }}>
                        <span style={{ color: "var(--ink-3)" }}>备注：</span>
                        {record.warranty.note}
                      </div>
                    ) : null}

                    {!warrantyLocked ? (
                      <div className="ewc-edit-bar">
                        <span className="ewc-edit-hint">填错了主邮箱？进组前可随时修改</span>
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
                      primaryLabel="建家庭组的常用主邮箱"
                      warningText="填你自己建组的常用邮箱，别填成上面的会员邮箱。"
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
                        placeholder="已发家庭组邀请，请帮忙安排进组"
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
                            <span>正在保存登记...</span>
                          </>
                        ) : (
                          <>
                            <CastoriceIcon name="check" size={18} />
                            <span>保存主邮箱登记</span>
                          </>
                        )}
                      </button>
                    </div>
                  </form>
                )}

                <details className="ewc-notice-details">
                  <summary className="ewc-notice-summary">使用与售后须知</summary>
                  <div className="ewc-notice-body">
                    <ol className="guide-steps">
                      <li>
                        <strong>千万别把会员邮箱踢出家庭组</strong>，踢掉立刻掉会员且不保修。
                      </li>
                      <li>
                        <strong>质保 30 天</strong>（从领号起算），非个人原因掉会员免费保修；领号后非商品问题不支持全额退款。
                      </li>
                      <li>自己账号被封、登不上不保修。</li>
                      <li>
                        个人原因（封号、12 个月换组限制、误踢会员邮箱）导致用不了，补救或换号收{" "}
                        <strong>79.9 元</strong>（不保证一定恢复）。
                      </li>
                    </ol>
                  </div>
                </details>
              </section>
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
