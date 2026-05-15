// Phase 2M — Minimal kebab (3-dots) menu used by row actions in the
// Batch Explorer. Replaces the always-visible trash icons with a
// single discreet trigger that reveals destructive + power actions
// only when the operator asks for them.
//
// Usage:
//   <KebabMenu
//     items={[
//       { label: "Process this file", onClick: onProcessFile },
//       { label: "Delete file", onClick: onDelete, tone: "danger" },
//     ]}
//   />
//
// State (open/closed) lives inside the component; positioning is
// fixed-anchored to the trigger button so it never gets clipped by
// the sidebar's overflow.

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

export type KebabItem = {
  label: string;
  onClick: () => void;
  tone?: "default" | "danger";
  icon?: React.ReactNode;
  disabled?: boolean;
  // Optional secondary line, e.g. "Process this file in isolation".
  hint?: string;
  // Hide the item without removing it (handy when one item is only
  // available in some states without re-keying the list).
  hidden?: boolean;
  testId?: string;
};

type Props = {
  items: KebabItem[];
  // Visible accessibility label for the trigger.
  ariaLabel?: string;
  testId?: string;
  // Optional CSS class on the trigger button (so callers can match
  // the row's existing icon-button sizing).
  className?: string;
};

export function KebabMenu({ items, ariaLabel, testId, className }: Props) {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const popoverRef = useRef<HTMLDivElement | null>(null);
  const [popStyle, setPopStyle] = useState<React.CSSProperties | null>(null);

  // Close on outside click + Escape.
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      const t = e.target as Node | null;
      if (
        t &&
        triggerRef.current &&
        !triggerRef.current.contains(t) &&
        popoverRef.current &&
        !popoverRef.current.contains(t)
      ) {
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

  // Position the popover under (or above) the trigger when it opens.
  useLayoutEffect(() => {
    if (!open || !triggerRef.current) return;
    const rect = triggerRef.current.getBoundingClientRect();
    const POP_W = 224;
    const POP_H_MAX = 280;
    const margin = 4;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    let left = rect.right - POP_W;
    if (left < margin) left = margin;
    if (left + POP_W + margin > vw) left = vw - POP_W - margin;
    let top = rect.bottom + 4;
    if (top + POP_H_MAX + margin > vh) {
      const above = rect.top - 4 - POP_H_MAX;
      if (above > margin) top = above;
    }
    setPopStyle({ position: "fixed", left, top, width: POP_W });
  }, [open]);

  const visible = items.filter((it) => !it.hidden);
  if (visible.length === 0) return null;

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        className={`kebab-menu-trigger ${className || ""} ${open ? "is-open" : ""}`}
        onClick={(e) => {
          e.stopPropagation();
          setOpen((v) => !v);
        }}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={ariaLabel || "More actions"}
        title={ariaLabel || "More actions"}
        data-testid={testId}
      >
        {/* Vertical-ellipsis Unicode glyph — immune to any global SVG
            rules in styles.css. Colour comes from the trigger
            (.kebab-menu-trigger sets `color`). */}
        <span
          className="kebab-menu-glyph"
          aria-hidden
          style={{
            display: "inline-block",
            fontSize: 16,
            fontWeight: 700,
            lineHeight: 1,
            letterSpacing: 0,
            transform: "translateY(-1px)",
          }}
        >
          ⋮
        </span>
      </button>
      {open &&
        createPortal(
          // Portal to document.body so the popover escapes any
          // stacking context (the sidebar, panels, etc. all create
          // their own and would clip the menu otherwise).
          <div
            ref={popoverRef}
            className="kebab-menu-popover"
            style={popStyle ?? { position: "fixed", left: -9999, top: -9999 }}
            role="menu"
          >
            {visible.map((it, idx) => (
              <button
                key={`${it.label}-${idx}`}
                type="button"
                className={`kebab-menu-item ${it.tone === "danger" ? "is-danger" : ""}`}
                role="menuitem"
                disabled={it.disabled}
                data-testid={it.testId}
                onMouseDown={(e) => {
                  // mousedown is the FIRST mouse event the browser
                  // fires. Run the action here so it lands before
                  // ANY parent close-on-outside-click logic (or the
                  // portal unmount) can race the click event.
                  e.stopPropagation();
                  e.preventDefault();
                  if (it.disabled) return;
                  try {
                    it.onClick();
                  } catch (err) {
                    // eslint-disable-next-line no-console
                    console.error("[kebab-item] handler threw", err);
                  }
                  setOpen(false);
                }}
                onClick={(e) => {
                  // mousedown already handled the action — this click
                  // handler exists only to swallow the event so the
                  // row underneath doesn't react.
                  e.stopPropagation();
                  e.preventDefault();
                }}
              >
                {it.icon && (
                  <span className="kebab-menu-item-icon" aria-hidden>
                    {it.icon}
                  </span>
                )}
                <span className="kebab-menu-item-body">
                  <span className="kebab-menu-item-label">{it.label}</span>
                  {it.hint && (
                    <span className="kebab-menu-item-hint">{it.hint}</span>
                  )}
                </span>
              </button>
            ))}
          </div>,
          document.body,
        )}
    </>
  );
}
