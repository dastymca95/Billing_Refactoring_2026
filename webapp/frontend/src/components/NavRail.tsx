// Minimal text-first workspace sidebar.

export type AppModule = "batches";

type Item = {
  key: AppModule;
  label: string;
};

const ITEMS: Item[] = [
  { key: "batches", label: "Batches" },
];

type Props = {
  active: AppModule;
  onSelect: (module: AppModule) => void;
  collapsed: boolean;
};

export function NavRail({ active, onSelect, collapsed }: Props) {
  if (collapsed) {
    return (
      <nav
        className="nav-rail is-collapsed"
        aria-label="App navigation"
        data-testid="nav-rail"
      />
    );
  }

  return (
    <nav
      className="nav-rail is-expanded"
      aria-label="App navigation"
      data-testid="nav-rail"
    >
      <ul className="nav-rail-list">
        {ITEMS.map((it) => (
          <li key={it.key}>
            <button
              type="button"
              className={`nav-rail-item ${active === it.key ? "active" : ""}`}
              onClick={() => onSelect(it.key)}
              data-testid={`nav-rail-${it.key}`}
            >
              <span className="nav-rail-label">{it.label}</span>
            </button>
          </li>
        ))}
      </ul>
    </nav>
  );
}
