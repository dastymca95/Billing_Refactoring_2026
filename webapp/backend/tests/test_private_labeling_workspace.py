import hashlib
import json
from pathlib import Path

import pytest

from webapp.backend.services.private_labeling_workspace import (
    FrozenDatasetError, LabelValidationError, PrivateLabelingWorkspace, WorkspaceError,
    validate_reviewer_1_label,
)


CATALOG = {
    "6500": {"Type": "Expense", "payable": True},
    "1100": {"Type": "Asset", "payable": False},
}


def build_workspace(tmp_path: Path, *, tier_d=1, duplicate_reserve=False) -> PrivateLabelingWorkspace:
    root = tmp_path / "private"
    for directory in ("selection", "inventory", "labels/reviewer_1", "documents"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    selected = []
    inventory = []
    for index in range(120):
        benchmark_id = f"bench-{index:03d}"
        quality = "D" if index < tier_d else "A"
        selected.append({"benchmark_id": benchmark_id, "selection_cohort": "scanned_bills" if index < tier_d else "digital_vendor_invoices",
                         "quality_tier": quality, "vendor_token": f"vendor-{index // 4}",
                         "template_signature": f"template-{index // 2}", "page_count": 1})
        document = root / "documents" / f"{benchmark_id}.pdf"; document.write_bytes(b"private fixture")
        inventory.append({"benchmark_id": benchmark_id, "private_relative_path": f"documents/{benchmark_id}.pdf",
                          "page_count": 1, "complexity_tier": quality, "estimated_ocr_quality": .2,
                          "blur_score": 2, "contrast_score": .3, "orientation": "portrait",
                          "inventory_warnings": ["low_visual_quality"]})
    reserve = []
    for index in range(20):
        benchmark_id = f"reserve-{index:03d}"
        reserve.append({"benchmark_id": benchmark_id, "selection_cohort": "scanned_bills",
                        "quality_tier": "B", "vendor_token": f"reserve-vendor-{index}",
                        "template_signature": f"reserve-template-{index}", "page_count": 2})
        document = root / "documents" / f"{benchmark_id}.pdf"; document.write_bytes(b"reserve fixture")
        inventory.append({"benchmark_id": benchmark_id, "private_relative_path": f"documents/{benchmark_id}.pdf",
                          "page_count": 2, "complexity_tier": "B", "inventory_warnings": []})
    (root / "selection/selected_120.json").write_text(json.dumps({"selection": selected}))
    (root / "selection/reserve_20.json").write_text(json.dumps({"selection": reserve}))
    with (root / "inventory/private_inventory.jsonl").open("w") as handle:
        for item in inventory: handle.write(json.dumps(item) + "\n")
    groups = [{"members": ["bench-000", "reserve-000"]}] if duplicate_reserve else []
    (root / "inventory/duplicate_groups.json").write_text(json.dumps({"groups": groups}))
    return PrivateLabelingWorkspace(root, CATALOG)


def valid_label(gl="6500", line_amount="10.00", total="10.00"):
    unknown = {"status": "unknown", "reason": "not visible"}
    return {"document": {"document_family": "invoice", "vendor_name": "Private Vendor",
            "vendor_normalization": "Private Vendor", "invoice_number": "I-1", "invoice_date": unknown,
            "due_date": unknown, "property": unknown, "service_address": unknown,
            "bill_or_credit": "bill", "total": total, "expected_route": "ai_vision",
            "document_completeness": "complete", "reviewer_confidence": .9},
            "line_items": [{"line_item_number": 1, "raw_description": "repair", "normalized_description": "repair",
                "quantity": unknown, "unit_price": unknown, "amount": line_amount, "tax": unknown,
                "location_unit": unknown, "line_family": "labor_service", "trade_family": "general",
                "work_mode": "labor_service", "capital_context": "expense", "expected_gl": gl,
                "acceptable_alternative_gls": [], "should_review": False, "should_block": False,
                "reasoning_notes": "Service evidence on page one", "evidence": [{"page": 1, "region": [0, 0, 1, 1]}]}],
            "unresolved_questions": []}


def test_tier_d_keep_records_auditable_transition(tmp_path):
    workspace = build_workspace(tmp_path)
    event = workspace.record_triage("bench-000", reviewer="reviewer-a", decision="keep_for_labeling", reason="Total and line evidence readable")
    assert event["previous_status"] == "pending" and event["new_status"] == "kept"
    assert workspace.status()["tier_d_reviewed"] == 1


def test_exclusion_replaces_from_same_cohort_and_preserves_120(tmp_path):
    workspace = build_workspace(tmp_path)
    event = workspace.record_triage("bench-000", reviewer="reviewer-a", decision="exclude_unadjudicable", reason="Corrupt source")
    assert event["new_status"] == "replaced" and event["replacement_benchmark_id"].startswith("reserve-")
    assert len(workspace.selected()) == 120
    replacement = next(row for row in workspace.selected() if row["benchmark_id"] == event["replacement_benchmark_id"])
    assert replacement["selection_cohort"] == "scanned_bills"


def test_duplicate_reserve_cannot_enter_selected_set(tmp_path):
    workspace = build_workspace(tmp_path, duplicate_reserve=True)
    candidates = workspace.replacement_candidates("bench-000")
    assert "reserve-000" not in {row["benchmark_id"] for row in candidates}


def test_vendor_and_template_limits_remain_valid(tmp_path):
    workspace = build_workspace(tmp_path)
    workspace.record_triage("bench-000", reviewer="r", decision="replace_with_reserve", reason="incomplete")
    selected = workspace.selected()
    from collections import Counter
    assert max(Counter(row["vendor_token"] for row in selected).values()) <= 5
    assert max(Counter(row["template_signature"] for row in selected).values()) <= 3


def test_dataset_freeze_hash_and_silent_mutation_guard(tmp_path):
    workspace = build_workspace(tmp_path)
    workspace.record_triage("bench-000", reviewer="r", decision="keep_for_labeling", reason="adjudicable")
    frozen = workspace.freeze_dataset("v1")
    snapshot = workspace.selection_dir / "selected_120_v1.json"
    assert hashlib.sha256(snapshot.read_bytes().rstrip(b"\n")).hexdigest() == frozen["sha256"]
    with pytest.raises(FrozenDatasetError):
        workspace.record_triage("bench-000", reviewer="r", decision="keep_for_labeling", reason="again")
    with pytest.raises(FrozenDatasetError): workspace.freeze_dataset("v1")


def test_snapshot_hash_changes_after_replacement(tmp_path):
    first = build_workspace(tmp_path / "one")
    first.record_triage("bench-000", reviewer="r", decision="keep_for_labeling", reason="keep")
    one = first.freeze_dataset("v1")["sha256"]
    second = build_workspace(tmp_path / "two")
    second.record_triage("bench-000", reviewer="r", decision="replace_with_reserve", reason="replace")
    two = second.freeze_dataset("v1")["sha256"]
    assert one != two


def test_blind_payload_excludes_app_and_reviewer_2_outputs(tmp_path):
    workspace = build_workspace(tmp_path)
    payload = json.dumps(workspace.blind_document_payload("bench-000")).lower()
    assert "accounting_decision" not in payload and "suggested_gl" not in payload
    assert "reviewer_2" not in payload and "historical_resman" not in payload
    assert "private_relative_path" not in payload


def test_autosave_and_crash_recovery_are_private_and_audited(tmp_path):
    workspace = build_workspace(tmp_path)
    saved = workspace.save_label("bench-000", valid_label(), reviewer_id="reviewer-a", dataset_version="v1")
    assert saved["completion_status"] == "in_progress" and saved["audit_history"][-1]["action"] == "autosave"
    assert (workspace.labels_dir / ".crash_recovery.json").is_file()


@pytest.mark.parametrize("gl", ["9999", "1100"])
def test_invalid_or_non_payable_gl_is_rejected(gl):
    assert any("invalid_or_non_payable" in error for error in validate_reviewer_1_label(valid_label(gl=gl), CATALOG))


def test_totals_mismatch_requires_explicit_flag():
    label = valid_label(line_amount="9.00", total="10.00")
    assert "reconciliation:totals_mismatch_requires_explicit_flag" in validate_reviewer_1_label(label, CATALOG)
    label["reconciliation_discrepancy"] = {"reason": "tax line unreadable"}
    assert "reconciliation:totals_mismatch_requires_explicit_flag" not in validate_reviewer_1_label(label, CATALOG)


def test_unknown_values_are_explicit_and_evidence_is_required():
    label = valid_label(); label["document"]["property"] = ""
    label["line_items"][0]["evidence"] = []
    errors = validate_reviewer_1_label(label, CATALOG)
    assert any("document.property" in error for error in errors)
    assert any("evidence" in error for error in errors)


def test_complete_label_fails_closed_but_draft_autosaves(tmp_path):
    workspace = build_workspace(tmp_path)
    invalid = valid_label(gl="9999")
    draft = workspace.save_label("bench-000", invalid, reviewer_id="r", dataset_version="v1")
    assert draft["validation_status"] == "invalid"
    with pytest.raises(LabelValidationError):
        workspace.save_label("bench-000", invalid, reviewer_id="r", dataset_version="v1", completion_status="complete")


def test_git_safe_status_contains_only_aggregates(tmp_path):
    workspace = build_workspace(tmp_path)
    rendered = workspace.safe_status_markdown()
    assert "bench-" not in rendered and "documents/" not in rendered and "Private Vendor" not in rendered
    assert "Selected documents: 120" in rendered
