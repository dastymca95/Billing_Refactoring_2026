# Billing Refactoring 2026

A vendor-aware bill processing platform for property management. Reads utility bills (digital PDFs, scanned PDFs, CSV/XLSX exports), runs vendor-specific YAML rules + OCR + General Ledger evidence, and produces a ResMan-ready import workbook.

The system has two faces:

* **CLI processors** — one per vendor, batch-runnable from the command line.
* **Web app** — a property-manager-friendly workspace for uploading, processing, reviewing, and exporting batches.

The web app and the CLI share the same vendor processor code; what differs is the wrapper around it (interactive UI vs scripted run).

---

## What the app does

For each batch of uploaded bills:

1. Detects the vendor from filename + content.
2. Extracts text via `pdfplumber` (digital) or Tesseract OCR (scanned).
3. Applies the vendor's YAML rules — regex banks for service codes, account numbers, dates, totals.
4. Matches the bill's service address to `Properties/Unit Info Clean.csv` for property abbreviation + unit.
5. Falls back to `Gl Codes/General Ledger Report.csv` evidence when the address is ambiguous.
6. Reconciles bill totals (line items must equal the bill total within tolerance).
7. Splits multi-bill scanned PDFs into per-bill support documents.
8. Uploads support documents to Dropbox (when configured) and writes shareable URLs into the export.
9. Builds a fresh copy of `Output/Template.xlsx` with the extracted invoice rows.

A web-app operator can then edit any cell, mark issues reviewed, and download the final Excel.

---

## Supported vendors

| Vendor key | Status |
| --- | --- |
| `richmond_utilities` | ✅ Production |
| `hopkinsville_water_environment_authority` | ✅ Production (digital, scanned, late-notice scans) |

Adding a new vendor: see [`docs/architecture/CONFIG_SOURCE_OF_TRUTH_REPORT.md`](docs/architecture/CONFIG_SOURCE_OF_TRUTH_REPORT.md).

---

## Project layout

```
.
├── README.md                            ← you are here
├── requirements.txt                     ← Python deps (FastAPI, pypdf, pdfplumber, etc.)
├── docker-compose.yml                   ← `docker compose up` to run the web app
├── webapp/Dockerfile                    ← backend container
├── webapp/frontend/Dockerfile           ← frontend container (vite preview)
├── .env.example                         ← copy to .env for Dropbox / AI keys
├── .gitignore
│
├── webapp/                              ← FastAPI backend + React frontend
│   ├── backend/
│   │   ├── api/                         ← REST endpoints
│   │   ├── services/                    ← batch_processor, ai_fallback, vendor_detection
│   │   ├── settings.py                  ← project paths
│   │   └── main.py                      ← `uvicorn webapp.backend.main:app`
│   ├── frontend/                        ← Vite + React + TS + pdfjs-dist
│   ├── README_WEBAPP.md                 ← detailed web app docs
│   └── (webapp_data/ gitignored)        ← per-batch uploads & cached results
│
├── config/                              ← YAML source of truth
│   ├── vendor_rules_index.yaml
│   ├── vendors/<vendor>.yaml
│   └── ai_fallback_rules.yaml           ← AI assist policy (disabled by default)
│
├── utils/                               ← shared helpers
│   ├── pdf_text_extractor.py            ← pdfplumber + Tesseract pipeline
│   ├── pdf_splitter.py                  ← per-page split for multi-bill PDFs (Phase 1I long-path fix)
│   ├── dropbox_uploader.py              ← env-only auth (refresh-token flow preferred)
│   ├── service_period_resolver.py
│   ├── progress_tracker.py              ← progress.json for the web app
│   └── location_validator.py            ← (property, unit) validation against Unit Info Clean
│
├── Training Bills_Invoices/             ← training bills (gitignored — real customer data)
│   └── Water - Sewer/<Vendor>/
│       ├── Bills_Training/              ← drop new bills here for training
│       ├── process_<vendor>.py          ← CLI processor for that vendor
│       └── README_<VENDOR>.md
│
├── Properties/Unit Info Clean.csv       ← canonical (property, unit) source
├── Gl Codes/General Ledger Report.csv   ← GL evidence for address resolution
├── Vendors/Vendor List.csv              ← canonical vendor names
├── Output/Template.xlsx                 ← canonical ResMan template (NEVER modified)
│
└── docs/
    ├── DOCKER_WEBAPP_README.md          ← Docker / docker-compose details
    ├── DROPBOX_INTEGRATION_README.md    ← Dropbox setup + token rotation
    ├── architecture/                    ← plans, source-of-truth maps, migration notes
    └── reports/
        ├── phases/                      ← per-phase implementation reports (1B–1L)
        └── vendors/                     ← vendor-specific implementation / fix reports
```

---

## Running locally

### Backend (Python)

```bash
python -m venv .venv
.\.venv\Scripts\activate            # PowerShell
# or: source .venv/bin/activate     # bash

pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn webapp.backend.main:app --reload --port 8000
```

The Python requirements include `pytest` and `httpx`; both are required for
backend unit tests and FastAPI `TestClient` contract tests. Run them with:

```bash
python -m pytest
```

### Frontend (React + Vite)

```bash
cd webapp/frontend
npm install
npx playwright install chromium
npm run dev          # http://localhost:5173 (proxies /api/* to backend)
```

Chromium is a separate Playwright runtime dependency and is not installed by
`npm install`. After installing it, run the browser suite with
`npm run test:e2e`. In CI/Linux, use `npx playwright install --with-deps chromium`
so the required system libraries are installed as well.

### Accounting-readiness export safety

Every web export is authorized by the versioned backend
`AccountingReadiness` decision. The historical behavior that copied an
already-generated vendor workbook directly into the batch export directory
has been **retired**. It bypassed row validation and invoice reconciliation.

Do not restore legacy workbook copying. A legacy batch must either be rebuilt
from its cached rows through the current readiness gate or return
`legacy_export_disabled` and be reprocessed. This applies even when the old
workbook appears structurally valid.

### Docker

```bash
copy .env.example .env       # PowerShell / cmd
cp   .env.example .env       # bash
docker compose up
```

See [`docs/DOCKER_WEBAPP_README.md`](docs/DOCKER_WEBAPP_README.md) for the full Docker walkthrough.

### CLI

Run a single vendor's processor directly (used during development and for unattended batch runs):

```bash
python "Training Bills_Invoices/Water - Sewer/Richmond Utilities/process_richmond_utilities.py"
python "Training Bills_Invoices/Water - Sewer/Hopkinsville Water Environment Authority/process_hopkinsville_water_environment_authority.py"
```

The CLI reads bills from each vendor's `Bills_Training/` folder and writes results to `Processed_Output/`. Source PDFs are never modified.

---

## Where the data lives

| Path | Read | Write | Notes |
| --- | --- | --- | --- |
| `Output/Template.xlsx` | ✅ | ❌ | Canonical ResMan template. Copies are written to `Processed_Output/` and `webapp_data/.../export/`. The original is never modified. |
| `Properties/Unit Info Clean.csv` | ✅ | ❌ | Canonical (property, unit) source. |
| `Gl Codes/General Ledger Report.csv` | ✅ | ❌ | Used for property-from-account-number evidence mining. |
| `Vendors/Vendor List.csv` | ✅ | ❌ | Canonical vendor names. |
| `config/vendors/<vendor>.yaml` | ✅ | rare | Vendor rules (regex banks, GL mapping, manual review triggers). |
| `Training Bills_Invoices/<Vendor>/Bills_Training/` | ✅ | ❌ | Training PDFs. |
| `Processed_Output/` (under each vendor folder) | ✅ | ✅ | CLI outputs (workbook, debug CSV, log). Gitignored. |
| `webapp_data/batches/<id>/` | ✅ | ✅ | Per-batch uploads, processing cache, progress.json, region_hints.json, exports. Gitignored. |

---

## Configuration & secrets

`config/vendor_rules_index.yaml` and `config/vendors/*.yaml` are the source of truth for vendor behaviour. See [`docs/architecture/CONFIG_SOURCE_OF_TRUTH_REPORT.md`](docs/architecture/CONFIG_SOURCE_OF_TRUTH_REPORT.md).

Secrets live exclusively in `.env` (gitignored). Required keys:

- `DROPBOX_REFRESH_TOKEN` + `DROPBOX_APP_KEY` + `DROPBOX_APP_SECRET` *(preferred)* or `DROPBOX_ACCESS_TOKEN`.
- `DROPBOX_BASE_FOLDER` (defaults to `/Billing_Refactoring_2026`).
- `AI_FALLBACK_ENABLED` (default `false`), `AI_PROVIDER` (default `disabled`), and the matching `<PROVIDER>_API_KEY` if you wire AI on.

The web app's AI fallback is **disabled by default** — no provider call is ever made unless an operator explicitly enables it. The status pill in the topbar reads `AI Off` when disabled.

---

## Documentation

- **[webapp/README_WEBAPP.md](webapp/README_WEBAPP.md)** — full web app guide (UX, endpoints, troubleshooting, all phases).
- **[docs/DOCKER_WEBAPP_README.md](docs/DOCKER_WEBAPP_README.md)** — Docker / docker-compose setup and env vars.
- **[docs/DROPBOX_INTEGRATION_README.md](docs/DROPBOX_INTEGRATION_README.md)** — Dropbox token rotation + folder layout.
- **[docs/architecture/](docs/architecture/)** — phase plans, config source-of-truth maps, migration notes.
- **[docs/reports/phases/](docs/reports/phases/)** — per-phase implementation reports (Phase 1B–1M, plus the Resizer bug-fix).
- **[docs/reports/vendors/](docs/reports/vendors/)** — vendor-specific implementation / fix / audit reports.

---

## Safety

- Source bills, the canonical `Output/Template.xlsx`, `Properties/Unit Info Clean.csv`, GL files, and `Vendors/Vendor List.csv` are read-only at runtime — every code path has been audited and verified across every phase report.
- AI fallback is off by default. Enabling it never overrides validated Unit Info Clean / GL evidence / bill-total reconciliation.
- `.env` is gitignored; API keys are read at process start from `os.environ` and never serialised in any HTTP response.

---

## Contributing / development workflow

- Vendor logic lives in `Training Bills_Invoices/<Vendor>/process_<vendor>.py` + `config/vendors/<vendor>.yaml`. The web app calls these via `webapp/backend/services/batch_processor.py`.
- New vendor? Start from `Training Bills_Invoices/Water - Sewer/Richmond Utilities/` as a template, add a YAML to `config/vendors/`, and register it in `_PROCESSOR_LOADERS` in `batch_processor.py`.
- Each significant change ships a phase report under `docs/reports/phases/` with verification (CLI regression, source-file SHA-256s, secret hygiene). See the existing reports for the format.

For repository details and issues, see the linked GitHub repo.
