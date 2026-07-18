"""Regression coverage for false vendor detection and silent zero output."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from webapp.backend.services import ai_invoice_processor
from webapp.backend.services import batch_processor
from webapp.backend.services import vendor_detection


COOKS_TEXT = """
INVOICE
Cook's Pest Control
SERVICES PERFORMED AT: ACCOUNT INFORMATION:
Account #: 411504
Granite Heights/ NextGen Multifamily
1400 N Chamberlain Ave
Office
Chattanooga TN 37406
YOUR INVOICE FOR SERVICE ON: 06/24/2026 INV #: 28719534
SERVICE(S) PERFORMED: QUANTITY AMOUNT
Com Pest - Monthly 1 $132.00
SUB TOTAL: $132.00
TAX: 0
TOTAL DUE: $132.00
NOTE: Payment is due at time of service.
Thank you for allowing Cook's to serve you. We appreciate your business.
"""


def main() -> None:
    path = Path("Invoice_28719534.pdf")
    with patch.object(vendor_detection, "_document_text_sample", return_value=COOKS_TEXT):
        matched, _, _ = vendor_detection._looks_like_nashville_electric_service(path)
    assert not matched, "NES must not match the substring inside 'business'"

    with patch.object(
        vendor_detection,
        "_document_text_sample",
        return_value="NES Current balance due Billing period Service Address Account #:",
    ):
        matched, _, _ = vendor_detection._looks_like_nashville_electric_service(path)
    assert matched

    raw = ai_invoice_processor._extract_cooks_pest_control_payload(COOKS_TEXT)
    assert raw["invoice_number"] == "28719534"
    normalized = ai_invoice_processor.validate_ai_extraction({
        **raw,
        "_document_text": COOKS_TEXT,
        "_source_file": path.name,
    })
    assert normalized["vendor_name"] == "Cook's Pest Control, INC"
    assert normalized["property_abbreviation"] == "TFF"
    assert normalized["line_items"][0]["gl_account_candidate"] == "6560"
    assert normalized["total_amount"] == 132.0

    empty = SimpleNamespace(invoices=[], manual_review_rows=[], errors=[])
    explained = SimpleNamespace(invoices=[], manual_review_rows=[], errors=["parse failed"])
    assert batch_processor._needs_zero_output_ai_fallback(empty, [path])
    assert not batch_processor._needs_zero_output_ai_fallback(explained, [path])
    print("zero-output routing smoke: PASS")


if __name__ == "__main__":
    main()
