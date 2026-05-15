"""Phase AI-2 smoke tests for AI vendor / GL mapping review.

No external AI provider is called. The learned mapping store is redirected to a
temporary YAML file for the duration of the script.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from webapp.backend.main import app  # noqa: E402
from webapp.backend.services import ai_mapping_review, batch_store  # noqa: E402
from webapp.backend.services.ai_invoice_processor import (  # noqa: E402
    ai_result_to_invoice,
    validate_ai_extraction,
)


client = TestClient(app)


def sample_payload() -> dict:
    return {
        "vendor_name": "Lowe's",
        "invoice_number": "83690",
        "invoice_date": "05/09/2026",
        "due_date": "06/08/2026",
        "bill_or_credit": "Bill",
        "account_number": "201078",
        "service_address": "850 Professional Park Dr, Clarksville, TN 37040",
        "property_candidate": "The Oakley at Pro Park",
        "property_abbreviation": "1732-HMA",
        "invoice_description": "Hardware supplies",
        "line_items": [
            {
                "description": "3-3/4-In (96mm) bar pull",
                "quantity": 1,
                "unit_price": 6.16,
                "amount": 6.16,
                "gl_account_candidate": "HARDWARE",
                "expense_type": "General",
                "is_replacement_reserve": False,
                "confidence": 0.92,
                "reason": "Line text visible",
            }
        ],
        "subtotal": 6.16,
        "tax_amount": 0.59,
        "shipping_amount": 0,
        "fees_amount": 0,
        "total_amount": 6.75,
        "confidence": 0.9,
        "warnings": [],
        "needs_manual_review": True,
    }


def write_fake_result(batch_id: str) -> None:
    processed = batch_store.get_processed_dir(batch_id)
    processed.mkdir(parents=True, exist_ok=True)
    row1 = {
        "Invoice Number": "83690",
        "Vendor": "Lowe's",
        "Property Abbreviation": "",
        "Location": "",
        "Line Item Number": 1,
        "Line Item Description": "3-3/4-In (96mm) bar pull",
        "GL Account": "HARDWARE",
        "Amount": 6.16,
        "_meta": {
            "source_file": "lowes.pdf",
            "ai_generated": True,
            "manual_review_reasons": [
                "Vendor 'Lowe's' was extracted but is not confirmed in the ResMan Vendor List. Confirm the vendor mapping.",
                "One or more line items have missing or invalid ResMan GL account codes. Confirm GL mapping.",
                "Property is not confirmed. Resolve property before export.",
                "Location is unresolved. Select a known unit/location or explicitly leave it blank.",
                "Tax handling requires review before export.",
            ],
            "ai_validation_flags": [
                "vendor_mapping_required",
                "property_mapping_required",
                "location_unresolved",
                "gl_mapping_required",
                "tax_handling_requires_review",
            ],
            "ai_service_address": "850 Professional Park Dr, Clarksville, TN 37040",
            "ai_provenance": {
                "invoice_total": 6.75,
                "tax_amount": 0.59,
                "subtotal": 6.16,
            },
        },
    }
    row2 = {
        "Invoice Number": "83690",
        "Vendor": "Lowe's",
        "Property Abbreviation": "",
        "Location": "",
        "Line Item Number": 2,
        "Line Item Description": "Sales tax",
        "GL Account": "",
        "Amount": 0.59,
        "_meta": {
            "source_file": "lowes.pdf",
            "ai_generated": True,
            "manual_review_reasons": [
                "Vendor 'Lowe's' was extracted but is not confirmed in the ResMan Vendor List. Confirm the vendor mapping.",
                "Property is not confirmed. Resolve property before export.",
                "Location is unresolved. Select a known unit/location or explicitly leave it blank.",
                "Tax handling requires review before export.",
            ],
            "ai_validation_flags": [
                "vendor_mapping_required",
                "property_mapping_required",
                "location_unresolved",
                "tax_handling_requires_review",
            ],
            "ai_service_address": "850 Professional Park Dr, Clarksville, TN 37040",
            "ai_provenance": {
                "invoice_total": 6.75,
                "tax_amount": 0.59,
                "subtotal": 6.16,
            },
        },
    }
    result = {
        "batch_id": batch_id,
        "summary": {
            "files_total": 1,
            "files_supported": 1,
            "files_unsupported": 0,
            "invoices_total": 1,
            "manual_review_total": 1,
        },
        "by_vendor": {
            "ai_assisted": {
                "summary": {"processing_mode": "ai_assisted"},
                "invoices": [
                    {
                        "source_file": "lowes.pdf",
                        "invoice_number": "83690",
                        "rows": [dict(row1), dict(row2)],
                    }
                ],
            }
        },
        "all_invoices": [
            {
                "source_file": "lowes.pdf",
                "invoice_number": "83690",
                "rows": [row1, row2],
            }
        ],
        "all_manual_review": [
            {
                "source_file": "lowes.pdf",
                "vendor": "Lowe's",
                "reasons": [
                    "Vendor 'Lowe's' was extracted but is not confirmed in the ResMan Vendor List. Confirm the vendor mapping.",
                    "One or more line items have missing or invalid ResMan GL account codes. Confirm GL mapping.",
                    "Property is not confirmed. Resolve property before export.",
                    "Location is unresolved. Select a known unit/location or explicitly leave it blank.",
                    "Tax handling requires review before export.",
                ],
                "reason_codes": [
                    "vendor_mapping_required",
                    "property_mapping_required",
                    "location_unresolved",
                    "gl_mapping_required",
                    "tax_handling_requires_review",
                ],
            }
        ],
        "unsupported_files": [],
    }
    (processed / "_webapp_result.json").write_text(
        json.dumps(result, indent=2),
        encoding="utf-8",
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        ai_mapping_review.LEARNED_MAPPINGS_PATH = Path(td) / "ai_learned_mappings.yaml"

        vendors = ai_mapping_review.vendor_candidates("Lowe's Pro Supply")
        assert vendors["candidates"], vendors
        assert vendors["candidates"][0]["vendor_name"] == "Lowes Pro Supply", vendors
        print("Vendor candidate generation: OK")

        learned_vendor = ai_mapping_review.save_vendor_mapping(
            detected_vendor="Lowes Receipt Alias",
            resman_vendor_name="Lowes Pro Supply",
        )
        assert learned_vendor["resman_vendor_name"] == "Lowes Pro Supply"
        vendors_after = ai_mapping_review.vendor_candidates("Lowes Receipt Alias")
        assert vendors_after["candidates"][0]["learned"] is True, vendors_after
        print("Accept vendor mapping + future candidate boost: OK")

        gl = ai_mapping_review.gl_candidates(
            line_item_description="3-3/4-In (96mm) bar pull",
            vendor_name="Lowes Pro Supply",
            ai_suggested_gl="HARDWARE",
        )
        assert any(c["gl_code"] == "6651" for c in gl["candidates"]), gl
        print("GL candidate generation: OK")

        learned_gl = ai_mapping_review.save_gl_mapping(
            vendor_name="Lowes Pro Supply",
            pattern="bar pull",
            gl_account="6651",
        )
        assert learned_gl["gl_code"] == "6651"
        gl_after = ai_mapping_review.gl_candidates(
            line_item_description="3-3/4-In (96mm) bar pull",
            vendor_name="Lowes Pro Supply",
            ai_suggested_gl="",
        )
        assert gl_after["candidates"][0]["learned"] is True, gl_after
        print("Accept GL mapping + future learned match: OK")

        properties = ai_mapping_review.property_candidates(
            service_address="850 Professional Park Dr, Clarksville, TN 37040",
            limit=3,
        )
        assert properties["candidates"], properties
        assert properties["candidates"][0]["property_abbreviation"] == "OG-PPA", properties
        locations = ai_mapping_review.location_candidates(
            property_abbreviation="OG-PPA",
            query="A-101",
        )
        assert any(c["location"] == "A-101" for c in locations["locations"]), locations
        assert ai_mapping_review.validate_property_location(
            property_abbreviation="OG-PPA",
            location="A-101",
        ), locations
        assert not ai_mapping_review.validate_property_location(
            property_abbreviation="OG-PPA",
            location="850 Professional Park Dr",
        )
        print("Property/location candidate validation: OK")

        try:
            ai_mapping_review.save_gl_mapping(
                vendor_name="Lowes Pro Supply",
                pattern="bar pull",
                gl_account="NOT_A_GL",
            )
        except ValueError:
            print("Invalid GL rejected: OK")
        else:
            raise AssertionError("Invalid GL was accepted")

        learned_payload = sample_payload()
        learned_payload["vendor_name"] = "Lowes Receipt Alias"
        normalized = validate_ai_extraction(learned_payload)
        # Current validation can already see user-confirmed vendor aliases;
        # older builds required a second apply pass. Accept either path, but
        # require the final normalized invoice to use the learned vendor/GL.
        pre_codes = set(normalized["manual_review_codes"])
        normalized = ai_mapping_review.apply_learned_mappings_to_normalized(normalized)
        assert normalized["vendor_name"] == "Lowes Pro Supply", normalized
        assert "vendor_mapping_required" not in normalized["manual_review_codes"]
        assert normalized["line_items"][0]["gl_account_candidate"] == "6651"
        assert (
            "vendor_mapping_required" in pre_codes
            or normalized.get("mapping_provenance")
        ), normalized
        print("Future AI result applies learned vendor/GL mappings: OK")

        hardening_payload = sample_payload()
        hardening_payload.update({
            "vendor_name": "Not A Real Vendor",
            "property_candidate": "Imaginary Apartments",
            "property_abbreviation": "FAKE-PROP",
            "service_address": "123 Fake Street, Nowhere, TN 37000",
            "invoice_date": "",
            "purchase_date": "05/06/2026",
            "tax_amount": 0.59,
            "total_amount": 6.75,
            "line_items": [
                {
                    "description": "3-3/4-In (96mm) bar pull",
                    "quantity": 1,
                    "unit_price": 6.16,
                    "amount": 6.16,
                    "gl_account_candidate": "MISCELLANEOUS",
                    "confidence": 0.91,
                    "reason": "Visible line item.",
                },
                {
                    "description": "Promotional discount app",
                    "quantity": 1,
                    "unit_price": 0,
                    "amount": 0,
                    "gl_account_candidate": "",
                    "confidence": 0.85,
                    "reason": "Zero-dollar source line.",
                },
            ],
        })
        hardened = validate_ai_extraction(hardening_payload)
        hardened_inv = ai_result_to_invoice(
            hardened,
            batch_id="batch_20990101_000000_000",
            source_file="lowes.pdf",
            vendor_key="unknown",
        )
        row = hardened_inv["rows"][0]
        codes = set(hardened["manual_review_codes"])
        assert "vendor_mapping_required" in codes, codes
        assert "property_mapping_required" in codes, codes
        assert "location_unresolved" in codes, codes
        if "gl_mapping_required" not in codes:
            assert str(row["GL Account"]).isdigit(), row
        assert "zero_amount_line_excluded" in codes, codes
        assert "invoice_date_inferred_from_purchase_date" in codes, codes
        assert row["GL Account"] != "MISCELLANEOUS", row
        assert row["Location"] == "", row
        assert len(hardened_inv["rows"]) == 1, hardened_inv
        assert row["Invoice Description"] != "Hardware and miscellaneous items", row
        print("AI variable invoice validation hardening: OK")

        created = client.post(
            "/api/batches",
            json={"batch_name": "QA AI mapping smoke", "document_mode": "digital_pdf"},
        )
        assert created.status_code == 200, created.text
        batch_id = created.json()["batch_id"]
        write_fake_result(batch_id)

        vendor_apply = client.post(
            f"/api/batches/{batch_id}/ai-review/vendor-mapping",
            json={
                "detected_vendor": "Lowe's",
                "selected_vendor_name": "Lowes Pro Supply",
                "row_index": 0,
                "save_for_future": True,
                "apply_scope": "current_invoice",
            },
        )
        assert vendor_apply.status_code == 200, vendor_apply.text
        assert vendor_apply.json()["applied_rows"] >= 2, vendor_apply.text
        print("Apply vendor mapping endpoint: OK")

        gl_apply = client.post(
            f"/api/batches/{batch_id}/ai-review/gl-mapping",
            json={
                "row_index": 0,
                "gl_account": "6651",
                "save_for_future": True,
                "apply_to_similar": False,
            },
        )
        assert gl_apply.status_code == 200, gl_apply.text
        assert gl_apply.json()["applied_rows"] >= 1, gl_apply.text
        print("Apply GL mapping endpoint: OK")

        property_apply = client.post(
            f"/api/batches/{batch_id}/ai-review/property-location",
            json={
                "row_index": 0,
                "property_abbreviation": "OG-PPA",
                "location": "A-101",
                "service_address": "850 Professional Park Dr, Clarksville, TN 37040",
                "save_for_future": True,
                "apply_scope": "current_invoice",
            },
        )
        assert property_apply.status_code == 200, property_apply.text
        assert property_apply.json()["applied_rows"] >= 2, property_apply.text
        print("Apply property/location endpoint: OK")

        invalid_location = client.post(
            f"/api/batches/{batch_id}/ai-review/property-location",
            json={
                "row_index": 0,
                "property_abbreviation": "OG-PPA",
                "location": "850 Professional Park Dr",
            },
        )
        assert invalid_location.status_code == 400, invalid_location.text
        print("Raw address rejected as Location: OK")

        tax_apply = client.post(
            f"/api/batches/{batch_id}/ai-review/tax-policy",
            json={"row_index": 0, "policy": "distribute_proportionally"},
        )
        assert tax_apply.status_code == 200, tax_apply.text
        assert tax_apply.json()["applied_rows"] >= 2, tax_apply.text
        print("Apply tax policy endpoint: OK")

        status = client.get("/api/ai/status").json()
        assert "AI_API_KEY" not in json.dumps(status), status
        print("No API key exposed in AI status: OK")

        cleanup = client.delete(f"/api/batches/{batch_id}")
        assert cleanup.status_code in {200, 404}, cleanup.text
        print("QA mapping smoke batch cleanup: OK")

    print("Phase AI-2/AI-4 mapping review and validation smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
