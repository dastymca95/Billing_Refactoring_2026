# Dropbox Integration

This document explains how vendor billing scripts upload support documents (the original utility bills or billing-history files) to Dropbox and how the resulting shareable link is written into the ResMan import template.

## Why

Every vendor's ResMan AP import row needs a link to the original document so the AP team can audit the charge later. The legacy approach (in `Old Scripts/`) hardcoded Dropbox tokens directly into each vendor's `.py` file. The new approach centralizes that logic in a single reusable helper and reads credentials from environment variables.

## Folder layout

```
project root/
├── utils/
│   └── dropbox_uploader.py        ← the reusable helper
├── .env.example                   ← copy this to .env and fill in real values
├── .env                           ← your real secrets (gitignored, never committed)
└── .gitignore                     ← blocks .env from accidentally getting committed
```

## Required environment variables

Two authentication modes are supported. Use **one** of them.

### Mode A — OAuth2 refresh token (preferred)

```
DROPBOX_APP_KEY=...
DROPBOX_APP_SECRET=...
DROPBOX_REFRESH_TOKEN=...
```

The refresh token does not expire; it's the safest option. All three values come from your Dropbox app at https://www.dropbox.com/developers/apps.

### Mode B — Long-lived access token (legacy)

```
DROPBOX_ACCESS_TOKEN=...
```

Older Dropbox apps use a single long-lived access token. The helper accepts this for compatibility, but the OAuth refresh-token flow is recommended.

### Optional

```
DROPBOX_BASE_FOLDER=/Billing_Refactoring_2026
```

Where uploads should land. Defaults to `/Billing_Refactoring_2026`. Vendor scripts append a per-vendor sub-folder underneath.

## Setting up `.env`

```bash
# from the project root
cp .env.example .env
# edit .env in your editor and paste your real values
```

`.env` is in `.gitignore`, so it stays local. **Never commit `.env`.**

If you don't yet have Dropbox credentials, the vendor scripts still run — uploads are just skipped, the link column is left blank, and each affected row is flagged `dropbox_credentials_missing` for manual review.

## How a vendor script calls the helper

```python
from utils.dropbox_uploader import DropboxUploader, build_dropbox_path

uploader = DropboxUploader.from_env(logger=my_logger)
if uploader.is_configured:
    dst = build_dropbox_path(
        base_folder=uploader.base_folder,
        vendor_name="Richmond Utilities",
        billing_date=billing_date,
        filename=path.name,
    )
    result = uploader.upload(local_path=path, dropbox_path=dst, overwrite=True)
    if result.success:
        write_link_into_template(result.shared_link)
    else:
        # graceful degradation; flag the row for manual review
        ...
else:
    # credentials missing — flag for review, leave link blank
    ...
```

The helper:

- Returns an `UploadResult` dataclass — never raises.
- Reuses an existing shared link before creating a new one (so re-runs don't pile up duplicate share links).
- Rewrites `?dl=0` → `?dl=1` so links are direct-download URLs.
- Logs only redacted token info (e.g. `kri…(len=15,REDACTED)`) — never the full secret.

## Where the link goes in the ResMan template

The helper returns a URL; the **vendor script** writes it into the template. By convention the support-document URL goes into the **last non-empty column** of the template's header row, or — if the template has a column literally named `Document Url` — that exact column. The vendor's YAML controls which strategy is used. For Richmond Utilities, the YAML block is:

```yaml
support_document_rules:
  enabled: true
  upload_provider: "dropbox"
  source_file_to_upload: "original_input_file"
  template_link_column_strategy: "last_non_empty_header_column"
  expected_link_column_header: "Document Url"
  same_link_for_all_rows_generated_from_same_file: true
  failure_behavior:
    leave_link_blank: true
    manual_review_reason: "dropbox_upload_failed"

dropbox_rules:
  enabled: true
  base_folder_env_var: "DROPBOX_BASE_FOLDER"
  base_folder_default: "/Billing_Refactoring_2026"
  vendor_folder: "Richmond Utilities"
  folder_pattern: "{base_folder}/{vendor_name}/{year}/{month_number} - {month_abbrev}"
  filename_strategy: "original_filename"
  overwrite_existing: true
  create_shared_link: true
```

## How to disable Dropbox upload

Set `support_document_rules.enabled: false` in the vendor's YAML. The script will skip the upload entirely and leave the link column blank — no manual-review flag. Use this only if the AP team accepts AP imports without support links.

## Failure modes

| Situation | Behavior | Manual review reason |
| --- | --- | --- |
| `dropbox` SDK not installed | Helper returns `success=False, error_kind=sdk_missing`. Upload skipped. | `dropbox_upload_failed` |
| No credentials in env | Helper not configured; upload skipped. | `dropbox_credentials_missing` |
| Auth token expired / revoked | API auth error. Upload skipped. | `dropbox_upload_failed` |
| Network / API error | Upload skipped. | `dropbox_upload_failed` |
| Local file doesn't exist | Helper returns `error_kind=io`. | `dropbox_upload_failed` |

In every failure mode, the rest of the AP run continues normally. The only effect is that the link column for affected rows is left blank.

## Security warnings

1. **Never commit `.env`.** It's in `.gitignore`. Double-check before any push.
2. **Never paste real tokens into reports, logs, or doc files.** The helper logs redacted summaries only.
3. **Rotate any token that has been in plain text source.** Tokens previously embedded in `Old Scripts/` should be considered exposed; revoke and re-issue them in the Dropbox App Console before continuing to use the helper in production.
4. **Don't grant the Dropbox app full Dropbox access if scoped access works.** This project only writes into `DROPBOX_BASE_FOLDER` and creates shared links — those are the only scopes the helper needs.

## Where to look when something goes wrong

1. The vendor script's processing log — every Dropbox event is logged with redacted credentials.
2. The manual-review workbook for the run — every flagged invoice lists its reasons in plain text.
3. The Dropbox App Console — to confirm the app's scopes are still valid and the refresh token hasn't been revoked.
