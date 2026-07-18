"""Smoke test for the webapp-native Zillow Rentals processor.

Uses synthetic text matching the current Zillow PDF layout. It does not touch
Dropbox, source bills, or Output/Template.xlsx.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp.backend.services.zillow_processor import (  # noqa: E402
    _finalize_invoice,
    _rows_for_invoice,
    parse_zillow_invoice_text,
)


def test_current_layout() -> None:
    text = """
Invoice
Bill to: Sold to: INV37114058
Admiral Place 705B Red River Street Invoice date: 05/31/2026
301 Ligon Dr Clarksville, Tennessee, 37040 Account #: ZRN-6572062-ZRN-7005775
Shelbyville, Tennessee, 37160 ap@nexgenmultifamily.com Invoice amount: $732.00
ap@nexgenmultifamily.com Due Date: 06/30/2026
Recurring monthly charges
Product Service period Price Quantity Discount Subtotal Tax Total after tax
Admiral Place: Zillow Rent
05/01/2026 -
Connect: Enhanced $732.00 1 $0.00 $732.00 $0.00 $732.00
05/31/2026
Package
Total
Purchase amount $732.00
Tax amount $0.00
Credits applied $0.00
Payments applied $0.00
Amount due $732.00
"""
    inv = parse_zillow_invoice_text(text, source_file="INV37114058.pdf", page_count=2)
    _finalize_invoice(inv)
    rows = _rows_for_invoice(inv)
    assert inv.invoice_number == "INV37114058"
    assert inv.account_number == "ZRN-6572062-ZRN-7005775"
    assert inv.property_abbreviation == "APA"
    assert inv.package == "Enhanced Package"
    assert str(inv.amount_due) == "732.00"
    assert inv.manual_review_reasons == []
    assert rows[0]["GL Account"] == "6335"
    assert rows[0]["Amount"] == 732.0
    assert "05/01/26-05/31/26 - Admiral Place: Zillow Rentals" in rows[0]["Invoice Description"]


def test_split_signature_package() -> None:
    text = """
Invoice
Bill to: Sold to: INV37133050
Canoe Creek 705B Red River Street Invoice date: 05/31/2026
151 A Hatchers Ln Clarksville, Tennessee, 37040 Account #: ZRN-6572062-ZRN-1538349
Clarksville, Tennessee, 37043 ap@nexgenmultifamily.com Invoice amount: $1,038.00
ap@nexgenmultifamily.com Due Date: 06/30/2026
Recurring monthly charges
Product Service period Price Quantity Discount Subtotal Tax Total after tax
Canoe Creek: Signature 05/01/2026 -
$1,038.00 1 $0.00 $1,038.00 $0.00 $1,038.00
Package 05/31/2026
Total
Amount due $1,038.00
"""
    inv = parse_zillow_invoice_text(text, source_file="INV37133050.pdf", page_count=1)
    _finalize_invoice(inv)
    rows = _rows_for_invoice(inv)
    assert inv.property_abbreviation == "OC-CCA"
    assert inv.package == "Signature Package"
    assert inv.manual_review_reasons == []
    assert "Signature Package" in rows[0]["Invoice Description"]


def test_separate_bill_to_block_layout() -> None:
    text = """
Invoice
Bill to:
Griffin Gate Apartments 705B Red River Street Invoice date: 05/31/2026
300 Griffin Gate Drive Clarksville, Tennessee, 37040 Account #: ZRN-6572062-ZRN-7470042
Hopkinsville, Kentucky, 42240 ap@nexgenmultifamily.com Invoice amount: $501.00
Sold to:
705B Red River Street
Clarksville, Tennessee, 37040
Due Date: 06/30/2026
Recurring monthly charges
Product Service period Price Quantity Discount Subtotal Tax Total after tax
Griffin Gate Apartments:
05/01/2026 -
Zillow Rent Connect: $501.00 1 $0.00 $501.00 $0.00 $501.00
05/31/2026
Enhanced Package
Total
Purchase amount $501.00
Tax amount $0.00
Credits applied $0.00
Payments applied $0.00
Amount due $501.00
"""
    inv = parse_zillow_invoice_text(text, source_file="INV37129734.pdf", page_count=2)
    _finalize_invoice(inv)
    rows = _rows_for_invoice(inv)
    assert inv.invoice_number == "INV37129734"
    assert inv.property_name_raw == "Griffin Gate Apartments"
    assert inv.property_abbreviation == "GGOG"
    assert inv.package == "Enhanced Package"
    assert inv.manual_review_reasons == []
    assert rows[0]["Invoice Description"] == (
        "05/01/26-05/31/26 - Griffin Gate Apartments: "
        "Zillow Rentals - Zillow Rent Connect: Enhanced Package"
    )


def test_new_property_aliases() -> None:
    cases = (
        ("The Flats at Lancaster", "FAL", "The Flats at Lancaster"),
        ("Pleasant View Townhomes", "PVT", "Pleasantview Village Townhomes"),
    )
    for property_name, abbreviation, display_name in cases:
        text = f"""
Invoice
Bill to: Sold to: INV37600000
{property_name} 705B Red River Street Invoice date: 06/30/2026
375 S Lancaster Rd Clarksville, Tennessee, 37040 Account #: ZRN-6572062-ZRN-1123581
Clarksville, Tennessee, 37042 ap@nexgenmultifamily.com Invoice amount: $100.00
ap@nexgenmultifamily.com Due Date: 07/30/2026
Recurring monthly charges
Product Service period Price Quantity Discount Subtotal Tax Total after tax
{property_name}: Zillow Rent Connect: Base Package 06/01/2026 - 06/30/2026 $100.00 1 $0.00 $100.00 $0.00 $100.00
Total
Amount due $100.00
"""
        inv = parse_zillow_invoice_text(text, source_file="INV37600000.pdf", page_count=2)
        _finalize_invoice(inv)
        assert inv.property_abbreviation == abbreviation
        assert inv.property_display_name == display_name
        assert inv.manual_review_reasons == []


if __name__ == "__main__":
    test_current_layout()
    test_split_signature_package()
    test_separate_bill_to_block_layout()
    test_new_property_aliases()
    print("smoke_zillow_processor: ok")
