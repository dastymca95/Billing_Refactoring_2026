from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp.backend.services.canonical_invoice_fixtures import list_fixtures, run_fixture  # noqa: E402


DEFAULT_OUTPUT = ROOT / "docs" / "architecture" / "PHASE_2_SHADOW_METRICS.json"


def collect_metrics() -> dict[str, Any]:
    metrics: dict[str, Any] = {
        "schema_version": "phase-2-shadow-metrics/1.0",
        "corpus": "complete canonical regression fixtures",
        "benchmark_scope": "regression_only_not_a_model_benchmark",
        "invoices_compared": 0,
        "lines_compared": 0,
        "legacy_v2_equal": 0,
        "legacy_v2_different": 0,
        "blocked_decisions": 0,
        "missing_gl": 0,
        "semantic_unknown": 0,
        "processing_failures": 0,
        "skipped_fixtures": [],
    }
    for fixture in list_fixtures()["fixtures"]:
        key = fixture["key"]
        if fixture["status"] != "complete":
            metrics["skipped_fixtures"].append(
                {"fixture": key, "reason": fixture.get("skip_reason") or "incomplete fixture"}
            )
            continue
        try:
            result = run_fixture(key)
        except Exception as exc:  # pragma: no cover - smoke failure reporting
            metrics["processing_failures"] += 1
            metrics.setdefault("failure_details", []).append({"fixture": key, "error": type(exc).__name__})
            continue
        metrics["invoices_compared"] += 1
        if not result.get("ok"):
            metrics["processing_failures"] += 1
        for row in result.get("rows") or []:
            metrics["lines_compared"] += 1
            meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
            shadow = meta.get("gl_shadow_comparison") or {}
            decision = meta.get("accounting_decision") or {}
            semantics = meta.get("semantic_classification") or {}
            if shadow.get("same") is True:
                metrics["legacy_v2_equal"] += 1
            else:
                metrics["legacy_v2_different"] += 1
            if decision.get("review_blocking") is True:
                metrics["blocked_decisions"] += 1
            if not decision.get("selected_gl_code"):
                metrics["missing_gl"] += 1
            if semantics.get("line_family") == "unknown" or semantics.get("work_mode") == "unknown":
                metrics["semantic_unknown"] += 1
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect deterministic Phase 2 shadow regression metrics.")
    parser.add_argument("--write", action="store_true", help="Persist the canonical JSON summary.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    metrics = collect_metrics()
    rendered = json.dumps(metrics, indent=2, sort_keys=True) + "\n"
    if args.write:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 1 if metrics["processing_failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
