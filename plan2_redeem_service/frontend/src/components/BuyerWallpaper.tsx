import { useEffect, useRef, useState } from "react";

export const WALLPAPER_VIDEO_URL = "/media/%E7%91%95%E8%9D%B6.mp4";
export const WALLPAPER_POSTER_URL = "/media/preview.jpg";

interface BuyerWallpaperProps {
  variant?: "hero" | "detail";
}

export function BuyerWallpaper({ variant = "hero" }: BuyerWallpaperProps) {
  const [videoFailed, setVideoFailed] = useState(false);
  const [reducedMotion, setReducedMotion] = useState(false);
  const videoRef = useRef<HTMLVideoElement | null>(null);

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
      return;
    }
    const mediaQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
    const syncMotion = () => setReducedMotion(mediaQuery.matches);
    syncMotion();
    if (typeof mediaQuery.addEventListener === "function") {
      mediaQuery.addEventListener("change", syncMotion);
      return () => mediaQuery.removeEventListener("change", syncMotion);
    }
    mediaQuery.addListener(syncMotion);
    return () => mediaQuery.removeListener(syncMotion);
  }, []);

  useEffect(() => {
    const videoEl = videoRef.current;
    if (!videoEl || reducedMotion || videoFailed) return;
    videoEl.defaultMuted = true;
    videoEl.muted = true;
    const playPromise = videoEl.play();
    if (playPromise && typeof playPromise.catch === "function") {
      playPromise.catch(() => {});
    }
  }, [reducedMotion, videoFailed]);

  return (
    <div className={`buyer-wallpaper buyer-wallpaper--${variant}`} aria-hidden="true">
      {!videoFailed && !reducedMotion ? (
        <video
          ref={videoRef}
          className="buyer-wallpaper-video"
          src={WALLPAPER_VIDEO_URL}
          poster={WALLPAPER_POSTER_URL}
          autoPlay
          muted
          loop
          playsInline
          preload="auto"
          disablePictureInPicture
          onError={() => setVideoFailed(true)}
        />
      ) : (
        <img
          src={WALLPAPER_POSTER_URL}
          alt=""
          className="buyer-wallpaper-poster"
        />
      )}
      <div className="buyer-wallpaper-scrim" />
    </div>
  );
}
