import type { BatchProgress } from "../types";

type Props = {
  progress: BatchProgress | null;
  isProcessing: boolean;
};

export function ProgressBar({ progress, isProcessing }: Props) {
  if (!isProcessing && !progress) return null;
  if (!progress && isProcessing) {
    return (
      <div className="progress-card">
        <div className="progress-step">Starting…</div>
        <div className="progress-bar">
          <div className="progress-bar-fill indeterminate" style={{ width: "30%" }} />
        </div>
      </div>
    );
  }
  if (!progress) return null;

  const status = progress.status ?? "idle";
  const isFinal =
    status === "completed" || status === "failed" || status === "cancelled";
  // Hide the bar a moment after completion — App.tsx still shows the
  // success/error banner so the operator gets feedback.
  if (!isProcessing && isFinal) return null;

  const pct = Math.max(0, Math.min(100, progress.percent ?? 0));
  const tone =
    status === "failed" ? "failed" : status === "completed" ? "completed" : "active";

  const summary: string[] = [];
  if (progress.files_total) {
    summary.push(`${progress.files_done ?? 0}/${progress.files_total} files`);
  }
  if (progress.pages_total) {
    summary.push(`${progress.pages_done ?? 0}/${progress.pages_total} pages`);
  }
  if (progress.invoices_created) {
    summary.push(`${progress.invoices_created} invoices`);
  }
  if (progress.rows_created) {
    summary.push(`${progress.rows_created} rows`);
  }
  if (progress.warnings_count) {
    summary.push(`${progress.warnings_count} flagged`);
  }

  return (
    <div className={`progress-card progress-${tone}`}>
      <div className="progress-step" title={progress.current_step ?? ""}>
        {status === "failed"
          ? `Failed: ${progress.error_message ?? "see logs"}`
          : progress.current_step || "Working…"}
      </div>
      <div className="progress-bar">
        <div
          className={`progress-bar-fill ${tone}`}
          style={{ width: `${pct.toFixed(1)}%` }}
        />
      </div>
      <div className="progress-meta">
        <span className="progress-pct">{pct.toFixed(0)}%</span>
        {summary.length > 0 && (
          <span className="progress-counts">{summary.join(" · ")}</span>
        )}
      </div>
    </div>
  );
}
