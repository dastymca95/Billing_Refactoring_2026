"""Execute the isolated, spend-bounded Phase A baseline."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run private Phase A calibration")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument(
        "--experiment-root", type=Path,
        default=PROJECT_ROOT / "tmp" / "document-learning-simulation",
    )
    parser.add_argument("--experiment-id", default="exp-document-learning-simulation")
    parser.add_argument("--local-only", action="store_true")
    parser.add_argument("--local-model", default="qwen3-vl:2b")
    parser.add_argument("--local-base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--local-profile-id", default="")
    parser.add_argument(
        "--execution-mode",
        choices=("LOCAL_ONLY", "CONTROLLED_EXTERNAL"),
        default="LOCAL_ONLY",
    )
    parser.add_argument("--private-authorization-record", type=Path)
    parser.add_argument("--expected-manifest-sha256", default="")
    parser.add_argument("--assignment-offset", type=int, default=0)
    parser.add_argument("--assignment-limit", type=int)
    parser.add_argument(
        "--authorize-private-provider-transfer",
        action="store_true",
        help=(
            "Confirm informed authorization to transmit selected private documents "
            "to the configured external providers under the Phase A spend cap."
        ),
    )
    args = parser.parse_args()

    experiment_root = args.experiment_root.resolve(strict=True)
    snapshots = sorted((experiment_root / "snapshots").glob("corpus-*"))
    if len(snapshots) != 1:
        parser.error("exactly one active inventory snapshot is required")
    split_selection = json.loads(
        (experiment_root / "splits" / "active_phase_a_split.json").read_text(
            encoding="utf-8"
        )
    )
    split_root = (
        experiment_root / "splits"
        / f"phase-a-{split_selection['active_split_sha256']}"
    )
    calibration_selection = json.loads(
        (
            experiment_root / "calibration" / "active_phase_a_calibration.json"
        ).read_text(encoding="utf-8")
    )
    calibration_root = (
        experiment_root / "calibration"
        / f"phase-a-{calibration_selection['active_calibration_version']}"
    )

    # Importing this module does not import application settings; isolation is
    # established inside the runner before the backend pipeline is loaded.
    from webapp.backend.services.phase_a_calibration_runner import run_phase_a_baseline

    result = run_phase_a_baseline(
        project_root=PROJECT_ROOT,
        source_root=args.source_root,
        experiment_runtime_root=experiment_root,
        inventory_snapshot_root=snapshots[0],
        calibration_manifest_path=calibration_root / "calibration_manifest.json",
        split_root=split_root,
        experiment_id=args.experiment_id,
        private_provider_transfer_authorized=args.authorize_private_provider_transfer,
        local_only=args.local_only or args.execution_mode == "LOCAL_ONLY",
        local_model=args.local_model,
        local_base_url=args.local_base_url,
        local_profile_id=args.local_profile_id,
        execution_mode=args.execution_mode,
        controlled_external_authorization_path=args.private_authorization_record,
        expected_manifest_sha256=args.expected_manifest_sha256 or None,
        assignment_offset=args.assignment_offset,
        assignment_limit=args.assignment_limit,
    )
    print(json.dumps(result.git_safe_summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
