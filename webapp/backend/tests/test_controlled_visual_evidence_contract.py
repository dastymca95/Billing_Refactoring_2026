from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from webapp.backend import settings
from webapp.backend.services import (
    ai_invoice_processor,
    ai_provider,
    ai_runtime_trace,
    document_ingestion,
)
from webapp.backend.services.accounting_contracts import (
    DocumentFacts,
    EvidenceReference,
    LineItemFacts,
)
from webapp.backend.services.gemini_supplementary_verification import (
    GeminiSupplementaryObservation,
    SupplementaryTarget,
    SupplementaryTargetType,
    merge_supplementary_observations,
)
from webapp.backend.services.controlled_external_experiment import (
    ControlledDocumentCallBudget,
    ExperimentExecutionMode,
    ExperimentProviderContext,
)
from webapp.backend.services.phase_a_calibration_runner import (
    _finalize_controlled_processor_result,
)


def _initial_facts() -> dict:
    return {
        "vendor_name": "Synthetic Vendor",
        "invoice_number": "SYN-1",
        "invoice_date": "2026-01-01",
        "service_date": "2026-01-01",
        "due_date": None,
        "due_date_text": "Upon Receipt",
        "payment_terms": "Upon Receipt",
        "bill_or_credit": "Bill",
        "account_number": None,
        "service_address": None,
        "sold_to_raw_text": None,
        "job_site_raw_text": None,
        "address_role": None,
        "location_candidate": None,
        "service_period_start": None,
        "service_period_end": None,
        "service_period": None,
        "property_candidate": None,
        "property_abbreviation": None,
        "invoice_description": "Synthetic visible service",
        "line_items": [{
            "source_page": 1,
            "section_header": None,
            "row_label": "A",
            "location_candidate": None,
            "activity": "Visible service",
            "description": "Visible service",
            "raw_description": "Visible service",
            "normalized_description": None,
            "generated_description": None,
            "quantity": 1,
            "unit_price": 100,
            "amount": 100,
            "tax": None,
            "confidence": 0.8,
            "evidence": [{
                "page": 1,
                "text": "visible line",
                "normalized_text": None,
                "bbox": [1, 2, 30, 40],
                "source_type": "document_observation",
                "extraction_method": "gemini_facts_transport",
                "confidence": 0.8,
            }],
        }],
        "excluded_paid_rows": [],
        "subtotal": 100,
        "tax_amount": 0,
        "shipping_amount": 0,
        "fees_amount": 0,
        "total_amount": 100,
        "confidence": 0.8,
        "warnings": ["financial_content_collapsed"],
        "needs_manual_review": True,
        "visual_extraction_status": "partial",
        "unresolved_visual_regions": [],
        "page_reconciliations": [{
            "page": 1,
            "component_total": 100,
            "printed_total": 100,
            "status": "reconciled",
        }],
        "evidence": [{
            "page": 1,
            "text": "visible header",
            "normalized_text": None,
            "bbox": [0, 0, 100, 25],
            "source_type": "document_observation",
            "extraction_method": "gemini_facts_transport",
            "confidence": 0.8,
        }],
        "observed_date_candidates": [],
        "transport_schema_version": "gemini-facts-transport/1.0",
        "transport_prompt_version": "gemini-facts-only/2.0",
    }


def _merged_unresolved_facts() -> tuple[dict, dict]:
    initial = _initial_facts()
    target = SupplementaryTarget(
        target_type=SupplementaryTargetType.TOTAL_MISMATCH,
        page_number=1,
        field_name="reconciliation",
        local_trigger_codes=["financial_content_collapsed"],
    )
    observation = GeminiSupplementaryObservation.model_validate({
        "target_type": "total_mismatch",
        "observed_candidate_value": None,
        "raw_visible_text": "visible but unresolved",
        "page_number": 1,
        "evidence_reference": {
            "page_number": 1,
            "bbox": [10, 20, 30, 40],
        },
        "confidence": 0.45,
        "contradiction_flag": False,
        "unresolved_flag": True,
        "warnings": [],
    })
    merged = merge_supplementary_observations(initial, [(target, observation)])
    return initial, merged


def _patch_single_invoice_route(
    monkeypatch, tmp_path: Path, merged: dict,
) -> tuple[dict, list[str]]:
    source = tmp_path / "synthetic.pdf"
    source.write_bytes(b"synthetic-pdf")
    batch_id = "batch_20260719_000000_001"
    monkeypatch.setattr(settings, "BATCHES_ROOT", tmp_path / "batches")
    synthetic_context = ExperimentProviderContext(
        execution_mode=ExperimentExecutionMode.CONTROLLED_EXTERNAL,
        authorized_provider="gemini",
        authorized_model="synthetic-model",
        authorized_profile_id="synthetic-gemini",
        allowed_endpoint=(
            "https://generativelanguage.googleapis.com/"
            "v1beta/openai/chat/completions"
        ),
        manifest_sha256="a" * 64,
        document_sha256="b" * 64,
        call_budget=ControlledDocumentCallBudget(),
    )
    monkeypatch.setattr(
        ai_invoice_processor,
        "require_experiment_provider_context",
        lambda value: value,
    )
    monkeypatch.setattr(
        ai_invoice_processor.ai_provider,
        "provider_status",
        lambda _context=None: ai_provider.AIProviderStatus(
            enabled=True,
            provider="gemini",
            model="synthetic-model",
            configured=True,
            supports_vision=True,
            vision_enabled=True,
            vision_provider="gemini",
            vision_model="synthetic-model",
            vision_mode="always",
            message="configured",
        ),
    )
    monkeypatch.setattr(
        ai_invoice_processor.ai_provider,
        "extraction_profile_identity",
        lambda **_: ("gemini", "synthetic-gemini", "synthetic-model"),
    )
    monkeypatch.setattr(ai_invoice_processor.ai_provider, "reset_cost_budget", lambda *_: None)
    monkeypatch.setattr(
        ai_invoice_processor.ai_provider, "controlled_external_active", lambda: True,
    )
    monkeypatch.setattr(
        ai_invoice_processor.ai_provider,
        "_send_chat_completion",
        lambda **_: (_ for _ in ()).throw(AssertionError("external_provider_call")),
    )
    monkeypatch.setattr(
        ai_invoice_processor,
        "load_references",
        lambda: {"vendors": [], "properties": [], "gl_accounts": []},
    )
    monkeypatch.setattr(
        ai_invoice_processor, "get_template_rules",
        lambda: {"columns": [], "required_columns": [], "recommended_columns": []},
    )
    monkeypatch.setattr(ai_invoice_processor, "_process_cached_document_manifest", lambda **_: None)
    monkeypatch.setattr(
        ai_invoice_processor.page_facts_cache,
        "finalize_document_manifest",
        lambda **_: None,
    )
    monkeypatch.setattr(
        ai_invoice_processor.document_ingestion,
        "ingest_document",
        lambda *_args, **_kwargs: document_ingestion.DocumentCandidate(
            source_file=source.name,
            source_type="pdf",
            source_path=str(source),
            mime_type="application/pdf",
            file_size_bytes=source.stat().st_size,
            page_count=1,
            document_text="synthetic visible invoice text",
            text_quality_score=0.2,
            needs_vision=True,
            pages=[document_ingestion.PageCandidate(
                page_number=1,
                text="synthetic visible invoice text",
                text_quality_score=0.2,
            )],
        ),
    )
    monkeypatch.setattr(ai_invoice_processor, "_extract_known_vendor_payload_from_ocr", lambda *_: {})
    monkeypatch.setattr(
        ai_invoice_processor, "_select_prompt_references",
        lambda *_args, **_kwargs: {"vendors": [], "properties": [], "gl_accounts": []},
    )
    monkeypatch.setattr(ai_invoice_processor, "_should_use_vision_for_candidate", lambda *_: True)
    monkeypatch.setattr(ai_invoice_processor, "_should_use_native_pdf_for_candidate", lambda *_: False)
    monkeypatch.setattr(ai_invoice_processor, "_page_facts_lookup", lambda **_: ([], None, None))
    monkeypatch.setattr(
        ai_invoice_processor.ai_vision,
        "render_pdf_pages_as_data_urls",
        lambda **_: ["data:image/png;base64,AA=="],
    )
    monkeypatch.setattr(ai_invoice_processor.ai_vision, "save_vision_trace_regions", lambda **_: None)
    monkeypatch.setattr(ai_invoice_processor, "_extract_fast_first_or_standard", lambda **_: copy.deepcopy(merged))
    monkeypatch.setattr(ai_invoice_processor, "_requires_critical_header_verification", lambda *_: False)
    monkeypatch.setattr(ai_invoice_processor, "_requires_row_identity_verification", lambda *_: False)
    monkeypatch.setattr(ai_invoice_processor, "_reconcile_high_confidence_vision_candidates", lambda value: value)
    monkeypatch.setattr(ai_invoice_processor, "_repair_ai_payload_from_ocr", lambda value, *_a, **_k: value)
    monkeypatch.setattr(
        ai_invoice_processor.support_documents,
        "upload_source_document_to_dropbox",
        lambda **_: SimpleNamespace(
            success=True, review_code="", review_message="", url="",
            status="dry_run", dropbox_path="",
        ),
    )
    strict_evidence = [
        EvidenceReference(
            document_id="doc-synthetic",
            page=item.get("page"),
            text=item.get("text"),
            bbox=item.get("bbox"),
            source_type=item["source_type"],
            extraction_method=item["extraction_method"],
            confidence=item.get("confidence"),
        )
        for item in merged["evidence"]
    ]
    strict_facts = DocumentFacts(
        document_id="doc-synthetic",
        invoice_id="SYN-1",
        vendor_candidate="Synthetic Vendor",
        invoice_number="SYN-1",
        invoice_date="2026-01-01",
        total_amount=100,
        line_items=[LineItemFacts(
            line_item_id="line-1",
            raw_description="Visible service",
            amount=100,
            evidence=strict_evidence,
        )],
        extraction_route="gemini_facts_transport",
        extraction_model="synthetic-model",
        evidence=strict_evidence,
    )
    accounting_calls: list[str] = []
    monkeypatch.setattr(
        ai_invoice_processor,
        "ai_result_to_invoice",
        lambda *_args, **_kwargs: accounting_calls.append("called") or {
            "invoice_number": "SYN-1",
            "source_file": source.name,
            "rows": [{
                "Invoice Number": "SYN-1",
                "Amount": 100,
                "_meta": {
                    "document_facts": strict_facts.model_dump(mode="json"),
                },
            }],
        },
    )
    result = ai_invoice_processor.process_ai_vendor_files(
        batch_id=batch_id,
        vendor_key="unknown",
        files=[source],
        detection={source.name: {"vendor_key": "unknown"}},
        dry_run=True,
        experiment_provider_context=synthetic_context,
    )
    return result, accounting_calls


def test_controlled_single_route_preserves_unresolved_visual_evidence(
    tmp_path: Path, monkeypatch,
):
    initial, merged = _merged_unresolved_facts()
    frozen = copy.deepcopy(initial)

    result, accounting_calls = _patch_single_invoice_route(
        monkeypatch, tmp_path, merged,
    )

    assert initial == frozen
    assert merged["evidence"][0] == initial["evidence"][0]
    assert merged["supplementary_evidence_revisions"][0]["source_role"] == (
        "supplementary_visual_observation"
    )
    assert any(
        item.get("extraction_method") == "gemini_supplementary_verification"
        for item in merged["evidence"]
    )
    assert result["invoices"]
    assert result["summary"]["processing_failures"] == 0
    assert accounting_calls == ["called"]
    assert result["manual_review_rows"]
    assert "supplementary_visual_evidence_unresolved" in (
        result["manual_review_rows"][0]["reason_codes"]
    )
    assert merged["needs_manual_review"] is True
    assert merged["unresolved_visual_regions"][-1]["reason"] == (
        "supplementary_visual_evidence_unresolved"
    )


@pytest.mark.parametrize("route", ["single", "segmented"])
def test_initial_and_supplementary_evidence_are_canonically_available(route: str):
    initial, merged = _merged_unresolved_facts()
    frozen = copy.deepcopy(merged)

    diagnostic = ai_invoice_processor._controlled_visual_evidence_diagnostic(
        merged, route=route, source_available=True,
    )

    assert merged == frozen
    assert diagnostic["evidence_validation_outcome"] == "valid"
    assert diagnostic["initial_evidence_count"] == 2
    assert diagnostic["supplementary_evidence_count"] == 1
    assert diagnostic["evidence_reference_count"] == 1
    assert diagnostic["page_reference_count"] == 4
    assert diagnostic["bounding_region_present_count"] == 4
    assert diagnostic["observed_text_present_count"] == 3
    assert diagnostic["merge_stage_outcome"] == "supplementary_merged"
    assert diagnostic["raise_branch"] == "not_raised"
    assert not diagnostic["missing_required_evidence_fields"]
    assert initial["evidence"][0] == merged["evidence"][0]


def test_missing_visual_evidence_remains_fail_closed_and_is_not_fabricated():
    diagnostic = ai_invoice_processor._controlled_visual_evidence_diagnostic(
        {}, route="single", source_available=True,
    )
    assert diagnostic["evidence_object_count"] == 0
    assert diagnostic["evidence_reference_count"] == 0
    assert diagnostic["evidence_validation_outcome"] == "missing_required_evidence"
    assert diagnostic["raise_branch"] == "single_visual_payload_insufficient"
    assert diagnostic["missing_required_evidence_fields"] == ["canonical_evidence"]
    with pytest.raises(
        ai_provider.AIProviderUnavailable,
        match="canonical visual evidence",
    ):
        ai_invoice_processor._require_controlled_visual_evidence(
            {}, route="single", source_available=True,
        )


def test_visual_evidence_diagnostic_is_strictly_categorical_and_private(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(settings, "BATCHES_ROOT", tmp_path)
    _initial, merged = _merged_unresolved_facts()
    merged["evidence"][0]["text"] = "PRIVATE-VENDOR PRIVATE-AMOUNT PRIVATE-ADDRESS"

    with ai_runtime_trace.operation(
        batch_id="batch_20260719_000000_002",
        stage="must-not-persist",
        provider="must-not-persist",
        model="must-not-persist",
        profile_id="must-not-persist",
    ):
        ai_invoice_processor._require_controlled_visual_evidence(
            merged, route="single", source_available=True,
        )

    trace = (
        tmp_path / "batch_20260719_000000_002" / "audit" / "ai_request_trace.jsonl"
    ).read_text(encoding="utf-8")
    event = json.loads(trace.splitlines()[-1])
    assert set(event) == {
        "schema", "event", "evidence_object_count", "initial_evidence_count",
        "supplementary_evidence_count", "evidence_reference_count",
        "page_reference_count", "bounding_region_present_count",
        "observed_text_present_count", "source_kind_categories",
        "missing_required_evidence_fields", "merge_stage_outcome",
        "evidence_validation_outcome", "raise_branch",
    }
    assert event["event"] == "visual_evidence_contract"
    assert "PRIVATE" not in trace
    assert "must-not-persist" not in trace
    assert "batch_" not in trace


def test_missing_canonical_evidence_never_reaches_document_facts_or_accounting(
    tmp_path: Path, monkeypatch,
):
    payload = _initial_facts()
    payload["evidence"] = []
    payload["line_items"][0]["evidence"] = []

    result, accounting_calls = _patch_single_invoice_route(
        monkeypatch, tmp_path, payload,
    )

    assert accounting_calls == []
    assert result["invoices"] == []
    assert result["summary"]["processing_failures"] == 1
    assert result["unsupported_files"][0]["reason"] == (
        "visual_evidence_unavailable"
    )


def test_synthetic_end_to_end_unresolved_evidence_is_review_required(
    tmp_path: Path, monkeypatch,
):
    _initial, merged = _merged_unresolved_facts()
    processor_result, accounting_calls = _patch_single_invoice_route(
        monkeypatch, tmp_path, merged,
    )
    callbacks = {"normalize": 0, "readiness": 0, "provenance": 0}
    result_path = tmp_path / "final" / "_webapp_result.json"

    finalized = _finalize_controlled_processor_result(
        processor_result,
        result_path=result_path,
        normalize_result=lambda _value: callbacks.__setitem__(
            "normalize", callbacks["normalize"] + 1,
        ),
        attach_readiness=lambda _value: callbacks.__setitem__(
            "readiness", callbacks["readiness"] + 1,
        ),
        assert_provenance=lambda _value: callbacks.__setitem__(
            "provenance", callbacks["provenance"] + 1,
        ),
    )

    disposition = finalized["phase_a_terminal_disposition"]
    assert accounting_calls == ["called"]
    assert result_path.exists()
    assert disposition["disposition"] == "review_required"
    assert disposition["document_facts_exist"] is True
    assert disposition["provenance_exists"] is True
    assert disposition["accepted"] is False
    assert finalized["export_allowed"] is False
    assert callbacks == {"normalize": 0, "readiness": 0, "provenance": 0}
