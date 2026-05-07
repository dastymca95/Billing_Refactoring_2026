# Webapp Phase 1D — Layout, PDF preview, drag/drop, per-bill PDF links

**Date:** 2026-05-02
**Scope:** Web UI redesign + PDF preview fix + drag/drop bug fix + per-bill PDF splitting + per-invoice Dropbox links. No changes to vendor processors that affect CSV behavior, no changes to `Output/Template.xlsx`, no changes to source files.

---

## What changed

| Area | Before | After |
| --- | --- | --- |
| Layout | One sidebar + a vertically-stacked main panel (DocumentPreview → ResMan → ManualReview). | Three-column grid: compact sidebar, collapsible document preview, ResMan workspace + collapsible manual-review drawer. |
| Document preview | `<embed>` against the `/raw` endpoint with default Content-Disposition. Sometimes triggered Chrome's download path on PDFs. | `<iframe>` against a new `/content` endpoint that sets `Content-Disposition: inline` + correct `Content-Type` + `X-Content-Type-Options: nosniff`. PDFs render inline; if the browser refuses, a graceful "PDF preview unavailable" fallback appears with an "Open in new tab" link. Path traversal is blocked. |
| Drag-drop | Only the drop zone called `preventDefault`. Dropping a PDF anywhere else navigated the browser to the file. | Window-level `dragenter` / `dragover` / `drop` guard that swallows file drops outside the drop zone (only when `dataTransfer.types.includes("Files")`). Drop zone uses depth counter so dragging over child elements doesn't flicker the dragging state. |
| Multi-bill PDF support links | All ResMan rows generated from a 14-page PDF shared the link to the **full** PDF. Operators had to scroll to the right page to verify a row. | Each PDF page is split into a 1-page support PDF (`Richmond_Utilities_<account>_<Mon>_<YY>.pdf`); each split PDF is uploaded to Dropbox separately under `…/split_bills/`; each ResMan row gets the URL of the specific bill it came from. CSV behavior is bit-for-bit unchanged. |

---

## Files changed

### New
- [utils/pdf_splitter.py](utils/pdf_splitter.py) — generic per-page PDF splitter (`split_pdf_pages(pdf_path, output_folder, page_metadata)`) that uses `pypdf`, never modifies the source PDF, and returns `SplitPdfResult` per page (status `ok` / `skipped_invalid_page` / `failed`, plus `output_pdf_path`, `account_number`, `invoice_number`, `warnings`).

### Backend
- [webapp/backend/api/preview.py](webapp/backend/api/preview.py)
  - Added `_resolve_input_file()` with explicit path-traversal defense (basename + `relative_to` containment check).
  - Added `_file_response(target, inline)` that builds `FileResponse` with `Content-Type` from the file extension (PDF → `application/pdf`, etc.), `Content-Disposition: inline|attachment`, and `X-Content-Type-Options: nosniff`.
  - New endpoint `GET /api/batches/{batch_id}/files/{filename}/content` — inline file streaming with the correct headers; replaces what `/raw` used to do (the legacy `/raw` endpoint stays for back-compat and now also returns `inline` headers).

- [Training Bills_Invoices/Water - Sewer/Richmond Utilities/process_richmond_utilities.py](Training%20Bills_Invoices/Water%20-%20Sewer/Richmond%20Utilities/process_richmond_utilities.py)
  - Imports `utils.pdf_splitter` defensively (graceful fallback when pypdf isn't installed).
  - In the PDF processing loop: builds per-page metadata (vendor slug, account number, month abbrev, 2-digit year), calls `split_pdf_pages` once per PDF, stores results in `split_results_by_page`.
  - Per-page upload logic: if a split exists for a page, upload the *split PDF* to Dropbox under `dropbox_rules.split_pdf_folder_pattern` ("…/split_bills/"); otherwise fall back to the full-PDF upload (with `support_pdf_split_failed` flag). Lazy: full-PDF upload only happens when at least one page falls back.
  - `_upload_support_document` gained an optional `folder_pattern_override` so the split path can write into the sibling subfolder without touching CSV behavior.
  - Path correctness: split PDFs land in `<out_dir>/<vendor_key>/support_documents/` for the CLI and `<out_dir>/support_documents/` for the webapp (the webapp's `out_dir` already ends in the vendor key). One code path handles both.

### Frontend
- [webapp/frontend/src/App.tsx](webapp/frontend/src/App.tsx) — full rewrite of the workspace layout. CSS-grid 3-column shell (`sidebar | document-column | template-column`). Document preview can collapse into a 36 px vertical rail. Template + manual review live in the main column; manual review is a bottom drawer with its own collapse toggle. Window-level drag/drop guard added in a `useEffect`.
- [webapp/frontend/src/components/DropZone.tsx](webapp/frontend/src/components/DropZone.tsx) — `dragenter`/`dragover`/`dragleave`/`drop` all call `preventDefault` + `stopPropagation`. Drag-depth counter. Sets `dataTransfer.dropEffect = "copy"` on enter (tells the browser we accept the file). Marks itself with `data-dropzone="true"` so the global guard can detect drops INTO the zone vs OUTSIDE.
- [webapp/frontend/src/components/DocumentPreviewPanel.tsx](webapp/frontend/src/components/DocumentPreviewPanel.tsx) — uses `api.fileContentUrl(...)` instead of `fileRawUrl`, renders PDFs in an `<iframe>` (more reliable cross-browser than `<embed>`), supports a collapsed mode driven by props, falls back to "PDF preview unavailable" with an "Open in new tab" link if the iframe errors.
- [webapp/frontend/src/components/ManualReviewPanel.tsx](webapp/frontend/src/components/ManualReviewPanel.tsx) — collapse state lifted to props (drawer button now in `App.tsx`); added new tooltips for `extracted_total_mismatch`, `support_pdf_split_failed`, `support_pdf_account_unknown`, `support_pdf_upload_failed`, `support_pdf_link_missing`.
- [webapp/frontend/src/api.ts](webapp/frontend/src/api.ts) — new `fileContentUrl(batchId, filename)` helper.
- [webapp/frontend/src/styles.css](webapp/frontend/src/styles.css) — full restyle for the new layout. CSS variables for `--sidebar-width` / `--doc-column-width`. Responsive breakpoints at 1100 px (drop the doc column) and 760 px (stack everything). Dropzone shrinks to a compact size in the sidebar.

### YAML
- [config/vendors/richmond_utilities.yaml](config/vendors/richmond_utilities.yaml)
  - `support_document_rules.pdf_multi_bill_handling.{enabled, split_strategy, link_strategy, split_pdf_subfolder, split_filename_format, fallback_filename_format, fallback_to_full_pdf_if_split_fails, manual_review_reason_on_split_failure, manual_review_reason_on_unknown_account}`.
  - `support_document_rules.csv_handling.link_strategy` (declares the unchanged CSV behavior).
  - `dropbox_rules.split_pdf_folder_pattern` = `…/split_bills/`.
  - 4 new manual-review triggers: `support_pdf_split_failed`, `support_pdf_account_unknown`, `support_pdf_upload_failed`, `support_pdf_link_missing`.
  - Change-log entry `user_ap_rules_v9_per_bill_pdf_split` documents the rationale.

### Untouched (intentionally)
- `Training Bills_Invoices/Water - Sewer/Richmond Utilities/Bills_Training/*` — all 14 CSVs and the 14-page PDF.
- `Output/Template.xlsx`.
- `Properties/Unit Info Clean.csv`, `Gl Codes/*.csv`, `Vendors/Vendor List.csv`.
- All earlier vendor-processor logic for CSV/XLSX (`build_invoice`, `parse_movement_file`, `find_latest_billing_cycle`, ...).

---

## Layout details

```
+----------------+----------------+----------------------------+
|  Sidebar       |  Doc preview   |  ResMan template (primary) |
|  (240 px)      |  (360 px,      |  (1fr — biggest column)    |
|                |   collapsible) |                            |
|  Upload zone   |  CSV → table   |  Editable grid             |
|  Files list    |  PDF → iframe  |  (Phase 1B inline edits)   |
|  Actions       |                |                            |
|                |                +----------------------------+
|                |                |  Manual review drawer      |
|                |                |  (max 38vh, collapsible)   |
+----------------+----------------+----------------------------+
```

CSS grid columns: `var(--sidebar-width) var(--doc-column-width) 1fr`. When the doc preview is collapsed the columns become `var(--sidebar-width) 36px 1fr` and a vertical rail button takes its place. Below 1100 px viewport the doc column hides; below 760 px all three stack.

The ResMan template uses `flex: 1 1 auto` inside the main column so it stretches to fill the available height, and its inner `.preview-pane` gets `overflow: auto` to scroll horizontally + vertically like a spreadsheet.

---

## PDF preview fix

### Backend
`GET /api/batches/{batch_id}/files/{filename}/content` returns:
```
HTTP/1.1 200 OK
Content-Type: application/pdf
Content-Disposition: inline; filename="Richmond Utilities - Blue Country 4-6-26.pdf"
X-Content-Type-Options: nosniff
```

For CSV files: `text/csv; charset=utf-8`. For images: matching `image/*` types. For anything else: `mimetypes.guess_type` with `application/octet-stream` fallback.

Path traversal is blocked at three layers:
1. `Path(filename).name` reduces the URL fragment to its basename.
2. `(in_dir / safe_name).resolve().relative_to(in_dir)` raises `ValueError` if the resolved path escapes the batch's input folder.
3. The reserved names `"."` and `".."` get rejected with HTTP 400.

Verified during the smoke test: `..%2F..%2Fevil` and `../../etc/passwd` both return HTTP 404, never an `etc/passwd` body.

### Frontend
`<iframe>` instead of `<embed>` because some Chrome / Edge versions disable `<embed src=...>` for cross-origin iframes and silently fall back to a download. `<iframe>` is honored consistently. If the iframe `onError` fires (rare; happens if the user has disabled PDF rendering), the panel shows:

> PDF preview unavailable. File can still be processed. **Open in new tab.**

---

## Drag/drop fix

### Cause
The browser's default action when a PDF is dropped on a page is "navigate to that file" (so Chrome opens it as a viewer). React's drop zone called `preventDefault` only for events fired *on the drop zone*. A drop on the body, the side panel, or the topbar would still trigger the default.

### Fix
Two layers:

1. **Drop zone** itself now `preventDefault`s on `dragenter`, `dragover`, `dragleave`, `drop` and `stopPropagation`s on each. A depth counter prevents flicker when dragging over child elements.

2. **Window-level guard** in `App.tsx`:
   ```ts
   useEffect(() => {
     const handler = (e: DragEvent) => {
       const hasFiles = Array.from(e.dataTransfer?.types ?? []).includes("Files");
       if (!hasFiles) return;
       const inside = (e.target as HTMLElement | null)?.closest('[data-dropzone="true"]');
       if (inside) return;
       e.preventDefault();
       if (e.dataTransfer) e.dataTransfer.dropEffect = "none";
     };
     window.addEventListener("dragenter", handler);
     window.addEventListener("dragover", handler);
     window.addEventListener("drop", handler);
     return () => { /* cleanup */ };
   }, []);
   ```

   The guard only swallows file drags (text/HTML drags don't carry `Files`), only fires outside the drop zone (the zone is marked with `data-dropzone="true"`), and sets `dropEffect = "none"` so the cursor shows the "no" icon over non-target areas. Cleanup removes the listeners on unmount.

---

## Per-bill PDF split + per-invoice Dropbox links

### Pipeline

```
PDF → parse_richmond_pdf_bill → list[_PdfPageBill]
       (per-page: account, dates, address, line items, total)

list[_PdfPageBill]
  ↓
split_pdf_pages(pdf, out/support_documents/, page_metadata)
  → list[SplitPdfResult]    (one 1-page PDF per bill)

For each _PdfPageBill:
  if split_results[page].success:
    upload split PDF → Dropbox under split_pdf_folder_pattern
    invoice.support_document_url = split URL
  else:
    upload full PDF (lazy, once) → Dropbox under folder_pattern
    invoice.support_document_url = full URL
    invoice.manual_review_reasons += [support_pdf_split_failed]

invoice.support_document_url → ResMan row's Document Url
```

### Output filenames
- Default: `Richmond_Utilities_341340.0094_Mar_26.pdf`
- Unknown account fallback: `Richmond_Utilities_page05_Mar_26.pdf` (uses page number; flagged `support_pdf_account_unknown`).

### Dropbox layout
| Source | Folder pattern | Example |
| --- | --- | --- |
| Original CSV / XLSX | `{base_folder}/{vendor_name}/{year}/{month_number} - {month_abbrev}` | `/Billing_Refactoring_2026/Richmond Utilities/2026/04 - Apr/34134000_94_BillingHistory_Recent (1).csv` |
| Original full PDF (only if any page falls back) | same as above | `/Billing_Refactoring_2026/Richmond Utilities/2026/03 - Mar/Richmond Utilities - Blue Country 4-6-26.pdf` |
| Per-bill split PDF | `dropbox_rules.split_pdf_folder_pattern` = `…/split_bills/` | `/Billing_Refactoring_2026/Richmond Utilities/2026/03 - Mar/split_bills/Richmond_Utilities_341340.0094_Mar_26.pdf` |

---

## Tests performed

### 1. Frontend build
```
> tsc -b && vite build
✓ 38 modules transformed.
dist/assets/index-*.css   7.67 kB │ gzip:  2.05 kB
dist/assets/index-*.js  162.07 kB │ gzip: 52.10 kB
✓ built in 732ms
```

### 2. Backend `/content` endpoint
- CSV → `Content-Type: text/csv; charset=utf-8`, `Content-Disposition: inline`, `X-Content-Type-Options: nosniff` ✓
- PDF → `Content-Type: application/pdf`, `Content-Disposition: inline`, `X-Content-Type-Options: nosniff` ✓
- Path traversal `../../etc/passwd` and similar → HTTP 404 ✓

### 3. Drag/drop guard (manual)
- Drop PDF on drop zone → uploads ✓
- Drop PDF on the topbar / sidebar / template area → page does NOT navigate; cursor shows "no-drop" ✓
- Drop text-only drag (e.g. selected text from another tab) → app behavior unchanged ✓
  *(text drags lack `dataTransfer.types.includes("Files")` so the guard is a no-op for them)*

### 4. Richmond PDF processing — webapp end-to-end
Uploaded all 14 CSVs + the 14-page PDF, processed:
- 28 invoices, 32 ResMan rows, 23 manual-review items.
- 14 split PDFs created in `webapp_data/batches/<id>/processed/richmond_utilities/support_documents/` (one per account, named `Richmond_Utilities_<account>_Mar_26.pdf`).
- Each split PDF uploaded separately to Dropbox under `…/split_bills/`.
- 28 distinct Document Url values across the 32 rows:
  - 14 CSV URLs (one per CSV file, shared between water-sewer and gas line items).
  - 14 split-PDF URLs (one per PDF page / per account).
- Multi-line invoices (e.g. `341340.0094 Mar 26` with 2 line items) correctly share the same split PDF link.

Sample preview output:
```
341340.0094 Apr 26 → ...34134000_94_BillingHistory_Recent (1).csv  (CSV)
341340.0094 Mar 26 → ...Richmond_Utilities_341340.0094_Mar_26.pdf  (split PDF)
361560.0096 Apr 26 → ...36156000_96_BillingHistory_Recent (1).csv  (CSV)
361560.0096 Mar 26 → ...Richmond_Utilities_361560.0096_Mar_26.pdf  (split PDF)
```

### 5. CSV regression
- 14 CSVs alone still produce 14 invoices / 16 ResMan lines (unchanged from Phase 1A baseline).
- CSV `Document Url` column still points to the original CSV (unchanged from Phase 1B/1C baseline).

### 6. Export round-trip
- `POST /api/batches/<id>/export` (no body) → exported xlsx contains the per-bill URLs (28 distinct across 32 data rows).
- Download streams the xlsx.
- `Output/Template.xlsx` SHA-256 unchanged.

### 7. CLI regression
```
$ python "Training Bills_Invoices/.../process_richmond_utilities.py"
PDF split: 14/14 pages written to .../Processed_Output/richmond_utilities/support_documents
PDF files processed          : 1
PDF pages processed          : 14
Invoices produced            : 28
ResMan line items            : 32
```
Same numbers as before the Phase 1D work.

### 8. Source-file integrity (post-test SHA-256)
| File | Status |
| --- | --- |
| `Output/Template.xlsx` | unchanged |
| `Properties/Unit Info Clean.csv` | unchanged |
| `Gl Codes/Chart Of Accounts.csv` | unchanged |
| `Gl Codes/General Ledger Report.csv` | unchanged |
| `Vendors/Vendor List.csv` | unchanged |
| 14 CSVs in `Bills_Training/` | unchanged |
| `Richmond Utilities - Blue Country 4-6-26.pdf` | unchanged |

---

## Known limitations

| Limitation | Detail | Mitigation |
| --- | --- | --- |
| Split path requires `pypdf` | If `pypdf` isn't installed in the active Python interpreter, every page falls back to the full-PDF link and is flagged `support_pdf_split_failed`. | The `.venv` and the system Python both have it installed (see `webapp/README_WEBAPP.md` setup section). The code does not crash if it's missing. |
| Per-bill links rely on Dropbox being configured | Without `DROPBOX_*` env vars the URLs are blank and rows are flagged `dropbox_credentials_missing` (existing behavior). The split PDFs are still written to disk for local audit. | Same as Phase 1A/1B. |
| Some browsers refuse to render `application/pdf` in iframes (privacy / extension policy) | The fallback "Open in new tab" link is shown. | Operators can open the PDF externally and continue editing the preview. |
| Drop-on-window guard runs at React mount time | Other libraries that swallow drag events earlier (e.g. an MFE host) might steal the drop. | The guard listeners are attached to `window` directly with the default phase, which is the standard pattern. No conflicts in this app. |
| Manual review drawer max-height is 38 vh | On very short viewports (< 600 px) the drawer can hide some rows behind the template. | Drawer has its own scroll bar; `Collapse` button shrinks it to a 1-line stub. |
| The Phase 1B inline edit feature is preserved | Edits to Document Url cells are still allowed (an operator can paste a different link). The template export uses the operator's edits. | No change needed. |

---

## Confirmation

- **Source files untouched.** Hashes captured before and after every test pass.
- **`Output/Template.xlsx` untouched.** The export path opens it read-only via `shutil.copy2` before openpyxl writes the destination.
- **Richmond Utilities CLI behavior unchanged** for CSV-only and CSV+PDF batches: 14/16 (CSV-only baseline) and 28/32 (mixed) remain the headline numbers.
- **Webapp Phase 1A / 1B / 1C behavior preserved.** The new layout is a re-shuffle of existing components; the only API addition is `/content` and the only YAML addition is the multi-bill block.
- **No new vendor processors created.** Splitter and inline endpoint live in shared utility / webapp code.
- **No Dropbox tokens exposed.** Credentials still read from environment via `utils/dropbox_uploader.py`.
