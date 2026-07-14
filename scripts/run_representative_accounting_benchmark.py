from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import os
os.environ.setdefault("INNER_VIEW_TEST_ASSET_ROOT", str(ROOT / "webapp/backend/tests/fixtures/runtime_assets"))

from webapp.backend.services.accounting_pipeline_v2 import capture_source_fields, decide_row  # noqa: E402
from webapp.backend.services.accounting_readiness import evaluate_rows  # noqa: E402
from webapp.backend.services.model_registry import CapabilityDiscovery, default_registry  # noqa: E402
from webapp.backend.services.representative_benchmark import (  # noqa: E402
    BenchmarkLabel, EvaluationRecord, RepresentativeManifest, resolve_document, summarize,
)

FIXTURE_ROOT = ROOT / "webapp/backend/tests/fixtures/document_benchmark"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default="current")
    parser.add_argument("--manifest", type=Path, default=FIXTURE_ROOT / "representative_manifest.json")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.profile != "current":
        capability = CapabilityDiscovery(default_registry()).discover(args.profile)
        if not capability.available:
            print(json.dumps({"status": "skipped", "reason": "capability_not_discovered",
                              "profile": args.profile}, indent=2))
            return 0
        print(json.dumps({"status": "skipped", "reason": "candidate_executor_not_authorized",
                          "profile": args.profile, "shadow_only": True}, indent=2))
        return 0

    manifest = RepresentativeManifest.model_validate_json(args.manifest.read_text(encoding="utf-8"))
    records: list[EvaluationRecord] = []
    counts = {"gold": 0, "partial": 0, "unlabeled": 0}
    for entry in manifest.entries:
        label_path = (FIXTURE_ROOT / entry.label_ref).resolve()
        label_path.relative_to(FIXTURE_ROOT.resolve())
        label = BenchmarkLabel.model_validate_json(label_path.read_text(encoding="utf-8"))
        counts[label.status.value] += 1
        if label.status.value != "gold":
            continue
        records.append(_evaluate_current(entry, label))
    summary = summarize(records)
    summary["profile"] = "current"
    summary["label_counts"] = counts
    summary["representative_minimum_met"] = counts["gold"] >= 100
    payload = {"schema_version": "representative-run/1.0", "summary": summary,
               "records": [asdict(record) for record in records]}
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


def _evaluate_current(entry, label: BenchmarkLabel) -> EvaluationRecord:
    started = time.perf_counter()
    text = resolve_document(entry, FIXTURE_ROOT).read_text(encoding="utf-8")
    gold = label.adjudicated_gold
    assert gold is not None
    invoice_match = re.search(r"\bInvoice\s+([A-Z0-9-]+)", text, re.IGNORECASE)
    total_match = re.search(r"\bTotal\s+([0-9]+(?:\.[0-9]{2})?)", text, re.IGNORECASE)
    description = next((line.strip() for line in text.splitlines()
                        if line.strip() and not line.startswith("SANITIZED") and not line.lower().startswith(("invoice", "total"))), "")
    amount = Decimal(total_match.group(1)) if total_match else None
    row = {"Invoice Number": invoice_match.group(1) if invoice_match else "", "Vendor": "",
           "Property Abbreviation": "", "GL Account": "", "Line Item Description": description,
           "Amount": amount, "_meta": {"raw_description": description}}
    capture_source_fields(row, document_id=entry.case_id, line_item_id="line-1")
    decide_row(row, document_id=entry.case_id, line_item_id="line-1", extraction_route="benchmark_deterministic")
    meta = row["_meta"]
    decision = meta["accounting_decision"]
    semantics = meta["semantic_classification"]
    readiness = evaluate_rows([row])
    expected_line = gold.line_items[0]
    acceptable = tuple(filter(None, [expected_line.expected_gl, *expected_line.acceptable_gl_alternatives]))
    ranked = tuple(item.get("gl_code") for item in decision.get("candidates_ranked", []) if item.get("gl_code"))
    elapsed = int((time.perf_counter() - started) * 1000)
    return EvaluationRecord(
        entry.case_id, label.document_class, label.known_vendor, label.complexity, label.value_cohort,
        "deterministic", "deterministic",
        {"invoice_number": row["Invoice Number"] == (gold.invoice_number_redacted or ""),
         "amount": str(amount) == gold.total_amount},
        {"line_family": semantics.get("line_family") == expected_line.line_family,
         "trade_family": semantics.get("trade_family") == expected_line.trade_family,
         "work_mode": semantics.get("work_mode") == expected_line.work_mode},
        acceptable, ranked, gold.should_review, readiness.status.value != "ready",
        gold.should_block, not readiness.export_allowed, readiness.export_allowed,
        decision.get("confidence"), 0, elapsed, 0.0,
    )


if __name__ == "__main__":
    raise SystemExit(main())
