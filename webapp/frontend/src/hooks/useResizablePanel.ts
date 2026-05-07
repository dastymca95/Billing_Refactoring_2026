// Phase 1J → fixed in Phase 1L.
//
// Pointer-driven panel resize hook with localStorage persistence.
//
// PHASE 1L BUG FIX
// ----------------
// Previously: after releasing the mouse on a divider, moving the mouse
// could continue to resize the panel ("sticky drag"). Root cause: the
// move/up listeners were registered on `window`, not the divider, AND
// `onUp` was rebuilt on every state change which created a brief
// window where the listener wasn't attached when pointerup fired —
// e.g. when the pointer was released outside the browser viewport.
//
// Fix:
//   * Use Pointer Events with `setPointerCapture` on the divider so
//     all subsequent pointermove / pointerup / pointercancel events
//     are routed to the same element regardless of where the pointer
//     ends up.
//   * Move/up handlers live on the divider, not the window.
//   * Hard guard: if `e.buttons === 0` while moving (i.e. no button
//     held down), stop immediately. This catches edge cases where the
//     OS swallows pointerup (alt-tab, dragging into devtools, etc.).
//   * Belt-and-braces window-level listeners for `pointerup`,
//     `pointercancel`, `blur`, `visibilitychange`, and a
//     `mouseleave` on `document` — any of which terminate the drag.
//   * `body.style.cursor` and `body.style.userSelect` are restored on
//     stop AND on hook unmount so a runaway drag can't leave the page
//     stuck in a col-resize cursor.

import { useCallback, useEffect, useRef, useState } from "react";

type Direction = "horizontal" | "vertical";

type Options = {
  storageKey: string;
  defaultSize: number;
  min: number;
  max: number;
  direction?: Direction;
  /** Inverted means the size shrinks as the user drags toward higher
   *  coordinates (e.g. when the panel sits on the right edge). */
  inverted?: boolean;
};

function readStored(key: string, fallback: number): number {
  try {
    const v = window.localStorage.getItem(key);
    if (!v) return fallback;
    const n = Number(v);
    if (!Number.isFinite(n) || n <= 0) return fallback;
    return n;
  } catch {
    return fallback;
  }
}

function writeStored(key: string, value: number): void {
  try {
    window.localStorage.setItem(key, String(Math.round(value)));
  } catch {
    /* localStorage may be disabled */
  }
}

export function useResizablePanel(opts: Options) {
  const { storageKey, defaultSize, min, max } = opts;
  const direction: Direction = opts.direction ?? "horizontal";
  const inverted = !!opts.inverted;

  const [size, setSize] = useState<number>(() =>
    clamp(readStored(storageKey, defaultSize), min, max),
  );

  // Drag state lives in refs (not state) so the move/up handlers see
  // current values without triggering re-registrations and without
  // stale closures.
  const draggingRef = useRef(false);
  const startCoordRef = useRef(0);
  const startSizeRef = useRef(size);
  const sizeRef = useRef(size);
  const handleElRef = useRef<HTMLElement | null>(null);
  const pointerIdRef = useRef<number | null>(null);

  // Keep `sizeRef` in sync so we always persist the latest value on
  // stop (size dep would otherwise force handler rebuilds).
  useEffect(() => {
    sizeRef.current = size;
  }, [size]);

  const stopDrag = useCallback(
    (persist: boolean) => {
      if (!draggingRef.current) return;
      draggingRef.current = false;
      if (typeof document !== "undefined") {
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
      }
      // Release pointer capture if we still have it. Wrapping in
      // try/catch because the element may already be detached or the
      // pointer id may already be released.
      const el = handleElRef.current;
      const pid = pointerIdRef.current;
      if (el && pid != null) {
        try {
          el.releasePointerCapture(pid);
        } catch {
          /* no-op */
        }
      }
      pointerIdRef.current = null;
      if (persist) {
        writeStored(storageKey, sizeRef.current);
      }
    },
    [storageKey],
  );

  const onPointerDown = useCallback(
    (e: React.PointerEvent<HTMLElement>) => {
      if (e.button !== 0) return; // primary button only
      draggingRef.current = true;
      startCoordRef.current =
        direction === "horizontal" ? e.clientX : e.clientY;
      startSizeRef.current = sizeRef.current;
      handleElRef.current = e.currentTarget;
      pointerIdRef.current = e.pointerId;
      try {
        e.currentTarget.setPointerCapture(e.pointerId);
      } catch {
        /* setPointerCapture not supported / already captured */
      }
      document.body.style.cursor =
        direction === "horizontal" ? "col-resize" : "row-resize";
      document.body.style.userSelect = "none";
      e.preventDefault();
    },
    [direction],
  );

  const onPointerMove = useCallback(
    (e: React.PointerEvent<HTMLElement>) => {
      if (!draggingRef.current) return;
      // HARD GUARD: if no primary button is held, the OS already
      // delivered a pointerup we may have missed (e.g. focus loss).
      // Bail and stop the drag.
      if (e.buttons === 0) {
        stopDrag(true);
        return;
      }
      const coord = direction === "horizontal" ? e.clientX : e.clientY;
      const delta = coord - startCoordRef.current;
      const next = clamp(
        startSizeRef.current + (inverted ? -delta : delta),
        min,
        max,
      );
      // Only schedule a re-render when the value actually changed.
      if (next !== sizeRef.current) {
        sizeRef.current = next;
        setSize(next);
      }
    },
    [direction, inverted, min, max, stopDrag],
  );

  const onPointerUp = useCallback(() => {
    stopDrag(true);
  }, [stopDrag]);

  const onPointerCancel = useCallback(() => {
    stopDrag(false);
  }, [stopDrag]);

  const onDoubleClick = useCallback(() => {
    setSize(defaultSize);
    sizeRef.current = defaultSize;
    writeStored(storageKey, defaultSize);
  }, [defaultSize, storageKey]);

  // Belt-and-braces: stop a drag if focus or visibility changes (e.g.
  // alt-tab away while holding the mouse). These never fire during
  // normal drag because we have the divider's pointermove/up listeners,
  // but they catch the edge cases the user reported.
  useEffect(() => {
    const onWinPointerUp = () => stopDrag(true);
    const onWinPointerCancel = () => stopDrag(false);
    const onBlur = () => stopDrag(true);
    const onVisibility = () => {
      if (document.visibilityState !== "visible") stopDrag(true);
    };
    const onDocMouseLeave = () => stopDrag(true);
    window.addEventListener("pointerup", onWinPointerUp);
    window.addEventListener("pointercancel", onWinPointerCancel);
    window.addEventListener("blur", onBlur);
    document.addEventListener("visibilitychange", onVisibility);
    document.addEventListener("mouseleave", onDocMouseLeave);
    return () => {
      window.removeEventListener("pointerup", onWinPointerUp);
      window.removeEventListener("pointercancel", onWinPointerCancel);
      window.removeEventListener("blur", onBlur);
      document.removeEventListener("visibilitychange", onVisibility);
      document.removeEventListener("mouseleave", onDocMouseLeave);
    };
  }, [stopDrag]);

  // Final cleanup on unmount: never leave the body in col-resize.
  useEffect(() => {
    return () => {
      if (draggingRef.current) {
        draggingRef.current = false;
        if (typeof document !== "undefined") {
          document.body.style.cursor = "";
          document.body.style.userSelect = "";
        }
      }
    };
  }, []);

  const reset = useCallback(() => {
    setSize(defaultSize);
    sizeRef.current = defaultSize;
    writeStored(storageKey, defaultSize);
  }, [defaultSize, storageKey]);

  const dragHandleProps = {
    onPointerDown,
    onPointerMove,
    onPointerUp,
    onPointerCancel,
    onLostPointerCapture: () => stopDrag(true),
    onDoubleClick,
    role: "separator",
    "aria-orientation":
      (direction === "horizontal" ? "vertical" : "horizontal") as
        | "vertical"
        | "horizontal",
    tabIndex: 0,
    title: "Drag to resize · double-click to reset",
  };

  return { size, dragHandleProps, reset };
}

function clamp(v: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, v));
}
