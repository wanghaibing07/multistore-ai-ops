import { flushSync } from "react-dom";

export type RedeemTransitionDirection = "forward" | "back";

interface ViewTransitionHandle {
  finished: Promise<void>;
  ready: Promise<void>;
  updateCallbackDone: Promise<void>;
}

type DocumentWithViewTransition = Document & {
  startViewTransition?: (callback: () => void) => ViewTransitionHandle;
};

export function prefersReducedMotion(): boolean {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
    return false;
  }
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

export function runRedeemViewTransition(
  updateDom: () => void,
  direction: RedeemTransitionDirection = "forward"
): void {
  if (typeof document === "undefined") {
    updateDom();
    return;
  }

  const doc = document as DocumentWithViewTransition;
  const root = document.documentElement;

  if (prefersReducedMotion() || typeof doc.startViewTransition !== "function") {
    root.dataset.vtFallback = direction;
    updateDom();
    window.setTimeout(() => {
      if (root.dataset.vtFallback === direction) {
        delete root.dataset.vtFallback;
      }
    }, 360);
    return;
  }

  root.dataset.vtDirection = direction;
  try {
    const transition = doc.startViewTransition(() => {
      flushSync(() => {
        updateDom();
      });
    });
    transition.finished
      .catch(() => undefined)
      .finally(() => {
        if (root.dataset.vtDirection === direction) {
          delete root.dataset.vtDirection;
        }
      });
  } catch {
    delete root.dataset.vtDirection;
    updateDom();
  }
}
