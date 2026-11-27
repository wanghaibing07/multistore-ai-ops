import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { getPublicCode } from "../api/client";
import { triggerButterflyBurst } from "../components/ButterflyBurstFX";
import { BuyerWallpaper, WallpaperMotionToggle } from "../components/BuyerWallpaper";
import { CastoriceIcon } from "../components/CastoriceIcon";
import { ChibiSticker } from "../components/ChibiSticker";
import { useToast } from "../components/ToastProvider";
import { isImeComposing } from "../utils/ime";
import { extractRedeemCode } from "../utils/validation";
import { prefersReducedMotion, runRedeemViewTransition } from "../utils/viewTransition";

export function HomePage() {
  const [rawInput, setRawInput] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [inputFocused, setInputFocused] = useState(false);
  const [typingWiggle, setTypingWiggle] = useState(false);
  const [pasteDone, setPasteDone] = useState(false);
  const [bursting, setBursting] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const inputRef = useRef<HTMLInputElement | null>(null);
  const submitBtnRef = useRef<HTMLButtonElement | null>(null);
  const wiggleTimerRef = useRef<number | null>(null);
  const pasteTimerRef = useRef<number | null>(null);
  const submitTimerRef = useRef<number | null>(null);
  const mountedRef = useRef(true);
  const navigate = useNavigate();
  const { pushToast } = useToast();

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (wiggleTimerRef.current !== null) window.clearTimeout(wiggleTimerRef.current);
      if (pasteTimerRef.current !== null) window.clearTimeout(pasteTimerRef.current);
      if (submitTimerRef.current !== null) window.clearTimeout(submitTimerRef.current);
    };
  }, []);

  const pulseTypingWiggle = () => {
    setTypingWiggle(true);
    if (wiggleTimerRef.current !== null) {
      window.clearTimeout(wiggleTimerRef.current);
    }
    wiggleTimerRef.current = window.setTimeout(() => {
      setTypingWiggle(false);
    }, 480);
  };

  const handleQuickPaste = async () => {
    if (!navigator.clipboard || typeof navigator.clipboard.readText !== "function") {
      inputRef.current?.focus();
      pushToast("当前环境不支持自动读取剪贴板，请长按输入框粘贴", "info");
      return;
    }
    try {
      const text = await navigator.clipboard.readText();
      const trimmed = text.trim();
      if (!trimmed) {
        inputRef.current?.focus();
        pushToast("剪贴板里还没有内容哦", "info");
        return;
      }
      setRawInput(trimmed);
      if (error) setError(null);
      pulseTypingWiggle();
      setPasteDone(true);
      if (pasteTimerRef.current !== null) {
        window.clearTimeout(pasteTimerRef.current);
      }
      pasteTimerRef.current = window.setTimeout(() => {
        setPasteDone(false);
      }, 1800);
      inputRef.current?.focus();
    } catch {
      inputRef.current?.focus();
      pushToast("浏览器未授权读取剪贴板，请直接在输入框粘贴", "info");
    }
  };

  const submitLookup = async () => {
    if (submitting) return;
    const extracted = extractRedeemCode(rawInput);
    if (!extracted || extracted.length < 4) {
      setError("请输入有效的兑换码，或者直接粘贴客服发给你的提货链接");
      inputRef.current?.focus();
      return;
    }
    setError(null);
    const targetUrl = `/r/${encodeURIComponent(extracted)}`;

    if (prefersReducedMotion()) {
      setSubmitting(true);
      const prefetchedRecord = await Promise.race([
        getPublicCode(extracted).catch(() => null),
        new Promise<null>((resolve) => window.setTimeout(() => resolve(null), 160)),
      ]);
      if (!mountedRef.current) return;
      runRedeemViewTransition(() => {
        navigate(targetUrl, {
          state: prefetchedRecord ? { prefetchedRecord } : undefined,
        });
      }, "forward");
      return;
    }

    setSubmitting(true);
    setBursting(true);
    triggerButterflyBurst(submitBtnRef.current);

    const burstLeadDelay = new Promise<void>((resolve) => {
      submitTimerRef.current = window.setTimeout(resolve, 220);
    });
    const prefetchPromise = Promise.race([
      getPublicCode(extracted).catch(() => null),
      new Promise<null>((resolve) => window.setTimeout(() => resolve(null), 220)),
    ]);

    const [, prefetchedRecord] = await Promise.all([burstLeadDelay, prefetchPromise]);
    if (!mountedRef.current) return;

    runRedeemViewTransition(() => {
      navigate(targetUrl, {
        state: prefetchedRecord ? { prefetchedRecord } : undefined,
      });
    }, "forward");
  };

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault();
    void submitLookup();
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key !== "Enter") return;
    if (isImeComposing(event)) {
      event.preventDefault();
    }
  };

  return (
    <div className="zzz-home">
      <BuyerWallpaper variant="hero" />

      <div className="zzz-shell">
        <header className="zzz-topbar">
          <Link to="/" className="zzz-brand">
            <img
              src="/assets/stickers/chibi-emblem.png"
              alt=""
              aria-hidden="true"
              className="zzz-brand-emblem"
            />
            <span className="zzz-brand-copy">
              <span className="zzz-brand-title">Google AI Ultra 20x</span>
              <span className="zzz-brand-sub">自助提货</span>
            </span>
          </Link>
          <WallpaperMotionToggle surface="light" />
        </header>

        <main className="zzz-stage">
          <div className="zzz-hero-stack">
            <section
              className="zzz-glass-card redeem-card-morph"
              aria-labelledby="home-hero-heading"
            >
              <ChibiSticker
                variant="peek"
                bubbleText="直接粘贴客服消息就能自动识别"
                wiggle={inputFocused || typingWiggle}
              />

              <div className="zzz-heading-block">
                <h1 id="home-hero-heading" className="zzz-hero-title">
                  Google AI Ultra 20x
                </h1>
                <p className="zzz-hero-subhead">输入兑换码，提货或查进度</p>
                <p className="zzz-hero-lead">
                  粘贴兑换码或提货链接，自动识别。
                </p>
              </div>

              <form onSubmit={handleSubmit} className="zzz-form" noValidate>
                <label htmlFor="redeem-code-input" className="zzz-field-label">
                  <CastoriceIcon name="voucher" size={17} />
                  <span>兑换码或提货链接</span>
                </label>

                <div className="zzz-input-row">
                  <div className="zzz-input-box">
                    <input
                      ref={inputRef}
                      id="redeem-code-input"
                      type="text"
                      enterKeyHint="go"
                      autoComplete="off"
                      spellCheck={false}
                      disabled={submitting}
                      aria-invalid={Boolean(error) || undefined}
                      aria-describedby={error ? "redeem-code-error" : undefined}
                      className={`zzz-input mono ${error ? "is-error" : ""}`}
                      value={rawInput}
                      onFocus={() => setInputFocused(true)}
                      onBlur={() => setInputFocused(false)}
                      onKeyDown={handleKeyDown}
                      onChange={(event) => {
                        setRawInput(event.target.value);
                        if (error) setError(null);
                        pulseTypingWiggle();
                      }}
                    />
                    <button
                      type="button"
                      disabled={submitting}
                      className={`zzz-paste-btn ${pasteDone ? "is-pasted" : ""}`}
                      onClick={() => void handleQuickPaste()}
                      aria-label="一键粘贴剪贴板中的兑换码或链接"
                    >
                      <CastoriceIcon name={pasteDone ? "check" : "paste"} size={15} />
                      <span>{pasteDone ? "✓ 已粘贴" : "粘贴"}</span>
                    </button>
                  </div>

                  <button
                    ref={submitBtnRef}
                    type="submit"
                    disabled={submitting}
                    className={`zzz-submit-btn ${bursting ? "is-bursting" : ""}`}
                  >
                    <span>查进度 / 去提货</span>
                    <CastoriceIcon name="arrow-right" size={18} />
                  </button>
                </div>

                {error ? (
                  <div id="redeem-code-error" className="zzz-field-error" role="alert">
                    {error}
                  </div>
                ) : null}
              </form>

              <p className="zzz-card-note">提货后收藏链接，随时查进度</p>
            </section>
          </div>
        </main>
      </div>
    </div>
  );
}
