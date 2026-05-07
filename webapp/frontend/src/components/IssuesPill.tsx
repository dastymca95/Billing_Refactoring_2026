// Phase 1L — small "Issues N" pill that opens the issues drawer.

type Props = {
  count: number;
  hasErrors: boolean;
  onClick: () => void;
  className?: string;
};

export function IssuesPill({ count, hasErrors, onClick, className }: Props) {
  if (count === 0) {
    return (
      <button
        type="button"
        className={`issues-pill issues-pill-clean ${className ?? ""}`}
        data-testid="issues-pill"
        onClick={onClick}
        title="No issues"
      >
        <CheckIcon />
        <span>No issues</span>
      </button>
    );
  }
  return (
    <button
      type="button"
      className={`issues-pill ${hasErrors ? "issues-pill-error" : "issues-pill-warn"} ${className ?? ""}`}
      data-testid="issues-pill"
      onClick={onClick}
      title="Open issues panel"
    >
      <AlertIcon />
      <span>
        {count} issue{count === 1 ? "" : "s"}
      </span>
    </button>
  );
}

function AlertIcon() {
  return (
    <svg
      width="13"
      height="13"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="10" />
      <line x1="12" y1="8" x2="12" y2="13" />
      <line x1="12" y1="16" x2="12" y2="16" />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg
      width="13"
      height="13"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <polyline points="20 6 9 17 4 12" />
    </svg>
  );
}
