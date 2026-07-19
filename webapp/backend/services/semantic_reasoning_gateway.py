"""Bounded AI escalation for unresolved semantics; never selects a final GL."""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from webapp.backend import settings

from . import ai_provider, ai_runtime_trace
from .accounting_contracts import EvidenceReference, GLCandidate, LineItemFacts, SemanticClassification
from .canonical_semantics import (
    resolve_canonical_concept,
    semantic_candidate_cache_key,
    tenant_accounting_context_fingerprint,
)
from .gl_catalog import load_gl_catalog
from .provider_capabilities import ModelProfileRole, ProfileLoader


SEMANTIC_REASONING_VERSION = "semantic-context-reasoning/1.2"
LINE_FAMILIES = {"materials", "labor_service", "utility", "fee", "subscription_membership",
                 "legal", "insurance", "unknown"}
WORK_MODES = {"material_purchase", "labor_service", "recurring_service", "renewal",
              "one_time_fee", "unknown"}


class SemanticReasoningProposal(BaseModel):
    line_family: str
    trade_family: str
    work_mode: str
    confidence: float = Field(ge=0, le=1)
    evidence_quotes: list[str] = Field(default_factory=list, max_length=8)
    candidate_gl_codes: list[str] = Field(default_factory=list, max_length=8)
    reasoning_summary: str


class SemanticReasoningResult(BaseModel):
    semantics: SemanticClassification
    candidates: list[GLCandidate] = Field(default_factory=list)
    trace: dict[str, Any]


class InvoiceSemanticLineRequest(BaseModel):
    facts: LineItemFacts
    semantics: SemanticClassification
    candidate_gl_codes: list[str] = Field(default_factory=list, max_length=12)


class InvoiceSemanticProposal(BaseModel):
    line_item_id: str
    line_family: str
    trade_family: str
    work_mode: str
    confidence: float = Field(ge=0, le=1)
    evidence_quotes: list[str] = Field(default_factory=list, max_length=8)
    candidate_gl_codes: list[str] = Field(default_factory=list, max_length=8)
    reasoning_summary: str


class InvoiceSemanticProposalEnvelope(BaseModel):
    proposals: list[InvoiceSemanticProposal]


def enrich_unknown_semantics(*, facts: LineItemFacts, semantics: SemanticClassification,
                             document_id: str, document_context: str,
                             force_no_safe_decision: bool = False,
                             tenant_id: str | None = None) -> SemanticReasoningResult:
    """Escalate only unresolved semantics and fail closed to the original result."""
    if not _enabled() or (not force_no_safe_decision and not _needs_escalation(semantics)):
        return SemanticReasoningResult(semantics=semantics, trace={"route": "deterministic", "called": False})
    profile = next((item for item in ProfileLoader().load()
                    if item.role is ModelProfileRole.ACCOUNTING_REASONING and item.enabled
                    and item.credentials_present), None)
    if profile is None:
        return SemanticReasoningResult(semantics=semantics,
            trace={"route": "manual_review", "called": False, "failure_code": "reasoning_profile_unavailable"})

    _, catalog = load_gl_catalog()
    source = _source_text(facts)
    context = document_context[:12000]
    concept = resolve_canonical_concept(
        source, line_family=semantics.line_family,
        trade_family=semantics.trade_family, work_mode=semantics.work_mode,
    )
    cache_key = semantic_candidate_cache_key(
        [concept], candidate_gl_codes=[sorted(catalog)], provider=profile.provider,
        profile_id=profile.profile_id, model_id=profile.model_id,
        tenant_context_fingerprint=tenant_accounting_context_fingerprint(tenant_id=tenant_id),
        version=SEMANTIC_REASONING_VERSION,
    )
    cache_path = (settings.WEBAPP_DATA_ROOT / "ai_cache" / "semantic_reasoning"
                  / SEMANTIC_REASONING_VERSION / f"{cache_key}.json") if cache_key else None
    cached = _read_cache(cache_path) if cache_path else None
    started = time.perf_counter()
    try:
        proposal = (_proposal_from_canonical_cache(cached, concept, source) if cached
                    else _request(profile, source, context, facts, catalog))
        if not cached and cache_path:
            _write_cache(cache_path, _candidate_only_payload(proposal))
        resolved, candidates = _validate_and_adapt(
            proposal, semantics, facts, document_id, source, context, catalog,
            replace_existing=force_no_safe_decision,
        )
        latency = int((time.perf_counter() - started) * 1000)
        return SemanticReasoningResult(semantics=resolved, candidates=candidates, trace={
            "route": "ai_contextual_semantics", "called": not bool(cached), "cache_hit": bool(cached),
            "profile_id": profile.profile_id, "model_id": profile.model_id,
            "latency_ms": latency, "estimated_cost_usd": _estimated_cost(source, context),
            "trigger": "no_safe_deterministic_decision" if force_no_safe_decision else "unknown_semantics",
            "version": SEMANTIC_REASONING_VERSION,
            "canonical_concept": concept.concept_id,
            "semantic_cache_key": cache_key,
        })
    except Exception as exc:
        return SemanticReasoningResult(semantics=semantics, trace={
            "route": "manual_review", "called": True, "profile_id": profile.profile_id,
            "failure_code": type(exc).__name__, "version": SEMANTIC_REASONING_VERSION,
        })


def enrich_invoice_semantics(
    *,
    lines: list[InvoiceSemanticLineRequest],
    document_id: str,
    document_context: str,
    tenant_id: str | None = None,
) -> dict[str, SemanticReasoningResult]:
    """Resolve all genuinely blocked lines in one bounded provider request.

    The result remains candidate-only.  Callers must pass every proposal back
    through ``AccountingDecisionEngine``.  Deterministically complete rows are
    intentionally absent from ``lines`` and therefore cost zero.
    """
    if not lines:
        return {}
    if not _enabled():
        return _manual_results(lines, "semantic_reasoning_disabled")
    profile = _select_accounting_profile()
    if profile is None:
        return _manual_results(lines, "reasoning_profile_unavailable")

    _, catalog = load_gl_catalog()
    context = (document_context or "")[:12000]
    request_lines = []
    concepts = []
    allowed_by_line: list[list[str]] = []
    for item in lines:
        source = _source_text(item.facts)
        allowed = [code for code in dict.fromkeys(item.candidate_gl_codes)
                   if code in catalog and catalog[code].payable][:12]
        concepts.append(resolve_canonical_concept(
            source, line_family=item.semantics.line_family,
            trade_family=item.semantics.trade_family, work_mode=item.semantics.work_mode,
        ))
        allowed_by_line.append(allowed)
        request_lines.append({
            "line_item_id": item.facts.line_item_id,
            "source_text": source,
            "quantity": str(item.facts.quantity) if item.facts.quantity is not None else None,
            "unit_price": str(item.facts.unit_price) if item.facts.unit_price is not None else None,
            "amount": str(item.facts.amount) if item.facts.amount is not None else None,
            "current_semantics": {
                "line_family": item.semantics.line_family,
                "trade_family": item.semantics.trade_family,
                "work_mode": item.semantics.work_mode,
            },
            "allowed_candidate_gl_codes": allowed,
        })
    cache_key = semantic_candidate_cache_key(
        concepts, candidate_gl_codes=allowed_by_line, provider=profile.provider,
        profile_id=profile.profile_id, model_id=profile.model_id,
        tenant_context_fingerprint=tenant_accounting_context_fingerprint(tenant_id=tenant_id),
        version=SEMANTIC_REASONING_VERSION,
    )
    cache_path = (settings.WEBAPP_DATA_ROOT / "ai_cache" / "semantic_reasoning_grouped"
                  / SEMANTIC_REASONING_VERSION / f"{cache_key}.json") if cache_key else None
    cached = _read_cache(cache_path) if cache_path else None
    cost_payload = {
        "version": SEMANTIC_REASONING_VERSION, "mode": "invoice_grouped",
        "provider": profile.provider, "profile_id": profile.profile_id,
        "model": profile.model_id, "context": context, "lines": request_lines,
    }
    estimated_cost = _estimate_profile_cost(profile, cost_payload, len(lines))
    max_cost = _float_setting("AI_MAX_SEMANTIC_COST_PER_INVOICE_USD", 0.08)
    if not cached and estimated_cost > max_cost:
        return _manual_results(lines, "semantic_cost_budget_exceeded", {
            "estimated_cost_usd": estimated_cost,
            "max_cost_usd": max_cost,
        })

    started = time.perf_counter()
    try:
        envelope = (_group_from_canonical_cache(cached, concepts, request_lines) if cached
                    else _request_invoice_group(profile, request_lines, context, catalog))
        if not cached and cache_path:
            _write_cache(cache_path, {
                "cache_contract": "canonical-semantic-candidates/1.0",
                "proposals": [_candidate_only_payload(proposal)
                              for proposal in envelope.proposals],
            })
        proposals = {proposal.line_item_id: proposal for proposal in envelope.proposals}
        results: dict[str, SemanticReasoningResult] = {}
        latency = int((time.perf_counter() - started) * 1000)
        for index, item in enumerate(lines):
            proposal = proposals.get(item.facts.line_item_id)
            if proposal is None:
                results[item.facts.line_item_id] = _manual_result(
                    item, "grouped_response_line_missing")
                continue
            legacy = SemanticReasoningProposal(
                line_family=proposal.line_family,
                trade_family=proposal.trade_family,
                work_mode=proposal.work_mode,
                confidence=proposal.confidence,
                evidence_quotes=proposal.evidence_quotes,
                candidate_gl_codes=[code for code in proposal.candidate_gl_codes
                                    if code in item.candidate_gl_codes],
                reasoning_summary=proposal.reasoning_summary,
            )
            resolved, candidates = _validate_and_adapt(
                legacy, item.semantics, item.facts, document_id,
                _source_text(item.facts), context, catalog,
            )
            results[item.facts.line_item_id] = SemanticReasoningResult(
                semantics=resolved,
                candidates=candidates,
                trace={
                    "route": "ai_contextual_semantics_grouped",
                    "called": not bool(cached),
                    "cache_hit": bool(cached),
                    "profile_id": profile.profile_id,
                    "model_id": profile.model_id,
                    "latency_ms": latency,
                    "estimated_cost_usd": estimated_cost,
                    "invoice_request_line_count": len(lines),
                    "version": SEMANTIC_REASONING_VERSION,
                    "canonical_concept": concepts[index].concept_id,
                    "semantic_cache_key": cache_key,
                },
            )
        return results
    except Exception as exc:
        return _manual_results(lines, type(exc).__name__, {
            "profile_id": profile.profile_id,
            "estimated_cost_usd": estimated_cost,
        })


def _request_invoice_group(profile, request_lines: list[dict[str, Any]],
                           context: str, catalog: dict) -> InvoiceSemanticProposalEnvelope:
    allowed_codes = sorted({code for line in request_lines
                            for code in line["allowed_candidate_gl_codes"]})
    accounts = [{"gl_code": code, "name": catalog[code].gl_name,
                 "family": catalog[code].gl_family,
                 "trades": catalog[code].trade_families,
                 "work_modes": catalog[code].compatible_work_modes}
                for code in allowed_codes]
    system = (
        "Classify unresolved invoice lines from observable facts. Return strict JSON only. "
        "Do not decide readiness, export, property, or a final GL. Each evidence quote must "
        "appear verbatim in the supplied line or document context. Distinguish the economic "
        "subject from the payment form: subscription_membership means recurring access, a "
        "license, membership, or renewal without performed trade labor; labor_service means "
        "people performed maintenance, installation, repair, or another service; materials "
        "means physical goods were purchased. Never call software access labor_service merely "
        "because the vendor hosts or supports it. Explain why the proposed candidates fit the "
        "line evidence and why materially different allowed candidates do not."
    )
    user = {
        "task": "resolve_invoice_line_semantics_in_one_request",
        "document_context": context,
        "lines": request_lines,
        "candidate_catalog_subset": accounts,
        "allowed_line_families": sorted(LINE_FAMILIES),
        "allowed_work_modes": sorted(WORK_MODES),
        "response_schema": {"proposals": [{
            "line_item_id": "exact supplied id", "line_family": "string",
            "trade_family": "string", "work_mode": "string", "confidence": "0..1",
            "evidence_quotes": ["verbatim quote"], "candidate_gl_codes": ["allowed code"],
            "reasoning_summary": "plain-language rationale",
        }]},
    }
    payload = {
        "model": profile.model_id,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": json.dumps(user)}],
    }
    if profile.provider == "deepseek":
        thinking_enabled = os.environ.get(
            "DEEPSEEK_ACCOUNTING_THINKING_ENABLED", "true"
        ).strip().lower() in {"1", "true", "yes", "on"}
        payload["thinking"] = {"type": "enabled" if thinking_enabled else "disabled"}
        if thinking_enabled:
            payload["reasoning_effort"] = os.environ.get(
                "DEEPSEEK_ACCOUNTING_REASONING_EFFORT", "medium"
            ).strip() or "medium"
    max_tokens = min(4000, max(800, 300 + len(request_lines) * 220))
    payload.update(ai_provider._completion_controls(profile.provider, max_tokens))
    ai_runtime_trace.update_context(
        stage="accounting_semantic_reasoning",
        provider=profile.provider,
        model=profile.model_id,
        profile_id=profile.profile_id,
        media_bytes=0,
        media_pixels=0,
    )
    content = ai_provider._send_chat_completion(
        provider=profile.provider, payload=payload,
        api_key_override=profile.api_key.get_secret_value() if profile.api_key else None,
        base_url_override=profile.base_url,
        timeout_seconds_override=profile.timeout_seconds,
        max_attempts_override=profile.max_retries + 1,
    )
    result = InvoiceSemanticProposalEnvelope(**ai_provider._extract_json_object(content))
    ai_runtime_trace.record_schema_result("valid")
    return result


def _select_accounting_profile():
    profiles = [item for item in ProfileLoader().load()
                if item.role is ModelProfileRole.ACCOUNTING_REASONING
                and item.enabled and item.credentials_present]
    if not profiles:
        return None
    verified = _verified_profile_ids()
    eligible = [item for item in profiles
                if item.profile_id.startswith("runtime-") or item.profile_id in verified]
    if not eligible:
        return None
    return min(eligible, key=lambda item: (
        item.input_cost_usd_per_million is None or item.output_cost_usd_per_million is None,
        (item.input_cost_usd_per_million or 0) + (item.output_cost_usd_per_million or 0),
        item.routing_priority,
        item.profile_id,
    ))


def _verified_profile_ids() -> set[str]:
    ids = {value.strip() for value in os.environ.get(
        "AI_COST_ROUTING_VERIFIED_PROFILE_IDS", "").split(",") if value.strip()}
    report_path = os.environ.get("AI_PROVIDER_CAPABILITY_REPORT", "").strip()
    if report_path:
        try:
            payload = json.loads(Path(report_path).read_text(encoding="utf-8"))
            ids.update(str(row.get("profile_id")) for row in payload.get("profiles", [])
                       if row.get("health_status") == "healthy" and row.get("profile_id"))
        except (OSError, ValueError, TypeError):
            pass
    return ids


def _estimate_profile_cost(profile, payload: dict[str, Any], line_count: int) -> float:
    input_tokens = len(json.dumps(payload, default=str)) / 4
    output_tokens = min(4000, max(800, 300 + line_count * 220))
    input_rate = profile.input_cost_usd_per_million
    output_rate = profile.output_cost_usd_per_million
    if input_rate is None or output_rate is None:
        input_rate, output_rate = 3.0, 15.0
    return round(input_tokens / 1_000_000 * input_rate
                 + output_tokens / 1_000_000 * output_rate, 6)


def _float_setting(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _manual_result(item: InvoiceSemanticLineRequest, failure_code: str,
                   extra: dict[str, Any] | None = None) -> SemanticReasoningResult:
    return SemanticReasoningResult(
        semantics=item.semantics,
        trace={"route": "manual_review", "called": False,
               "failure_code": failure_code, "version": SEMANTIC_REASONING_VERSION,
               **(extra or {})},
    )


def _manual_results(lines: list[InvoiceSemanticLineRequest], failure_code: str,
                    extra: dict[str, Any] | None = None) -> dict[str, SemanticReasoningResult]:
    return {item.facts.line_item_id: _manual_result(item, failure_code, extra)
            for item in lines}


def _enabled() -> bool:
    if os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get("AI_SEMANTIC_REASONING_TEST_ENABLED") != "1":
        return False
    raw = os.environ.get("AI_SEMANTIC_REASONING_ENABLED")
    enabled = settings.AI_ASSIST_ENABLED if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return False
    return any(
        profile.role is ModelProfileRole.ACCOUNTING_REASONING
        and profile.enabled
        and profile.credentials_present
        for profile in ProfileLoader().load()
    )


def _needs_escalation(semantics: SemanticClassification) -> bool:
    return "unknown" in {semantics.line_family, semantics.work_mode} or bool(semantics.contradictions)


def _source_text(facts: LineItemFacts) -> str:
    return str(facts.raw_description or facts.raw_activity or facts.normalized_description
               or facts.normalized_activity or "").strip()


def _request(profile, source: str, context: str, facts: LineItemFacts, catalog: dict) -> SemanticReasoningProposal:
    accounts = [{"gl_code": code, "name": item.gl_name, "family": item.gl_family,
                 "trades": item.trade_families, "work_modes": item.compatible_work_modes}
                for code, item in catalog.items() if item.payable]
    system = (
        "You classify accounting semantics from observable invoice facts. Return strict JSON only. "
        "Do not decide readiness, export authorization, property, or a final GL. "
        "candidate_gl_codes are non-authoritative candidates from the supplied payable chart. "
        "Use evidence_quotes copied from source/context; never invent product meaning. Distinguish physical goods from labor/services."
    )
    user = {"task": "resolve_unknown_line_semantics", "line_source_text": source,
            "document_context": context, "quantity": str(facts.quantity) if facts.quantity is not None else None,
            "unit_price": str(facts.unit_price) if facts.unit_price is not None else None,
            "amount": str(facts.amount) if facts.amount is not None else None,
            "allowed_line_families": sorted(LINE_FAMILIES), "allowed_work_modes": sorted(WORK_MODES),
            "payable_gl_catalog": accounts,
            "response_schema": {"line_family": "string", "trade_family": "string", "work_mode": "string",
                "confidence": "0..1", "evidence_quotes": ["verbatim source quote"],
                "candidate_gl_codes": ["chart code"], "reasoning_summary": "plain-language rationale"}}
    payload = {"model": profile.model_id, "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(user)}]}
    if profile.provider == "openai":
        payload.update({"max_completion_tokens": 1200, "reasoning_effort": "medium"})
    else:
        payload.update({"max_tokens": 1200, "temperature": 0})
    content = ai_provider._send_chat_completion(provider=profile.provider, payload=payload,
        api_key_override=profile.api_key.get_secret_value() if profile.api_key else None,
        base_url_override=profile.base_url, timeout_seconds_override=profile.timeout_seconds,
        max_attempts_override=profile.max_retries + 1)
    return SemanticReasoningProposal(**ai_provider._extract_json_object(content))


def _validate_and_adapt(proposal: SemanticReasoningProposal, original: SemanticClassification,
                        facts: LineItemFacts, document_id: str, source: str, context: str,
                        catalog: dict, *, replace_existing: bool = False
                        ) -> tuple[SemanticClassification, list[GLCandidate]]:
    if proposal.line_family not in LINE_FAMILIES or proposal.work_mode not in WORK_MODES:
        raise ValueError("semantic_enum_invalid")
    haystack = f"{source}\n{context}".casefold()
    quotes = [quote.strip() for quote in proposal.evidence_quotes
              if quote.strip() and quote.strip().casefold() in haystack]
    if not quotes:
        raise ValueError("source_grounding_missing")
    updates = {
        "line_family": proposal.line_family if replace_existing or original.line_family == "unknown" else original.line_family,
        "trade_family": proposal.trade_family if replace_existing or original.trade_family == "unknown" else original.trade_family,
        "work_mode": proposal.work_mode if replace_existing or original.work_mode == "unknown" else original.work_mode,
        "confidence": max(original.confidence, proposal.confidence),
        "positive_evidence": list(original.positive_evidence) + [EvidenceReference(
            document_id=document_id, text=quote, source_type="document_context",
            extraction_method=SEMANTIC_REASONING_VERSION, confidence=proposal.confidence,
        ) for quote in quotes],
    }
    resolved = original.model_copy(update=updates) if hasattr(original, "model_copy") else original.copy(update=updates)
    candidates = []
    for code in dict.fromkeys(proposal.candidate_gl_codes):
        account = catalog.get(str(code))
        if not account or not account.payable:
            continue
        candidates.append(GLCandidate(gl_code=str(code), gl_name=account.gl_name,
            source="ai_semantic_reasoning_candidate", source_id=SEMANTIC_REASONING_VERSION,
            base_score=min(0.75, proposal.confidence),
            positive_evidence=[{"quotes": quotes, "reasoning_summary": proposal.reasoning_summary}],
            rule_version=SEMANTIC_REASONING_VERSION))
    return resolved, candidates


def _estimated_cost(source: str, context: str) -> float:
    # Conservative reproducible estimate; actual provider billing remains authoritative.
    return round(max(0.001, (len(source) + len(context)) / 4 / 1_000_000 * 3.0 + 1200 / 1_000_000 * 15.0), 6)


def _candidate_only_payload(proposal: SemanticReasoningProposal | InvoiceSemanticProposal) -> dict[str, Any]:
    """Persist reusable candidate semantics without literal source evidence."""
    return {
        "line_family": proposal.line_family,
        "trade_family": proposal.trade_family,
        "work_mode": proposal.work_mode,
        "confidence": proposal.confidence,
        "candidate_gl_codes": list(proposal.candidate_gl_codes),
        "reasoning_summary": proposal.reasoning_summary,
    }


def _grounding_quote(concept, source: str) -> str:
    phrase = str(concept.matched_phrase or "").strip()
    if phrase and phrase.casefold() in source.casefold():
        start = source.casefold().index(phrase.casefold())
        return source[start:start + len(phrase)]
    raise ValueError("canonical_cache_source_grounding_missing")


def _proposal_from_canonical_cache(payload: dict[str, Any], concept,
                                   source: str) -> SemanticReasoningProposal:
    return SemanticReasoningProposal(
        **payload,
        evidence_quotes=[_grounding_quote(concept, source)],
    )


def _group_from_canonical_cache(payload: dict[str, Any], concepts,
                                request_lines: list[dict[str, Any]]) -> InvoiceSemanticProposalEnvelope:
    rows = payload.get("proposals") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) != len(request_lines):
        raise ValueError("canonical_cache_line_count_mismatch")
    proposals = []
    for cached, concept, request_line in zip(rows, concepts, request_lines, strict=True):
        if not isinstance(cached, dict):
            raise ValueError("canonical_cache_proposal_invalid")
        proposals.append(InvoiceSemanticProposal(
            **cached,
            line_item_id=request_line["line_item_id"],
            evidence_quotes=[_grounding_quote(concept, request_line["source_text"])],
        ))
    return InvoiceSemanticProposalEnvelope(proposals=proposals)


def _read_cache(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    except (OSError, ValueError):
        return None


def _write_cache(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


__all__ = ["InvoiceSemanticLineRequest", "SEMANTIC_REASONING_VERSION",
           "SemanticReasoningProposal", "SemanticReasoningResult",
           "enrich_invoice_semantics", "enrich_unknown_semantics"]
