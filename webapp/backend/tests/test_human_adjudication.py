from __future__ import annotations

import copy
import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from webapp.backend.services import human_adjudication as adjudication
from webapp.backend.services import tenant_accounting_policies
from webapp.backend.services.accounting_readiness import evaluate_rows
from webapp.backend.main import create_app


BATCH_ID = "batch_20260718_120000_001"


def _actor(
    tenant: str = "tenant-a",
    role: adjudication.ReviewerRole = adjudication.ReviewerRole.ACCOUNTING_MANAGER_CONTROLLER,
) -> adjudication.ActorContext:
    return adjudication.ActorContext(
        reviewer_id="reviewer@example.invalid", role=role, tenant_id=tenant,
    )


def _row() -> dict:
    return {
        "Invoice Number": "INV-100",
        "Vendor": "Example Legal Office",
        "Bill or Credit": "Bill",
        "Invoice Date": "2026-07-18",
        "Accounting Date": "2026-07-18",
        "Due Date": "2026-08-17",
        "Invoice Description": "Legal filing services",
        "Line Item Number": 1,
        "Property Abbreviation": "PROP",
        "Location": "A",
        "GL Account": "6205",
        "Line Item Description": "Attorney writ filing fee",
        "Quantity": 1,
        "Unit Price": 75,
        "Amount": 75,
        "Expense Type": "General",
        "Document Url": "",
        "_meta": {
            "tenant_id": "tenant-a",
            "line_item_id": "legal-line-1",
            "source_file": "legal.png",
            "source_page": 1,
            "trace_ids": ["trace-location"],
            "ai_confidence": 0.72,
            "source_text": {
                "raw_description": "Attorney writ filing fee",
                "raw_invoice_description": "Legal filing services",
            },
            "document_facts": {
                "schema_version": "document-facts/1.0",
                "extraction_model": "fixture-model",
                "line_items": [{
                    "line_item_id": "legal-line-1",
                    "raw_description": "Attorney writ filing fee",
                    "amount": "75",
                    "quantity": "1",
                    "unit_price": "75",
                }],
            },
            "semantic_classification": {
                "semantic_version": "semantic-classification/1.0",
                "line_item_id": "legal-line-1",
                "document_family": "invoice",
                "line_family": "fee",
                "trade_family": "legal",
                "work_mode": "fee",
                "recurrence": "one_time",
                "capital_context": "operating",
                "specific_assets": [],
                "positive_evidence": [],
                "negative_evidence": [],
                "contradictions": [],
                "confidence": 0.9,
            },
            "accounting_decision": {
                "candidates_ranked": [{"gl_code": "6205"}, {"gl_code": "6600"}],
            },
            "ai_provenance": {"invoice_total": 75},
            "total_reconciliation_passed": True,
        },
    }


def _result() -> dict:
    invoice = {"source_file": "legal.png", "invoice_number": "INV-100", "rows": [_row()]}
    return {
        "batch_id": BATCH_ID,
        "all_invoices": [invoice],
        "by_vendor": {"legal": {"invoices": [copy.deepcopy(invoice)]}},
    }


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(adjudication.settings, "WEBAPP_DATA_ROOT", tmp_path)
    batches = tmp_path / "batches"
    batch = batches / BATCH_ID
    for child in ("input", "processed", "trace"):
        (batch / child).mkdir(parents=True, exist_ok=True)
    source = batch / "input" / "legal.png"
    Image.new("RGB", (300, 200), color="white").save(source)
    (batch / "trace" / "legal.png.json").write_text(json.dumps({
        "items": [{
            "trace_id": "trace-location",
            "feeds_columns": ["Location"],
            "detected_text": "Location A",
            "bbox": {"x": 0.15, "y": 0.25, "w": 0.45, "h": 0.35},
        }],
    }), encoding="utf-8")
    monkeypatch.setattr(adjudication.settings, "BATCHES_ROOT", batches)
    monkeypatch.setattr(tenant_accounting_policies.settings, "WEBAPP_DATA_ROOT", tmp_path)
    return tmp_path


def _record(
    result: dict, *, value: object = "6205", field: str = "GL Account",
    benchmark: bool = False, learning: bool = False, rule: bool = False,
    actor: adjudication.ActorContext | None = None,
):
    return adjudication.record_manual_edits(
        result=result,
        batch_id=BATCH_ID,
        edits_by_index={0: {field: value}},
        options=adjudication.AdjudicationOptions(
            rationale="Human verified the value against the source document.",
            add_to_benchmark=benchmark,
            approve_learning_example=learning,
            propose_reusable_rule=rule,
        ),
        actor=actor or _actor(),
    )


def test_invoice_only_correction_is_versioned_and_survives_reprocessing():
    result = _result()
    source_before = copy.deepcopy(result["all_invoices"][0]["rows"][0]["_meta"]["source_text"])
    report = _record(result, field="Location", value="B")

    assert report.recorded == 1
    assert result["all_invoices"][0]["rows"][0]["Location"] == "B"
    assert result["by_vendor"]["legal"]["invoices"][0]["rows"][0]["Location"] == "B"
    assert result["all_invoices"][0]["rows"][0]["_meta"]["source_text"] == source_before

    fresh = _result()
    replay = adjudication.apply_to_result(
        fresh, batch_id=BATCH_ID, tenant_id="tenant-a",
    )
    assert replay.unresolved == 0
    assert fresh["all_invoices"][0]["rows"][0]["Location"] == "B"
    assert fresh["all_invoices"][0]["rows"][0]["_meta"]["human_adjudication_badges"]["Location"] == [
        "manually_corrected"
    ]


def test_benchmark_is_submitted_then_human_approved_and_revisions_are_immutable():
    result = _result()
    first = _record(result, field="Location", value="B", benchmark=True)
    revision_id = first.revision_ids[0]
    pending = adjudication.list_governance_events("tenant-a", revision_id=revision_id)
    assert {item.event_type for item in pending} == {"benchmark_submitted"}

    approved = adjudication.decide_benchmark(revision_id, approve=True, actor=_actor())
    assert approved.event_type == "benchmark_approved"
    replayed = _result()
    adjudication.apply_to_result(replayed, batch_id=BATCH_ID, tenant_id="tenant-a")
    assert "benchmark_approved" in (
        replayed["all_invoices"][0]["rows"][0]["_meta"]
        ["human_adjudication_badges"]["Location"]
    )
    _record(result, field="Location", value="C")
    revisions = [item for item in adjudication.list_revisions("tenant-a")
                 if item.field == "Location"]
    assert len(revisions) == 2
    assert revisions[0].revision_number == 2
    assert revisions[0].supersedes_revision_id == revision_id
    assert revisions[1].corrected_value == "B"
    assert revisions[1].original_ai_value == "A"


def test_approved_learning_example_is_tenant_private_candidate_only():
    result = _result()
    report = _record(result, learning=True)
    revision = adjudication.list_revisions("tenant-a")[0]
    candidates = adjudication.approved_learning_candidates(
        tenant_id="tenant-a",
        canonical_concept=revision.canonical_concept,
        document_family=revision.document_family or "invoice",
        line_family=revision.line_family or "unknown",
        trade_family=revision.trade_family or "unknown",
        work_mode=revision.work_mode or "unknown",
    )
    assert report.learning_approvals == 1
    assert candidates[0]["gl_code"] == "6205"
    assert candidates[0]["selection_authority"] is False
    assert adjudication.approved_learning_candidates(
        tenant_id="tenant-b",
        canonical_concept=revision.canonical_concept,
        document_family="invoice", line_family="fee", trade_family="legal", work_mode="fee",
    ) == []


def test_rule_proposal_requires_separate_controller_approval(monkeypatch):
    result = _result()
    report = _record(result, rule=True)
    revision_id = report.revision_ids[0]
    proposed = adjudication.list_governance_events("tenant-a", revision_id=revision_id)
    assert proposed[0].event_type == "rule_proposed"
    assert proposed[0].status == "pending_approval"

    with pytest.raises(PermissionError):
        adjudication.approve_rule(
            revision_id,
            actor=_actor(role=adjudication.ReviewerRole.ACCOUNTANT_AP),
        )

    # The tenant policy engine itself is exercised here; the controller's
    # explicit approval is the only activation boundary.
    approved = adjudication.approve_rule(revision_id, actor=_actor())
    assert approved.event_type == "rule_approved"
    assert approved.details["tenant_policy_status"] == "active"
    replayed = _result()
    adjudication.apply_to_result(replayed, batch_id=BATCH_ID, tenant_id="tenant-a")
    assert "governed_by_rule" in (
        replayed["all_invoices"][0]["rows"][0]["_meta"]
        ["human_adjudication_badges"]["GL Account"]
    )


def test_property_manager_cannot_approve_learning_and_tenants_are_isolated():
    result = _result()
    manager = _actor(role=adjudication.ReviewerRole.PROPERTY_MANAGER)
    with pytest.raises(PermissionError):
        _record(result, learning=True, actor=manager)
    other = _actor(tenant="tenant-b")
    with pytest.raises(PermissionError):
        _record(result, field="Location", value="B", actor=other)
    assert adjudication.list_revisions("tenant-b") == []


def test_preview_export_values_share_the_replayed_approved_correction(monkeypatch):
    result = _result()
    result["all_invoices"][0]["rows"][0]["GL Account"] = ""
    result["by_vendor"]["legal"]["invoices"][0]["rows"][0]["GL Account"] = ""
    _record(result, field="GL Account", value="6205")
    fresh = _result()
    fresh["all_invoices"][0]["rows"][0]["GL Account"] = ""
    fresh["by_vendor"]["legal"]["invoices"][0]["rows"][0]["GL Account"] = ""
    adjudication.apply_to_result(fresh, batch_id=BATCH_ID, tenant_id="tenant-a")
    preview_row = fresh["all_invoices"][0]["rows"][0]
    export_row = copy.deepcopy(preview_row)
    monkeypatch.setattr(
        "webapp.backend.services.accounting_readiness.get_template_rules",
        lambda: {"required_columns": [
            "Invoice Number", "Vendor", "Property Abbreviation", "GL Account", "Amount",
        ]},
    )
    readiness = evaluate_rows([export_row])
    assert preview_row["GL Account"] == export_row["GL Account"] == "6205"
    assert readiness.export_allowed is True


def test_save_edits_api_records_adjudication_and_audit_history(
    monkeypatch, isolated_runtime,
):
    batch = adjudication.settings.BATCHES_ROOT / BATCH_ID
    cache = batch / "processed" / "_webapp_result.json"
    cache.write_text(__import__("json").dumps(_result()), encoding="utf-8")
    monkeypatch.setattr(adjudication, "runtime_actor_context", lambda: _actor())

    client = TestClient(create_app())
    response = client.post(f"/api/batches/{BATCH_ID}/save-edits", json={
        "edits": {"0": {"Location": "B"}},
        "adjudication": {
            "rationale": "Human verified location B against the source.",
            "add_to_benchmark": True,
            "approve_learning_example": False,
            "propose_reusable_rule": False,
        },
    })
    assert response.status_code == 200, response.text
    assert response.json()["adjudication"]["recorded"] == 1
    persisted = __import__("json").loads(cache.read_text(encoding="utf-8"))
    assert persisted["all_invoices"][0]["rows"][0]["Location"] == "B"
    activity = client.get(f"/api/batches/{BATCH_ID}/activity").json()["items"]
    assert {item["event_type"] for item in activity} >= {
        "human_adjudication_saved", "benchmark_submission",
    }
    crop = client.get(
        f"/api/batches/{BATCH_ID}/adjudications/evidence/0/Location/crop",
    )
    assert crop.status_code == 200
    assert crop.headers["content-type"] == "image/png"
    assert len(crop.content) > 100
