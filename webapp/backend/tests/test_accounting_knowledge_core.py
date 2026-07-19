from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from webapp.backend import settings
from webapp.backend.main import create_app
from webapp.backend.services import (
    accounting_artifact_cache, context_intelligence, human_adjudication, resman_context_data,
)
from webapp.backend.services import tenant_accounting_policies
from webapp.backend.services.accounting_pipeline_v2 import _extend_knowledge_candidates
from webapp.backend.services.accounting_decision_engine import _source_support
from webapp.backend.services.gl_catalog import load_gl_catalog
from webapp.backend.services.accounting_knowledge_core import (
    AccountingKnowledgeCore,
    record_approved_export,
)


TENANT = "tenant-knowledge"
BATCH = "batch_20260718_120000_999"


def _actor(tenant: str = TENANT):
    return human_adjudication.ActorContext(
        reviewer_id="controller@example.invalid",
        role=human_adjudication.ReviewerRole.ACCOUNTING_MANAGER_CONTROLLER,
        tenant_id=tenant,
    )


def _row(line: int = 1) -> dict:
    return {
        "Invoice Number": "INV-KNOWLEDGE",
        "Vendor": "Example Service",
        "Bill or Credit": "Bill",
        "Invoice Date": "2026-07-18",
        "Accounting Date": "2026-07-18",
        "Due Date": "2026-08-18",
        "Invoice Description": "Legal filing service",
        "Line Item Number": line,
        "Property Abbreviation": "EP",
        "Location": str(line),
        "GL Account": "6205",
        "Line Item Description": "Attorney filing fee",
        "Amount": 75,
        "Expense Type": "General",
        "_meta": {
            "tenant_id": TENANT,
            "line_item_id": f"line-{line}",
            "source_file": "synthetic.pdf",
            "source_page": 1,
            "trace_ids": [f"trace-{line}"],
            "source_text": {"raw_description": "Attorney filing fee"},
            "semantic_classification": {
                "semantic_version": "semantic-classification/1.0",
                "line_item_id": f"line-{line}", "document_family": "invoice",
                "line_family": "fee", "trade_family": "legal", "work_mode": "fee",
                "recurrence": "one_time", "capital_context": "operating",
                "specific_assets": [], "contradictions": [], "confidence": .9,
            },
            "total_reconciliation_passed": True,
        },
    }


def _result(two_rows: bool = False) -> dict:
    rows = [_row(1), _row(2)] if two_rows else [_row(1)]
    invoice = {"source_file": "synthetic.pdf", "invoice_number": "INV-KNOWLEDGE", "rows": rows}
    return {"batch_id": BATCH, "all_invoices": [invoice]}


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "WEBAPP_DATA_ROOT", tmp_path / "runtime")
    monkeypatch.setattr(settings, "BATCHES_ROOT", tmp_path / "batches")
    for child in ("input", "processed", "trace"):
        (settings.BATCHES_ROOT / BATCH / child).mkdir(parents=True, exist_ok=True)
    return tmp_path


def _publish_history() -> None:
    fixtures = {
        resman_context_data.DatasetKind.VENDORS: b"Vendor List,,,,\nCompany,Company Abbreviation,Customer #,Status,Active\nExample Service,ES,1,Approved,Yes\n",
        resman_context_data.DatasetKind.PROPERTIES_UNITS: b"All Units,,,,\nExample Property,,,,\nUnit,Unit Type,Unit Status,Sq Ft,Lease Status\n101,1B1B,Ready,700,Current\n",
        resman_context_data.DatasetKind.GL_ACCOUNTS: b"Chart Of Accounts,,,\nNumber,Name,Type,Description\n6205,Legal Expense,Expense,Legal fees\n6600,Other Expense,Expense,Other\n",
        resman_context_data.DatasetKind.GENERAL_LEDGER: b"General Ledger,,,,,,,,\nDate,Reference,Property,Name,Description,Debit,Credit,Balance\n6205 Legal Expense,,,Beginning Balance:,,,,0.00\n01/15/2026,I-1,EP,Example Service,Filing,75.00,,75.00\n",
        resman_context_data.DatasetKind.INVOICE_HISTORY: b"Invoice Detail,,,,,,,,,\nNumber,Invoice Date / Property,,Act. Date / GL Account,Due Date,Description,,Total,Batch,PO\nExample Service,,,,,,,,,\nI-1,01/01/2026,,01/15/2026,01/31/2026,Filing,,75.00,,\n,EP,6205,,Filing,,75.00,,,,\n",
    }
    for dataset, content in fixtures.items():
        staged = resman_context_data.stage_import(TENANT, dataset, f"{dataset.value}.csv", content)
        assert staged.status == "preview_ready"
        resman_context_data.publish_import(TENANT, dataset, staged.import_id)
    context_intelligence.scan_resman(TENANT, actor="test")


def _correct(result: dict, *, edits: dict[int, dict[str, object]], benchmark: bool = False,
             learning: bool = False, rule: bool = False, bulk: bool = False):
    return human_adjudication.record_manual_edits(
        result=result, batch_id=BATCH, edits_by_index=edits,
        options=human_adjudication.AdjudicationOptions(
            rationale="Human verified the source evidence and accounting context.",
            add_to_benchmark=benchmark, approve_learning_example=learning,
            propose_reusable_rule=rule, bulk_scope_confirmed=bulk,
        ), actor=_actor(),
    )


def _simulate_proposed_rule(revision_id: str) -> None:
    event = next(
        item for item in human_adjudication.list_governance_events(TENANT, revision_id=revision_id)
        if item.event_type == "rule_proposed"
    )
    tenant_accounting_policies.simulate_policy(
        TENANT,
        str(event.details["tenant_policy_id"]),
        [tenant_accounting_policies.PolicySimulationLine(
            line_id="line-1",
            raw_description="Attorney filing fee",
            document_family="invoice",
            line_family="fee",
            trade_family="legal",
            work_mode="fee",
            current_gl="6205",
            candidate_gl_codes=["6205"],
        )],
        actor="test-controller",
    )


def _readiness(*, allowed: bool = True, snapshot: str = "ready-snapshot-1") -> dict:
    return {
        "contract_version": "accounting-readiness/1.0",
        "snapshot_id": snapshot,
        "export_allowed": allowed,
    }


def test_historical_prior_is_non_authoritative_and_snapshot_is_immutable():
    _publish_history()
    before = context_intelligence.vendor_detail(TENANT, context_intelligence.list_matrix(TENANT).items[0]["vendor_key"]).model_dump(mode="json")
    context = AccountingKnowledgeCore().line_context(tenant_id=TENANT, row=_row())
    assert context.historical_vendor_priors[0].gl_code == "6205"
    assert context.historical_vendor_priors[0].authoritative is False
    assert context.selection_authority is False
    _correct(_result(), edits={0: {"Location": "2"}})
    after = context_intelligence.vendor_detail(TENANT, context_intelligence.list_matrix(TENANT).items[0]["vendor_key"]).model_dump(mode="json")
    assert after == before


def test_benchmark_has_zero_production_effect_and_learning_is_candidate_only():
    result = _result()
    report = _correct(result, edits={0: {"GL Account": "6205"}}, benchmark=True)
    human_adjudication.decide_benchmark(report.revision_ids[0], approve=True, actor=_actor())
    core = AccountingKnowledgeCore()
    benchmark_only = core.line_context(tenant_id=TENANT, row=_row())
    assert core.benchmarks.approved(TENANT)
    assert benchmark_only.benchmark_examples_visible_to_production == 0
    assert benchmark_only.similar_approved_learning_examples == []

    human_adjudication.approve_learning(report.revision_ids[0], actor=_actor())
    learned = core.line_context(tenant_id=TENANT, row=_row())
    assert learned.similar_approved_learning_examples[0].gl_code == "6205"
    assert learned.similar_approved_learning_examples[0].candidate_only is True
    assert learned.selection_authority is False
    _, catalog = load_gl_catalog()
    candidates = []
    _extend_knowledge_candidates(candidates, learned, catalog)
    learning_candidate = next(item for item in candidates if item.source == "human_approved_learning_example")
    assert learning_candidate.base_score == .78
    assert learning_candidate.positive_evidence[0]["selection_authority"] is False
    support = _source_support(
        learning_candidate.source, {"historical_support": .05}, learning_candidate.base_score,
    )
    assert support["historical_support"] == pytest.approx(.039)


def test_approved_rule_is_a_constraint_and_engine_authority_is_unchanged():
    result = _result()
    report = _correct(result, edits={0: {"GL Account": "6205"}}, rule=True)
    with pytest.raises(ValueError, match="must be simulated"):
        human_adjudication.approve_rule(report.revision_ids[0], actor=_actor())
    _simulate_proposed_rule(report.revision_ids[0])
    human_adjudication.approve_rule(report.revision_ids[0], actor=_actor())
    context = AccountingKnowledgeCore().line_context(tenant_id=TENANT, row=_row())
    assert context.active_governed_rules[0].allowed_gl_codes == ["6205"]
    assert context.active_governed_rules[0].candidate_constraint_only is True
    assert context.selection_authority is False


def test_bulk_correction_deduplicates_learning_and_requires_confirmed_scope():
    result = _result(two_rows=True)
    with pytest.raises(ValueError, match="explicit human confirmation"):
        _correct(copy.deepcopy(result), edits={0: {"GL Account": "6205"}, 1: {"GL Account": "6205"}}, learning=True)
    report = _correct(result, edits={0: {"GL Account": "6205"}, 1: {"GL Account": "6205"}}, learning=True, bulk=True)
    assert report.recorded == 2
    assert report.learning_approvals == 1
    candidates = AccountingKnowledgeCore().line_context(tenant_id=TENANT, row=_row()).similar_approved_learning_examples
    assert len(candidates) == 1


def test_tenant_isolation_and_final_distribution_updates_only_after_approved_export():
    result = _result()
    _correct(result, edits={0: {"GL Account": "6205"}})
    core = AccountingKnowledgeCore()
    assert core.analytics(TENANT).final_posted_gl_distribution == {}
    other = _row()
    other["_meta"]["tenant_id"] = "other-tenant"
    with pytest.raises(PermissionError):
        core.line_context(tenant_id=TENANT, row=other)

    count = record_approved_export(
        tenant_id=TENANT, batch_id=BATCH, rows=result["all_invoices"][0]["rows"],
        readiness=_readiness(),
    )
    assert count == 1
    assert record_approved_export(
        tenant_id=TENANT, batch_id=BATCH, rows=result["all_invoices"][0]["rows"],
        readiness=_readiness(),
    ) == 0
    with pytest.raises(ValueError, match="export_allowed=true"):
        record_approved_export(
            tenant_id=TENANT, batch_id=BATCH, rows=result["all_invoices"][0]["rows"],
            readiness=_readiness(allowed=False, snapshot="blocked-snapshot"),
        )
    analytics = core.analytics(TENANT)
    assert analytics.approved_export_gl_distribution == {"6205": 1}
    assert analytics.posted_gl_distribution == {}
    assert analytics.final_posted_gl_distribution == {}
    assert analytics.ai_prediction_distribution == {"6205": 1}
    assert analytics.human_correction_distribution == {"6205": 1}
    assert core.analytics("other-tenant").final_posted_gl_distribution == {}


def test_cross_report_metrics_reflect_benchmark_learning_and_active_rule():
    result = _result()
    report = _correct(result, edits={0: {"GL Account": "6205"}}, benchmark=True, learning=True, rule=True)
    human_adjudication.decide_benchmark(report.revision_ids[0], approve=True, actor=_actor())
    _simulate_proposed_rule(report.revision_ids[0])
    human_adjudication.approve_rule(report.revision_ids[0], actor=_actor())
    analytics = AccountingKnowledgeCore().analytics(TENANT)
    assert analytics.approved_benchmark_count == 1
    assert analytics.approved_learning_count == 1
    assert analytics.active_rule_count == 1
    assert analytics.disagreement_rate == 0
    assert analytics.promotion_candidates[0]["automatic_promotion"] is False


def test_knowledge_core_api_uses_server_tenant_and_returns_impact(monkeypatch):
    result = _result()
    cache = settings.BATCHES_ROOT / BATCH / "processed" / "_webapp_result.json"
    cache.write_text(json.dumps(result), encoding="utf-8")
    monkeypatch.setattr(human_adjudication, "runtime_actor_context", lambda: _actor())
    client = TestClient(create_app())
    context = client.get(f"/api/knowledge-core/batches/{BATCH}/lines/0")
    assert context.status_code == 200
    assert context.json()["tenant_id"] == TENANT
    assert context.json()["selection_authority"] is False
    impact = client.post("/api/knowledge-core/impact", json={
        "batch_id": BATCH, "edits": {"0": {"GL Account": "6205"}},
        "add_to_benchmark": True, "approve_learning_example": True,
        "propose_reusable_rule": True,
    })
    assert impact.status_code == 200
    assert impact.json()["benchmark_examples"] == 1
    assert impact.json()["learning_examples"] == 1
    assert impact.json()["rule_proposals"] == 1

    hostile = _result()
    hostile["all_invoices"][0]["rows"][0]["_meta"]["tenant_id"] = "other-tenant"
    cache.write_text(json.dumps(hostile), encoding="utf-8")
    assert client.get(f"/api/knowledge-core/batches/{BATCH}/lines/0").status_code == 403
    assert client.post("/api/knowledge-core/impact", json={
        "batch_id": BATCH, "edits": {"0": {"GL Account": "6205"}},
    }).status_code == 403
    assert client.get(
        f"/api/batches/{BATCH}/adjudications/evidence/0/GL%20Account/crop"
    ).status_code == 403


def test_stale_historical_profile_is_detected_and_excluded(monkeypatch):
    monkeypatch.setattr(
        context_intelligence, "status",
        lambda tenant_id: {"tenant_id": tenant_id, "state": "stale"},
    )
    context = AccountingKnowledgeCore().line_context(tenant_id=TENANT, row=_row())
    assert context.historical_profile_state == "stale"
    assert context.historical_vendor_priors == []
    assert context.historical_property_priors == []
    assert context.vendor_property_joint_priors == []
    assert "historical_profile_stale" in {item.code for item in context.contradictions}


def test_accounting_cache_dependencies_are_tenant_scoped_and_governance_aware():
    before = accounting_artifact_cache.dependency_versions(TENANT)
    other = accounting_artifact_cache.dependency_versions("other-tenant")
    assert before["tenant_id"] == TENANT
    assert other["tenant_id"] == "other-tenant"
    assert before != other

    report = _correct(_result(), edits={0: {"GL Account": "6205"}}, learning=True)
    assert report.learning_approvals == 1
    after = accounting_artifact_cache.dependency_versions(TENANT)
    assert after["human_corrections"] != before["human_corrections"]
    assert after["knowledge_governance"] != before["knowledge_governance"]
