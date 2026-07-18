from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp.backend.services.utility_processor_common import load_chart_of_accounts
from webapp.backend.services.utility_wave2_processors import (
    _finalize_invoice,
    _invoice_to_preview_dict,
)
from webapp.backend.services.utility_wave3_processors import (
    SPECS,
    _clear_soft_vision_review_if_valid,
    _load_config,
    _parse_city_union_city,
)


def _finalize_sample(text: str):
    spec = SPECS["city_of_union_city"]
    invoice = _parse_city_union_city(spec, text, "sample.pdf")[0]
    _finalize_invoice(invoice, spec, _load_config(None, spec.key), load_chart_of_accounts())
    return invoice, _invoice_to_preview_dict(invoice)


def test_standard_bill() -> None:
    text = """
    City of Union City Water & Sewer
    SERVICE ADDRESS ACCOUNT # BILL DATE DUE DATE
    1531 HIGH SCHOOL DR #62 088-03 14-33 5/13/2026 6/5/2026
    DATE READING DATE READING USAGE
    5/6/2026 330 4/8/2026 300 30 WATER
    PREVIOUS BALANCE 0.00
    WATER $7.68
    SEWER $7.22
    SANITATION $22.50
    STORMWATER USER FEE $2.50
    TAX $0.75
    CURRENT BILL $40.65
    AMOUNT DUE $40.65
    """
    invoice, preview = _finalize_sample(text)

    assert invoice.invoice_number == "088-0314-33 May 26"
    assert invoice.invoice_date.strftime("%Y-%m-%d") == "2026-05-13"
    assert invoice.due_date.strftime("%Y-%m-%d") == "2026-06-05"
    assert invoice.service_address == "1531 High School Dr # 62"
    assert invoice.property_abbreviation == "VOA"
    assert invoice.location == "62"
    assert Decimal(str(preview["line_items_total"])) == Decimal("40.65")
    assert [row["GL Account"] for row in preview["rows"]] == ["6955", "6955", "6940", "6995"]
    assert preview["rows"][0]["Invoice Description"] == "04/08/26-05/06/26 - 1531 High School Dr # 62"
    assert not invoice.manual_review_reasons


def test_final_bill_with_bad_sewer_ocr() -> None:
    text = """
    City of Union City Water & Sewer FINAL BILL
    SERVICE ADDRESS ACCOUNT # BILL DATE DUE DATE
    1533 HIGH SCHOOL DR #68 088-0503-14 5/12/2026 5/25/2026
    DATE READING DATE READING USAGE
    4/27/2026 793 4/8/2026 790 3 WATER
    PREVIOUS BALANCE 0.00
    WATER $7.68
    SEWER 793
    SANITATION $22.50
    STORMWATER USER FEE $2.50
    TAX $0.75
    CURRENT BILL $40.65
    AMOUNT DUE $40.65
    """
    invoice, preview = _finalize_sample(text)
    invoice.manual_review_reasons = ["vision_recommended"]
    _clear_soft_vision_review_if_valid(invoice)

    assert invoice.invoice_number == "088-0503-14 May 26 Final"
    assert invoice.service_period_start.strftime("%Y-%m-%d") == "2026-04-08"
    assert invoice.service_period_end.strftime("%Y-%m-%d") == "2026-04-27"
    assert invoice.location == "68"
    assert Decimal(str(preview["line_items_total"])) == Decimal("40.65")
    assert preview["rows"][0]["Invoice Description"] == "04/08/26-04/27/26 - 1533 High School Dr # 68"
    assert any(row["Line Item Description"].endswith(" - Sewer") for row in preview["rows"])
    assert not invoice.manual_review_reasons


if __name__ == "__main__":
    test_standard_bill()
    test_final_bill_with_bad_sewer_ocr()
    print("city_union_city_water_parser: ok")
