from webapp.backend.services.accounting_integration_bridges import AIResultAccountingV2Adapter, RowAccountingV2Adapter
from webapp.backend.services.accounting_readiness import evaluate_rows
from webapp.backend.services.gl_payability import is_payable_gl_account


def _row(gl="6530"):
    return {"Invoice Number": "T-1", "Bill or Credit": "Bill", "Invoice Date": "2026-01-01",
            "Accounting Date": "2026-01-01", "Vendor": "Test Vendor",
            "Invoice Description": "Repair", "Line Item Number": 1,
            "Property Abbreviation": "TEST", "GL Account": gl,
            "Line Item Description": "Plumbing repair service", "Amount": 25,
            "Expense Type": "General", "Is Replacement Reserve": False,
            "Document Url": "https://example.invalid/invoice", "_meta": {"source_line_description": "raw plumbing repair"}}


def test_payable_utility_handles_mapping_sequence_and_models():
    catalog = {"6530": {"gl_account_type": "Expense"}, "1100": {"gl_account_type": "Asset"}}
    assert is_payable_gl_account(" 6530 ", catalog)
    assert not is_payable_gl_account("1100", catalog)
    assert not is_payable_gl_account("", catalog)
    assert not is_payable_gl_account("9999", catalog)


def test_real_row_bridge_preserves_source_and_records_engine_decision():
    row = _row()
    RowAccountingV2Adapter().enrich_rows([row], {"document_id": "doc-1"})
    assert row["_meta"]["source_text"]["raw_description"] == "raw plumbing repair"
    assert row["_meta"]["semantic_classification"]["semantic_version"]
    decision = row["_meta"]["accounting_decision"]
    assert decision["decision_source"] == "AccountingDecisionEngine"
    assert row["GL Account"] == decision["selected_gl_code"]


def test_ai_bridge_does_not_accept_invalid_ai_gl_and_readiness_blocks_null():
    row = _row("1100")
    row["_meta"]["ai_source_gl_candidate"] = "1100"
    invoice = AIResultAccountingV2Adapter().convert({"rows": [row]}, {"document_id": "ai-doc"})
    decision = invoice["rows"][0]["_meta"]["accounting_decision"]
    assert decision["selected_gl_code"] != "1100"
    readiness = evaluate_rows(invoice["rows"])
    assert readiness.export_allowed is False
