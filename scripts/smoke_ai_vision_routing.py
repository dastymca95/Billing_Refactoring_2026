"""Regression checks for scanned-invoice AI/Vision routing.

No external provider, Dropbox, or source document is touched.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp.backend.services import ai_invoice_processor, document_ingestion  # noqa: E402


DEGRADED_PHOTO_OCR = """
Ay i y A) 2501 S Virginia St Unit 11
4 oh) Sa Hopkinsville KY 42240 p:931-905-6862
ernesto2272@hotmail.com i wvoices__775
a ae Boo Beeninod Ave OWN ville TW 762
QUANTIT DESCRIPTION 2 Glelin Clint GAAS. $ 90>
Kitden Chet Tip A Gov
"""

GOOD_INVOICE_OCR = """
TITO INSTALLATION INVOICE #910 DATE 06/24/2026
300 Greenwood Ave Unit A11
DESCRIPTION Kitchen Cabinet Installation $800.00
Kitchen Cabinet Top $800.00
Installation of electrical appliances $200.00
Remove old cabinets and old appliances $300.00
TOTAL $2,100.00
"""


def main() -> int:
    weak_score = document_ingestion._text_quality_score(DEGRADED_PHOTO_OCR)
    good_score = document_ingestion._text_quality_score(GOOD_INVOICE_OCR)
    assert weak_score < 0.45, weak_score
    assert good_score >= 0.70, good_score

    status = SimpleNamespace(vision_enabled=True, vision_mode="fallback_only")
    candidate = document_ingestion.DocumentCandidate(
        source_file="photo.pdf",
        source_type="pdf_scanned",
        document_text=DEGRADED_PHOTO_OCR,
        text_quality_score=weak_score,
        needs_vision=True,
        extraction_quality={
            "text_quality_score": weak_score,
            "vision_recommended": True,
        },
    )
    assert ai_invoice_processor._should_use_vision_for_candidate(candidate, status)
    status.vision_model = "gemini-2.5-flash-lite"
    status.model = "deepseek-v4-flash"
    assert (
        ai_invoice_processor._vision_model_for_candidate(status, candidate)
        == "gemini-2.5-flash"
    )

    unusable_payload = {
        "vendor_name": "",
        "invoice_date": "",
        "total_amount": 0,
        "confidence": 0.25,
        "warnings": ["Handwritten text is ambiguous."],
        "line_items": [{"description": "garbled", "amount": 90}],
    }
    assert ai_invoice_processor._ai_payload_requires_vision(unusable_payload, status)

    references = ai_invoice_processor.load_references()
    canonical_vendor = ai_invoice_processor._canonical_vendor(
        "TITO INSTALLATION",
        references["vendors"],
    )
    assert canonical_vendor == "Ernesto Ferrera dba Tito Installation", canonical_vendor

    selected = ai_invoice_processor._select_prompt_references(
        references,
        query="Tito kitchen cabinet 300 Greenwood A11",
        vendor_hint=canonical_vendor,
    )
    assert any("Tito Installation" in json.dumps(row) for row in selected["vendors"])
    assert any("Penn Warren" in json.dumps(row) for row in selected["properties"])
    assert any("A11" in json.dumps(row) for row in selected["properties"])
    selected_gls = {str(row.get("gl_code") or "") for row in selected["gl_accounts"]}
    assert {"6512", "6760", "6775", "7595"}.issubset(selected_gls), selected_gls

    expected_gls = {
        "Kitchen Cabinet Installation": "7595",
        "Kitchen Cabinet Top": "6512",
        "Installation of electrical appliances": "6505",
        "Remove old Cabinets and old appliances": "7595",
    }
    for description, expected_gl in expected_gls.items():
        account = ai_invoice_processor._suggest_valid_gl_candidate(
            description=description,
            vendor_name=canonical_vendor,
            ai_suggested_gl="",
        )
        assert account and account.get("gl_code") == expected_gl, (description, account)

    property_abbreviation, location, matched = ai_invoice_processor._resolve_property_context(
        property_abbreviation="",
        property_candidate="",
        service_address="300 Greenwood Ave",
        location_candidate="A-11 Unit",
        properties=references["properties"],
    )
    assert property_abbreviation == "TPW", property_abbreviation
    assert location == "A11", location
    assert matched.get("Property Name") == "The Penn Warren", matched

    normalized = {
        "category": "other_infrequent",
        "invoice_date": "06/24/2026",
        "service_address": "300 Greenwood Ave",
        "unit_number": "A11",
        "location": "A11",
        "vendor_name": canonical_vendor,
        "line_items": [{"description": "Kitchen Cabinet Installation", "amount": 800}],
        "bill_or_credit": "Bill",
        "canonical_invoice_description": "Jun-26 - Kitchen Cabinet Installation",
    }
    description = ai_invoice_processor._compose_invoice_description(
        normalized,
        normalized["line_items"][0],
    )
    assert description == "Jun-26 - Kitchen Cabinet Installation", description

    reconciled = ai_invoice_processor._reconcile_high_confidence_vision_candidates({
        "invoice_date": "06/21/2026",
        "location_candidate": "",
        "vision_candidates": [
            {
                "field_key": "invoice_date",
                "value": "6/24/2026",
                "confidence": 0.98,
            },
            {
                "field_key": "location_candidate",
                "value": "A-11 unit",
                "confidence": 0.95,
            },
        ],
    })
    assert reconciled["invoice_date"] == "06/24/2026", reconciled
    assert reconciled["location_candidate"] == "A-11 unit", reconciled

    print(
        "PASS: scanned invoice OCR routes to Vision, contextual references are "
        "retrieved, construction GL semantics are stable, and explicit units "
        "resolve against the property reference."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
