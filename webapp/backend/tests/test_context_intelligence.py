from __future__ import annotations

from pathlib import Path

import pytest

from webapp.backend import settings
from webapp.backend.services import context_intelligence as intelligence
from webapp.backend.services import resman_context_data as hub
from webapp.backend.services import accounting_pipeline_v2, gl_catalog


VENDORS = b"""Vendor List,,,,\nCompany,Company Abbreviation,Customer #,Status,Active\nExample Utility,EU,1,Approved,Yes\n"""
UNITS = b"""All Units,,,,\nExample Property,,,,\nUnit,Unit Type,Unit Status,Sq Ft,Lease Status\n101,1B1B,Ready,700,Current\n"""
CHART = b"""Chart Of Accounts,,,\nNumber,Name,Type,Description\n6100,Utility Expense,Expense,Recurring utility\n6200,Repair Expense,Expense,Repairs\n"""
LEDGER = b"""General Ledger,,,,,,,,\nDate,Reference,Property,Name,Description,Debit,Credit,Balance\n6100 Utility Expense,,,Beginning Balance:,,,,0.00\n01/15/2026,I-1,EP,Example Utility,Service,100.00,,100.00\n"""


def invoice_detail() -> bytes:
    rows = [
        "Invoice Detail,,,,,,,,,",
        'Number,"Invoice Date /\nProperty",,"Act. Date /\nGL Account",Due Date,Description,,Total,Batch,PO',
        "Example Utility,,,,,,,,,",
    ]
    for month in range(1, 7):
        rows.extend([
            f"I-{month},0{month}/01/2026,,0{month}/15/2026,0{month}/28/2026,Monthly service,,100.00,,",
            ",EP,6100,,Monthly service,,100.00,,,,",
        ])
    return ("\n".join(rows) + "\n").encode()


@pytest.fixture()
def isolated_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "WEBAPP_DATA_ROOT", tmp_path / "runtime")
    return tmp_path


def _publish_all(tenant: str = "tenant-a") -> None:
    for dataset, content in (
        (hub.DatasetKind.VENDORS, VENDORS),
        (hub.DatasetKind.PROPERTIES_UNITS, UNITS),
        (hub.DatasetKind.GL_ACCOUNTS, CHART),
        (hub.DatasetKind.GENERAL_LEDGER, LEDGER),
        (hub.DatasetKind.INVOICE_HISTORY, invoice_detail()),
    ):
        preview = hub.stage_import(tenant, dataset, f"{dataset.value}.csv", content)
        assert preview.status == "preview_ready", preview.issues
        hub.publish_import(tenant, dataset, preview.import_id)


def test_scan_is_explicit_versioned_and_builds_cross_report_profiles(isolated_context: Path):
    assert intelligence.status("tenant-a")["state"] == "not_generated"
    with pytest.raises(ValueError, match="missing published datasets"):
        intelligence.scan_resman("tenant-a")
    _publish_all()

    report = intelligence.scan_resman("tenant-a", actor="tester")
    profile = next(item for item in report.vendors if item.vendor_name == "Example Utility")
    assert report.invoice_count == 6
    assert report.allocation_count == 6
    assert report.gl_account_count == 2
    assert report.ledger_record_count == 1
    assert len(report.source_hashes) == 5
    assert profile.gl_usage[0].key == "6100"
    assert profile.gl_usage[0].label == "Utility Expense"
    assert profile.ledger_posting_count == 1
    assert profile.ledger_total_amount == "100.00"
    assert profile.gl_usage[0].count == 6
    assert profile.property_usage[0].label == "EP"
    assert profile.recommended_mode == "deterministic_candidate"
    assert profile.governance_status == "unreviewed"
    assert report.audit[-1]["rules_activated"] is False
    assert intelligence.status("tenant-a")["state"] == "ready"


def test_historical_evidence_is_candidate_only_and_human_exclusion_is_auditable(isolated_context: Path):
    _publish_all()
    report = intelligence.scan_resman("tenant-a")
    profile = next(item for item in report.vendors if item.vendor_name == "Example Utility")
    evidence = intelligence.historical_gl_evidence("tenant-a", "Example Utility", "EP")
    assert evidence[0]["gl_code"] == "6100"
    assert evidence[0]["authoritative"] is False

    updated = intelligence.update_vendor_governance(
        "tenant-a", profile.vendor_key,
        intelligence.GovernanceUpdate(
            governance_status="excluded", reviewer_notes="Pattern needs human handling", actor="reviewer",
        ),
    )
    assert updated.governance_status == "excluded"
    assert intelligence.historical_gl_evidence("tenant-a", "Example Utility", "EP") == []

    regenerated = intelligence.scan_resman("tenant-a", actor="reviewer")
    preserved = next(item for item in regenerated.vendors if item.vendor_key == profile.vendor_key)
    assert preserved.governance_status == "excluded"
    assert any(event["event"] == "vendor_governance_updated" for event in regenerated.audit)


def test_source_change_marks_matrix_stale(isolated_context: Path):
    _publish_all()
    intelligence.scan_resman("tenant-a")
    changed = VENDORS.replace(b"Approved", b"Reviewed")
    preview = hub.stage_import("tenant-a", hub.DatasetKind.VENDORS, "vendors-new.csv", changed)
    hub.publish_import("tenant-a", hub.DatasetKind.VENDORS, preview.import_id)
    assert intelligence.status("tenant-a")["state"] == "stale"
    assert intelligence.historical_gl_evidence("tenant-a", "Example Utility") == []


def test_matrix_pagination_search_and_property_dimension(isolated_context: Path):
    _publish_all()
    intelligence.scan_resman("tenant-a")
    vendors = intelligence.list_matrix("tenant-a", dimension="vendors", search="utility")
    properties = intelligence.list_matrix("tenant-a", dimension="properties", search="EP")
    assert vendors.total == 1
    assert vendors.items[0]["vendor_name"] == "Example Utility"
    assert properties.total >= 1
    assert properties.items[0]["gl_usage"][0]["key"] == "6100"


def test_current_matrix_enters_pipeline_as_candidate_only(
    isolated_context: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("INNER_VIEW_TENANT_ID", "tenant-a")
    _publish_all()
    intelligence.scan_resman("tenant-a")
    gl_catalog.load_gl_catalog.cache_clear()
    row = {
        "Vendor": "Example Utility",
        "Property Abbreviation": "EP",
        "Invoice Number": "NEW-1",
        "Invoice Date": "2026-07-01",
        "Line Item Description": "Monthly utility service",
        "Amount": "100.00",
        "_meta": {
            "tenant_id": "tenant-a",
            "source_text": {"raw_description": "Monthly utility service"},
        },
    }
    decision = accounting_pipeline_v2.decide_row(
        row, document_id="doc-1", line_item_id="line-1", extraction_route="test",
    )
    historical = [item for item in decision.candidates_ranked if item.source == "context_intelligence_history"]
    assert historical
    assert historical[0].gl_code == "6100"
    assert historical[0].positive_evidence[0]["selection_authority"] is False
    assert row["_meta"]["context_intelligence_evidence"][0]["authoritative"] is False
    gl_catalog.load_gl_catalog.cache_clear()
