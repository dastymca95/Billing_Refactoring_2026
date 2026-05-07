// Phase 1H — top-level PDF workspace.
//
// Composes the toolbar, the canvas (PDF.js render), and the overlay
// (region drawing + select/move/resize/delete). Region state is persisted
// to the backend via the `api.replaceRegions` PUT endpoint after each
// save. Selecting a different file resets the selection to page 1.

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { api, getFriendlyErrorMessage, isApiError } from "../../api";
import type { RegionHint, RegionLabel } from "../../types";
import { PdfOverlay } from "./PdfOverlay";
import { loadPdfDocument, PdfPageCanvas } from "./PdfPageCanvas";
import { TraceOverlay } from "./TraceOverlay";
import type { TraceItem } from "../../types";
import { ViewerToolbar } from "./ViewerToolbar";
import type { Tool } from "./types";

type Props = {
  batchId: string;
  fileUrl: string;
  fileId: string; // filename inside the batch input/ folder
  targetPage?: { pageNumber: number; nonce: number } | null;
  onActivePageChange?: (pageNumber: number) => void;
  // Phase 2J — Extraction Trace Overlay.
  // Trace ids the parent wants visually highlighted (e.g. set when a
  // template row is selected and we want to surface the regions that
  // fed it). Empty / undefined leaves nothing pre-highlighted.
  highlightedTraceIds?: ReadonlyArray<string>;
  // Called when the user clicks an overlay region. The parent uses
  // this to focus the corresponding template row.
  onTraceClick?: (traceId: string) => void;
  // Called when the user hovers an overlay region (id) or leaves it (null).
  onTraceHover?: (traceId: string | null) => void;
  // Phase 2K — Remap mode. When active, drawing a region calls
  // `onRemapDrawn` instead of persisting a regular region hint.
  remapActive?: boolean;
  onRemapDrawn?: (
    page: number,
    bbox: { x: number; y: number; w: number; h: number },
  ) => void;
};

type PageSize = { width: number; height: number };

export function PdfWorkspace({
  batchId,
  fileUrl,
  fileId,
  targetPage,
  onActivePageChange,
  highlightedTraceIds,
  onTraceClick,
  onTraceHover,
  remapActive,
  onRemapDrawn,
}: Props) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const pageRefs = useRef<Record<number, HTMLDivElement | null>>({});
  const ratiosRef = useRef<Map<number, number>>(new Map());
  const [activePageNumber, setActivePageNumber] = useState(1);
  const [pageCount, setPageCount] = useState(0);
  const [pageSizes, setPageSizes] = useState<Record<number, PageSize>>({});
  const [focusedPageNumber, setFocusedPageNumber] = useState<number | null>(null);
  // ``userZoom`` is what the operator chose via the toolbar / Ctrl+wheel.
  // ``effectiveZoom`` (computed below) is what the canvas actually
  // renders at — it can be clamped by the container's width so the
  // page auto-shrinks on a narrow window. Auto-grow is intentionally
  // NOT implemented: widening the window past the user's zoom keeps
  // the page at userZoom (no jumpy reflow).
  const [userZoom, setUserZoom] = useState(1.0);
  const [containerWidth, setContainerWidth] = useState<number | null>(null);
  // First page width at zoom=1 — used as the reference for fit-to-width.
  // We back this out from the rendered ``pageSizes[1].width`` divided by
  // the zoom that produced it, then cache the value across re-renders.
  const naturalPageWidthRef = useRef<number | null>(null);
  const [tool, setTool] = useState<Tool>("select");
  const [drawLabel, setDrawLabel] = useState<RegionLabel>("service_address");
  const [allRegions, setAllRegions] = useState<RegionHint[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  // Phase 2J — Extraction Trace Overlay.
  const [traces, setTraces] = useState<TraceItem[]>([]);
  const [tracesEnabled, setTracesEnabled] = useState<boolean>(true);
  const [hoveredTraceId, setHoveredTraceId] = useState<string | null>(null);
  const onActivePageChangeRef = useRef(onActivePageChange);

  useEffect(() => {
    onActivePageChangeRef.current = onActivePageChange;
  }, [onActivePageChange]);

  // ----- Phase 2I — fit-to-width on narrow + Ctrl+wheel zoom -----
  //
  // Track the canvas-area width; recompute on every resize. We use a
  // ResizeObserver instead of window.resize so we react to layout
  // changes (sidebar collapse, panel maximize) too.
  useEffect(() => {
    const node = scrollRef.current;
    if (!node) return;
    setContainerWidth(node.clientWidth);
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect?.width;
      if (typeof w === "number") setContainerWidth(w);
    });
    ro.observe(node);
    return () => ro.disconnect();
  }, []);

  // Compute the fit-to-width zoom from the latest natural page width.
  // When we don't have a natural width yet (PDF still loading), fit
  // is unconstrained → display zoom equals userZoom.
  const FIT_HORIZONTAL_PADDING = 32; // matches the canvas-area gutter
  const fitZoom = useMemo(() => {
    const natural = naturalPageWidthRef.current;
    if (!natural || !containerWidth || containerWidth <= 0) return Infinity;
    const usable = Math.max(120, containerWidth - FIT_HORIZONTAL_PADDING);
    return usable / natural;
  }, [containerWidth, pageSizes]);

  // Display zoom resolution.
  //
  //   • userZoom <= 1.0  →  auto-fit when the container is narrower
  //                          than the page (so a default view never
  //                          forces horizontal scroll). This is the
  //                          "shrink to fit" behaviour the user asked
  //                          for in Phase 2I.
  //   • userZoom  > 1.0  →  honour the user's chosen zoom EXACTLY,
  //                          even if the page now overflows the
  //                          container. The canvas-area has
  //                          ``overflow: auto`` so a horizontal scroll
  //                          bar appears, letting the operator pan
  //                          through the page to read small print.
  //
  // Floor at 0.2 so a tiny container doesn't produce a 0-pixel canvas.
  const zoom = useMemo(() => {
    if (userZoom > 1.0) return Math.max(0.2, userZoom);
    return Math.max(0.2, Math.min(userZoom, fitZoom));
  }, [userZoom, fitZoom]);

  // Phase 2I — Ctrl+wheel zoom, rewritten with the document-level
  // capture-phase technique used by react-pdf-viewer, Figma, etc.
  //
  // Why this approach beats the previous element-level listener:
  //
  //   1. The handler runs in the *capture* phase on `document`, which
  //      is the very first hook into the wheel event — before any
  //      bubbling, before any React synthetic listener, and before
  //      Chrome's built-in Ctrl+wheel page-zoom handler. preventDefault
  //      works reliably here even when the cursor is over a nested
  //      element with its own pointer state (canvas, overlay, scrollbar).
  //
  //   2. Some Chromium builds force passive on listeners attached to
  //      scrollable elements. A document listener is exempt from that
  //      heuristic, so `{ passive: false }` is always honoured.
  //
  //   3. We only act when the cursor is inside our workspace, gated by
  //      a single `contains()` DOM check. No element-tree assumptions.
  //
  // Wheel deltas are still rAF-batched: at most one zoom state update
  // per frame, regardless of how many wheel events fire. That keeps
  // React reconciliation off the critical path so the CSS-scaled
  // canvas can stay at 60 fps GPU compositing.
  // Phase 2I.2 — iPhone-pinch-feeling Ctrl+wheel zoom.
  //
  // Three things working together:
  //
  //   1. Direction follows the natural-scroll convention. Scrolling
  //      forward (positive deltaY on most laptops) GROWS the page;
  //      scrolling back shrinks it. The user explicitly asked for
  //      this orientation.
  //
  //   2. Multiplicative (exponential) deltas. A single wheel tick
  //      produces the *same perceived* change at 50 % zoom and at
  //      200 % zoom, exactly how trackpad pinch behaves on iOS.
  //
  //   3. Spring-style interpolation toward a target. Each wheel event
  //      bumps a target ref; a long-running rAF loop lerps the
  //      visible zoom toward that target by ~30 % per frame and
  //      stops when the difference is negligible. This converts a
  //      single discrete mouse-wheel notch (which would otherwise
  //      produce a one-frame jump) into a 5–10-frame smooth ramp.
  //      Trackpad pinch already fires fine-grained events, and the
  //      interpolation just rides along with them at high frequency
  //      — no perceptible lag.
  const ZOOM_SENSITIVITY = 0.0008;
  // How aggressive the per-frame interpolation is. 0.30 = 30 % of the
  // remaining gap each frame → settles in roughly 5-8 frames at 60 Hz.
  const ZOOM_LERP = 0.30;
  // When |target - current| drops below this, snap to target and stop
  // the animation loop. 0.0005 is well below visible threshold.
  const ZOOM_EPSILON = 0.0005;

  const targetZoomRef = useRef<number>(1.0);
  const lerpRafRef = useRef<number | null>(null);
  const userZoomRef = useRef<number>(1.0);
  useEffect(() => {
    userZoomRef.current = userZoom;
  }, [userZoom]);

  // Phase 2I.5 — Photoshop-style Space-to-pan.
  //
  // Hold Space → cursor flips to grab and the workspace becomes
  // pannable regardless of which tool is currently active. Click+
  // drag scrolls the canvas-area; release Space to revert. Mirrors
  // Photoshop / Figma / Illustrator. Implemented at the workspace
  // level so the overlay's draw/select handlers keep working when
  // Space is NOT held.
  //
  // Lifecycle:
  //  - keydown(Space) → spacePanActiveRef.current = true; CSS class.
  //  - pointerdown while active → start pan; capture pointer; record
  //    starting scroll + cursor.
  //  - pointermove while panning → delta-scroll the canvas-area.
  //  - pointerup / pointercancel → end pan (CSS class still on while
  //    Space is held).
  //  - keyup(Space) → clear active flag.
  //  - Space-down inside an input/textarea is ignored so typing keeps
  //    working.
  const [spacePanActive, setSpacePanActive] = useState(false);
  const panStateRef = useRef<{
    startX: number;
    startY: number;
    startScrollLeft: number;
    startScrollTop: number;
    pointerId: number;
  } | null>(null);
  // Phase 2I.8 — true while a pan is in flight AND for a short cooldown
  // afterwards. Suppresses scroll-mutating side effects (targetPage
  // scrollIntoView, IntersectionObserver→onActivePageChange callback)
  // that would otherwise fight the pan and "fly" the viewport away.
  const panActiveRef = useRef<boolean>(false);
  const panCooldownRef = useRef<number | null>(null);

  const finishPanGesture = useCallback(() => {
    const pan = panStateRef.current;
    const node = scrollRef.current;
    if (pan) {
      try {
        node?.releasePointerCapture?.(pan.pointerId);
      } catch {
        /* ignore */
      }
    }
    panStateRef.current = null;
    if (panCooldownRef.current != null) {
      window.clearTimeout(panCooldownRef.current);
    }
    panCooldownRef.current = window.setTimeout(() => {
      panActiveRef.current = false;
      panCooldownRef.current = null;
    }, 250);
  }, []);

  useEffect(() => {
    const isEditableTarget = (el: EventTarget | null) => {
      if (!(el instanceof HTMLElement)) return false;
      const tag = el.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
      if (el.isContentEditable) return true;
      return false;
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.code !== "Space" && e.key !== " ") return;
      if (isEditableTarget(e.target)) return;
      // Only engage when the pointer is over the workspace, OR when
      // the workspace is in the focus path. Otherwise Space could
      // hijack scrolling on totally unrelated parts of the app.
      const node = scrollRef.current;
      if (!node) return;
      // Heuristic: cursor is currently inside the workspace if the
      // mouse last moved over it. We don't track mouse globally, so
      // accept Space whenever the workspace exists in the layout —
      // the cursor change makes the intent obvious to the operator.
      // Prevent the browser's native repeated-Space scroll. Holding
      // Space fires repeat keydown events; if any repeat is allowed to
      // perform its default action, long multi-page PDFs drift toward
      // the last page while the operator is trying to pan.
      e.preventDefault();
      e.stopPropagation();
      setSpacePanActive(true);
    };
    const onKeyUp = (e: KeyboardEvent) => {
      if (e.code !== "Space" && e.key !== " ") return;
      e.preventDefault();
      setSpacePanActive(false);
      // If a pan was in flight, end it cleanly.
      finishPanGesture();
    };
    // Also drop pan if the window loses focus (Cmd/Alt-Tab) — otherwise
    // we'd come back to a "stuck Space" state.
    const onBlur = () => {
      setSpacePanActive(false);
      finishPanGesture();
    };
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("keyup", onKeyUp);
    window.addEventListener("blur", onBlur);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("keyup", onKeyUp);
      window.removeEventListener("blur", onBlur);
      if (panCooldownRef.current != null) {
        window.clearTimeout(panCooldownRef.current);
        panCooldownRef.current = null;
      }
    };
  }, [finishPanGesture]);

  // Pointer drag handlers wired to the scroll container. They no-op
  // unless Space is currently held, so the overlay's draw/select
  // pointer handlers continue to work in normal mode.
  const handlePanPointerDown = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      if (!spacePanActive) return;
      const node = scrollRef.current;
      if (!node) return;
      // Phase 2I.7 — kill any in-flight zoom animation BEFORE the pan
      // starts. Otherwise the lerp's setUserZoom would keep firing and
      // the zoom-to-cursor useLayoutEffect would snap scrollTop back
      // to the anchor on every frame, which felt like the page was
      // "flying down" while the operator tried to pan upward.
      if (lerpRafRef.current != null) {
        window.cancelAnimationFrame(lerpRafRef.current);
        lerpRafRef.current = null;
      }
      // Sync the lerp target to the visible zoom so a future Ctrl+
      // wheel doesn't suddenly resume from a stale target.
      targetZoomRef.current = userZoomRef.current;
      // Drop the cursor anchor so the layout-effect early-returns
      // through the entire pan gesture.
      zoomAnchorRef.current = null;

      panStateRef.current = {
        startX: e.clientX,
        startY: e.clientY,
        startScrollLeft: node.scrollLeft,
        startScrollTop: node.scrollTop,
        pointerId: e.pointerId,
      };
      panActiveRef.current = true;
      if (panCooldownRef.current != null) {
        window.clearTimeout(panCooldownRef.current);
        panCooldownRef.current = null;
      }
      // setPointerCapture so the move stays bound to the canvas-area
      // even if the cursor leaves it during the drag.
      node.setPointerCapture?.(e.pointerId);
      // Stop the overlay from interpreting this as a draw / select.
      e.preventDefault();
      e.stopPropagation();
    },
    [spacePanActive],
  );
  const handlePanPointerMove = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      const pan = panStateRef.current;
      if (!pan) return;
      const node = scrollRef.current;
      if (!node) return;
      const dx = e.clientX - pan.startX;
      const dy = e.clientY - pan.startY;
      // Phase 2I.7 — Photoshop hand-tool convention.
      //
      //   The cursor and the page move in OPPOSITE directions: you
      //   "grab" the document and drag it. So:
      //     • cursor RIGHT → page slides RIGHT (you see what was on
      //       the left) → scrollLeft DECREASES
      //     • cursor LEFT  → page slides LEFT  → scrollLeft INCREASES
      //     • cursor DOWN  → page slides DOWN  → scrollTop DECREASES
      //                                          (reveals top half)
      //     • cursor UP    → page slides UP    → scrollTop INCREASES
      //                                          (reveals bottom half)
      //
      // Browser scroll clamps at [0, max], so no fly-off at the
      // edges; on page 1 a downward drag stops at scrollTop=0
      // exactly when the top is reached.
      node.scrollLeft = pan.startScrollLeft - dx;
      node.scrollTop = pan.startScrollTop - dy;
      e.preventDefault();
    },
    [],
  );
  const handlePanPointerUp = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      const pan = panStateRef.current;
      if (!pan) return;
      // Keep the suppression flag on briefly: a smooth-scroll started
      // before pan-end can still be in flight, and the
      // IntersectionObserver fires async after the last scrollTop
      // mutation. 250 ms is enough for the observer to settle without
      // being noticeable to the operator.
      finishPanGesture();
      e.preventDefault();
    },
    [finishPanGesture],
  );

  // Phase 2I.4 — zoom-to-cursor anchor.
  //
  // Each wheel event captures the document point under the cursor at
  // *that* moment. While the lerp animates the zoom toward its target,
  // a useLayoutEffect adjusts ``scrollLeft / scrollTop`` so that the
  // captured document point stays under the cursor at every
  // intermediate zoom level. Toolbar buttons clear the anchor so they
  // continue to scroll-from-current-center without surprising jumps.
  //
  // ``docX / docY`` are in unscaled (zoom = 1) document coordinates,
  // ``viewX / viewY`` are the cursor's offset inside the scroll
  // container at capture time. The math: at any new zoom z,
  //   newScrollX = docX * z - viewX
  //   newScrollY = docY * z - viewY
  // keeps the anchor under the cursor.
  const zoomAnchorRef = useRef<
    { docX: number; docY: number; viewX: number; viewY: number } | null
  >(null);

  useEffect(() => {
    const startLerpLoop = () => {
      if (lerpRafRef.current != null) return;
      const tick = () => {
        lerpRafRef.current = null;
        const target = targetZoomRef.current;
        const current = userZoomRef.current;
        if (Math.abs(target - current) < ZOOM_EPSILON) {
          // Snap exactly to target (kills tiny floating-point drift).
          if (current !== target) {
            setUserZoom(+target.toFixed(4));
          }
          // Animation done — clear the cursor anchor so subsequent
          // toolbar zoom changes don't get pinned to a stale point.
          zoomAnchorRef.current = null;
          return;
        }
        const next = current + (target - current) * ZOOM_LERP;
        setUserZoom(+next.toFixed(4));
        lerpRafRef.current = window.requestAnimationFrame(tick);
      };
      lerpRafRef.current = window.requestAnimationFrame(tick);
    };

    const onWheel = (e: WheelEvent) => {
      if (!(e.ctrlKey || e.metaKey)) return;
      const node = scrollRef.current;
      if (!node) return;
      const target = e.target as Node | null;
      if (!target || !node.contains(target)) return;
      // We're handling this — stop the browser's default Ctrl+wheel
      // page-zoom AND any element-level scroll fall-through.
      e.preventDefault();
      e.stopPropagation();

      // Phase 2I.4 — capture the document point under the cursor BEFORE
      // we update the target zoom, so subsequent lerp frames can keep
      // that point pinned to the cursor.
      const rect = node.getBoundingClientRect();
      const viewX = e.clientX - rect.left;
      const viewY = e.clientY - rect.top;
      // Stay anchored to whatever zoom is *visible* right now (which
      // is what the operator's eye is locked onto). Using the live
      // userZoomRef rather than targetZoomRef avoids snap-back when
      // the cursor moves mid-animation.
      const liveZoom = Math.max(0.0001, userZoomRef.current);
      zoomAnchorRef.current = {
        docX: (node.scrollLeft + viewX) / liveZoom,
        docY: (node.scrollTop + viewY) / liveZoom,
        viewX,
        viewY,
      };

      // Multiplicative target update. Math.exp keeps the visual
      // step uniform regardless of where in the [0.25, 4.0] range we
      // currently sit. Sign chosen so scrolling FORWARD / fingers UP
      // grows the page and scroll BACK shrinks it (matches the user's
      // mental model regardless of OS natural-scroll setting).
      const factor = Math.exp(-e.deltaY * ZOOM_SENSITIVITY);
      const nextTarget = Math.max(
        0.25,
        Math.min(4.0, +(targetZoomRef.current * factor).toFixed(4)),
      );
      targetZoomRef.current = nextTarget;
      startLerpLoop();
    };
    // Capture-phase + passive:false at the document level. This is the
    // most reliable hook to outrun Chrome's built-in Ctrl+wheel zoom.
    document.addEventListener("wheel", onWheel, { passive: false, capture: true });
    return () => {
      document.removeEventListener(
        "wheel",
        onWheel as EventListener,
        { capture: true } as EventListenerOptions,
      );
      if (lerpRafRef.current != null) {
        window.cancelAnimationFrame(lerpRafRef.current);
        lerpRafRef.current = null;
      }
    };
  }, []);

  // Keep the lerp target aligned with the user's chosen zoom whenever
  // it's nudged via the toolbar buttons (not via wheel). Otherwise the
  // next wheel event would suddenly snap back to whatever the target
  // ref still holds.
  useEffect(() => {
    if (
      lerpRafRef.current == null &&
      Math.abs(targetZoomRef.current - userZoom) > ZOOM_EPSILON
    ) {
      targetZoomRef.current = userZoom;
    }
  }, [userZoom]);

  // Phase 2I.4 — zoom-to-cursor scroll adjustment.
  //
  // Runs synchronously after each zoom commit but before the browser
  // paints, so the operator sees the page already scrolled to the
  // right position; no jitter, no double-paint. Only fires while a
  // cursor anchor is active (i.e. a Ctrl+wheel gesture is in flight)
  // — toolbar zoom keeps the page centered on whatever was at the
  // top-left of the viewport, which is the conventional behaviour.
  useLayoutEffect(() => {
    const node = scrollRef.current;
    const anchor = zoomAnchorRef.current;
    if (!node || !anchor) return;
    // anchor.docX is in zoom = 1 coordinates, so the new scroll is
    // simply the anchor's screen position multiplied by the new zoom
    // minus the cursor offset inside the container.
    const desiredScrollX = anchor.docX * zoom - anchor.viewX;
    const desiredScrollY = anchor.docY * zoom - anchor.viewY;
    // Only assign if a meaningful change — avoids resetting subpixel
    // scroll values when nothing actually moved.
    if (Math.abs(node.scrollLeft - desiredScrollX) > 0.5) {
      node.scrollLeft = desiredScrollX;
    }
    if (Math.abs(node.scrollTop - desiredScrollY) > 0.5) {
      node.scrollTop = desiredScrollY;
    }
  }, [zoom]);

  useEffect(() => {
    let cancelled = false;
    setPageCount(0);
    setPageSizes({});
    ratiosRef.current.clear();
    // Phase 2I — natural width is per-file; reset when fileUrl changes.
    naturalPageWidthRef.current = null;
    (async () => {
      try {
        const { doc } = await loadPdfDocument(fileUrl);
        if (!cancelled) setPageCount(doc.numPages || 1);
      } catch (e) {
        if (!cancelled) setPageCount(1);
        // eslint-disable-next-line no-console
        console.warn("PDF metadata load failed:", e);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [fileUrl]);

  // Reload regions on batch change. Failures are tolerated: a missing
  // region_hints.json is NOT an error — the workspace simply starts
  // empty. Real network/server errors surface a compact retry button
  // instead of a raw HTTP message.
  const [loadAttempt, setLoadAttempt] = useState(0);
  useEffect(() => {
    let cancelled = false;
    setSaveError(null);
    (async () => {
      try {
        const r = await api.listRegions(batchId);
        if (!cancelled) setAllRegions(r.regions || []);
      } catch (e) {
        // 404 (no regions yet, or batch lookup miss) is not user-facing
        // noise — quietly start with an empty list. Anything else is a
        // real error worth surfacing in a compact, non-technical way.
        if (isApiError(e) && e.status === 404) {
          if (!cancelled) setAllRegions([]);
        } else if (!cancelled) {
          setSaveError(
            "Region hints could not be loaded.",
          );
          // Detailed error to console so a developer can still see it.
          // eslint-disable-next-line no-console
          console.warn("listRegions failed:", e);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [batchId, loadAttempt]);

  // Phase 2J — fetch extraction traces for the active document.
  // Failures are silent (the feature is best-effort): an empty list
  // means "the active vendor doesn't emit traces yet" and the toggle
  // stays disabled in the toolbar.
  useEffect(() => {
    let cancelled = false;
    setTraces([]);
    if (!batchId || !fileId) return;
    (async () => {
      try {
        const res = await api.getDocumentTrace(batchId, fileId);
        if (!cancelled) setTraces(res.items || []);
      } catch {
        if (!cancelled) setTraces([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [batchId, fileId]);

  // Group traces by page once so the per-page render loop doesn't
  // re-filter the array on every render.
  const tracesByPage = useMemo(() => {
    const out: Record<number, TraceItem[]> = {};
    for (const t of traces) {
      const p = Number(t.page) || 1;
      (out[p] ||= []).push(t);
    }
    return out;
  }, [traces]);

  const highlightedSet = useMemo(() => {
    return new Set<string>(highlightedTraceIds || []);
  }, [highlightedTraceIds]);

  const handleTraceHover = useCallback(
    (id: string | null) => {
      setHoveredTraceId(id);
      onTraceHover?.(id);
    },
    [onTraceHover],
  );

  // Reset to page 1 when file changes.
  useEffect(() => {
    setActivePageNumber(1);
    onActivePageChangeRef.current?.(1);
    setSelectedId(null);
  }, [fileId]);

  const pageNumbers = useMemo(() => {
    const count = Math.max(1, pageCount || 1);
    return Array.from({ length: count }, (_, i) => i + 1);
  }, [pageCount]);

  const regionsByPage = useMemo(() => {
    const grouped: Record<number, RegionHint[]> = {};
    for (const r of allRegions) {
      if (r.file_id !== fileId) continue;
      const page = r.page_number || 1;
      if (!grouped[page]) grouped[page] = [];
      grouped[page].push(r);
    }
    return grouped;
  }, [allRegions, fileId]);

  const regionsOnActivePage = useMemo(
    () => regionsByPage[activePageNumber] ?? [],
    [activePageNumber, regionsByPage],
  );

  const setPageRef = useCallback(
    (pageNumber: number) => (el: HTMLDivElement | null) => {
      pageRefs.current[pageNumber] = el;
    },
    [],
  );

  const scrollToPage = useCallback((pageNumber: number, behavior: ScrollBehavior = "smooth") => {
    const target = Math.max(1, Math.min(pageCount || pageNumber, pageNumber));
    const el = pageRefs.current[target];
    if (!el) return;
    setFocusedPageNumber(target);
    el.scrollIntoView({ behavior, block: "start", inline: "nearest" });
    window.setTimeout(() => {
      setFocusedPageNumber((current) => (current === target ? null : current));
    }, 1400);
  }, [pageCount]);

  useEffect(() => {
    if (!targetPage) return;
    if (targetPage.pageNumber < 1) return;
    // Skip while a pan is in flight: a programmatic scrollIntoView
    // would compete with the user's drag and "fly" them to whatever
    // page the parent thinks is active.
    if (panActiveRef.current) return;
    const handle = window.setTimeout(() => {
      if (panActiveRef.current) return;
      scrollToPage(targetPage.pageNumber, "smooth");
    }, 0);
    return () => window.clearTimeout(handle);
  }, [pageCount, scrollToPage, targetPage?.nonce, targetPage?.pageNumber]);

  useEffect(() => {
    const root = scrollRef.current;
    if (!root || pageNumbers.length === 0) return;
    const observer = new IntersectionObserver(
      (entries) => {
        let changed = false;
        for (const entry of entries) {
          const raw = (entry.target as HTMLElement).dataset.pageNumber;
          const n = raw ? Number(raw) : 0;
          if (!n) continue;
          if (entry.isIntersecting) {
            ratiosRef.current.set(n, entry.intersectionRatio);
          } else {
            ratiosRef.current.delete(n);
          }
          changed = true;
        }
        if (!changed || ratiosRef.current.size === 0) return;
        let bestPage = activePageNumber;
        let bestRatio = -1;
        for (const [n, ratio] of ratiosRef.current.entries()) {
          if (ratio > bestRatio || (ratio === bestRatio && n < bestPage)) {
            bestPage = n;
            bestRatio = ratio;
          }
        }
        if (bestPage !== activePageNumber) {
          setActivePageNumber(bestPage);
          // While panning, do NOT notify the parent. The parent uses
          // this signal to update ``targetPage``, which would feed
          // back into a programmatic scrollIntoView and fight the
          // user's drag.
          if (!panActiveRef.current) {
            onActivePageChangeRef.current?.(bestPage);
          }
        }
      },
      {
        root,
        threshold: [0.18, 0.35, 0.5, 0.7, 0.9],
      },
    );
    pageNumbers.forEach((n) => {
      const el = pageRefs.current[n];
      if (el) observer.observe(el);
    });
    return () => observer.disconnect();
  }, [activePageNumber, pageNumbers]);

  // Persist the full list to the backend (debounced via state-write
  // batching — every change calls saveRegions once on the next tick).
  const saveRegions = useCallback(
    async (next: RegionHint[]) => {
      setSaving(true);
      setSaveError(null);
      try {
        await api.replaceRegions(batchId, next);
      } catch (e) {
        setSaveError(getFriendlyErrorMessage(e, "Save regions"));
        // eslint-disable-next-line no-console
        console.warn("save regions failed:", e);
      } finally {
        setSaving(false);
      }
    },
    [batchId],
  );

  const handleAdd = useCallback(
    (region: RegionHint) => {
      // Phase 2K — when in remap mode, intercept the drawn region and
      // hand the bbox back to the parent instead of persisting it as
      // a region hint. We also exit draw mode automatically so the
      // viewer falls back to its normal "select" behaviour.
      if (remapActive && onRemapDrawn) {
        onRemapDrawn(region.page_number || 1, {
          x: region.bbox.x,
          y: region.bbox.y,
          w: region.bbox.w,
          h: region.bbox.h,
        });
        setTool("select");
        return;
      }
      const next = [...allRegions, region];
      setAllRegions(next);
      void saveRegions(next);
    },
    [allRegions, saveRegions, remapActive, onRemapDrawn],
  );

  // Force draw mode whenever the parent activates remap; restore to
  // "select" when remap turns off so regular panning resumes.
  useEffect(() => {
    if (remapActive) setTool("draw");
  }, [remapActive]);

  const handleUpdate = useCallback(
    (region: RegionHint) => {
      const next = allRegions.map((r) => (r.id === region.id ? region : r));
      setAllRegions(next);
      // Throttle saves to mouseup-grade frequency: only save when the
      // change affects bbox (move/resize commit). Selection-only
      // updates wouldn't reach this path. For Phase 1H foundation,
      // persist on every update — there's only one user at a time.
      void saveRegions(next);
    },
    [allRegions, saveRegions],
  );

  const handleDelete = useCallback(
    (id: string) => {
      const next = allRegions.filter((r) => r.id !== id);
      setAllRegions(next);
      if (selectedId === id) setSelectedId(null);
      void saveRegions(next);
    },
    [allRegions, saveRegions, selectedId],
  );

  return (
    <div className="pdf-workspace">
      {/* Phase 2J — Extraction Trace toggle. Only rendered when the
          active document has at least one trace; otherwise the toggle
          would be a dead control. Click toggles the overlay on/off; the
          subtle accent fill signals the active state. */}
      {traces.length > 0 && (
        <button
          type="button"
          className={`pdf-trace-toggle ${tracesEnabled ? "is-on" : ""}`}
          onClick={() => setTracesEnabled((v) => !v)}
          title={
            tracesEnabled
              ? `Hide extraction traces (${traces.length})`
              : `Show extraction traces (${traces.length})`
          }
          aria-pressed={tracesEnabled}
          data-testid="pdf-trace-toggle"
        >
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <rect x="4" y="6" width="14" height="3" rx="1" />
            <rect x="4" y="13" width="10" height="3" rx="1" />
            <circle cx="20" cy="7.5" r="1.5" />
            <circle cx="16" cy="14.5" r="1.5" />
          </svg>
          <span>Traces ({traces.length})</span>
        </button>
      )}
      <ViewerToolbar
        tool={tool}
        onToolChange={setTool}
        zoom={zoom}
        // Phase 2I — toolbar now mutates ``userZoom`` (the *intent*).
        // The displayed zoom is still the auto-fit-clamped value, but
        // a click on +/- moves the underlying preference up or down by
        // a fine 10 % step (was 25 %) so the buttons feel as fluid as
        // Ctrl+wheel.
        onZoomIn={() => setUserZoom((z) => Math.min(4.0, +(z + 0.1).toFixed(2)))}
        onZoomOut={() => setUserZoom((z) => Math.max(0.25, +(z - 0.1).toFixed(2)))}
        onResetZoom={() => setUserZoom(1.0)}
        drawLabel={drawLabel}
        onDrawLabelChange={setDrawLabel}
        pageNumber={activePageNumber}
        pageCount={pageCount}
        onPrevPage={() => scrollToPage(Math.max(1, activePageNumber - 1))}
        onNextPage={() => scrollToPage(Math.min(pageCount || activePageNumber + 1, activePageNumber + 1))}
        regionsCount={regionsOnActivePage.length}
      />
      {/* Phase 1W cleanup — only render the status row when there's
          actually something to say (saving / error). The previous
          "No field regions yet…" placeholder pill cluttered the
          toolbar; the Draw / Field controls in the toolbar already
          communicate what to do. When marks are present we surface
          the count via the toolbar's "N marks on this page" meta. */}
      {(saving || saveError) && (
        <div className="pdf-workspace-status">
          {saving && <span className="pill pill-info">Saving marks…</span>}
          {saveError && (
            <span className="pill pill-warn">
              {saveError}{" "}
              <button
                type="button"
                className="pill-link"
                onClick={() => setLoadAttempt((n) => n + 1)}
              >
                Retry
              </button>
            </span>
          )}
        </div>
      )}
      <div
        className={`pdf-workspace-canvas-area ${
          spacePanActive
            ? panStateRef.current
              ? "is-space-panning"
              : "is-space-pan"
            : ""
        }`}
        ref={scrollRef}
        data-testid="pdf-continuous-scroll"
        onPointerDown={handlePanPointerDown}
        onPointerMove={handlePanPointerMove}
        onPointerUp={handlePanPointerUp}
        onPointerCancel={handlePanPointerUp}
      >
        <div className="pdf-workspace-stack pdf-workspace-continuous">
          {pageNumbers.map((n) => {
            const size = pageSizes[n];
            const isActive = n === activePageNumber;
            const isFocused = n === focusedPageNumber;
            return (
              <div
                key={`${fileUrl}:${n}`}
                ref={setPageRef(n)}
                className={`pdf-page-shell ${isActive ? "active-page" : ""} ${
                  isFocused ? "focused-page" : ""
                }`}
                data-page-number={n}
                data-testid="pdf-page-shell"
              >
                <div className="pdf-page-label">Page {n}</div>
                <PdfPageCanvas
                  fileUrl={fileUrl}
                  pageNumber={n}
                  zoom={zoom}
                  onPageRendered={(info) => {
                    // Phase 2I defensive: never accept pageCount <= 0
                    // from a child render. A bug in earlier code emitted
                    // 0 on each zoom tick, which collapsed pageNumbers
                    // to [1] and killed continuous scroll. Keep whatever
                    // count we already have if the child can't supply
                    // a fresh, positive number.
                    if (typeof info.pageCount === "number" && info.pageCount > 0) {
                      setPageCount((prev) => Math.max(prev, info.pageCount));
                    }
                    setPageSizes((prev) => ({
                      ...prev,
                      [info.pageNumber]: {
                        width: info.pageWidth,
                        height: info.pageHeight,
                      },
                    }));
                    // Phase 2I — back the natural (zoom=1) width out of
                    // the rendered width. We capture this once per file
                    // (page 1 is enough — every page in a vendor's PDF
                    // bundle has the same width). zoom can briefly be
                    // 0 during the very first render; guard it.
                    if (
                      info.pageNumber === 1 &&
                      zoom > 0 &&
                      naturalPageWidthRef.current == null
                    ) {
                      const natural = info.pageWidth / zoom;
                      if (Number.isFinite(natural) && natural > 0) {
                        naturalPageWidthRef.current = natural;
                        // Force a re-evaluation of fitZoom by nudging
                        // pageSizes (already set above — useMemo will
                        // pick up the new ref value).
                        setContainerWidth((w) => (w == null ? w : w));
                      }
                    }
                  }}
                />
                {size && (
                  <PdfOverlay
                    pageWidth={size.width}
                    pageHeight={size.height}
                    pageNumber={n}
                    fileId={fileId}
                    tool={tool}
                    drawLabel={drawLabel}
                    regions={regionsByPage[n] ?? []}
                    selectedId={selectedId}
                    onSelect={setSelectedId}
                    onAdd={handleAdd}
                    onUpdate={handleUpdate}
                    onDelete={handleDelete}
                  />
                )}
                {size && (
                  <TraceOverlay
                    pageNumber={n}
                    pageWidth={size.width}
                    pageHeight={size.height}
                    items={tracesByPage[n] ?? []}
                    highlightedTraceIds={highlightedSet}
                    hoveredTraceId={hoveredTraceId}
                    onTraceHover={handleTraceHover}
                    onTraceClick={onTraceClick}
                    enabled={tracesEnabled}
                  />
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
