from pathlib import Path

import pytest

from webapp.backend.services.accounting_readiness import evaluate_rows
from webapp.backend.services.validated_export_bridge import ExportAuthorizationError, ReadinessValidatedExporter


def row(gl="6530", amount=10):
    return {"Invoice Number": "E-1", "Bill or Credit": "Bill", "Invoice Date": "2026-01-01",
            "Accounting Date": "2026-01-01", "Vendor": "Vendor", "Invoice Description": "Repair",
            "Line Item Number": 1, "Property Abbreviation": "TEST", "GL Account": gl,
            "Line Item Description": "Repair", "Amount": amount, "Expense Type": "General",
            "Is Replacement Reserve": False, "Due Date": "2026-01-31",
            "Document Url": "https://example.invalid/doc"}


def test_valid_rows_write_only_after_authorization(tmp_path):
    called = []
    rows = [row()]
    snapshot = evaluate_rows(rows)
    result, readiness = ReadinessValidatedExporter().export("batch", rows,
        {"snapshot_id": snapshot.snapshot_id}, tmp_path / "out.xlsx",
        lambda path, values: called.append((path, values)) or 1)
    assert result == 1 and called and readiness["export_allowed"] is True


@pytest.mark.parametrize("rows", [[row("")], [row(amount="bad")]])
def test_invalid_export_rows_are_blocked(rows):
    with pytest.raises(ExportAuthorizationError, match="accounting_readiness_blocked"):
        ReadinessValidatedExporter().authorize("batch", rows)


def test_stale_snapshot_is_blocked():
    with pytest.raises(ExportAuthorizationError, match="stale_readiness_snapshot"):
        ReadinessValidatedExporter().authorize("batch", [row()], "old-snapshot")
