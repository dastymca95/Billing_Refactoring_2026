import type { BatchProgress } from "../types";

type Props = {
  progress?: BatchProgress | null;
  currentFilename?: string | null;
  variant?: "status" | "document";
};

const FALLBACK_STAGES = [
  "Scanning invoice",
  "Reading line items",
  "Matching vendor",
  "Mapping GL accounts",
  "Validating totals",
  "Building ResMan template",
];

export function AiScanOverlay({
  progress,
  currentFilename,
  variant = "status",
}: Props) {
  if (!progress || progress.status !== "processing") return null;
  const isAi =
    progress.processing_mode === "ai_assisted" ||
    Boolean(progress.ai_stage) ||
    /ai|scanning invoice|reading line items|mapping gl|validating totals/i.test(
      progress.current_step || "",
    );
  if (!isAi) return null;
  if (
    currentFilename &&
    progress.current_file &&
    progress.current_file !== currentFilename
  ) {
    return null;
  }

  const disabled = progress.ai_enabled === false;
  const stage = disabled
    ? progress.ai_disabled_reason || "AI invoice processing is not configured."
    : progress.ai_stage || progress.current_step || FALLBACK_STAGES[0];
  const pct = Math.max(0, Math.min(100, progress.percent ?? 0));
  const isDocument = variant === "document";

  return (
    <div
      className={`ai-scan-overlay ${
        isDocument ? "is-document" : "is-status"
      }`}
      aria-live={isDocument ? undefined : "polite"}
      aria-hidden={isDocument ? true : undefined}
      data-testid={isDocument ? "ai-scan-document-overlay" : "ai-scan-overlay"}
    >
      <div className="ai-scan-beam" aria-hidden />
      {!isDocument && (
        <div className="ai-scan-card">
          <div className="ai-scan-eyebrow">AI invoice assist</div>
          <div className="ai-scan-title">
            {disabled ? "AI not configured" : stage}
          </div>
          <div className="ai-scan-file" title={progress.current_file || currentFilename || ""}>
            {progress.current_file || currentFilename || "Current document"}
          </div>
          <div className="ai-scan-progress" aria-label={`AI scan progress ${pct.toFixed(0)}%`}>
            <span style={{ width: `${pct}%` }} />
          </div>
          <div className="ai-scan-meta">{pct.toFixed(0)}%</div>
        </div>
      )}
    </div>
  );
}
