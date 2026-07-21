from __future__ import annotations

import json
from decimal import Decimal

import pytest

from webapp.backend import settings
from webapp.backend.services import ai_provider, ai_runtime_trace
from webapp.backend.services.accounting_pipeline_v2 import capture_source_fields, decide_row
from webapp.backend.services.gemini_facts_transport import (
    GeminiTransportJSONError,
    GeminiTransportSchemaError,
    TRANSPORT_SCHEMA_VERSION,
    build_safe_diagnostic,
    gemini_response_format,
    parse_and_normalize_gemini_facts,
)


def _payload() -> dict:
    return {
        "vendor_name": "Synthetic Vendor",
        "invoice_number": "SYN-1",
        "invoice_date": "07/19/2026",
        "service_date": None,
        "due_date": None,
        "due_date_text": "Upon Receipt",
        "payment_terms": "Upon Receipt",
        "bill_or_credit": "Bill",
        "account_number": None,
        "service_address": None,
        "sold_to_raw_text": None,
        "job_site_raw_text": None,
        "address_role": "unknown",
        "location_candidate": None,
        "service_period_start": None,
        "service_period_end": None,
        "service_period": None,
        "property_candidate": None,
        "property_abbreviation": None,
        "invoice_description": "Synthetic service",
        "line_items": [{
            "source_page": 1,
            "section_header": None,
            "row_label": "A",
            "location_candidate": None,
            "activity": "Synthetic activity",
            "raw_description": "Synthetic line",
            "quantity": "2",
            "unit_price": "12.50",
            "amount": "25.00",
            "tax": None,
            "confidence": 0.9,
            "evidence": [{
                "page": 1,
                "text": "Synthetic line",
                "bbox": [1.0, 2.0, 3.0, 4.0],
                "source_type": "line_item",
                "confidence": 0.9,
            }],
        }],
        "excluded_paid_rows": [],
        "subtotal": "25.00",
        "tax_amount": "0",
        "shipping_amount": None,
        "fees_amount": None,
        "total_amount": 25,
        "visual_extraction_status": "complete",
        "unresolved_visual_regions": [],
        "page_reconciliations": [{
            "page": 1,
            "component_total": "25.00",
            "printed_total": 25,
            "status": "reconciled",
        }],
        "evidence": [],
        "warnings": [],
        "confidence": 0.9,
    }


def _parse(text: str, **metadata):
    return parse_and_normalize_gemini_facts(
        text,
        provider="gemini",
        model="configured-test-model",
        request_profile="gemini-vision:facts-only",
        response_metadata=metadata,
    )


@pytest.mark.parametrize("wrapper", [
    lambda value: value,
    lambda value: f"```json\n{value}\n```",
    lambda value: f"Here is the requested JSON:\n{value}\nEnd of response.",
])
def test_compatibility_cases_clean_fence_and_harmless_prose(wrapper):
    result = _parse(wrapper(json.dumps(_payload())))
    assert result["total_amount"] == Decimal("25")
    assert result["line_items"][0]["amount"] == Decimal("25.00")
    assert result["transport_schema_version"] == TRANSPORT_SCHEMA_VERSION


def test_compatibility_numeric_strings_and_null_unknown_values_normalize_safely():
    payload = _payload()
    payload["subtotal"] = " 25.00 "
    payload["shipping_amount"] = ""
    payload["service_date"] = None
    result = _parse(json.dumps(payload))
    assert result["subtotal"] == Decimal("25.00")
    assert result["shipping_amount"] is None
    assert result["service_date"] is None
    invoice_date = next(
        row for row in result["observed_date_candidates"]
        if row["field"] == "invoice_date"
    )
    assert invoice_date == {
        "field": "invoice_date",
        "raw_value": "07/19/2026",
        "normalized_candidate": "2026-07-19",
        "provenance": "document_observed",
    }


def test_compatibility_extra_unknown_field_is_a_warning_not_authority():
    payload = _payload()
    payload["provider_note"] = "non-authoritative"
    result = _parse(json.dumps(payload))
    assert "gemini_transport_unknown_field:provider_note" in result["warnings"]
    assert "provider_note" not in result


def test_invalid_observed_numeric_never_silently_becomes_null():
    payload = _payload()
    payload["line_items"][0]["amount"] = "illegible-amount"
    result = _parse(json.dumps(payload))
    assert result["line_items"][0]["amount"] is None
    assert (
        "gemini_transport_invalid_numeric:line_items.0.amount"
        in result["warnings"]
    )
    assert result["needs_manual_review"] is True


def test_compatibility_missing_required_field_fails_closed():
    payload = _payload()
    payload.pop("line_items")
    with pytest.raises(GeminiTransportSchemaError) as caught:
        _parse(json.dumps(payload))
    assert caught.value.diagnostic["missing_required_field_count"] == 1


@pytest.mark.parametrize("text", [
    "not-json",
    '{"vendor_name":"synthetic"',
    json.dumps(_payload()) + "\n" + json.dumps(_payload()),
])
def test_compatibility_malformed_truncated_and_multiple_objects_fail_closed(text):
    with pytest.raises(GeminiTransportJSONError):
        _parse(text)


def test_compatibility_output_token_exhaustion_fails_even_if_json_is_complete():
    with pytest.raises(GeminiTransportJSONError) as caught:
        _parse(
            json.dumps(_payload()),
            finish_reason="length",
            prompt_token_count=100,
            output_token_count=200,
            output_token_limit_reached=True,
        )
    assert caught.value.diagnostic["output_token_limit_reached"] is True


def test_transport_schema_contains_no_accounting_or_authorization_fields():
    encoded = json.dumps(gemini_response_format(), sort_keys=True).casefold()
    for forbidden in (
        "selected_gl", "final_gl", "gl_account", "export_allowed", "readiness",
        "governed_rule", "human_correction", "holdout", "tenant_id",
    ):
        assert forbidden not in encoded


def test_safe_diagnostic_never_contains_private_response_values(tmp_path, monkeypatch):
    private_values = [
        "PRIVATE-VENDOR-ALPHA",
        "999 Private Street",
        "account-998877",
        "$12345.67",
        "C:\\Users\\Private\\invoice.pdf",
        "secret-api-token",
    ]
    raw = json.dumps({
        "vendor_name": private_values[0],
        "service_address": private_values[1],
        "account_number": private_values[2],
        "amount": private_values[3],
        "local_path": private_values[4],
        "secret-api-token": private_values[5],
    })
    parsed = json.loads(raw)
    diagnostic = build_safe_diagnostic(
        raw,
        provider="gemini",
        model="configured-test-model",
        request_profile="private-safe-test",
        parsed=parsed,
        parser_error_type="SchemaValidationError",
        schema_validation_error_path="line_items",
    )
    monkeypatch.setattr(settings, "BATCHES_ROOT", tmp_path)
    with ai_runtime_trace.operation(
        batch_id="batch_privacy_test", stage="facts_only",
        provider="gemini", model="configured-test-model",
        profile_id="private-safe-test",
    ):
        ai_runtime_trace.record_structured_response_failure(diagnostic)
    serialized_diagnostic = json.dumps(diagnostic, sort_keys=True)
    trace = (
        tmp_path / "batch_privacy_test" / "audit" / "ai_request_trace.jsonl"
    ).read_text(encoding="utf-8")
    for private in private_values:
        assert private not in serialized_diagnostic
        assert private not in trace
    assert diagnostic["response_sha256"]
    assert diagnostic["unknown_field_count"] == 3


def test_structured_failure_matrix_is_typed_and_private_safe():
    private_marker = "PRIVATE-SYNTHETIC-VALUE-MUST-NOT-PERSIST"
    valid = _payload()

    malformed_cases = (
        (f"not-json-{private_marker}", "GeminiTransportJSONError"),
        (json.dumps(valid)[:-1], "GeminiTransportJSONError"),
        (json.dumps(valid) + "\n" + json.dumps(valid), "GeminiTransportJSONError"),
    )
    diagnostics: list[dict] = []
    for raw, expected_type in malformed_cases:
        with pytest.raises(GeminiTransportJSONError) as caught:
            _parse(raw)
        assert type(caught.value).__name__ == expected_type
        diagnostics.append(caught.value.diagnostic)

    missing = dict(valid)
    missing.pop("line_items")
    with pytest.raises(GeminiTransportSchemaError) as caught:
        _parse(json.dumps(missing))
    diagnostics.append(caught.value.diagnostic)
    assert "line_items" in caught.value.diagnostic["missing_required_field_names"]

    wrong_type = dict(valid)
    wrong_type["line_items"] = {"private": private_marker}
    with pytest.raises(GeminiTransportSchemaError) as caught:
        _parse(json.dumps(wrong_type))
    diagnostics.append(caught.value.diagnostic)
    assert caught.value.diagnostic["schema_validation_error_path"] == "line_items"

    extra = {**valid, f"private-{private_marker}": private_marker}
    extra_diagnostic = build_safe_diagnostic(
        json.dumps(extra),
        provider="gemini",
        model="configured-test-model",
        request_profile="private-safe-test",
        parsed=extra,
        parser_error_type="UnexpectedField",
    )
    diagnostics.append(extra_diagnostic)
    assert extra_diagnostic["unknown_field_count"] == 1
    assert len(extra_diagnostic["unexpected_field_name_hashes"]) == 1

    serialized = json.dumps(diagnostics, sort_keys=True)
    assert private_marker not in serialized
    for diagnostic in diagnostics:
        assert diagnostic["response_sha256"]
        assert isinstance(diagnostic["json_object_boundary_count"], int)
        assert isinstance(diagnostic["json_array_boundary_count"], int)


def test_source_observations_remain_separate_from_generated_and_accounting_fields():
    result = _parse(json.dumps(_payload()))
    line = result["line_items"][0]
    assert line["raw_description"] == "Synthetic line"
    assert line["normalized_description"] is None
    assert line["generated_description"] is None
    assert "gl_account_candidate" not in line
    assert line["evidence"][0]["text"] == "Synthetic line"
    strict_payload = ai_provider._validate_invoice_schema(result)
    assert ai_provider._validate_visual_line_structure(
        strict_payload, require_generated_description=False,
    )["line_items"]
    with pytest.raises(ai_provider.AIProviderInvalidSchema):
        ai_provider._validate_visual_line_structure(strict_payload)


def test_transport_evidence_validates_through_strict_document_facts_contract():
    result = _parse(json.dumps(_payload()))
    line = result["line_items"][0]
    row = {
        "Invoice Number": "SYN-1",
        "Invoice Date": "2026-07-19",
        "Due Date": "2026-07-19",
        "Vendor": "Synthetic Vendor",
        "Invoice Description": "Synthetic service",
        "Line Item Description": "Synthetic line",
        "Line Item Number": 1,
        "Property Abbreviation": "",
        "Location": "",
        "GL Account": "",
        "Amount": Decimal("25.00"),
        "Quantity": Decimal("2"),
        "Unit Price": Decimal("12.50"),
        "Tax": False,
        "Expense Type": "General",
        "Is Replacement Reserve": False,
        "_meta": {
            "source_page": 1,
            "source_line_description": "Synthetic line",
            "ai_transport_evidence": line["evidence"],
            "ai_document_transport_evidence": result["evidence"],
        },
    }
    capture_source_fields(row, document_id="synthetic-doc", line_item_id="line-1")
    decide_row(
        row,
        document_id="synthetic-doc",
        line_item_id="line-1",
        extraction_route="gemini_facts_transport",
    )
    facts = row["_meta"]["document_facts"]
    evidence = facts["line_items"][0]["evidence"]
    assert any(item["extraction_method"] == "gemini_facts_transport" for item in evidence)
    assert row["GL Account"] in {"", None} or row["_meta"]["accounting_decision"]["selected_gl_code"]
