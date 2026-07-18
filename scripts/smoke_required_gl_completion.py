"""Ensure supplier invoices never retain silent blank required GL fields."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from webapp.backend.services import ai_invoice_processor as processor


def main() -> None:
    descriptions = [
        "30x80 6 Panel Interior Left Hand Split Jamb",
        "Universal 6 Chrome Drip Bowl 6/Pkg",
        "Urethane Wax Ring and Bolts",
        "Universal 8 Chrome Drip Bowl 6/Pkg",
        "13 17W Dimmable Integrated LED Flush Mount 5CCT",
        "1 in. Vinyl 39inx64in Room Darkening Blind White",
    ]
    expected = ["7520", "6606", "6675", "6606", "6660", "6710"]
    payload = {
        "vendor_name": "HD Supply Facilities Maintenance, Ltd.",
        "invoice_number": "9250283339",
        "invoice_date": "06/12/2026",
        "due_date": "07/12/2026",
        "bill_or_credit": "Bill",
        "property_candidate": "Blue Country Apartments",
        "property_abbreviation": "BCA",
        "service_address": "254 Lombardy St, #24, Richmond, KY 40475",
        "location_candidate": "",
        "invoice_description": "Maintenance supplies",
        "line_items": [
            {
                "description": description,
                "amount": 10.0,
                "quantity": 1,
                "unit_price": 10.0,
                "gl_account_candidate": "",
                "confidence": 0.95,
            }
            for description in descriptions
        ],
        "subtotal": 60.0,
        "tax_amount": 0.0,
        "shipping_amount": 0.0,
        "fees_amount": 0.0,
        "total_amount": 60.0,
        "confidence": 0.95,
        "warnings": [],
        "_document_text": "HD Supply invoice for Blue Country Apartments",
    }
    normalized = processor.validate_ai_extraction(payload)
    actual = [item.get("gl_account_candidate") for item in normalized["line_items"]]
    assert actual == expected, (actual, expected)
    assert "gl_mapping_required" not in normalized["manual_review_codes"]
    assert "required_gl_account" not in normalized["manual_review_codes"]
    assert all(item.get("gl_resolution_explanation") for item in normalized["line_items"])

    ocr_abbreviations = {
        "30x80 6 Pnl Int Ph Lft Hnd Spilt Jamb": "7520",
        "1 in. Vinyl 39inx64in Rm Drknng Blnd Wht": "6710",
    }
    for description, expected_gl in ocr_abbreviations.items():
        account = processor._suggest_valid_gl_candidate(
            description=description,
            vendor_name="HD Supply Facilities Maintenance, Ltd.",
            ai_suggested_gl="",
        )
        assert account and account["gl_code"] == expected_gl

    abbreviation, location, _ = processor._resolve_property_context(
        property_abbreviation="BCA",
        property_candidate="Blue Country Apartments",
        service_address="254 Lombardy St, #24, Richmond, KY 40475",
        location_candidate="#24",
        properties=[{
            "Property Name": "Blue Country Apartments",
            "Property Abbreviation": "BCA",
            "Unit": "254-24",
            "Address": "254-24 Lombardy Street",
        }],
    )
    assert abbreviation == "BCA"
    assert location == "254-24"

    fallback = processor._suggest_valid_gl_candidate(
        description="Unrecognized Maintenance Component",
        vendor_name="HD Supply Facilities Maintenance, Ltd.",
        ai_suggested_gl="",
    )
    assert fallback and fallback["gl_code"] == "6669"
    print("required GL completion smoke: PASS")


if __name__ == "__main__":
    main()
