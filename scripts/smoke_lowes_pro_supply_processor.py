"""Smoke coverage for the deterministic Lowe's Pro Supply processor."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp.backend.services.lowes_pro_supply_processor import (  # noqa: E402
    _finalize_invoice,
    _rows_for_invoice,
    parse_lowes_pro_supply_text,
)


SAMPLE = """
INVOICE
Bill To # 202616
Order # 20665897-00
Invoice Date 04/29/26
Due Date 05/29/26
PO # POOL
Reference 978353
SHIP TO:
The Park at Carson
Attn: Leasing Office 105 Candy Mountain Rd
Birmingham, AL 35217-1254
Ship Point ** Drop Ship ** Via LOWE'S STORE BH0/1
Lowe's Invoice Number: 78353
1 L-155670 1 EA 1 0.00
PROMOTIONAL DISCOUNT APP
GL CODE:MISCELLANEOUS
2 L-1597629 1 EA 1 7.77 7.77
KS GREEN PAINT THINNER Q
GL CODE:PAINT
3 L-493054 5 EA 5 12.24 61.20
BH 6-FT 13-GA HD U-POST
GL CODE:LUMBER
4 L-66735 7 EA 7 1.88 13.16
HM 8-IN X 12-IN NO TRESS
GL CODE:HARDWARE
4 Lines Total Qty Shipped Total 14 Total 82.13
LAR SalesTax 8.22
Invoice Total 90.35
Description Total Merchandise
HARDWARE 14.48
LUMBER 67.33
MISCELLANEOUS
PAINT 8.54
Customer Copy Page 1 of 1
"""

CREDIT_SAMPLE = """
CREDIT
Bill To # 202616
Order # 21062190-00
Invoice Date 06/12/26
Due Date 07/12/26
PO # LIBERTY 0000
SHIP TO:
Rowe at Gate 1, The
705B Red River Rd
Clarksville, TN 37040
Ship Point Store
1 L-3626925 (1) EA (1) 75.98 (75.98)
100-FT 14/3 RUBBER CORD
GL CODE:ELECTRICAL
2 L-866423 (1) EA (1) 31.81 (31.81)
MS PVC 1-IN KIT - WH
GL CODE:ROUGH PLUMBING
2 Lines Total Qty Shipped Total 2 Total (107.79)
LAR SalesTax (10.05)
Invoice Total (117.84)
Description Total Merchandise
ELECTRICAL (82.91)
ROUGH PLUMBING (34.93)
Customer Copy Page 1 of 1
"""


def test_order_number_totals_and_optional_fields() -> None:
    invoice = parse_lowes_pro_supply_text(SAMPLE, source_file="lowes.pdf", page_count=1)
    _finalize_invoice(invoice)
    rows = _rows_for_invoice(invoice)

    assert invoice.order_number == "20665897-00"
    assert invoice.property_abbreviation == "TPAC"
    assert invoice.location == ""
    assert len(rows) == 3
    assert invoice.manual_review_reasons == []
    assert sum((Decimal(str(row["Amount"])) for row in rows), Decimal("0.00")) == Decimal("90.35")
    assert [row["GL Account"] for row in rows] == ["6770", "6666", "6651"]
    assert [row["Amount"] for row in rows] == [8.54, 67.33, 14.48]
    assert rows[0]["Invoice Description"] == (
        "Pool Supplies - Paint Thinner, U-Posts and Warning Signs"
    )
    assert len(rows[0]["Invoice Description"]) <= 75
    assert all(row["Invoice Number"] == "20665897-00" for row in rows)
    assert all(row["Due Date"] == "05/29/2026" for row in rows)
    for row in rows:
        for field in (
            "Location",
            "Payment Date",
            "Reference Number",
            "Payment Method",
            "Department",
            "Quantity",
            "Unit Price",
            "Tax",
            "Received Date",
        ):
            assert row[field] == "", (field, row[field])


def test_property_name_po_noise_and_credit() -> None:
    invoice = parse_lowes_pro_supply_text(CREDIT_SAMPLE, source_file="credit.pdf", page_count=1)
    _finalize_invoice(invoice)
    rows = _rows_for_invoice(invoice)

    assert invoice.property_abbreviation == "TRG1"
    assert invoice.bill_or_credit == "Credit"
    assert invoice.total_amount == Decimal("-117.84")
    assert invoice.manual_review_reasons == []
    assert len(rows) == 2
    assert all(row["Bill or Credit"] == "Credit" for row in rows)
    assert all("0000" not in row["Invoice Description"] for row in rows)
    assert sum((Decimal(str(row["Amount"])) for row in rows), Decimal("0.00")) == Decimal("-117.84")


if __name__ == "__main__":
    test_order_number_totals_and_optional_fields()
    test_property_name_po_noise_and_credit()
    print("smoke_lowes_pro_supply_processor: ok")
