type PerfMeta = Record<string, unknown>;

export type BillingPerfEvent = {
  name: string;
  t: number;
  durationMs?: number;
  meta?: PerfMeta;
};

declare global {
  interface Window {
    __billingPerfEvents?: BillingPerfEvent[];
  }
}

const MAX_EVENTS = 600;

function enabled(): boolean {
  return (
    import.meta.env.DEV &&
    typeof window !== "undefined" &&
    typeof performance !== "undefined"
  );
}

function shouldLog(): boolean {
  if (!enabled()) return false;
  try {
    return window.localStorage?.getItem("billing_perf_debug") === "1";
  } catch {
    return false;
  }
}

export function perfMark(
  name: string,
  meta?: PerfMeta,
  durationMs?: number,
): void {
  if (!enabled()) return;
  const event: BillingPerfEvent = {
    name,
    t: performance.now(),
    durationMs,
    meta,
  };
  const events = window.__billingPerfEvents ?? [];
  events.push(event);
  if (events.length > MAX_EVENTS) events.splice(0, events.length - MAX_EVENTS);
  window.__billingPerfEvents = events;
  if (shouldLog()) {
    // eslint-disable-next-line no-console
    console.debug("[perf]", name, {
      durationMs:
        typeof durationMs === "number" ? Math.round(durationMs) : undefined,
      ...meta,
    });
  }
}

export function perfStart(name: string, meta?: PerfMeta) {
  if (!enabled()) return () => {};
  const start = performance.now();
  perfMark(`${name}:start`, meta);
  return (extraMeta?: PerfMeta) => {
    perfMark(name, { ...meta, ...extraMeta }, performance.now() - start);
  };
}
