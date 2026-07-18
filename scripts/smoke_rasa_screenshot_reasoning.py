from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp.backend.services import ai_invoice_processor as processor  # noqa: E402
from webapp.backend.services.output_contract_validator import validate_row_contract  # noqa: E402


def _normalize(payload: dict) -> dict:
    payload = dict(payload)
    payload["_document_text"] = payload.pop("_test_text", "")
    payload["_source_file"] = "fixture.pdf"
    return processor.validate_ai_extraction(payload, references=processor.load_references())


def main() -> int:
    rasa_text = """
    Invoice
    Sold to: Install At:
    ELEMENT-CLARKSVILLE ELEMENT-CLARKSVILLE
    PDF INVOICE 2833-3 #B COBALT DR
    , APT 2833-1H
    CLARKSVILLE, TN 37040
    Invoice Date Invoice Number Order Date Install Date
    5/27/2026 VZ81193D 5/26/2026 5/27/2026
    Unit # / Model # Telephone PO Number
    2833-1H/2BD CPT (931) 534-8739 N/A
    METRO PLUS-12 OAK MIST 46.67 SY
    VINYL INSTALL 48.67 SY
    INVOICE TOTAL: $1,395.15
    BALANCE DUE: $1,395.15
    PLEASE REMIT TO: RASA FLOORS
    """
    rasa_raw = processor._extract_known_vendor_payload_from_ocr(rasa_text)
    rasa_raw["_test_text"] = rasa_text
    rasa = _normalize(rasa_raw)
    assert rasa["vendor_name"] == "Rasa Floors & Carpet Cleaning, LLC"
    assert rasa["property_abbreviation"] == "TEC"
    assert rasa["location"] == "2833-1 H"
    assert rasa["due_date"] == "06/26/2026"
    assert rasa["line_items"][0]["gl_account_candidate"] == "7536"
    assert rasa["manual_review_codes"] == []

    a1_text = """
    A-1 Heating and Air 160 Industrial Dr Clarksville, TN 37040
    Bill to Nex-Gen Management Ship to 162 JACK MILLER #607
    162 JACK MILLER #607 CLARKSVILLE, TN 37042
    Invoice #: i33687
    Work Order #: 44491 Transaction Date: 7/7/2026 Terms: Due on receipt
    Work Summary
    Unit was leaking due to stopped up drain cleared drain and vacuumed water from drain pan and unit is now working properly
    Subtotal: $220.66 Tax: $0.00 Total: $220.66 Payments: $0.00 Balance Due: $220.66
    """
    a1_raw = processor._extract_known_vendor_payload_from_ocr(a1_text)
    a1_raw["_test_text"] = a1_text
    a1 = _normalize(a1_raw)
    assert a1["invoice_number"] == "i33687"
    assert a1["property_abbreviation"] == "TRG1"
    assert a1["location"] == "607"
    assert a1["line_items"][0]["gl_account_candidate"] == "6555"

    cash_text = """
    CASH & CARRY BUILDING SUPPLY
    INVOICE # 17172 ACCOUNT # 5034 DATE 07-Jul-26
    MISC ITEMS | 6.00 0.99 EACH 5.94
    MISC ITEMS | 6.00 0.29 EACH 1.74
    MISC ITEMS | 6.00 0.39 EACH 2.34
    334026 12OZ 2X GLOSS BLACK 2.00 7.99 EACH 15.98
    PVC02112 1600HA 3/4X1/2 BUSH 1.00 1.99 EACH 1.99
    PVC02113 0600HA 1/2 MIP PLUG 1.00 1.99 EACH 1.99
    3.00% Service Fee 1.00 0.89 EACH 0.89
    3.00% Cash Discount -1.00 0.89 EACH -0.89
    SUBTOTAL $29.98 TAX $3.00 TOTAL $32.98
    NEX-GEN MANAGEMENT LLC 705 B Red River Street Clarksville, TN 37040
    """
    cash_raw = processor._extract_known_vendor_payload_from_ocr(cash_text)
    cash_raw["_test_text"] = cash_text
    cash = _normalize(cash_raw)
    assert cash["invoice_date"] == "07/07/2026"
    assert cash["due_date"] == "08/06/2026"
    assert cash["property_abbreviation"] == ""
    assert "property_inferred_from_vendor_history" not in cash["manual_review_codes"]
    invoice = processor.ai_result_to_invoice(
        cash,
        batch_id="fixture",
        source_file="cash.pdf",
        vendor_key="unknown",
        support_document_url="https://example.invalid/cash.pdf",
    )
    rows = invoice["rows"]
    assert round(sum(row["Amount"] for row in rows), 2) == 32.98
    fee = next(row for row in rows if row["Line Item Description"] == "Service Fee")
    discount = next(row for row in rows if row["Line Item Description"] == "Cash Discount")
    assert fee["Amount"] == 0.89
    assert discount["Amount"] == -0.89
    assert "invoice_description_missing_service_address" not in validate_row_contract(rows[0])

    older = processor.ai_result_to_invoice(
        a1,
        batch_id="fixture",
        source_file="older.pdf",
        vendor_key="unknown",
        support_document_url="https://example.invalid/older.pdf",
    )
    newer_normalized = dict(a1)
    newer_normalized["total_amount"] = 220.64
    newer_normalized["line_items"] = [{**a1["line_items"][0], "amount": 220.64}]
    newer = processor.ai_result_to_invoice(
        newer_normalized,
        batch_id="fixture",
        source_file="newer.pdf",
        vendor_key="unknown",
        support_document_url="https://example.invalid/newer.pdf",
    )
    deduped, reviews = processor._deduplicate_invoices([older, newer], [])
    assert len(deduped) == 1
    assert deduped[0]["source_file"] == "newer.pdf"
    assert deduped[0]["manual_review_codes"] == ["duplicate_invoice_total_conflict"]
    assert reviews[0]["reason_codes"] == ["duplicate_invoice_total_conflict"]

    bravo_payload = {
        "vendor_name": "Bravo Flooring",
        "line_items": [{"description": "Tip Charge - Zone 1", "amount": 75, "gl_account_candidate": "6750"}],
        "total_amount": 75,
        "warnings": ["Line item amounts were not visible for all lines. Invoice total used as fallback."],
    }
    bravo, changed = processor._repair_bravo_flooring_payload(
        bravo_payload,
        "Bravo Flooring SOLD TO Penn Warren SHIPPED TO A-27 Vinyl Everywhere",
    )
    assert changed
    assert bravo["line_items"][0]["description"] == "Trip Charge - Zone 1"
    assert bravo["line_items"][0]["gl_account_candidate"] == "7536"
    assert bravo["warnings"] == []

    print("Rasa screenshot reasoning smoke test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
