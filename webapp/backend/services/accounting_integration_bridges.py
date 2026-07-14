"""Thin clean-checkout bridges from legacy rows/results into Phase 2 contracts."""
from __future__ import annotations

from typing import Any

from .accounting_contracts import GLCandidate, LineItemFacts, SemanticClassification
from .accounting_pipeline_v2 import capture_source_fields, decide_row, v2_enabled


class ServiceReasoningCandidateAdapter:
    def generate_candidates(self, line_facts: LineItemFacts, semantic_classification: SemanticClassification,
                            gl_catalog: dict[str, Any], vendor_context: dict | None = None,
                            invoice_context: dict | None = None) -> list[GLCandidate]:
        candidates: list[GLCandidate] = []
        for code, account in gl_catalog.items():
            if not getattr(account, "payable", False):
                continue
            trade_ok = not account.trade_families or semantic_classification.trade_family in account.trade_families
            mode_ok = not account.compatible_work_modes or semantic_classification.work_mode in account.compatible_work_modes
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
        context = document_context or {}
        for index, row in enumerate(rows, 1):
            meta = row.setdefault("_meta", {})
            document_id = str(context.get("document_id") or meta.get("source_file") or "normalized-row")
            line_id = str(meta.get("line_item_id") or row.get("Line Item Number") or index)
            capture_source_fields(row, document_id=document_id, line_item_id=line_id)
            source = meta.get("source_text") or {}
            meta["normalized_source_description"] = str(row.get("Line Item Description") or "").strip() or None
            meta["generated_line_description"] = row.get("Line Item Description")
            meta["generated_invoice_description"] = row.get("Invoice Description")
            decide_row(row, document_id=document_id, line_item_id=line_id,
                       extraction_route=str(context.get("extraction_route") or "row_normalizer_bridge"))
        return rows


class AIResultAccountingV2Adapter:
    def convert(self, invoice: dict[str, Any], document_context: dict[str, Any] | None = None,
                references: Any = None, gl_catalog: Any = None,
                policy_context: dict[str, Any] | None = None) -> dict[str, Any]:
        context = {**(document_context or {}), "extraction_route": "ai_result_bridge"}
        RowAccountingV2Adapter().enrich_rows(invoice.get("rows") or [], context, gl_catalog, policy_context)
        return invoice


__all__ = ["AIResultAccountingV2Adapter", "RowAccountingV2Adapter", "ServiceReasoningCandidateAdapter"]
