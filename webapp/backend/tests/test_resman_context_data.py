from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from webapp.backend import settings
from webapp.backend.main import create_app
from webapp.backend.services import resman_context_data as hub
from webapp.backend.services import gl_catalog
from utils import canonical_vendors


VENDOR_REPORT = b'''"Property A, Property B",,,,,,,,,,,,,\nTenant LLC,,,,,,,,,,,,\nVendor List,,,,,,,,,,,,\nPrinted 7/15/2026,,,,,,,,,,,,\nCompany,Company Abbreviation,Customer #,Status,Active,General Contact,General Address,General City,General State,General Zip,General Work Phone,General Email,ACH Routing #,ACH Account #,Recipient ID,Workflow,Insur Type,Insur Provider,Insur Policy #,Insur Coverage,Insur Expiration,Default GL\nAcme Services,ACME,42,Approved,Yes,Office,1 Main St,Nashville,TN,37000,555-0100,office@example.test,SECRET-ROUTING,SECRET-ACCOUNT,SECRET-TIN,General,GeneralLiability,Carrier,ABC123,"1,000,000.00",12/31/2026,6500\nAcme Services,ACME,,,,,,,,,,,,,,,WorkersCompensation,Carrier 2,WC456,"2,000,000.00",01/31/2027,\n'''

VENDOR_REPORT_UPDATED = VENDOR_REPORT.replace(b"Approved,Yes", b"Reviewed,Yes")

UNITS_REPORT = b'''Property A,Property B,,,,\nTenant LLC,,,,\nAll Units,,,,\n7/15/2026,,,,\nProperty A,,,,\nUnit,Unit Type,Unit Status,Sq Ft,Lease Status,Residents,Lease Start,Lease End,Market Rent\n101,1B1B,Ready,700,C,Private Resident,01/01/2026,12/31/2026,950.00\n102,2B1B,Not Ready,900,,,,,1100.00\n'''

GL_REPORT = b''',,,\nChart Of Accounts,,,\nPrinted 7/15/2026,,,\nNumber,Name,Type,Description\n6500,Repairs,Expense,Repair expense\n1100,Cash,Bank,Operating cash\n'''

LEDGER_REPORT = b'''Property A,,,,,,,,\nTenant LLC,,,,,,,,\nGeneral Ledger,,,,,,,,\nJanuary 2026 - July 2026,,,,,,,,\nPrinted 7/15/2026,,,,,,,,\nDate,Reference,Property,Name,Description,Debit,Credit,Balance,\n6500 Repairs,,,Beginning Balance:,,,,,0.00\n01/02/2026,INV-1,PA,Acme Services,Repair,125.00,,125.00,\n01/03/2026,INV-2,PA,Acme Services,Refund,,25.00,100.00,\n'''

INVOICE_DETAIL_REPORT = b'''Property A,,,,,,,,,\nTenant LLC,,,,,,,,,\nInvoice Detail,,,,,,,,,\n01/01/2026 - 07/15/2026,,,,,,,,,\nPrinted 07/15/2026,,,,,,,,,\nNumber,"Invoice Date /\nProperty",,"Act. Date /\nGL Account",Due Date,Description,,Total,Batch,PO\nAcme Services,,,,,,,,,\nINV-1,01/01/2026,,01/02/2026,02/01/2026,Repair invoice,,125.00,,PO-7\n,PA,6500,,Labor and materials,,125.00,,,\nAcme Services,,,,,,,,,\nINV-1,01/04/2026,,01/05/2026,02/04/2026,Second occurrence,,50.00,,,\n,PA,6500,,Follow-up repair,,50.00,,,\n'''


@pytest.fixture()
def isolated_hub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "WEBAPP_DATA_ROOT", tmp_path / "runtime")
    yield tmp_path


def test_vendor_import_preserves_raw_excludes_sensitive_and_merges_continuations(isolated_hub: Path):
    preview = hub.stage_import("tenant-a", hub.DatasetKind.VENDORS, "Vendor List.csv", VENDOR_REPORT)

    assert preview.status == "preview_ready"
    assert preview.parsed_records == 1
    assert "ACH Routing #" in preview.excluded_sensitive_columns
    sample = preview.sample_records[0]
    assert "SECRET-ROUTING" not in str(sample)
    assert "SECRET-ACCOUNT" not in str(sample)
    assert "SECRET-TIN" not in str(sample)
    assert len(sample["insurances"]) == 2

    snapshot = hub.publish_import("tenant-a", hub.DatasetKind.VENDORS, preview.import_id)
    raw_files = list((settings.WEBAPP_DATA_ROOT / "resman_context" / "tenant-a" / "raw" / "vendors").glob("*.csv"))
    assert len(raw_files) == 1
    assert raw_files[0].read_bytes() == VENDOR_REPORT
    assert snapshot.sha256 == preview.sha256


def test_manual_patch_survives_reimport_without_hiding_other_resman_changes(isolated_hub: Path):
    first = hub.stage_import("tenant-a", hub.DatasetKind.VENDORS, "vendors.csv", VENDOR_REPORT)
    first_snapshot = hub.publish_import("tenant-a", hub.DatasetKind.VENDORS, first.import_id)
    hub.update_record(
        "tenant-a", hub.DatasetKind.VENDORS, "vendor:acme",
        {"default_gl": "6600"}, actor="reviewer",
    )

    second = hub.stage_import("tenant-a", hub.DatasetKind.VENDORS, "vendors-new.csv", VENDOR_REPORT_UPDATED)
    second_snapshot = hub.publish_import("tenant-a", hub.DatasetKind.VENDORS, second.import_id)
    record = hub.list_records("tenant-a", hub.DatasetKind.VENDORS).items[0]
    assert record["default_gl"] == "6600"
    assert record["status"] == "Reviewed"
    assert record["_record"]["source_kind"] == "manual_overlay"

    rolled_back = hub.activate_snapshot(
        "tenant-a", hub.DatasetKind.VENDORS, first_snapshot.snapshot_id, actor="reviewer",
    )
    assert rolled_back.active is True
    assert second_snapshot.snapshot_id != rolled_back.snapshot_id
    prior_record = hub.list_records("tenant-a", hub.DatasetKind.VENDORS).items[0]
    assert prior_record["default_gl"] == "6600"
    assert prior_record["status"] == "Approved"


@pytest.mark.parametrize(
    ("dataset", "content", "expected"),
    [
        (hub.DatasetKind.PROPERTIES_UNITS, UNITS_REPORT, 3),
        (hub.DatasetKind.GL_ACCOUNTS, GL_REPORT, 2),
        (hub.DatasetKind.GENERAL_LEDGER, LEDGER_REPORT, 2),
        (hub.DatasetKind.INVOICE_HISTORY, INVOICE_DETAIL_REPORT, 2),
    ],
)
def test_resman_report_shapes_are_normalized(isolated_hub: Path, dataset: hub.DatasetKind, content: bytes, expected: int):
    preview = hub.stage_import("tenant-a", dataset, f"{dataset.value}.csv", content)
    assert preview.status == "preview_ready"
    assert preview.parsed_records == expected
    snapshot = hub.publish_import("tenant-a", dataset, preview.import_id)
    assert snapshot.record_count == expected

    records = hub.list_records("tenant-a", dataset, page_size=10)
    assert records.total == expected
    if dataset is hub.DatasetKind.PROPERTIES_UNITS:
        assert "Private Resident" not in str(records.items)
    if dataset is hub.DatasetKind.GL_ACCOUNTS:
        payable = {item["gl_code"]: item["payable"] for item in records.items}
        assert payable == {"1100": False, "6500": True}
    if dataset is hub.DatasetKind.INVOICE_HISTORY:
        assert {item["invoice_occurrence_id"] for item in records.items}.__len__() == 2
        assert all(item["invoice_reconciliation_status"] == "reconciled" for item in records.items)


def test_invoice_history_reconciles_exactly_without_selecting_or_overwriting_gl(isolated_hub: Path):
    vendor = hub.stage_import("tenant-a", hub.DatasetKind.VENDORS, "vendors.csv", VENDOR_REPORT)
    hub.publish_import("tenant-a", hub.DatasetKind.VENDORS, vendor.import_id)
    hub.create_record(
        "tenant-a", hub.DatasetKind.PROPERTIES_UNITS,
        {"entity_type": "property", "property_name": "Property A", "property_code": "PA", "active": True},
        actor="reviewer",
    )
    chart = hub.stage_import("tenant-a", hub.DatasetKind.GL_ACCOUNTS, "chart.csv", GL_REPORT)
    hub.publish_import("tenant-a", hub.DatasetKind.GL_ACCOUNTS, chart.import_id)
    ledger = hub.stage_import("tenant-a", hub.DatasetKind.GENERAL_LEDGER, "ledger.csv", LEDGER_REPORT)
    hub.publish_import("tenant-a", hub.DatasetKind.GENERAL_LEDGER, ledger.import_id)
    invoice = hub.stage_import(
        "tenant-a", hub.DatasetKind.INVOICE_HISTORY, "Invoice Detail.csv", INVOICE_DETAIL_REPORT,
    )
    hub.publish_import("tenant-a", hub.DatasetKind.INVOICE_HISTORY, invoice.import_id)

    rows = hub.list_records("tenant-a", hub.DatasetKind.INVOICE_HISTORY, page_size=10).items
    matched = next(row for row in rows if row["invoice_total"] == "125.00")
    unmatched = next(row for row in rows if row["invoice_total"] == "50.00")
    assert matched["ledger_reconciliation_status"] == "matched_to_ledger"
    assert matched["vendor_validation_status"] == "exact"
    assert matched["property_valid"] is True
    assert matched["gl_valid"] is True
    assert matched["gl_payable"] is True
    assert matched["gl_code"] == "6500"
    assert matched["reference_validation_evidence"]["authoritative_for_gl_selection"] is False
    assert unmatched["ledger_reconciliation_status"] == "invoice_only"
    ledger_rows = hub.list_records("tenant-a", hub.DatasetKind.GENERAL_LEDGER, page_size=10).items
    matched_ledger = next(row for row in ledger_rows if row["reference"] == "INV-1")
    ledger_only = next(row for row in ledger_rows if row["reference"] == "INV-2")
    assert matched_ledger["invoice_history_reconciliation_status"] == "matched_to_invoice_history"
    assert ledger_only["invoice_history_reconciliation_status"] == "ledger_only"


def test_invoice_total_mismatch_is_visible_and_duplicate_numbers_are_not_collapsed(isolated_hub: Path):
    mismatched = INVOICE_DETAIL_REPORT.replace(b"Follow-up repair,,50.00", b"Follow-up repair,,49.00")
    preview = hub.stage_import(
        "tenant-a", hub.DatasetKind.INVOICE_HISTORY, "Invoice Detail.csv", mismatched,
    )
    assert preview.parsed_records == 2
    assert any(issue.code == "invoice_allocation_total_mismatch" for issue in preview.issues)
    hub.publish_import("tenant-a", hub.DatasetKind.INVOICE_HISTORY, preview.import_id)
    rows = hub.list_records("tenant-a", hub.DatasetKind.INVOICE_HISTORY, page_size=10).items
    assert len(rows) == 2
    assert len({row["invoice_occurrence_id"] for row in rows}) == 2
    assert any(row["invoice_reconciliation_status"] == "total_mismatch" for row in rows)


@pytest.mark.parametrize(
    ("ledger_patch", "expected"),
    [
        ({}, "matched_to_ledger"),
        ({"transaction_date": "2026-01-03"}, "posting_date_difference"),
        ({"debit": "124.00"}, "amount_mismatch"),
        ({"account_code": "6501"}, "gl_mismatch"),
        ({"property_code": "PB"}, "property_mismatch"),
    ],
)
def test_reconciliation_classifies_one_dimension_differences(ledger_patch: dict, expected: str):
    invoice = {
        "vendor_name": "Acme Services", "invoice_number": "INV-1",
        "accounting_date": "2026-01-02", "property_code": "PA",
        "gl_code": "6500", "allocation_amount": "125.00",
    }
    ledger = {
        "counterparty_name": "Acme Services", "reference": "INV-1",
        "transaction_date": "2026-01-02", "property_code": "PA",
        "account_code": "6500", "debit": "125.00", "credit": None,
    }
    status, evidence = hub._reconcile_invoice_allocation(invoice, [{**ledger, **ledger_patch}])
    assert status.value == expected
    assert evidence[0]["match_type"] == expected


def test_crud_is_tenant_scoped_and_delete_is_auditable(isolated_hub: Path):
    created = hub.create_record(
        "tenant-a", hub.DatasetKind.VENDORS,
        {"company": "Manual Vendor", "abbreviation": "MV", "active": True},
        actor="reviewer",
    )
    assert created["_record"]["source_kind"] == "manual_overlay"
    assert hub.list_records("tenant-a", hub.DatasetKind.VENDORS).total == 1
    assert hub.list_records("tenant-b", hub.DatasetKind.VENDORS).total == 0

    result = hub.delete_record(
        "tenant-a", hub.DatasetKind.VENDORS,
        created["_record"]["natural_key"], actor="reviewer",
    )
    assert result == {
        "deleted": True,
        "natural_key": "vendor:mv",
        "audit_preserved": True,
    }
    assert hub.list_records("tenant-a", hub.DatasetKind.VENDORS).total == 0


def test_resman_context_api_preview_publish_and_pagination(isolated_hub: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("INNER_VIEW_TENANT_ID", "tenant-a")
    client = TestClient(create_app())

    preview_response = client.post(
        "/api/resman-context/gl_accounts/imports/preview",
        files={"file": ("Chart Of Accounts.csv", GL_REPORT, "text/csv")},
    )
    assert preview_response.status_code == 200
    preview = preview_response.json()
    assert preview["parsed_records"] == 2

    publish_response = client.post(
        f"/api/resman-context/gl_accounts/imports/{preview['import_id']}/publish",
        json={"actor": "reviewer"},
    )
    assert publish_response.status_code == 200
    records = client.get("/api/resman-context/gl_accounts/records?page=1&page_size=1")
    assert records.status_code == 200
    assert records.json()["total"] == 2
    assert len(records.json()["items"]) == 1


def test_published_vendor_and_chart_feed_backward_compatible_adapters(
    isolated_hub: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("INNER_VIEW_TENANT_ID", "tenant-a")
    vendor = hub.stage_import("tenant-a", hub.DatasetKind.VENDORS, "vendors.csv", VENDOR_REPORT)
    hub.publish_import("tenant-a", hub.DatasetKind.VENDORS, vendor.import_id)
    canonical_vendors._CACHE = None
    assert canonical_vendors.canonical_vendor_name(vendor_key="ACME") == "Acme Services"

    imported_chart = b'''Chart Of Accounts,,,\nNumber,Name,Type,Description\n9876,Imported Specialist Expense,Expense,Published tenant chart account\n'''
    chart = hub.stage_import("tenant-a", hub.DatasetKind.GL_ACCOUNTS, "chart.csv", imported_chart)
    hub.publish_import("tenant-a", hub.DatasetKind.GL_ACCOUNTS, chart.import_id)
    legacy_chart = isolated_hub / "legacy-chart.csv"
    legacy_chart.write_text("Number,Name,Type,Description\n1100,Cash,Bank,\n", encoding="utf-8")
    monkeypatch.setattr(gl_catalog, "CHART_PATH", legacy_chart)
    gl_catalog.load_gl_catalog.cache_clear()
    version, catalog = gl_catalog.load_gl_catalog()
    assert "9876" in catalog
    assert catalog["9876"].payable is True
    assert "+resman-" in version
    gl_catalog.load_gl_catalog.cache_clear()


def test_ledger_keeps_source_name_and_adds_only_exact_vendor_resolution(isolated_hub: Path):
    vendor = hub.stage_import("tenant-a", hub.DatasetKind.VENDORS, "vendors.csv", VENDOR_REPORT)
    hub.publish_import("tenant-a", hub.DatasetKind.VENDORS, vendor.import_id)
    ledger_source = LEDGER_REPORT + b"01/04/2026,REF-3,PA,Summary - 1/4/2026,3 Entries,10.00,,110.00,\n"
    ledger = hub.stage_import(
        "tenant-a", hub.DatasetKind.GENERAL_LEDGER, "ledger.csv", ledger_source,
    )
    hub.publish_import("tenant-a", hub.DatasetKind.GENERAL_LEDGER, ledger.import_id)

    rows = hub.list_records(
        "tenant-a", hub.DatasetKind.GENERAL_LEDGER, page_size=20,
    ).items
    exact = [row for row in rows if row["counterparty_name"] == "Acme Services"]
    unresolved = next(row for row in rows if row["counterparty_name"].startswith("Summary"))
    assert exact
    assert all(row["resolved_vendor_name"] == "Acme Services" for row in exact)
    assert all(row["vendor_resolution_status"] == "exact" for row in exact)
    assert unresolved["resolved_vendor_name"] is None
    assert unresolved["vendor_resolution_status"] == "unresolved"
    assert unresolved["counterparty_name"] == "Summary - 1/4/2026"


def test_non_unique_vendor_name_is_ambiguous_not_guessed(isolated_hub: Path):
    hub.create_record(
        "tenant-a", hub.DatasetKind.VENDORS,
        {"company": "Shared Counterparty", "abbreviation": "SHARED-A", "active": True},
        actor="reviewer",
    )
    hub.create_record(
        "tenant-a", hub.DatasetKind.VENDORS,
        {"company": "Shared Counterparty", "abbreviation": "SHARED-B", "active": True},
        actor="reviewer",
    )

    result = hub.resolve_ledger_vendor("tenant-a", "Shared Counterparty")
    assert result["vendor_resolution_status"] == "ambiguous"
    assert result["resolved_vendor_name"] is None
    assert result["vendor_resolution_evidence"][0]["authoritative"] is False
