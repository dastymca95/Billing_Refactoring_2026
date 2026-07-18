from decimal import Decimal

from webapp.backend.services.accounting_contracts import DocumentFacts, GLCandidate, GLAccountMetadata, LineItemFacts
from webapp.backend.services.accounting_decision_engine import AccountingDecisionEngine
from webapp.backend.services.ai_invoice_processor import (
    _extract_allocated_insurance_rows,
    _extract_property_placed_insurance_payload,
    _repair_ai_payload_from_ocr,
)
from webapp.backend.services.gl_catalog import load_gl_catalog
from webapp.backend.services.semantic_classifier import classify_line


SOURCE = """
PROPERTY PLACED INSURANCE
ACCOUNT NAME COVERAGE # FIRST NAME LAST NAME PROPERTY ADDRESS CITY STATE ZIP COVERAGE START COVERAGE END PREMIUM SURCHARGE TOTAL
Example Place COVX10001 * RESIDENT LLC 100 Main Street 101 Metro TN 37000 6/1/2026 6/30/2026 $8.00 $0.40 $8.40
Example Place COVX10002 Jane Doe 100 Main Street 102 Metro TN 37000 6/15/2026 6/30/2026 $6.29 $0.31 $6.60
EXAMPLE PLACE TOTAL: $15.00
INVOICE TOTAL: $17.00
* There has been a $2.00 processing fee added to the invoice total.
"""


def test_explicit_insurance_allocations_are_never_grouped():
    rows, total = _extract_allocated_insurance_rows(SOURCE)
    assert total == 15.00
    assert [row["location_candidate"] for row in rows] == ["101", "102"]
    assert [row["amount"] for row in rows] == [8.40, 6.60]
    assert len({row["row_label"] for row in rows}) == 2


def test_document_family_parser_returns_allocations_and_explicit_fee_without_ai():
    payload = _extract_property_placed_insurance_payload(SOURCE)
    assert payload["_local_parser"] == "property_placed_insurance_table"
    assert len(payload["line_items"]) == 3
    assert payload["invoice_date"] == ""
    assert payload["subtotal"] == 15.00
    assert payload["total_amount"] == 17.00
    assert payload["line_items"][-1]["activity"] == "Processing fee"
    assert payload["needs_manual_review"] is True


def test_ocr_repair_replaces_grouped_summary_and_makes_fee_an_explicit_row():
    grouped = {"invoice_number": "INV", "total_amount": 17.0, "subtotal": 15.0,
        "fees_amount": 2.0, "line_items": [{"description": "Two insurance units", "amount": 15.0}]}
    repaired = _repair_ai_payload_from_ocr(grouped, SOURCE)
    assert len(repaired["line_items"]) == 3
    assert repaired["fees_amount"] == 0.0
    assert sum(row["amount"] for row in repaired["line_items"]) == 17.0
    assert repaired["line_items"][-1]["activity"] == "Processing fee"


def _decision(text: str, codes: list[str]):
    line = LineItemFacts(line_item_id="1", raw_description=text, amount=Decimal("10"))
    facts = DocumentFacts(document_id="doc", invoice_id="inv", line_items=[line], extraction_route="test")
    semantics = classify_line(line, document_id="doc")
    _, catalog = load_gl_catalog()
    catalog = dict(catalog)
    catalog.setdefault("6171", GLAccountMetadata(gl_code="6171", gl_name="Renters Insurance Cost",
        gl_family="renters_insurance", trade_families=["renters_insurance"],
        compatible_work_modes=["recurring_service", "renewal"], specificity="specific", payable=True,
        description_tokens=["renters", "insurance", "cost"], metadata_source="chart+approved_config", metadata_confidence=.98))
    catalog.setdefault("6173", GLAccountMetadata(gl_code="6173", gl_name="Service Charges/Convenience Fees",
        gl_family="fee", trade_families=["processing_fee", "admin"], compatible_work_modes=["one_time_fee"],
        specificity="specific", payable=True, description_tokens=["service", "charges", "convenience", "fees"],
        metadata_source="chart+approved_config", metadata_confidence=.98))
    candidates = [GLCandidate(gl_code=code, gl_name=catalog[code].gl_name,
        source="service_reasoning_candidate", base_score=.75) for code in codes]
    return semantics, AccountingDecisionEngine().decide(facts, semantics, catalog, candidates, {})


def test_property_placed_insurance_prefers_specific_renters_insurance_cost():
    semantics, decision = _decision("Property placed insurance - unit 101", ["6171", "7120"])
    assert semantics.line_family == "insurance"
    assert semantics.trade_family == "renters_insurance"
    assert semantics.work_mode == "recurring_service"
    assert decision.selected_gl_code == "6171"


def test_processing_fee_prefers_convenience_fee_not_connect_fee():
    semantics, decision = _decision("Processing fee added to invoice total", ["6173", "6956"])
    assert semantics.line_family == "fee"
    assert semantics.trade_family == "processing_fee"
    assert semantics.work_mode == "one_time_fee"
    assert decision.selected_gl_code == "6173"
