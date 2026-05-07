// Phase 1O — stable single-page canvas renderer.
//
// Phase 2I — split *display* zoom from *raster* zoom.
//
//   The earlier implementation re-rendered the PDF.js page on every
//   zoom change. PDF.js raster work is heavy: in continuous scroll a
//   14-page document means 14 simultaneous heavy renders per wheel
//   tick. The result was a chunky, juddering zoom even on fast
//   machines.
//
//   The fix is the technique every quality PDF/image viewer uses:
//
//     • The canvas is rasterised at a stable ``rasterScale``.
//     • While the user is zooming, the canvas is visually scaled
//       via CSS (``transform`` + a matching outer width/height).
//       The browser does a single bilinear stretch — basically free.
//     • When the user pauses for ~150 ms we re-rasterise at the
//       latest ``displayScale`` and reset the CSS scale to 1×.
//
//   This keeps Ctrl+wheel zoom hyper-fluid and only pays the heavy
//   raster cost once the operator settles on a zoom level.

import { useCallback, useEffect, useRef, useState } from "react";

type PageInfo = {
  pageWidth: number;
  pageHeight: number;
  pageNumber: number;
  pageCount: number;
};

type Props = {
  fileUrl: string;
  pageNumber: number;
  zoom: number;
  onPageRendered?: (info: PageInfo) => void;
};

// Per-tab cache for loaded PDF documents. Avoids re-parsing the same
// file when navigating between its pages. Keyed by the absolute URL
// the document was loaded from. We cap the cache size aggressively so
// we don't hold many MB of pdf.js state for batches that are no longer
// open. Eviction policy: oldest first.
export type PdfDoc = {
  pdfjs: any;
  doc: any;
};
const _docCache = new Map<string, Promise<PdfDoc>>();
const MAX_DOC_CACHE = 4;

let _workerSrc: string | null = null;

async function loadPdfjs(): Promise<any> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const pdfjs: any = await import("pdfjs-dist/build/pdf.mjs");
  if (_workerSrc == null) {
    _workerSrc = (
      await import("pdfjs-dist/build/pdf.worker.mjs?url")
    ).default;
    pdfjs.GlobalWorkerOptions.workerSrc = _workerSrc;
  }
  return pdfjs;
}

async function getDoc(fileUrl: string): Promise<PdfDoc> {
  let p = _docCache.get(fileUrl);
  if (!p) {
    p = (async () => {
      const pdfjs = await loadPdfjs();
      const loadingTask = pdfjs.getDocument(fileUrl);
      const doc = await loadingTask.promise;
      return { pdfjs, doc };
    })();
    _docCache.set(fileUrl, p);
    if (_docCache.size > MAX_DOC_CACHE) {
      const oldest = _docCache.keys().next().value;
      if (oldest && oldest !== fileUrl) _docCache.delete(oldest);
    }
  }
  return p;
}

export async function loadPdfDocument(fileUrl: string): Promise<PdfDoc> {
  return getDoc(fileUrl);
}

// How long the user has to be idle (no zoom change) before we trigger
// a high-fidelity re-raster. 150 ms is the sweet spot — long enough
// to coalesce a wheel-spin into one render, short enough that pausing
// briefly while zooming feels instant.
const RASTER_DEBOUNCE_MS = 150;

// Beyond this stretch we trigger a re-raster even mid-gesture so the
// page never looks too pixelated. 1.6× is roughly where bilinear up-
// scaling starts to read as fuzzy on retina displays.
const STRETCH_THRESHOLD = 1.6;

export function PdfPageCanvas({ fileUrl, pageNumber, zoom, onPageRendered }: Props) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [hasFrame, setHasFrame] = useState(false);
  // Phase 2I — natural (zoom = 1) page size, captured on the first
  // successful render. All subsequent layout math is derived from it,
  // so we don't have to re-rasterise just to learn the page dimensions.
  const [naturalSize, setNaturalSize] = useState<{ width: number; height: number } | null>(null);

  // The scale we *actually* rasterised at. Lags the `zoom` prop while
  // the user is wheel-zooming; catches up after the debounce.
  const [rasterScale, setRasterScale] = useState<number>(zoom);
  const lastZoomRef = useRef<number>(zoom);
  // Loading is true while a render is in flight, but the overlay only
  // appears after a small delay so quick page navs don't flash.
  const [loadingVisible, setLoadingVisible] = useState(false);

  // Stable ref for the parent's onPageRendered callback so we don't
  // re-fire the effect on every parent re-render.
  const onPageRenderedRef = useRef(onPageRendered);
  useEffect(() => {
    onPageRenderedRef.current = onPageRendered;
  }, [onPageRendered]);

  const setLoadingDelayed = useCallback((on: boolean) => {
    if (on) {
      // Wait 250 ms before showing the overlay; if the render is
      // already done by then we never show it at all.
      const handle = window.setTimeout(() => setLoadingVisible(true), 250);
      return () => window.clearTimeout(handle);
    }
    setLoadingVisible(false);
    return () => {};
  }, []);

  // Reset raster cache when the file or page changes.
  useEffect(() => {
    setNaturalSize(null);
    setHasFrame(false);
    setRasterScale(zoom);
    lastZoomRef.current = zoom;
    // We deliberately omit `zoom` here — the goal is to reset only on
    // file/page swaps, not on zoom changes (which the dedicated
    // debounce effect below handles).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fileUrl, pageNumber]);

  // Phase 2I — debounce the heavy raster re-render. While the user is
  // wheel-zooming, ``zoom`` keeps changing every few milliseconds; we
  // only push the new value into ``rasterScale`` once they pause.
  // Exception: if the stretch from rasterScale to zoom passes the
  // pixelation threshold, raster sooner so the page doesn't look
  // visibly soft mid-gesture.
  useEffect(() => {
    lastZoomRef.current = zoom;
    if (Math.abs(zoom - rasterScale) < 0.001) return;
    const ratio = zoom / Math.max(0.0001, rasterScale);
    const aggressive = ratio > STRETCH_THRESHOLD || ratio < 1 / STRETCH_THRESHOLD;
    const delay = aggressive ? 0 : RASTER_DEBOUNCE_MS;
    const handle = window.setTimeout(() => {
      // Belt-and-braces: only raster the *latest* value the wheel
      // came to rest on, not whatever stale prop fired this timeout.
      setRasterScale(lastZoomRef.current);
    }, delay);
    return () => window.clearTimeout(handle);
  }, [zoom, rasterScale]);

  // Heavy raster effect. Runs only when fileUrl / pageNumber /
  // rasterScale change — *not* on every wheel tick. Renders into an
  // offscreen buffer first so the visible canvas keeps its previous
  // frame until the new one is fully painted.
  useEffect(() => {
    let cancelled = false;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    let renderTask: any = null;
    setError(null);
    const cancelDelay = setLoadingDelayed(true);

    (async () => {
      try {
        const { doc } = await getDoc(fileUrl);
        if (cancelled) return;
        const page = await doc.getPage(pageNumber);
        if (cancelled) return;

        const viewport = page.getViewport({ scale: rasterScale });
        const visibleCanvas = canvasRef.current;
        if (!visibleCanvas) return;

        const dpr = window.devicePixelRatio || 1;
        const w = Math.floor(viewport.width);
        const h = Math.floor(viewport.height);
        const offscreen = document.createElement("canvas");
        offscreen.width = Math.floor(w * dpr);
        offscreen.height = Math.floor(h * dpr);
        const offCtx = offscreen.getContext("2d");
        if (!offCtx) return;
        offCtx.setTransform(dpr, 0, 0, dpr, 0, 0);

        renderTask = page.render({ canvasContext: offCtx, viewport });
        await renderTask.promise;
        if (cancelled) return;

        visibleCanvas.width = offscreen.width;
        visibleCanvas.height = offscreen.height;
        const visCtx = visibleCanvas.getContext("2d");
        if (visCtx) {
          visCtx.drawImage(offscreen, 0, 0);
        }

        // Phase 2I — capture natural (zoom = 1) page size from the
        // first successful raster. We back it out of the rasterised
        // size and the scale we used. Keeps the value stable across
        // future re-rasters (which would otherwise round it).
        const natural =
          naturalSize ?? {
            width: w / rasterScale,
            height: h / rasterScale,
          };
        if (!naturalSize) setNaturalSize(natural);

        setHasFrame(true);
        cancelDelay();
        setLoadingVisible(false);
        // Notify the parent with the *displayed* size at the current
        // zoom (not the rasterised size). The parent uses this for
        // overlay geometry; the overlay should follow display, not
        // raster. Use lastZoomRef so we always emit the latest zoom
        // even if rasterScale has lagged.
        const displayZoom = lastZoomRef.current;
        onPageRenderedRef.current?.({
          pageWidth: Math.round(natural.width * displayZoom),
          pageHeight: Math.round(natural.height * displayZoom),
          pageNumber,
          pageCount: doc.numPages,
        });
      } catch (e: unknown) {
        if (cancelled) return;
        if ((e as { name?: string })?.name === "RenderingCancelledException")
          return;
        setError("Could not render this PDF page.");
        // eslint-disable-next-line no-console
        console.warn("PDF render failed:", e);
        cancelDelay();
        setLoadingVisible(false);
      }
    })();

    return () => {
      cancelled = true;
      cancelDelay();
      try {
        renderTask?.cancel?.();
      } catch {
        /* ignore */
      }
    };
  }, [fileUrl, pageNumber, rasterScale, setLoadingDelayed]);

  // Display geometry. Width/height of the wrap follow ``zoom`` so the
  // surrounding layout reflows in real time. The canvas is scaled via
  // CSS by ``zoom / rasterScale`` so the bitmap stretches without a
  // re-raster while the user is wheel-zooming.
  const displayWidth = naturalSize ? naturalSize.width * zoom : null;
  const displayHeight = naturalSize ? naturalSize.height * zoom : null;
  const stretch = rasterScale > 0 ? zoom / rasterScale : 1;

  // Phase 2I — the layout-update effect that used to re-emit
  // ``onPageRendered`` on every zoom change has been retired.
  //
  // It was passing ``pageCount: 0`` (because we don't know the real
  // count from inside this component without re-asking PDF.js). The
  // parent's ``setPageCount(info.pageCount)`` then collapsed the
  // ``pageNumbers`` memo to ``[1]`` and unmounted the other pages,
  // killing continuous scroll mid-zoom.
  //
  // The wrap's inline width/height already follow `zoom` (see the
  // `displayWidth/displayHeight` style block below) so the page layout
  // reflows in real time without poking the parent. The overlay sees
  // a brief raster-scale-vs-display mismatch during the gesture; the
  // 150 ms debounce realigns it as soon as the user pauses.

  return (
    <div
      className={`pdf-canvas-wrap ${hasFrame ? "has-frame" : "waiting-first-frame"}`}
      style={
        displayWidth != null && displayHeight != null
          ? {
              width: `${displayWidth}px`,
              minHeight: `${displayHeight}px`,
              height: `${displayHeight}px`,
            }
          : undefined
      }
    >
      <canvas
        ref={canvasRef}
        className={`pdf-canvas ${hasFrame ? "" : "hidden-until-ready"}`}
        style={
          // Phase 2I — instant CSS-driven zoom. Width/height are the
          // raster size at rasterScale; transform stretches the canvas
          // to the live displayScale. Once the debounce fires and
          // rasterScale catches up, ``stretch`` is 1.0 and the canvas
          // sits at native fidelity again.
          naturalSize
            ? {
                width: `${naturalSize.width * rasterScale}px`,
                height: `${naturalSize.height * rasterScale}px`,
                transform:
                  Math.abs(stretch - 1) < 0.001
                    ? undefined
                    : `scale(${stretch})`,
                transformOrigin: "0 0",
                // Use auto image-rendering — bilinear gives the best
                // perceived quality for moderate stretches; pixelated
                // would look harsh during the gesture.
                imageRendering: "auto",
                // GPU-accelerate the transform so it stays at 60 fps
                // even on slower laptops.
                willChange: stretch !== 1 ? "transform" : "auto",
              }
            : undefined
        }
      />
      {!hasFrame && !error && (
        <div className="pdf-canvas-first-frame" data-testid="pdf-first-frame-placeholder">
          <div className="pdf-canvas-first-frame-page" />
        </div>
      )}
      {loadingVisible && !error && (
        <div className="pdf-canvas-loading-overlay" aria-live="polite">
          <div className="pdf-canvas-loading-card">
            <span className="pdf-loading-dots" aria-hidden>
              <span /><span /><span />
            </span>
            <span className="pdf-loading-label">Loading document…</span>
          </div>
        </div>
      )}
      {error && (
        <div className="pdf-canvas-error">PDF render failed: {error}</div>
      )}
    </div>
  );
}
