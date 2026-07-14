import hashlib
import json
from collections import Counter

import pytest

from webapp.backend.services.private_labeling_workspace import WorkspaceError
from webapp.backend.services.reviewer_1_pilot import Reviewer1Pilot
from webapp.backend.tests.test_private_labeling_workspace import build_workspace, valid_label


def pilot(tmp_path):
    workspace = build_workspace(tmp_path, tier_d=35)
    payload = json.loads(workspace.selected_path.read_text())
    for index, row in enumerate(payload["selection"]):
        row["quality_tier"] = "A" if index < 10 or index >= 20 else ("C" if index < 15 else "D")
    workspace.selected_path.write_text(json.dumps(payload))
    controller = Reviewer1Pilot(workspace)
    return workspace, controller


def test_manifest_is_exact_stratified_subset_and_preserves_dataset(tmp_path):
    workspace, controller = pilot(tmp_path)
    before = hashlib.sha256(workspace.selected_path.read_bytes()).hexdigest()
    manifest = controller.prepare_manifest()
    assert len(manifest["documents"]) == 20
    assert Counter(row["difficulty"] for row in manifest["documents"]) == {"easy": 10, "moderate": 5, "difficult": 5}
    assert controller.pilot_ids() <= {row["benchmark_id"] for row in workspace.selected()}
    assert hashlib.sha256(workspace.selected_path.read_bytes()).hexdigest() == before


def test_existing_invalid_draft_is_included_and_not_rewritten(tmp_path):
    workspace, controller = pilot(tmp_path)
    draft_id = "bench-000"
    saved = workspace.save_label(draft_id, valid_label(gl="9999"), reviewer_id="r",
                                 dataset_version="selected_120_v1")
    before = (workspace.labels_dir / f"{draft_id}.json").read_bytes()
    manifest = controller.prepare_manifest()
    assert draft_id in {row["benchmark_id"] for row in manifest["documents"]}
    row = next(item for item in controller.queue() if item["benchmark_id"] == draft_id)
    assert row["draft_validation_status"] == "invalid" and row["draft_validation_errors"]
    assert saved["completion_status"] == "in_progress"
    assert (workspace.labels_dir / f"{draft_id}.json").read_bytes() == before


def test_active_time_excludes_paused_periods(tmp_path):
    workspace, controller = pilot(tmp_path); benchmark_id = next(iter(controller.pilot_ids()))
    events = [
        {"benchmark_id": benchmark_id, "action": "start", "timestamp": "2026-01-01T00:00:00+00:00"},
        {"benchmark_id": benchmark_id, "action": "pause", "timestamp": "2026-01-01T00:01:00+00:00"},
        {"benchmark_id": benchmark_id, "action": "resume", "timestamp": "2026-01-01T01:00:00+00:00"},
        {"benchmark_id": benchmark_id, "action": "complete", "timestamp": "2026-01-01T01:02:00+00:00"},
    ]
    for event in events: workspace._append_jsonl(controller.events_path, event)
    assert controller.active_seconds(benchmark_id) == 180


def test_queue_never_opens_remaining_100_and_stops_after_20(tmp_path):
    workspace, controller = pilot(tmp_path); manifest_ids = controller.pilot_ids()
    assert {row["benchmark_id"] for row in controller.queue()} <= manifest_ids
    assert len(controller.queue()) == 20
    for benchmark_id in manifest_ids:
        workspace.save_label(benchmark_id, valid_label(), reviewer_id="r", dataset_version="selected_120_v1",
                             completion_status="complete")
    assert controller.queue() == [] and controller.metrics()["remaining"] == 0


def test_reviewer_2_is_disabled_and_blind_payload_keeps_private_source_evidence(tmp_path):
    _, controller = pilot(tmp_path)
    with pytest.raises(WorkspaceError, match="Reviewer 2 is disabled"): controller.reviewer_2_start()
    payload = json.dumps(controller.queue()[0]).lower()
    assert "source_metadata_evidence" in payload
    for forbidden in ("accounting_decision", "selected_gl", "gl_candidates", "ai_confidence",
                      "model_route", "historical_resman", "reviewer_2", "strong_reasoner"):
        assert forbidden not in payload


def test_private_metrics_and_git_safe_report_boundaries(tmp_path):
    workspace, controller = pilot(tmp_path); manifest = controller.prepare_manifest()
    controller.write_reports()
    assert controller.manifest_path.is_relative_to(workspace.root)
    assert controller.reports_dir.is_relative_to(workspace.root)
    safe = controller.git_safe_markdown()
    for row in manifest["documents"]: assert row["benchmark_id"] not in safe
    assert "filename" not in safe.lower() or "filename conflicts" in safe.lower()
    assert str(workspace.root) not in safe


def test_abandon_preserves_draft_and_audit_history(tmp_path):
    workspace, controller = pilot(tmp_path); benchmark_id = "bench-000"
    workspace.save_label(benchmark_id, valid_label(gl="9999"), reviewer_id="r", dataset_version="selected_120_v1")
    controller.prepare_manifest()
    result = controller.abandon_draft(benchmark_id, reviewer_id="r", reason="Cannot adjudicate source")
    assert result["completion_status"] == "abandoned"
    assert result["validation_errors"] and result["document"]
    assert result["audit_history"][-1]["action"] == "abandon"
