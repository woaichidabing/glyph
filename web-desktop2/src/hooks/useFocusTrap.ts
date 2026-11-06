import { useEffect, type RefObject } from "react";

const FOCUSABLE =
  'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * 把键盘焦点圈在一个弹窗里：Tab / Shift+Tab 在首尾之间循环，
 * 关闭后焦点回到打开前的元素。Esc 由各弹窗自己处理。
 */
export function useFocusTrap(ref: RefObject<HTMLElement | null>, active: boolean): void {
  useEffect(() => {
    if (!active) return;
    const root = ref.current;
    if (!root) return;
    const previous = document.activeElement as HTMLElement | null;
    const items = (): HTMLElement[] =>
      Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter((el) => el.offsetParent !== null);
    const preferred = root.querySelector<HTMLElement>("[data-autofocus]");
    const first = items()[0];
    (preferred ?? first ?? root).focus();
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key !== "Tab") return;
      const list = items();
      if (list.length === 0) return;
      const firstEl = list[0];
      const lastEl = list[list.length - 1];
      if (ev.shiftKey && document.activeElement === firstEl) {
        ev.preventDefault();
        lastEl.focus();
      } else if (!ev.shiftKey && document.activeElement === lastEl) {
        ev.preventDefault();
        firstEl.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      previous?.focus?.();
    };
  }, [active, ref]);
}
