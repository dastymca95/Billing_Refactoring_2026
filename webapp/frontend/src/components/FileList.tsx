// Phase 1K — refined file cards.
//
// Each row shows a filename, file size, a file-type badge (PDF/CSV/...)
// with a soft tone, and a small vendor badge. The selected row gets a
// subtle accent strip. Empty state is a clean centered message.

import type { FileEntry } from "../types";

type Props = {
  files: FileEntry[];
  selected: string | null;
  onSelect: (filename: string) => void;
  // Phase 1U — show skeleton rows while a batch switch is in flight.
  isSwitchingBatch?: boolean;
  expectedFileCount?: number;
};

function formatSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function vendorBadge(f: FileEntry) {
  if (f.vendor_key === "unknown")
    return { className: "badge gray", text: "needs review" };
  if (!f.supported_in_phase_1)
    return { className: "badge yellow", text: prettyVendor(f.vendor_key) };
  return { className: "badge green", text: prettyVendor(f.vendor_key) };
}

function prettyVendor(key: string): string {
  if (key === "richmond_utilities") return "Richmond";
  if (key === "hopkinsville_water_environment_authority") return "Hopkinsville";
  return key.replace(/_/g, " ");
}

function fileTypeFor(ext: string): {
  className: string;
  label: string;
} | null {
  const e = (ext || "").toLowerCase().replace(/^\./, "");
  if (e === "pdf") return { className: "file-type-badge pdf", label: "PDF" };
  if (e === "csv") return { className: "file-type-badge csv", label: "CSV" };
  if (e === "xlsx" || e === "xls")
    return { className: "file-type-badge xlsx", label: "XLSX" };
  if (["png", "jpg", "jpeg", "gif", "webp", "bmp"].includes(e))
    return { className: "file-type-badge image", label: e.toUpperCase() };
  if (!e) return null;
  return { className: "file-type-badge", label: e.toUpperCase() };
}

export function FileList({
  files,
  selected,
  onSelect,
  isSwitchingBatch,
  expectedFileCount,
}: Props) {
  // Phase 1U — show skeleton rows while a batch switch is in flight,
  // sized to the expected file count from the cached batch list entry
  // so the operator sees a non-empty placeholder immediately.
  if (isSwitchingBatch) {
    const n = Math.max(1, Math.min(8, expectedFileCount ?? 3));
    return (
      <ul className="file-list file-list-skeleton" aria-hidden>
        {Array.from({ length: n }, (_, i) => (
          <li key={i} className="file-row file-row-skeleton">
            <div className="file-row-skeleton-body">
              <div className="skeleton-line skeleton-line-name" />
              <div className="skeleton-line skeleton-line-meta" />
            </div>
            <div className="skeleton-line skeleton-line-badge" />
          </li>
        ))}
      </ul>
    );
  }
  if (files.length === 0) {
    return (
      <div className="empty-state small">
        <div style={{ fontWeight: 600, color: "var(--text)" }}>No files yet</div>
        <div style={{ marginTop: 4 }}>
          Drop bills into the upload zone above.
        </div>
      </div>
    );
  }
  return (
    <ul className="file-list">
      {files.map((f) => {
        const b = vendorBadge(f);
        const type = fileTypeFor(f.extension);
        return (
          <li
            key={f.filename}
            className={`file-row ${selected === f.filename ? "selected" : ""}`}
            onClick={() => onSelect(f.filename)}
          >
            <div style={{ overflow: "hidden", flex: 1, minWidth: 0 }}>
              <div className="name" title={f.filename}>
                {f.filename}
              </div>
              <div className="file-row-badges">
                {type && <span className={type.className}>{type.label}</span>}
                <span className="meta">{formatSize(f.size_bytes)}</span>
              </div>
            </div>
            <span className={b.className} title={f.vendor_detection_reason}>
              {b.text}
            </span>
          </li>
        );
      })}
    </ul>
  );
}
