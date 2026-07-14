import hashlib
import json

import pytest

from webapp.backend.services.assisted_labeling import AssistedLabelingService
from webapp.backend.services.private_labeling_workspace import WorkspaceError
from webapp.backend.services.reviewer_1_pilot import Reviewer1Pilot
from webapp.backend.tests.test_reviewer_1_pilot import pilot
from webapp.backend.tests.test_private_labeling_workspace import valid_label


def assisted(tmp_path):
    workspace, controller = pilot(tmp_path)
    controller.prepare_manifest()
    return workspace, controller, AssistedLabelingService(workspace, controller)


def test_dataset_owner_validity_closes_triage_without_creating_labels(tmp_path):
    workspace, _, service = assisted(tmp_path)
    before = list(workspace.labels_dir.glob("*.json"))
    event = service.record_dataset_owner_validity()
    assert event["decision"] == "all_selected_documents_adjudicable"
    assert event["effect"] == "validity_triage_closed_only"
    assert list(workspace.labels_dir.glob("*.json")) == before


def test_proposal_is_unverified_separate_and_does_not_overwrite_human_draft(tmp_path):
    workspace, controller, service = assisted(tmp_path); benchmark_id = "bench-000"
    workspace.save_label(benchmark_id, valid_label(), reviewer_id="human", dataset_version="selected_120_v1")
    before = (workspace.labels_dir / f"{benchmark_id}.json").read_bytes()
    proposal = service.proposal(benchmark_id)
    assert proposal["source"] == "machine_proposed" and proposal["status"] == "unverified"
    assert proposal["authoritative"] is False and proposal["strong_reasoner_used"] is False
    assert (workspace.labels_dir / f"{benchmark_id}.json").read_bytes() == before


def test_accept_and_correct_are_explicit_and_preserve_original_proposal(tmp_path):
    _, controller, service = assisted(tmp_path); benchmark_id = "bench-000"
    proposal = service.proposal(benchmark_id); field = proposal["fields"][0]
    accepted = service.decide_field(benchmark_id, field["field_path"], reviewer_id="r", action="accept",
                                    evidence_inspected=True)
    corrected = service.decide_field(benchmark_id, field["field_path"], reviewer_id="r", action="correct",
                                     human_value="human correction", reason="document differs", evidence_inspected=True)
    assert accepted["human_value"] == field["proposed_value"]
    assert corrected["machine_proposal"] == field and corrected["human_value"] == "human correction"
    assert service.review_state(benchmark_id)["benchmark_status"] == "machine_proposed"


def test_document_approval_fails_closed_and_never_marks_gold(tmp_path):
    _, controller, service = assisted(tmp_path); benchmark_id = "bench-000"
    with pytest.raises(WorkspaceError, match="blocking validation errors"):
        service.approve_document(benchmark_id, reviewer_id="r", evidence_inspected=True)
    assert service.review_state(benchmark_id)["adjudicated_gold"] is False


def test_exception_filter_and_safe_accept_require_document_inspection(tmp_path):
    _, controller, service = assisted(tmp_path); benchmark_id = "bench-000"
    assert service.exception_fields(benchmark_id)
    with pytest.raises(WorkspaceError, match="inspection"):
        service.accept_non_conflicting(benchmark_id, reviewer_id="r", evidence_inspected=False)
    result = service.accept_non_conflicting(benchmark_id, reviewer_id="r", evidence_inspected=True)
    assert result["accepted"] >= 0


def test_assisted_sidecars_do_not_change_dataset_snapshot_hash(tmp_path):
    workspace, controller, service = assisted(tmp_path)
    frozen = workspace.selection_dir / "selected_120_v1.json"
    frozen.write_bytes(workspace.selected_path.read_bytes())
    before = hashlib.sha256(frozen.read_bytes()).hexdigest()
    benchmark_id = "bench-000"; service.record_dataset_owner_validity(); service.proposal(benchmark_id)
    assert hashlib.sha256(frozen.read_bytes()).hexdigest() == before


def test_no_bulk_approval_or_reviewer_2_transition_exists(tmp_path):
    _, controller, service = assisted(tmp_path)
    assert not hasattr(service, "approve_all")
    with pytest.raises(WorkspaceError): controller.reviewer_2_start()


def test_rotation_persists_as_metadata_without_modifying_source(tmp_path):
    workspace, _, _ = assisted(tmp_path); source = workspace.private_document_path("bench-000")
    before = source.read_bytes(); workspace.set_preview_rotation("bench-000", 90, reviewer_id="r")
    assert workspace.preview_rotation("bench-000") == 90
    assert source.read_bytes() == before


def test_structured_workspace_has_no_primary_raw_json_editor():
    path = __import__("pathlib").Path(__file__).parents[1] / "static/reviewer_1_assisted_workspace.html"
    html = path.read_text(encoding="utf-8")
    assert 'id="label"' not in html
    assert 'data-testid="label-form"' in html and 'data-testid="preview-panel"' in html
    assert "Accept non-conflicting proposals" in html and "Review only exceptions" in html
