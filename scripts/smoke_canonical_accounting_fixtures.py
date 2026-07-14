from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault(
    "INNER_VIEW_TEST_ASSET_ROOT",
    str(ROOT / "webapp" / "backend" / "tests" / "fixtures" / "runtime_assets"),
)

from webapp.backend.services.canonical_invoice_fixtures import run_all_complete  # noqa: E402


def main() -> int:
    result = run_all_complete()
    checked = 0
    failures: list[str] = []
    for fixture in result["results"]:
        key = fixture.get("fixture_key", "unknown")
        if fixture.get("skipped"):
            print(f"{key}: SKIPPED - {fixture.get('skip_reason', 'incomplete evidence')}")
            continue
        for row in fixture.get("rows") or []:
            checked += 1
            meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
            decision = meta.get("accounting_decision") or {}
            shadow = meta.get("gl_shadow_comparison") or {}
            if decision.get("decision_source") != "AccountingDecisionEngine":
                failures.append(f"{key}: final GL was not selected by AccountingDecisionEngine")
            if decision.get("selected_gl_code") != row.get("GL Account"):
                failures.append(f"{key}: selected GL does not match enriched row")
            if shadow.get("same") is not True:
                failures.append(f"{key}: legacy/V2 GL mismatch")
            raw = meta.get("document_facts") or {}
            if not isinstance(raw, dict):
                failures.append(f"{key}: missing source-preserving document facts")
    for failure in failures:
        print(f"FAIL: {failure}")
    if failures or not checked:
        return 1
    print(f"PASS: {checked} canonical accounting lines use the central V2 decision engine.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
