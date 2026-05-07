// Phase 2M — Grouped totals view for the template grid.
//
// When the operator picks a "Group by" column the normal row-by-row
// table is replaced with this: one row per distinct value in that
// column, plus the SUM of every numeric column (today only "Amount"
// is summed; everything else collapses to a count or blank).
//
// Click a group row to filter the bill viewer the same way clicking a
// detail row would — the parent receives the first underlying row's
// index for that group.

import { useMemo } from "react";

import type { PreviewResponse, PreviewRow } from "../types";

type Props = {
  preview: PreviewResponse;
  visibleRowIndexes: Set<number> | null;
  groupBy: string;
  onSelectGroupRow?: (rowIndex: number) => void;
  selectedRowIndex?: number | null;
};

type GroupBucket = {
  key: string;       // raw key used for sort/grouping (already lowercased)
  display: string;   // value for the operator (preserves case)
  rowIndexes: number[];
  total: number;     // sum of Amount column
  count: number;
};

function groupKey(value: unknown): { key: string; display: string } {
  if (value == null || value === "") return { key: "__blank__", display: "(blank)" };
  const s = String(value);
  return { key: s.trim().toLowerCase(), display: s };
}

function asNumber(v: unknown): number {
  if (typeof v === "number") return v;
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

export function GroupedTotalsTable({
  preview,
  visibleRowIndexes,
  groupBy,
  onSelectGroupRow,
  selectedRowIndex,
}: Props) {
  const buckets = useMemo<GroupBucket[]>(() => {
    const map = new Map<string, GroupBucket>();
    preview.rows.forEach((row: PreviewRow, idx: number) => {
      if (visibleRowIndexes && !visibleRowIndexes.has(idx)) return;
      const { key, display } = groupKey((row as Record<string, unknown>)[groupBy]);
      let bucket = map.get(key);
      if (!bucket) {
        bucket = { key, display, rowIndexes: [], total: 0, count: 0 };
        map.set(key, bucket);
      }
      bucket.rowIndexes.push(idx);
      bucket.total += asNumber((row as Record<string, unknown>)["Amount"]);
      bucket.count += 1;
    });
    return Array.from(map.values()).sort((a, b) => b.total - a.total);
  }, [preview, visibleRowIndexes, groupBy]);

  const grandTotal = useMemo(
    () => buckets.reduce((acc, b) => acc + b.total, 0),
    [buckets],
  );
  const grandCount = useMemo(
    () => buckets.reduce((acc, b) => acc + b.count, 0),
    [buckets],
  );

  return (
    <div className="card template-grid-card grouped-totals-card" data-testid="grouped-totals-card">
      <div className="card-body tight preview-pane grouped-totals-pane">
        <table className="data-table grouped-totals-table">
          <thead>
            <tr>
              <th className="col-required">{groupBy}</th>
              <th className="col-recommended num">Rows</th>
              <th className="col-required num">Total Amount</th>
            </tr>
          </thead>
          <tbody>
            {buckets.length === 0 && (
              <tr>
                <td colSpan={3} className="grouped-totals-empty">
                  No rows match the current filters.
                </td>
              </tr>
            )}
            {buckets.map((b) => {
              const firstIdx = b.rowIndexes[0];
              const isSelected = selectedRowIndex != null
                && b.rowIndexes.includes(selectedRowIndex);
              return (
                <tr
                  key={b.key}
                  className={isSelected ? "selected-row" : ""}
                  onClick={() => {
                    if (onSelectGroupRow && firstIdx != null) {
                      onSelectGroupRow(firstIdx);
                    }
                  }}
                  data-testid="grouped-row"
                  style={{ cursor: onSelectGroupRow ? "pointer" : "default" }}
                >
                  <td>{b.display}</td>
                  <td className="num">{b.count}</td>
                  <td className="num">{formatAmount(b.total)}</td>
                </tr>
              );
            })}
          </tbody>
          {buckets.length > 0 && (
            <tfoot>
              <tr className="grouped-totals-footer">
                <td>Grand total ({buckets.length} group{buckets.length === 1 ? "" : "s"})</td>
                <td className="num">{grandCount}</td>
                <td className="num">{formatAmount(grandTotal)}</td>
              </tr>
            </tfoot>
          )}
        </table>
      </div>
    </div>
  );
}

function formatAmount(n: number): string {
  return n.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}
