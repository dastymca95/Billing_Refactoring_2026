"""Regression checks for scanned, previously unseen supplier invoices.

This is intentionally provider-free: it validates the accounting gate and
post-extraction normalization without calling AI, Dropbox, or modifying data.
"""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp.backend.services import ai_invoice_processor as processor
from webapp.backend.services import document_ingestion
from webapp.backend.services import vendor_detection


OCR_TEXT = """APPLIANCE PARTS TODAY
INVOICE NUMBER ACCOUNT NUMBER SALES # LOCATION
108516 2708868105 1 W
SOLD TO:
KATIE NEWCOMB NEXT GEN ASPEN M
705 B RED RIVER ST
CLARKSVILLE TN 37040
SHIP TO:
INFO@ASPENMEADOWKY.COM
INVOICE DATE 05/01/2026
QTY MAKE PRODUCT DESCRIPTION PRICE EXTENSION
1 WCI 240364503 PAN 121.01 121.01
SUBTOTAL 121.01
TAX 11.50
TOTAL 132.51
"""


def _payload() -> dict:
    return {
        "vendor_name": "Appliance Parts Today",
        "invoice_number": "108516",
        "account_number": "2708868105",
        "invoice_date": "05/01/2026",
        "payment_terms": "Net 30",
        "bill_or_credit": "Bill",
        "service_address": "705 B RED RIVER ST, CLARKSVILLE TN 37040",
        "address_role": "sold_to",
        # Deliberately conflicting provider guess. Document identity must win.
        "property_candidate": "The Element Clarksville",
        "property_abbreviation": "TEC",
        "location_candidate": "W",
        "line_items": [
            {
                "description": "WCI 240364503 Pan",
                "quantity": 1,
                "unit_price": 121.01,
                "amount": 121.01,
                "gl_account_candidate": "6606",
                "confidence": 0.96,
                "reason": "Visible product line",
            }
        ],
        "subtotal": 121.01,
        "tax_amount": 11.50,
        "shipping_amount": 0,
        "fees_amount": 0,
        "total_amount": 132.51,
        "tax_handling": "distribute_proportionally",
        "confidence": 0.96,
        "warnings": [],
        "_document_text": OCR_TEXT,
        "_source_file": "unknown-scan.pdf",
    }


def main() -> None:
    routed = vendor_detection.detect_vendor_from_text(
        Path("UtilityBill.pdf"),
        "Hopkinsville Water Environment Authority hwea-ky.com account total",
    )
    assert routed and routed["vendor_key"] == "hopkinsville_water_environment_authority", routed

    incomplete_ocr = OCR_TEXT.replace("108516 2708868105 1 W\n", "").replace(
        "SUBTOTAL 121.01\nTAX 11.50\nTOTAL 132.51\n",
        "",
    )
    evidence = document_ingestion._invoice_field_evidence(incomplete_ocr)
    assert "invoice_number" in evidence["missing"], evidence
    assert "total_amount" in evidence["missing"], evidence
    assert evidence["score"] < 0.72, evidence
    assert document_ingestion._text_quality_score(incomplete_ocr) <= 0.54

    assert processor._is_invoice_number_placeholder("ACCOUNT")
    assert processor._is_invoice_number_placeholder("Invoice Number")
    assert not processor._is_invoice_number_placeholder("108516")

    references = processor.load_references()
    normalized = processor.validate_ai_extraction(_payload(), references=references)
    assert normalized["vendor_name"] == "Appliance Parts Today", normalized
    assert normalized["invoice_number"] == "108516", normalized
    assert normalized["account_number"] == "2708868105", normalized
    assert normalized["invoice_date"] == "05/01/2026", normalized
    assert normalized["due_date"] == "05/31/2026", normalized
    assert normalized["total_amount"] == 132.51, normalized
    assert normalized["property_abbreviation"] == "AMA", normalized
    assert normalized["location"] == "", normalized
    assert normalized["unit_number"] == "", normalized
    assert normalized["service_address"] == "", normalized
    assert normalized["billing_address"].startswith("705 B RED RIVER"), normalized
    assert normalized["address_role"] == "sold_to", normalized
    assert "location_unresolved" not in normalized["manual_review_codes"], normalized
    assert normalized["line_items"][0]["gl_account_candidate"] == "6606", normalized

    invoice = processor.ai_result_to_invoice(
        normalized,
        batch_id="smoke",
        source_file="unknown-scan.pdf",
        vendor_key="unknown",
        support_document_url="https://example.invalid/unknown-scan.pdf",
    )
    row = invoice["rows"][0]
    assert row["Amount"] == 132.51, row
    assert row["Unit Price"] == 132.51, row
    assert row["Property Abbreviation"] == "AMA", row
    assert row["GL Account"] == "6606", row

    # A strongly localized visual candidate must correct a contradictory
    # top-level value before validation.
    reconciled = processor._reconcile_high_confidence_vision_candidates(
        {
            "total_amount": 121.01,
            "tax_amount": 0,
            "vision_candidates": [
                {"field_key": "total_amount", "value": "132.51", "confidence": 0.97},
                {"field_key": "tax_amount", "value": "11.50", "confidence": 0.96},
            ],
        }
    )
    assert reconciled["total_amount"] == 132.51, reconciled
    assert reconciled["tax_amount"] == 11.50, reconciled

    # New vendors remain usable while still requiring an operator mapping.
    new_vendor = _payload()
    new_vendor["vendor_name"] = "One-Time Local Repair LLC"
    retained = processor.validate_ai_extraction(new_vendor, references=references)
    assert retained["vendor_name"] == "One-Time Local Repair LLC", retained
    assert "vendor_mapping_required" in retained["manual_review_codes"], retained

    print("PASS: unknown scanned invoice quality gate and accounting normalization")


if __name__ == "__main__":
    main()
