# Phase AI-6 - Configurable Output Rules Studio

Date: 2026-05-12

## Scope

Added an operator-facing configuration stage for required ResMan output fields:

- Invoice Number
- Invoice Description
- Line Item Description

The goal is to let managers change formatting rules later from the web app instead of changing Python code.

## What Was Added

### Backend Rule Engine

Added `webapp/backend/services/invoice_format_rules.py`.

The engine supports:

- General rules for any bill/invoice.
- Vendor-specific rules.
- Vendor-group rules.
- GL-code-specific rules.
- GL-group rules.
- Property-specific rules.
- Property-group rules.
- Rule priority.
- Document type targeting: `bill`, `invoice`, or `any`.
- Template variables such as:
  - `{account_number}`
  - `{invoice_date_yyyymmdd}`
  - `{service_period_range}`
  - `{service_period_start_month3_upper}`
  - `{service_period_end_year2}`
  - `{vendor_name}`
  - `{property_abbreviation}`
  - `{service_address_or_property}`
  - `{gl_account}`
  - `{gl_name}`
  - `{line_item_description}`

Rules are stored in `config/invoice_format_rules.yaml` and are written atomically with backups in `config/.backups/invoice_format_rules/`.

### Backend API

Added:

- `GET /api/invoice-format-rules`
- `PUT /api/invoice-format-rules`
- `POST /api/invoice-format-rules/preview`

The API returns sorted reference data for:

- Vendors
- GL accounts
- Properties

### AI Processing Integration

AI-assisted invoice processing now calls the rule engine when composing:

- Required/generated invoice number.
- Invoice description.
- Line item description.

Existing deterministic vendor processors remain untouched.

### Frontend Stage

Added a new sidebar stage:

- `Formats`

The stage includes:

- Rule list.
- Rule editor.
- Scope targeting.
- Template presets.
- Live preview with sample bill data.
- Variable library.
- Vendor/GL/property reference library.
- Vendor/GL/property group editor.

## Example Use Case

To implement:

`account number + first 3 letters of service month + last 2 digits of service year`

Use this invoice number template:

```text
{account_number}-{service_period_start_month3_upper}{service_period_end_year2}
```

Example:

```text
040582701-01-MAR26
```

## Verification

Commands run:

```powershell
python -m compileall webapp\backend
python scripts\verify_backend_routes.py
cd webapp\frontend
npm.cmd run build
npx.cmd tsc --noEmit
```

Browser verification:

- Opened `http://localhost:5174/`.
- Confirmed `Formats` appears in the sidebar.
- Confirmed rules load from backend.
- Confirmed live preview renders invoice number and service-period descriptions.

## Known Limitations

- The UI now edits rule groups, but it does not yet provide drag-and-drop grouping.
- Rules apply to AI-assisted composition first. Deterministic vendor processors remain protected and unchanged.
- A backend restart is required after adding new API routes to an already-running server.

## Next Recommended Phase

Add a "rule impact preview" that tests a proposed format rule against an existing QA batch before saving it.
