import type { FileEntry } from "../types";

type Props = {
  files: FileEntry[];
  selected: string | null;
  onSelect: (filename: string) => void;
};

function formatSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function vendorBadge(f: FileEntry) {
  if (f.vendor_key === "unknown") return { className: "badge gray", text: "needs review" };
  if (!f.supported_in_phase_1) return { className: "badge yellow", text: f.vendor_key };
  return { className: "badge green", text: f.vendor_key.replace(/_/g, " ") };
}

export function FileList({ files, selected, onSelect }: Props) {
  if (files.length === 0) {
    return (
      <div className="empty-state">No files uploaded yet.</div>
    );
  }
  return (
    <ul className="file-list">
      {files.map((f) => {
        const b = vendorBadge(f);
        return (
          <li
            key={f.filename}
            className={`file-row ${selected === f.filename ? "selected" : ""}`}
            onClick={() => onSelect(f.filename)}
          >
            <div style={{ overflow: "hidden", flex: 1 }}>
              <div className="name" title={f.filename}>{f.filename}</div>
              <div className="meta">
                {formatSize(f.size_bytes)} · {f.extension || "no-ext"}
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
