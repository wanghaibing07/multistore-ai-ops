import { useEffect, useRef, useState } from "react";
import { copyToClipboard } from "../utils/format";
import { CastoriceIcon } from "./CastoriceIcon";
import { useToast } from "./ToastProvider";

interface CopyButtonProps {
  text: string;
  label?: string;
  copiedLabel?: string;
  toastMessage?: string;
  variant?: "primary" | "secondary" | "ghost";
  size?: "sm" | "md";
  block?: boolean;
}

export function CopyButton({
  text,
  label = "复制",
  copiedLabel = "✓ 已复制",
  toastMessage = "已复制到剪贴板",
  variant = "secondary",
  size = "sm",
  block = false,
}: CopyButtonProps) {
  const [state, setState] = useState<"idle" | "success" | "error">("idle");
  const timerRef = useRef<number | null>(null);
  const { pushToast } = useToast();

  useEffect(() => {
    return () => {
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current);
      }
    };
  }, []);

  const handleCopy = async () => {
    const ok = await copyToClipboard(text);
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
    }
    if (ok) {
      setState("success");
      pushToast(toastMessage, "success");
      timerRef.current = window.setTimeout(() => setState("idle"), 1800);
    } else {
      setState("error");
      pushToast("复制失败，请手动长按选中复制", "error");
      timerRef.current = window.setTimeout(() => setState("idle"), 2000);
    }
  };

  const classNames = [
    "btn",
    variant === "primary" ? "btn-primary" : variant === "ghost" ? "btn-ghost" : "btn-secondary",
    size === "sm" ? "btn-sm" : "",
    block ? "btn-block" : "",
    state === "success" ? "is-success" : "",
    state === "error" ? "is-error" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <button type="button" className={classNames} onClick={handleCopy}>
      {state === "success" ? (
        <CastoriceIcon name="check" size={15} />
      ) : state === "error" ? (
        <CastoriceIcon name="alert" size={15} />
      ) : (
        <CastoriceIcon name="paste" size={15} />
      )}
      <span>{state === "success" ? copiedLabel : state === "error" ? "复制失败" : label}</span>
    </button>
  );
}
