# WEBAPP Phase QA-2 - Utility Line Classification, Fee GL Mapping, Fire Service Handling, and Weakley OCR Improvement

Date: 2026-05-14

## 1. Vendor-by-vendor GL classification audit

QA-2 audited the 26 active deterministic utility overlays and the representative U4 browser fixtures. The main systemic risk was that a vendor default GL could be applied before special line-item categories were classified. That made fees, trash/sanitation, and fire-related service lines vulnerable to being swallowed by normal electric/water/gas mappings.

| Vendor | Source line description / family audited | Current QA-2 classification | Expected GL behavior | Status |
|---|---:|---|---|---|
| Alabama Power | electric service | electric_service | electric utility GL from vendor/canonical rules | Pass |
| Atmos Energy Auto Pay | gas service | gas_service | gas utility GL from vendor/canonical rules | Pass |
| Birmingham Water Works | water / sewer / fees | water_service / sewer_service / fee | water/sewer GL unless special fee rule applies | Pass |
| CDE Lightband | electric / internet-style utility lines | electric_service / internet_fiber_service | vendor utility GL by source service | Pass |
| City of Chattanooga Wastewater Department | wastewater service | wastewater_service | water/sewer/wastewater GL | Pass |
| City of Martin | utility service | electric_service or water_service by source | vendor utility GL | Pass |
| City of McMinnville Water/Sewer | water / sewer | water_service / sewer_service | water/sewer GL | Pass |
| City of Union City | utility service | electric_service or water_service by source | vendor utility GL | Pass |
| Clarksville Gas and Water | gas / water / sewer | gas_service / water_service / sewer_service | service-specific GL | Pass |
| Columbia Power and Water System | electric / water / sewer | electric_service / water_service / sewer_service | service-specific GL | Pass |
| EPB Fiber Optics | fiber / internet service | internet_fiber_service | internet/fiber/cable utility GL | Pass |
| Guardian Water and Power | water / power | water_service / electric_service | service-specific GL | Pass |
| Hardin County Water District No. 2 | water service | water_service | water/sewer GL | Pass |
| Hopkinsville Electric System | electric + connect fee | electric_service / connection_fee | connection fee separate GL 6956 | Pass |
| HWEA | water/sewer/sanitation/connect fee | water_service / sewer_service / trash_service / connection_fee | sanitation GL 6940; connect fee GL 6956 | Pass |
| Kentucky Utilities | master electric billing | electric_service | master invoice GL remains electric | Pass |
| Knoxville Utility Board | master utility billing | service-specific rows | master rows stay sequential and reconciled | Pass |
| McMinnville Electric System | electric service | electric_service | electric utility GL | Pass |
| Nolin RECC Smarthub | community electric billing | electric_service | community grouping preserved | Pass |
| Pennyrile Electric | electric service | electric_service | electric utility GL | Pass |
| Richmond Utilities | water/sewer/sanitation | water_service / sewer_service / trash_service | sanitation GL 6940; no tax-only rows | Pass |
| Shelbyville Power System | electric service | electric_service | electric utility GL | Pass |
| Tennessee American Water | fire protection / water | fire_protection_service | fire service GL 6860, not normal water GL 6955 | Pass |
| The City of Henderson | service + fee/tax lines | service-specific; taxes allocated | no standalone taxes | Pass |
| Union City Energy Authority | electric utility | electric_service | electric utility GL | Pass |
| Weakley County Municipal Electric | image electric bill | deterministic image route; weak OCR review | valid rows only if mandatory fields extract, otherwise blocking review | Pass |

## 2. Connection / reconnection fee fix

Connection-style keywords are now classified before normal utility service matching. Keywords include connect fee, connection fee, reconnection fee, reconnect fee, recon chg, service connection, turn on fee, and activation fee in utility context.

Result:
- Connection/reconnection rows are separate line items.
- GL is forced to `6956`.
- Taxes are not allocated into connection fee rows.
- A connection fee with any non-`6956` GL is a blocking contract failure.

HWEA also had a display-level issue: the grouped line description was `Connect fee / Balance forward`, which looked like a balance-forward expense. It is now rendered as `Connect Fee`.

## 3. Late fee rule verification

Late fee keywords are classified before normal utility service matching but after connection fees. Late fees are explicitly blocked from using GL `6956`.

Result:
- Late fee with `6956` fails validation.
- Late fee uses the underlying/vendor default service GL when safe, or review if ambiguous.

## 4. Fire service classification fix

Tennessee American Water can include fire detection/protection/private fire service lines. QA-2 added a separate `fire_protection_service` category before normal water classification.

Result:
- Fire service no longer maps to regular water.
- Tennessee American Water uses configured GL `6860` for fire service.
- If future water vendors expose fire service without a configured GL, validation can block with fire-service GL review instead of guessing.

Verified sample:
- `1026-210052442136 Apr 26.pdf`
- Row: `Fire Protection Service`
- GL: `6860`
- Amount: `1006.85`

## 5. Trash / sanitation service handling

Some water vendors include sanitation/trash lines. QA-2 moved trash/sanitation classification ahead of generic water/wastewater matching.

Result:
- Sanitation/trash service maps to GL `6940`.
- Sanitation/trash is not treated as water/sewer only because the vendor is a water vendor.
- Validator flags trash/sanitation descriptions mapped to a non-trash GL.

## 6. Classifier priority changes

The shared classifier now uses this priority:

1. payment / previous balance exclusions
2. connection/reconnection fee
3. late fee
4. tax/fee lines
5. fire protection service
6. trash/sanitation service
7. specific utility service type
8. other fee / unknown

The classifier returns a structured result with classification, confidence, matched keywords, reason, GL strategy, tax behavior, separate-line behavior, and manual review flags.

## 7. Canonical rules and YAML changes

Updated:
- `config/canonical_rules.yaml`
- `config/vendors/tennessee_american_water.yaml`
- `config/vendors/hopkinsville_water_environment_authority.yaml`

Canonical utility rules now explicitly include:
- connection fee forced GL `6956`
- late fee not GL `6956`
- fire protection service keywords and GL behavior
- trash/sanitation keywords and GL `6940`
- priority order for line classification

## 8. Weakley OCR / image handling

Weakley image/scanned bills still have weak OCR in some files, but QA-2 improves the pipeline and avoids fake output.

Changes:
- Image OCR now runs multiple preprocessing variants: contrast, upscale/sharpen, threshold, and alternate page segmentation modes.
- Ingestion records the OCR variant and low-quality warning.
- Weakley remains deterministic; it does not fall through to AI fake rows.
- If mandatory fields cannot be extracted, output is blocked with review reasons rather than marked ready.

Current expected behavior:
- Valid rows are produced only if mandatory fields are extracted and validated.
- Otherwise, manual review clearly reports missing or weak fields such as OCR quality, invoice number, dates, service address/property, and GL mapping.

## 9. Validation hardening

Post-generation validation now flags:
- connection fee keyword row not using GL `6956`
- late fee using GL `6956`
- fire protection service mapped as normal water GL `6955`
- trash/sanitation mapped away from GL `6940`
- standalone tax rows
- blank or invalid required GLs
- payment/previous balance exported as expense

## 10. Browser screenshots

Screenshots were captured under:

`docs/reports/phases/screenshots/phase_qa2_line_classification_gl_fixes/`

Files:
- `tennessee_american_water_fire_service_single.png`
- `tennessee_american_water_bulk_no_tax_standalone.png`
- `hwea_connection_fee_6956_single.png`
- `weakley_image_manual_review.png`

## 11. Smoke results

Passed:
- `python -m compileall webapp\backend utils scripts`
- `python scripts\verify_backend_routes.py`
- `python scripts\smoke_document_ingestion.py`
- `python scripts\smoke_canonical_rules_engine.py`
- `python scripts\smoke_canonical_invoice_fixtures.py`
- `python scripts\smoke_utility_processors.py`
- `python scripts\smoke_utility_e2e_outputs.py`
- `python scripts\smoke_utility_e2e_outputs.py --prepare-browser-fixtures`
- `python scripts\smoke_description_contract.py`
- `python scripts\smoke_required_fields_contract.py`
- `python scripts\smoke_utility_line_classification.py`
- `python scripts\smoke_weakley_image_bill.py`
- `python scripts\smoke_full_batch_regression.py`
- `cd webapp/frontend && npm.cmd run build`
- `cd webapp/frontend && npx.cmd tsc --noEmit`
- `cd webapp/frontend && npm.cmd run test:e2e` — 35 passed

## 12. Integrity notes

- No Dropbox calls were made in automated tests.
- `Output/Template.xlsx` was protected by smoke checks.
- Source training bills were not modified.
- Existing deterministic utility processors stayed routed deterministically.
- QA-1 service-address description fixes were preserved.

## 13. Remaining limitations

- Weakley image OCR is improved but still depends on source image quality. Low-quality images may remain manual review instead of producing ready rows.
- Fire service GL is configured for Tennessee American Water. Other future vendors with fire-service billing need either canonical GL confirmation or review blocking.
- Vendor-specific late-fee GL preferences beyond default utility behavior may need future manager-configurable rules.

## 14. Next recommended phase

Phase QA-3 should add a visual manager-facing classification inspector in Single Invoice Mode: show each source line, classifier result, GL, matched keyword, and reason, with an inline override/save-mapping flow.
