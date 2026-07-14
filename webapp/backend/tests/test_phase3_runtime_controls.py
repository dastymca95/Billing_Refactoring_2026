import json
from datetime import date
from decimal import Decimal

import pytest

from webapp.backend.services.accounting_contracts import AccountingDecision, DocumentFacts, LineItemFacts
from webapp.backend.services.accounting_reasoning_shadow import StrongAccountingReasonerShadow
from webapp.backend.services.ai_runtime_controls import (
    BudgetExceeded, BudgetLedger, RuntimeBudget, TraceSink, VersionedJsonCache,
)
from webapp.backend.services.model_registry import ModelCapability, ModelRole


def test_cost_and_latency_budgets_fail_closed():
    ledger = BudgetLedger(RuntimeBudget(max_cost_usd=.10, max_latency_ms=1000))
    ledger.record(cost_usd=.05, latency_ms=400)
    with pytest.raises(BudgetExceeded, match="cost"):
        ledger.authorize(estimated_cost_usd=.06, estimated_latency_ms=1)
    with pytest.raises(BudgetExceeded, match="latency"):
        ledger.authorize(estimated_cost_usd=0, estimated_latency_ms=601)


def test_facts_and_reasoning_cache_keys_cannot_collide(tmp_path):
    payload = {"document": "same"}
    facts = VersionedJsonCache(tmp_path, "facts", "1")
    reasoning = VersionedJsonCache(tmp_path, "accounting_reasoning", "1")
    assert facts.key(payload) != reasoning.key(payload)
    facts.put(facts.key(payload), {"raw": "preserved"})
    assert reasoning.get(facts.key(payload)) is None


def test_shadow_reasoner_never_applies_its_result_and_writes_safe_trace(tmp_path):
    facts = DocumentFacts(schema_version="1", document_id="private-doc-123", invoice_id="inv",
                          invoice_date=date(2026, 1, 1), total_amount=Decimal("10"),
                          line_items=[LineItemFacts(line_item_id="l1", raw_description="repair", amount=Decimal("10"))],
                          extraction_route="deterministic")
    decision = AccountingDecision(decision_id="d", line_item_id="l1", selected_gl_code="6500", selected_gl_name="Repairs",
                                  decision_source="AccountingDecisionEngine", why_selected="typed evidence",
                                  confidence=.8, review_required=False, review_blocking=False,
                                  decision_version="1", semantic_version="1", catalog_version="1")
    capability = ModelCapability("strong", "mock", True, "test", frozenset({ModelRole.ACCOUNTING_REASONING}))
    trace_path = tmp_path / "trace.jsonl"
    runner = StrongAccountingReasonerShadow(capability, lambda _payload: {"selected_gl_code": "9999", "reason": "shadow"},
                                            BudgetLedger(RuntimeBudget(1, 5000)), TraceSink(trace_path))
    result = runner.evaluate(facts, decision, estimated_cost_usd=.01, estimated_latency_ms=1)
    assert result.applied is False
    assert result.authoritative_gl == "6500" and result.shadow_gl == "9999"
    assert decision.selected_gl_code == "6500"
    trace = json.loads(trace_path.read_text().strip())
    assert trace["shadow_only"] is True
    assert "private-doc-123" not in trace_path.read_text()
