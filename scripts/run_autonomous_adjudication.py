"""Run Phase 3.9B against the private pilot without mutating benchmark labels."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
os.environ.setdefault("INNER_VIEW_TEST_ASSET_ROOT", str(ROOT / "webapp/backend/tests/fixtures/runtime_assets"))

from webapp.backend.services.assisted_labeling import AssistedLabelingService
from webapp.backend.services.autonomous_adjudication import AutonomousAdjudicator
from webapp.backend.services.autonomous_private_runner import AutonomousPrivateRunner
from webapp.backend.services.gl_catalog import load_gl_catalog
from webapp.backend.services.private_labeling_workspace import PrivateLabelingWorkspace
from webapp.backend.services.reviewer_1_pilot import Reviewer1Pilot


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--root", type=Path,
        default=Path(os.environ.get("INNER_VIEW_PRIVATE_BENCHMARK_ROOT", "")))
    args = parser.parse_args()
    if not str(args.root) or not args.root.is_dir(): raise SystemExit("INNER_VIEW_PRIVATE_BENCHMARK_ROOT is missing")
    _, catalog = load_gl_catalog(); workspace = PrivateLabelingWorkspace(args.root, catalog)
    pilot = Reviewer1Pilot(workspace); assisted = AssistedLabelingService(workspace, pilot)
    summary = AutonomousPrivateRunner(workspace, pilot, assisted, AutonomousAdjudicator()).run_pilot()
    print(json.dumps(summary, indent=2, sort_keys=True)); return 0


if __name__ == "__main__": raise SystemExit(main())
