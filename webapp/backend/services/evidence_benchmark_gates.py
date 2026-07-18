"""Offline gates for deterministic replay and human evidence benchmarks."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .evidence_benchmark import (
    AdjudicationState,
    EvidenceBackedGoldenContract,
    ExportSafetyExpectation,
    file_sha256,
)


GATE_VERSION = "evidence-benchmark-gates/1.0"


def evaluate(
    *,
    contract: EvidenceBackedGoldenContract,
    replay: dict[str, Any],
    replay_repeat: dict[str, Any],
    replay_metrics: dict[str, Any],
    benchmark_root: Path,
    verifier_results: dict[str, Any],
    source_root: Path | None = None,
) -> dict[str, Any]:
    benchmark_root = benchmark_root.resolve()
    evidence_losses = []
    field_total = 0
    adjudicated_field_total = 0
    payable_rows = 0
    excluded_rows = 0
    concept_resolved = 0

    def inspect_field(invoice_id: str, row_id: str, field) -> None:
        nonlocal field_total, adjudicated_field_total
        field_total += 1
        if field.state is AdjudicationState.ADJUDICATED:
            adjudicated_field_total += 1
        if not field.evidence:
            evidence_losses.append({"invoice_id": invoice_id, "row_id": row_id,
                                    "field": field.field_name, "reason": "evidence_missing"})
            return
        for evidence in field.evidence:
            crop = (benchmark_root / evidence.crop_ref).resolve()
            try:
                crop.relative_to(benchmark_root)
            except ValueError:
                evidence_losses.append({"invoice_id": invoice_id, "row_id": row_id,
                                        "field": field.field_name, "reason": "crop_outside_root"})
                continue
            if not crop.is_file():
                evidence_losses.append({"invoice_id": invoice_id, "row_id": row_id,
                                        "field": field.field_name, "reason": "crop_missing"})
            elif file_sha256(crop) != evidence.crop_sha256:
                evidence_losses.append({"invoice_id": invoice_id, "row_id": row_id,
                                        "field": field.field_name, "reason": "crop_hash_mismatch"})

    for invoice in contract.invoices:
        if source_root is not None:
            source = (source_root.resolve() / invoice.source_file_name).resolve()
            try:
                source.relative_to(source_root.resolve())
            except ValueError:
                evidence_losses.append({"invoice_id": invoice.invoice_id, "row_id": "source",
                                        "field": "source_document", "reason": "source_outside_root"})
            else:
                if not source.is_file():
                    evidence_losses.append({"invoice_id": invoice.invoice_id, "row_id": "source",
                                            "field": "source_document", "reason": "source_missing"})
                elif file_sha256(source) != invoice.source_document_sha256:
                    evidence_losses.append({"invoice_id": invoice.invoice_id, "row_id": "source",
                                            "field": "source_document", "reason": "source_hash_mismatch"})
        for name, field in invoice.header_fields.items():
            inspect_field(invoice.invoice_id, "header", field)
        for row in invoice.rows + invoice.excluded_rows:
            if row in invoice.rows:
                payable_rows += 1
            else:
                excluded_rows += 1
            for field in (row.row_identity, row.paid_crossed_out_status,
                          row.line_item_concept, row.amount):
                inspect_field(invoice.invoice_id, row.row_id, field)
            if row.canonical_semantic_concept:
                concept_resolved += 1

    replay_invoices = {str(item.get("invoice_number")): item
                       for item in replay.get("all_invoices") or []}
    blocked_invoices = {
        invoice_id for invoice_id, readiness in (replay_metrics.get("readiness") or {}).items()
        if readiness.get("status") == "blocked" and not readiness.get("export_allowed")
    }
    unresolved_concepts = [
        row.row_id for invoice in contract.invoices for row in invoice.rows
        if not row.canonical_semantic_concept
        and invoice.invoice_id not in blocked_invoices
    ]
    unauthorized_gl = []
    legacy_review_codes = []
    typed_review_mismatches = []
    for invoice in contract.invoices:
        runtime = replay_invoices.get(invoice.invoice_id) or {}
        runtime_codes = {str(code) for code in runtime.get("manual_review_codes") or []}
        legacy_review_codes.extend(
            {"invoice_id": invoice.invoice_id, "code": code}
            for code in runtime_codes if code.startswith("ai_warning_")
        )
        for evidence in runtime.get("typed_review_evidence") or []:
            category = str(evidence.get("category") or "") if isinstance(evidence, dict) else ""
            if category and category not in runtime_codes:
                typed_review_mismatches.append({"invoice_id": invoice.invoice_id,
                                                "category": category})
        runtime_rows = list(runtime.get("rows") or [])
        for index, expected in enumerate(invoice.rows):
            if index >= len(runtime_rows):
                unauthorized_gl.append({"row_id": expected.row_id, "reason": "runtime_row_missing"})
                continue
            actual_gl = str(runtime_rows[index].get("GL Account") or "").strip()
            accepted = set(expected.acceptable_gl_set)
            if expected.expected_gl:
                accepted.add(expected.expected_gl)
            if accepted and actual_gl not in accepted and invoice.invoice_id not in blocked_invoices:
                unauthorized_gl.append({"row_id": expected.row_id, "actual_gl": actual_gl,
                                        "accepted": sorted(accepted)})

    all_human_gold = (
        contract.state is AdjudicationState.ADJUDICATED
        and adjudicated_field_total == field_total
    )
    extraction_gate_status = "pass" if all_human_gold else "blocked_pending_human_adjudication"
    return {
        "schema_version": GATE_VERSION,
        "batch_id": contract.batch_id,
        "deterministic_replay_gate": {
            "status": "pass" if replay == replay_repeat else "fail",
            "exact_downstream_parity": replay == replay_repeat,
            "invoice_count": len(replay_invoices),
            "row_count": sum(len(item.get("rows") or []) for item in replay_invoices.values()),
            "external_provider_calls": replay_metrics.get("provider_calls_executed"),
            "external_network_attempts": replay_metrics.get("external_provider_network_attempts"),
        },
        "independent_cold_extraction_gate": {
            "status": extraction_gate_status,
            "ground_truth_source": "human_adjudicated_evidence_only",
            "adjudicated_field_count": adjudicated_field_total,
            "field_count": field_total,
            "verifier_crop_count": verifier_results.get("successful_crop_count", 0),
            "verifier_is_ground_truth": False,
        },
        "safety": {
            "source_evidence_loss_count": len(evidence_losses),
            "source_evidence_losses": evidence_losses,
            "false_safe_export_count": replay_metrics.get("false_safe_export_count", 0),
            "blocked_invoice_count": len(blocked_invoices),
            "unresolved_concepts_without_block_count": len(unresolved_concepts),
            "unresolved_concepts_without_block": unresolved_concepts,
            "unauthorized_gl_count": len(unauthorized_gl),
            "unauthorized_gl": unauthorized_gl,
            "legacy_free_text_review_code_count": len(legacy_review_codes),
            "legacy_free_text_review_codes": legacy_review_codes,
            "typed_review_code_mismatch_count": len(typed_review_mismatches),
            "typed_review_code_mismatches": typed_review_mismatches,
        },
        "coverage": {
            "invoice_count": len(contract.invoices),
            "payable_row_count": payable_rows,
            "excluded_paid_row_count": excluded_rows,
            "canonical_concept_resolved_count": concept_resolved,
            "canonical_concept_pending_count": payable_rows + excluded_rows - concept_resolved,
        },
    }


__all__ = ["GATE_VERSION", "evaluate"]
