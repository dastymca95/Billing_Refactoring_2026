// Minimal text-first workspace sidebar.

export type AppModule =
  | "batches"
  | "billing-v2"
  | "accounting-rules"
  | "context-intelligence"
  | "resman-vendors"
  | "resman-properties"
  | "resman-gl"
  | "resman-invoices"
  | "resman-ledger";

type Item = {
  key: AppModule;
  label: string;
};

const ITEMS: Item[] = [
  { key: "billing-v2", label: "Billing V2" },
  { key: "accounting-rules", label: "Accounting Rules" },
  { key: "context-intelligence", label: "Context Matrix" },
  { key: "resman-vendors", label: "Vendors" },
  { key: "resman-properties", label: "Properties & Units" },
  { key: "resman-gl", label: "Chart of Accounts" },
  { key: "resman-invoices", label: "Invoice History" },
  { key: "resman-ledger", label: "General Ledger" },
  { key: "batches", label: "Legacy Billing" },
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
              aria-label={it.label}
              title={it.label}
              data-testid={`nav-rail-${it.key}`}
            >
              <span className="nav-rail-icon" aria-hidden>
                <ModuleIcon module={it.key} />
              </span>
              <span className="nav-rail-label">{it.label}</span>
            </button>
          </li>
        ))}
      </ul>
    </nav>
  );
}

function ModuleIcon({ module }: { module: AppModule }) {
  if (module === "billing-v2") return <BillingV2Icon />;
  if (module === "accounting-rules") return <RulesIcon />;
  if (module === "context-intelligence") return <IntelligenceIcon />;
  if (module === "resman-vendors") return <DataIcon kind="vendor" />;
  if (module === "resman-properties") return <DataIcon kind="property" />;
  if (module === "resman-gl") return <DataIcon kind="gl" />;
  if (module === "resman-invoices") return <DataIcon kind="invoice" />;
  if (module === "resman-ledger") return <DataIcon kind="ledger" />;
  return <BatchesIcon />;
}

function IntelligenceIcon() {
  return <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><circle cx="6" cy="6" r="2"/><circle cx="18" cy="6" r="2"/><circle cx="12" cy="18" r="2"/><path d="m8 7 3 8m5-8-3 8M8 6h8"/></svg>;
}

function DataIcon({ kind }: { kind: "vendor" | "property" | "gl" | "invoice" | "ledger" }) {
  if (kind === "property") return <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="m3 10 9-7 9 7"/><path d="M5 9v11h14V9M9 20v-6h6v6"/></svg>;
  if (kind === "gl") return <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M4 4h16v16H4zM4 9h16M9 4v16"/><path d="M12 13h5M12 16h3"/></svg>;
  if (kind === "invoice") return <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M6 3h12v18l-3-2-3 2-3-2-3 2z"/><path d="M9 8h6M9 12h6M9 16h4"/></svg>;
  if (kind === "ledger") return <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M5 3h14v18H5zM8 7h8M8 11h8M8 15h4"/></svg>;
  return <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M4 20v-2a4 4 0 0 1 4-4h8a4 4 0 0 1 4 4v2M12 10a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z"/></svg>;
}

function RulesIcon() {
  return <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M6 4h12v16H6z" />
    <path d="m9 9 1.2 1.2L13 7.5M9 15h6" />
  </svg>;
}

function BillingV2Icon() {
  return (
    <svg
      width="17"
      height="17"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.9"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M5 3h11l3 3v15H5z" />
      <path d="M16 3v4h4" />
      <path d="M8 10h8" />
      <path d="M8 14h8" />
      <path d="M8 18h5" />
    </svg>
  );
}

function BatchesIcon() {
  return (
    <svg
      width="17"
      height="17"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.9"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M4 6.5h6l1.8 2H20a1.5 1.5 0 0 1 1.5 1.5v6.8a1.7 1.7 0 0 1-1.7 1.7H4.2a1.7 1.7 0 0 1-1.7-1.7V8.2A1.7 1.7 0 0 1 4.2 6.5Z" />
      <path d="M5.7 12h12.6" />
      <path d="M5.7 15h8.4" />
    </svg>
  );
}
