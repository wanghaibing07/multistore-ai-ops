import { useEffect, useId, useRef, useState, type PointerEvent } from "react";
import { triggerStickerStardust } from "./ButterflyBurstFX";

interface ChibiStickerProps {
  variant: "peek" | "mail";
  bubbleText: string;
  wiggle?: boolean;
  springEntrance?: boolean;
}

const STICKER_ASSETS = {
  peek: {
    src: "/assets/stickers/chibi-peek.png",
    label: "查看小瑕蝶贴士",
  },
  mail: {
    src: "/assets/stickers/chibi-mail.png",
    label: "查看收信小提示",
  },
} as const;

export function ChibiSticker({
  variant,
  bubbleText,
  wiggle = false,
  springEntrance = false,
}: ChibiStickerProps) {
  const [bubbleOpen, setBubbleOpen] = useState(false);
  const [pinnedByClick, setPinnedByClick] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  const autoHideTimerRef = useRef<number | null>(null);
  const bubbleId = useId();

  useEffect(() => {
    return () => {
      if (autoHideTimerRef.current !== null) {
        window.clearTimeout(autoHideTimerRef.current);
      }
    };
  }, []);

  useEffect(() => {
    if (!bubbleOpen) return;
    const handleOutside = (event: MouseEvent | TouchEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) {
        setBubbleOpen(false);
        setPinnedByClick(false);
      }
    };
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setBubbleOpen(false);
        setPinnedByClick(false);
      }
    };
    document.addEventListener("mousedown", handleOutside);
    document.addEventListener("touchstart", handleOutside, { passive: true });
    document.addEventListener("keydown", handleKey);
    return () => {
      document.removeEventListener("mousedown", handleOutside);
      document.removeEventListener("touchstart", handleOutside);
      document.removeEventListener("keydown", handleKey);
    };
  }, [bubbleOpen]);

  const clearAutoHide = () => {
    if (autoHideTimerRef.current !== null) {
      window.clearTimeout(autoHideTimerRef.current);
      autoHideTimerRef.current = null;
    }
  };

  const handlePointerEnter = (event: PointerEvent<HTMLDivElement>) => {
    if (event.pointerType === "touch") return;
    clearAutoHide();
    setBubbleOpen(true);
    triggerStickerStardust(buttonRef.current);
  };

  const handlePointerLeave = (event: PointerEvent<HTMLDivElement>) => {
    if (event.pointerType === "touch" || pinnedByClick) return;
    clearAutoHide();
    autoHideTimerRef.current = window.setTimeout(() => {
      setBubbleOpen(false);
    }, 180);
  };

  const handleToggle = () => {
    clearAutoHide();
    triggerStickerStardust(buttonRef.current);
    setBubbleOpen((prev) => {
      const next = !prev;
      setPinnedByClick(next);
      if (next) {
        autoHideTimerRef.current = window.setTimeout(() => {
          setBubbleOpen(false);
          setPinnedByClick(false);
        }, 4200);
      }
      return next;
    });
  };

  const meta = STICKER_ASSETS[variant];
  const rootClasses = [
    "chibi-sticker",
    `chibi-sticker--${variant}`,
    wiggle ? "is-wiggling" : "",
    springEntrance ? "is-spring-in" : "",
    bubbleOpen ? "is-open" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div
      ref={rootRef}
      className={rootClasses}
      onPointerEnter={handlePointerEnter}
      onPointerLeave={handlePointerLeave}
    >
      <div
        id={bubbleId}
        role="status"
        aria-live="polite"
        className={`chibi-speech-bubble ${bubbleOpen ? "is-visible" : ""}`}
      >
        <span>{bubbleText}</span>
      </div>

      <button
        ref={buttonRef}
        type="button"
        className="chibi-sticker-btn"
        aria-label={meta.label}
        aria-expanded={bubbleOpen}
        aria-controls={bubbleId}
        onClick={handleToggle}
      >
        <img
          src={meta.src}
          alt=""
          aria-hidden="true"
          decoding="async"
          draggable={false}
          className="chibi-sticker-img"
        />
      </button>
    </div>
  );
}
