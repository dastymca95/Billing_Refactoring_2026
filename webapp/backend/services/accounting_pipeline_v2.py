"""Compatibility adapters from legacy processor rows into Phase 2 contracts."""

from __future__ import annotations

import os
import hashlib
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from .accounting_contracts import DocumentFacts, EvidenceReference, GLCandidate, LineItemFacts, model_dict
from .accounting_decision_engine import AccountingDecisionEngine
from .gl_catalog import load_gl_catalog
from .model_registry import CapabilityDiscovery, default_registry
from .reasoning_router import RoutingSignals, RoutingStateMachine
from .semantic_classifier import classify_line
from .economic_responsibility import FilenameFolderContextParser


def v2_enabled() -> bool:
    raw = os.environ.get("ACCOUNTING_DECISION_ENGINE_V2")
    if raw is None or not raw.strip():
        return True
    value = raw.strip().lower()
    if value in {"1", "true", "on", "yes"}:
        return True
    if value not in {"0", "false", "off", "no"}:
        raise RuntimeError(
            "ACCOUNTING_DECISION_ENGINE_V2 must be an explicit boolean value; "
            f"received {raw!r}."
        )
    rollback = os.environ.get("ACCOUNTING_DECISION_ENGINE_V2_ALLOW_LEGACY_ROLLBACK", "").strip().lower()
    if rollback not in {"1", "true", "on", "yes"}:
        raise RuntimeError(
            "Disabling ACCOUNTING_DECISION_ENGINE_V2 requires the explicit "
            "ACCOUNTING_DECISION_ENGINE_V2_ALLOW_LEGACY_ROLLBACK=1 authorization."
        )
    return False


def capture_source_fields(row: dict[str, Any], *, document_id: str, line_item_id: str) -> None:
    meta = row.setdefault("_meta", {})
    if not isinstance(meta, dict):
        row["_meta"] = meta = {}
    meta.setdefault("source_text", {})
    source = meta["source_text"]
    if isinstance(source, dict):
        source.setdefault("raw_activity", meta.get("raw_activity") or meta.get("source_activity") or meta.get("ai_line_activity"))
        source.setdefault("raw_description", meta.get("source_line_description") or meta.get("ai_source_line_description") or row.get("Line Item Description"))
        source.setdefault("raw_invoice_description", meta.get("source_invoice_description") or row.get("Invoice Description"))
        source.setdefault("raw_section_header", meta.get("ai_line_section_header") or meta.get("source_section_header"))
        source.setdefault("document_id", document_id)
        source.setdefault("line_item_id", line_item_id)
    source_file = _text(meta.get("source_file"))
    if source_file and "source_metadata_candidates" not in meta:
        parsed = FilenameFolderContextParser().parse(document_id, source_file, [])
        # Raw filename already has its dedicated source_file field.  The facts
        # contract stores only parsed, explicitly non-authoritative candidates
        # plus a linkage hash, so interpretations can change without mutating
        # source evidence.
        meta["source_metadata_candidates"] = {
            "schema_version": parsed.schema_version,
            "source_filename_sha256": hashlib.sha256(source_file.encode("utf-8")).hexdigest(),
            "candidates": [model_dict(candidate) for candidate in parsed.candidates],
            "warnings": list(parsed.warnings),
            "authoritative": False,
        }


def decide_row(row: dict[str, Any], *, document_id: str, line_item_id: str, extraction_route: str,
               document_context: str | None = None,
               semantic_reasoning_result: Any | None = None,
               allow_ai_semantic_reasoning: bool = False):
    meta = row.setdefault("_meta", {})
    if not isinstance(meta, dict):
        row["_meta"] = meta = {}
    source = meta.get("source_text") if isinstance(meta.get("source_text"), dict) else {}
    raw_description = _text(source.get("raw_description"))
    raw_activity = _text(source.get("raw_activity"))
    normalized_description = _text(meta.get("normalized_source_description") or raw_description)
    generated_description = _text(row.get("Line Item Description"))
    raw_section_header = _text(source.get("raw_section_header"))
    evidence = [EvidenceReference(document_id=document_id, page=_int(meta.get("source_page")),
        text=raw_description or raw_activity or None, normalized_text=normalized_description or None,
        source_type="line_item", extraction_method=extraction_route)]
    if raw_section_header:
        evidence.append(EvidenceReference(
            document_id=document_id, page=_int(meta.get("source_page")),
            text=raw_section_header, normalized_text=raw_section_header,
            source_type="line_section_header", extraction_method=extraction_route,
        ))
    facts_line = LineItemFacts(line_item_id=line_item_id, raw_activity=raw_activity or None,
        raw_description=raw_description or None, normalized_activity=raw_activity or None,
        normalized_description=normalized_description or None, generated_description=generated_description or None,
        quantity=_decimal(row.get("Quantity")), unit_price=_decimal(row.get("Unit Price")),
        amount=_decimal(row.get("Amount")), tax=_decimal(row.get("Tax")),
        detected_location=_text(row.get("Location")) or None, evidence=evidence)
    metadata = meta.get("source_metadata_candidates") if isinstance(meta.get("source_metadata_candidates"), dict) else {}
    metadata_evidence = [EvidenceReference(
        document_id=document_id,
        normalized_text=_text(candidate.get("normalized_value")) or None,
        source_type=f"filename_context:{_text(candidate.get('candidate_type')) or 'unknown'}",
        extraction_method="filename_folder_parser_non_authoritative",
        confidence=float(candidate.get("confidence") or 0),
    ) for candidate in metadata.get("candidates") or [] if isinstance(candidate, dict)]
    facts = DocumentFacts(document_id=document_id, invoice_id=_text(row.get("Invoice Number")) or line_item_id,
        vendor_candidate=_text(row.get("Vendor")) or None, invoice_number=_text(row.get("Invoice Number")) or None,
        invoice_date=_date(row.get("Invoice Date")), due_date=_date(row.get("Due Date")),
        property_candidate=_text(row.get("Property Abbreviation")) or None,
        total_amount=_decimal(row.get("Amount")), line_items=[facts_line], extraction_route=extraction_route,
        extraction_model=_text(meta.get("extraction_model")) or None, evidence=metadata_evidence)
    semantics = classify_line(facts_line, document_id=document_id,
        document_family=_document_family(row),
        document_context=document_context if document_context is not None else _text(source.get("raw_invoice_description")))
    from .semantic_reasoning_gateway import SemanticReasoningResult, enrich_unknown_semantics
    from .tenant_accounting_policies import tenant_id_for_row
    tenant_id = tenant_id_for_row(row)
    semantic_reasoning = semantic_reasoning_result or SemanticReasoningResult(
        semantics=semantics,
        trace={"route": "deterministic", "called": False,
               "reason": "external_reasoning_not_authorized"},
    )
    if semantic_reasoning_result is None and allow_ai_semantic_reasoning:
        semantic_reasoning = enrich_unknown_semantics(
            facts=facts_line, semantics=semantics, document_id=document_id,
            document_context=(document_context if document_context is not None
                              else _text(source.get("raw_invoice_description"))),
            tenant_id=tenant_id,
        )
    semantics = semantic_reasoning.semantics
    candidates = _adapt_candidates(row, normalized_description or raw_description or raw_activity)
    candidates.extend(semantic_reasoning.candidates)
    _, catalog = load_gl_catalog()
    from .accounting_integration_bridges import ServiceReasoningCandidateAdapter
    candidates.extend(ServiceReasoningCandidateAdapter().generate_candidates(
        facts_line, semantics, catalog,
        vendor_context={"vendor_candidate": facts.vendor_candidate},
        invoice_context={"invoice_id": facts.invoice_id},
    ))
    from .operator_accounting_rules import apply_active_rules
    from .tenant_accounting_policies import apply_active_policies
    # Human-approved learning examples are tenant-private candidate evidence.
    # They never write the row or bypass semantic/catalog compatibility; the
    # central engine remains the only selected-GL authority.
    from .canonical_semantics import resolve_canonical_concept
    canonical = resolve_canonical_concept(
        normalized_description or raw_description or raw_activity,
        line_family=semantics.line_family,
        trade_family=semantics.trade_family,
        work_mode=semantics.work_mode,
    )
    from .accounting_knowledge_core import AccountingKnowledgeCore
    knowledge = AccountingKnowledgeCore().line_context(
        tenant_id=tenant_id, row=row, semantics_payload=model_dict(semantics),
        canonical_concept=canonical.concept_id,
    )
    _extend_knowledge_candidates(candidates, knowledge, catalog)
    meta["accounting_knowledge_context"] = knowledge.model_dump(mode="json")
    # Compatibility adapters remain during the transition; both are derived
    # from the shared typed context and neither contains benchmark examples.
    meta["human_learning_candidate_evidence"] = [
        item.model_dump(mode="json") for item in knowledge.similar_approved_learning_examples
    ]
    meta["context_intelligence_evidence"] = [
        item.model_dump(mode="json") for item in knowledge.vendor_property_joint_priors
        or knowledge.historical_vendor_priors
    ]
    rule_application = apply_active_rules(
        row=row,
        semantics=semantics,
        catalog=catalog,
        candidates=candidates,
        tenant_id=tenant_id,
    )
    candidates = rule_application.candidates
    tenant_policy_application = apply_active_policies(
        tenant_id=tenant_id,
        row=row,
        semantics=semantics,
        catalog=catalog,
        candidates=candidates,
    )
    candidates = tenant_policy_application.candidates
    decision = AccountingDecisionEngine().decide(facts, semantics, catalog, candidates, {})
    # A deterministic classification can be syntactically complete yet still
    # produce no safe payable candidate (for example when a pricing word masks
    # the actual expense subject). Escalate that outcome once for grounded
    # contextual reclassification. The model still only proposes semantics and
    # candidates; AccountingDecisionEngine remains the sole GL selector.
    if (allow_ai_semantic_reasoning
            and decision.selected_gl_code is None
            and semantic_reasoning.trace.get("route") == "deterministic"):
        fallback_reasoning = enrich_unknown_semantics(
            facts=facts_line, semantics=semantics, document_id=document_id,
            document_context=(document_context if document_context is not None
                              else _text(source.get("raw_invoice_description"))),
            force_no_safe_decision=True,
            tenant_id=tenant_id,
        )
        if fallback_reasoning.trace.get("route") == "ai_contextual_semantics":
            semantics = fallback_reasoning.semantics
            semantic_reasoning = fallback_reasoning
            candidates = _adapt_candidates(row, normalized_description or raw_description or raw_activity)
            candidates.extend(fallback_reasoning.candidates)
            candidates.extend(ServiceReasoningCandidateAdapter().generate_candidates(
                facts_line, semantics, catalog,
                vendor_context={"vendor_candidate": facts.vendor_candidate},
                invoice_context={"invoice_id": facts.invoice_id},
            ))
            canonical = resolve_canonical_concept(
                normalized_description or raw_description or raw_activity,
                line_family=semantics.line_family,
                trade_family=semantics.trade_family,
                work_mode=semantics.work_mode,
            )
            knowledge = AccountingKnowledgeCore().line_context(
                tenant_id=tenant_id, row=row, semantics_payload=model_dict(semantics),
                canonical_concept=canonical.concept_id,
            )
            _extend_knowledge_candidates(candidates, knowledge, catalog)
            meta["accounting_knowledge_context"] = knowledge.model_dump(mode="json")
            rule_application = apply_active_rules(
                row=row,
                semantics=semantics,
                catalog=catalog,
                candidates=candidates,
                tenant_id=tenant_id,
            )
            candidates = rule_application.candidates
            tenant_policy_application = apply_active_policies(
                tenant_id=tenant_id,
                row=row,
                semantics=semantics,
                catalog=catalog,
                candidates=candidates,
            )
            candidates = tenant_policy_application.candidates
            decision = AccountingDecisionEngine().decide(
                facts, semantics, catalog, candidates, {},
            )
    legacy_gl = _text(row.get("GL Account"))
    meta["document_facts"] = model_dict(facts)
    meta["semantic_classification"] = model_dict(semantics)
    meta["semantic_reasoning_trace"] = semantic_reasoning.trace
    meta["operator_accounting_rule_trace"] = rule_application.trace
    meta["tenant_accounting_policy_trace"] = tenant_policy_application.trace
    meta["accounting_decision"] = model_dict(decision)
    meta["ai_gl_accounting_reasoning"] = model_dict(decision)  # temporary UI adapter
    phase3_route = RoutingStateMachine(
        CapabilityDiscovery(default_registry()), text_available=False, vision_available=False
    ).decide_accounting_shadow(RoutingSignals(
        deterministic_parser_succeeded=True,
        facts_complete=True,
        accounting_ambiguity=bool(decision.review_required),
    ))
    meta["phase3_accounting_route"] = {
        "route": phase3_route.route.value,
        "reason_code": phase3_route.reason_code,
        "model_id": phase3_route.model_id,
        "shadow_only": phase3_route.shadow_only,
    }
    meta["gl_shadow_comparison"] = {"legacy_selected_gl": legacy_gl or None,
        "v2_selected_gl": decision.selected_gl_code, "same": legacy_gl == (decision.selected_gl_code or ""),
        "difference_reason": None if legacy_gl == (decision.selected_gl_code or "") else decision.why_selected}
    if v2_enabled():
        row["GL Account"] = decision.selected_gl_code or ""
        reasons = list(meta.get("manual_review_reasons") or [])
        reasons = [reason for reason in reasons if reason not in {
            "gl_account_missing", "invalid_gl_account", "tenant_policy_conflict",
            "tenant_policy_amount_mismatch",
        }]
        if tenant_policy_application.trace.get("policy_conflict_blocking"):
            reasons.append("tenant_policy_conflict")
        if tenant_policy_application.trace.get("policy_review_required"):
            reasons.append("tenant_policy_amount_mismatch")
        if decision.review_blocking:
            reasons.append("accounting_decision_blocking")
        meta["manual_review_reasons"] = sorted(set(reasons))
    return decision


def _extend_knowledge_candidates(
    candidates: list[GLCandidate], knowledge: Any, catalog: dict[str, Any],
) -> None:
    """Adapt typed Knowledge Core evidence into candidate-only engine input."""
    existing = {(item.gl_code, item.source, item.source_id) for item in candidates}
    for learned in knowledge.similar_approved_learning_examples:
        code = _text(learned.gl_code)
        key = (code, "human_approved_learning_example", learned.revision_id)
        if key in existing or not code or code not in catalog or not catalog[code].payable:
            continue
        candidates.append(GLCandidate(
            gl_code=code, gl_name=catalog[code].gl_name,
            source="human_approved_learning_example", source_id=learned.revision_id,
            base_score=0.78,
            positive_evidence=[{
                **learned.model_dump(mode="json"),
                "selection_authority": False,
            }],
            rule_version="accounting-knowledge-core/1.0",
        ))
        existing.add(key)
    prior_groups = (
        knowledge.vendor_property_joint_priors,
        knowledge.historical_vendor_priors,
        knowledge.historical_property_priors,
    )
    seen_history: set[tuple[str, str]] = set()
    for group in prior_groups:
        for historical in group:
            code = _text(historical.gl_code)
            history_key = (code, historical.snapshot_id)
            if history_key in seen_history or not code or code not in catalog or not catalog[code].payable:
                continue
            seen_history.add(history_key)
            candidates.append(GLCandidate(
                gl_code=code, gl_name=catalog[code].gl_name,
                source="context_intelligence_history",
                source_id=historical.snapshot_id,
                base_score=min(.70, .45 + .25 * float(historical.share)),
                positive_evidence=[{
                    **historical.model_dump(mode="json"),
                    "evidence_kind": "tenant_historical_frequency",
                    "selection_authority": False,
                }],
                rule_version="accounting-knowledge-core/1.0",
            ))


def _adapt_candidates(row: dict[str, Any], evidence_text: str) -> list[GLCandidate]:
    _, catalog = load_gl_catalog()
    meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
    out: list[GLCandidate] = []
    seen: set[tuple[str, str]] = set()
    legacy_source = "learned_correction" if meta.get("learned_corrections_applied") else str(
        meta.get("gl_suggestion_source") or meta.get("accounting_source") or "deterministic_parser"
    )
    sources = [
        (row.get("GL Account"), legacy_source, "legacy_row"),
        (meta.get("approved_operator_gl_candidate"),
         "manual_approved_operator_correction", "approved_operator_correction"),
        (meta.get("ai_source_gl_candidate"), "ai_candidate", "ai_source_gl_candidate"),
        (meta.get("vendor_default_gl"), "vendor_default", "vendor_profile"),
        (meta.get("historical_gl"), "historical_mapping", "historical_mapping"),
        (meta.get("learned_gl"), "learned_correction", "approved_correction"),
        (meta.get("canonical_gl"), "canonical_rule", "canonical_rule"),
    ]
    for item in meta.get("gl_candidate_inputs") or []:
        if isinstance(item, dict):
            sources.append((item.get("gl_code"), str(item.get("source") or "legacy_adapter"), item.get("source_id")))
    for raw, source, source_id in sources:
        code = _text(raw)
        if not code or code not in catalog or (code, source) in seen:
            continue
        seen.add((code, source))
        positive_evidence = (
            [dict(meta.get("approved_operator_gl_evidence") or {})]
            if source == "manual_approved_operator_correction"
            else [{"source": source_id, "value": code}]
        )
        out.append(GLCandidate(gl_code=code, gl_name=catalog[code].gl_name, source=source,
            source_id=source_id, base_score=(
                1.0 if source == "manual_approved_operator_correction"
                else 0.95 if source in {"deterministic_parser", "canonical_rule"}
                else 0.75
            ),
            positive_evidence=positive_evidence, rule_version="legacy-adapter/1.0"))
    # Catalog candidates are proposals only. The engine still performs all selection.
    normalized_evidence = evidence_text.lower()
    for code, account in catalog.items():
        if account.trade_families and any(token in normalized_evidence for token in account.description_tokens):
            out.append(GLCandidate(gl_code=code, gl_name=account.gl_name, source="catalog_text_match",
                source_id="chart_metadata", base_score=0.55, rule_version="gl-catalog/1.0"))
    return out


def _document_family(row: dict[str, Any]) -> str:
    bill_type = _text(row.get("Bill or Credit")).lower()
    return "credit" if "credit" in bill_type else "invoice"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except (InvalidOperation, ValueError):
        return None


def _date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


__all__ = ["capture_source_fields", "decide_row", "v2_enabled"]
