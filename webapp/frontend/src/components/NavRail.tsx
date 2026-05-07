// Slim left nav rail.
//
// Phase 1Z: now also routes between top-level modules. Currently:
//   * Batches — the original batch workspace
//   * Rules   — the Vendor Rules Studio (Phase 1Z)
//
// Other former icons (Review / Vendors / Exports / Settings) used to be
// disabled placeholders; they are now removed entirely so the rail only
// shows what's actually wired up.

import type { ReactNode } from "react";

export type AppModule = "batches" | "rules";

type Item = {
  key: AppModule;
  label: string;
  icon: ReactNode;
};

const ITEMS: Item[] = [
  { key: "batches", label: "Batches", icon: <BatchesIcon /> },
  { key: "rules", label: "Rules", icon: <RulesIcon /> },
];

type Props = {
  active: AppModule;
  onSelect: (module: AppModule) => void;
};

export function NavRail({ active, onSelect }: Props) {
  return (
    <nav className="nav-rail" aria-label="App navigation" data-testid="nav-rail">
      <div className="nav-rail-brand" aria-hidden title="Billing Refactoring">
        <BillIcon />
      </div>
      <ul className="nav-rail-list">
        {ITEMS.map((it) => (
          <li
            key={it.key}
            className={`nav-rail-item ${active === it.key ? "active" : ""}`}
            title={it.label}
            onClick={() => onSelect(it.key)}
            role="button"
            tabIndex={0}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") onSelect(it.key);
            }}
            data-testid={`nav-rail-${it.key}`}
          >
            <span className="nav-rail-icon" aria-hidden>
              {it.icon}
            </span>
            <span className="nav-rail-label">{it.label}</span>
          </li>
        ))}
      </ul>
    </nav>
  );
}

function BillIcon() {
  return (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <polyline points="14 2 14 8 20 8" />
      <line x1="8" y1="13" x2="16" y2="13" />
      <line x1="8" y1="17" x2="13" y2="17" />
    </svg>
  );
}

function BatchesIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="3" y="3" width="7" height="7" rx="1.5" />
      <rect x="14" y="3" width="7" height="7" rx="1.5" />
      <rect x="3" y="14" width="7" height="7" rx="1.5" />
      <rect x="14" y="14" width="7" height="7" rx="1.5" />
    </svg>
  );
}

function RulesIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M9 11l3 3L22 4" />
      <path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" />
    </svg>
  );
}
