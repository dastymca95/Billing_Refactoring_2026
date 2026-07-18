from __future__ import annotations

import threading
import time
import json
import io
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from webapp.backend import settings
from webapp.backend.services import (
    accounting_artifact_cache,
    accounting_integration_bridges,
    accounting_readiness,
    ai_provider,
    ai_invoice_processor,
    fast_first_facts,
    local_processing_guard,
    page_facts_cache,
)
from webapp.backend.services.accounting_pipeline_v2 import capture_source_fields, decide_row


def _image(path: Path, *, paid: bool = False) -> None:
    image = Image.new("RGB", (612, 792), "white")
    draw = ImageDraw.Draw(image)
    draw.text((72, 72), "Invoice 1001   Unit 57B   $125.00", fill="black")
    if paid:
        draw.text((400, 72), "PAID", fill="black")
    image.save(path)


def test_exact_visual_identity_reuses_copies_but_not_paid_edits(tmp_path, monkeypatch):
    root = tmp_path / "data"
    batch = root / "batches" / "batch_20260717_000000_001" / "input"
    batch.mkdir(parents=True)
    _image(batch / "first.png")
    (batch / "copy.png").write_bytes((batch / "first.png").read_bytes())
    _image(batch / "paid.png", paid=True)
    monkeypatch.setattr(settings, "WEBAPP_DATA_ROOT", root)
    monkeypatch.setattr(settings, "BATCHES_ROOT", root / "batches")

    first = page_facts_cache.exact_visual_page_identity(
        batch_id="batch_20260717_000000_001", filename="first.png", page_number=1
    )
    copied = page_facts_cache.exact_visual_page_identity(
        batch_id="batch_20260717_000000_001", filename="copy.png", page_number=1
    )
    paid = page_facts_cache.exact_visual_page_identity(
        batch_id="batch_20260717_000000_001", filename="paid.png", page_number=1
    )
    assert first == copied
    assert first.visual_sha256 != paid.visual_sha256


def test_page_facts_cache_preserves_typed_evidence_and_discards_gl(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "WEBAPP_DATA_ROOT", tmp_path)
    identity = page_facts_cache.VisualPageIdentity(
        visual_sha256="a" * 64,
        width_points=612,
        height_points=792,
        rotation=0,
        raster_width=1224,
        raster_height=1584,
        colorspace_components=3,
    )
    context = page_facts_cache.PageFactsCacheContext(
        provider="provider", profile_id="vision", model="model"
    )
    payload = {
        "vendor_name": "Observed Vendor",
        "invoice_number": "1001",
        "total_amount": 125,
        "line_items": [{
            "raw_description": "Visible service",
            "amount": 125,
            "gl_account_candidate": "9999",
        }],
        "date_provenance": [{
            "field": "service_date",
            "value": "2025-02-21",
            "raw_value": "2/21/25",
            "provenance": "document_observed",
        }],
        "_handwritten_row_identities": [{
            "raw_value": "57B",
            "alternatives": [],
            "confidence": 0.8,
            "crop_coordinates": {
                "page": 1, "x": 100, "y": 200, "width": 100, "height": 100,
                "render_dpi": 600,
            },
            "catalog_matches": ["57B"],
            "resolved_unit": "57B",
            "status": "confirmed",
            "resolution_basis": "visual_and_catalog_agree",
        }],
        "excluded_paid_rows": [{
            "raw_apartment_number": "53B",
            "component_amounts": {"Bath Tub": "350.00"},
            "row_total": "350.00",
            "paid_marker_evidence": [{"page": 1, "text": "PAID", "confidence": 0.99}],
            "exclusion_reason": "visible_paid_marker",
        }],
    }
    artifact = page_facts_cache.save(
        identities=[identity], context=context, observed_payload=payload
    )
    assert artifact.date_provenance[0].provenance == "document_observed"
    assert artifact.handwritten_identity_evidence[0].raw_value == "57B"
    assert artifact.excluded_paid_rows[0].raw_apartment_number == "53B"
    assert "gl_account_candidate" not in artifact.observed_payload["line_items"][0]


def test_persisted_fact_migration_preserves_inference_and_row_identity_evidence(
    tmp_path, monkeypatch
):
    root = tmp_path / "data"
    batch_id = "batch_20260717_000000_002"
    input_dir = root / "batches" / batch_id / "input"
    processed_dir = root / "batches" / batch_id / "processed"
    input_dir.mkdir(parents=True)
    processed_dir.mkdir(parents=True)
    _image(input_dir / "invoice.png")
    row_verification = {
        "payable_needs_confirmation": True,
        "candidates": [{"raw_value": "57B", "alternatives": ["53B"]}],
    }
    persisted = {
        "all_invoices": [{
            "source_file": "invoice.png",
            "source_page": 1,
            "invoice_number": "1001",
            "invoice_date": "02/21/2025",
            "total_amount": 125.0,
            "confidence": 0.8,
            "validation_summary": {
                "total_reconciliation_passed": True,
                "reconciled_total": 125.0,
            },
            "manual_review_codes": [
                "invoice_date_inferred_from_service_date",
                "row_identity_needs_confirmation",
            ],
            "rows": [{
                "Vendor": "Observed Vendor",
                "Invoice Number": "1001",
                "Invoice Date": "02/21/2025",
                "Due Date": "02/21/2025",
                "Bill or Credit": "Bill",
                "Line Item Description": "Visible service",
                "Quantity": 1,
                "Unit Price": 125,
                "Amount": 125,
                "_meta": {
                    "ai_service_date": "02/21/2025",
                    "ai_due_date_text": "Upon Receipt",
                    "ai_payment_terms": "Upon Receipt",
                    "ai_date_provenance": [
                        {
                            "field": "invoice_date",
                            "value": "02/21/2025",
                            "raw_value": None,
                            "provenance": "tenant_policy_inference",
                            "source_field": "service_date",
                        },
                        {
                            "field": "due_date",
                            "value": "02/21/2025",
                            "raw_value": None,
                            "provenance": "tenant_policy_inference",
                            "source_field": "due_date_text",
                        },
                    ],
                    "ai_row_identity_verification": row_verification,
                },
            }],
        }]
    }
    (processed_dir / "_webapp_result.json").write_text(
        json.dumps(persisted), encoding="utf-8"
    )
    monkeypatch.setattr(settings, "WEBAPP_DATA_ROOT", root)
    monkeypatch.setattr(settings, "BATCHES_ROOT", root / "batches")
    identity = page_facts_cache.exact_visual_page_identity(
        batch_id=batch_id, filename="invoice.png", page_number=1
    )
    context = page_facts_cache.PageFactsCacheContext(
        provider="provider", profile_id="vision", model="model"
    )

    artifact = page_facts_cache.seed_from_persisted_result(
        batch_id=batch_id,
        source_file="invoice.png",
        source_page=1,
        identities=[identity],
        context=context,
    )

    assert artifact is not None
    assert artifact.observed_payload["invoice_date"] == ""
    assert artifact.observed_payload["due_date"] == ""
    assert artifact.observed_payload["service_date"] == "02/21/2025"
    assert artifact.observed_payload["due_date_text"] == "Upon Receipt"
    assert artifact.observed_payload["_row_identity_verification"] == row_verification
    assert artifact.observed_payload["visual_extraction_status"] == "needs_confirmation"


def test_reference_key_migration_accepts_only_direct_observed_facts(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "WEBAPP_DATA_ROOT", tmp_path)
    identity = page_facts_cache.VisualPageIdentity(
        visual_sha256="b" * 64,
        width_points=612,
        height_points=792,
        rotation=0,
        raster_width=1224,
        raster_height=1584,
        colorspace_components=3,
    )
    old_context = page_facts_cache.PageFactsCacheContext(
        provider="provider", profile_id="vision", model="model",
        reference_fingerprint="old-tenant-references",
    )
    new_context = page_facts_cache.PageFactsCacheContext(
        provider="provider", profile_id="vision", model="model",
        reference_fingerprint="",
    )
    page_facts_cache.save(
        identities=[identity],
        context=old_context,
        observed_payload={
            "vendor_name": "Observed Vendor", "invoice_number": "1001",
            "total_amount": 25, "line_items": [{"description": "repair", "amount": 25}],
            "service_date": "4 9 25", "confidence": 0.70,
        },
    )
    page_facts_cache.save(
        identities=[identity],
        context=old_context.model_copy(update={"reference_fingerprint": "other-old-key"}),
        observed_payload={
            "vendor_name": "Observed Vendor", "invoice_number": "1001",
            "total_amount": 25, "line_items": [{"description": "repair", "amount": 25}],
            "service_date": "04/09/2025", "confidence": 0.90,
        },
    )
    migrated = page_facts_cache.load_compatible_exact_observed(
        [identity], new_context
    )
    assert migrated is not None
    assert migrated.context.reference_fingerprint == ""
    assert migrated.observed_payload["service_date"] == "04/09/2025"

    other_identity = identity.model_copy(update={"visual_sha256": "c" * 64})
    page_facts_cache.save(
        identities=[other_identity],
        context=old_context,
        observed_payload={
            "_migrated_from_validated_result": True,
            "vendor_name": "Derived Vendor", "invoice_number": "1002",
            "total_amount": 25, "line_items": [{"description": "repair", "amount": 25}],
        },
    )
    assert page_facts_cache.load_compatible_exact_observed(
        [other_identity], new_context
    ) is None


def test_document_manifest_shortcuts_only_exact_source_and_current_model(
    tmp_path, monkeypatch
):
    root = tmp_path / "data"
    batch_id = "batch_20260717_000000_003"
    input_dir = root / "batches" / batch_id / "input"
    input_dir.mkdir(parents=True)
    _image(input_dir / "invoice.png")
    monkeypatch.setattr(settings, "WEBAPP_DATA_ROOT", root)
    monkeypatch.setattr(settings, "BATCHES_ROOT", root / "batches")
    identity = page_facts_cache.exact_visual_page_identity(
        batch_id=batch_id, filename="invoice.png", page_number=1
    )
    context = page_facts_cache.PageFactsCacheContext(
        provider="provider", profile_id="vision", model="model"
    )
    artifact = page_facts_cache.save(
        identities=[identity], context=context,
        observed_payload={
            "vendor_name": "Observed Vendor", "invoice_number": "1001",
            "total_amount": 25, "line_items": [{"description": "repair", "amount": 25}],
        },
    )
    page_facts_cache.register_document_artifact(
        batch_id=batch_id, filename="invoice.png", group_index=1,
        page_numbers=[1], artifact=artifact,
    )
    assert page_facts_cache.load_document_manifest(
        batch_id=batch_id, filename="invoice.png",
        allowed_provider_models={("provider", "model")},
    ) == []
    page_facts_cache.finalize_document_manifest(
        batch_id=batch_id, filename="invoice.png", expected_group_count=1
    )
    loaded = page_facts_cache.load_document_manifest(
        batch_id=batch_id, filename="invoice.png",
        allowed_provider_models={("provider", "model")},
    )
    assert len(loaded) == 1
    assert loaded[0][1].observed_payload["invoice_number"] == "1001"
    assert page_facts_cache.load_document_manifest(
        batch_id=batch_id, filename="invoice.png",
        allowed_provider_models={("provider", "other-model")},
    ) == []
    manifest_text = next(
        (root / "cache" / "document_facts_manifest").glob("*.json")
    ).read_text(encoding="utf-8")
    assert "invoice.png" not in manifest_text


def test_normalized_facts_cache_is_separate_and_dependency_versioned(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "WEBAPP_DATA_ROOT", tmp_path)
    monkeypatch.setattr(
        page_facts_cache, "normalization_dependency_fingerprint", lambda: "deps-v1"
    )
    page_facts_cache.save_normalized_facts(
        "observed-key",
        {
            "vendor_name": "Vendor",
            "line_items": [{
                "description": "repair", "amount": 25,
                "accounting_decision": {"selected_gl_code": "6512"},
                "selected_gl": "6512",
            }],
            "accounting_readiness": {"export_allowed": True},
            "export_allowed": True,
        },
    )
    loaded = page_facts_cache.load_normalized_facts("observed-key")
    assert loaded is not None
    assert "accounting_readiness" not in loaded
    assert "export_allowed" not in loaded
    assert "accounting_decision" not in loaded["line_items"][0]
    assert "selected_gl" not in loaded["line_items"][0]
    monkeypatch.setattr(
        page_facts_cache, "normalization_dependency_fingerprint", lambda: "deps-v2"
    )
    assert page_facts_cache.load_normalized_facts("observed-key") is None


def test_normalization_dependency_tracks_active_resman_snapshots(monkeypatch):
    monkeypatch.setattr(
        page_facts_cache,
        "_active_resman_context_fingerprints",
        lambda: {"gl_accounts": "snapshot-a", "properties_units": "snapshot-a"},
    )
    first = page_facts_cache.normalization_dependency_fingerprint()
    monkeypatch.setattr(
        page_facts_cache,
        "_active_resman_context_fingerprints",
        lambda: {"gl_accounts": "snapshot-b", "properties_units": "snapshot-a"},
    )
    second = page_facts_cache.normalization_dependency_fingerprint()
    assert first != second


def test_property_history_fallback_rejects_administrative_address_role(monkeypatch):
    monkeypatch.setattr(
        ai_invoice_processor,
        "_vendor_rule_for_name",
        lambda _vendor: {"source_properties_observed": "TPW"},
    )

    def forbidden_history_lookup(**_kwargs):
        raise AssertionError("administrative evidence must not reach vendor history")

    monkeypatch.setattr(
        ai_invoice_processor,
        "_historical_property_for_vendor",
        forbidden_history_lookup,
    )
    assert ai_invoice_processor._required_property_fallback(
        vendor_name="Observed Vendor",
        property_candidate="Management Company",
        service_address="",
        address_role="sold_to",
        document_text="",
    ) == ("", "")


def test_accounting_artifact_invalidates_manual_and_policy_changes(monkeypatch):
    dependencies = {"gl_catalog": "v1", "policy": "a"}
    row = {
        "Vendor": "Vendor",
        "Property": "P1",
        "Location": "1A",
        "GL Account": "6512",
        "Total Amount": 25,
        "_meta": {"accounting_decision": {
            "decision_id": "decision-1", "selected_gl_code": "6512"
        }},
    }
    accounting_artifact_cache.mark(row, dependencies)
    assert accounting_artifact_cache.is_reusable(row, dependencies)
    row["GL Account"] = "6669"
    assert not accounting_artifact_cache.is_reusable(row, dependencies)
    row["GL Account"] = "6512"
    assert not accounting_artifact_cache.is_reusable(
        row, {"gl_catalog": "v1", "policy": "b"}
    )


def test_ai_accounting_artifact_persists_only_authoritative_engine_result(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "WEBAPP_DATA_ROOT", tmp_path)
    dependencies = {"gl_catalog": "v1", "policy": "a"}
    original = {
        "Vendor": "Vendor", "Property": "P1", "Location": "1A",
        "GL Account": "", "Amount": 25,
        "_meta": {
            "ai_generated": True,
            "source_text": {"raw_description": "repair"},
            "document_facts": {"document_id": "source.pdf"},
        },
    }
    key = accounting_artifact_cache.request_fingerprint(original, dependencies)
    original["GL Account"] = "6512"
    original["_meta"]["accounting_decision"] = {
        "decision_id": "decision-1", "selected_gl_code": "6512"
    }
    accounting_artifact_cache.mark(original, dependencies, request_key=key)
    fresh = {
        "Vendor": "Vendor", "Property": "P1", "Location": "1A",
        "GL Account": "", "Amount": 25,
        "_meta": {"ai_generated": True, "source_text": {"raw_description": "repair"}},
    }
    assert accounting_artifact_cache.hydrate(fresh, dependencies)
    assert fresh["GL Account"] == "6512"
    assert fresh["_meta"]["accounting_decision"]["decision_id"] == "decision-1"


def test_cached_accounting_decision_never_skips_source_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "WEBAPP_DATA_ROOT", tmp_path)
    monkeypatch.setattr(accounting_integration_bridges, "v2_enabled", lambda: True)
    dependencies = {"gl_catalog": "v1", "policy": "a"}
    monkeypatch.setattr(
        accounting_artifact_cache,
        "dependency_versions",
        lambda: dependencies,
    )
    base = {
        "Vendor": "Observed Vendor",
        "Invoice Number": "1001",
        "Invoice Description": "Visible repair invoice",
        "Line Item Number": "1",
        "Line Item Description": "Visible repair",
        "Property Abbreviation": "P1",
        "Location": "1A",
        "GL Account": "",
        "Amount": 25,
        "_meta": {"ai_generated": True},
    }
    seeded = json.loads(json.dumps(base))
    capture_source_fields(seeded, document_id="source.pdf", line_item_id="1")
    seeded["_meta"]["normalized_source_description"] = "Visible repair"
    seeded["_meta"]["generated_line_description"] = "Visible repair"
    seeded["_meta"]["generated_invoice_description"] = "Visible repair invoice"
    request_key = accounting_artifact_cache.request_fingerprint(
        seeded, dependencies
    )
    decision = decide_row(
        seeded,
        document_id="source.pdf",
        line_item_id="1",
        extraction_route="test",
        allow_ai_semantic_reasoning=False,
    )
    accounting_artifact_cache.mark(
        seeded,
        dependencies,
        request_key=request_key,
    )

    fresh = json.loads(json.dumps(base))
    rows = accounting_integration_bridges.RowAccountingV2Adapter().enrich_rows(
        [fresh],
        document_context={"document_id": "source.pdf"},
    )

    assert rows[0]["GL Account"] == decision.selected_gl_code
    assert rows[0]["_meta"]["source_text"]["raw_description"] == "Visible repair"
    assert rows[0]["_meta"]["document_facts"]["document_id"] == "source.pdf"
    assert (
        rows[0]["_meta"]["accounting_decision"]["decision_id"]
        == decision.decision_id
    )


def test_bounded_group_pool_is_concurrent_failure_isolated_and_ordered(monkeypatch):
    lock = threading.Lock()
    active = 0
    peak = 0

    def worker(*, invoice_groups, **kwargs):
        nonlocal active, peak
        index = invoice_groups[0]["_stable_group_index"]
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.03 * (5 - index))
        with lock:
            active -= 1
        if index == 3:
            raise RuntimeError("isolated")
        return {
            "invoices": [{"index": index}],
            "manual_review": [],
            "unsupported": [],
        }

    monkeypatch.setattr(ai_invoice_processor, "_process_segmented_invoice_groups", worker)
    monkeypatch.setattr(settings, "AI_INVOICE_GROUP_WORKERS", 4)
    result = ai_invoice_processor._process_segmented_invoice_groups_bounded(
        invoice_groups=[{}, {}, {}, {}],
        source_file=Path("source.pdf"),
        vendor_hint="Vendor",
    )
    assert peak >= 4
    assert [item["index"] for item in result["invoices"]] == [1, 2, 4]
    assert result["manual_review"][0]["reason_codes"] == [
        "segmented_invoice_processing_failed"
    ]


def test_bounded_ai_file_pool_is_concurrent_failure_isolated_and_ordered(tmp_path):
    files = [tmp_path / f"{index}.pdf" for index in range(5)]
    lock = threading.Lock()
    active = 0
    peak = 0

    def worker(path: Path):
        nonlocal active, peak
        index = int(path.stem)
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.03 * (5 - index))
        with lock:
            active -= 1
        if index == 3:
            raise RuntimeError("isolated")
        return {"index": index}

    results = ai_invoice_processor._run_ai_file_workers_bounded(
        files, worker, max_workers=4
    )

    assert [path.stem for path, _, _ in results] == ["0", "1", "2", "3", "4"]
    assert [payload and payload.get("index") for _, payload, _ in results] == [
        0, 1, 2, None, 4
    ]
    assert results[3][2] is not None
    assert peak == 4


def test_failed_provider_source_remains_visible_blank_and_unexportable(
    tmp_path, monkeypatch
):
    source = tmp_path / "failed.pdf"
    source.write_bytes(b"immutable source")
    invoice = ai_invoice_processor._failed_extraction_invoice(
        batch_id="batch_20260717_000000_004",
        source_file=source,
        vendor_hint="unknown vendor",
        failure_code="provider_circuit_open",
    )
    row = invoice["rows"][0]
    assert invoice["source_file"] == "failed.pdf"
    assert invoice["source_artifact_retained"] is True
    assert row["GL Account"] == ""
    assert row["Property Abbreviation"] == ""
    assert row["Amount"] is None
    assert row["_meta"]["source_text"]["raw_description"] is None

    monkeypatch.setattr(accounting_integration_bridges, "v2_enabled", lambda: True)
    enriched = accounting_integration_bridges.RowAccountingV2Adapter().enrich_rows(
        [row], document_context={"document_id": "failed.pdf"}
    )
    assert enriched[0]["GL Account"] == ""
    assert "accounting_decision" not in enriched[0]["_meta"]
    readiness = accounting_readiness.evaluate_rows(enriched)
    assert readiness.export_allowed is False
    assert any(issue.code.startswith("required_field_missing") for issue in readiness.blockers)


def _runtime_status_for_fallback_tests() -> ai_provider.AIProviderStatus:
    return ai_provider.AIProviderStatus(
        enabled=True,
        provider="openai",
        model="runtime-text-model",
        configured=True,
        supports_vision=True,
        vision_enabled=True,
        vision_provider="openai",
        vision_model="runtime-vision-model",
        vision_mode="fallback_only",
        message="configured",
    )


def test_visual_transport_failure_uses_distinct_runtime_fallback(monkeypatch):
    calls = []

    def extract(**kwargs):
        calls.append(dict(kwargs))
        if kwargs.get("force_model_override"):
            return {"invoice_number": "safe-runtime-result"}
        raise ai_provider.AIProviderUnavailable(
            "primary unavailable", failure_code="vision_transport_error"
        )

    monkeypatch.setattr(ai_provider, "extract_invoice_vision_structured", extract)
    monkeypatch.setattr(
        ai_provider, "provider_status", _runtime_status_for_fallback_tests
    )
    monkeypatch.setattr(
        ai_provider,
        "extraction_profile_identity",
        lambda *, vision, model_override="", force_model_override=False: (
            ("openai", "runtime-vision-forced", model_override)
            if force_model_override
            else ("gemini", "gemini-vision", "economy-vision")
        ),
    )

    result = ai_invoice_processor._extract_vision_with_reduced_retry(
        vendor_hint="",
        document_text="safe",
        page_images_or_refs=["image-a"],
        template_schema={},
        property_reference=[],
        gl_reference=[],
        vendor_reference=[],
    )

    assert result["invoice_number"] == "safe-runtime-result"
    assert len(calls) == 2
    assert calls[1]["force_model_override"] is True
    assert calls[1]["model_override"] == "runtime-vision-model"


def test_text_transport_failure_uses_distinct_runtime_fallback(monkeypatch):
    calls = []

    def extract(**kwargs):
        calls.append(dict(kwargs))
        if kwargs.get("force_model_override"):
            return {"invoice_number": "safe-runtime-result"}
        raise ai_provider.AIProviderUnavailable(
            "primary unavailable", failure_code="provider_transport_error"
        )

    monkeypatch.setattr(ai_provider, "extract_invoice_structured", extract)
    monkeypatch.setattr(
        ai_provider, "provider_status", _runtime_status_for_fallback_tests
    )
    monkeypatch.setattr(
        ai_provider,
        "extraction_profile_identity",
        lambda *, vision, model_override="", force_model_override=False: (
            ("openai", "runtime-text-forced", model_override)
            if force_model_override
            else ("gemini", "gemini-text", "economy-text")
        ),
    )

    result = ai_invoice_processor._extract_text_with_runtime_fallback(
        vendor_hint="",
        document_text="safe",
        page_images_or_refs=[],
        template_schema={},
        property_reference=[],
        gl_reference=[],
        vendor_reference=[],
    )

    assert result["invoice_number"] == "safe-runtime-result"
    assert len(calls) == 2
    assert calls[1]["force_model_override"] is True
    assert calls[1]["model_override"] == "runtime-text-model"


def test_cross_file_dedup_does_not_drop_other_invoice_review_from_packet():
    def invoice(source: str, number: str):
        return {
            "source_file": source,
            "invoice_number": number,
            "invoice_date": "01/01/2026",
            "total_amount": 25,
            "rows": [{"Vendor": "Vendor", "Amount": 25, "_meta": {}}],
        }

    invoices = [
        invoice("packet.pdf", "A"),
        invoice("packet.pdf", "B"),
        invoice("single.pdf", "A"),
    ]
    reviews = [
        {"source_file": "packet.pdf", "invoice_number": "A"},
        {"source_file": "packet.pdf", "invoice_number": "B"},
        {"source_file": "single.pdf", "invoice_number": "A"},
    ]

    deduped, retained_reviews = ai_invoice_processor._deduplicate_invoices(
        invoices, reviews
    )

    assert [(item["source_file"], item["invoice_number"]) for item in deduped] == [
        ("single.pdf", "A"), ("packet.pdf", "B")
    ]
    assert {(item["source_file"], item["invoice_number"]) for item in retained_reviews} == {
        ("single.pdf", "A"), ("packet.pdf", "B")
    }


def test_native_pdf_capability_failure_is_process_local_and_fails_to_raster():
    ai_provider._reset_native_pdf_surface_for_tests()


@pytest.mark.parametrize("status,expected_attempts", [(401, 2), (403, 1), (404, 1)])
def test_permanent_provider_failure_opens_process_circuit(
    monkeypatch, status, expected_attempts
):
    ai_provider._reset_provider_circuits_for_tests()
    attempts = 0

    def reject(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise urllib.error.HTTPError(
            url="https://provider.invalid/v1/chat/completions",
            code=status,
            msg="permanent",
            hdrs=None,
            fp=io.BytesIO(b'{"error":{"type":"authentication_error"}}'),
        )

    monkeypatch.setattr(ai_provider.urllib.request, "urlopen", reject)
    payload = {
        "model": "probe-model",
        "messages": [{"role": "user", "content": "harmless probe"}],
    }
    with pytest.raises(ai_provider.AIProviderUnavailable) as first:
        ai_provider._send_chat_completion(
            provider="openai",
            payload=payload,
            vision=True,
            api_key_override="private-test-key",
            base_url_override="https://provider.invalid/v1",
            max_attempts_override=3,
            capability_override="visual_document_understanding",
        )
    assert first.value.http_status == status
    assert attempts == expected_attempts
    assert ai_provider.provider_circuit_report() == [{
        "provider": "openai",
        "model": "probe-model",
        "endpoint_surface": "chat_completions",
        "capability": "visual_document_understanding",
        "http_status": status,
        "failure_code": "vision_http_error",
    }]

    with pytest.raises(ai_provider.AIProviderUnavailable) as blocked:
        ai_provider._send_chat_completion(
            provider="openai",
            payload=payload,
            vision=True,
            api_key_override="private-test-key",
            base_url_override="https://provider.invalid/v1",
            capability_override="visual_document_understanding",
        )
    assert blocked.value.failure_code == "provider_circuit_open"
    assert attempts == expected_attempts
    ai_provider._reset_provider_circuits_for_tests()


def test_local_document_native_operations_are_serialized():
    lock = threading.Lock()
    active = 0
    peak = 0

    @local_processing_guard.serialized_local_document_operation
    def render(index: int) -> int:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.01)
        with lock:
            active -= 1
        return index

    with ThreadPoolExecutor(max_workers=4) as executor:
        values = list(executor.map(render, range(4)))
    assert values == [0, 1, 2, 3]
    assert peak == 1
    assert ai_provider.native_pdf_surface_available("model-a")
    ai_provider._mark_native_pdf_surface_unavailable("model-a")
    assert not ai_provider.native_pdf_surface_available("model-a")
    assert ai_provider.native_pdf_surface_available("model-b")
    ai_provider._reset_native_pdf_surface_for_tests()


def test_fast_first_fails_closed_and_escalates_unsafe_facts(monkeypatch):
    monkeypatch.setattr(settings, "AI_FAST_FIRST_FACTS_ONLY_ENABLED", True)
    monkeypatch.setattr(settings, "AI_FAST_FIRST_GOLDEN_PARITY_APPROVED", False)
    assert not fast_first_facts.production_enabled()
    reasons = fast_first_facts.escalation_reasons({
        "vendor_name": "Vendor",
        "invoice_number": "1001",
        "total_amount": 100,
        "line_items": [{"amount": 90, "activity": "Bath Tub", "row_label": ""}],
        "visual_extraction_status": "partial",
        "unresolved_visual_regions": ["apt column"],
    })
    assert "visual_status:partial" in reasons
    assert "unresolved_visual_regions" in reasons
    assert "row_identity_ambiguous" in reasons
    assert "invoice_reconciliation_failed" in reasons
