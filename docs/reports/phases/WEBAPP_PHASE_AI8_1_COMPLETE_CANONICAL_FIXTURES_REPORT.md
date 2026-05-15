# Phase AI-8.1 — Complete Remaining Canonical Fixtures

Date: 2026-05-13  
Project: Billing Refactoring Web Console

## 1. Fixtures completed

### TK Elevator

Status: complete, passing.

The TK Elevator fixture now uses a stable cached extraction payload for invoice `7000080162`. It verifies:

- vendor: `TK Elevator Corporation`
- category: `other_infrequent`
- bill/credit: `Bill`
- invoice date: `08/27/2025`
- due date: `08/27/2025`
- account/customer reference: `8039646-229260`
- property: `HP-Hogs`
- location: blank, because no exact unit/location is present
- GL account: `6530`
- line items:
  - `Governor switch tripped adjusted governor - labor` for `$637.50`
  - `Trip charge` for `$95.00`
- total: `$732.50`
- expected review flag: `location_unresolved`

### EPB Fiber Optics

Status: complete, passing.

The EPB fixture now uses a stable cached extraction payload for the scanned EPB bill. It verifies:

- vendor: `EPB Fiber Optics`
- category: `utilities`
- source invoice number: `18547827`
- final invoice number: `C10181446 May 26`, from the current utility invoice-number formatting policy
- account number: `C10181446`
- invoice date: `05/07/2026`
- due date: `05/22/2026`
- service period: `05/08/26-06/07/26`
- property: `RCC`
- location: blank, because no valid unit is present
- GL account: `6139` for EPB internet/fiber service
- line items:
  - `Fi-Speed Internet` for `$67.98`
  - `Tax and Surcharges` for `$0.97`
- total: `$68.95`
- ignored source items: previous balance and payments
- expected review flags: `invoice_number_formatted_from_policy`, `location_unresolved`

## 2. Fixtures still skipped

### Servall Pest

Status: incomplete, skipped with explicit reason.

Servall remains skipped because the only available sample is a low-resolution conversation screenshot. The invoice number, invoice date, due date, property, total, and exact Servall entity cannot be verified reliably enough for a golden regression fixture. Vendor matching is also ambiguous between `Servall Termite & Pest Control (Clarksville)` and `Servall, LLC (Martin)`.

To complete this fixture safely, add a stable source document or a cached OCR/vision candidate JSON for the exact Servall invoice.

## 3. Expected behavior by vendor

Capital Waste, Spectrum, Lowe’s Pro Supply, EPB, and TK Elevator now exercise different canonical behaviors:

- Capital Waste verifies trash-service current-charge handling, property-level blank location, GL `6940`, and ignored payments/balances.
- Spectrum verifies subscription/cable-internet charges, zero-dollar exclusion, property `AMA`, and GL `6905`.
- Lowe’s Pro Supply verifies variable supplier invoices, tax handling, numeric GL-only behavior, and zero-dollar line exclusion.
- EPB verifies utility invoice-number formatting policy, internet GL mapping, current-charge extraction, and balance/payment exclusion.
- TK Elevator verifies variable service invoice handling, property matching, blank location, GL mapping, and service-line totals.

## 4. Category-specific lessons

- Utility fixtures must honor active output-format policy. EPB intentionally expects account-plus-period invoice number output.
- Utility current charges must ignore previous balance and payments.
- Variable service invoices can pass with review flags when optional location remains unresolved.
- Golden fixtures must use stable cached candidates, not live AI responses.
- Incomplete fixtures must explain why they are skipped, so regressions do not hide behind silent skips.

## 5. Smoke results

`python scripts\smoke_canonical_invoice_fixtures.py`

- `capital_waste`: PASS
- `spectrum`: PASS
- `lowes_pro_supply`: PASS
- `epb`: PASS
- `tk_elevator`: PASS
- `servall_pest`: SKIPPED with explicit missing-source reason

Additional validation performed:

- `cd webapp/frontend && npm.cmd run build`: PASS
- `cd webapp/frontend && npx.cmd tsc --noEmit`: PASS
- `cd webapp/frontend && npm.cmd run test:e2e`: PASS, 23 passed, 1 existing skip
- `python -m compileall webapp\backend`: PASS
- `python scripts\verify_backend_routes.py`: PASS
- `python scripts\smoke_canonical_rules_engine.py`: PASS
- `python scripts\smoke_canonical_rules_studio.py`: PASS

## 6. Limitations

- Servall is not complete because a reliable source/cached payload is still missing.
- Fixture tests do not call real AI, real Dropbox, or export. They validate the canonical pipeline from cached extraction candidates into normalized rows.
- EPB’s expected invoice number is tied to the currently configured utility formatting rule. If that rule changes, the fixture expectation should be intentionally updated.

## 7. Next recommended phase

Add a stable Servall source fixture and expand the golden library with at least one scanned pest-control invoice, one HVAC/service invoice, and one ambiguous vendor mapping case with expected manual review outcomes.
