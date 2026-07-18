import type {
  ProcessingRouteDecision,
  ProcessingRouteMode,
  ProcessingRouteSnapshot,
} from "../types";

type Props = {
  snapshot: ProcessingRouteSnapshot | null;
  filename: string | null;
  busy?: boolean;
  error?: string | null;
  onSetDocument: (mode: ProcessingRouteMode | null) => void | Promise<void>;
  onApplyBatch: (mode: ProcessingRouteMode) => void | Promise<void>;
  onRefresh?: () => void | Promise<void>;
};

const OPTIONS: Array<{
  mode: ProcessingRouteMode;
  label: string;
  description: string;
}> = [
  {
    mode: "auto_cost_safe",
    label: "Auto · cost-safe",
    description: "Registered parsers stay deterministic; only unknown documents may use AI.",
  },
  {
    mode: "deterministic_only",
    label: "Deterministic only",
    description: "Never call AI. A missing or failed parser becomes an explicit review blocker.",
  },
  {
    mode: "ai_fallback_allowed",
    label: "Allow AI fallback",
    description: "Run the deterministic parser first; AI is permitted only if it cannot produce a result.",
  },
];

export function ProcessingRouteControl({
  snapshot,
  filename,
  busy = false,
  error,
  onSetDocument,
  onApplyBatch,
  onRefresh,
}: Props) {
  const document = snapshot?.documents.find(
    (item) => item.filename.toLocaleLowerCase() === (filename || "").toLocaleLowerCase(),
  );
  const decision = document?.decision ?? null;
  const badge = routeBadge(decision);

  return (
    <details className="processing-route-control" data-testid="processing-route-control">
      <summary
        className={`processing-route-summary route-${decision?.effective_route || "loading"}`}
        title={decision ? explainReason(decision) : "Loading processing route"}
      >
        <span className="processing-route-shield" aria-hidden>◆</span>
        <span>{badge}</span>
        {busy && <span className="processing-route-spinner" aria-label="Saving route" />}
      </summary>
      <div className="processing-route-popover">
        <header>
          <div>
            <strong>Processing route</strong>
            <p>The backend controls whether a paid AI request is authorized.</p>
          </div>
          {onRefresh && (
            <button type="button" onClick={() => void onRefresh()} disabled={busy}>
              Refresh
            </button>
          )}
        </header>

        {error && <p className="processing-route-error">{error}</p>}

        <section>
          <h4>This document</h4>
          <p className="processing-route-current">
            {filename || "No document selected"}
            {decision?.vendor_key ? ` · ${humanize(decision.vendor_key)}` : ""}
          </p>
          {decision && (
            <p className="processing-route-explanation">{explainReason(decision)}</p>
          )}
          <div className="processing-route-options">
            <button
              type="button"
              disabled={busy || !filename}
              className={decision?.inherited_from !== "document" && decision?.inherited_from !== "page" ? "active" : ""}
              onClick={() => void onSetDocument(null)}
            >
              <strong>Use batch default</strong>
              <span>Remove this document's override.</span>
            </button>
            {OPTIONS.map((option) => (
              <button
                type="button"
                key={option.mode}
                disabled={busy || !filename}
                className={
                  decision?.inherited_from === "document" &&
                  decision.requested_mode === option.mode
                    ? "active"
                    : ""
                }
                onClick={() => void onSetDocument(option.mode)}
              >
                <strong>{option.label}</strong>
                <span>{option.description}</span>
              </button>
            ))}
          </div>
        </section>

        <section className="processing-route-bulk">
          <h4>Apply to every document and page</h4>
          <p>This replaces individual exceptions. Use AI permission only when you intend to allow provider cost.</p>
          <div className="processing-route-bulk-actions">
            {OPTIONS.map((option) => (
              <button
                type="button"
                key={option.mode}
                disabled={busy || !snapshot}
                className={
                  snapshot?.batch.resolution.requested_mode === option.mode &&
                  snapshot.batch.resolution.inherited_from === "batch"
                    ? "active"
                    : ""
                }
                onClick={() => {
                  const label = option.label.toLocaleLowerCase();
                  if (window.confirm(`Apply ${label} to the whole batch and clear page/document exceptions?`)) {
                    void onApplyBatch(option.mode);
                  }
                }}
              >
                {option.label}
              </button>
            ))}
          </div>
        </section>
      </div>
    </details>
  );
}

export function findPageRouteDecision(
  snapshot: ProcessingRouteSnapshot | null | undefined,
  filename: string,
  page: number,
): ProcessingRouteDecision | null {
  const pageDecision = snapshot?.pages.find(
    (item) =>
      item.filename.toLocaleLowerCase() === filename.toLocaleLowerCase() &&
      item.page === page,
  )?.decision;
  if (pageDecision) return pageDecision;
  return snapshot?.documents.find(
    (item) => item.filename.toLocaleLowerCase() === filename.toLocaleLowerCase(),
  )?.decision ?? null;
}

export function shortRouteBadge(decision: ProcessingRouteDecision | null): string {
  if (!decision) return "Auto";
  if (decision.effective_route === "blocked") return "Blocked";
  if (decision.effective_route === "ai") return "AI";
  return decision.ai_fallback_authorized ? "D→AI" : "D";
}

function routeBadge(decision: ProcessingRouteDecision | null): string {
  if (!decision) return "Routing…";
  if (decision.effective_route === "blocked") return "Deterministic unavailable · AI blocked";
  if (decision.effective_route === "ai") return "AI route · no parser found";
  if (decision.ai_fallback_authorized) return "Deterministic first · AI allowed";
  return "Deterministic locked · no AI cost";
}

function explainReason(decision: ProcessingRouteDecision): string {
  switch (decision.reason_code) {
    case "operator_locked_deterministic":
      return "You locked this scope to its registered deterministic processor. AI cannot run.";
    case "deterministic_processor_unavailable":
      return "No registered deterministic processor was found, so this scope is blocked instead of silently using AI.";
    case "deterministic_first_ai_fallback_authorized":
      return "The registered parser runs first. AI may run only if that parser produces no safe disposition.";
    case "ai_authorized_no_deterministic_processor":
    case "cost_safe_ai_no_deterministic_processor":
      return "No deterministic processor matched this document, so the universal AI route is eligible.";
    default:
      return "A registered deterministic processor matched this document. Cost-safe mode prohibits a silent AI fallback.";
  }
}

function humanize(value: string): string {
  return value.replace(/_/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}
