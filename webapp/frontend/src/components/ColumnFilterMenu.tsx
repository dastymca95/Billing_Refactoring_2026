// Phase 2M — Excel-style per-column filter popover.
//
// Opens against a column header's funnel button. Lists every distinct
// value found in the column and lets the operator pick a subset to
// keep visible. "Select all" toggles the whole list at once. The
// search box narrows the list when a column has many values.
//
// State lives in the parent (TemplateWorkspace): the menu is purely
// presentational + emits onApply / onClear callbacks.

import { useEffect, useMemo, useRef, useState } from "react";

type Props = {
  column: string;
  // Anchor coordinates (viewport pixels) of the funnel button so the
  // popover can position itself directly under the header.
  anchorRect: DOMRect;
  // All distinct values that exist in this column across the visible
  // dataset. Order is preserved by the parent (typically natural row
  // order, then sort-on-display).
  allValues: string[];
  // Currently allowed values. When ``null`` no filter is active —
  // every value is allowed by default.
  selected: string[] | null;
  onApply: (next: string[] | null) => void;
  onClose: () => void;
};

export function ColumnFilterMenu({
  column,
  anchorRect,
  allValues,
  selected,
  onApply,
  onClose,
}: Props) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [search, setSearch] = useState("");
  // Local working copy so the user can toggle without instantly
  // rerendering the parent grid on every checkbox click.
  const initial = useMemo<Set<string>>(() => {
    if (selected == null) return new Set(allValues);
    return new Set(selected);
  }, [selected, allValues]);
  const [picked, setPicked] = useState<Set<string>>(initial);

  // Re-initialise when the column / dataset changes.
  useEffect(() => {
    setPicked(initial);
  }, [initial]);

  // Close on outside click + Escape.
  useEffect(() => {
    const onDoc = (e: MouseEvent) => {
      const node = ref.current;
      if (!node) return;
      if (e.target instanceof Node && !node.contains(e.target)) onClose();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    // Defer attach so the click that opened the menu doesn't
    // immediately close it.
    const t = window.setTimeout(() => {
      document.addEventListener("mousedown", onDoc);
      document.addEventListener("keydown", onKey);
    }, 0);
    return () => {
      window.clearTimeout(t);
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [onClose]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return allValues;
    return allValues.filter((v) => v.toLowerCase().includes(q));
  }, [allValues, search]);

  const allFilteredPicked = filtered.length > 0 && filtered.every((v) => picked.has(v));
  const noneFilteredPicked = filtered.every((v) => !picked.has(v));

  const toggle = (v: string) => {
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(v)) next.delete(v);
      else next.add(v);
      return next;
    });
  };

  const toggleAll = () => {
    setPicked((prev) => {
      const next = new Set(prev);
      if (allFilteredPicked) {
        // Uncheck everything that's currently visible in the search.
        filtered.forEach((v) => next.delete(v));
      } else {
        filtered.forEach((v) => next.add(v));
      }
      return next;
    });
  };

  const apply = () => {
    if (picked.size === allValues.length) {
      // Equivalent to "no filter" — clear it.
      onApply(null);
    } else {
      onApply(Array.from(picked));
    }
    onClose();
  };

  const clear = () => {
    onApply(null);
    onClose();
  };

  // Position. Default: under the funnel button. Clamp into viewport.
  const style = computePopoverStyle(anchorRect);

  return (
    <div ref={ref} className="column-filter-menu" style={style} role="dialog" aria-label={`Filter ${column}`}>
      <div className="column-filter-menu-header">
        <span className="column-filter-menu-title">Filter · {column}</span>
        <button
          type="button"
          className="column-filter-menu-close"
          onClick={onClose}
          title="Close filter"
          aria-label="Close filter"
        >
          ✕
        </button>
      </div>
      <input
        type="search"
        className="column-filter-menu-search"
        placeholder="Search values…"
        value={search}
        onChange={(e) => setSearch(e.target.value)}
        autoFocus
      />
      <label className="column-filter-menu-toggleall">
        <input
          type="checkbox"
          checked={allFilteredPicked}
          ref={(el) => {
            if (el) el.indeterminate = !allFilteredPicked && !noneFilteredPicked;
          }}
          onChange={toggleAll}
        />
        <span>(Select all{search ? " visible" : ""})</span>
      </label>
      <div className="column-filter-menu-list">
        {filtered.length === 0 && (
          <div className="column-filter-menu-empty">No matches</div>
        )}
        {filtered.map((v) => (
          <label key={v} className="column-filter-menu-row">
            <input
              type="checkbox"
              checked={picked.has(v)}
              onChange={() => toggle(v)}
            />
            <span className="column-filter-menu-row-value" title={v}>
              {v === "" ? <em className="column-filter-menu-empty-tag">(blank)</em> : v}
            </span>
          </label>
        ))}
      </div>
      <div className="column-filter-menu-actions">
        <button
          type="button"
          className="btn btn-compact"
          onClick={clear}
          title="Remove this column's filter"
        >
          Clear
        </button>
        <button
          type="button"
          className="btn btn-compact btn-accent"
          onClick={apply}
          disabled={picked.size === 0}
        >
          Apply
        </button>
      </div>
    </div>
  );
}

function computePopoverStyle(anchor: DOMRect): React.CSSProperties {
  const POPOVER_W = 260;
  const POPOVER_H_MAX = 360;
  const margin = 6;
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  let left = anchor.left;
  if (left + POPOVER_W + margin > vw) left = vw - POPOVER_W - margin;
  if (left < margin) left = margin;
  let top = anchor.bottom + 4;
  if (top + POPOVER_H_MAX + margin > vh) {
    // Flip above if there's no room below.
    const above = anchor.top - 4 - POPOVER_H_MAX;
    if (above > margin) top = above;
  }
  return { position: "fixed", left, top, width: POPOVER_W };
}
