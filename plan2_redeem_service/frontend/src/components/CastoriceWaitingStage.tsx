import { type CSSProperties, useEffect, useRef, useState } from "react";
import type { PublicCodeResponse } from "../types/api";
import { formatDateTime, formatElapsedMinutes } from "../utils/format";
import "../styles/waiting-stage.css";

export const EXCLUSIVE_WAIT_MAX_MINUTES = 36 * 60;

interface CastoriceWaitingStageProps {
  record: PublicCodeResponse;
  autoPollIntervalMs?: number;
  refreshing?: boolean;
  exclusiveDismissed?: boolean;
}

type WaitingStageMode = "tea" | "courier" | "exclusive_wait" | "none";
type ExclusiveWaitPhase = "golden" | "peak" | "overdue";

interface BurstItem {
  id: number;
  kind: "butterfly" | "star";
  originX: string;
  originY: string;
  deltaX: number;
  deltaY: number;
  rotation: number;
}

interface MilestoneStep {
  title: string;
  detail: string;
  state: "done" | "active" | "pending";
  meta?: string;
}

const TEA_WAIT_ASSET = "/assets/waiting/castorice-tea-wait.png";
const COURIER_RUN_ASSET = "/assets/waiting/castorice-courier-run.png";
const EXCLUSIVE_WAIT_ASSET = "/assets/waiting/castorice-exclusive-wait.webp";
const EXCLUSIVE_CHECK_ASSET = "/assets/waiting/castorice-exclusive-check.webp";
const MINI_BUTTERFLY_ASSET = "/assets/waiting/mini-butterfly-glow.png";

const TEA_REASSURANCE_TIPS = [
  "邮箱已提交，正在排队拉进家庭组，不用重复操作",
  "一般 3-7 小时发邀请，最晚 24 小时；关掉页面也会继续排",
  "发出邀请后页面会自动变绿，记得看收件箱和垃圾箱",
] as const;

const POKE_HEALING_LINES = [
  "别急呀，小蝶正替你盯着队列进度呢",
  "去喝口水歇会儿，进度到了会自动刷新",
  "紫蝶信使正在穿梭加速中，很快就好",
  "关掉页面也不会掉队，忙完回来看就可以",
  "收到你的戳戳啦，好运和进组通知都在路上",
] as const;

const EXCLUSIVE_POKE_LINES = [
  "你的家庭组邀请已登记在案，源头正按序处理",
  "一般 3-7 小时就会同意进组，去忙别的不用一直盯屏",
  "对方一点同意，你就能立马再拉 4 位家人一起用",
  "关掉本页也不会丢进度，随时回来点去家庭组看一眼就行",
  "收到戳戳，紫蝶正在替你守着 6 席家庭组星环",
] as const;

const BLOOM_VECTORS = [
  { dx: -96, dy: -56, rot: -24 },
  { dx: -48, dy: -76, rot: -12 },
  { dx: 0, dy: -84, rot: 4 },
  { dx: 52, dy: -74, rot: 16 },
  { dx: 96, dy: -52, rot: 28 },
  { dx: -72, dy: -22, rot: -18 },
] as const;

const CONSTELLATION_SEATS = [
  { id: "leader", role: "leader" as const, cx: 110, cy: 20 },
  { id: "member", role: "member" as const, cx: 187.9, cy: 65 },
  { id: "family-1", role: "reserved" as const, cx: 187.9, cy: 155 },
  { id: "family-2", role: "reserved" as const, cx: 110, cy: 200 },
  { id: "family-3", role: "reserved" as const, cx: 32.1, cy: 155 },
  { id: "family-4", role: "reserved" as const, cx: 32.1, cy: 65 },
] as const;

function parseFallbackElapsedMinutes(isoText: string | null | undefined): number | null {
  const raw = (isoText || "").trim();
  if (!raw) return null;
  const parsedMs = Date.parse(raw);
  if (Number.isNaN(parsedMs)) return null;
  return Math.max(0, Math.floor((Date.now() - parsedMs) / 60_000));
}

function resolveBaseWarrantyElapsedMinutes(record: PublicCodeResponse): number {
  if (
    typeof record.fulfillment.warranty_elapsed_minutes === "number" &&
    Number.isFinite(record.fulfillment.warranty_elapsed_minutes)
  ) {
    return Math.max(0, Math.floor(record.fulfillment.warranty_elapsed_minutes));
  }
  const fallback = parseFallbackElapsedMinutes(
    record.fulfillment.warranty_submitted_at ?? record.warranty?.submitted_at
  );
  return fallback ?? 0;
}

export function isExclusiveAwaitingActivation(
  record: PublicCodeResponse,
  dismissed = false
): boolean {
  if (dismissed) return false;
  if (record.product_type !== "account") return false;
  if (record.status !== "completed") return false;
  const warrantyStatus = record.warranty?.status;
  if (warrantyStatus !== "submitted" && warrantyStatus !== "registered_upstream") {
    return false;
  }
  if (record.fulfillment.exclusive_activated_at) return false;
  const elapsedMinutes = resolveBaseWarrantyElapsedMinutes(record);
  return elapsedMinutes < EXCLUSIVE_WAIT_MAX_MINUTES;
}

function resolveWaitingStageMode(
  record: PublicCodeResponse,
  exclusiveDismissed: boolean
): WaitingStageMode {
  const isExclusive = record.product_type === "account";
  if (
    record.status === "unused" ||
    record.status === "revoked" ||
    record.status === "needs_manual_review"
  ) {
    return "none";
  }

  if (!isExclusive) {
    if (record.status === "waiting_fulfillment") {
      return "tea";
    }
    if (record.status === "validating" || record.status === "purchasing") {
      return "courier";
    }
    return "none";
  }

  if (isExclusiveAwaitingActivation(record, exclusiveDismissed)) {
    return "exclusive_wait";
  }

  if (
    record.status === "validating" ||
    record.status === "purchasing" ||
    record.status === "waiting_fulfillment" ||
    record.status === "delivered_pending_warranty"
  ) {
    return "courier";
  }

  return "none";
}

function isTerminalCompletedRecord(record: PublicCodeResponse): boolean {
  if (record.status !== "completed") {
    return false;
  }
  if (record.product_type === "account" && isExclusiveAwaitingActivation(record, false)) {
    return false;
  }
  return true;
}

function resolveExclusivePhase(elapsedMinutes: number): ExclusiveWaitPhase {
  if (elapsedMinutes >= 24 * 60) return "overdue";
  if (elapsedMinutes >= 7 * 60) return "peak";
  return "golden";
}

function computeExclusiveSlaPercent(elapsedMinutes: number): number {
  const clamped = Math.max(0, elapsedMinutes);
  if (clamped <= 180) {
    return Math.max(4, Math.round((clamped / 180) * 28));
  }
  if (clamped <= 420) {
    return Math.round(28 + ((clamped - 180) / 240) * 38);
  }
  if (clamped < 1440) {
    return Math.min(98, Math.round(66 + ((clamped - 420) / 1020) * 34));
  }
  return 100;
}

function formatCountdownClock(totalSeconds: number): string {
  const clamped = Math.max(0, Math.floor(totalSeconds));
  const minutes = String(Math.floor(clamped / 60)).padStart(2, "0");
  const seconds = String(clamped % 60).padStart(2, "0");
  return `${minutes}:${seconds}`;
}

function resolveCourierTrackMeta(record: PublicCodeResponse): {
  nodes: [string, string, string];
  activeNode: 0 | 1 | 2;
  progressPct: number;
  caption: string;
  statusPill: string | null;
  spriteSrc: string;
  spriteAlt: string;
  isExclusiveCheckSprite: boolean;
} {
  const isExclusive = record.product_type === "account";
  if (!isExclusive) {
    const activeNode: 0 | 1 = record.status === "validating" ? 0 : 1;
    return {
      nodes: ["核对兑换码", "提交排队", "等待发邀请"],
      activeNode,
      progressPct: activeNode === 0 ? 22 : 56,
      caption: "正在核对兑换码并安排排队，马上就好",
      statusPill: null,
      spriteSrc: COURIER_RUN_ASSET,
      spriteAlt: "Q 版瑕蝶捧信火速派送",
      isExclusiveCheckSprite: false,
    };
  }

  if (record.status === "validating") {
    return {
      nodes: ["核对兑换码", "分配会员邮箱", "建组填主邮箱"],
      activeNode: 0,
      progressPct: 18,
      caption: "正在核对兑换码，马上分配独立会员邮箱…",
      statusPill: null,
      spriteSrc: COURIER_RUN_ASSET,
      spriteAlt: "Q 版瑕蝶捧信火速派送",
      isExclusiveCheckSprite: false,
    };
  }

  if (record.status === "purchasing") {
    return {
      nodes: ["核对兑换码", "分配会员邮箱", "建组填主邮箱"],
      activeNode: 1,
      progressPct: 50,
      caption: "正在向上游申请独立会员邮箱，请稍等片刻",
      statusPill: null,
      spriteSrc: COURIER_RUN_ASSET,
      spriteAlt: "Q 版瑕蝶捧信火速派送",
      isExclusiveCheckSprite: false,
    };
  }

  if (record.status === "waiting_fulfillment") {
    return {
      nodes: ["核对兑换码", "排队分配邮箱", "建组填主邮箱"],
      activeNode: 1,
      progressPct: 56,
      caption: "上游已接单，正在排队分配独立会员邮箱 · 一般 3-7 小时，最晚 24 小时",
      statusPill: "排队取号中",
      spriteSrc: EXCLUSIVE_CHECK_ASSET,
      spriteAlt: "Q 版瑕蝶核对排队出号进度",
      isExclusiveCheckSprite: true,
    };
  }

  return {
    nodes: ["领取会员邮箱", "建组拉它入组", "填主邮箱进入等待"],
    activeNode: 2,
    progressPct: 82,
    caption: "会员邮箱领到了！拉它进家庭组后填主邮箱登记质保，即进入 3-7 小时交付等待",
    statusPill: "第 2/4 步 · 待填主邮箱",
    spriteSrc: COURIER_RUN_ASSET,
    spriteAlt: "Q 版瑕蝶捧信火速派送",
    isExclusiveCheckSprite: false,
  };
}

function resolveExclusivePhaseMeta(phase: ExclusiveWaitPhase): {
  badgeTitle: string;
  badgeSub: string;
  slaTag: string;
  etaHeadline: string;
  etaSub: string;
  stickerSrc: string;
  stickerAlt: string;
  step3Detail: string;
  footerHint: string;
} {
  if (phase === "overdue") {
    return {
      badgeTitle: "等待已超 24 小时",
      badgeSub: "建议加急",
      slaTag: "已超 24h 承诺窗",
      etaHeadline: "优先加急",
      etaSub: "已超 24h 承诺，建议联系客服",
      stickerSrc: EXCLUSIVE_CHECK_ASSET,
      stickerAlt: "Q 版瑕蝶核对超时加急进度",
      step3Detail: "已超 24 小时承诺窗，请先打开家庭组确认是否已入组，未入组请带凭证找客服加急",
      footerHint: "超 24 小时未入组可点击下方凭证按钮，发客服优先人工处理",
    };
  }
  if (phase === "peak") {
    return {
      badgeTitle: "已进入交付等待 · 高峰排队",
      badgeSub: "仍在 24h 内",
      slaTag: "高峰处理窗（7–24h）",
      etaHeadline: "24 小时内",
      etaSub: "常规 3-7h 已过，仍在承诺内",
      stickerSrc: EXCLUSIVE_CHECK_ASSET,
      stickerAlt: "Q 版瑕蝶核对高峰排队进度",
      step3Detail: "当前处于高峰时段，仍在 24 小时承诺窗内，源头同意后即刻生效",
      footerHint: "源头按序处理中，关掉页面不影响排队进度",
    };
  }
  return {
    badgeTitle: "已进入交付等待 · 黄金窗",
    badgeSub: "3-7h 常规 / ≤24h",
    slaTag: "常规交付窗（3–7h）",
    etaHeadline: "3-7 小时",
    etaSub: "最晚不超过 24 小时",
    stickerSrc: EXCLUSIVE_WAIT_ASSET,
    stickerAlt: "Q 版瑕蝶守护独享家庭组等待",
    step3Detail: "源头正按序登录会员邮箱同意你的家庭组邀请（一般 3-7 小时，最晚 24 小时）",
    footerHint: "关闭页面也不会影响进组进度，随时回来查看即可",
  };
}

function SparkleStarSvg({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 16 16"
      fill="none"
      aria-hidden="true"
      className={className}
    >
      <path
        d="M8 1L9.65 6.35L15 8L9.65 9.65L8 15L6.35 9.65L1 8L6.35 6.35L8 1Z"
        fill="currentColor"
      />
    </svg>
  );
}

function MilestoneCheckSvg() {
  return (
    <svg viewBox="0 0 12 12" fill="none" aria-hidden="true" className="cws-ms-check-svg">
      <path
        d="M2.5 6.2L4.9 8.6L9.6 3.5"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function FamilySeatConstellation() {
  return (
    <svg
      viewBox="0 0 220 220"
      className="cws-constellation-svg"
      aria-hidden="true"
    >
      <circle
        cx="110"
        cy="110"
        r="90"
        className="cws-constellation-ring"
      />
      <circle
        cx="110"
        cy="110"
        r="69"
        className="cws-constellation-inner"
      />
      <path
        d="M 110 20 A 90 90 0 0 1 187.9 65"
        className="cws-constellation-active-arc"
      />
      {CONSTELLATION_SEATS.map((seat) => {
        if (seat.role === "leader") {
          return (
            <g key={seat.id} className="cws-seat cws-seat--leader">
              <circle cx={seat.cx} cy={seat.cy} r="13" className="cws-seat-halo" />
              <circle cx={seat.cx} cy={seat.cy} r="8.5" className="cws-seat-core" />
              <path
                d={`M ${seat.cx - 3.2} ${seat.cy + 0.2} L ${seat.cx - 0.8} ${seat.cy + 2.7} L ${seat.cx + 3.5} ${seat.cy - 2.3}`}
                className="cws-seat-check"
              />
            </g>
          );
        }
        if (seat.role === "member") {
          return (
            <g key={seat.id} className="cws-seat cws-seat--member">
              <circle cx={seat.cx} cy={seat.cy} r="14" className="cws-seat-sonar" />
              <circle cx={seat.cx} cy={seat.cy} r="9" className="cws-seat-core" />
              <circle cx={seat.cx} cy={seat.cy} r="3" className="cws-seat-dot" />
            </g>
          );
        }
        return (
          <g key={seat.id} className="cws-seat cws-seat--reserved">
            <circle cx={seat.cx} cy={seat.cy} r="7.5" className="cws-seat-core" />
            <circle cx={seat.cx} cy={seat.cy} r="2" className="cws-seat-dot" />
          </g>
        );
      })}
    </svg>
  );
}

export function CastoriceWaitingStage({
  record,
  autoPollIntervalMs = 30_000,
  refreshing = false,
  exclusiveDismissed = false,
}: CastoriceWaitingStageProps) {
  const pollCycleSeconds = Math.max(5, Math.round(autoPollIntervalMs / 1000));
  const currentMode = resolveWaitingStageMode(record, exclusiveDismissed);
  const autoPollingActive =
    currentMode === "exclusive_wait" ||
    record.status === "waiting_fulfillment" ||
    record.status === "validating" ||
    record.status === "purchasing";

  const [secondsLeft, setSecondsLeft] = useState<number>(pollCycleSeconds);
  const [extraElapsedMin, setExtraElapsedMin] = useState<number>(0);
  const [tipIndex, setTipIndex] = useState<number>(0);
  const [companionCount, setCompanionCount] = useState<number>(0);
  const [speechLine, setSpeechLine] = useState<string | null>(null);
  const [bouncing, setBouncing] = useState<boolean>(false);
  const [burstItems, setBurstItems] = useState<BurstItem[]>([]);
  const [isBlooming, setIsBlooming] = useState<boolean>(false);

  const prevCodeRef = useRef<string>(record.code);
  const prevModeRef = useRef<WaitingStageMode>(currentMode);
  const prevDismissedRef = useRef<boolean>(exclusiveDismissed);
  const lastSpeechIdxRef = useRef<number>(-1);
  const burstSeqRef = useRef<number>(1);
  const speechTimerRef = useRef<number | null>(null);
  const bounceTimerRef = useRef<number | null>(null);
  const bounceRafRef = useRef<number | null>(null);
  const burstTimersRef = useRef<Set<number>>(new Set());

  const clearTransientTimers = () => {
    if (speechTimerRef.current !== null) {
      window.clearTimeout(speechTimerRef.current);
      speechTimerRef.current = null;
    }
    if (bounceTimerRef.current !== null) {
      window.clearTimeout(bounceTimerRef.current);
      bounceTimerRef.current = null;
    }
    if (bounceRafRef.current !== null) {
      window.cancelAnimationFrame(bounceRafRef.current);
      bounceRafRef.current = null;
    }
    burstTimersRef.current.forEach((timerHandle) => {
      window.clearTimeout(timerHandle);
    });
    burstTimersRef.current.clear();
  };

  useEffect(() => {
    if (prevCodeRef.current !== record.code) {
      prevCodeRef.current = record.code;
      prevModeRef.current = currentMode;
      prevDismissedRef.current = exclusiveDismissed;
      lastSpeechIdxRef.current = -1;
      clearTransientTimers();
      setIsBlooming(false);
      setSpeechLine(null);
      setCompanionCount(0);
      setBurstItems([]);
      setBouncing(false);
      setTipIndex(0);
      setExtraElapsedMin(0);
      setSecondsLeft(pollCycleSeconds);
      return;
    }

    const wasWaiting =
      prevModeRef.current === "tea" ||
      prevModeRef.current === "courier" ||
      prevModeRef.current === "exclusive_wait";
    const manuallyDismissedNow = !prevDismissedRef.current && exclusiveDismissed;

    prevModeRef.current = currentMode;
    prevDismissedRef.current = exclusiveDismissed;

    if (
      wasWaiting &&
      currentMode === "none" &&
      !manuallyDismissedNow &&
      isTerminalCompletedRecord(record)
    ) {
      setIsBlooming(true);
    }
  }, [currentMode, exclusiveDismissed, pollCycleSeconds, record]);

  useEffect(() => {
    setExtraElapsedMin(0);
  }, [record.code, record.updated_at, record.fulfillment.warranty_elapsed_minutes]);

  useEffect(() => {
    if (currentMode !== "exclusive_wait") return;
    const minuteTimer = window.setInterval(() => {
      setExtraElapsedMin((prevMinutes) => prevMinutes + 1);
    }, 60_000);
    return () => window.clearInterval(minuteTimer);
  }, [currentMode]);

  useEffect(() => {
    if (!isBlooming) return;
    const bloomTimer = window.setTimeout(() => {
      setIsBlooming(false);
    }, 1500);
    return () => window.clearTimeout(bloomTimer);
  }, [isBlooming]);

  useEffect(() => {
    setSecondsLeft(pollCycleSeconds);
  }, [pollCycleSeconds, record.updated_at, record.fulfillment.last_polled_at]);

  useEffect(() => {
    if (!refreshing) return;
    setSecondsLeft(pollCycleSeconds);
  }, [pollCycleSeconds, refreshing]);

  useEffect(() => {
    if (currentMode === "none" || !autoPollingActive) return;
    const intervalId = window.setInterval(() => {
      setSecondsLeft((prevSeconds) => (prevSeconds <= 1 ? pollCycleSeconds : prevSeconds - 1));
    }, 1000);
    return () => window.clearInterval(intervalId);
  }, [autoPollingActive, currentMode, pollCycleSeconds]);

  useEffect(() => {
    if (currentMode !== "tea") return;
    const tipTimer = window.setInterval(() => {
      setTipIndex((prevTip) => (prevTip + 1) % TEA_REASSURANCE_TIPS.length);
    }, 6000);
    return () => window.clearInterval(tipTimer);
  }, [currentMode]);

  useEffect(() => {
    return () => {
      clearTransientTimers();
    };
  }, []);

  if (currentMode === "none" && !isBlooming) {
    return null;
  }

  const courierMeta = resolveCourierTrackMeta(record);
  const baseWarrantyElapsed = resolveBaseWarrantyElapsedMinutes(record);
  const displayElapsedMinutes = Math.max(0, baseWarrantyElapsed + extraElapsedMin);
  const exclusivePhase = resolveExclusivePhase(displayElapsedMinutes);
  const exclusiveMeta = resolveExclusivePhaseMeta(exclusivePhase);
  const slaPercent = computeExclusiveSlaPercent(displayElapsedMinutes);

  const handlePokeCastorice = () => {
    setCompanionCount((prevCount) => prevCount + 1);

    const linePool = currentMode === "exclusive_wait" ? EXCLUSIVE_POKE_LINES : POKE_HEALING_LINES;
    let nextLineIdx = Math.floor(Math.random() * linePool.length);
    if (linePool.length > 1 && nextLineIdx === lastSpeechIdxRef.current) {
      nextLineIdx = (nextLineIdx + 1) % linePool.length;
    }
    lastSpeechIdxRef.current = nextLineIdx;
    setSpeechLine(linePool[nextLineIdx]);

    if (speechTimerRef.current !== null) {
      window.clearTimeout(speechTimerRef.current);
    }
    speechTimerRef.current = window.setTimeout(() => {
      setSpeechLine(null);
    }, 4200);

    setBouncing(false);
    if (bounceRafRef.current !== null) {
      window.cancelAnimationFrame(bounceRafRef.current);
    }
    bounceRafRef.current = window.requestAnimationFrame(() => {
      bounceRafRef.current = null;
      setBouncing(true);
    });
    if (bounceTimerRef.current !== null) {
      window.clearTimeout(bounceTimerRef.current);
    }
    bounceTimerRef.current = window.setTimeout(() => {
      setBouncing(false);
    }, 580);

    const spawnCount = 5 + (burstSeqRef.current % 2);
    const originX =
      currentMode === "exclusive_wait"
        ? "122px"
        : currentMode === "tea"
        ? "76px"
        : `${courierMeta.progressPct}%`;
    const originY = currentMode === "exclusive_wait" ? "138px" : currentMode === "tea" ? "76px" : "68px";
    const createdBatch: BurstItem[] = [];

    for (let itemIndex = 0; itemIndex < spawnCount; itemIndex += 1) {
      const nextId = burstSeqRef.current;
      burstSeqRef.current += 1;
      const spreadAngle = -Math.PI * (0.18 + (itemIndex / Math.max(1, spawnCount - 1)) * 0.64);
      const travel = 42 + Math.random() * 28;
      createdBatch.push({
        id: nextId,
        kind: itemIndex % 2 === 0 ? "butterfly" : "star",
        originX,
        originY,
        deltaX: Math.round(Math.cos(spreadAngle) * travel),
        deltaY: Math.round(Math.sin(spreadAngle) * travel - 14),
        rotation: Math.round((Math.random() - 0.5) * 48),
      });
    }

    const createdIds = new Set(createdBatch.map((entry) => entry.id));
    setBurstItems((prevItems) => [...prevItems.slice(-10), ...createdBatch]);
    const burstCleanupTimer = window.setTimeout(() => {
      burstTimersRef.current.delete(burstCleanupTimer);
      setBurstItems((prevItems) => prevItems.filter((entry) => !createdIds.has(entry.id)));
    }, 980);
    burstTimersRef.current.add(burstCleanupTimer);
  };

  if (isBlooming) {
    const isExclusiveDone = record.product_type === "account";
    return (
      <section
        className="cws-stage is-blooming"
        data-testid="castorice-waiting-stage"
        data-stage-mode="bloom"
        aria-live="polite"
      >
        <div className="cws-bloom-wrap">
          {BLOOM_VECTORS.map((vec, vecIdx) => {
            const bloomStyle = {
              "--cws-bx": `${vec.dx}px`,
              "--cws-by": `${vec.dy}px`,
              "--cws-brot": `${vec.rot}deg`,
            } as CSSProperties;
            return (
              <img
                key={vecIdx}
                src={MINI_BUTTERFLY_ASSET}
                alt=""
                aria-hidden="true"
                className="cws-bloom-butterfly"
                style={bloomStyle}
              />
            );
          })}
          <div className="cws-bloom-badge" aria-hidden="true">
            <MilestoneCheckSvg />
          </div>
          <div className="cws-bloom-title">
            {isExclusiveDone ? "独享家庭组已生效！" : "家庭组邀请已发出！"}
          </div>
          <div className="cws-bloom-sub">
            {isExclusiveDone
              ? "30 天质保已就绪，现在可以拉入最多 4 位家人共享会员"
              : "已送到，马上为你打开查收操作说明…"}
          </div>
        </div>
      </section>
    );
  }

  const cycleProgressPct = Math.min(
    100,
    Math.max(12, Math.round(((pollCycleSeconds - secondsLeft) / pollCycleSeconds) * 88 + 12))
  );
  const courierBubbleSideClass =
    courierMeta.progressPct >= 48 ? "is-side-left" : "is-side-right";

  const warrantySubmittedTime =
    record.fulfillment.warranty_submitted_at ?? record.warranty?.submitted_at ?? null;
  const exclusiveMilestones: MilestoneStep[] = [
    {
      title: "领取独立会员邮箱",
      detail: "商店套餐会员邮箱已发放，可随时在下方复制核对",
      state: "done",
      meta: record.delivery?.delivered_at ? formatDateTime(record.delivery.delivered_at) : "已完成",
    },
    {
      title: "建组邀请并登记主邮箱",
      detail:
        record.warranty?.status === "registered_upstream"
          ? "主邮箱已提交并完成 30 天质保自动报备"
          : "主邮箱已登记，系统正在同步官方 30 天质保报备",
      state: "done",
      meta: warrantySubmittedTime ? formatDateTime(warrantySubmittedTime) : "已提交",
    },
    {
      title: "等待源头登录同意进组",
      detail: exclusiveMeta.step3Detail,
      state: "active",
      meta: exclusiveMeta.slaTag,
    },
    {
      title: "全员 6 席会员生效",
      detail: "对方同意后立即开启会员，作为组长还能再邀请 4 位家人共享",
      state: "pending",
      meta: "待生效",
    },
  ];

  const stagePhaseClass =
    currentMode === "exclusive_wait" ? `cws-phase--${exclusivePhase}` : "cws-phase--golden";
  const stageAriaLabel =
    currentMode === "exclusive_wait"
      ? "独享家庭组交付等待舞台"
      : currentMode === "tea"
      ? "瑕蝶花园茶歇等待舞台"
      : "瑕蝶信使派送等待舞台";
  const sceneTitle =
    currentMode === "exclusive_wait"
      ? exclusiveMeta.badgeTitle
      : currentMode === "tea"
      ? "花园茶歇陪伴"
      : "信使火速派送";
  const sceneSub =
    currentMode === "exclusive_wait"
      ? ` · ${exclusiveMeta.badgeSub}`
      : currentMode === "tea"
      ? " · 后台会自动帮你查进度"
      : " · 实时更新进度";

  return (
    <section
      className={`cws-stage cws-stage--${currentMode} ${stagePhaseClass}`}
      data-testid="castorice-waiting-stage"
      data-stage-mode={currentMode}
      data-exclusive-phase={currentMode === "exclusive_wait" ? exclusivePhase : undefined}
      aria-label={stageAriaLabel}
    >
      <div className="cws-header">
        <div className="cws-scene-badge" data-testid="cws-exclusive-phase">
          <span className="cws-scene-dot" aria-hidden="true" />
          <span>
            {sceneTitle}
            <span className="cws-scene-sub">{sceneSub}</span>
          </span>
        </div>

        <div className="cws-header-right">
          {companionCount > 0 ? (
            <span className="cws-companion-pill" data-testid="cws-companion-pill">
              <SparkleStarSvg className="cws-companion-star" />
              <span>{`陪伴值 +${companionCount}`}</span>
            </span>
          ) : null}

          {autoPollingActive ? (
            <span className="cws-countdown-pill" data-testid="cws-countdown-pill">
              <span>
                {refreshing ? (
                  "正在同步进度"
                ) : (
                  <>
                    <span className="cws-countdown-prefix">下次</span>自动同步
                  </>
                )}
              </span>
              {!refreshing ? (
                <span className="cws-countdown-time">{formatCountdownClock(secondsLeft)}</span>
              ) : null}
            </span>
          ) : courierMeta.statusPill ? (
            <span className="cws-countdown-pill" data-testid="cws-status-pill">
              <span>{courierMeta.statusPill}</span>
            </span>
          ) : null}
        </div>
      </div>

      {currentMode === "courier" && speechLine ? (
        <div
          className={`cws-speech-bubble is-courier ${courierBubbleSideClass}`}
          data-testid="cws-speech-bubble"
          role="status"
          aria-live="polite"
        >
          {speechLine}
        </div>
      ) : null}

      <div className="cws-burst-layer" aria-hidden="true">
        {burstItems.map((item) => {
          const particleStyle = {
            left: item.originX,
            top: item.originY,
            "--cws-dx": `${item.deltaX}px`,
            "--cws-dy": `${item.deltaY}px`,
            "--cws-rot": `${item.rotation}deg`,
          } as CSSProperties;
          return (
            <span key={item.id} className="cws-burst-particle" style={particleStyle}>
              {item.kind === "butterfly" ? (
                <img
                  src={MINI_BUTTERFLY_ASSET}
                  alt=""
                  className="cws-burst-butterfly"
                  draggable={false}
                />
              ) : (
                <SparkleStarSvg className="cws-burst-star" />
              )}
            </span>
          );
        })}
      </div>

      {currentMode === "exclusive_wait" ? (
        <div className="cws-ex-grid">
          <div className="cws-ex-hero">
            <div className="cws-constellation-stage">
              <div className="cws-ex-spotlight" aria-hidden="true" />
              <FamilySeatConstellation />
              <button
                type="button"
                className="cws-chibi-btn cws-chibi-btn--exclusive"
                data-testid="cws-poke-btn"
                onClick={handlePokeCastorice}
                aria-label="轻触小蝶互动解压"
              >
                <span className={`cws-chibi-spring ${bouncing ? "is-bouncing" : ""}`}>
                  <img
                    src={exclusiveMeta.stickerSrc}
                    alt={exclusiveMeta.stickerAlt}
                    className="cws-ex-sticker"
                    draggable={false}
                  />
                </span>
              </button>
            </div>

            {speechLine ? (
              <div
                className="cws-speech-bubble is-exclusive"
                data-testid="cws-speech-bubble"
                role="status"
                aria-live="polite"
              >
                {speechLine}
              </div>
            ) : (
              <span className="cws-ex-poke-caption">
                {companionCount === 0 ? "轻触小蝶互动 · 6 席家庭组等待中" : exclusiveMeta.slaTag}
              </span>
            )}

            <div className="cws-seat-legend" aria-label="6 席家庭组席位状态">
              <span className="cws-seat-legend-item">
                <span className="cws-legend-dot is-leader" aria-hidden="true" />
                <span>组长 1 席</span>
              </span>
              <span className="cws-seat-legend-item">
                <span className="cws-legend-dot is-member" aria-hidden="true" />
                <span>会员 1 席待入组</span>
              </span>
              <span className="cws-seat-legend-item">
                <span className="cws-legend-dot is-reserved" aria-hidden="true" />
                <span>家人预留 4 席</span>
              </span>
            </div>
          </div>

          <div className="cws-ex-right">
            <div className="cws-bento-metrics">
              <div className="cws-bento-card">
                <span className="cws-bento-label">已等待时长</span>
                <strong className="cws-bento-value tabular">
                  {formatElapsedMinutes(displayElapsedMinutes)}
                </strong>
                <span className="cws-bento-sub tabular">
                  {warrantySubmittedTime
                    ? `提交于 ${formatDateTime(warrantySubmittedTime)}`
                    : "已进入交付队列"}
                </span>
              </div>
              <div className="cws-bento-card">
                <span className="cws-bento-label">预计交付时效</span>
                <strong className="cws-bento-value cws-bento-value--accent">
                  {exclusiveMeta.etaHeadline}
                </strong>
                <span className="cws-bento-sub">
                  {exclusiveMeta.etaSub}
                </span>
              </div>
            </div>

            <div className="cws-sla-card" data-testid="cws-sla-progress">
              <div className="cws-sla-head">
                <span className="cws-sla-title">交付时效标尺</span>
                <span className="cws-sla-pill">{exclusiveMeta.slaTag}</span>
              </div>
              <div
                className="cws-sla-track"
                role="progressbar"
                aria-label="独享交付等待进度"
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={slaPercent}
              >
                <div className="cws-sla-golden-zone" aria-hidden="true" />
                <div className="cws-sla-fill" style={{ width: `${slaPercent}%` }}>
                  <span className="cws-sla-knob" aria-hidden="true" />
                </div>
              </div>
              <div className="cws-sla-ticks" aria-hidden="true">
                <span className="cws-sla-tick is-start">0h</span>
                <span className="cws-sla-tick is-golden-start">3h</span>
                <span className="cws-sla-tick is-golden-end">7h 常规窗</span>
                <span className="cws-sla-tick is-end">24h</span>
              </div>
            </div>

            <div className="cws-milestone-card" data-testid="cws-milestones">
              <ol className="cws-ms-list" aria-label="独享交付里程碑">
                {exclusiveMilestones.map((step) => (
                  <li key={step.title} className={`cws-ms-item is-${step.state}`}>
                    <span className="cws-ms-node" aria-hidden="true">
                      {step.state === "done" ? (
                        <MilestoneCheckSvg />
                      ) : step.state === "active" ? (
                        <span className="cws-ms-active-core" />
                      ) : (
                        <span className="cws-ms-pending-core" />
                      )}
                    </span>
                    <div className="cws-ms-body">
                      <div className="cws-ms-head">
                        <span className="cws-ms-title">{step.title}</span>
                        {step.meta ? <span className="cws-ms-meta tabular">{step.meta}</span> : null}
                      </div>
                      <p className="cws-ms-detail">{step.detail}</p>
                    </div>
                  </li>
                ))}
              </ol>
            </div>
          </div>
        </div>
      ) : currentMode === "tea" ? (
        <div className="cws-tea-grid">
          <div className="cws-avatar-zone">
            <div className="cws-avatar-halo" aria-hidden="true" />
            <div className="cws-orbit-ring" aria-hidden="true">
              <img
                src={MINI_BUTTERFLY_ASSET}
                alt=""
                className="cws-orbit-butterfly"
                draggable={false}
              />
            </div>
            <button
              type="button"
              className="cws-chibi-btn"
              data-testid="cws-poke-btn"
              onClick={handlePokeCastorice}
              aria-label="轻触小蝶互动解压"
            >
              <span className={`cws-chibi-spring ${bouncing ? "is-bouncing" : ""}`}>
                <img
                  src={TEA_WAIT_ASSET}
                  alt="Q 版瑕蝶茶歇等待陪伴"
                  className="cws-tea-img"
                  draggable={false}
                />
              </span>
              {companionCount === 0 ? <span className="cws-poke-hint">戳一戳小蝶</span> : null}
            </button>
          </div>

          <div className="cws-info-col">
            <div className="cws-pulse-meta">
              {speechLine ? (
                <div
                  className="cws-speech-bubble is-inline"
                  data-testid="cws-speech-bubble"
                  role="status"
                  aria-live="polite"
                >
                  {speechLine}
                </div>
              ) : (
                <>
                  <span>正在自动排队中</span>
                  <span>关掉页面也不会掉队</span>
                </>
              )}
            </div>
            <div
              className="cws-pulse-track"
              role="progressbar"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={cycleProgressPct}
            >
              <div className="cws-pulse-fill" style={{ width: `${cycleProgressPct}%` }} />
            </div>
            <div className="cws-tip-box">
              <p key={tipIndex} className="cws-tip-text" data-testid="cws-rotating-tip">
                {TEA_REASSURANCE_TIPS[tipIndex]}
              </p>
              <div className="cws-tip-dots" role="tablist" aria-label="排队定心提示切换">
                {TEA_REASSURANCE_TIPS.map((tipText, dotIdx) => (
                  <button
                    key={tipText}
                    type="button"
                    role="tab"
                    aria-selected={dotIdx === tipIndex}
                    aria-label={`查看第 ${dotIdx + 1} 条提示`}
                    className={`cws-tip-dot ${dotIdx === tipIndex ? "is-active" : ""}`}
                    onClick={() => setTipIndex(dotIdx)}
                  />
                ))}
              </div>
            </div>
          </div>
        </div>
      ) : (
        <div className="cws-courier-body">
          <div className="cws-courier-track-area">
            <div className="cws-courier-rail">
              <div
                className="cws-courier-rail-fill"
                style={{ width: `${courierMeta.progressPct}%` }}
              />
            </div>

            <div
              className="cws-courier-runner-anchor"
              style={{ left: `${courierMeta.progressPct}%` }}
            >
              <div className="cws-stardust-trail" aria-hidden="true">
                <span className="cws-stardust-dot" />
                <span className="cws-stardust-dot" />
                <span className="cws-stardust-dot" />
              </div>
              <button
                type="button"
                className="cws-chibi-btn is-courier"
                data-testid="cws-poke-btn"
                onClick={handlePokeCastorice}
                aria-label="轻触奔跑小蝶互动解压"
              >
                <span className={`cws-chibi-spring ${bouncing ? "is-bouncing" : ""}`}>
                  <img
                    src={courierMeta.spriteSrc}
                    alt={courierMeta.spriteAlt}
                    className={`cws-courier-img ${
                      courierMeta.isExclusiveCheckSprite ? "is-exclusive-check" : ""
                    }`}
                    draggable={false}
                  />
                </span>
              </button>
            </div>
          </div>

          <ol className="cws-node-list" aria-label="派送节点进度">
            {courierMeta.nodes.map((nodeLabel, nodeIdx) => {
              const isDone = nodeIdx < courierMeta.activeNode;
              const isActive = nodeIdx === courierMeta.activeNode;
              return (
                <li
                  key={nodeLabel}
                  className={`cws-node-item ${isDone ? "is-done" : ""} ${isActive ? "is-active" : ""}`}
                >
                  <span className="cws-node-pip" aria-hidden="true" />
                  <span>{nodeLabel}</span>
                </li>
              );
            })}
          </ol>

          <p className="cws-courier-caption">{courierMeta.caption}</p>
        </div>
      )}
    </section>
  );
}
