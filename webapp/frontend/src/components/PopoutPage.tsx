// Phase 2C — Popout window host.
//
// Routes (parsed from window.location.hash):
//   #popout/template?batch=<id>           — read-only TemplateWorkspace
//   #popout/document?batch=<id>&file=<name>— DocumentPreviewPanel
//
// Why read-only? Cell edits in the main app live in App.tsx state and are
// only persisted on Export. A two-window editable flow would need backend
// edit persistence + cross-window sync, which is Phase 2D scope. For now
// the popout is a passive viewer that calls the same backend APIs and
// stays in sync via batch_id (operator triggers a refresh from the close
// button or by reopening the popout after re-processing).
//
// Detached-template sync: a BroadcastChannel keyed by batch_id carries
// `row-select` messages between the popout and the main window so when
// the operator picks a row here, the Document panel in the main window
// scrolls to the matching bill page (and vice versa).

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api, getFriendlyErrorMessage } from "../api";
import type {
  BatchStatus,
  ManualReviewItem,
  PreviewResponse,
} from "../types";
import { DocumentPreviewPanel } from "./DocumentPreviewPanel";
import { TemplateWorkspace } from "./TemplateWorkspace";

type PopoutKind = "template" | "document";

type PopoutQuery = {
  kind: PopoutKind;
  batch: string;
  file?: string;
};

export function parsePopoutHash(hash: string): PopoutQuery | null {
  // Accept both "#popout/template?…" and "#/popout/template?…" so we
  // don't fail on opinionated browsers that normalise the slash.
  const cleaned = hash.replace(/^#\/?/, "");
  if (!cleaned.startsWith("popout/")) return null;
  const [pathPart, queryPart = ""] = cleaned.slice("popout/".length).split("?");
  const kind = pathPart as PopoutKind;
  if (kind !== "template" && kind !== "document") return null;
  const params = new URLSearchParams(queryPart);
  const batch = params.get("batch") || "";
  if (!batch) return null;
  return {
    kind,
    batch,
    file: params.get("file") || undefined,
  };
}

export default function PopoutPage({ query }: { query: PopoutQuery }) {
  const [status, setStatus] = useState<BatchStatus | null>(null);
  const [preview, setPreview] = useState<PreviewResponse | null>(null);
  const [review, setReview] = useState<ManualReviewItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activePage, setActivePage] = useState<
    { batchId: string; filename: string; pageNumber: number } | null
  >(query.file
    ? { batchId: query.batch, filename: query.file, pageNumber: 1 }
    : null);
  const [selectedRowIndex, setSelectedRowIndex] = useState<number | null>(null);

  // BroadcastChannel sync with the main window. Outgoing: this popout
  // posts `row-select` whenever the operator picks a row. Incoming:
  // when the main window selects a row (e.g. via the Issues drawer
  // or a trace click), we mirror the selection here.
  const channelRef = useRef<BroadcastChannel | null>(null);
  const lastIncomingRowRef = useRef<number | null>(null);
  useEffect(() => {
    if (query.kind !== "template") return;
    if (typeof BroadcastChannel === "undefined") return;
    const ch = new BroadcastChannel(`bill-popout-sync-${query.batch}`);
    channelRef.current = ch;
    ch.onmessage = (ev) => {
      const data = ev.data as
        | { type: "row-select"; rowIndex: number | null; source: string }
        | undefined;
      if (!data || data.type !== "row-select" || data.source === "popout") return;
      lastIncomingRowRef.current = data.rowIndex; // suppress echo
      setSelectedRowIndex(data.rowIndex);
    };
    return () => {
      ch.close();
      if (channelRef.current === ch) channelRef.current = null;
    };
  }, [query.kind, query.batch]);

  const handleSelectRow = useCallback((rowIndex: number | null) => {
    setSelectedRowIndex(rowIndex);
    const ch = channelRef.current;
    if (!ch) return;
    if (lastIncomingRowRef.current === rowIndex) {
      lastIncomingRowRef.current = null;
      return;
    }
    ch.postMessage({
      type: "row-select",
      rowIndex,
      source: "popout",
    });
  }, []);

  // Mark the body so popout-specific CSS can apply (no scrollbars on root,
  // no app shell padding, etc.).
  useEffect(() => {
    document.body.classList.add("popout-mode");
    document.title = `Popout · ${query.kind} · ${query.batch}`;
    return () => {
      document.body.classList.remove("popout-mode");
    };
  }, [query.kind, query.batch]);

  // Load the batch + preview once on mount. We deliberately do NOT poll —
  // the popout is a snapshot view; the user can close+reopen to refresh.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    (async () => {
      try {
        const s = await api.getBatch(query.batch);
        if (cancelled) return;
        setStatus(s);
        if (query.kind === "template") {
          if (s.preview_available) {
            const [p, r] = await Promise.all([
              api.preview(query.batch),
              api.manualReview(query.batch),
            ]);
            if (cancelled) return;
            setPreview(p);
            setReview(r.items);
          } else {
            setPreview(null);
            setReview([]);
          }
        }
      } catch (e) {
        if (cancelled) return;
        setError(getFriendlyErrorMessage(e, "Load popout"));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [query.batch, query.kind]);

  const closeWindow = useCallback(() => {
    window.close();
  }, []);

  const vendorLabel = useMemo(() => prettyVendor(status), [status]);
  const fileCount = status?.files?.length ?? 0;
  const headerTitle =
    query.kind === "template" ? "Template" : "Document Viewer";
  const subTitle = status?.batch_name || query.batch;

  if (error) {
    return (
      <div className="popout-page">
        <header className="popout-header">
          <div>
            <span className="popout-header-title">{headerTitle}</span>
            <span className="popout-header-meta">· {subTitle}</span>
          </div>
          <button type="button" className="popout-header-close" onClick={closeWindow}>
            Close window
          </button>
        </header>
        <div className="popout-error">{error}</div>
      </div>
    );
  }
  if (loading || !status) {
    return (
      <div className="popout-page">
        <header className="popout-header">
          <div>
            <span className="popout-header-title">{headerTitle}</span>
            <span className="popout-header-meta">· {subTitle}</span>
          </div>
          <button type="button" className="popout-header-close" onClick={closeWindow}>
            Close window
          </button>
        </header>
        <div className="popout-loading">Loading…</div>
      </div>
    );
  }

  return (
    <div className="popout-page">
      <header className="popout-header">
        <div>
          <span className="popout-header-title">{headerTitle}</span>
          <span className="popout-header-meta">
            · {status.batch_name || query.batch}
          </span>
          <span className="popout-header-readonly">Read-only</span>
        </div>
        <button type="button" className="popout-header-close" onClick={closeWindow}>
          Close window
        </button>
      </header>
      <div className="popout-body">
        {query.kind === "template" && (
          <TemplateWorkspace
            preview={preview}
            edits={{}}
            onCellEdit={() => undefined}
            fileCount={fileCount}
            selectedRowIndex={selectedRowIndex}
            activeDocumentPage={null}
            // Picking a row here broadcasts to the main window so the
            // Document panel scrolls to the matching bill page.
            onSelectRow={handleSelectRow}
            // Suppress edit-only affordances inside the popout.
            readOnly
            batchName={status.batch_name}
            vendorLabel={vendorLabel}
            exportName={(status.metadata as any)?.export_name || ""}
            defaultExportName={
              status.batch_name
                ? `${status.batch_name}.xlsx`
                : "ResMan_Import.xlsx"
            }
          />
        )}
        {query.kind === "document" && (
          <DocumentPreviewPanel
            batchId={query.batch}
            filename={query.file || null}
            targetPage={
              activePage
                ? { ...activePage, nonce: 1 }
                : null
            }
            onActivePageChange={(p) => setActivePage(p)}
          />
        )}
        {/* `review` is intentionally fetched but not surfaced — Phase
            2C keeps the popout viewer minimal. The next phase wires
            it to an Issues drawer if requested. */}
        {void review}
      </div>
    </div>
  );
}

function prettyVendor(status: BatchStatus | null): string {
  if (!status) return "";
  const supported = (status.metadata as any)?.supported_vendor_summary || {};
  const keys = Object.keys(supported);
  if (keys.length === 1) {
    const k = keys[0];
    if (k === "richmond_utilities") return "Richmond Utilities";
    if (k === "hopkinsville_water_environment_authority")
      return "Hopkinsville Water";
    return k.replace(/_/g, " ");
  }
  if (keys.length > 1) return "Mixed vendors";
  return "";
}
