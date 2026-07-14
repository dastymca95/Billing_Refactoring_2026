"""Strong-model accounting evaluation in non-authoritative shadow mode."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .accounting_contracts import AccountingDecision, DocumentFacts, model_dict
from .ai_runtime_controls import BudgetLedger, DecisionTrace, Timer, TraceSink, hashed_document_id
from .model_registry import ModelCapability


@dataclass(frozen=True)
class ShadowComparison:
    model_id: str
    authoritative_gl: str | None
    shadow_gl: str | None
    same: bool
    shadow_reason: str
    cost_usd: float
    latency_ms: int
    applied: bool = False


class StrongAccountingReasonerShadow:
    """Evaluates typed facts but cannot mutate rows or decisions."""
    def __init__(self, capability: ModelCapability, invoke: Callable[[Mapping[str, Any]], Mapping[str, Any]],
                 ledger: BudgetLedger, trace_sink: TraceSink | None = None) -> None:
        if not capability.available:
            raise ValueError("strong reasoner capability was not discovered")
        self.capability = capability
        self.invoke = invoke
        self.ledger = ledger
        self.trace_sink = trace_sink

    def evaluate(self, facts: DocumentFacts, authoritative: AccountingDecision, *,
                 estimated_cost_usd: float, estimated_latency_ms: int) -> ShadowComparison:
        self.ledger.authorize(estimated_cost_usd=estimated_cost_usd, estimated_latency_ms=estimated_latency_ms)
        payload = {"contract": "accounting-shadow/1.0", "facts": model_dict(facts),
                   "authoritative_decision": model_dict(authoritative)}
        with Timer() as timer:
            response = dict(self.invoke(payload))
        latency = max(timer.elapsed_ms, estimated_latency_ms)
        self.ledger.record(cost_usd=estimated_cost_usd, latency_ms=latency)
        shadow_gl = str(response.get("selected_gl_code") or "").strip() or None
        authoritative_gl = authoritative.selected_gl_code
        result = ShadowComparison(self.capability.model_id, authoritative_gl, shadow_gl,
                                  shadow_gl == authoritative_gl, str(response.get("reason") or ""),
                                  estimated_cost_usd, latency, applied=False)
        if self.trace_sink:
            self.trace_sink.append(DecisionTrace(
                "reasoning-trace/1.0", hashed_document_id(facts.document_id), "strong_reasoning_shadow",
                "ambiguous_accounting_decision", self.capability.model_id, True, latency,
                estimated_cost_usd, "accounting_reasoning", "same" if result.same else "different"))
        return result
