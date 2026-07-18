"""Thin clean-checkout bridges from legacy rows/results into Phase 2 contracts."""
from __future__ import annotations

from typing import Any

from .accounting_contracts import (
    AccountingDecision, GLCandidate, LineItemFacts, SemanticClassification,
)
from .accounting_pipeline_v2 import capture_source_fields, decide_row, v2_enabled


class ServiceReasoningCandidateAdapter:
    def generate_candidates(self, line_facts: LineItemFacts, semantic_classification: SemanticClassification,
                            gl_catalog: dict[str, Any], vendor_context: dict | None = None,
                            invoice_context: dict | None = None) -> list[GLCandidate]:
        candidates: list[GLCandidate] = []
        from .canonical_rules import load_rules
        from .semantic_classifier import detect_utility_service_type
        source_text = " ".join(value for value in (
            line_facts.raw_activity,
            line_facts.raw_description,
            line_facts.normalized_activity,
            line_facts.normalized_description,
        ) if value)
        utility_type = detect_utility_service_type(source_text, source_text)
        utility_defaults = (
            (load_rules().get("utility_processing") or {})
            .get("gl_mapping", {})
            .get("defaults", {})
        )
        utility_key = {
            "water_sewer": "water_sewer",
            "stormwater": "stormwater",
            "trash": "sanitation",
            "internet_fiber": "internet_fiber",
        }.get(utility_type or "")
        utility_code = str(utility_defaults.get(utility_key) or "") if utility_key else ""
        if utility_code in gl_catalog and getattr(gl_catalog[utility_code], "payable", False):
            candidates.append(GLCandidate(
                gl_code=utility_code,
                gl_name=gl_catalog[utility_code].gl_name,
                source="canonical_utility_service_candidate",
                source_id=f"utility_processing.gl_mapping.defaults.{utility_key}",
                base_score=0.95,
                positive_evidence=[{
                    "service_type": utility_type,
                    "source_text": source_text,
                }],
                compatibility_results=[{"compatible": True}],
                rule_version="canonical-utility-candidate/1.0",
            ))
        semantic_known = any((
            semantic_classification.trade_family != "unknown",
            semantic_classification.line_family != "unknown",
            semantic_classification.work_mode != "unknown",
        ))
        if not semantic_known:
            return candidates
        for code, account in gl_catalog.items():
            if not getattr(account, "payable", False):
                continue
            metadata_known = bool(
                account.gl_family != "unknown"
                or account.trade_families
                or account.compatible_work_modes
            )
            if not metadata_known:
                continue
            trade_ok = (
                semantic_classification.trade_family != "unknown"
                and semantic_classification.trade_family in account.trade_families
            ) or (
                semantic_classification.line_family != "unknown"
                and account.gl_family == semantic_classification.line_family
            )
            mode_ok = (
                not account.compatible_work_modes
                or (
                    semantic_classification.work_mode != "unknown"
                    and semantic_classification.work_mode in account.compatible_work_modes
                )
            )
            if trade_ok and mode_ok:
                candidates.append(GLCandidate(gl_code=code, gl_name=account.gl_name,
                    source="service_reasoning_candidate", source_id="phase2.5-service-adapter",
                    base_score=0.62, positive_evidence=[{"trade_family": semantic_classification.trade_family,
                    "work_mode": semantic_classification.work_mode}], compatibility_results=[{"compatible": True}],
                    rule_version="service-candidate-adapter/1.0"))
        return candidates


class RowAccountingV2Adapter:
    def enrich_rows(self, rows: list[dict[str, Any]], document_context: dict[str, Any] | None = None,
                    gl_catalog: Any = None, policy_context: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if not v2_enabled():
            return rows
        from . import accounting_artifact_cache

        context = document_context or {}
        row_identity: dict[int, tuple[str, str]] = {}
        # Source contracts are not accounting artifacts.  Rebuild them on
        # every adapter entry before considering a cached decision so a warm
        # accounting hit can never remove DocumentFacts or source_text.
        for index, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                continue
            meta = row.setdefault("_meta", {})
            document_id = str(
                context.get("document_id")
                or meta.get("source_file")
                or "normalized-row"
            )
            line_id = str(
                meta.get("line_item_id")
                or row.get("Line Item Number")
                or index
            )
            if meta.get("source_extraction_failed"):
                # A transport/schema failure is not an accounting fact. Keep
                # the retained source placeholder blank and blocking; neither
                # semantic reasoning nor a cached decision may manufacture GL.
                row_identity[id(row)] = (document_id, line_id)
                continue
            capture_source_fields(
                row,
                document_id=document_id,
                line_item_id=line_id,
            )
            meta["normalized_source_description"] = (
                str(row.get("Line Item Description") or "").strip() or None
            )
            meta["generated_line_description"] = row.get("Line Item Description")
            meta["generated_invoice_description"] = row.get("Invoice Description")
            row_identity[id(row)] = (document_id, line_id)

        dependencies = accounting_artifact_cache.dependency_versions()
        for row in rows:
            if (
                isinstance(row, dict)
                and not (row.get("_meta") or {}).get("source_extraction_failed")
                and not accounting_artifact_cache.is_reusable(row, dependencies)
            ):
                accounting_artifact_cache.hydrate(row, dependencies)
        pending_rows = [
            row for row in rows
            if isinstance(row, dict)
            and not (row.get("_meta") or {}).get("source_extraction_failed")
            and not accounting_artifact_cache.is_reusable(row, dependencies)
        ]
        if not pending_rows:
            return rows
        request_keys = {
            id(row): accounting_artifact_cache.request_fingerprint(row, dependencies)
            for row in pending_rows
        }
        semantic_context = " | ".join(dict.fromkeys(
            str(value).strip() for row in rows if isinstance(row, dict)
            for value in (row.get("Invoice Description"),
                          ((row.get("_meta") or {}).get("source_line_description") if isinstance(row.get("_meta"), dict) else None),
                          row.get("Line Item Description"))
            if value and str(value).strip()
        ))
        first_pass: list[tuple[dict[str, Any], str, str, AccountingDecision]] = []
        for index, row in enumerate(pending_rows, 1):
            document_id, line_id = row_identity.get(
                id(row),
                (str(context.get("document_id") or "normalized-row"), str(index)),
            )
            decision = decide_row(
                row, document_id=document_id, line_item_id=line_id,
                extraction_route=str(context.get("extraction_route") or "row_normalizer_bridge"),
                document_context=semantic_context,
                allow_ai_semantic_reasoning=False,
            )
            first_pass.append((row, document_id, line_id, decision))

        # One grouped request per invoice, and only when the central engine
        # could not produce a safe GL.  A valid deterministic selection costs
        # zero even when optional semantic taxonomy fields remain unknown.
        from .semantic_reasoning_gateway import (
            InvoiceSemanticLineRequest, enrich_invoice_semantics,
        )
        unresolved: list[InvoiceSemanticLineRequest] = []
        row_by_line_id: dict[str, tuple[dict[str, Any], str]] = {}
        for row, document_id, line_id, decision in first_pass:
            if decision.selected_gl_code and not decision.review_blocking:
                continue
            meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
            facts_payload = meta.get("document_facts") if isinstance(meta.get("document_facts"), dict) else {}
            semantic_payload = meta.get("semantic_classification") if isinstance(meta.get("semantic_classification"), dict) else {}
            facts_lines = facts_payload.get("line_items") if isinstance(facts_payload, dict) else []
            if not facts_lines or not semantic_payload:
                continue
            facts = LineItemFacts(**facts_lines[0])
            semantics = SemanticClassification(**semantic_payload)
            unresolved.append(InvoiceSemanticLineRequest(
                facts=facts,
                semantics=semantics,
                candidate_gl_codes=_candidate_codes_for_row(row, semantics, decision),
            ))
            row_by_line_id[line_id] = (row, document_id)

        grouped = (
            enrich_invoice_semantics(
                lines=unresolved,
                document_id=str(context.get("document_id") or "normalized-row"),
                document_context=semantic_context,
            )
            if unresolved else {}
        )
        for line_id, reasoning in grouped.items():
            if reasoning.trace.get("route") != "ai_contextual_semantics_grouped":
                row_and_document = row_by_line_id.get(line_id)
                if row_and_document:
                    row_and_document[0].setdefault("_meta", {})[
                        "semantic_reasoning_trace"
                    ] = reasoning.trace
                continue
            row_and_document = row_by_line_id.get(line_id)
            if not row_and_document:
                continue
            row, document_id = row_and_document
            decide_row(
                row, document_id=document_id, line_item_id=line_id,
                extraction_route=str(context.get("extraction_route") or "row_normalizer_bridge"),
                document_context=semantic_context,
                semantic_reasoning_result=reasoning,
                allow_ai_semantic_reasoning=False,
            )
        for row, _, _, _ in first_pass:
            accounting_artifact_cache.mark(
                row,
                dependencies,
                request_key=request_keys.get(id(row), ""),
            )
        return rows


def _candidate_codes_for_row(row: dict[str, Any], semantics: SemanticClassification,
                             decision: AccountingDecision) -> list[str]:
    """Build a small relevant shortlist without selecting a final GL."""
    from .gl_catalog import load_gl_catalog
    _, catalog = load_gl_catalog()
    codes: list[str] = []
    for value in [row.get("GL Account"),
                  *(candidate.gl_code for candidate in decision.candidates_ranked)]:
        code = str(value or "").strip()
        if code in catalog and catalog[code].payable and code not in codes:
            codes.append(code)
    source = " ".join(str(value or "") for value in (
        ((row.get("_meta") or {}).get("source_line_description")
         if isinstance(row.get("_meta"), dict) else ""),
        row.get("Line Item Description"), row.get("Invoice Description"),
    )).casefold()
    scored: list[tuple[int, str]] = []
    for code, account in catalog.items():
        if not account.payable or code in codes:
            continue
        score = 0
        if semantics.trade_family != "unknown" and semantics.trade_family in account.trade_families:
            score += 4
        if semantics.line_family != "unknown" and semantics.line_family == account.gl_family:
            score += 3
        if semantics.work_mode != "unknown" and semantics.work_mode in account.compatible_work_modes:
            score += 2
        if any(token and token.casefold() in source for token in account.description_tokens):
            score += 2
        if score:
            scored.append((score, code))
    codes.extend(code for _, code in sorted(scored, key=lambda item: (-item[0], item[1])))
    return codes[:12]


class AIResultAccountingV2Adapter:
    def convert(self, invoice: dict[str, Any], document_context: dict[str, Any] | None = None,
                references: Any = None, gl_catalog: Any = None,
                policy_context: dict[str, Any] | None = None) -> dict[str, Any]:
        context = {**(document_context or {}), "extraction_route": "ai_result_bridge"}
        RowAccountingV2Adapter().enrich_rows(invoice.get("rows") or [], context, gl_catalog, policy_context)
        _reconcile_resolved_pre_engine_gl_issues(invoice)
        return invoice


_PRE_ENGINE_GL_CODES = {"gl_mapping_required", "required_gl_account"}


def _is_pre_engine_gl_reason(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return text.startswith("gl mapping remained unresolved") or text.startswith(
        "gl account is required by canonical rules"
    )


def _reconcile_resolved_pre_engine_gl_issues(invoice: dict[str, Any]) -> None:
    """Close stale pre-engine GL warnings while retaining resolution evidence.

    Validation runs before the Phase 2 adapter, so a candidate-only extraction
    may initially report missing GL.  Once every row has an authoritative,
    payable AccountingDecision, those warnings are no longer active blockers.
    They remain in a resolved audit record rather than being silently erased.
    """

    rows = [row for row in (invoice.get("rows") or []) if isinstance(row, dict)]
    if not rows:
        return
    decisions: list[dict[str, Any]] = []
    for row in rows:
        meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
        decision = meta.get("accounting_decision") if isinstance(meta, dict) else None
        if not isinstance(decision, dict):
            return
        selected = str(decision.get("selected_gl_code") or "").strip()
        if not selected or selected != str(row.get("GL Account") or "").strip():
            return
        decisions.append(decision)

    resolution_evidence = [{
        "line_item_id": decision.get("line_item_id"),
        "decision_id": decision.get("decision_id"),
        "selected_gl_code": decision.get("selected_gl_code"),
        "decision_source": decision.get("decision_source"),
    } for decision in decisions]
    invoice["manual_review_codes"] = [
        code for code in (invoice.get("manual_review_codes") or [])
        if str(code) not in _PRE_ENGINE_GL_CODES
    ]
    invoice["manual_review_reasons"] = [
        reason for reason in (invoice.get("manual_review_reasons") or [])
        if not _is_pre_engine_gl_reason(reason)
    ]
    summary = invoice.get("validation_summary")
    if isinstance(summary, dict):
        summary["blocking_required_fields"] = [
            field for field in (summary.get("blocking_required_fields") or [])
            if str(field) != "GL Account"
        ]
        summary["pre_engine_gl_issues_resolved"] = True

    for row in rows:
        meta = row.setdefault("_meta", {})
        previous_codes = list(meta.get("ai_validation_flags") or [])
        previous_reasons = list(meta.get("manual_review_reasons") or [])
        resolved_codes = [
            code for code in previous_codes if str(code) in _PRE_ENGINE_GL_CODES
        ]
        resolved_reasons = [
            reason for reason in previous_reasons if _is_pre_engine_gl_reason(reason)
        ]
        meta["ai_validation_flags"] = [
            code for code in previous_codes if str(code) not in _PRE_ENGINE_GL_CODES
        ]
        meta["manual_review_reasons"] = [
            reason for reason in previous_reasons if not _is_pre_engine_gl_reason(reason)
        ]
        if resolved_codes or resolved_reasons:
            meta["resolved_pre_engine_issues"] = [{
                "codes": resolved_codes,
                "reasons": resolved_reasons,
                "resolution": "authoritative_accounting_decision_selected_payable_gl",
                "evidence": resolution_evidence,
            }]


__all__ = ["AIResultAccountingV2Adapter", "RowAccountingV2Adapter", "ServiceReasoningCandidateAdapter"]
