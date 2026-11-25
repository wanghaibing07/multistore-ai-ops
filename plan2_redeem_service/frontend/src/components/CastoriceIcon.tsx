import type { CSSProperties } from "react";

export type CastoriceIconName =
  | "emblem"
  | "voucher"
  | "paste"
  | "arrow-right"
  | "arrow-left"
  | "external"
  | "download"
  | "check"
  | "mail"
  | "alert"
  | "refresh"
  | "retry"
  | "close"
  | "chibi-peek"
  | "chibi-mail"
  | "admin-brand"
  | "admin-overview"
  | "admin-craft"
  | "admin-ledger"
  | "admin-settings"
  | "admin-bye"
  | "admin-alert"
  | "admin-wait"
  | "admin-wallet"
  | "admin-happy";

const ICON_SRC_MAP: Record<CastoriceIconName, string> = {
  emblem: "/assets/stickers/chibi-emblem.png",
  voucher: "/assets/stickers/icon-voucher.png",
  paste: "/assets/stickers/icon-paste.png",
  "arrow-right": "/assets/stickers/icon-wing-arrow.png",
  "arrow-left": "/assets/stickers/icon-wing-arrow.png",
  external: "/assets/stickers/icon-wing-arrow.png",
  download: "/assets/stickers/icon-wing-arrow.png",
  check: "/assets/stickers/icon-check.png",
  mail: "/assets/stickers/icon-mail.png",
  alert: "/assets/stickers/icon-alert.png",
  refresh: "/assets/stickers/icon-refresh.png",
  retry: "/assets/stickers/icon-refresh.png",
  close: "/assets/stickers/icon-close.png",
  "chibi-peek": "/assets/stickers/chibi-peek.png",
  "chibi-mail": "/assets/stickers/chibi-mail.png",
  "admin-brand": "/assets/stickers/admin-brand.png",
  "admin-overview": "/assets/stickers/admin-overview.png",
  "admin-craft": "/assets/stickers/admin-craft.png",
  "admin-ledger": "/assets/stickers/admin-ledger.png",
  "admin-settings": "/assets/stickers/admin-settings.png",
  "admin-bye": "/assets/stickers/admin-bye.png",
  "admin-alert": "/assets/stickers/admin-alert.png",
  "admin-wait": "/assets/stickers/admin-wait.png",
  "admin-wallet": "/assets/stickers/admin-wallet.png",
  "admin-happy": "/assets/stickers/admin-happy.png",
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
    name === "arrow-left" || name === "retry" ? "castorice-icon--flip-x" : "",
    name === "external" ? "castorice-icon--external" : "",
    name === "download" ? "castorice-icon--down" : "",
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
