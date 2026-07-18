import copy
import json
from datetime import datetime, timezone

import pytest

from webapp.backend.services import accounting_assistant
from webapp.backend.services import approved_invoice_corrections as approved
from webapp.backend.services import batch_store, revisions
from webapp.backend.services import operator_activity_log
from webapp.backend.services.invoice_identity import build_invoice_identities


def _row():
    return {
        "Invoice Number": "38680",
        "Vendor": "Example Legal Office",
        "Bill or Credit": "Bill",
        "Invoice Date": "2026-03-01",
        "Accounting Date": "2026-03-01",
        "Invoice Description": "Legal services",
        "Line Item Number": 2,
        "Property Abbreviation": "TEST",
        "Location": "A",
        "GL Account": "",
        "Line Item Description": "Attorney Fee - Writ",
        "Quantity": 1,
        "Unit Price": 75,
        "Amount": 75,
        "Expense Type": "General",
        "Is Replacement Reserve": False,
        "Document Url": "https://example.invalid/legal.pdf",
        "_meta": {
            "line_item_id": "legal-line-2",
            "source_file": "legal.pdf",
            "source_text": {
                "raw_description": "Attorney Fee - Writ",
                "raw_invoice_description": "Legal filing and eviction costs",
            },
        },
    }


def _result():
    invoice = {
        "source_file": "legal.pdf",
        "invoice_number": "38680",
        "rows": [_row()],
    }
    return {
        "batch_id": "batch-legal",
        "all_invoices": [invoice],
        "by_vendor": {"legal": {"invoices": [copy.deepcopy(invoice)]}},
    }


@pytest.fixture
def isolated_runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(approved.settings, "WEBAPP_DATA_ROOT", tmp_path)
    monkeypatch.setattr(accounting_assistant.settings, "WEBAPP_DATA_ROOT", tmp_path)
    monkeypatch.setattr(operator_activity_log.settings, "WEBAPP_DATA_ROOT", tmp_path)
    return tmp_path


def test_approved_gl_is_candidate_selected_by_engine_and_survives_reprocess(isolated_runtime):
    result = _result()
    group_id = build_invoice_identities(result["all_invoices"])[0].group_id
    approved.approve(
        batch_id="batch-legal",
        invoice_group_id=group_id,
        interaction_id="aai_durable_test",
        corrections=[{
            "row_index": 0,
            "field": "GL Account",
            "new_value": "6205",
            "rationale": "Observed attorney writ fee is a legal/eviction cost.",
            "evidence": ["Attorney Fee - Writ"],
        }],
        result=result,
        actor="reviewer",
    )

    first = approved.apply_to_result(result, batch_id="batch-legal")
    row = result["all_invoices"][0]["rows"][0]
    assert first.matched == 1
    assert row["GL Account"] == "6205"
    assert row["_meta"]["accounting_decision"]["decision_source"] == "AccountingDecisionEngine"
    assert "manual_approved_operator_correction" in {
        item["source"] for item in row["_meta"]["accounting_decision"]["candidates_ranked"]
    }

    fresh = _result()
    replay = approved.apply_to_result(fresh, batch_id="batch-legal")
    assert replay.matched == 1
    assert fresh["all_invoices"][0]["rows"][0]["GL Account"] == "6205"
    assert fresh["by_vendor"]["legal"]["invoices"][0]["rows"][0]["GL Account"] == "6205"


def test_interaction_history_and_approval_decision_are_durable(
    isolated_runtime, monkeypatch, tmp_path,
):
    result = _result()
    group_id = build_invoice_identities(result["all_invoices"])[0].group_id
    processed = tmp_path / "batch" / "processed"
    processed.mkdir(parents=True)
    cache = processed / "_webapp_result.json"
    cache.write_text(json.dumps(result), encoding="utf-8")
    monkeypatch.setattr(batch_store, "get_processed_dir", lambda _batch_id: processed)
    monkeypatch.setattr(revisions, "current_revision_id", lambda _batch_id: None)

    chat = accounting_assistant.AssistantChatResult(
        interaction_id="aai_history_test",
        batch_id="batch-legal",
        invoice_group_id=group_id,
        assistant_message="Use the payable legal/eviction account.",
        corrections=[accounting_assistant.ProposedInvoiceCorrection(
            row_index=0,
            field="GL Account",
            new_value="6205",
            rationale="Observed attorney writ fee supports legal/eviction expense.",
            evidence=["Attorney Fee - Writ"],
        )],
        requires_correction_confirmation=True,
        requires_rule_confirmation=False,
        provider_profile_id="test-accounting",
        estimated_cost_usd=0,
        created_at=datetime.now(timezone.utc),
        correction_status="pending",
    )
    accounting_assistant._write_interaction(chat, user_message="Correct this legal fee.")

    history = accounting_assistant.list_interactions(
        batch_id="batch-legal", invoice_group_id=group_id,
    )
    assert [item["user_message"] for item in history] == ["Correct this legal fee."]

    decision = accounting_assistant.decide_corrections(
        chat.interaction_id, approve=True, actor="reviewer",
    )
    assert decision["result"]["correction_status"] == "applied"
    assert decision["result"]["requires_correction_confirmation"] is False
    assert json.loads(cache.read_text(encoding="utf-8"))["all_invoices"][0]["rows"][0]["GL Account"] == "6205"

    reloaded = accounting_assistant.list_interactions(
        batch_id="batch-legal", invoice_group_id=group_id,
    )
    assert reloaded[0]["result"]["correction_status"] == "applied"
    events = operator_activity_log.list_events(batch_id="batch-legal")
    assert events[0].event_type == "ai_corrections_applied"
    assert events[0].source == "ai"
    assert len([event for event in events if event.event_type == "ai_corrections_applied"]) == 1


def test_pre_activity_log_approval_is_visible_without_mutating_raw_log(isolated_runtime):
    result = _result()
    group_id = build_invoice_identities(result["all_invoices"])[0].group_id
    approved.approve(
        batch_id="batch-legacy-audit",
        invoice_group_id=group_id,
        interaction_id="aai_before_activity_contract",
        corrections=[{
            "row_index": 0,
            "field": "GL Account",
            "new_value": "6205",
            "rationale": "Observed legal writ fee supports legal/eviction expense.",
            "evidence": ["Attorney Fee - Writ"],
        }],
        result=result,
        actor="historical_reviewer",
    )

    events = operator_activity_log.list_events(batch_id="batch-legacy-audit")
    assert len(events) == 1
    assert events[0].event_type == "ai_corrections_applied"
    assert events[0].actor == "historical_reviewer"
    assert events[0].details["legacy_adapter"] is True
    assert events[0].details["interaction_id"] == "aai_before_activity_contract"
    assert not (isolated_runtime / "operator_activity" / "batch-legacy-audit.jsonl").exists()


def test_generated_description_change_does_not_change_source_fingerprint():
    row = _row()
    before = approved.line_fingerprint(row)
    row["Line Item Description"] = "Generated operator-friendly explanation"
    assert approved.line_fingerprint(row) == before


def test_human_gl_correction_cannot_cross_an_incompatible_semantic_family(isolated_runtime):
    result = _result()
    row = result["all_invoices"][0]["rows"][0]
    row["Line Item Description"] = "Electrical receptacle material"
    row["_meta"]["source_text"]["raw_description"] = "Electrical receptacle material"
    result["by_vendor"]["legal"]["invoices"][0]["rows"][0] = copy.deepcopy(row)
    group_id = build_invoice_identities(result["all_invoices"])[0].group_id
    approved.approve(
        batch_id="batch-legal", invoice_group_id=group_id,
        interaction_id="aai_incompatible_test",
        corrections=[{
            "row_index": 0, "field": "GL Account", "new_value": "6205",
            "rationale": "Deliberately incompatible test proposal.", "evidence": [],
        }],
        result=result, actor="reviewer",
    )
    approved.apply_to_result(result, batch_id="batch-legal")
    assert result["all_invoices"][0]["rows"][0]["GL Account"] != "6205"


def test_human_resolution_can_disambiguate_generic_legal_filing_fee(isolated_runtime):
    result = _result()
    row = result["all_invoices"][0]["rows"][0]
    row["Line Item Description"] = "Writ Filing Fee"
    row["_meta"]["source_text"]["raw_description"] = "Writ Filing Fee"
    result["by_vendor"]["legal"]["invoices"][0]["rows"][0] = copy.deepcopy(row)
    group_id = build_invoice_identities(result["all_invoices"])[0].group_id
    approved.approve(
        batch_id="batch-legal", invoice_group_id=group_id,
        interaction_id="aai_generic_fee_test",
        corrections=[{
            "row_index": 0, "field": "GL Account", "new_value": "6205",
            "rationale": "A writ filing fee is a legal/eviction cost, not a utility connect fee.",
            "evidence": ["Writ Filing Fee"],
        }],
        result=result, actor="reviewer",
    )
    approved.apply_to_result(result, batch_id="batch-legal")
    selected = result["all_invoices"][0]["rows"][0]
    assert selected["GL Account"] == "6205"
    assert selected["_meta"]["accounting_decision"]["decision_source"] == "AccountingDecisionEngine"


def test_activity_log_separates_manual_ai_and_rule_sources(isolated_runtime):
    for source in ("manual", "ai", "rule"):
        operator_activity_log.record(
            batch_id="batch-audit", event_type=f"{source}_event", source=source,
            actor="tester", summary=f"{source} change",
        )
    events = operator_activity_log.list_events(batch_id="batch-audit")
    assert {event.source for event in events} == {"manual", "ai", "rule"}
    assert all(event.contract_version == "operator-activity/1.0" for event in events)
