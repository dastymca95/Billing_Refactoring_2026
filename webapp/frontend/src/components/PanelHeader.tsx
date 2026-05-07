// Phase 2D — Module window controls (— □ ×).
//
// Used by every major panel (Batches / Document / Template) so the
// workspace feels like a desktop app: each module has its own minimal
// chrome with minimize, maximize, and close affordances. Geometry is
// owned by the parent (App.tsx); this component is purely presentational
// + dispatches callbacks.

import type { ReactNode } from "react";

type Props = {
  title: ReactNode;
  subtitle?: ReactNode;
  // Optional left-side content (icon, breadcrumb, …)
  leading?: ReactNode;
  // Optional right-side actions before the window controls.
  trailing?: ReactNode;
  // Window control callbacks. Pass undefined to hide the corresponding button.
  onMinimize?: () => void;
  onMaximize?: () => void;
  onClose?: () => void;
  // Visual state. ``isMaximized`` flips the maximize icon to "restore".
  isMinimized?: boolean;
  isMaximized?: boolean;
  // Test hook.
  testId?: string;
};

export function PanelHeader({
  title,
  subtitle,
  leading,
  trailing,
  onMinimize,
  onMaximize,
  onClose,
  isMinimized,
  isMaximized,
  testId,
}: Props) {
  return (
    <div className="panel-header" data-testid={testId}>
      {leading && <div className="panel-header-leading">{leading}</div>}
      <div className="panel-header-titles">
        <div className="panel-header-title">{title}</div>
        {subtitle && <div className="panel-header-subtitle">{subtitle}</div>}
      </div>
      {trailing && <div className="panel-header-trailing">{trailing}</div>}
      <div className="panel-window-controls">
        {onMinimize && (
          <button
            type="button"
            className="panel-window-btn"
            onClick={onMinimize}
            title={isMinimized ? "Restore" : "Minimize"}
            aria-label={isMinimized ? "Restore" : "Minimize"}
            data-testid="panel-minimize"
          >
            <MinimizeIcon />
          </button>
        )}
        {onMaximize && (
          <button
            type="button"
            className={`panel-window-btn ${isMaximized ? "is-active" : ""}`}
            onClick={onMaximize}
            title={isMaximized ? "Exit fullscreen" : "Maximize"}
            aria-label={isMaximized ? "Exit fullscreen" : "Maximize"}
            aria-pressed={isMaximized || false}
            data-testid="panel-maximize"
          >
            {isMaximized ? <RestoreIcon /> : <MaximizeIcon />}
          </button>
        )}
        {onClose && (
          <button
            type="button"
            className="panel-window-btn panel-window-btn-close"
            onClick={onClose}
            title="Close panel"
            aria-label="Close panel"
            data-testid="panel-close"
          >
            <CloseIcon />
          </button>
        )}
      </div>
    </div>
  );
}

function MinimizeIcon() {
  return (
    <svg width="11" height="11" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" aria-hidden="true">
      <line x1="2.5" y1="9" x2="9.5" y2="9" />
    </svg>
  );
}
function MaximizeIcon() {
  return (
    <svg width="11" height="11" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="2.5" y="2.5" width="7" height="7" rx="0.6" />
    </svg>
  );
}
function RestoreIcon() {
  return (
    <svg width="11" height="11" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="3.5" y="2" width="6.5" height="6.5" rx="0.5" />
      <rect x="2" y="3.5" width="6.5" height="6.5" rx="0.5" />
    </svg>
  );
}
function CloseIcon() {
  return (
    <svg width="11" height="11" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" aria-hidden="true">
      <line x1="3" y1="3" x2="9" y2="9" />
      <line x1="9" y1="3" x2="3" y2="9" />
    </svg>
  );
}
