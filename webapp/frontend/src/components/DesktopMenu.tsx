import { useEffect, useRef, useState } from "react";

export type DesktopMenuItem =
  | {
      kind?: "item";
      label: string;
      shortcut?: string;
      disabled?: boolean;
      checked?: boolean;
      onSelect?: () => void;
    }
  | { kind: "separator" };

type Props = {
  label: string;
  items: DesktopMenuItem[];
};

export function DesktopMenu({ label, items }: Props) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = (event: MouseEvent) => {
      const node = wrapRef.current;
      if (!node) return;
      if (event.target instanceof Node && !node.contains(event.target)) {
        setOpen(false);
      }
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div className="desktop-menu" ref={wrapRef}>
      <button
        type="button"
        className={`desktop-menu-trigger ${open ? "is-open" : ""}`}
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
      >
        {label}
      </button>
      {open && (
        <div className="desktop-menu-popover" role="menu">
          {items.map((item, index) => {
            if (item.kind === "separator") {
              return <div key={`sep-${index}`} className="desktop-menu-separator" />;
            }
            return (
              <button
                key={`${item.label}-${index}`}
                type="button"
                role={item.checked !== undefined ? "menuitemcheckbox" : "menuitem"}
                aria-checked={item.checked}
                className={`desktop-menu-item ${item.checked ? "is-checked" : ""}`}
                disabled={item.disabled}
                onClick={() => {
                  if (item.disabled) return;
                  item.onSelect?.();
                  setOpen(false);
                }}
              >
                <span className="desktop-menu-check" aria-hidden>
                  {item.checked ? "*" : ""}
                </span>
                <span className="desktop-menu-label">{item.label}</span>
                {item.shortcut && (
                  <span className="desktop-menu-shortcut">{item.shortcut}</span>
                )}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
