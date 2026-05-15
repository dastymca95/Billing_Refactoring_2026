# Phase AI-9 - Universal File Ingestion Normalization Report

Date: 2026-05-14

## 1. DocumentCandidate Schema

Implemented the normalized ingestion model in `webapp/backend/services/document_ingestion.py`.

The ingestion layer now produces a `DocumentCandidate` with:

- source identity: `source_file`, `source_path`, `mime_type`, `file_size_bytes`
- normalized type: `pdf_digital`, `pdf_scanned`, `image`, `screenshot`, `excel`, `csv`, `word`, `internal_template`, or `unknown`
- structure: `page_count`, `sheet_count`, `pages`, `tables`, `images`
- extracted content: `document_text`, per-page text, table rows, image refs
- quality: `text_quality_score`, `extraction_quality`, `needs_ocr`, `needs_vision`
- hints: `vendor_hint`, `category_hint`
- diagnostics: `warnings`, `metadata`

Supporting models were added for `PageCandidate`, `TextBlockCandidate`, `TableCandidate`, and `ImageCandidate`.

This model is intentionally ingestion-only. It does not choose vendors, properties, GL accounts, or final ResMan rows.

## 2. Ingestion Service Architecture

`ingest_document(file_path, allow_ocr=True, allow_vision_hint=True, max_pages=None)` now acts as the common normalization entry point.

The service:

- detects file type
- extracts text and tables when possible
- records page/table/image candidates
- scores text quality
- flags weak OCR or vision-recommended paths
- protects internal templates
- avoids AI, Dropbox, export writes, and accounting decisions

The universal reasoner can now consume `DocumentCandidate` objects while deterministic utility processors continue to route first and remain untouched.

## 3. PDF Support

Digital PDFs are supported with per-page text extraction through the existing PDF text path.

Output includes:

- document text
- page count
- per-page candidates
- PDF text blocks where available
- quality scoring

Digital PDFs with strong text are classified as `pdf_digital`.

## 4. Scanned PDF Support

Scanned or weak-text PDFs are classified as `pdf_scanned` when OCR is required or text quality is low.

Behavior:

- OCR is attempted when allowed and available
- weak/no OCR creates review-friendly warnings
- `needs_vision=true` is set when text is low or missing and vision hints are allowed
- no mandatory accounting fields are invented from weak OCR

## 5. Image and Screenshot Support

PNG, JPG, JPEG, and WEBP files are normalized as image/screenshot candidates.

Behavior:

- one `PageCandidate` is created
- image dimensions are preserved
- OCR is attempted when available
- weak OCR returns low quality plus warnings
- image references are preserved for later vision assist
- no image bytes or base64 are logged

This supports screenshot workflows without making AI or vision automatic in deterministic utility paths.

## 6. Excel Support

`.xlsx` files are supported using `openpyxl`.

The service extracts:

- sheet names
- used ranges
- table-like rows
- synthesized document text
- sheet/table metadata

Internal ResMan templates are protected. `Output/Template.xlsx` and template-like internal workbooks are classified as `internal_template` with `internal_resman_template_not_ingested`.

`.xls` remains limited/unsupported unless a safe parser is added later.

## 7. CSV Support

CSV files are supported as table candidates.

Behavior:

- UTF-8 and Latin-1 fallback
- delimiter sniffing
- header extraction
- row extraction
- synthesized text preview
- truncation warnings for large files

This does not replace deterministic CSV paths such as Richmond. It is for universal/semi-structured ingestion and diagnostics.

## 8. Word Support

`.docx` is supported when `python-docx` is available.

The ingestion layer extracts:

- paragraphs
- document tables
- synthesized document text

Legacy `.doc` is reported as unsupported with a friendly warning.

## 9. Universal Reasoner Integration

`universal_invoice_reasoner.py` now has a normalized ingestion path available before AI/canonical reasoning.

Intended flow:

1. file path
2. `DocumentCandidate`
3. vendor/category hints
4. canonical rules
5. AI text only if needed and configured
6. AI vision only if recommended, enabled, supported, and allowed
7. normalized invoice model
8. Bulk and Single Invoice projections

Deterministic utility processors still route before the universal reasoner, preserving Richmond, HWEA, Pennyrile, Shelbyville, Alabama, EPB, Henderson, Nolin, Knoxville, Kentucky, and the other active utility overlays.

## 10. UI File Support Status

Batch file metadata now exposes normalized support labels for compact sidebar badges:

- Digital PDF
- Scanned PDF
- Image
- Screenshot
- Excel
- CSV
- Word
- Unsupported
- Internal template ignored

Low-quality files can surface subtle diagnostics such as OCR weak or vision recommended without cluttering the batch tree.

Screenshot path:

`docs/reports/phases/screenshots/phase_ai9_universal_ingestion/`

## 11. Ingestion Preview Endpoint

Added:

`GET /api/batches/{batch_id}/files/{filename}/ingestion-preview`

The endpoint returns a capped, safe diagnostic preview:

- source type
- quality score
- page/sheet/table counts
- warnings
- OCR/vision recommendation
- vendor/category hints
- short text preview
- limited table preview

Safety:

- validates batch id and filename
- no AI calls
- no Dropbox calls
- no export/output writes
- no huge text payloads

## 12. Quality and Fallback Policy

Text quality is scored and labeled:

- `high`: enough structured text and invoice-like markers
- `medium`: usable text with some missing markers
- `low`: sparse/noisy OCR or weak extraction
- `none`: empty extraction

`needs_vision=true` is set only as a recommendation when text is low/none, the document is image-like, or key content is likely visual.

The ingestion layer never sends content to vision by itself. Downstream processing must still confirm:

- AI vision enabled
- provider supports vision
- route allows universal/AI-assisted processing
- file/page limits permit it

## 13. Tests Performed

Frontend:

- `cd webapp/frontend && npm.cmd run build` - PASS
- `cd webapp/frontend && npx.cmd tsc --noEmit` - PASS
- `cd webapp/frontend && npm.cmd run test:e2e` - PASS, 35 passed
- `cd webapp/frontend && npx.cmd playwright test ingestion-ai9` - PASS

Backend:

- `python -m compileall webapp\backend` - PASS
- `python scripts\verify_backend_routes.py` - PASS
- `python scripts\smoke_document_ingestion.py` - PASS
- `python scripts\smoke_canonical_rules_engine.py` - PASS
- `python scripts\smoke_canonical_invoice_fixtures.py` - PASS
- `python scripts\smoke_utility_processors.py` - PASS, 26 utility overlays validated
- `python scripts\smoke_ai_openai_compatible_provider.py` - PASS
- `python scripts\smoke_ai_mapping_review.py` - PASS

Canonical fixture status remains:

- capital_waste - PASS
- spectrum - PASS
- lowes_pro_supply - PASS
- epb - PASS
- tk_elevator - PASS
- servall_pest - SKIPPED with explicit missing-source reason

## 14. Deterministic Utility Regression Status

The deterministic utility routing was not rewritten.

`smoke_utility_processors.py` still validates 26 utility vendor overlays, including U1/U2/U3 active vendors. The new ingestion service does not hijack deterministic processors or make AI the default for utilities.

Weak image/OCR paths remain conservative: weak extraction produces warnings/manual review instead of fake mandatory fields.

## 15. Limitations

- `.xls` legacy Excel is not fully supported.
- `.doc` legacy Word is unsupported.
- OCR quality depends on local OCR availability and source image quality.
- PDF table extraction is limited to available text/layout extraction; complex visual tables may still need vision assist.
- Ingestion preview is diagnostic only and intentionally capped.
- Vision is recommended by ingestion but not automatically invoked.
- Excel and Word ingestion normalize content but do not yet guarantee invoice semantics for every portal export.

## 16. Next Recommended Phase

Recommended next phase:

Phase AI-10 - DocumentCandidate-driven universal reasoning hardening.

Focus areas:

- use normalized tables more deeply in the universal reasoner
- add more fixture-backed portal exports for Excel/CSV/Word
- expand vision trace use for scanned tables
- expose a small ingestion debug panel only when operator diagnostics are needed
- add regression fixtures for weak OCR/image utility bills

