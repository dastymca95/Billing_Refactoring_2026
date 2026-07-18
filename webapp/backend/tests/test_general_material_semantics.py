from decimal import Decimal

from webapp.backend.services.accounting_contracts import LineItemFacts
from webapp.backend.services.accounting_integration_bridges import RowAccountingV2Adapter
from webapp.backend.services.semantic_classifier import classify_line


def test_tangible_retail_description_is_material_without_vendor_hardcode():
    semantics = classify_line(
        LineItemFacts(line_item_id="1", raw_description="Decorative accessories"),
        document_id="doc-general-material",
    )
    assert semantics.line_family == "materials"
    assert semantics.work_mode == "material_purchase"


def test_vendor_neutral_pos_abbreviations_are_normalized_without_overwriting_raw_text():
    facts = LineItemFacts(line_item_id="1", raw_description="MENS ACC/GFT")
    semantics = classify_line(facts, document_id="doc-pos-abbreviation")
    assert facts.raw_description == "MENS ACC/GFT"
    assert semantics.line_family == "materials"
    assert semantics.work_mode == "material_purchase"


def test_abbreviated_sku_with_physical_unit_and_commercial_values_is_material():
    facts = LineItemFacts(
        line_item_id="1", raw_description="5 GAL X91Q2048 INT SA EXTRA Color: WHITE",
        quantity=Decimal("12"), unit_price=Decimal("31.50"), amount=Decimal("378.00"),
    )
    semantics = classify_line(facts, document_id="doc-abbreviated-product")
    assert semantics.line_family == "materials"
    assert semantics.work_mode == "material_purchase"
    assert any("physical_unit:gal" in str(item.normalized_text) for item in semantics.positive_evidence)


def test_material_line_may_inherit_trade_from_document_context_without_inheriting_work_mode():
    facts = LineItemFacts(
        line_item_id="1", raw_description="5 GAL X91Q2048 INT SA EXTRA Color: WHITE",
        quantity=Decimal("12"), unit_price=Decimal("31.50"), amount=Decimal("378.00"),
    )
    semantics = classify_line(
        facts, document_id="doc-contextual-trade", document_context="Paint products in white color",
    )
    assert semantics.trade_family == "painting"
    assert semantics.line_family == "materials"
    assert semantics.work_mode == "material_purchase"


def test_premium_product_is_not_misclassified_as_insurance():
    facts = LineItemFacts(line_item_id="1", raw_description='Premium 2" cordless faux wood blind - white',
                          quantity=Decimal("3"), unit_price=Decimal("40.71"), amount=Decimal("122.13"))
    semantics = classify_line(facts, document_id="doc-window-covering")
    assert semantics.line_family == "materials"
    assert semantics.trade_family == "window_coverings"
    assert semantics.work_mode == "material_purchase"


def test_premium_means_insurance_only_with_insurance_policy_context():
    facts = LineItemFacts(line_item_id="1", raw_description="Annual insurance policy premium renewal")
    semantics = classify_line(facts, document_id="doc-insurance")
    assert semantics.line_family == "insurance"
    assert semantics.trade_family == "insurance"
    assert semantics.work_mode in {"renewal", "recurring_service"}


def test_row_adapter_generates_engine_selected_candidate_and_metadata_facts():
    row = {
        "Invoice Number": "INV-1", "Bill or Credit": "Bill",
        "Invoice Date": "2026-07-14", "Accounting Date": "2026-07-14",
        "Vendor": "Unmapped merchant", "Invoice Description": "Tangible goods purchase",
        "Line Item Number": 1, "Property Abbreviation": "TEST", "GL Account": "",
        "Amount": 12.34, "Line Item Description": "Decorative accessories",
        "Expense Type": "General", "Is Replacement Reserve": False,
        "Due Date": "2026-08-13", "Document Url": "/private/doc",
        "_meta": {"source_file": "$12.34-Education-Corporate.pdf",
                  "source_line_description": "DECORATIVE ACCESSORIES"},
    }
    RowAccountingV2Adapter().enrich_rows([row], {"document_id": "doc-1"})
    meta = row["_meta"]
    assert meta["source_metadata_candidates"]["authoritative"] is False
    assert meta["source_metadata_candidates"]["source_filename_sha256"]
    assert "original_filename" not in meta["source_metadata_candidates"]
    assert any(
        evidence["extraction_method"] == "filename_folder_parser_non_authoritative"
        for evidence in meta["document_facts"]["evidence"]
    )
    assert meta["semantic_classification"]["work_mode"] == "material_purchase"
    assert meta["accounting_decision"]["decision_source"] == "AccountingDecisionEngine"
    assert row["GL Account"] == meta["accounting_decision"]["selected_gl_code"]


def test_invoice_context_from_another_line_does_not_assign_trade_or_unknown_catalog_accounts():
    row = {
        "Invoice Number": "INV-2", "Bill or Credit": "Bill",
        "Invoice Date": "2026-07-14", "Accounting Date": "2026-07-14",
        "Vendor": "Unmapped merchant", "Invoice Description": "Stationery and unrelated goods",
        "Line Item Number": 1, "Property Abbreviation": "TEST", "GL Account": "",
        "Amount": 10, "Line Item Description": "Unrecognized SKU 123",
        "Expense Type": "General", "Is Replacement Reserve": False,
        "Due Date": "2026-08-13", "Document Url": "/private/doc",
        "_meta": {"source_line_description": "UNRECOGNIZED SKU 123"},
    }
    RowAccountingV2Adapter().enrich_rows([row], {"document_id": "doc-2"})
    decision = row["_meta"]["accounting_decision"]
    assert row["_meta"]["semantic_classification"]["trade_family"] == "unknown"
    assert row["GL Account"] == ""
    assert decision["candidates_ranked"] == []
    assert decision["review_blocking"] is True
