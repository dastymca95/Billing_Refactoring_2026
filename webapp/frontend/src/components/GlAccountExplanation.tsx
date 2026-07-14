import {
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
} from "react";
import { createPortal } from "react-dom";

import type { PreviewRow } from "../types";

type Alternative = { code: string; name: string; whyNot: string };
type Explanation = {
  accountName: string;
  reason: string;
  evidence: string[];
  alternatives: Alternative[];
  confidence: string;
  review: string;
  source: string;
  versions: string;
  scoreComponents: string[];
};

export function GlAccountExplanation({ row, glAccount, glName }: {
  row: PreviewRow;
  glAccount: unknown;
  glName?: string;
}) {
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState<{ left: number; top: number; placement: "left" | "right" }>({ left: 0, top: 0, placement: "right" });
  const id = useId();
  const containerRef = useRef<HTMLSpanElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const popoverRef = useRef<HTMLDivElement | null>(null);
  const closeTimerRef = useRef<number | null>(null);
  const glCode = normalizeGlCode(glAccount);
  const explanation = useMemo(() => backendExplanation(row._meta?.accounting_decision, glCode, glName), [row, glCode, glName]);

  const clearCloseTimer = () => {
    if (closeTimerRef.current != null) window.clearTimeout(closeTimerRef.current);
    closeTimerRef.current = null;
  };
  const openPopover = () => { clearCloseTimer(); setOpen(true); };
  const scheduleClose = () => {
    clearCloseTimer();
    closeTimerRef.current = window.setTimeout(() => setOpen(false), 90);
  };
  const updatePosition = () => {
    const rect = triggerRef.current?.getBoundingClientRect();
    if (!rect) return;
    const margin = 12;
    const gap = 10;
    const width = Math.min(420, window.innerWidth * 0.78);
    const height = popoverRef.current?.offsetHeight || 220;
    let placement: "left" | "right" = "right";
    let left = rect.right + gap;
    if (left + width + margin > window.innerWidth) {
      placement = "left";
      left = Math.max(margin, rect.left - width - gap);
    }
    const centerY = rect.top + rect.height / 2;
    const top = Math.max(margin + height / 2, Math.min(window.innerHeight - margin - height / 2, centerY));
    setPosition({ left, top, placement });
  };

  useLayoutEffect(() => { if (open) updatePosition(); }, [open, glCode]);
  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); setOpen(false); }
    };
    document.addEventListener("keydown", onKeyDown);
    window.addEventListener("resize", updatePosition);
    window.addEventListener("scroll", updatePosition, true);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("resize", updatePosition);
      window.removeEventListener("scroll", updatePosition, true);
    };
  }, [open]);
  useEffect(() => () => clearCloseTimer(), []);

  if (!glCode || !explanation) return null;
  const style: CSSProperties = { left: position.left, top: position.top };
  const closeIfFocusLeaves = (target: EventTarget | null) => {
    const node = target instanceof Node ? target : null;
    if (node && (containerRef.current?.contains(node) || popoverRef.current?.contains(node))) return;
    setOpen(false);
  };
  const popover = open && typeof document !== "undefined" ? createPortal(
    <div id={id} ref={popoverRef} className="gl-explain-popover" data-placement={position.placement}
      role="tooltip" style={style} onMouseEnter={openPopover} onMouseLeave={scheduleClose}
      onFocus={openPopover} onBlur={(event) => closeIfFocusLeaves(event.relatedTarget)}>
      <div className="gl-explain-title">{glCode} - {explanation.accountName}</div>
      <p>{explanation.reason}</p>
      <dl>
        <dt>Evidence</dt>
        <dd>{explanation.evidence.map((item) => <span key={item} className="gl-explain-line">{item}</span>)}</dd>
        <dt>Rejected</dt>
        <dd>{explanation.alternatives.map((item) => <span key={item.code} className="gl-explain-alt"><b>{item.code} - {item.name}</b>: {item.whyNot}</span>)}</dd>
        <dt>Confidence</dt><dd>{explanation.confidence}</dd>
        <dt>Review</dt><dd>{explanation.review}</dd>
        <dt>Decision source</dt><dd>{explanation.source}</dd>
        <dt>Score</dt><dd>{explanation.scoreComponents.map((item) => <span key={item} className="gl-explain-line">{item}</span>)}</dd>
        <dt>Versions</dt><dd>{explanation.versions}</dd>
      </dl>
    </div>, document.body) : null;

  return <span ref={containerRef} className="gl-explain" onMouseEnter={openPopover} onMouseLeave={scheduleClose}
    onFocus={openPopover} onBlur={(event) => closeIfFocusLeaves(event.relatedTarget)}>
    <button ref={triggerRef} type="button" className="gl-explain-trigger"
      aria-label={`Explain GL account ${glCode}`} aria-expanded={open} aria-describedby={open ? id : undefined}>!</button>
    {popover}
  </span>;
}

function backendExplanation(raw: unknown, glCode: string, explicitName?: string): Explanation | null {
  if (!raw || typeof raw !== "object") return null;
  const decision = raw as Record<string, unknown>;
  const selected = normalizeGlCode(decision.selected_gl_code);
  if (!selected || selected !== glCode) return null;
  const ranked = arrayValue(decision.candidates_ranked);
  const evidence = arrayValue(decision.evidence).map((item) => {
    if (!item || typeof item !== "object") return "";
    const ref = item as Record<string, unknown>;
    const text = stringValue(ref.text) || stringValue(ref.normalized_text);
    return text ? `"${text}"` : "";
  }).filter(Boolean);
  const alternatives = [...arrayValue(decision.rejected_alternatives), ...ranked]
    .map<Alternative | null>((item) => {
      if (!item || typeof item !== "object") return null;
      const value = item as Record<string, unknown>;
      const code = normalizeGlCode(value.gl_code);
      if (!code || code === selected) return null;
      return { code, name: stringValue(value.gl_name), whyNot: stringValue(value.reason) || "Lower backend score." };
    }).filter((item): item is Alternative => item !== null);
  const top = ranked[0] && typeof ranked[0] === "object" ? ranked[0] as Record<string, unknown> : {};
  const components = top.score_components && typeof top.score_components === "object"
    ? Object.entries(top.score_components as Record<string, unknown>).map(([key, value]) => `${key}: ${Number(value).toFixed(2)}`)
    : [];
  const confidence = typeof decision.confidence === "number" ? `${Math.round(decision.confidence * 100)}%` : "Not supplied";
  return {
    accountName: stringValue(decision.selected_gl_name) || explicitName || "",
    reason: stringValue(decision.why_selected), evidence, alternatives: dedupe(alternatives), confidence,
    review: Boolean(decision.review_required) ? `${Boolean(decision.review_blocking) ? "Blocking" : "Required"}${stringValue(decision.review_reason) ? ` - ${stringValue(decision.review_reason)}` : ""}` : "None",
    source: stringValue(decision.decision_source),
    versions: [decision.decision_version, decision.semantic_version, decision.catalog_version].filter(Boolean).join(" · "),
    scoreComponents: components,
  };
}

function normalizeGlCode(value: unknown): string {
  return (String(value ?? "").trim().match(/\b\d{4}\b/) || [])[0] || "";
}
function arrayValue(value: unknown): unknown[] { return Array.isArray(value) ? value : []; }
function stringValue(value: unknown): string { return String(value ?? "").trim(); }
function dedupe(values: Alternative[]): Alternative[] {
  const seen = new Set<string>();
  return values.filter((value) => !seen.has(value.code) && Boolean(seen.add(value.code)));
}
