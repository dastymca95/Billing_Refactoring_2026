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

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

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
  initialNaturalSize?: { width: number; height: number } | null;
  onFirstFrame?: (pageNumber: number) => void;
  onPageRendered?: (info: PageInfo) => void;
  suppressFirstFramePlaceholder?: boolean;
};

type PageFrame = {
  fileUrl: string;
  canvas: HTMLCanvasElement;
  naturalSize: { width: number; height: number };
  pageNumber: number;
  pageCount: number;
  rasterScale: number;
  dpr: number;
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
const _pageFrameCache = new Map<string, PageFrame>();
const _pageFramePromises = new Map<string, Promise<PageFrame>>();
const _pagePreviewCache = new Map<string, PageFrame>();
const MAX_PAGE_FRAME_CACHE = 96;

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

function pageFrameKey(
  fileUrl: string,
  pageNumber: number,
  rasterScale: number,
  dpr: number,
): string {
  return `${fileUrl}::${pageNumber}::${rasterScale.toFixed(3)}::${dpr.toFixed(2)}`;
}

function pagePreviewKey(fileUrl: string, pageNumber: number, dpr: number): string {
  return `${fileUrl}::${pageNumber}::${dpr.toFixed(2)}`;
}

function getCachedPageFrame(
  fileUrl: string,
  pageNumber: number,
  rasterScale: number,
  dpr: number,
): PageFrame | null {
  const key = pageFrameKey(fileUrl, pageNumber, rasterScale, dpr);
  const frame = _pageFrameCache.get(key);
  if (!frame) return null;
  _pageFrameCache.delete(key);
  _pageFrameCache.set(key, frame);
  return frame;
}

function getPreviewPageFrame(
  fileUrl: string,
  pageNumber: number,
  dpr: number,
): PageFrame | null {
  return _pagePreviewCache.get(pagePreviewKey(fileUrl, pageNumber, dpr)) ?? null;
}

function trimPageFrameCache(): void {
  while (_pageFrameCache.size > MAX_PAGE_FRAME_CACHE) {
    const oldest = _pageFrameCache.keys().next().value;
    if (!oldest) break;
    const frame = _pageFrameCache.get(oldest);
    _pageFrameCache.delete(oldest);
    if (frame) {
      const previewKey = pagePreviewKey(frame.fileUrl, frame.pageNumber, frame.dpr);
      if (_pagePreviewCache.get(previewKey) === frame) {
        _pagePreviewCache.delete(previewKey);
      }
    }
  }
}

function paintPageFrame(canvas: HTMLCanvasElement, frame: PageFrame): void {
  canvas.width = frame.canvas.width;
  canvas.height = frame.canvas.height;
  const ctx = canvas.getContext("2d");
  if (ctx) {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(frame.canvas, 0, 0);
  }
}

async function renderPageFrame(
  fileUrl: string,
  pageNumber: number,
  rasterScale: number,
  dpr: number,
): Promise<PageFrame> {
  const key = pageFrameKey(fileUrl, pageNumber, rasterScale, dpr);
  const cached = _pageFrameCache.get(key);
  if (cached) return cached;
  const pending = _pageFramePromises.get(key);
  if (pending) return pending;

  const promise = (async () => {
    const { doc } = await getDoc(fileUrl);
    const page = await doc.getPage(pageNumber);
    const viewport = page.getViewport({ scale: rasterScale });
    const w = Math.floor(viewport.width);
    const h = Math.floor(viewport.height);
    const offscreen = document.createElement("canvas");
    offscreen.width = Math.floor(w * dpr);
    offscreen.height = Math.floor(h * dpr);
    const offCtx = offscreen.getContext("2d");
    if (!offCtx) throw new Error("Canvas context unavailable.");
    offCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const renderTask = page.render({ canvasContext: offCtx, viewport });
    await renderTask.promise;
    const frame: PageFrame = {
      fileUrl,
      canvas: offscreen,
      naturalSize: {
        width: w / rasterScale,
        height: h / rasterScale,
      },
      pageNumber,
      pageCount: doc.numPages,
      rasterScale,
      dpr,
    };
    _pageFrameCache.set(key, frame);
    _pagePreviewCache.set(pagePreviewKey(fileUrl, pageNumber, dpr), frame);
    trimPageFrameCache();
    return frame;
  })();

  _pageFramePromises.set(key, promise);
  promise.then(
    () => _pageFramePromises.delete(key),
    () => _pageFramePromises.delete(key),
  );
  return promise;
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
const DEFAULT_LAYOUT_SIZE = { width: 612, height: 792 };

export function PdfPageCanvas({
  fileUrl,
  pageNumber,
  zoom,
  initialNaturalSize,
  onFirstFrame,
  onPageRendered,
  suppressFirstFramePlaceholder = false,
}: Props) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [hasFrame, setHasFrame] = useState(false);
  // Phase 2I — natural (zoom = 1) page size, captured on the first
  // successful render. All subsequent layout math is derived from it,
  // so we don't have to re-rasterise just to learn the page dimensions.
  const [naturalSize, setNaturalSize] = useState<{ width: number; height: number } | null>(
    initialNaturalSize ?? null,
  );

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
  const onFirstFrameRef = useRef(onFirstFrame);
  useEffect(() => {
    onFirstFrameRef.current = onFirstFrame;
  }, [onFirstFrame]);

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
    const dpr = window.devicePixelRatio || 1;
    const cached =
      getCachedPageFrame(fileUrl, pageNumber, zoom, dpr) ||
      getPreviewPageFrame(fileUrl, pageNumber, dpr);
    setNaturalSize(cached?.naturalSize ?? initialNaturalSize ?? null);
    setHasFrame(Boolean(cached));
    setRasterScale(zoom);
    lastZoomRef.current = zoom;
    // We deliberately omit `zoom` here — the goal is to reset only on
    // file/page swaps, not on zoom changes (which the dedicated
    // debounce effect below handles).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fileUrl, pageNumber, initialNaturalSize?.width, initialNaturalSize?.height]);

  const notifyRenderedFrame = useCallback((frame: PageFrame) => {
    const displayZoom = lastZoomRef.current;
    onPageRenderedRef.current?.({
      pageWidth: Math.round(frame.naturalSize.width * displayZoom),
      pageHeight: Math.round(frame.naturalSize.height * displayZoom),
      pageNumber: frame.pageNumber,
      pageCount: frame.pageCount,
    });
    onFirstFrameRef.current?.(frame.pageNumber);
  }, []);

  useLayoutEffect(() => {
    const visibleCanvas = canvasRef.current;
    if (!visibleCanvas) return;
    const dpr = window.devicePixelRatio || 1;
    const cached =
      getCachedPageFrame(fileUrl, pageNumber, rasterScale, dpr) ||
      getPreviewPageFrame(fileUrl, pageNumber, dpr);
    if (!cached) return;
    paintPageFrame(visibleCanvas, cached);
    setNaturalSize(cached.naturalSize);
    setHasFrame(true);
    setLoadingVisible(false);
    notifyRenderedFrame(cached);
  }, [fileUrl, pageNumber, rasterScale, notifyRenderedFrame]);

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
    setError(null);
    const cancelDelay = setLoadingDelayed(true);

    (async () => {
      try {
        const visibleCanvas = canvasRef.current;
        if (!visibleCanvas) return;

        const dpr = window.devicePixelRatio || 1;
        const frame = await renderPageFrame(fileUrl, pageNumber, rasterScale, dpr);
        if (cancelled) return;

        paintPageFrame(visibleCanvas, frame);

        // Phase 2I — capture natural (zoom = 1) page size from the
        // first successful raster. We back it out of the rasterised
        // size and the scale we used. Keeps the value stable across
        // future re-rasters (which would otherwise round it).
        const natural = naturalSize ?? frame.naturalSize;
        if (!naturalSize) setNaturalSize(natural);

        setHasFrame(true);
        notifyRenderedFrame(frame);
        cancelDelay();
        setLoadingVisible(false);
      } catch (e: unknown) {
        if (cancelled) return;
        if ((e as { name?: string })?.name === "RenderingCancelledException")
          return;
        setError("Could not render this PDF page.");
        onFirstFrameRef.current?.(pageNumber);
        // eslint-disable-next-line no-console
        console.warn("PDF render failed:", e);
        cancelDelay();
        setLoadingVisible(false);
      }
    })();

    return () => {
      cancelled = true;
      cancelDelay();
    };
  }, [fileUrl, pageNumber, rasterScale, setLoadingDelayed, notifyRenderedFrame]);

  // Display geometry. Width/height of the wrap follow ``zoom`` so the
  // surrounding layout reflows in real time. The canvas is scaled via
  // CSS by ``zoom / rasterScale`` so the bitmap stretches without a
  // re-raster while the user is wheel-zooming.
  const layoutSize = naturalSize ?? initialNaturalSize ?? DEFAULT_LAYOUT_SIZE;
  const displayWidth = layoutSize.width * zoom;
  const displayHeight = layoutSize.height * zoom;
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
        {
          width: `${displayWidth}px`,
          minHeight: `${displayHeight}px`,
          height: `${displayHeight}px`,
        }
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
      {!suppressFirstFramePlaceholder && !hasFrame && !error && (
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
