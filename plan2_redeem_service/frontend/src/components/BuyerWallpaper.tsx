import { useEffect, useRef, useState } from "react";
import { BuyerAmbientFX } from "./BuyerAmbientFX";

export const WALLPAPER_VIDEO_URL = "/media/%E7%91%95%E8%9D%B6.mp4";
export const WALLPAPER_POSTER_URL = "/media/preview.jpg";
const WALLPAPER_PREF_KEY = "plan2_dynamic_wallpaper_v1";
const WALLPAPER_PREF_EVENT = "plan2:wallpaper-pref-change";

type WallpaperPref = "on" | "off" | null;

interface NetworkInfoLike {
  saveData?: boolean;
  effectiveType?: string;
  addEventListener?: (type: string, listener: () => void) => void;
  removeEventListener?: (type: string, listener: () => void) => void;
}

function readStoredPref(): WallpaperPref {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(WALLPAPER_PREF_KEY);
    if (raw === "on" || raw === "off") return raw;
  } catch {
    return null;
  }
  return null;
}

function writeStoredPref(pref: "on" | "off"): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(WALLPAPER_PREF_KEY, pref);
  } catch {
    // ignore storage failures in private browsing
  }
  window.dispatchEvent(new CustomEvent(WALLPAPER_PREF_EVENT, { detail: pref }));
}

function detectConstrainedEnvironment(): {
  reducedMotion: boolean;
  compactOrTouch: boolean;
  constrainedNetwork: boolean;
} {
  if (typeof window === "undefined") {
    return { reducedMotion: false, compactOrTouch: false, constrainedNetwork: false };
  }
  const match = (query: string) =>
    typeof window.matchMedia === "function" ? window.matchMedia(query).matches : false;
  const reducedMotion = match("(prefers-reduced-motion: reduce)");
  const compactOrTouch = match("(max-width: 767px), (pointer: coarse)");
  const conn = (navigator as Navigator & { connection?: NetworkInfoLike }).connection;
  const effective = String(conn?.effectiveType || "").toLowerCase();
  const constrainedNetwork = Boolean(
    conn?.saveData || effective === "slow-2g" || effective === "2g" || effective === "3g"
  );
  return { reducedMotion, compactOrTouch, constrainedNetwork };
}

function useWallpaperMotionState() {
  const [env, setEnv] = useState(() => detectConstrainedEnvironment());
  const [userPref, setUserPref] = useState<WallpaperPref>(() => readStoredPref());

  useEffect(() => {
    if (typeof window === "undefined") return;
    const syncEnv = () => setEnv(detectConstrainedEnvironment());
    syncEnv();

    const motionMq =
      typeof window.matchMedia === "function"
        ? window.matchMedia("(prefers-reduced-motion: reduce)")
        : null;
    const compactMq =
      typeof window.matchMedia === "function"
        ? window.matchMedia("(max-width: 767px), (pointer: coarse)")
        : null;
    const conn = (navigator as Navigator & { connection?: NetworkInfoLike }).connection;

    const bindMq = (mq: MediaQueryList | null) => {
      if (!mq) return () => {};
      if (typeof mq.addEventListener === "function") {
        mq.addEventListener("change", syncEnv);
        return () => mq.removeEventListener("change", syncEnv);
      }
      mq.addListener(syncEnv);
      return () => mq.removeListener(syncEnv);
    };

    const unbindMotion = bindMq(motionMq);
    const unbindCompact = bindMq(compactMq);
    conn?.addEventListener?.("change", syncEnv);

    const onPrefEvent = (event: Event) => {
      const detail = (event as CustomEvent<WallpaperPref>).detail;
      setUserPref(detail === "on" || detail === "off" ? detail : readStoredPref());
    };
    const onStorage = (event: StorageEvent) => {
      if (event.key === WALLPAPER_PREF_KEY) {
        setUserPref(readStoredPref());
      }
    };

    window.addEventListener(WALLPAPER_PREF_EVENT, onPrefEvent);
    window.addEventListener("storage", onStorage);

    return () => {
      unbindMotion();
      unbindCompact();
      conn?.removeEventListener?.("change", syncEnv);
      window.removeEventListener(WALLPAPER_PREF_EVENT, onPrefEvent);
      window.removeEventListener("storage", onStorage);
    };
  }, []);

  const autoAllowed = !env.reducedMotion && !env.compactOrTouch && !env.constrainedNetwork;
  const dynamicEnabled = userPref === "on" ? true : userPref === "off" ? false : autoAllowed;

  const toggleDynamic = () => {
    writeStoredPref(dynamicEnabled ? "off" : "on");
  };

  return { dynamicEnabled, toggleDynamic, reducedMotion: env.reducedMotion };
}

interface WallpaperMotionToggleProps {
  surface?: "light" | "dark";
}

export function WallpaperMotionToggle({ surface = "dark" }: WallpaperMotionToggleProps) {
  const { dynamicEnabled, toggleDynamic } = useWallpaperMotionState();

  return (
    <button
      type="button"
      className={`wallpaper-toggle-btn wallpaper-toggle-btn--${surface} ${
        dynamicEnabled ? "is-dynamic" : "is-static"
      }`}
      aria-pressed={dynamicEnabled}
      title={dynamicEnabled ? "切换为省流静图背景" : "开启动态视频背景"}
      onClick={toggleDynamic}
    >
      <span className="wallpaper-toggle-dot" aria-hidden="true" />
      <span>{dynamicEnabled ? "动态壁纸：开" : "省流静图"}</span>
    </button>
  );
}

interface BuyerWallpaperProps {
  variant?: "hero" | "detail";
}

export function BuyerWallpaper({ variant = "hero" }: BuyerWallpaperProps) {
  const [videoFailed, setVideoFailed] = useState(false);
  const [videoReady, setVideoReady] = useState(false);
  const [webglActive, setWebglActive] = useState(false);
  const { dynamicEnabled, reducedMotion } = useWallpaperMotionState();
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const posterRef = useRef<HTMLImageElement | null>(null);
  const particleCanvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    if (!dynamicEnabled || videoFailed) {
      setVideoReady(false);
      setWebglActive(false);
      return;
    }
    let cancelled = false;
    const activate = () => {
      if (!cancelled) setVideoReady(true);
    };
    const idleWindow = window as Window & {
      requestIdleCallback?: (cb: () => void, opts?: { timeout: number }) => number;
      cancelIdleCallback?: (id: number) => void;
    };
    if (typeof idleWindow.requestIdleCallback === "function") {
      const idleId = idleWindow.requestIdleCallback(activate, { timeout: 350 });
      return () => {
        cancelled = true;
        idleWindow.cancelIdleCallback?.(idleId);
      };
    }
    const timer = window.setTimeout(activate, 180);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [dynamicEnabled, videoFailed]);

  useEffect(() => {
    const videoEl = videoRef.current;
    if (!videoEl || !dynamicEnabled || !videoReady || videoFailed) return;
    videoEl.defaultMuted = true;
    videoEl.muted = true;
    const playPromise = videoEl.play();
    if (playPromise && typeof playPromise.catch === "function") {
      playPromise.catch(() => {});
    }
  }, [dynamicEnabled, videoReady, videoFailed]);

  const showVideo = dynamicEnabled && videoReady && !videoFailed;
  const enableAmbient = dynamicEnabled && !reducedMotion;
  const enableWebGL = dynamicEnabled && !reducedMotion;

  return (
    <div
      className={`buyer-wallpaper buyer-wallpaper--${variant}`}
      data-webgl-active={enableWebGL && webglActive ? "true" : "false"}
      data-wallpaper-mode={dynamicEnabled ? "dynamic" : "static"}
      aria-hidden="true"
    >
      <div className="buyer-wallpaper-media-stage">
        {showVideo ? (
          <video
            ref={videoRef}
            className={`buyer-wallpaper-video ${enableWebGL && webglActive ? "is-webgl-source" : ""}`}
            src={WALLPAPER_VIDEO_URL}
            poster={WALLPAPER_POSTER_URL}
            crossOrigin="anonymous"
            autoPlay
            muted
            loop
            playsInline
            preload="metadata"
            disablePictureInPicture
            onError={() => setVideoFailed(true)}
          />
        ) : (
          <img
            ref={posterRef}
            src={WALLPAPER_POSTER_URL}
            alt=""
            decoding="async"
            className={`buyer-wallpaper-poster ${enableWebGL && webglActive ? "is-webgl-source" : ""}`}
          />
        )}

        {enableAmbient ? (
          <BuyerAmbientFX
            videoRef={videoRef}
            posterRef={posterRef}
            particleCanvasRef={particleCanvasRef}
            posterUrl={WALLPAPER_POSTER_URL}
            videoFailed={videoFailed}
            reducedMotion={reducedMotion}
            enableWebGL={enableWebGL}
            variant={variant}
            onWebGLStateChange={setWebglActive}
          />
        ) : null}
      </div>

      <div className="buyer-wallpaper-glow" />
      <div className="buyer-wallpaper-scrim" />
      {enableAmbient ? (
        <canvas
          ref={particleCanvasRef}
          className="buyer-wallpaper-particles"
          style={{ zIndex: 5 }}
          aria-hidden="true"
        />
      ) : null}
    </div>
  );
}
