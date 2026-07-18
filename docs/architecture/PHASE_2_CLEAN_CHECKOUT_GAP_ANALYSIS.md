# Phase 2 clean-checkout gap analysis

Audit date: 2026-07-14. Baseline checkout: detached `18981ac`, containing
only `fb3e861`, `1344566`, `ef0b4ec`, and `18981ac` after `497d492`.

## Reproducibility prerequisites discovered

The repository cannot initially discover tests from a literal clean checkout.
`settings._find_project_root()` requires both ignored `config/vendors/` and
ignored `Output/Template.xlsx`. Phase 2 also requires the ignored
`Gl Codes/Chart Of Accounts.csv`; existing canonical processing uses ignored
`Properties/` and `Vendors/` reference data. Compile passes, but imports fail
before collection when those environment assets are absent.

For diagnostic continuation only, copies of those assets were provisioned in
the temporary worktree. They were not staged or committed. This is an existing
repository/environment packaging gap, not Phase 1/2 accounting wiring.

## Results with four commits only

- Backend compile: PASS.
- Initial pytest discovery: FAIL during settings/root detection.
- Discovery after external assets: initially FAIL because
  `gl_catalog.py` imports an uncommitted `is_payable_gl_account` symbol from
  `ai_mapping_review.py`.
- Discovery after a diagnostic-only minimal payable helper: 43 tests found.
- Full pytest: 39 passed, 4 failed, 4 subtests passed.
- Canonical fixtures: Capital Waste, EPB, Lowe's, and Spectrum pass; ServAll is
  skipped; TK Elevator fails on historical property/location/review behavior.

No provider was called.

## Proven integration gaps

| File | Hunk/range | Classification | Required behavior | Consequence if omitted | Safe to isolate |
| --- | --- | --- | --- | --- | --- |
| `services/batch_processor.py` | export helpers and `export_batch` around current 1511-1607 | REQUIRED_PHASE_1_INTEGRATION | Evaluate readiness for edited/cached rows and disable opaque legacy copy | Legacy workbook test returns copied export instead of `legacy_export_disabled`; export can bypass centralized authorization | Not yet: hunk includes historical Dropbox/template helpers |
| `api/export.py` | remaining download sort hunk | HISTORICAL_UNRELATED | Stable latest-download ordering | No readiness effect | Yes, but must not include |
| `frontend/App.tsx`, `api.ts`, `types.ts`, `BatchExplorer.tsx` | readiness/API/UI hunks | REQUIRED_PHASE_1_INTEGRATION mixed with HISTORICAL_UNRELATED | Refresh readiness after edits and consume `export_allowed` | Checkout UI cannot prove the validated frontend gate | Not yet; large adjacent UI history |
| `services/row_normalizer.py` | imports plus capture/metadata/decide calls around current 54 and 167-250 | REQUIRED_PHASE_2_INTEGRATION mixed with OPTIONAL_REFACTOR | Capture raw source before display normalization and invoke V2 for every normalized row | `source_text` is absent and a real normalization route bypasses V2 | Partially; polish, URL fallback, date and vendor refactors must be excluded |
| `services/ai_invoice_processor.py` | adapter immediately before `ai_result_to_invoice` return | REQUIRED_PHASE_2_INTEGRATION | Direct AI result route invokes capture + decision | Canonical/direct AI rows have no `accounting_decision` | Yes, narrow hunk |
| `services/ai_mapping_review.py` | `is_payable_gl_account` helper | REQUIRED_PHASE_2_INTEGRATION | Catalog payable guard import | Phase 2 tests cannot even collect | Yes, narrow helper; file was missing from original list |
| `services/canonical_rules.py` | service reasoner invocation in `_apply_gl_rules` | REQUIRED_PHASE_2_INTEGRATION mixed with semantic historical changes | Produce typed service candidates through the approved engine adapter | Service reasoning test lacks `gl_accounting_reasoning` | Not safely as currently written: surrounding hunk also changes legacy semantic selection |
| `services/description_builder.py` | standalone fee/source preservation | OPTIONAL_REFACTOR for wiring; required for one historical fixture expectation | Preserve terse standalone source description | Does not cause the V2 bypass; affects TK fixture output | Not isolated from large historical rewrite |
| canonical TK fixture/property hunks | multiple files/data | HISTORICAL_UNRELATED | Historical property/location behavior | TK fixture remains red in four-commit checkout | No accounting bridge claim |
| `frontend/GlAccountExplanation.tsx` | already committed | REQUIRED_PHASE_2_INTEGRATION | Render backend explanation | Component exists, but shared parent/type wiring remains absent | Parent wiring not yet safe |
| Playwright outputs/caches/runtime data | whole paths | GENERATED_RUNTIME | None | No product consequence | Never stage |

## Failing tests and meaning

1. `test_opaque_legacy_workbook_is_disabled_instead_of_copied`: Phase 1 export
   integration missing.
2. `test_row_adapter_preserves_raw_normalized_and_generated_descriptions`:
   Phase 2 row-normalizer integration missing.
3. `test_complete_canonical_fixtures_pass_through_central_decision_engine`:
   direct AI/canonical route does not invoke the engine.
4. `test_kros_invoice_uses_line_level_accounting_reasoning`: canonical service
   reasoner adapter is not wired.

## Commit decision

No bridge commit was created in this audit pass because Commit A would either
omit required export/UI behavior or include large unrelated Dropbox/UI hunks,
and Commit B would either omit canonical/service wiring or incorporate an
unapproved legacy semantic rewrite. The request explicitly requires unsafe or
inseparable hunks to remain unstaged. Creating nominal bridge commits under
these conditions would falsely claim clean-checkout parity.

Required next action is a focused extraction design for:

1. a minimal export/readiness adapter independent of historical Dropbox code;
2. minimal row and AI-direct adapter calls;
3. a candidate-only canonical service adapter that does not restore direct GL
   selection; and
4. minimal frontend readiness/decision types and parent wiring.

Only after those patches pass in the isolated worktree should the two bridge
commits be created.
