// Phase 2L — vertical wheel → horizontal scroll on a container.
//
// When the user hovers over a horizontally-scrollable container
// (typically because the table or canvas is wider than the viewport
// and the bottom horizontal scrollbar is showing) and turns the
// scroll wheel, this hook redirects the vertical wheel delta to
// horizontal scrolling. Mirrors how trackpads behave when you swipe
// left/right and lets mouse-only operators reach off-screen columns
// without grabbing the scrollbar thumb.
//
// Behaviour:
//   * Active only when the container has horizontal overflow
//     (`scrollWidth > clientWidth`) AND the cursor is inside the
//     bottom horizontal-scrollbar strip. Anywhere else inside the
//     container the wheel falls through so vertical scrolling (and
//     parent scrolling) keeps working as the user expects.
//   * Vertical wheel (deltaY) becomes horizontal scroll (scrollLeft).
//     Direction is INVERTED (wheel-down → scroll left, wheel-up →
//     scroll right) per operator preference.
//   * Skipped when the user holds Ctrl / Meta (those modifier keys
//     are reserved for zoom in PDF viewers and shouldn't be hijacked).
//   * Smoothness: each wheel event nudges a *target* scrollLeft and
//     a long-running rAF loop lerps the visible scrollLeft toward
//     that target by ~25 % per frame. A single discrete mouse-wheel
//     notch becomes a 5–10-frame ramp, which feels as fluid as the
//     PDF viewer's Ctrl+wheel zoom (same lerp pattern).
//
// Usage:
//
//     const setScrollPane = useWheelHorizontalScroll();
//     return <div ref={setScrollPane} className="my-scroll-area">...</div>;

import { useCallback, useEffect, useRef, useState } from "react";

const SCROLL_LERP = 0.25;

export function useWheelHorizontalScroll(): (
  node: HTMLElement | null,
) => void {
  const [node, setNode] = useState<HTMLElement | null>(null);
  const refCallback = useCallback((n: HTMLElement | null) => {
    setNode(n);
  }, []);
  const targetRef = useRef<number>(0);
  const rafRef = useRef<number | null>(null);

  useEffect(() => {
    if (!node) return;

    targetRef.current = node.scrollLeft;

    const startLerp = () => {
      if (rafRef.current != null) return;
      const tick = () => {
        const current = node.scrollLeft;
        const target = targetRef.current;
        const diff = target - current;
        if (Math.abs(diff) < 0.5) {
          node.scrollLeft = target;
          rafRef.current = null;
          return;
        }
        node.scrollLeft = current + diff * SCROLL_LERP;
        rafRef.current = window.requestAnimationFrame(tick);
      };
      rafRef.current = window.requestAnimationFrame(tick);
    };

    const onWheel = (e: WheelEvent) => {
      // Don't hijack zoom (Ctrl/Meta + wheel).
      if (e.ctrlKey || e.metaKey) return;
      // Need horizontal overflow to do anything useful.
      if (node.scrollWidth <= node.clientWidth) return;
      // Only react when the cursor is hovering the bottom horizontal
      // scrollbar strip. Anywhere else in the container we want the
      // wheel to behave normally (vertical scroll, parent scroll).
      // The strip height is offsetHeight - clientHeight (the space
      // the native scrollbar takes from the inner content area).
      const scrollbarHeight = node.offsetHeight - node.clientHeight;
      if (scrollbarHeight <= 0) return;
      const rect = node.getBoundingClientRect();
      // Add a small fudge so the strip is easier to hit. The native
      // scrollbar is usually ~12-16 px tall; we accept a few extra
      // pixels above it as still "on the bar".
      const stripTop = rect.bottom - scrollbarHeight - 4;
      if (e.clientY < stripTop || e.clientY > rect.bottom) return;
      // Only act if there's a meaningful vertical delta. If the user
      // is doing a horizontal swipe (deltaX != 0, deltaY ≈ 0) the
      // browser already does the right thing — leave it alone.
      const dy = e.deltaY;
      if (dy === 0) return;
      e.preventDefault();
      // Inverted direction per operator preference: wheel-down (dy>0)
      // moves the view LEFT (decreases scrollLeft).
      const max = node.scrollWidth - node.clientWidth;
      const next = Math.max(0, Math.min(max, targetRef.current - dy));
      targetRef.current = next;
      startLerp();
    };

    // Keep the lerp target in sync if the user drags the scrollbar
    // thumb directly — otherwise the next wheel event would snap
    // back to a stale target.
    const onScroll = () => {
      if (rafRef.current == null) {
        targetRef.current = node.scrollLeft;
      }
    };

    node.addEventListener("wheel", onWheel, { passive: false });
    node.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      node.removeEventListener("wheel", onWheel);
      node.removeEventListener("scroll", onScroll);
      if (rafRef.current != null) {
        window.cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
    };
  }, [node]);

  return refCallback;
}
