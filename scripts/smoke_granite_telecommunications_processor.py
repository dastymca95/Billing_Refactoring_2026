"""Smoke coverage for the deterministic Granite Telecommunications processor.

Uses synthetic statement text only. It does not call Dropbox, modify source
bills, or write Output/Template.xlsx.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp.backend.services.granite_telecommunications_processor import (  # noqa: E402
    _finalize_invoice,
    _rows_for_invoice,
    parse_granite_invoice_text,
)


def _statement(
    *,
    account: str,
    invoice: str,
    location: str,
    address: str,
    current: str,
    adjustment: str,
    total_due: str,
) -> str:
    return f"""
Invoice
www.granitenet.com ACCOUNT NUMBER: {account}
INVOICE DATE: 6/1/26
CURRENT CHARGES, TAXES, SURCHARGES: ${current}
ADJUSTMENTS: {adjustment}
TOTAL AMOUNT DUE: ${total_due}
YOUR ACCOUNT NUMBER: {account}
INVOICE NUMBER: {invoice}
INVOICE DATE: 6/1/26
Page 3 of 5
Account Number : {account}
Invoice: {invoice} Invoice Date: 06/01/2026
Location : {location}
{address}
Administrative Service Fee $0.90 1 $0.90 6/1/26 6/30/26
"""


def test_existing_property_and_net_charge() -> None:
    inv = parse_granite_invoice_text(
        _statement(
            account="05798620",
            invoice="749615442",
            location="Admiral Place Apartments",
            address="301 Ligon Dr | Shelbyville | TN | 37160",
            current="62.99",
            adjustment="-$20.00",
            total_due="42.99",
        ),
        source_file="granite.pdf",
        page_count=5,
    )
    _finalize_invoice(inv)
    row = _rows_for_invoice(inv)[0]
    assert inv.property_abbreviation == "APA"
    assert str(inv.amount_to_post) == "42.99"
    assert inv.manual_review_reasons == []
    assert row["GL Account"] == "6178"
    assert row["Amount"] == 42.99
    assert row["Due Date"] == "06/01/2026"
    assert row["Invoice Description"] == "06/01/26-06/30/26 - Phone Service - Account 05798620"
    assert row["Line Item Description"] == row["Invoice Description"]


def test_new_properties() -> None:
    cases = (
        ("05870897", "The Kensington", "2100 Clifton Ave | Nashville | TN | 37203", "TKA"),
        ("05870916", "Trinity Lofts", "1400 Brick Church Pike | Nashville | TN | 37207", "TLA"),
        ("05914590", "375 S Lancaster Rd", "375 S Lancaster Rd | Clarksville | TN | 37042", "FAL"),
    )
    for account, location, address, expected_property in cases:
        inv = parse_granite_invoice_text(
            _statement(
                account=account,
                invoice=f"749{account}",
                location=location,
                address=address,
                current="146.03",
                adjustment="$0.00",
                total_due="146.03",
            )
        )
        _finalize_invoice(inv)
        assert inv.property_abbreviation == expected_property
        assert inv.manual_review_reasons == []


if __name__ == "__main__":
    test_existing_property_and_net_charge()
    test_new_properties()
    print("smoke_granite_telecommunications_processor: ok")
