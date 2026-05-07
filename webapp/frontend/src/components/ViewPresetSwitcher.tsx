// Phase 1K — workspace view-preset switcher.
//
// Three presets:
//   "review"   — document visible, template large, inspector visible. Default.
//   "template" — document collapsed, inspector collapsed, template fills.
//   "document" — document large, template smaller, inspector visible.
//
// The actual collapse / size effects are applied in App.tsx via the
// existing collapse states + resize hook. This component is just the
// segmented control + persistence helper.

import { useEffect } from "react";

export type ViewPreset = "review" | "template" | "document";

const STORAGE_KEY = "billing_refactoring_layout_view_preset";

export function loadStoredPreset(): ViewPreset {
  try {
    const v = window.localStorage.getItem(STORAGE_KEY);
    if (v === "review" || v === "template" || v === "document") return v;
  } catch {
    /* localStorage may be disabled */
  }
  return "review";
}

export function persistPreset(p: ViewPreset): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, p);
  } catch {
    /* non-fatal */
  }
}

const OPTIONS: { key: ViewPreset; label: string; title: string }[] = [
  { key: "review",   label: "Review",          title: "Balanced — document and template both visible" },
  { key: "template", label: "Template focus",  title: "Hide document — template fills the screen" },
  { key: "document", label: "Document focus",  title: "Wider document workspace for marking" },
];

type Props = {
  value: ViewPreset;
  onChange: (next: ViewPreset) => void;
  className?: string;
};

export function ViewPresetSwitcher({ value, onChange, className }: Props) {
  // Persist whenever the preset changes.
  useEffect(() => {
    persistPreset(value);
  }, [value]);

  return (
    <div
      className={`view-preset-switch ${className ?? ""}`}
      role="tablist"
      aria-label="Workspace layout"
    >
      {OPTIONS.map((o) => (
        <button
          key={o.key}
          role="tab"
          aria-selected={value === o.key}
          className={`view-preset-switch-btn ${value === o.key ? "active" : ""}`}
          onClick={() => onChange(o.key)}
          title={o.title}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}
