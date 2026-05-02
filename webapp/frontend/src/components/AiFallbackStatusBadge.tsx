// Phase 1J — premium AI status pill with hover popover.
//
// Reads `GET /api/ai/status` once on mount. Pill text is human-friendly
// (AI Off / AI Ready / AI Not Configured) — never raw enum values.
// Hovering opens a popover that explains what AI would help with and
// whether it's currently active. Never displays API keys.

import { useEffect, useRef, useState } from "react";

import { api } from "../api";
import type { AiStatus } from "../types";

type Props = {
  className?: string;
};

const AI_HELP_TASKS = [
  "Service address extraction",
  "Account number extraction",
  "Invoice / due dates",
  "Total amount disambiguation",
  "Notice boundary detection",
  "OCR cleanup on messy scans",
  "Manual-review explanations",
];

export function AiFallbackStatusBadge({ className }: Props) {
  const [status, setStatus] = useState<AiStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLSpanElement | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const s = await api.getAiStatus();
        if (!cancelled) setStatus(s);
      } catch (e) {
        if (!cancelled) {
          setError(String(e));
          // eslint-disable-next-line no-console
          console.warn("AI status fetch failed:", e);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Click outside to close popover.
  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open]);

  const labelInfo = labelFor(status, error);

  return (
    <span
      ref={wrapRef}
      className={`ai-pill-wrap ${className ?? ""}`}
    >
      <button
        type="button"
        className={`ai-pill ${labelInfo.tone}`}
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-haspopup="dialog"
      >
        <span className={`ai-pill-dot ${labelInfo.tone}`} aria-hidden />
        {labelInfo.label}
      </button>
      {open && (
        <div className="ai-pill-popover" role="dialog" aria-label="AI status">
          <div className="ai-pop-row">
            <span className="ai-pop-key">Status</span>
            <span className="ai-pop-val">{labelInfo.label}</span>
          </div>
          <div className="ai-pop-row">
            <span className="ai-pop-key">Provider</span>
            <span className="ai-pop-val">
              {prettyProvider(status?.provider)}
            </span>
          </div>
          <div className="ai-pop-row">
            <span className="ai-pop-key">Policy</span>
            <span className="ai-pop-val">
              {status?.policy
                ? prettyPolicy(status.policy)
                : "—"}
            </span>
          </div>
          {status?.max_cost_per_batch_usd ? (
            <div className="ai-pop-row">
              <span className="ai-pop-key">Cost ceiling</span>
              <span className="ai-pop-val">
                ${status.max_cost_per_batch_usd.toFixed(2)} / batch
              </span>
            </div>
          ) : null}
          <div className="ai-pop-message">{labelInfo.message}</div>
          <div className="ai-pop-section-title">What AI would help with</div>
          <ul className="ai-pop-tasks">
            {AI_HELP_TASKS.map((t) => (
              <li key={t}>{t}</li>
            ))}
          </ul>
          {!status?.enabled && (
            <div className="ai-pop-hint">
              Configure a provider in <code>.env</code>
              {" "}(<code>AI_FALLBACK_ENABLED=true</code> +
              <code> AI_PROVIDER=…</code> + the matching API key) to enable
              fallback suggestions.
            </div>
          )}
        </div>
      )}
    </span>
  );
}

type Tone = "tone-loading" | "tone-off" | "tone-configured" | "tone-ready" | "tone-error";

function labelFor(
  status: AiStatus | null,
  error: string | null,
): { label: string; tone: Tone; message: string } {
  if (error) {
    return {
      label: "AI Error",
      tone: "tone-error",
      message:
        "AI status check failed. The app continues with rules and OCR only.",
    };
  }
  if (!status) {
    return {
      label: "AI…",
      tone: "tone-loading",
      message: "Checking AI configuration…",
    };
  }
  if (status.enabled) {
    return {
      label: "AI Ready",
      tone: "tone-ready",
      message:
        "AI fallback is configured and may suggest values when rules + OCR confidence is low. Every AI-filled field is flagged for manual review.",
    };
  }
  if (status.provider === "disabled") {
    return {
      label: "AI Off",
      tone: "tone-off",
      message:
        "AI fallback is intentionally off. The app is running rules, OCR, YAML, and validation only.",
    };
  }
  if (!status.configured) {
    return {
      label: "AI Not Configured",
      tone: "tone-configured",
      message:
        "A provider is selected but no API key is set. Add the key to .env to activate.",
    };
  }
  return {
    label: "AI Off",
    tone: "tone-off",
    message:
      "AI fallback is currently off. The app is running rules, OCR, YAML, and validation only.",
  };
}

function prettyProvider(p?: string): string {
  if (!p) return "—";
  switch (p) {
    case "disabled":
      return "Disabled";
    case "openai":
      return "OpenAI";
    case "anthropic":
      return "Anthropic";
    case "google_gemini":
      return "Google Gemini";
    case "deepseek":
      return "DeepSeek";
    default:
      return p;
  }
}

function prettyPolicy(p: string): string {
  return p.replace(/_/g, " ");
}
