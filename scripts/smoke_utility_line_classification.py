from __future__ import annotations

import shutil
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp.backend.services.output_contract_validator import validate_row_contract  # noqa: E402
from webapp.backend.services.utility_processor_common import (  # noqa: E402
    UtilityChargeLine,
    allocate_tax_proportionally,
    classify_utility_line,
    default_gl_for_line,
    load_chart_of_accounts,
    validate_utility_template_rows,
)
from webapp.backend.services.utility_wave3_processors import process_tennessee_american_water_batch  # noqa: E402


TEMPLATE_PATH = ROOT / "Output" / "Template.xlsx"
TAW_FIRE_SAMPLE = (
    ROOT
    / "Training Bills_Invoices"
    / "Water - Sewer"
    / "Tennessee American Water"
    / "Bills_Training"
    / "1026-210052442136 Apr 26.pdf"
)


def main() -> int:
    failures: list[str] = []
    valid_gls = load_chart_of_accounts()

    _assert_classifier_contract(failures)
    _assert_tax_contract(failures)
    _assert_row_validator_contract(failures, valid_gls)
    _assert_tennessee_fire_service(failures)

    if failures:
        print("FAIL:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("PASS: utility line classification, fee GL, fire service, and trash/sanitation rules are valid.")
    return 0


def _assert_classifier_contract(failures: list[str]) -> None:
    cases = {
        "Connect Fee": ("connection_fee", "6956"),
        "Reconnection Charge": ("connection_fee", "6956"),
        "Recon Chg": ("connection_fee", "6956"),
        "Late Payment Charge": ("late_fee", "6955"),
        "Fire Protection Service": ("fire_protection_service", "6860"),
        "Private Fire Service Charge": ("fire_protection_service", "6860"),
        "Sprinkler Service": ("fire_protection_service", "6860"),
        "Sanitation": ("trash_service", "6940"),
        "Trash Service": ("trash_service", "6940"),
        "Stormwater User Fee": ("stormwater_service", "6995"),
        "Storm Water": ("stormwater_service", "6995"),
        "Water Service": ("water_service", "6955"),
    }
    cfg = {
        "accounting_mapping": {"default_gl_code": "6955"},
        "utility_processing": {"fire_service_rules": {"gl_account": "6860"}},
    }
    for label, (expected_class, expected_gl) in cases.items():
        actual_class = classify_utility_line(label)
        actual_gl = default_gl_for_line(label, vendor_config=cfg)
        if actual_class != expected_class:
            failures.append(f"{label}: expected class {expected_class}, got {actual_class}")
        if expected_gl and actual_gl != expected_gl:
            failures.append(f"{label}: expected GL {expected_gl}, got {actual_gl}")
    if default_gl_for_line("Late Fee", vendor_config={"accounting_mapping": {"default_gl_code": "6955"}}) == "6956":
        failures.append("late fee mapped to GL 6956")


def _assert_tax_contract(failures: list[str]) -> None:
    lines = [
        UtilityChargeLine("Water Service", "100.00", gl_account="6955"),
        UtilityChargeLine("Reconnection Charge", "25.00", line_type="connection_fee", gl_account="6956", taxable=False),
    ]
    allocated = allocate_tax_proportionally(lines, Decimal("10.00"))
    if allocated.lines[0].money != Decimal("110.00"):
        failures.append(f"tax should allocate only to taxable service line, got {allocated.lines[0].money}")
    if allocated.lines[1].money != Decimal("25.00"):
        failures.append("connection fee should not receive proportional tax allocation")


def _assert_row_validator_contract(failures: list[str], valid_gls: dict[str, str]) -> None:
    base_row = {
        "Invoice Number": "100 Apr 26",
        "Bill or Credit": "Bill",
        "Invoice Date": "04/30/2026",
        "Accounting Date": "04/30/2026",
        "Vendor": "Utility Vendor",
        "Invoice Description": "04/01/26-04/30/26 - 100 Main St",
        "Line Item Number": "1",
        "Property Abbreviation": "TST",
        "Location": "",
        "GL Account": "6955",
        "Line Item Description": "04/01/26-04/30/26 - 100 Main St - Water Service",
        "Amount": "100.00",
        "Expense Type": "General",
        "Is Replacement Reserve": "false",
        "Due Date": "05/30/2026",
        "_meta": {"service_address": "100 Main St"},
    }
    connection_wrong = dict(base_row)
    connection_wrong["Line Item Description"] = "04/01/26-04/30/26 - 100 Main St - Connection Fee"
    if "connection_fee_wrong_gl" not in validate_row_contract(connection_wrong, valid_gl_accounts=valid_gls):
        failures.append("connection fee with non-6956 GL did not block")

    connection_good = dict(connection_wrong)
    connection_good["GL Account"] = "6956"
    if "connection_fee_wrong_gl" in validate_row_contract(connection_good, valid_gl_accounts=valid_gls):
        failures.append("connection fee with GL 6956 was incorrectly blocked")

    late_bad = dict(base_row)
    late_bad["GL Account"] = "6956"
    late_bad["Line Item Description"] = "04/01/26-04/30/26 - 100 Main St - Late Payment Charge"
    if "late_fee_wrong_connect_gl" not in validate_row_contract(late_bad, valid_gl_accounts=valid_gls):
        failures.append("late fee with GL 6956 did not block")

    fire_bad = dict(base_row)
    fire_bad["Line Item Description"] = "04/01/26-04/30/26 - 100 Main St - Fire Protection Service"
    fire_bad["_meta"] = {"service_address": "100 Main St", "line_classification": "fire_protection_service"}
    if "fire_service_mapped_as_water" not in validate_row_contract(fire_bad, valid_gl_accounts=valid_gls):
        failures.append("fire service mapped as water did not block")

    trash_bad = dict(base_row)
    trash_bad["Line Item Description"] = "04/01/26-04/30/26 - 100 Main St - Sanitation"
    if "trash_service_wrong_gl" not in validate_row_contract(trash_bad, valid_gl_accounts=valid_gls):
        failures.append("trash/sanitation service with water GL did not block")


def _assert_tennessee_fire_service(failures: list[str]) -> None:
    if not TAW_FIRE_SAMPLE.is_file():
        failures.append(f"Tennessee American Water fire-service sample missing: {TAW_FIRE_SAMPLE}")
        return
    if not TEMPLATE_PATH.is_file():
        failures.append("Output/Template.xlsx missing")
        return
    before = TEMPLATE_PATH.stat().st_mtime_ns
    with tempfile.TemporaryDirectory(prefix="qa2_taw_fire_", ignore_cleanup_errors=True) as tmp:
        tmp_root = Path(tmp)
        inp = tmp_root / "input"
        out = tmp_root / "output"
        inp.mkdir()
        out.mkdir()
        shutil.copy2(TAW_FIRE_SAMPLE, inp / TAW_FIRE_SAMPLE.name)
        result = process_tennessee_american_water_batch(
            input_folder=inp,
            output_folder=out,
            template_path=TEMPLATE_PATH,
            config_path=ROOT / "config" / "vendors" / "tennessee_american_water.yaml",
            run_context={"dry_run": True, "smoke_test": "qa2_fire_service"},
        )
        rows = [row for inv in result.invoices for row in (inv.get("rows") or [])]
        if not rows:
            failures.append("Tennessee American Water fire sample produced no rows")
            return
        if any(str(row.get("GL Account")) == "6955" and "Fire" in str(row.get("Line Item Description")) for row in rows):
            failures.append("Tennessee American Water fire service mapped to normal water GL 6955")
        if not any(str(row.get("GL Account")) == "6860" for row in rows):
            failures.append(f"Tennessee American Water fire sample expected GL 6860, got {[row.get('GL Account') for row in rows]}")
        validation = validate_utility_template_rows(rows, valid_gl_accounts=load_chart_of_accounts())
        blocking = [flag for flag in validation.blocking_reasons if "fire_service_mapped_as_water" in flag]
        if blocking:
            failures.append(f"Tennessee American Water fire rows failed validation: {blocking}")
    if TEMPLATE_PATH.stat().st_mtime_ns != before:
        failures.append("Output/Template.xlsx was modified during QA-2 line classification smoke")


if __name__ == "__main__":
    raise SystemExit(main())
