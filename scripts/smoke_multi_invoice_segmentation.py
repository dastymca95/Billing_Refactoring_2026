"""Regression coverage for page-aligned OCR and multi-invoice PDFs."""

from __future__ import annotations

import tempfile
import sys
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfWriter

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from webapp.backend.services import ai_invoice_processor as processor
from webapp.backend.services import canonical_rules
from webapp.backend.services import document_ingestion


def _candidate(texts: list[str]) -> document_ingestion.DocumentCandidate:
    pages = [
        document_ingestion.PageCandidate(page_number=index, text=text)
        for index, text in enumerate(texts, start=1)
    ]
    return document_ingestion.DocumentCandidate(
        source_file="multi.pdf",
        source_type="pdf_scanned",
        page_count=len(pages),
        document_text="\n\n".join(texts),
        pages=pages,
    )


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = Path(tmp) / "scanned.pdf"
        writer = PdfWriter()
        for _ in range(3):
            writer.add_blank_page(width=612, height=792)
        with pdf_path.open("wb") as handle:
            writer.write(handle)
        expected = ["INVOICE NO. 101", "continuation detail", "INVOICE NO. 202"]
        with patch.object(document_ingestion, "_ocr_pdf_pages", return_value=expected):
            ingested = document_ingestion.ingest_document(pdf_path, max_pages=3)
        assert [page.text for page in ingested.pages] == expected

    grouped = processor._segment_document_invoice_groups(_candidate([
        "Vendor INVOICE NO. 101 DATE 01/01/2026",
        "Page 2 of 2 additional line detail",
        "Vendor INVOICE NO. 202 DATE 01/02/2026",
    ]))
    assert [group["page_numbers"] for group in grouped] == [[1, 2], [3]]
    assert [group["invoice_identity"] for group in grouped] == ["101", "202"]

    ordinary = processor._segment_document_invoice_groups(_candidate([
        "Vendor INVOICE NO. 303 DATE 01/03/2026",
        "Vendor INVOICE NO. 303 DATE 01/03/2026 Page 2",
    ]))
    assert len(ordinary) == 1

    properties = [{
        "Property Name": "The Raintree Apartments",
        "Property Abbreviation": "TRA-Rain",
        "Unit": "2304C",
        "Address": "2318 Rain Tree Drive",
    }]
    abbreviation, location, _ = processor._resolve_property_context(
        property_abbreviation="",
        property_candidate="Rain Tree Apartments",
        service_address="2318 Rain Tree Drive, Birmingham, AL 35215",
        location_candidate="2304-C",
        properties=properties,
    )
    assert abbreviation == "TRA-Rain"
    assert location == "2304C"

    invoice = processor.ai_result_to_invoice(
        {
            "invoice_number": "101",
            "bill_or_credit": "Bill",
            "invoice_date": "01/01/2026",
            "vendor_name": "Test Vendor",
            "invoice_description": "HVAC service",
            "canonical_invoice_description": "HVAC service",
            "property_abbreviation": "TRA-Rain",
            "location": "2304C",
            "due_date": "01/31/2026",
            "line_items": [{
                "description": "HVAC service",
                "canonical_line_item_description": "HVAC service",
                "amount": 85,
                "gl_account_candidate": "6555",
                "expense_type": "General",
                "confidence": 0.99,
            }],
            "total_amount": 85,
            "confidence": 0.99,
            "manual_review_reasons": [],
            "manual_review_codes": [],
        },
        batch_id="test",
        source_file="multi.pdf",
        source_page=3,
        vendor_key="unknown",
        support_document_url="https://example.invalid/invoice.pdf",
    )
    assert invoice["source_page"] == 3
    assert invoice["rows"][0]["_meta"]["source_page"] == 3

    replacement = canonical_rules._semantic_expense_gl(
        {
            "vendor_name": "KT Heating & Cooling",
            "invoice_nature": "one_time",
            "invoice_description": "Complete HVAC System Replacement",
            "_document_text": "Remove old air handler and A/C condensing unit and replace with new",
        },
        {"description": "New disconnect, whip and thermostat"},
        "other_infrequent",
    )
    repair = canonical_rules._semantic_expense_gl(
        {
            "vendor_name": "KT Heating & Cooling",
            "invoice_nature": "one_time",
            "invoice_description": "A/C service call",
            "_document_text": "No refrigerant due to cracked copper line",
        },
        {"description": "HVAC repair and diagnostic"},
        "other_infrequent",
    )
    assert replacement and replacement[0] == "7544"
    assert repair and repair[0] == "6555"
    print("multi-invoice segmentation smoke: PASS")


if __name__ == "__main__":
    main()
