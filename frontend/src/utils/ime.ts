import type { KeyboardEvent } from "react";

export function isImeComposing(event: KeyboardEvent<HTMLElement>): boolean {
  const nativeEvent = event.nativeEvent as globalThis.KeyboardEvent & { keyCode?: number };
  return Boolean(nativeEvent.isComposing || nativeEvent.keyCode === 229);
}
