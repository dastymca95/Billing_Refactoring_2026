from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp.backend.services.apartments_com_processor import (  # noqa: E402
    _finalize_invoice,
    _rows_for_invoice,
    parse_apartments_com_invoice_text,
)
from webapp.backend.services.vendor_detection import detect_vendor_for_file  # noqa: E402


def test_current_costar_layout() -> None:
    text = """
Invoice Page 1 of 2
Invoice Number 124126076
501 S 5th Street Account #/Location ID 213757911
Richmond, VA 23219
Invoice Date 06/01/2026
CoStar Federal Tax ID 52-2134617
Payment Terms Net 30
Due Date 07/01/2026
Service Period 06/01/2026 to 06/30/2026
Invoice Amount USD 519.00
ACCOUNTS PAYABLE
NEX-GEN - THE GABLES AT RED RIVER
1525 WILMA RUDOLPH BLVD
CLARKSVILLE TN 37040-6781
CURRENT INVOICE
See the following page(s) for detail
Network 3 Gold Plus USD 519.00
Sub-Total USD 519.00
Tax USD 0.00
Current Invoice Total USD 519.00
Apartments LLC
Page 2 of 2
Account #/Location ID Invoice Date Invoice Number Federal Tax ID Page
213757911 06/01/2026 124126076 52-2134617 2 of 2
Nex-Gen - The Gables at Red River-1525 Wilma Rudolph Blvd, Clarksville, TN, 37040
PRODUCT SITE ID SUBMARKET CONTRACT # BILLING PERIOD SUBTOTAL TAX AMOUNT
Network 3 Gold Plus 213757911 1371971 06/01/2026 to 06/30/2026 519.00 0.00 519.00
Current Invoice Total (USD): 519.00 0.00 519.00
"""
    inv = parse_apartments_com_invoice_text(text, source_file="invoice_124126076.pdf")
    _finalize_invoice(inv)
    rows = _rows_for_invoice(inv)
    assert inv.invoice_number == "124126076"
    assert inv.account_number == "213757911"
    assert inv.manual_review_reasons == []
    assert rows[0]["Vendor"] == "Apartments.com"
    assert rows[0]["Property Abbreviation"] == "SWTG"
    assert rows[0]["GL Account"] == "6335"
    assert rows[0]["Amount"] == 519.0
    assert rows[0]["Due Date"] == "07/01/2026"
    assert rows[0]["Reference Number"] == "1371971"
    assert rows[0]["Invoice Description"] == "06/01/26-06/30/26 - Network 3 Gold Plus"
    assert rows[0]["Line Item Description"] == "06/01/26-06/30/26 - Network 3 Gold Plus"


def test_detector_with_mocked_text() -> None:
    from webapp.backend.services import vendor_detection

    original = vendor_detection._document_text_sample
    try:
        vendor_detection._document_text_sample = lambda _path, _limit=5000: (
            "Invoice Number 124126076\n"
            "CoStar Federal Tax ID 52-2134617\n"
            "Account #/Location ID 213757911\n"
            "Apartments LLC\n"
        )
        det = detect_vendor_for_file(Path("invoice_124126076.pdf"))
    finally:
        vendor_detection._document_text_sample = original
    assert det["vendor_key"] == "apartments_com"
    assert det["processing_mode"] == "deterministic"


if __name__ == "__main__":
    test_current_costar_layout()
    test_detector_with_mocked_text()
    print("smoke_apartments_com_processor: ok")
