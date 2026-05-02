# Old Scripts — Migration Analysis

**Generated:** 2026-05-01
**Scope:** Inventory of `Old Scripts/` to identify what is worth migrating into the new architecture (`config/`, vendor YAMLs, `utils/`, vendor processors). Old scripts were **read only**; none were modified.

---

## 1. Scripts found

| File | Size | Apparent vendor / purpose | Imports `dropbox` |
| --- | ---: | --- | :---: |
| `Alabama_Power.py` | 43.5 KB | Alabama Power utility-bill PDF parser | ✓ |
| `Apartments.com.py` | 16.8 KB | Apartments.com invoice parser | ✓ |
| `CDE Light Band.py` | 19.4 KB | CDE Lightband (Clarksville electric) | ✓ |
| `CPWS.py` | 18.8 KB | Columbia Power & Water System | ✓ |
| `EPB_Fiber.py` | 54.2 KB | EPB Fiber Optics | ✓ |
| `HWEA Test.py` | 27.0 KB | Hopkinsville Water Environment Authority | ✓ |
| `Hardin CWD2.py` | 20.0 KB | Hardin County Water District No. 2 | ✓ |
| `Henderson Bills.py` | 26.5 KB | The City of Henderson utility bills | ✓ |
| `Nolin REC.py` | 24.2 KB | Nolin RECC (electric coop) | ✓ |
| `Pennyrile Bills.py` | 22.3 KB | Pennyrile Electric | ✓ |
| `Resman.py` | 27.5 KB | Generic ResMan-template filler | ✓ |
| `Shelbyville Power.py` | 20.4 KB | Shelbyville Power System | ✓ |

12 files, ~321 KB total. Every file uses the same Dropbox upload helper pattern.

## 2. What each script appears to do

All 12 scripts share the same broad shape (Spanish-language code comments):

1. Read input bill files from a hardcoded local folder (typically a Dropbox-synced path).
2. Parse vendor-specific PDF / Excel layouts using vendor-specific regex.
3. Determine invoice number, account, GL code, property abbreviation, line amounts.
4. Move/rename the original bill into a "Historic Bills PDFs" folder.
5. Upload the bill file to Dropbox and obtain a shareable link.
6. Open `Output\Template.xlsx` via `xlwings` and write the parsed rows; the Dropbox link goes into the `Document Url` column (named in `KEY_TO_HEADER` as `"document_url" -> "Document Url"`).
7. Save the workbook in place.

The shared `KEY_TO_HEADER` dict in every script points to the same target columns in the ResMan template. Useful for confirming which columns the new processor should populate.

## 3. Useful logic worth migrating

| Source | Where | Migration target |
| --- | --- | --- |
| `subir_a_dropbox_y_obtener_link(ruta_local, nombre_archivo)` (identical in 12 files) | All 12 scripts | **Already migrated** in this update wave to `utils/dropbox_uploader.py` with credentials from environment variables, configurable Dropbox base folder, and structured success/failure response. |
| `KEY_TO_HEADER` mapping (Spanish-key → ResMan column header) | All 12 scripts | Captured implicitly in the new vendor YAML schema (`accounting_mapping`, `invoice_description_rules`, etc.). The 16 keys in the old map all correspond to columns the new processor already writes. |
| Direct-link transformation `?dl=0 → ?dl=1` (forces direct download) | All 12 scripts | Migrated into the new helper as a configurable post-process. |
| "Look up existing shared link first; create one only if none exists" pattern | All 12 scripts | Migrated to the helper to avoid Dropbox API errors when re-uploading. |
| Vendor-specific PDF parsing (Alabama Power, EPB, CDE, etc.) | 11 vendor scripts | Not migrated yet. Recommendation: do per-vendor migrations on demand. Each script's parsing logic is tied to a specific PDF layout and should become its own `process_<vendor>.py` reading from `config/vendors/<vendor>.yaml`. |
| Generic Resman template filler | `Resman.py` | Already superseded by the new project's per-vendor processor + Output\Template.xlsx workflow. Not migrating directly. |

## 4. Hardcoded rules that should move to YAML

The following hardcoded values in old scripts should NOT be ported into Python going forward — they belong in YAML:

| Hardcoded value | Old location | YAML target |
| --- | --- | --- |
| `property_code = "TGAP"` (Alabama Power), `"BCA"` (HWEA), etc. | Module constants in each old script | `vendor_identity.category` + `accounting_source.source_properties_observed` + `location_rules.default_property_abbreviation` (per-vendor YAML) |
| `VENDOR_NAME = "Alabama Power"` | Module constants | `vendor_identity.vendor_name` |
| GL codes embedded in parsing logic | Various inline assignments | `service_gl_mapping.<group>.gl_code` |
| Service grouping keywords | Inline if/else parsing | `service_grouping_rules.output_service_groups.<group>.keywords` |
| Invoice description format strings | f-strings in code | `invoice_description_rules.format` |

Richmond Utilities was already migrated to YAML in earlier waves; these remain TODOs for the other 11 vendors.

## 5. Hardcoded paths

Every old script hardcodes Windows local paths, e.g.:

```
HISTORIC_BILLS_DIR = r"C:\Users\Dasty\Nex-Gen Management Dropbox\Diego Santos\Historic Bills PDFs"
os.environ["OneDriveCommercial"] = r"C:\Users\Dasty\OneDrive - nexgenmgmt.us"
```

The new architecture replaces these with project-relative resolution (`PROJECT_ROOT` is auto-detected from script location), and per-vendor `Bills_Training/` folders. No further migration needed for paths — the new pattern already handles this.

## 6. Dropbox-related code found

All 12 old scripts contain a near-identical function `subir_a_dropbox_y_obtener_link(ruta_local, nombre_archivo)`:

```python
def subir_a_dropbox_y_obtener_link(ruta_local: str, nombre_archivo: str) -> str:
    try:
        ruta_dbx = f"/Diego Santos/Historic Bills PDFs/{nombre_archivo}"
        with open(ruta_local, "rb") as f:
            dbx.files_upload(f.read(), ruta_dbx, mode=dropbox.files.WriteMode("overwrite"))
        res = dbx.sharing_list_shared_links(path=ruta_dbx, direct_only=True)
        if res.links:
            return res.links[0].url.replace("?dl=0", "?dl=1")
        return dbx.sharing_create_shared_link_with_settings(ruta_dbx).url.replace("?dl=0", "?dl=1")
    except Exception:
        return ""
```

Behavior captured in the new helper:

- OAuth2 refresh-token flow (`oauth2_refresh_token`, `app_key`, `app_secret`)
- `WriteMode("overwrite")` (overwrite existing file at the same path)
- "Reuse existing share link before creating a new one" pattern
- `?dl=0 → ?dl=1` rewrite for direct-download links
- Silent failure (`return ""` on any Dropbox error)

The new helper improves on this by:
- Reading credentials from `os.environ` (or an optional `.env` file via `python-dotenv`).
- Returning a structured `UploadResult` so callers can distinguish success / failure / missing-credentials.
- Configurable Dropbox base folder via `DROPBOX_BASE_FOLDER`.
- Configurable per-vendor sub-folder pattern (driven by YAML).
- Explicit graceful degradation when the SDK isn't installed or credentials are absent — the rest of the run still succeeds.

## 7. Security concerns found

> **Real, unredacted Dropbox OAuth credentials are present in every old script.** They were found in `Alabama_Power.py`, `Apartments.com.py`, `CDE Light Band.py`, `CPWS.py`, `EPB_Fiber.py`, `HWEA Test.py`, `Hardin CWD2.py`, `Henderson Bills.py`, `Nolin REC.py`, `Pennyrile Bills.py`, `Resman.py`, `Shelbyville Power.py` — 12 files in total. The exact values are NOT reproduced in this report.

Specifically:

| Variable | Status | Action taken |
| --- | --- | --- |
| `APP_KEY` | FOUND_REDACTED | Not duplicated in this report. User should put it in `.env` only. |
| `APP_SECRET` | FOUND_REDACTED | Same. |
| `REFRESH_TOKEN` | FOUND_REDACTED | Same. |

**Per the task brief, the new project does NOT auto-populate `.env` with the values from the old scripts.** A `.env.example` with placeholder values has been created instead. The user is the only person who should put real values into `.env` (which is gitignored).

Recommendations going forward:

1. **Rotate the Dropbox app secret + refresh token.** Anything that has lived in plain text in source files should be considered exposed. The Dropbox App Console at [dropbox.com/developers/apps](https://www.dropbox.com/developers/apps) lets you regenerate the App Secret; existing refresh tokens can be revoked and re-issued via the OAuth flow.
2. **Once rotated, store new credentials only in `.env`** (which is now in `.gitignore`).
3. **Old scripts** should be left in place per the brief (`Do NOT delete anything`) but considered read-only artifacts. If they ever need to run again, copy the credentials from `.env` into a local-only sidecar — never re-add them to versioned source.

## 8. Recommended migration approach

| Step | Status |
| --- | :---: |
| Build a single reusable `utils/dropbox_uploader.py` | ✅ done in this wave |
| Add `.env.example` + `.gitignore` for `.env` | ✅ done in this wave |
| Wire Richmond Utilities into the new helper | ✅ done in this wave |
| Migrate the next vendor (e.g. Alabama Power) into a `process_alabama_power.py` reading `config/vendors/alabama_power.yaml` and using the same Dropbox helper | TODO — do one vendor at a time |
| Eventually decommission the 12 old scripts (after every vendor is re-implemented in the new architecture) | TODO |

The brief explicitly says **"Do not migrate every old vendor script yet."** Only Richmond Utilities is in scope for this wave.

## 9. Confirmation: no old scripts were modified

All 12 files in `Old Scripts/` were opened with `Read`, `Grep`, and `Bash head` only. No write operations touched the folder. File sizes and timestamps are unchanged from when this wave started.

Verified at the end of this wave:

```
Old Scripts/Alabama_Power.py        43533 bytes   (unchanged)
Old Scripts/Apartments.com.py       16814 bytes   (unchanged)
Old Scripts/CDE Light Band.py       19422 bytes   (unchanged)
Old Scripts/CPWS.py                 18758 bytes   (unchanged)
Old Scripts/EPB_Fiber.py            54237 bytes   (unchanged)
Old Scripts/HWEA Test.py            26955 bytes   (unchanged)
Old Scripts/Hardin CWD2.py          20034 bytes   (unchanged)
Old Scripts/Henderson Bills.py      26517 bytes   (unchanged)
Old Scripts/Nolin REC.py            24219 bytes   (unchanged)
Old Scripts/Pennyrile Bills.py      22346 bytes   (unchanged)
Old Scripts/Resman.py               27552 bytes   (unchanged)
Old Scripts/Shelbyville Power.py    20425 bytes   (unchanged)
```
