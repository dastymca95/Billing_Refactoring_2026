from __future__ import annotations

import argparse
import logging
import shutil
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp.backend.services import batch_processor  # noqa: E402
from webapp.backend.services.utility_processor_common import (  # noqa: E402
    UtilityChargeLine,
    allocate_tax_proportionally,
    build_utility_invoice_number,
    classify_utility_line,
    compose_invoice_description,
    compose_line_item_description,
    default_gl_for_line,
    filter_exportable_utility_lines,
    load_chart_of_accounts,
    validate_utility_template_rows,
)


VENDOR_DIR = ROOT / "config" / "vendors"
TEMPLATE_PATH = ROOT / "Output" / "Template.xlsx"

EXPECTED_U1_VENDOR_KEYS = {
    "alabama_power",
    "atmos_energy_auto_pay",
    "birmingham_water_works",
    "cde_lightband",
    "city_of_chattanooga_wastewater_department",
    "city_of_martin",
    "city_of_union_city",
    "city_of_mcminnville_water_sewer_dept",
    "clarksville_gas_and_water",
    "columbia_power_and_water_system",
    "epb_fiber_optics",
    "guardian_water_power",
    "hardin_county_water_district_no_2",
    "hopkinsville_electric_system",
    "hopkinsville_water_environment_authority",
    "kentucky_utilities",
    "knoxville_utilities_board",
    "mcminnville_electric_system",
    "nolin_recc_smarthub",
    "pennyrile_electric",
    "richmond_utilities",
    "shelbyville_power_system",
    "tennessee_american_water",
    "the_city_of_henderson",
    "union_city_energy_authority",
    "weakley_county_municipal_electric_system",
}

WAVE2_VENDOR_KEYS = {
    "alabama_power",
    "cde_lightband",
    "epb_fiber_optics",
    "nolin_recc_smarthub",
    "the_city_of_henderson",
}

WAVE3_VENDOR_KEYS = {
    "birmingham_water_works",
    "city_of_chattanooga_wastewater_department",
    "city_of_martin",
    "city_of_mcminnville_water_sewer_dept",
    "city_of_union_city",
    "clarksville_gas_and_water",
    "guardian_water_power",
    "hopkinsville_electric_system",
    "kentucky_utilities",
    "knoxville_utilities_board",
    "tennessee_american_water",
    "union_city_energy_authority",
    "weakley_county_municipal_electric_system",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--contract-only",
        action="store_true",
        help="Skip temp-folder processor dry-runs and validate only shared contracts.",
    )
    args = parser.parse_args()

    failures: list[str] = []
    warnings: list[str] = []

    _assert_common_rules(failures)
    overlays = _load_utility_overlays(failures)
    _assert_vendor_overlays(overlays, failures)
    _assert_registered_active_processors(overlays, failures)

    if not args.contract_only:
        _run_active_processor_dry_runs(overlays, failures, warnings)

    if warnings:
        print("Warnings:")
        for warning in warnings:
            print(f"  - {warning}")

    if failures:
        print("FAIL:")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("PASS: utility processor shared contract is valid.")
    print(f"Validated {len(overlays)} utility vendor YAML overlay(s).")
    return 0


def _assert_common_rules(failures: list[str]) -> None:
    lines = [
        UtilityChargeLine("Water service", "100.00", gl_account="6955"),
        UtilityChargeLine("Sewer service", "50.00", gl_account="6955"),
        UtilityChargeLine("Sales tax", "15.00", line_type="tax"),
        UtilityChargeLine("Previous balance", "45.00", line_type="previous_balance"),
        UtilityChargeLine("Payment received", "-45.00", line_type="payment"),
        UtilityChargeLine("Zero usage line", "0.00"),
    ]
    filtered = filter_exportable_utility_lines(lines)
    if [line.description for line in filtered] != ["Water service", "Sewer service"]:
        failures.append("non-current utility lines were not filtered correctly")

    allocated = allocate_tax_proportionally(filtered, Decimal("15.00"))
    allocated_total = sum(line.money for line in allocated.lines)
    if allocated_total != Decimal("165.00"):
        failures.append(f"tax allocation did not reconcile to 165.00: {allocated_total}")
    if allocated.allocation_by_index != {0: Decimal("10.00"), 1: Decimal("5.00")}:
        failures.append(f"unexpected proportional tax allocation: {allocated.allocation_by_index}")

    rounding = allocate_tax_proportionally(
        [
            UtilityChargeLine("Electric base A", "33.33"),
            UtilityChargeLine("Electric base B", "33.33"),
            UtilityChargeLine("Electric base C", "33.34"),
        ],
        Decimal("0.01"),
    )
    if sum(rounding.allocation_by_index.values(), Decimal("0.00")) != Decimal("0.01"):
        failures.append("rounding allocation did not reconcile to one cent")

    if classify_utility_line("Reconnect charge") != "connection_fee":
        failures.append("connection/reconnection fee classifier failed")
    if classify_utility_line("Late payment charge") != "late_fee":
        failures.append("late fee classifier failed")
    if classify_utility_line("Fire Protection Service") != "fire_protection_service":
        failures.append("fire protection classifier failed")
    if classify_utility_line("Sanitation") != "trash_service":
        failures.append("sanitation/trash classifier failed")
    if default_gl_for_line("Fiber internet service", vendor_key="epb_fiber_optics") != "6960":
        failures.append("internet/fiber default GL should be 6960")
    if default_gl_for_line("Connect fee") != "6956":
        failures.append("connect fee GL should be 6956")
    late_gl = default_gl_for_line(
        "Late fee",
        vendor_config={"accounting_mapping": {"default_gl_code": "6955"}},
    )
    if late_gl == "6956":
        failures.append("late fee incorrectly mapped to connect fee GL 6956")
    fire_gl = default_gl_for_line(
        "Private Fire Service Charge",
        vendor_config={"utility_processing": {"fire_service_rules": {"gl_account": "6860"}}},
    )
    if fire_gl != "6860":
        failures.append(f"fire service GL should be configurable to 6860, got {fire_gl}")
    trash_gl = default_gl_for_line("Sanitation", vendor_config={"accounting_mapping": {"default_gl_code": "6955"}})
    if trash_gl != "6940":
        failures.append(f"sanitation/trash service should map to GL 6940, got {trash_gl}")

    inv_no = build_utility_invoice_number(
        account_number="341340.0094",
        service_period_end="04/30/2026",
    )
    if inv_no != "341340.0094 Apr 26":
        failures.append(f"canonical utility invoice-number format failed: {inv_no}")

    desc = compose_invoice_description(
        service_period_start="03/26/2026",
        service_period_end="04/27/2026",
        service_address_or_property="21752 river canyon rd",
    )
    line_desc = compose_line_item_description(
        service_period_start="03/26/2026",
        service_period_end="04/27/2026",
        service_address_or_property="21752 river canyon rd",
        source_line_description="fi-speed internet",
    )
    if desc != "03/26/26-04/27/26 - 21752 River Canyon Rd":
        failures.append(f"invoice description composition failed: {desc}")
    if line_desc != "03/26/26-04/27/26 - 21752 River Canyon Rd - Fi-Speed Internet":
        failures.append(f"line description composition failed: {line_desc}")

    valid_gls = load_chart_of_accounts()
    good_row = {
        "Invoice Number": "341340.0094 Apr 26",
        "Bill or Credit": "Bill",
        "Invoice Date": "04/30/2026",
        "Accounting Date": "04/30/2026",
        "Vendor": "Utility Vendor",
        "Invoice Description": desc,
        "Line Item Number": "1",
        "Property Abbreviation": "RCC",
        "Location": "",
        "GL Account": "6955",
        "Line Item Description": line_desc,
        "Amount": "165.00",
        "Expense Type": "General",
        "Is Replacement Reserve": "false",
        "Due Date": "05/30/2026",
    }
    if not validate_utility_template_rows([good_row], valid_gl_accounts=valid_gls).ok:
        failures.append("valid utility row did not pass validation")

    bad_row = dict(good_row)
    bad_row["GL Account"] = "MISCELLANEOUS"
    bad_row["Location"] = "21752 River Canyon Rd"
    bad_row["Line Item Description"] = "Sales tax"
    result = validate_utility_template_rows([bad_row], valid_gl_accounts=valid_gls)
    expected_flags = {
        "row_1:invalid_gl_account",
        "row_1:raw_address_in_location",
        "row_1:standalone_tax_line",
    }
    if not expected_flags.issubset(set(result.blocking_reasons)):
        failures.append(f"bad utility row did not raise expected flags: {result.blocking_reasons}")

    connection_wrong = dict(good_row)
    connection_wrong["Line Item Description"] = "03/26/26-04/27/26 - 21752 River Canyon Rd - Connection Fee"
    connection_result = validate_utility_template_rows([connection_wrong], valid_gl_accounts=valid_gls)
    if "row_1:connection_fee_wrong_gl" not in connection_result.blocking_reasons:
        failures.append("connection fee row with non-6956 GL did not fail validation")

    fire_wrong = dict(good_row)
    fire_wrong["Line Item Description"] = "03/26/26-04/27/26 - 21752 River Canyon Rd - Fire Protection Service"
    fire_wrong["_meta"] = {"service_address": "21752 River Canyon Rd", "line_classification": "fire_protection_service"}
    fire_result = validate_utility_template_rows([fire_wrong], valid_gl_accounts=valid_gls)
    if "row_1:fire_service_mapped_as_water" not in fire_result.blocking_reasons:
        failures.append("fire service row mapped as water did not fail validation")


def _load_utility_overlays(failures: list[str]) -> dict[str, dict[str, Any]]:
    overlays: dict[str, dict[str, Any]] = {}
    for key in sorted(EXPECTED_U1_VENDOR_KEYS):
        path = VENDOR_DIR / f"{key}.yaml"
        if not path.is_file():
            failures.append(f"missing vendor YAML: {key}")
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            failures.append(f"{key}: YAML failed to parse: {exc}")
            continue
        overlay = data.get("utility_processing")
        if not isinstance(overlay, dict):
            failures.append(f"{key}: missing utility_processing overlay")
            continue
        overlays[key] = overlay
    return overlays


def _assert_vendor_overlays(overlays: dict[str, dict[str, Any]], failures: list[str]) -> None:
    for key, overlay in sorted(overlays.items()):
        if overlay.get("phase") not in {"U1", "U2", "U3"}:
            failures.append(f"{key}: utility overlay phase is not U1/U2/U3")
        if overlay.get("processing_mode") != "deterministic":
            failures.append(f"{key}: processing_mode should remain deterministic")
        if overlay.get("tax_allocation_rules", {}).get("standalone_tax_lines") != "forbidden":
            failures.append(f"{key}: standalone tax lines must be forbidden")
        if overlay.get("fee_rules", {}).get("connection_reconnection_gl") != "6956":
            failures.append(f"{key}: connection/reconnection GL must be 6956")
        if overlay.get("property_location_rules", {}).get("raw_address_in_location") != "forbidden":
            failures.append(f"{key}: raw addresses must be forbidden in Location")
        status = str(overlay.get("status") or "")
        if not status:
            failures.append(f"{key}: missing implementation status")
        counts = overlay.get("training_file_counts") or {}
        if status != "needs_more_training" and sum(int(counts.get(k, 0) or 0) for k in counts) <= 0:
            failures.append(f"{key}: status {status} but no training files counted")


def _assert_registered_active_processors(
    overlays: dict[str, dict[str, Any]],
    failures: list[str],
) -> None:
    for key, overlay in sorted(overlays.items()):
        if overlay.get("status") != "active":
            continue
        if key not in batch_processor._PROCESSOR_LOADERS:
            failures.append(f"{key}: active vendor is not registered in batch processor")
            continue
        loader, entrypoint = batch_processor._PROCESSOR_LOADERS[key]
        try:
            module = loader()
            fn = getattr(module, entrypoint)
        except Exception as exc:
            failures.append(f"{key}: processor import failed: {exc}")
            continue
        if not callable(fn):
            failures.append(f"{key}: processor entrypoint is not callable")


def _run_active_processor_dry_runs(
    overlays: dict[str, dict[str, Any]],
    failures: list[str],
    warnings: list[str],
) -> None:
    if not TEMPLATE_PATH.is_file():
        failures.append("Output/Template.xlsx is missing; cannot verify dry-run safety")
        return
    before_mtime = TEMPLATE_PATH.stat().st_mtime_ns
    for key, overlay in sorted(overlays.items()):
        if overlay.get("status") != "active":
            continue
        if key not in batch_processor._PROCESSOR_LOADERS:
            continue
        sample = _first_training_pdf(overlay)
        if sample is None:
            warnings.append(f"{key}: no PDF sample found for temp dry-run")
            continue
        loader, entrypoint = batch_processor._PROCESSOR_LOADERS[key]
        module = loader()
        fn = getattr(module, entrypoint)
        with tempfile.TemporaryDirectory(prefix=f"u1_{key}_", ignore_cleanup_errors=True) as tmp:
            tmp_root = Path(tmp)
            inp = tmp_root / "input"
            out = tmp_root / "output"
            inp.mkdir()
            out.mkdir()
            shutil.copy2(sample, inp / sample.name)
            try:
                result = fn(
                    input_folder=inp,
                    output_folder=out,
                    template_path=TEMPLATE_PATH,
                    config_path=VENDOR_DIR / f"{key}.yaml",
                    run_context={"dry_run": True, "smoke_test": "utility_processors"},
                    progress_callback=lambda **_: None,
                )
            except TypeError:
                result = fn(
                    input_folder=inp,
                    output_folder=out,
                    template_path=TEMPLATE_PATH,
                    config_path=VENDOR_DIR / f"{key}.yaml",
                    run_context={"dry_run": True, "smoke_test": "utility_processors"},
                )
            except Exception as exc:
                warnings.append(f"{key}: dry-run raised {type(exc).__name__}: {exc}")
                logging.shutdown()
                continue
            finally:
                logging.shutdown()
            workbook = str(getattr(result, "resman_workbook_path", "") or "")
            if workbook and Path(workbook).exists():
                failures.append(f"{key}: dry-run produced workbook unexpectedly: {workbook}")
            elif workbook:
                warnings.append(f"{key}: dry-run returned workbook path but no workbook was written")
            summary = getattr(result, "summary", {}) or {}
            if summary.get("dropbox_called") is True:
                failures.append(f"{key}: dry-run reported a Dropbox call")
            if int(summary.get("rows_total") or 0) <= 0 and int(summary.get("invoices_produced") or 0) <= 0:
                failures.append(f"{key}: active dry-run produced no preview rows for {sample.name}")
            rows: list[dict[str, Any]] = []
            for invoice in list(getattr(result, "invoices", []) or []):
                rows.extend(list((invoice or {}).get("rows") or []))
            if key in (WAVE2_VENDOR_KEYS | WAVE3_VENDOR_KEYS) and rows:
                validation = validate_utility_template_rows(rows, valid_gl_accounts=load_chart_of_accounts())
                if not validation.ok:
                    failures.append(
                        f"{key}: generated invalid utility preview rows: {validation.blocking_reasons}"
                    )
            logging.shutdown()
    after_mtime = TEMPLATE_PATH.stat().st_mtime_ns
    if after_mtime != before_mtime:
        failures.append("Output/Template.xlsx was modified during utility smoke")


def _first_training_pdf(overlay: dict[str, Any]) -> Path | None:
    folder = ROOT / str(overlay.get("training_folder") or "")
    if not folder.is_dir():
        return None
    for path in sorted(folder.rglob("*.pdf"), key=lambda p: p.name.lower()):
        if path.is_file():
            return path
    return None


if __name__ == "__main__":
    raise SystemExit(main())
