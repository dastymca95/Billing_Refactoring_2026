"""Private-root persistence for Phase 3.9B analytical outputs."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from .assisted_labeling import AssistedLabelingService
from .autonomous_adjudication import AutonomousAdjudicator, deterministic_verification, proposal_to_extraction
from .private_labeling_workspace import PrivateLabelingWorkspace, WorkspaceError
from .reviewer_1_pilot import Reviewer1Pilot


class AutonomousPrivateRunner:
    def __init__(self, workspace: PrivateLabelingWorkspace, pilot: Reviewer1Pilot,
                 assisted: AssistedLabelingService, adjudicator: AutonomousAdjudicator) -> None:
        self.workspace = workspace; self.pilot = pilot; self.assisted = assisted; self.adjudicator = adjudicator
        self.output_dir = workspace.root / "analysis" / "autonomous_3_9b"
        self.report_path = workspace.root / "reports" / "autonomous_adjudication_3_9b.json"

    def run_pilot(self) -> dict[str, Any]:
        snapshot = self.workspace.selection_dir / "selected_120_v1.json"
        before = _hash(snapshot); statuses = Counter(); exceptions = Counter(); results = []
        for benchmark_id in sorted(self.pilot.pilot_ids()):
            proposal = self.assisted.proposal(benchmark_id); primary = proposal_to_extraction(proposal)
            inventory = self.workspace._inventory.get(benchmark_id, {})
            visual_required = str(inventory.get("complexity_tier") or "").upper() in {"C", "D"}
            result = self.adjudicator.adjudicate(benchmark_id, deterministic_primary=primary,
                deterministic_verification=deterministic_verification(primary), visual_required=visual_required)
            payload = result.model_dump(mode="json"); self.workspace._atomic_json(self.output_dir / f"{benchmark_id}.json", payload)
            statuses[result.status.value] += 1; exceptions.update(result.exception_codes); results.append(result)
        after = _hash(snapshot)
        if before != after: raise WorkspaceError("selected_120_v1 changed during autonomous analysis")
        summary = {"schema_version": "autonomous-adjudication-run/1.0", "documents": len(results),
            "statuses": dict(statuses), "exception_codes": dict(exceptions),
            "machine_gold_count": 0, "human_labels_modified": 0, "reviewer_2_started": False,
            "dataset_sha256": after, "dataset_hash_unchanged": True,
            "available_model_capabilities": sum(len(result.capability_evidence) for result in results),
            "strong_reasoner_executions": 0}
        self.workspace._atomic_json(self.report_path, summary)
        return summary


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().rstrip(b"\r\n")).hexdigest()


__all__ = ["AutonomousPrivateRunner"]
