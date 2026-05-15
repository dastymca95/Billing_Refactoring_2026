from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp.backend.services.output_contract_validator import validate_row_contract  # noqa: E402
from webapp.backend.services.utility_processor_common import load_chart_of_accounts  # noqa: E402


def _base_row() -> dict:
    return {
        "Invoice Number": "C10181446 May 26",
        "Bill or Credit": "Bill",
        "Invoice Date": "05/07/2026",
        "Accounting Date": "05/07/2026",
        "Vendor": "EPB Fiber Optics",
        "Invoice Description": "05/08/26-06/07/26 - 21752 River Canyon Rd",
        "Line Item Number": "1",
        "Property Abbreviation": "RCC",
        "Location": "",
        "GL Account": "6960",
        "Line Item Description": "05/08/26-06/07/26 - 21752 River Canyon Rd - Fiber Internet",
        "Amount": "68.95",
        "Expense Type": "General",
        "Is Replacement Reserve": "false",
        "Due Date": "05/22/2026",
        "Document Url": "",
        "_meta": {
            "service_address": "21752 River Canyon Rd",
            "matched_property_name": "River Canyon Apartments",
        },
    }


def main() -> int:
    failures: list[str] = []
    valid_gls = load_chart_of_accounts()

    good = _base_row()
    good_flags = validate_row_contract(good, valid_gl_accounts=valid_gls)
    if good_flags:
        failures.append(f"valid row should pass without blocking flags: {good_flags}")

    required = _base_row()
    for key in ("Invoice Number", "Property Abbreviation", "GL Account", "Due Date"):
        required[key] = ""
    required_flags = set(validate_row_contract(required, valid_gl_accounts=valid_gls))
    for expected in (
        "invoice_number_missing",
        "property_abbreviation_missing",
        "gl_account_missing",
        "due_date_missing",
    ):
        if expected not in required_flags:
            failures.append(f"missing required-field flag {expected}; got {sorted(required_flags)}")

    invalid_gl = _base_row()
    invalid_gl["GL Account"] = "MISCELLANEOUS"
    if "invalid_gl_account" not in validate_row_contract(invalid_gl, valid_gl_accounts=valid_gls):
        failures.append("text GL account was not rejected")

    raw_location = _base_row()
    raw_location["Location"] = "21752 River Canyon Rd"
    if "raw_address_in_location" not in validate_row_contract(raw_location, valid_gl_accounts=valid_gls):
        failures.append("raw full address in Location was not rejected")

    tax_line = _base_row()
    tax_line["Line Item Description"] = "05/08/26-06/07/26 - 21752 River Canyon Rd - Sales Tax"
    if "standalone_tax_line" not in validate_row_contract(tax_line, valid_gl_accounts=valid_gls):
        failures.append("standalone tax line was not rejected")

    payment = _base_row()
    payment["Line Item Description"] = "05/08/26-06/07/26 - 21752 River Canyon Rd - Payment Received"
    if "payment_or_previous_balance_expense_line" not in validate_row_contract(payment, valid_gl_accounts=valid_gls):
        failures.append("payment line was not rejected")

    document_url = _base_row()
    document_url_flags = validate_row_contract(
        document_url,
        valid_gl_accounts=valid_gls,
        require_document_url=True,
    )
    if "document_url_missing" not in document_url_flags:
        failures.append("Document Url requirement did not trigger when enabled")

    if failures:
        print("FAIL:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("PASS: mandatory ResMan row field contract is enforced.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
