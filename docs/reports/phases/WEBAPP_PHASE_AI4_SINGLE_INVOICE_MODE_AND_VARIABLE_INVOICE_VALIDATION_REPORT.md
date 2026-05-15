# Phase AI-4 - Single Invoice Mode and Variable Invoice Validation Hardening

Date: 2026-05-10

## 1. Single Invoice Mode Architecture

Added a second Template workspace visualization mode:

- Bulk mode: existing ResMan grid for batch-wide review and export.
- Single invoice mode: one invoice group at a time, with invoice header fields, property/context, review reasons, and line items.

The mode is frontend UI state only. It does not alter backend data by itself.

Invoice groups are derived from row metadata when available, then by source file, invoice number, and vendor/detected vendor. This keeps the mode compatible with deterministic rows and AI-assisted rows.

## 2. Bulk vs Single Mode Behavior

Bulk mode remains the default export review grid.

Single invoice mode includes:

- Invoice header fields: Vendor, Invoice Number, Bill or Credit, Invoice Date, Due Date, Accounting Date, Invoice Description, Total, Status.
- Property/context panel: Property Abbreviation, Location, AI-captured service address, AI confidence, validation flags.
- Manual review block with human-readable reasons.
- Line item table: Property, Location, GL Account, Description, Unit Price, Quantity, Total, Tax, Expense Type, Replacement Reserve.
- Previous/Next invoice navigation.
- Open source document and Show trace actions.
- Return to bulk mode action.

Edits made in single invoice mode call the same `onCellEdit` path used by the bulk grid. Header edits are applied across all rows in the active invoice group; line-item edits update the specific row. Returning to bulk mode shows the edited values immediately.

## 3. AI Validation Hardening

Hardened `webapp/backend/services/ai_invoice_processor.py` so AI output is treated as extraction candidates, not trusted row data.

New/updated validation rules:

- Vendor must match `Vendors/Vendor List.csv`; otherwise `vendor_mapping_required`.
- Property abbreviation must be confirmed from known property/unit sources; otherwise `property_mapping_required`.
- Location is never populated from a raw full address.
- Location is only populated from known unit/location references.
- Raw service address is preserved in metadata, not written into the ResMan Location column.
- GL Account must be a valid numeric GL code from the GL reference.
- Vendor-side text such as `MISCELLANEOUS` is preserved as source metadata, but not written to `GL Account`.
- Sales tax defaults to `manual_review` handling and creates `tax_handling_requires_review`.
- Zero-dollar line items are excluded by default and flagged with `zero_amount_line_excluded`.
- Invoice date source priority is explicit invoice date, purchase date, ship date, then received date.
- If a non-invoice-date source is used, review is flagged, e.g. `invoice_date_inferred_from_purchase_date`.

## 4. GL Mapping Changes

AI-suggested GL text now passes through `ai_mapping_review.validate_gl_account`.

If valid:

- The row receives only the numeric GL code.
- GL name is retained in normalized metadata.

If invalid:

- The ResMan `GL Account` value is blank.
- Source text is preserved in `_meta.ai_source_gl_candidate`.
- The row is flagged with `gl_mapping_required`.

The existing AI mapping review UI now recognizes both legacy `ambiguous_gl_mapping` and new `gl_mapping_required` flags.

## 5. Property and Location Handling

Property validation now resolves against property/unit reference rows. Exact property abbreviation, exact property name, or exact known address can confirm the property.

Location handling is stricter:

- Confirmed unit/location values can flow to `Location`.
- Raw addresses cannot flow to `Location`.
- AI-captured service address is shown in Single Invoice Mode under Property / Context.
- Unknown property or weak AI property guesses are left blank and flagged for review.

## 6. Tax Handling Policy

Added backend config:

```env
AI_TAX_HANDLING=manual_review
AI_INCLUDE_ZERO_AMOUNT_LINES=false
```

Default tax policy is `manual_review`.

In this phase, nonzero tax does not silently create a blank-GL payable line. The row set is flagged so the operator can decide whether to distribute tax, map a separate tax GL, or handle it manually before export.

## 7. Zero-Dollar Line Policy

Default policy excludes zero-dollar line items from payable ResMan rows.

Excluded lines create a review flag:

- `zero_amount_line_excluded`

This avoids creating payable rows for promotional or informational source lines such as `$0.00` app/download lines.

## 8. Description Composition Changes

AI-assisted invoice descriptions are now deterministically composed after extraction.

Composition uses:

- invoice date or inferred date
- validated vendor name, or detected vendor as display fallback
- confirmed property abbreviation when available
- concise line-item description when useful

Example shape:

```text
05/06/26 - Lowe's Pro Supply - OC-CCA - 3-3/4-In bar pull
```

Generic AI text such as `Hardware and miscellaneous items` is no longer blindly used as the ResMan invoice description.

Rows are marked with `_meta.ai_generated_description=true`.

## 9. Tests Performed

Frontend:

- `npm.cmd run build` - passed.
- `npx.cmd tsc --noEmit` - passed.
- `npm.cmd run test:e2e` - passed: 19 passed, 2 skipped due unavailable optional local fixtures.

Backend:

- `python -m compileall webapp\backend` - passed.
- `python scripts\verify_backend_routes.py` - passed.
- `python scripts\smoke_ai_openai_compatible_provider.py` - passed.
- `python scripts\smoke_ai_mapping_review.py` - passed.

Added/expanded validation coverage:

- AI invoice with text GL `MISCELLANEOUS` does not output `GL Account=MISCELLANEOUS`.
- Full address is not placed in `Location`.
- Unknown property triggers `property_mapping_required`.
- Unknown vendor triggers `vendor_mapping_required`.
- Zero amount line is excluded and flagged.
- Tax without a validated handling policy is flagged.
- Purchase-date fallback flags `invoice_date_inferred_from_purchase_date`.
- Single Invoice Mode renders invoice header and line items.
- Editing a Single Invoice field updates the bulk grid.

Integrity:

- `Output/Template.xlsx` unchanged.
- `Training Bills_Invoices` unchanged.
- `.env` unchanged.
- `Vendors` source files unchanged.
- No Dropbox calls were made.
- No unnecessary real AI calls were added to automated tests.

## 10. Remaining Limitations

- Single Invoice Mode is a modern web-console view inspired by ResMan entry screens; it is not a pixel clone of ResMan.
- Unit price and quantity are not currently first-class ResMan preview columns, so they render as placeholders unless future row metadata adds those values.
- Tax handling is intentionally conservative in this phase. A future phase should add operator controls for distributing tax or assigning a dedicated tax GL.
- Property matching is exact/reference-based to avoid unsafe guesses. Fuzzy property candidate review can be added later with explicit operator confirmation.
- Single Invoice edits are local preview edits until the operator saves the current revision or exports through the normal workflow.

## 11. Next Recommended Phase

Recommended Phase AI-5:

- Add a dedicated tax handling control in Single Invoice Mode.
- Add property/location candidate review similar to vendor/GL review.
- Add Unit Price and Quantity as preserved AI metadata and editable line-item fields.
- Add cell-level provenance popovers for AI-generated description, inferred invoice date, and excluded zero-dollar lines.
- Add a batch-level “AI review complete” status once vendor, property, location, GL, tax, and date flags are resolved.
