# Phase 1/2 commit separation plan

Prepared against `HEAD 497d492` on branch `main`. Staging is explicit and
patch-based where a tracked file contains unrelated history. No runtime data,
user business documents, screenshots, generated workbooks, caches, resets,
rebases, or pushes are part of this plan.

| File | Hunk/range | Assigned phase | Reason | Safe to stage? |
| --- | --- | --- | --- | --- |
| `webapp/backend/services/accounting_readiness.py` | whole new file | phase_1 | Versioned readiness/export authority | Yes |
| `webapp/backend/api/export.py` | import plus readiness request/endpoint around current lines 14 and 93-106 | phase_1 | Readiness API | Yes, patch only |
| `webapp/backend/services/batch_processor.py` | current 1511-1607 export authorization/legacy retirement region | phase_1 + phase_1_hardening | Central export gate and retired copy path | No: contiguous hunk also contains older Dropbox/export work; leave unstaged |
| `webapp/backend/api/billing_v2.py`; `services/billing_v2.py` | whole new files | phase_1 | Billing V2 readiness consumer | Yes |
| `webapp/backend/main.py` | billing_v2 import/router hunks | phase_1 | Route registration | Yes, patch only |
| `webapp/backend/tests/test_accounting_readiness.py` | whole new file | phase_1 | Critical readiness decisions | Yes |
| `webapp/frontend/src/types.ts` | readiness types around current 306-350; V2 types after 784 | phase_1 / phase_2 | Shared type file also contains historical changes | No: phase boundaries share adjacent hunks; leave unstaged |
| `webapp/frontend/src/App.tsx`, `api.ts`, `BatchExplorer.tsx` | readiness/export occurrences among many UI/perf hunks | phase_1 | Frontend consumption | No: inseparable from substantial prior UI work; leave unstaged |
| `webapp/frontend/src/features/billing-v2/**`; `e2e/billing-v2.spec.ts` | whole new paths | phase_1 | Billing V2 consumer and contract test | Yes |
| `requirements.txt` | final four lines | phase_1_hardening | pytest/httpx setup | Yes |
| `README.md` | current 113-145 | phase_1_hardening | Test/browser setup and legacy-copy policy | Yes |
| `webapp/frontend/e2e/readiness-gate.spec.ts` | whole new file | phase_1_hardening | Browser readiness gate | Yes |
| `webapp/backend/services/batch_processor.py` | legacy copy removal in shared export hunk | phase_1_hardening | Opaque workbook retirement | No: same ambiguous shared hunk noted above |
| duplicate detector issue | no repository artifact found | phase_1_hardening | Tracked externally/separately | Not applicable |
| `config/accounting_decision_v2.yaml` | whole new file | phase_2 | Versioned catalog/semantic/ranking metadata | Yes |
| `accounting_contracts.py`, `gl_catalog.py`, `semantic_classifier.py`, `accounting_decision_engine.py` | whole new files | phase_2 | Typed Phase 2 contracts and sole selector | Yes |
| `accounting_pipeline_v2.py` | whole new file except strict flag parser lines 16-37 | phase_2 + phase_2_checkpoint | Adapter plus checkpoint hardening share one new file | Yes, patch-based split |
| `service_invoice_gl_reasoning.py` | whole new file | phase_2 | Compatibility candidate adapter | Yes |
| `row_normalizer.py` | current 178-236 | phase_2 | Source capture and central decision adapter | No: adjacent historical normalization changes make the hunk unsafe |
| `ai_invoice_processor.py` | current 4304-4321 | phase_2 | Direct-call adapter | No: file has thousands of prior unrelated changed lines; leave unstaged |
| `description_builder.py` | standalone service-fee preservation near current 335 | phase_2 | Prevent generated text replacing source | No: embedded in a much larger historical description rewrite |
| `GlAccountExplanation.tsx` | whole new file | phase_2 | Backend-generated explanation UI | Yes |
| `test_accounting_decision_v2.py` | all except final flag tests | phase_2 | Decision/semantic regression coverage | Yes, patch-based split |
| `test_service_invoice_gl_reasoning.py` | whole new file | phase_2 | Service/material guardrails | Yes |
| `ACCOUNTING_DECISION_ENGINE_V2.md` | whole new file | phase_2 | Architecture and legacy adapters | Yes |
| `.gitignore` | only checkpoint runtime block; `/Vendors/` hunk excluded | phase_2_checkpoint / unrelated_existing | Runtime hygiene versus prior vendor-config work | Yes, patch only |
| `accounting_pipeline_v2.py` | strict flag parser | phase_2_checkpoint | Fail-fast and double-authorized rollback | Yes, patch split |
| `test_accounting_decision_v2.py` | final two flag tests | phase_2_checkpoint | Critical flag behavior | Yes, patch split |
| `PHASE_2_BASELINE.md`, `PHASE_2_SHADOW_METRICS.json`, `smoke_phase2_shadow_metrics.py` | whole new files | phase_2_checkpoint | Reproducible baseline and metrics | Yes |
| `PROJECT_CLEANUP_AUDIT_REPORT.md` | addendum line containing explicit `<br>` | phase_2_checkpoint within unrelated addendum | Authorized whitespace-only fix, but entire addendum is pre-existing/unrelated | No: do not stage the surrounding unrelated addendum |
| `PHASE_COMMIT_PLAN.md` | whole new file | phase_2_checkpoint | Commit isolation record | Yes |
| canonical expected YAML changes | whole tracked diffs | unrelated_existing | Predate remediation separation | No |
| utility/vendor/AI processors, `config/vendors/**`, UI/PDF/perf/report changes | all remaining hunks | unrelated_existing | Historical user work | No |
| `webapp/frontend/test-results/.last-run.json` | historically tracked runtime result | generated_runtime | Playwright state, must later be untracked only with approval | No |
| `test-results/`, `playwright-report/`, caches, `dist/`, `webapp_data/`, root `tmp_*.png` | whole paths | generated_runtime | Reproducible runtime output | No |
| `Punctual Process/**`, report screenshots/logs, generated workbooks | whole paths | unrelated_existing / generated_runtime ambiguity | May be user evidence or deliverables | No; preserve untouched |

## Ambiguities and safety decision

The central export implementation and several frontend consumers live in files
whose current diffs combine remediation work with months of uncommitted UI,
Dropbox, performance, and processor changes. Staging those entire files would
violate the requested isolation. This plan therefore commits only provably
isolated files/hunks and leaves ambiguous integrations unstaged. Tests execute
against the complete working tree; a commit is not claimed to be independently
deployable when its required integration hunk remains listed above.
