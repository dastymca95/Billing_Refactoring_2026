"""Smoke test for the deterministic ResMan, LLC processor.

This uses synthetic text samples so it does not touch source training bills,
Output/Template.xlsx, Dropbox, or AI providers.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp.backend.services.resman_processor import (
    _finalize_invoice,
    _rows_for_invoice,
    parse_resman_invoice_text,
)
from webapp.backend.services.utility_processor_common import load_chart_of_accounts


def _finalized(text: str):
    inv = parse_resman_invoice_text(text, source_file="synthetic.pdf")
    _finalize_invoice(inv, valid_gls=load_chart_of_accounts())
    return inv


def test_credit_screening_invoice() -> None:
    text = """
Sales Invoice
Invoice #: RSM392869
Invoice Date: 05/19/2026
Due Date: 06/13/2026
Customer Number Sales Rep Terms Due Date
RSM-000023758-000040739 Net 25 06/13/2026
Item/Description Qty Rate Amount Taxable
ResMan Qualifier 6 $23.64 $141.84 NT
Qualifier - PREMIUM
04/01/2026 - 04/30/2026
Credit Builder 19 $5.95 $113.05 NT
May Credit Builder
05/01/2026 - 05/31/2026
Qualifier_FACTA 1 $2.79 $2.79 NT
Facta
04/01/2026 - 04/30/2026
Subtotal $257.68
Tax Total $0.00
Invoice Total $257.68
Less Payments and Credits ($0.00)
Invoice Amount Due $257.68
E: accounting@myresman.com
"""
    inv = _finalized(text)
    assert inv.invoice_type == "B"
    assert inv.property_abbreviation == "BCA"
    assert len(inv.line_items) == 3
    assert {line.gl_account for line in inv.line_items} == {"6115"}
    assert str(sum((line.amount_with_tax for line in inv.line_items))) == "257.68"
    assert not inv.manual_review_reasons


def test_software_invoice_with_tax_allocation() -> None:
    text = """
Sales Invoice
Invoice #: RSM400001
Invoice Date: 05/05/2026
Due Date: 05/30/2026
Customer Number Sales Rep Terms Due Date
RSM-000023758-000033136 Net 25 05/30/2026
Item/Description Qty Rate Amount Taxable
Website Package 1 $100.00 $100.00 T
Website Package
05/01/2026 - 05/31/2026
ResMan Conventional Monthly Service 1 $200.00 $200.00 NT
Monthly Service
05/01/2026 - 05/31/2026
Subtotal $300.00
Tax Total $9.75
Invoice Total $309.75
Less Payments and Credits ($0.00)
Invoice Amount Due $309.75
E: accounting@myresman.com
"""
    inv = _finalized(text)
    assert inv.invoice_type == "A"
    assert inv.property_abbreviation == "LLA"
    assert [line.gl_account for line in inv.line_items] == ["6315", "6136"]
    assert [str(line.amount_with_tax) for line in inv.line_items] == ["109.75", "200.00"]
    assert str(sum((line.amount_with_tax for line in inv.line_items))) == "309.75"
    assert not inv.manual_review_reasons


def test_new_property_prorated_lines_and_optional_columns() -> None:
    text = """
Sales Invoice
Invoice #: RSM396582
Invoice Date: 06/05/2026
Due Date: 06/30/2026
Bill To Ship To Amount Due
The Flats at Lancaster The Flats at Lancaster
Customer Number Sales Rep Terms Due Date
RSM-000023758-000079944 Net 25 06/30/2026
Item/Description Qty Rate Amount Taxable
ResMan Conventional Monthly Service 112 $0.65482143 $73.34 T
05/22/2026 - 05/31/2026
ResMan Conventional Monthly Service 112 $2.03 $227.36 T
06/01/2026 - 06/30/2026
SMS Texting Services 112 $0.080625 $9.03 NT
05/22/2026 - 05/31/2026
SMS Texting Services 112 $0.25 $28.00 NT
06/01/2026 - 06/30/2026
ResMan Leasing Pro: BlueMoon 2.0 112 $0.25482143 $28.54 T
05/22/2026 - 05/31/2026
ResMan Leasing Pro: BlueMoon 2.0 112 $0.79 $88.48 T
06/01/2026 - 06/30/2026
Subtotal $454.75
Tax Total $39.67
Invoice Total $494.42
Invoice Amount Due $494.42
"""
    inv = _finalized(text)
    assert inv.property_abbreviation == "FAL"
    assert len(inv.line_items) == 6
    assert str(sum((line.amount_with_tax for line in inv.line_items))) == "494.42"
    assert not inv.manual_review_reasons
    for row in _rows_for_invoice(inv):
        assert row["Due Date"] == "06/30/2026"
        assert row["Quantity"] == ""
        assert row["Unit Price"] == ""
        assert row["Tax"] == ""
        assert row["Received Date"] == ""


def test_corporate_resman_customer_maps_to_ngm() -> None:
    text = """
Sales Invoice
Invoice #: RSM397204
Invoice Date: 06/05/2026
Due Date: 06/30/2026
Bill To Ship To Amount Due
Nex Gen Management Nex Gen Management
Customer Number Sales Rep Terms Due Date
RSM-000023758-000000000 Net 25 06/30/2026
Item/Description Qty Rate Amount Taxable
ResMan-University 5 $10.00 $50.00 T
06/01/2026 - 06/30/2026
Subtotal $50.00
Tax Total $4.75
Invoice Total $54.75
Invoice Amount Due $54.75
"""
    inv = _finalized(text)
    assert inv.property_abbreviation == "NGM"
    assert not inv.manual_review_reasons


if __name__ == "__main__":
    test_credit_screening_invoice()
    test_software_invoice_with_tax_allocation()
    test_new_property_prorated_lines_and_optional_columns()
    test_corporate_resman_customer_maps_to_ngm()
    print("smoke_resman_processor: ok")
