// Phase 2F — topbar Windows menu for the desktop-style workspace shell.
//
// The menu shows each major panel and whether it is visible, minimized
// into the bottom dock, or closed. Visible panels can be closed from
// here; docked/closed panels can be restored from here.

import { useEffect, useRef, useState } from "react";

type PanelKey = "batches" | "document" | "template";

const ITEMS: { key: PanelKey; label: string }[] = [
  { key: "batches", label: "Batches" },
  { key: "document", label: "Document Viewer" },
  { key: "template", label: "Template" },
];

type Props = {
  closedPanels: Set<PanelKey>;
  minimizedPanels: Set<PanelKey>;
  onRestorePanel: (key: PanelKey) => void;
  onClosePanel: (key: PanelKey) => void;
  onRestoreAll: () => void;
  onMinimizeAll?: () => void;
};

export function WindowsMenu({
  closedPanels,
  minimizedPanels,
  onRestorePanel,
  onClosePanel,
  onRestoreAll,
  onMinimizeAll,
}: Props) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      const node = wrapRef.current;
      if (!node) return;
      if (e.target instanceof Node && !node.contains(e.target)) {
        setOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const allOpen = closedPanels.size === 0 && minimizedPanels.size === 0;
  const allMinimized = minimizedPanels.size === ITEMS.length;

  return (
    <div className="windows-menu" ref={wrapRef}>
      <button
        type="button"
        className={`windows-menu-toggle ${open ? "is-open" : ""}`}
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
        title="Show, hide, or restore workspace panels"
        data-testid="windows-menu-toggle"
      >
        <PanelsIcon />
        <span>Windows</span>
        <span className="windows-menu-caret" aria-hidden>
          ▾
        </span>
      </button>
      {open && (
        <div
          className="windows-menu-popover"
          role="menu"
          data-testid="windows-menu-popover"
        >
          <ul className="windows-menu-list">
            {ITEMS.map((it) => {
              const state = closedPanels.has(it.key)
                ? "closed"
                : minimizedPanels.has(it.key)
                ? "minimized"
                : "visible";
              const visible = state === "visible";
              return (
                <li key={it.key}>
                  <button
                    type="button"
                    role="menuitemcheckbox"
                    aria-checked={visible}
                    className={`windows-menu-item is-${state}`}
                    onClick={() => {
                      if (visible) onClosePanel(it.key);
                      else onRestorePanel(it.key);
                      setOpen(false);
                    }}
                    data-testid={`windows-menu-${it.key}`}
                  >
                    <span className="windows-menu-check" aria-hidden>
                      {visible ? <CheckIcon /> : state === "minimized" ? <DockIcon /> : null}
                    </span>
                    <span className="windows-menu-label">{it.label}</span>
                    <span className="windows-menu-state">
                      {visible ? "Visible" : state === "minimized" ? "Docked" : "Closed"}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
          <div className="windows-menu-footer">
            {onMinimizeAll && (
              <button
                type="button"
                className="windows-menu-restore-all"
                onClick={() => {
                  onMinimizeAll();
                  setOpen(false);
                }}
                disabled={allMinimized}
                title="Minimize every workspace panel to the dock"
                data-testid="windows-menu-minimize-all"
              >
                Minimize all
              </button>
            )}
            <button
              type="button"
              className="windows-menu-restore-all"
              onClick={() => {
                onRestoreAll();
                setOpen(false);
              }}
              disabled={allOpen}
              title={allOpen ? "All panels are visible" : "Restore every workspace panel"}
              data-testid="windows-menu-restore-all"
            >
              Restore all
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function PanelsIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="3" y="3" width="7" height="7" rx="1" />
      <rect x="14" y="3" width="7" height="7" rx="1" />
      <rect x="3" y="14" width="7" height="7" rx="1" />
      <rect x="14" y="14" width="7" height="7" rx="1" />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <polyline points="20 6 9 17 4 12" />
    </svg>
  );
}

function DockIcon() {
  return (
    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M5 19h14" />
      <path d="M7 15h10" />
    </svg>
  );
}
