import { type CSSProperties, useEffect, useRef, useState } from "react";
import type { PublicCodeResponse } from "../types/api";
import "../styles/waiting-stage.css";

interface CastoriceWaitingStageProps {
  record: PublicCodeResponse;
  autoPollIntervalMs?: number;
  refreshing?: boolean;
}

type WaitingStageMode = "tea" | "courier" | "none";

interface BurstItem {
  id: number;
  symbol: string;
  originX: string;
  originY: string;
  deltaX: number;
  deltaY: number;
  rotation: number;
}

const TEA_WAIT_ASSET = "/assets/waiting/castorice-tea-wait.png";
const COURIER_RUN_ASSET = "/assets/waiting/castorice-courier-run.png";
const MINI_BUTTERFLY_ASSET = "/assets/waiting/mini-butterfly-glow.png";

const TEA_REASSURANCE_TIPS = [
  "淘宝显示“无需物流已发货”即已发提货码（非空包裹），当前正排队拉入家庭组",
  "一般 2~6 小时内发邀请（最晚 24 小时），关掉页面不影响排队，收藏本页随时看进度",
  "发出邀请后页面会自动变绿提醒，记得留意 Gmail 收件箱与垃圾箱哦",
] as const;

const POKE_HEALING_LINES = [
  "别急呀，小蝶正盯着队列呢~",
  "去喝口水休息一下，很快就好啦 ✨",
  "紫蝶信使正在花丛间加速穿梭中~",
  "不用一直守着屏幕哦，忙完回来看就可以啦 🌸",
  "收到你的戳戳啦，好运和邀请都在路上~",
] as const;

const BLOOM_VECTORS = [
  { dx: -92, dy: -54, rot: -24 },
  { dx: -48, dy: -74, rot: -12 },
  { dx: 0, dy: -82, rot: 4 },
  { dx: 52, dy: -72, rot: 16 },
  { dx: 96, dy: -50, rot: 28 },
  { dx: -72, dy: -22, rot: -18 },
] as const;

function resolveWaitingStageMode(record: PublicCodeResponse): WaitingStageMode {
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

  if (
    record.status === "validating" ||
    record.status === "purchasing" ||
    record.status === "waiting_fulfillment" ||
    record.status === "delivered_pending_warranty"
  ) {
    return "courier";
  }

  if (record.status === "completed" && record.warranty?.status === "submitted") {
    return "courier";
  }

  return "none";
}

function isTerminalCompletedRecord(record: PublicCodeResponse): boolean {
  if (record.status !== "completed") {
    return false;
  }
  if (record.product_type === "account" && record.warranty?.status === "submitted") {
    return false;
  }
  return true;
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
} {
  const isExclusive = record.product_type === "account";
  if (!isExclusive) {
    const activeNode: 0 | 1 = record.status === "validating" ? 0 : 1;
    return {
      nodes: ["读取兑换码", "锁定车位派单", "转入排队巡检"],
      activeNode,
      progressPct: activeNode === 0 ? 22 : 56,
      caption: "小蝶正在为您核验兑换码并锁定家庭组车位，马上切入自动队列~",
      statusPill: null,
    };
  }

  const nodes: [string, string, string] = [
    "读取兑换码",
    "分配独享账号/拉群",
    "等待接单/交保修",
  ];

  if (record.status === "validating") {
    return {
      nodes,
      activeNode: 0,
      progressPct: 18,
      caption: "正在核对兑换码有效性，准备为您调拨独享商店套餐账号…",
      statusPill: null,
    };
  }

  if (record.status === "purchasing" || record.status === "waiting_fulfillment") {
    return {
      nodes,
      activeNode: 1,
      progressPct: 52,
      caption: "小蝶正捧着紫蝶火漆信封火速取号派送中，请稍候片刻~",
      statusPill: null,
    };
  }

  if (record.status === "completed" && record.warranty?.status === "submitted") {
    return {
      nodes,
      activeNode: 2,
      progressPct: 86,
      caption: "已收到您的家庭组邀请截图，商家正在为您登记 30 天官方质保报备~",
      statusPill: "质保报备排队中",
    };
  }

  return {
    nodes,
    activeNode: 2,
    progressPct: 82,
    caption: "商店套餐邮箱已送达！按下方指引拉进家庭组后上传截图，即可完成质保登记 ✨",
    statusPill: "节点 3/3 · 待交保修",
  };
}

export function CastoriceWaitingStage({
  record,
  autoPollIntervalMs = 30_000,
  refreshing = false,
}: CastoriceWaitingStageProps) {
  const pollCycleSeconds = Math.max(5, Math.round(autoPollIntervalMs / 1000));
  const currentMode = resolveWaitingStageMode(record);
  const autoPollingActive =
    record.status === "waiting_fulfillment" ||
    record.status === "validating" ||
    record.status === "purchasing";

  const [secondsLeft, setSecondsLeft] = useState<number>(pollCycleSeconds);
  const [tipIndex, setTipIndex] = useState<number>(0);
  const [companionCount, setCompanionCount] = useState<number>(0);
  const [speechLine, setSpeechLine] = useState<string | null>(null);
  const [bouncing, setBouncing] = useState<boolean>(false);
  const [burstItems, setBurstItems] = useState<BurstItem[]>([]);
  const [isBlooming, setIsBlooming] = useState<boolean>(false);

  const prevCodeRef = useRef<string>(record.code);
  const prevModeRef = useRef<WaitingStageMode>(currentMode);
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
      lastSpeechIdxRef.current = -1;
      clearTransientTimers();
      setIsBlooming(false);
      setSpeechLine(null);
      setCompanionCount(0);
      setBurstItems([]);
      setBouncing(false);
      setTipIndex(0);
      setSecondsLeft(pollCycleSeconds);
      return;
    }

    const wasWaiting = prevModeRef.current === "tea" || prevModeRef.current === "courier";
    prevModeRef.current = currentMode;

    if (wasWaiting && currentMode === "none" && isTerminalCompletedRecord(record)) {
      setIsBlooming(true);
    }
  }, [currentMode, pollCycleSeconds, record]);

  useEffect(() => {
    if (!isBlooming) return;
    const bloomTimer = window.setTimeout(() => {
      setIsBlooming(false);
    }, 1350);
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

  const handlePokeCastorice = () => {
    setCompanionCount((prevCount) => prevCount + 1);

    let nextLineIdx = Math.floor(Math.random() * POKE_HEALING_LINES.length);
    if (POKE_HEALING_LINES.length > 1 && nextLineIdx === lastSpeechIdxRef.current) {
      nextLineIdx = (nextLineIdx + 1) % POKE_HEALING_LINES.length;
    }
    lastSpeechIdxRef.current = nextLineIdx;
    setSpeechLine(POKE_HEALING_LINES[nextLineIdx]);

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

    const symbols = ["🌸", "💗", "🌸", "✨", "🌸"];
    const spawnCount = 4 + (burstSeqRef.current % 2);
    const originX = currentMode === "tea" ? "76px" : `${courierMeta.progressPct}%`;
    const originY = currentMode === "tea" ? "74px" : "68px";
    const createdBatch: BurstItem[] = [];

    for (let itemIndex = 0; itemIndex < spawnCount; itemIndex += 1) {
      const nextId = burstSeqRef.current;
      burstSeqRef.current += 1;
      const spreadAngle = -Math.PI * (0.22 + (itemIndex / Math.max(1, spawnCount - 1)) * 0.56);
      const travel = 36 + Math.random() * 26;
      createdBatch.push({
        id: nextId,
        symbol: symbols[itemIndex % symbols.length],
        originX,
        originY,
        deltaX: Math.round(Math.cos(spreadAngle) * travel),
        deltaY: Math.round(Math.sin(spreadAngle) * travel - 12),
        rotation: Math.round((Math.random() - 0.5) * 44),
      });
    }

    const createdIds = new Set(createdBatch.map((entry) => entry.id));
    setBurstItems((prevItems) => [...prevItems.slice(-10), ...createdBatch]);
    const burstCleanupTimer = window.setTimeout(() => {
      burstTimersRef.current.delete(burstCleanupTimer);
      setBurstItems((prevItems) => prevItems.filter((entry) => !createdIds.has(entry.id)));
    }, 960);
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
          <div className="cws-bloom-title">
            {isExclusiveDone ? "🌸 质保报备已完成！" : "🌸 家庭组邀请已发出！"}
          </div>
          <div className="cws-bloom-sub">紫蝶信使已送达，正在为您展开下一步指引…</div>
        </div>
      </section>
    );
  }

  const cycleProgressPct = Math.min(
    100,
    Math.max(12, Math.round(((pollCycleSeconds - secondsLeft) / pollCycleSeconds) * 88 + 12))
  );
  const courierBubbleSideClass =
    courierMeta.progressPct >= 48
      ? "is-side-left"
      : "is-side-right";

  return (
    <section
      className={`cws-stage cws-stage--${currentMode}`}
      data-testid="castorice-waiting-stage"
      data-stage-mode={currentMode}
      aria-label={currentMode === "tea" ? "瑕蝶花园茶歇等待舞台" : "瑕蝶信使派送等待舞台"}
    >
      <div className="cws-header">
        <div className="cws-scene-badge">
          <span className="cws-scene-dot" aria-hidden="true" />
          <span>
            {currentMode === "tea" ? "花园茶歇陪伴" : "信使火速派送"}
            <span className="cws-scene-sub">
              {currentMode === "tea" ? " · 后台每 20 分钟自动巡检" : " · 实时节点追踪"}
            </span>
          </span>
        </div>

        <div className="cws-header-right">
          {companionCount > 0 ? (
            <span className="cws-companion-pill" data-testid="cws-companion-pill">
              {`🌸 陪伴值 +${companionCount}`}
            </span>
          ) : null}

          {autoPollingActive ? (
            <span className="cws-countdown-pill" data-testid="cws-countdown-pill">
              <span>
                {refreshing ? (
                  "正在同步进度"
                ) : (
                  <>
                    <span className="cws-countdown-prefix">下次</span>自动刷新
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
              {item.symbol}
            </span>
          );
        })}
      </div>

      {currentMode === "tea" ? (
        <div className="cws-tea-grid">
          <div className="cws-avatar-zone">
            <div className="cws-avatar-halo" aria-hidden="true" />
            <div className="cws-orbit-ring" aria-hidden="true">
              <img
                src={MINI_BUTTERFLY_ASSET}
                alt=""
                className="cws-orbit-butterfly"
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
                  <span>自动排队巡检脉冲</span>
                  <span>已为您守护队列</span>
                </>
              )}
            </div>
            <div className="cws-pulse-track" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={cycleProgressPct}>
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
                    src={COURIER_RUN_ASSET}
                    alt="Q 版瑕蝶捧信火速派送"
                    className="cws-courier-img"
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
