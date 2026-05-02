// Phase 1J — slim left-edge navigation rail.
//
// Only "Batches" is wired up in this phase. Other items are placeholders
// with a "Soon" pill so they look intentional without lying about what
// works.

type RailItem = {
  key: string;
  label: string;
  icon: string;
  active?: boolean;
  enabled?: boolean;
  hint?: string;
};

const ITEMS: RailItem[] = [
  { key: "batches", label: "Batches", icon: "📦", active: true, enabled: true },
  { key: "review", label: "Review", icon: "✅", enabled: false, hint: "Coming soon — currently part of the active batch." },
  { key: "vendors", label: "Vendor Rules", icon: "📐", enabled: false, hint: "Coming soon — edit YAML rules from the UI." },
  { key: "exports", label: "Exports", icon: "↓", enabled: false, hint: "Coming soon — full export history." },
  { key: "settings", label: "Settings", icon: "⚙", enabled: false, hint: "Coming soon — AI / Dropbox config." },
];

export function NavRail() {
  return (
    <nav className="nav-rail" aria-label="App navigation">
      <div className="nav-rail-brand" aria-hidden>
        BR
      </div>
      <ul className="nav-rail-list">
        {ITEMS.map((it) => (
          <li
            key={it.key}
            className={`nav-rail-item ${it.active ? "active" : ""} ${it.enabled ? "" : "disabled"}`}
            title={it.hint || it.label}
          >
            <span className="nav-rail-icon" aria-hidden>
              {it.icon}
            </span>
            <span className="nav-rail-label">{it.label}</span>
            {!it.enabled && (
              <span className="nav-rail-soon" aria-label="Coming soon">
                Soon
              </span>
            )}
          </li>
        ))}
      </ul>
    </nav>
  );
}
