// Phase 1J — compact workflow indicator: Upload → Process → Review → Export.
//
// Lives in the topbar. Each step shows pending / active / complete /
// warning state with a one-line subtitle (e.g. "12 invoices",
// "3 issues"). The step is purely a status indicator — clicking does
// not navigate. The active step is the first one with `pending` /
// `active` state.

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
    status:
      fileCount === 0 ? "active" : "complete",
    detail: fileCount === 0 ? "Drop files" : `${fileCount} file${fileCount === 1 ? "" : "s"}`,
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
      ? "Running…"
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
    <ol className="workflow-steps" aria-label="Workflow">
      {steps.map((s, i) => (
        <li key={s.key} className={`workflow-step status-${s.status}`}>
          <span className="workflow-step-num">{i + 1}</span>
          <div className="workflow-step-text">
            <span className="workflow-step-label">{s.label}</span>
            {s.detail && (
              <span className="workflow-step-detail">{s.detail}</span>
            )}
          </div>
          {i < steps.length - 1 && <span className="workflow-step-sep" aria-hidden />}
        </li>
      ))}
    </ol>
  );
}
