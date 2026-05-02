// Phase 1J — pointer-driven panel resize hook with localStorage persistence.
//
// Returns the current size + drag handle props. Wire the returned
// `dragHandleProps` onto a `<div>` divider; the hook handles
// pointer capture, cursor styling, min/max clamping, and persistence.
//
// Usage:
//   const { size, dragHandleProps, reset } = useResizablePanel({
//     storageKey: "billing_refactoring_layout_sidebar_width",
//     defaultSize: 260,
//     min: 200,
//     max: 480,
//     direction: "horizontal",
//   });
//   <aside style={{ width: size }}>…</aside>
//   <div className="resizer" {...dragHandleProps} />
//
// Direction:
//   "horizontal"  — drag changes width
//   "vertical"    — drag changes height
//
// Drag math:
//   When dragging, we read the live mouse position from a window-level
//   listener (NOT the divider's own pointermove) so dragging beyond the
//   divider into the next pane doesn't lose the grip.

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
  const draggingRef = useRef(false);
  const startCoordRef = useRef(0);
  const startSizeRef = useRef(size);

  const stop = useCallback(() => {
    if (!draggingRef.current) return;
    draggingRef.current = false;
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
  }, []);

  const onMove = useCallback(
    (e: PointerEvent) => {
      if (!draggingRef.current) return;
      const coord = direction === "horizontal" ? e.clientX : e.clientY;
      const delta = coord - startCoordRef.current;
      const next = clamp(
        startSizeRef.current + (inverted ? -delta : delta),
        min,
        max,
      );
      setSize(next);
    },
    [direction, inverted, min, max],
  );

  const onUp = useCallback(() => {
    if (!draggingRef.current) return;
    stop();
    // Persist the final value once on release. Saving on every move
    // would write to localStorage 60×/sec.
    writeStored(storageKey, size);
  }, [size, storageKey, stop]);

  useEffect(() => {
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
    };
  }, [onMove, onUp]);

  const onPointerDown = useCallback(
    (e: React.PointerEvent<HTMLElement>) => {
      // Only respond to primary mouse / touch / pen.
      if (e.button !== 0) return;
      draggingRef.current = true;
      startCoordRef.current =
        direction === "horizontal" ? e.clientX : e.clientY;
      startSizeRef.current = size;
      document.body.style.cursor =
        direction === "horizontal" ? "col-resize" : "row-resize";
      document.body.style.userSelect = "none";
      e.preventDefault();
    },
    [direction, size],
  );

  const onDoubleClick = useCallback(() => {
    setSize(defaultSize);
    writeStored(storageKey, defaultSize);
  }, [defaultSize, storageKey]);

  const reset = useCallback(() => {
    setSize(defaultSize);
    writeStored(storageKey, defaultSize);
  }, [defaultSize, storageKey]);

  const dragHandleProps = {
    onPointerDown,
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
