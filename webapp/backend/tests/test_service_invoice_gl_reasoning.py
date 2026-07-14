from __future__ import annotations

from webapp.backend.services import ai_invoice_processor, canonical_rules
from webapp.backend.services.service_invoice_gl_reasoning import (
    build_gl_accounting_reasoning,
    classify_line_item_semantics,
)


def _canonicalized(payload: dict):
    references = ai_invoice_processor.load_references()
    normalized = ai_invoice_processor.validate_ai_extraction(payload, references=references)
    return canonical_rules.canonicalize_normalized_invoice(normalized, references=references)


def _reasoning_for_line(vendor: str, line: dict, *, total: float | None = None) -> dict:
    amount = float(line.get("amount") or total or 100)
    normalized = {
        "vendor_name": vendor,
        "raw_vendor_name": vendor,
        "invoice_description": line.get("invoice_description") or line.get("description") or line.get("activity") or "",
        "invoice_nature": "one_time",
        "property_abbreviation": line.get("property_abbreviation") or "VOA",
        "total_amount": total or amount,
        "line_items": [line],
        "validation_summary": {"total_reconciliation_passed": True},
    }
    reasoning = build_gl_accounting_reasoning(normalized, line, "other_infrequent")
    assert reasoning, "expected service invoice GL reasoning"
    return reasoning


def _alt_text(reasoning: dict) -> str:
    pieces = []
    for key in ("alternatives", "rejected_alternatives"):
        for item in reasoning.get(key) or []:
            pieces.append(f"{item.get('gl_code')} {item.get('gl_name')} {item.get('reason')}")
    return " ".join(pieces).lower()


def test_kros_invoice_uses_line_level_accounting_reasoning():
    payload = {
        "vendor_name": "Kros Home Services LLC",
        "invoice_number": "1323",
        "invoice_date": "04/28/2025",
        "due_date": "05/28/2025",
        "property_abbreviation": "VOA",
        "property_candidate": "Villages of Autumnwood",
        "service_address": "1509 High School Dr, Union City, TN",
        "invoice_description": "Kros Home Services LLC invoice 1323",
        "total_amount": 1450.00,
        "line_items": [
            {"activity": "Painting", "description": "Unit 21, 2 Bedrooms and 1 bathroom", "amount": 700.00, "confidence": 0.93},
            {"activity": "Maintenance", "description": "", "amount": 300.00, "confidence": 0.88},
            {"activity": "Cleaning Services", "description": "", "amount": 150.00, "confidence": 0.93},
            {"activity": "Painting", "description": "bathtub Unit 60", "amount": 300.00, "confidence": 0.93},
        ],
    }

    result = _canonicalized(payload)
    items = result["line_items"]

    assert result["vendor_name"] == "Kros Home Services LLC"
    assert result["total_amount"] == 1450.00
    assert len(items) == 4

    line1 = items[0]
    r1 = line1["gl_accounting_reasoning"]
    assert "Painting" in line1["description"]
    assert r1["classification"]["location_detected"] == "21"
    assert line1["gl_account_candidate"] != "6770"
    assert r1["classification"]["work_mode"] == "labor_service"
    assert r1["classification"]["trade_family"] == "painting"
    assert any(alt["gl_code"] == "6770" and "no itemized materials" in alt["reason"].lower() for alt in r1["rejected_alternatives"])

    line2 = items[1]
    r2 = line2["gl_accounting_reasoning"]
    assert "Maintenance" in line2["description"]
    assert r2["confidence"] < 0.85
    assert r2["review"]["level"] in {"non_blocking", "required"}
    assert "vague" in (r2["review"]["reason"] or "").lower()

    line3 = items[2]
    r3 = line3["gl_accounting_reasoning"]
    assert "Cleaning Services" in line3["description"]
    assert line3["gl_account_candidate"] != "6770"
    assert r3["classification"]["trade_family"] == "cleaning"
    assert line3["gl_account_candidate"] in {"6750", "6775", "7595", "6500"}

    line4 = items[3]
    r4 = line4["gl_accounting_reasoning"]
    assert "bathtub Unit 60" in line4["description"]
    assert r4["classification"]["location_detected"] == "60"
    assert line4["gl_account_candidate"] != "6770"
    assert r4["classification"]["trade_family"] == "tub_refinishing"
    assert line4["gl_account_candidate"] in {"6570", "6760", "7595", "6500"}

    for reasoning in (r1, r2, r3, r4):
        alt_text = _alt_text(reasoning)
        for unrelated in ("electric - vacant", "water", "sewer", "office supplies", "legal", "insurance", "marketing"):
            assert unrelated not in alt_text
        evidence_text = " ".join(ev["text"] for ev in reasoning["evidence"])
        assert "No strong keyword" not in evidence_text


def test_painting_labor_is_not_paint_supplies():
    reasoning = _reasoning_for_line(
        "Any Local Contractor",
        {"activity": "Painting", "description": "Unit 21, 2 Bedrooms and 1 bathroom", "amount": 700},
    )

    assert reasoning["classification"]["work_mode"] == "labor_service"
    assert reasoning["classification"]["trade_family"] == "painting"
    assert reasoning["classification"]["location_detected"] == "21"
    assert reasoning["selected_gl_code"] == "6760"
    assert any(alt["gl_code"] == "6770" for alt in reasoning["rejected_alternatives"])


def test_paint_materials_prefer_paint_supplies():
    line = {"description": "5 gallons interior paint, rollers, tape", "amount": 320}
    semantics = classify_line_item_semantics(
        line,
        {"vendor_name": "Sherwin-Williams", "vendor_type": "material_supplier"},
        {"invoice_text": line["description"], "property_abbreviation": "VOA"},
    )
    reasoning = _reasoning_for_line("Sherwin-Williams", line)

    assert semantics["work_mode"] == "materials"
    assert semantics["trade_family"] == "painting"
    assert reasoning["selected_gl_code"] == "6770"
    assert all(alt["gl_code"] != "6920" for alt in reasoning["rejected_alternatives"])


def test_cleaning_service_and_plumbing_leak_stay_in_family():
    cleaning = _reasoning_for_line(
        "Top Notch Cleaning LLC",
        {"activity": "Cleaning Services", "description": "Move-out clean Unit 12", "amount": 150},
    )
    plumbing = _reasoning_for_line(
        "Hiller LLC",
        {"description": "No visible leaks in Unit 7", "amount": 199},
    )

    assert cleaning["classification"]["work_mode"] == "labor_service"
    assert cleaning["classification"]["trade_family"] == "cleaning"
    assert cleaning["classification"]["location_detected"] == "12"
    assert cleaning["selected_gl_code"] == "6750"
    assert "paint & supplies" not in _alt_text(cleaning)

    assert plumbing["classification"]["trade_family"] == "plumbing"
    assert plumbing["classification"]["work_mode"] == "labor_service"
    assert plumbing["classification"]["location_detected"] == "7"
    assert plumbing["selected_gl_code"] == "6565"
    assert "electric - vacant" not in _alt_text(plumbing)
