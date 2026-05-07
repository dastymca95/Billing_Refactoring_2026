// Phase 1J — premium AI status pill with hover popover.
//
// Reads `GET /api/ai/status` once on mount. Pill text is human-friendly
// (AI Off / AI Ready / AI Not Configured) — never raw enum values.
// Hovering opens a popover that explains what AI would help with and
// whether it's currently active. Never displays API keys.

import { useEffect, useRef, useState } from "react";

import { api, getFriendlyErrorMessage } from "../api";
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
          setError(getFriendlyErrorMessage(e, "AI status"));
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
          <div className="ai-pop-message">{labelInfo.message}</div>
          <div className="ai-pop-row">
            <span className="ai-pop-key">Status</span>
            <span className="ai-pop-val">{labelInfo.label}</span>
          </div>
          <div className="ai-pop-row">
            <span className="ai-pop-key">Provider</span>
            <span className="ai-pop-val">
              {status?.provider === "disabled"
                ? "Not configured"
                : prettyProvider(status?.provider)}
            </span>
          </div>
          <div className="ai-pop-row">
            <span className="ai-pop-key">Mode</span>
            <span className="ai-pop-val">
              {status?.enabled ? "Rules + OCR + AI" : "Rules + OCR"}
            </span>
          </div>
          <div className="ai-pop-section-title">What AI assist can help with</div>
          <ul className="ai-pop-tasks">
            {AI_HELP_TASKS.map((t) => (
              <li key={t}>{t}</li>
            ))}
          </ul>
          <button
            type="button"
            className="ai-pop-cta"
            disabled
            title="Coming later — for now, configure provider credentials in .env"
          >
            Configure AI
          </button>
          <details className="ai-pop-details">
            <summary>Developer setup</summary>
            <div className="ai-pop-hint">
              Set <code>AI_FALLBACK_ENABLED=true</code>,{" "}
              <code>AI_PROVIDER</code>, and the matching API key in{" "}
              <code>.env</code>. See <code>docs/architecture/</code> for the
              full provider list.
            </div>
          </details>
        </div>
      )}
    </span>
  );
}

type Tone = "tone-loading" | "tone-off" | "tone-configured" | "tone-ready" | "tone-error";

function labelFor(
  status: AiStatus | null,
  error: string | null,
): { label: string; tone: Tone; message: string; hint?: string } {
  // Phase 1L — only call this an "error" when the backend exists and
  // actually reports a runtime/provider failure. A failed fetch (404,
  // network blip) is a deployment/config issue, not a runtime AI
  // failure — show "AI Off" with a friendly message instead.
  if (error) {
    return {
      label: "AI Off",
      tone: "tone-off",
      message:
        "AI assist is currently off. The app is using rules, OCR, YAML, and validation only.",
      hint: "Configure provider credentials in .env to enable AI fallback.",
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
        "AI assist is ready. It will suggest values only when rules and OCR confidence is low. Every AI-filled field is flagged for manual review.",
    };
  }
  if (status.provider === "disabled") {
    return {
      label: "AI Off",
      tone: "tone-off",
      message:
        "AI assist is currently off. The app is using rules, OCR, YAML, and validation only.",
      hint: "Configure provider credentials in .env to enable AI fallback.",
    };
  }
  if (!status.configured) {
    return {
      label: "AI Not Configured",
      tone: "tone-configured",
      message:
        "A provider is selected but no API key is set yet.",
      hint: "Configure provider credentials in .env to enable AI fallback.",
    };
  }
  return {
    label: "AI Off",
    tone: "tone-off",
    message:
      "AI assist is currently off. The app is using rules, OCR, YAML, and validation only.",
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
