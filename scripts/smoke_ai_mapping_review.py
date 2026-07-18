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
from webapp.backend.services import ai_invoice_processor, ai_mapping_review, batch_store  # noqa: E402
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

        lowes_layout_text = """
        6910 Brasada Drive
        Houston, TX 77085
        RETURN SERVICE REQUESTED
        BILL TO:
        INVOICE
        Bill To #
        Order #
        Invoice Date
        Due Date
        PO #
        Reference
        202617
        20954736-00
        06/05/26
        07/05/26
        9811
        Remit To:
        P.O. Box 301451
        Dallas, TX 75303-1451
        SHIP TO:
        The Villas of Pine Valley
        1200 Pine Valley Dr
        Elizabethtown, KY 42701-8671
        Ship Point LPS-Indianapolis-4506 Via LINE HAUL LU0/2 / Ship Date 06/05/26 Terms Net 30
        Ln# Bin Loc. Product
        Description
        Quantity Ordered Qty. U/M Quantity Shipped Unit Price (Net) Extended Amount
        6 / A/ 04/4 427375 6 Pack 6 10.00 60.00
        LED 60W A19 CL FIL 5K
        CLEAR FILAMENT
        GL CODE:Light Bulbs
        8 / B/ 14/3 821270 5 Pack 5 6.00 30.00
        PRO EDGE MICROFIBER 3/8"
        X 4" ROLLER COVER 2/PK
        GL CODE:Paint
        7 / F/ 21/3 670857 4 Each 4 15.00 60.00
        GRAZ TOWEL RING
        BRUSHED NICKEL
        Customer Copy Page 1 of 2
        Lines Total Qty Shipped Total 15 Total 150.00
        Taxes 9.00
        Invoice Total 159.00
        Description Total Merchandise
        Hardware 60.00
        Light Bulbs 60.00
        Paint 30.00
        """
        lowes_raw = ai_invoice_processor._repair_ai_payload_from_ocr(
            {
                "vendor_name": "",
                "invoice_number": "",
                "invoice_date": "",
                "due_date": "",
                "bill_or_credit": "Bill",
                "line_items": [],
                "total_amount": 0,
                "warnings": [],
            },
            lowes_layout_text,
            source_file="lowes_layout_without_logo_text.pdf",
        )
        lowes_normalized = validate_ai_extraction(lowes_raw)
        lowes_invoice = ai_result_to_invoice(
            lowes_normalized,
            batch_id="batch_20990101_000000_000",
            source_file="lowes_layout_without_logo_text.pdf",
            vendor_key="lowes",
            support_document_url="https://dropbox.example/lowes-layout.pdf",
        )
        lowes_rows = lowes_invoice["rows"]
        assert lowes_normalized["vendor_name"] == "Lowes Pro Supply", lowes_normalized
        assert lowes_normalized["category"] == "other_infrequent", lowes_normalized
        assert lowes_normalized["invoice_number"] == "20954736-00", lowes_normalized
        assert lowes_normalized["due_date"] == "07/05/2026", lowes_normalized
        assert lowes_normalized["blocking_required_fields"] == [], lowes_normalized
        assert [row["GL Account"] for row in lowes_rows] == ["6660", "6770", "6651"], lowes_rows
        assert round(sum(float(row["Amount"]) for row in lowes_rows), 2) == 159.00, lowes_rows
        assert "Lowes" not in lowes_rows[0]["Invoice Description"], lowes_rows[0]
        assert "06/05" not in lowes_rows[0]["Invoice Description"], lowes_rows[0]
        assert lowes_rows[1]["GL Account"] != "6139", lowes_rows[1]
        assert lowes_rows[2]["Line Item Description"] == "Graz Towel Ring Brushed Nickel", lowes_rows[2]
        print("Lowe's Pro Supply image-only vendor layout repair: OK")

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

        landscape_payload = {
            "vendor_name": "Landscape Services, Inc.",
            "invoice_number": "205843",
            "invoice_date": "05/01/2026",
            "due_date": "",
            "service_address": "300 Greenwood Drive\nClarksville, TN 37040",
            "property_candidate": "Penn Warren Apartments",
            "invoice_description": "300 Greenwood Dr Clarksville",
            "line_items": [
                {
                    "description": "Storm Damage Limb Removal and Disposal- 4/29",
                    "amount": 260.0,
                    "gl_account_candidate": "1300",
                    "confidence": 0.95,
                }
            ],
            "total_amount": 260.0,
            "confidence": 0.95,
            "warnings": ["image_ocr_cache_hit"],
        }
        landscape = validate_ai_extraction(landscape_payload)
        landscape_invoice = ai_result_to_invoice(
            landscape,
            batch_id="batch_20990101_000000_001",
            source_file="landscape.png",
            vendor_key="ai_assisted",
        )
        landscape_row = landscape_invoice["rows"][0]
        assert landscape["line_items"][0]["gl_account_candidate"] == "6810", landscape
        assert landscape["due_date"] == "05/31/2026", landscape
        assert landscape["service_period_start"] == "04/29/2026", landscape
        assert "required_due_date" not in landscape["manual_review_codes"], landscape
        assert "ai_warning_image_ocr_cache_hit" not in landscape["manual_review_codes"], landscape
        assert landscape_row["GL Account"] == "6810", landscape_row
        assert landscape_row["Invoice Description"].startswith("May-26 - "), landscape_row
        assert landscape_row["Line Item Description"].startswith("May-26 - "), landscape_row
        print("Landscape screenshot GL/date/period guard: OK")

        bels_ocr_text = """
To: Invoice # 1327
admiral place (charles) Invoice Date 05/05/2026
301 Ligon Drive, Shelbyville, Tennessee 37160
Payment Term Net 30
Shelbyville, TN 37160
Amount Due $1,150.00
Item Quantity Price Tax1 Tax2 Line Total
Mowing admiral place may 1.0 $1,150.00 $1,150.00
Subtotal: $1,150.00
Tax: $0.00
Past Due Amount: $0.00
Amount Due: $1,150.00
Notes
Thank You For Your Business!
"""
        bels_bad_ai_payload = {
            "vendor_name": "Nex-Gen Management LLC dba Magnolia Village Apartments",
            "invoice_number": "1328",
            "invoice_date": "05/05/2026",
            "due_date": "",
            "line_items": [
                {
                    "description": "Magnolia village apartments may",
                    "amount": 1150.0,
                    "gl_account_candidate": "6335",
                    "confidence": 0.75,
                }
            ],
            "subtotal": 1150.0,
            "tax_amount": 0.0,
            "total_amount": 1150.0,
            "confidence": 0.75,
            "warnings": [],
        }
        bels_repaired = ai_invoice_processor._repair_ai_payload_from_ocr(
            bels_bad_ai_payload,
            bels_ocr_text,
            source_file="bels_screenshot.pdf",
        )
        bels = validate_ai_extraction(bels_repaired)
        bels_invoice = ai_result_to_invoice(
            bels,
            batch_id="batch_20990101_000000_002",
            source_file="bels_screenshot.pdf",
            vendor_key="ai_assisted",
        )
        bels_row = bels_invoice["rows"][0]
        assert bels["vendor_name"] == "Bel's Landscaping", bels
        assert bels["invoice_number"] == "1327", bels
        assert bels["due_date"] == "06/04/2026", bels
        assert bels["property_abbreviation"] == "APA", bels
        assert bels_row["GL Account"] == "6810", bels_row
        assert bels_row["Invoice Description"].startswith("May-26 - "), bels_row
        assert bels_row["Line Item Description"].startswith("May-26 - "), bels_row
        print("Bel's Landscaping screenshot customer/vendor guard: OK")

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
