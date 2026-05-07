// Phase 1V — premium collapsed-panel rail.
//
// Earlier phases shipped colored vertical stripes + bottom-anchored
// chevrons that the operator perceived as anonymous decorative
// shapes. Phase 1V redesigns the rail as a small "mini-panel" that
// matches the visual language of the expanded panel:
//
//   * 40 px wide.
//   * Rounded card with the same border + background as expanded panels.
//   * Header strip at the top with a chevron in the same position the
//     expanded panel's collapse button occupies (so the operator's eye
//     learns one location).
//   * Below the header: a small icon + optional badge count.
//   * No colored vertical stripes, no bottom-floating arrows, no big
//     labels crossing decorative shapes.
//
// The whole rail is the click target, with a tooltip describing the
// expand action.

import type { ReactNode } from "react";

type Props = {
  side: "left" | "right";
  /** Visual identity. Selects the icon + tooltip wording. */
  variant: "files" | "document" | "issues";
  /** Short label used in the tooltip. */
  label: string;
  /** Optional badge count rendered next to the icon. */
  count?: number;
  onExpand: () => void;
};

const ICONS: Record<Props["variant"], ReactNode> = {
  files: <FilesIcon />,
  document: <DocIcon />,
  issues: <AlertIcon />,
};

export function CollapseRail({
  side,
  variant,
  label,
  count,
  onExpand,
}: Props) {
  const tooltip = `Expand ${label.toLowerCase()}`;
  return (
    <button
      type="button"
      className={`collapse-rail collapse-rail-${side} collapse-rail-${variant}`}
      onClick={onExpand}
      title={tooltip}
      aria-label={tooltip}
      aria-expanded={false}
      data-testid={`collapse-rail-${variant}`}
    >
      <span className="collapse-rail-header">
        <span className="collapse-rail-chevron" aria-hidden>
          {side === "left" ? <ChevronRight /> : <ChevronLeft />}
        </span>
      </span>
      <span className="collapse-rail-body">
        <span className="collapse-rail-icon" aria-hidden>
          {ICONS[variant]}
        </span>
        {count != null && count > 0 && (
          <span className="collapse-rail-count" aria-hidden>
            {count > 99 ? "99+" : count}
          </span>
        )}
      </span>
    </button>
  );
}

function FilesIcon() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
    </svg>
  );
}

function DocIcon() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <polyline points="14 2 14 8 20 8" />
    </svg>
  );
}

function AlertIcon() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
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

function ChevronLeft() {
  return (
    <svg
      width="13"
      height="13"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.4"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <polyline points="15 18 9 12 15 6" />
    </svg>
  );
}
function ChevronRight() {
  return (
    <svg
      width="13"
      height="13"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.4"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <polyline points="9 18 15 12 9 6" />
    </svg>
  );
}
