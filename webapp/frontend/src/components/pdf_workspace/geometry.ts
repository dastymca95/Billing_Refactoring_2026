// Phase 1H — geometry helpers for the PDF workspace.
//
// Regions live in NORMALIZED coordinates: x, y, w, h ∈ [0, 1] of the
// rendered page. This survives zoom changes, screen size changes, and
// device pixel ratio. The conversions live here so the canvas + overlay
// + regionbox stay in sync.

export type Px = { x: number; y: number; w: number; h: number };
export type Norm = { x: number; y: number; w: number; h: number };

export function clamp(v: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, v));
}

export function normToPx(n: Norm, pageW: number, pageH: number): Px {
  return {
    x: n.x * pageW,
    y: n.y * pageH,
    w: n.w * pageW,
    h: n.h * pageH,
  };
}

export function pxToNorm(p: Px, pageW: number, pageH: number): Norm {
  if (pageW <= 0 || pageH <= 0) {
    return { x: 0, y: 0, w: 0, h: 0 };
  }
  return {
    x: clamp(p.x / pageW, 0, 1),
    y: clamp(p.y / pageH, 0, 1),
    w: clamp(p.w / pageW, 0, 1),
    h: clamp(p.h / pageH, 0, 1),
  };
}

export function normaliseDragBox(
  start: { x: number; y: number },
  end: { x: number; y: number },
  pageW: number,
  pageH: number,
): Norm {
  const x1 = Math.min(start.x, end.x);
  const y1 = Math.min(start.y, end.y);
  const x2 = Math.max(start.x, end.x);
  const y2 = Math.max(start.y, end.y);
  return pxToNorm({ x: x1, y: y1, w: x2 - x1, h: y2 - y1 }, pageW, pageH);
}

// Hit-test a normalized region against a click point in pixel space.
export function hitTest(
  region: Norm,
  clickPx: { x: number; y: number },
  pageW: number,
  pageH: number,
): boolean {
  const px = normToPx(region, pageW, pageH);
  return (
    clickPx.x >= px.x &&
    clickPx.x <= px.x + px.w &&
    clickPx.y >= px.y &&
    clickPx.y <= px.y + px.h
  );
}

// Generate a random region id. Not cryptographically secure — only used
// to differentiate regions in the local list and on the wire.
export function newRegionId(): string {
  return (
    "rg_" +
    Math.random().toString(36).slice(2, 8) +
    Date.now().toString(36).slice(-4)
  );
}
