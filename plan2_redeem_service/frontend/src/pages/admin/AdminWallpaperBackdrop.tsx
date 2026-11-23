import { useEffect, useRef, useState } from "react";

const WALLPAPER_VIDEO_URL = "/media/%E7%91%95%E8%9D%B6.mp4";
const WALLPAPER_POSTER_URL = "/media/preview.jpg";

interface AdminWallpaperBackdropProps {
  variant?: "login" | "workspace";
}

function readReducedMotion(): boolean {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
    return false;
  }
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

export function AdminWallpaperBackdrop({ variant = "workspace" }: AdminWallpaperBackdropProps) {
  const [reducedMotion, setReducedMotion] = useState<boolean>(readReducedMotion);
  const [videoFailed, setVideoFailed] = useState(false);
  const videoRef = useRef<HTMLVideoElement | null>(null);

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
      return;
    }
    const mediaQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
    const handleChange = () => {
      setReducedMotion(mediaQuery.matches);
    };
    handleChange();
    if (typeof mediaQuery.addEventListener === "function") {
      mediaQuery.addEventListener("change", handleChange);
      return () => mediaQuery.removeEventListener("change", handleChange);
    }
    mediaQuery.addListener(handleChange);
    return () => mediaQuery.removeListener(handleChange);
  }, []);

  useEffect(() => {
    if (reducedMotion || videoFailed) return;
    const videoEl = videoRef.current;
    if (!videoEl) return;

    videoEl.muted = true;
    videoEl.defaultMuted = true;
    const playPromise = videoEl.play();
    if (playPromise && typeof playPromise.catch === "function") {
      playPromise.catch(() => {
        // Keep poster fallback gracefully visible if autoplay is restricted
      });
    }
  }, [reducedMotion, videoFailed]);

  const shouldRenderVideo = !reducedMotion && !videoFailed;

  return (
    <div
      className={`admin-wallpaper admin-wallpaper--${variant}`}
      aria-hidden="true"
    >
      <div className="admin-wallpaper-poster" />
      {shouldRenderVideo && (
        <video
          ref={videoRef}
          className="admin-wallpaper-video"
          src={WALLPAPER_VIDEO_URL}
          poster={WALLPAPER_POSTER_URL}
          autoPlay
          muted
          loop
          playsInline
          preload="auto"
          tabIndex={-1}
          disablePictureInPicture
          disableRemotePlayback
          onError={() => setVideoFailed(true)}
        />
      )}
      <div className="admin-wallpaper-scrim" />
      <div className="admin-wallpaper-aura" />
    </div>
  );
}
