// Phase 1H — single-page canvas renderer.
//
// Renders one page of a PDF to a `<canvas>` via PDF.js. The canvas is
// rendered ONCE per (file, page, zoom) tuple — no re-render on mouse
// move. The overlay is positioned absolutely on top to receive
// pointer events.

import { useEffect, useRef, useState } from "react";

type Props = {
  fileUrl: string;
  pageNumber: number;
  zoom: number; // 1.0 = native render
  onPageRendered?: (info: {
    pageWidth: number;
    pageHeight: number;
    pageNumber: number;
    pageCount: number;
  }) => void;
};

export function PdfPageCanvas({ fileUrl, pageNumber, zoom, onPageRendered }: Props) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    let renderTask: any = null;
    setError(null);
    setLoading(true);

    (async () => {
      try {
        // Lazy-import pdfjs so the native preview path doesn't pay for
        // the bundle. We import the legacy build because the modern
        // ESM worker setup is more involved.
        const pdfjs = await import("pdfjs-dist/build/pdf.mjs");
        // Inline worker: pdfjs lets us point at a worker URL via Vite's
        // ?url import. This avoids a separate worker file in /public.
        const workerSrc = (
          await import("pdfjs-dist/build/pdf.worker.mjs?url")
        ).default;
        pdfjs.GlobalWorkerOptions.workerSrc = workerSrc;

        const loadingTask = pdfjs.getDocument(fileUrl);
        const doc = await loadingTask.promise;
        if (cancelled) return;
        const page = await doc.getPage(pageNumber);
        if (cancelled) return;

        const viewport = page.getViewport({ scale: zoom });
        const canvas = canvasRef.current;
        if (!canvas) return;
        const ctx = canvas.getContext("2d");
        if (!ctx) return;

        const dpr = window.devicePixelRatio || 1;
        canvas.width = Math.floor(viewport.width * dpr);
        canvas.height = Math.floor(viewport.height * dpr);
        canvas.style.width = `${Math.floor(viewport.width)}px`;
        canvas.style.height = `${Math.floor(viewport.height)}px`;
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

        renderTask = page.render({ canvasContext: ctx, viewport });
        if (renderTask) {
          await renderTask.promise;
        }
        if (cancelled) return;
        setLoading(false);
        onPageRendered?.({
          pageWidth: Math.floor(viewport.width),
          pageHeight: Math.floor(viewport.height),
          pageNumber,
          pageCount: doc.numPages,
        });
      } catch (e: unknown) {
        if (cancelled) return;
        // pdf.js throws an exception with name="RenderingCancelledException"
        // when we cancel; that's expected on remount, not an error.
        if ((e as { name?: string })?.name === "RenderingCancelledException")
          return;
        const msg =
          (e as { message?: string })?.message ?? String(e);
        setError(msg);
        setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
      try {
        renderTask?.cancel?.();
      } catch {
        /* ignore */
      }
    };
  }, [fileUrl, pageNumber, zoom, onPageRendered]);

  return (
    <div className="pdf-canvas-wrap">
      {loading && <div className="pdf-canvas-loading">Rendering…</div>}
      {error && (
        <div className="pdf-canvas-error">PDF render failed: {error}</div>
      )}
      <canvas ref={canvasRef} className="pdf-canvas" />
    </div>
  );
}
