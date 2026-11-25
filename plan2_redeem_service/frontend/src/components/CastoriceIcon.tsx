import type { CSSProperties } from "react";

export type CastoriceIconName =
  | "emblem"
  | "voucher"
  | "paste"
  | "arrow-right"
  | "arrow-left"
  | "external"
  | "check"
  | "mail"
  | "alert"
  | "refresh";

const ICON_SRC_MAP: Record<CastoriceIconName, string> = {
  emblem: "/assets/stickers/chibi-emblem.png",
  voucher: "/assets/stickers/icon-voucher.png",
  paste: "/assets/stickers/icon-paste.png",
  "arrow-right": "/assets/stickers/icon-wing-arrow.png",
  "arrow-left": "/assets/stickers/icon-wing-arrow.png",
  external: "/assets/stickers/icon-wing-arrow.png",
  check: "/assets/stickers/icon-check.png",
  mail: "/assets/stickers/icon-mail.png",
  alert: "/assets/stickers/icon-alert.png",
  refresh: "/assets/stickers/icon-refresh.png",
};

interface CastoriceIconProps {
  name: CastoriceIconName;
  size?: number;
  spin?: boolean;
  className?: string;
  style?: CSSProperties;
}

export function CastoriceIcon({
  name,
  size = 18,
  spin = false,
  className = "",
  style,
}: CastoriceIconProps) {
  const classes = [
    "castorice-icon",
    name === "arrow-left" ? "castorice-icon--flip-x" : "",
    name === "external" ? "castorice-icon--external" : "",
    spin ? "spin" : "",
    className,
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <img
      src={ICON_SRC_MAP[name]}
      alt=""
      aria-hidden="true"
      width={size}
      height={size}
      decoding="async"
      draggable={false}
      className={classes}
      style={{ width: size, height: size, ...style }}
    />
  );
}
