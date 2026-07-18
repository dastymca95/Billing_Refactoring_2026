import type {
  BatchListEntry,
  BatchProgress,
  BillingV2AuditResponse,
  BillingV2PrepareLinksResponse,
  FileEntry,
  PreviewResponse,
  PreviewRow,
  UploadFileProgress,
} from "../../types";
import type { CellEdits } from "../../components/ResManTemplatePreview";

export type BillingV2Filter =
  | "all"
  | "needs_review"
  | "ready"
  | "missing_required"
  | "missing_link"
  | "ai_generated";

export type BillingV2ViewMode = "bulk" | "single";

export type BillingV2DocumentTarget = {
  batchId: string;
  filename: string;
  pageNumber: number;
  nonce: number;
};

export type BillingV2ActivePage = {
  batchId: string;
  filename: string;
  pageNumber: number;
};

export type BillingV2State = {
  audit: BillingV2AuditResponse | null;
  batchList: BatchListEntry[];
  activeBatchId: string | null;
  activeBatchName: string;
  files: FileEntry[];
  preview: PreviewResponse | null;
  progress: BatchProgress | null;
  linkSummary: BillingV2PrepareLinksResponse | null;
  selectedFilename: string | null;
  selectedRowIndex: number | null;
  selectedColumnKey: string | null;
  activePage: BillingV2ActivePage | null;
  documentTarget: BillingV2DocumentTarget | null;
  search: string;
  filter: BillingV2Filter;
  groupBy: string;
  viewMode: BillingV2ViewMode;
  singleGroupIndex: number;
  edits: CellEdits;
  uploadItems: UploadFileProgress[];
  loadingBatches: boolean;
  loadingBatch: boolean;
  processing: boolean;
  cancelling: boolean;
  exporting: boolean;
  preparingLinks: boolean;
  error: string;
  notice: string;
};

export type BillingV2Action =
  | { type: "auditLoaded"; audit: BillingV2AuditResponse }
  | { type: "batchListLoading"; loading: boolean }
  | { type: "batchListLoaded"; batches: BatchListEntry[] }
  | { type: "batchLoading"; loading: boolean }
  | {
      type: "batchLoaded";
      batchId: string;
      batchName: string;
      files: FileEntry[];
      preview: PreviewResponse | null;
      progress: BatchProgress | null;
      hasExport?: boolean;
    }
  | { type: "setActiveBatch"; batchId: string | null; batchName?: string }
  | { type: "setFiles"; files: FileEntry[] }
  | { type: "setPreview"; preview: PreviewResponse | null }
  | {
      type: "setAccountingReadiness";
      accountingReadiness: PreviewResponse["accounting_readiness"];
    }
  | { type: "setProgress"; progress: BatchProgress | null }
  | { type: "setProcessing"; processing: boolean }
  | { type: "setCancelling"; cancelling: boolean }
  | { type: "setExporting"; exporting: boolean }
  | { type: "setPreparingLinks"; preparing: boolean }
  | { type: "linksPrepared"; summary: BillingV2PrepareLinksResponse }
  | { type: "selectDocument"; filename: string | null; pageNumber?: number }
  | { type: "selectRow"; rowIndex: number | null; target?: BillingV2DocumentTarget | null }
  | { type: "selectCell"; rowIndex: number | null; column: string | null }
  | { type: "activePageChanged"; page: BillingV2ActivePage }
  | { type: "setSearch"; search: string }
  | { type: "setFilter"; filter: BillingV2Filter }
  | { type: "setGroupBy"; groupBy: string }
  | { type: "setViewMode"; viewMode: BillingV2ViewMode }
  | { type: "setSingleGroupIndex"; index: number }
  | { type: "cellEdited"; rowIndex: number; column: string; value: unknown }
  | { type: "resetEdits" }
  | { type: "upsertUploadItem"; item: UploadFileProgress }
  | { type: "patchUploadItem"; id: string; patch: Partial<UploadFileProgress> }
  | { type: "clearFinishedUploads" }
  | { type: "setError"; error: string }
  | { type: "setNotice"; notice: string };

export const initialBillingV2State: BillingV2State = {
  audit: null,
  batchList: [],
  activeBatchId: null,
  activeBatchName: "",
  files: [],
  preview: null,
  progress: null,
  linkSummary: null,
  selectedFilename: null,
  selectedRowIndex: null,
  selectedColumnKey: null,
  activePage: null,
  documentTarget: null,
  search: "",
  filter: "all",
  groupBy: "",
  viewMode: "bulk",
  singleGroupIndex: 0,
  edits: {},
  uploadItems: [],
  loadingBatches: false,
  loadingBatch: false,
  processing: false,
  cancelling: false,
  exporting: false,
  preparingLinks: false,
  error: "",
  notice: "",
};

export function billingV2Reducer(
  state: BillingV2State,
  action: BillingV2Action,
): BillingV2State {
  switch (action.type) {
    case "auditLoaded":
      return { ...state, audit: action.audit };
    case "batchListLoading":
      return { ...state, loadingBatches: action.loading };
    case "batchListLoaded":
      return { ...state, batchList: action.batches, loadingBatches: false };
    case "batchLoading":
      return { ...state, loadingBatch: action.loading };
    case "batchLoaded": {
      const selectedFilename = pickSelectedFilename(
        action.files,
        state.selectedFilename,
      );
      return {
        ...state,
        activeBatchId: action.batchId,
        activeBatchName: action.batchName,
        files: action.files,
        preview: action.preview,
        progress: action.progress,
        selectedFilename,
        selectedRowIndex: null,
        selectedColumnKey: null,
        activePage: selectedFilename
          ? { batchId: action.batchId, filename: selectedFilename, pageNumber: 1 }
          : null,
        documentTarget: selectedFilename
          ? {
              batchId: action.batchId,
              filename: selectedFilename,
              pageNumber: 1,
              nonce: Date.now(),
            }
          : null,
        edits: {},
        linkSummary: null,
        loadingBatch: false,
        error: "",
      };
    }
    case "setActiveBatch":
      return {
        ...state,
        activeBatchId: action.batchId,
        activeBatchName: action.batchName ?? "",
        files: [],
        preview: null,
        progress: null,
        linkSummary: null,
        selectedFilename: null,
        selectedRowIndex: null,
        selectedColumnKey: null,
        activePage: null,
        documentTarget: null,
        edits: {},
        error: "",
      };
    case "setFiles":
      return {
        ...state,
        files: action.files,
        selectedFilename: pickSelectedFilename(action.files, state.selectedFilename),
      };
    case "setPreview":
      return {
        ...state,
        preview: action.preview,
        selectedRowIndex: null,
        selectedColumnKey: null,
        edits: {},
      };
    case "setAccountingReadiness":
      return state.preview
        ? {
            ...state,
            preview: {
              ...state.preview,
              accounting_readiness: action.accountingReadiness,
            },
          }
        : state;
    case "setProgress":
      return { ...state, progress: action.progress };
    case "setProcessing":
      return { ...state, processing: action.processing };
    case "setCancelling":
      return { ...state, cancelling: action.cancelling };
    case "setExporting":
      return { ...state, exporting: action.exporting };
    case "setPreparingLinks":
      return { ...state, preparingLinks: action.preparing };
    case "linksPrepared":
      return {
        ...state,
        linkSummary: action.summary,
        preparingLinks: false,
      };
    case "selectDocument":
      return {
        ...state,
        selectedFilename: action.filename,
        documentTarget:
          action.filename && state.activeBatchId
            ? {
                batchId: state.activeBatchId,
                filename: action.filename,
                pageNumber: Math.max(1, Math.floor(action.pageNumber || 1)),
                nonce: Date.now(),
              }
            : null,
      };
    case "selectRow":
      return {
        ...state,
        selectedRowIndex: action.rowIndex,
        selectedColumnKey: action.rowIndex == null ? null : state.selectedColumnKey,
        selectedFilename: action.target?.filename ?? state.selectedFilename,
        documentTarget: action.target === undefined ? state.documentTarget : action.target,
      };
    case "selectCell":
      return {
        ...state,
        selectedRowIndex: action.rowIndex,
        selectedColumnKey: action.column,
      };
    case "activePageChanged":
      return {
        ...state,
        activePage: action.page,
        selectedFilename: action.page.filename,
      };
    case "setSearch":
      return { ...state, search: action.search };
    case "setFilter":
      return { ...state, filter: action.filter };
    case "setGroupBy":
      return { ...state, groupBy: action.groupBy };
    case "setViewMode":
      return { ...state, viewMode: action.viewMode };
    case "setSingleGroupIndex":
      return { ...state, singleGroupIndex: action.index };
    case "cellEdited":
      return {
        ...state,
        edits: {
          ...state.edits,
          [action.rowIndex]: {
            ...(state.edits[action.rowIndex] ?? {}),
            [action.column]: action.value,
          },
        },
      };
    case "resetEdits":
      return { ...state, edits: {} };
    case "upsertUploadItem":
      return {
        ...state,
        uploadItems: [
          ...state.uploadItems.filter((item) => item.id !== action.item.id),
          action.item,
        ],
      };
    case "patchUploadItem":
      return {
        ...state,
        uploadItems: state.uploadItems.map((item) =>
          item.id === action.id ? { ...item, ...action.patch } : item,
        ),
      };
    case "clearFinishedUploads":
      return {
        ...state,
        uploadItems: state.uploadItems.filter(
          (item) => item.status !== "done" && item.status !== "failed",
        ),
      };
    case "setError":
      return { ...state, error: action.error };
    case "setNotice":
      return { ...state, notice: action.notice };
  }
}

function pickSelectedFilename(files: FileEntry[], current: string | null): string | null {
  if (current && files.some((file) => file.filename === current)) return current;
  return files[0]?.filename ?? null;
}

export function rowDocumentTarget(
  batchId: string | null,
  row: PreviewRow | null | undefined,
): BillingV2DocumentTarget | null {
  if (!batchId || !row?._meta?.source_file) return null;
  const rawPage = row._meta.source_page;
  const pageNumber =
    typeof rawPage === "number" && Number.isFinite(rawPage) && rawPage > 0
      ? Math.floor(rawPage)
      : 1;
  return {
    batchId,
    filename: row._meta.source_file,
    pageNumber,
    nonce: Date.now(),
  };
}

export function mergeEditedRows(
  rows: PreviewRow[],
  edits: CellEdits,
): Record<string, unknown>[] {
  return rows.map((row, index) => ({
    ...row,
    ...(edits[index] ?? {}),
  }));
}
