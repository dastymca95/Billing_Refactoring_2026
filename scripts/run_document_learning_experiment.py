"""Run the provider-free local inventory for the document learning experiment."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from webapp.backend.services.document_learning_experiment import (
    assert_git_safe_summary,
    classify_ground_truth_eligibility,
    create_phase_a_calibration_sample,
    create_phase_a_split,
    inventory_local_corpus,
    render_git_safe_summary,
    run_phase0_preflight,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inventory a private corpus locally; no AI/provider calls are made."
    )
    parser.add_argument(
        "--phase", choices=("preflight", "inventory", "eligibility", "split", "sample"), default="inventory",
    )
    parser.add_argument("--experiment-id", default="exp-document-learning-simulation")
    parser.add_argument(
        "--expected-branch", default="experiment/document-learning-simulation",
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        default=_environment_path("INNER_VIEW_REASONING_TRAINING_ROOT"),
        help="Private source corpus outside the repository.",
    )
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=_environment_path("INNER_VIEW_DOCUMENT_LEARNING_EXPERIMENT_ROOT"),
        help="Ignored private artifact root under repository tmp or webapp_data.",
    )
    parser.add_argument("--split-seed", default="document-learning-split-v1")
    parser.add_argument("--inventory-snapshot-root", type=Path)
    parser.add_argument("--historical-tenant-id", default="local-default")
    parser.add_argument("--eligibility-path", type=Path)
    parser.add_argument(
        "--safe-summary-output",
        type=Path,
        help="Optional aggregate-only JSON or Markdown output.",
    )
    args = parser.parse_args()
    if args.source_root is None:
        parser.error("--source-root or INNER_VIEW_REASONING_TRAINING_ROOT is required")
    if args.runtime_root is None:
        parser.error(
            "--runtime-root or INNER_VIEW_DOCUMENT_LEARNING_EXPERIMENT_ROOT is required"
        )

    if args.phase == "preflight":
        result = run_phase0_preflight(
            project_root=PROJECT_ROOT,
            source_root=args.source_root,
            runtime_root=args.runtime_root,
            experiment_id=args.experiment_id,
            expected_branch=args.expected_branch,
        )
    elif args.phase == "inventory":
        result = inventory_local_corpus(
            project_root=PROJECT_ROOT,
            source_root=args.source_root,
            runtime_root=args.runtime_root,
            split_seed=args.split_seed,
        )
    elif args.phase == "eligibility":
        snapshot_root = args.inventory_snapshot_root
        if snapshot_root is None:
            snapshots = sorted((args.runtime_root / "snapshots").glob("corpus-*"))
            if len(snapshots) != 1:
                parser.error("--inventory-snapshot-root is required when snapshot selection is ambiguous")
            snapshot_root = snapshots[0]
        result = classify_ground_truth_eligibility(
            inventory_snapshot_root=snapshot_root,
            source_root=args.source_root,
            experiment_runtime_root=args.runtime_root,
            historical_tenant_id=args.historical_tenant_id,
        )
    elif args.phase == "split":
        snapshot_root = args.inventory_snapshot_root
        if snapshot_root is None:
            snapshots = sorted((args.runtime_root / "snapshots").glob("corpus-*"))
            if len(snapshots) != 1:
                parser.error("--inventory-snapshot-root is required when snapshot selection is ambiguous")
            snapshot_root = snapshots[0]
        eligibility_path = args.eligibility_path
        if eligibility_path is None:
            selection_path = args.runtime_root / "eligibility" / "active_eligibility.json"
            if not selection_path.is_file():
                parser.error("--eligibility-path is required before an active eligibility artifact exists")
            selection = json.loads(selection_path.read_text(encoding="utf-8"))
            eligibility_path = (
                args.runtime_root
                / "eligibility"
                / f"eligibility-{selection['active_eligibility_sha256']}.jsonl"
            )
        result = create_phase_a_split(
            inventory_snapshot_root=snapshot_root,
            eligibility_path=eligibility_path,
            experiment_runtime_root=args.runtime_root,
            seed=args.split_seed,
            maximum_unique_invoices=100,
        )
    else:
        snapshot_root = args.inventory_snapshot_root
        if snapshot_root is None:
            snapshots = sorted((args.runtime_root / "snapshots").glob("corpus-*"))
            if len(snapshots) != 1:
                parser.error("--inventory-snapshot-root is required when snapshot selection is ambiguous")
            snapshot_root = snapshots[0]
        split_selection = json.loads(
            (args.runtime_root / "splits" / "active_phase_a_split.json").read_text(
                encoding="utf-8"
            )
        )
        split_root = (
            args.runtime_root / "splits"
            / f"phase-a-{split_selection['active_split_sha256']}"
        )
        result = create_phase_a_calibration_sample(
            inventory_snapshot_root=snapshot_root,
            split_root=split_root,
            experiment_runtime_root=args.runtime_root,
            seed=args.split_seed,
            maximum_documents=100,
        )
    assert_git_safe_summary(result.git_safe_summary)
    if args.safe_summary_output:
        output = args.safe_summary_output.resolve(strict=False)
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.suffix.lower() in {".md", ".markdown"}:
            output.write_text(
                render_git_safe_summary(result.git_safe_summary), encoding="utf-8"
            )
        else:
            output.write_text(
                json.dumps(result.git_safe_summary, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
    print(json.dumps(result.git_safe_summary, indent=2, sort_keys=True))
    return 0


def _environment_path(name: str) -> Path | None:
    value = os.environ.get(name, "").strip()
    return Path(value) if value else None


if __name__ == "__main__":
    raise SystemExit(main())
