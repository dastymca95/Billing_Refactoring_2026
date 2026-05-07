// Phase 1L — compact workflow status strip.
//
// Replaces the Phase 1J/1K toy 1-2-3-4 numbered circles with a compact
// inline status strip:
//
//    Upload  ·  Process  ·  Review  ·  Export
//      ✓        ✓           ⚠           …
//
// Each step is a tiny label + a status dot (color = state). Active
// step gets a subtle accent halo. The whole strip is informational —
// clicking does not navigate.

type StepStatus = "pending" | "active" | "complete" | "warning";

type Step = {
  key: string;
  label: string;
  status: StepStatus;
  detail?: string;
};

type Props = {
  fileCount: number;
  isProcessing: boolean;
  invoiceCount: number;
  manualReviewCount: number;
  hasExport: boolean;
};

export function WorkflowSteps({
  fileCount,
  isProcessing,
  invoiceCount,
  manualReviewCount,
  hasExport,
}: Props) {
  const upload: Step = {
    key: "upload",
    label: "Upload",
    status: fileCount === 0 ? "active" : "complete",
    detail: fileCount === 0
      ? "Drop files"
      : `${fileCount} file${fileCount === 1 ? "" : "s"}`,
  };
  const process: Step = {
    key: "process",
    label: "Process",
    status: isProcessing
      ? "active"
      : fileCount === 0
        ? "pending"
        : invoiceCount > 0
          ? "complete"
          : "active",
    detail: isProcessing
      ? "Running"
      : invoiceCount > 0
        ? `${invoiceCount} invoice${invoiceCount === 1 ? "" : "s"}`
        : fileCount > 0
          ? "Ready"
          : "—",
  };
  const review: Step = {
    key: "review",
    label: "Review",
    status:
      invoiceCount === 0
        ? "pending"
        : manualReviewCount > 0
          ? "warning"
          : "complete",
    detail:
      invoiceCount === 0
        ? "—"
        : manualReviewCount > 0
          ? `${manualReviewCount} issue${manualReviewCount === 1 ? "" : "s"}`
          : "All clear",
  };
  const exportStep: Step = {
    key: "export",
    label: "Export",
    status: hasExport
      ? "complete"
      : invoiceCount === 0
        ? "pending"
        : "active",
    detail: hasExport ? "Exported" : invoiceCount === 0 ? "—" : "Ready",
  };

  const steps = [upload, process, review, exportStep];

  return (
    <ol className="workflow-strip" aria-label="Workflow">
      {steps.map((s, i) => (
        <li key={s.key} className={`workflow-strip-item status-${s.status}`}>
          <span className={`workflow-strip-dot status-${s.status}`} aria-hidden />
          <span className="workflow-strip-text">
            <span className="workflow-strip-label">{s.label}</span>
            {s.detail && (
              <span className="workflow-strip-detail">· {s.detail}</span>
            )}
          </span>
          {i < steps.length - 1 && (
            <span className="workflow-strip-sep" aria-hidden>
              ›
            </span>
          )}
        </li>
      ))}
    </ol>
  );
}
