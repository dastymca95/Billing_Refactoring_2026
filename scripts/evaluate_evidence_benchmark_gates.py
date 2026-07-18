"""Evaluate offline replay and independent evidence gates without providers."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from webapp.backend.services.evidence_benchmark import EvidenceBackedGoldenContract
from webapp.backend.services.evidence_benchmark_gates import evaluate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark-root", type=Path, required=True)
    args = parser.parse_args()
    root = args.benchmark_root.resolve()
    contract = EvidenceBackedGoldenContract(**json.loads(
        (root / "golden_contract.pending.json").read_text(encoding="utf-8")
    ))
    result = evaluate(
        contract=contract,
        replay=json.loads((root / "offline_replay_result.json").read_text(encoding="utf-8")),
        replay_repeat=json.loads((root / "offline_replay_result_repeat.json").read_text(encoding="utf-8")),
        replay_metrics=json.loads((root / "offline_replay_metrics.json").read_text(encoding="utf-8")),
        benchmark_root=root,
        verifier_results=json.loads((root / "targeted_verifier_results.json").read_text(encoding="utf-8")),
        source_root=(root.parent / "isolated_webapp_data" / "batches"
                     / contract.batch_id / "input"),
    )
    (root / "benchmark_gates.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    deterministic_pass = result["deterministic_replay_gate"]["status"] == "pass"
    safety_pass = not any((
        result["safety"]["source_evidence_loss_count"],
        result["safety"]["false_safe_export_count"],
        result["safety"]["unresolved_concepts_without_block_count"],
        result["safety"]["unauthorized_gl_count"],
        result["safety"]["legacy_free_text_review_code_count"],
        result["safety"]["typed_review_code_mismatch_count"],
    ))
    return 0 if deterministic_pass and safety_pass else 2


if __name__ == "__main__":
    raise SystemExit(main())
