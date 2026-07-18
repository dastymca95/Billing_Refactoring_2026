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
from webapp.backend.services import vendor_detection  # noqa: E402
from webapp.backend.services.utility_wave2_processors import (  # noqa: E402
    ParsedUtilityInvoice,
    SPECS as WAVE2_SPECS,
    _apply_cde_property_override,
    _best_gl,
    _parse_cde_lightband,
    _parse_epb_electric_power,
    _rows_for_invoice,
)
from webapp.backend.services.utility_wave3_processors import (  # noqa: E402
    SPECS as WAVE3_SPECS,
    _parse_chattanooga_wastewater,
    _parse_kentucky_utilities,
    _parse_pleasant_view_utility_district,
)
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
from utils.text_normalization import normalize_service_address_for_description  # noqa: E402


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
    _assert_pennyrile_rules(failures)
    _assert_cde_propark_rules(failures)
    _assert_cde_harmony_rules(failures)

    pvud_text = """PLEASANT VIEW UTILITY DISTRICT
BILLING DATE CUSTOMER NAME SERVICE ADDRESS
5/27/2026 KM DEVELOPMENTS LLC 434 CENTRE ST
CUSTOMER PIN NUMBER SERVICE PERIOD DAYS STATUS
NUMBER FROM TO
24224 9819 4/21/2026 5/20/2026 29 A
SERVICE TYPE PREVIOUS PRESENT CONSUMPTION CODE CHARGES
WATER SERVICE 796 796 0 WTA $21.30
SEWER SERVICE SWR $21.30
WTR LEAK RELIEF LRR $2.00
TAX $1.97
LATE DATE TOTAL AMOUNT DUE
$46.57
06/10/26 IF PAID AFTER LATE DATE $50.83
"""
    pvud = _parse_pleasant_view_utility_district(
        WAVE3_SPECS["pleasant_view_utility_district"],
        pvud_text,
        "Statement 2026-06.pdf",
    )[0]
    if pvud.account_number != "24224" or pvud.service_address != "434 CENTRE ST":
        failures.append("PVUD account/service-address parsing failed")
    if pvud.property_abbreviation != "PVT" or pvud.location != "434":
        failures.append("PVUD property/location mapping failed")
    pvud_charleston = _parse_pleasant_view_utility_district(
        WAVE3_SPECS["pleasant_view_utility_district"],
        pvud_text.replace("434 CENTRE ST", "221 CHARLESTON AV"),
        "Statement 2026-06 (2).pdf",
    )[0]
    if pvud_charleston.service_address != "221 CHARLESTON Ave" or pvud_charleston.location != "221":
        failures.append("PVUD Charleston AV/Ave normalization failed")
    if sum((line.money for line in pvud.line_items), Decimal("0.00")) != Decimal("44.60"):
        failures.append("PVUD named charge parsing failed")
    if pvud.tax_total != Decimal("1.97") or pvud.debug_info.get("source_total") != "46.57":
        failures.append("PVUD tax/total parsing failed")
    if "pleasant_view_utility_district" not in batch_processor._PROCESSOR_LOADERS:
        failures.append("PVUD deterministic processor is not registered")
    original_pvud_text_sample = vendor_detection._document_text_sample
    try:
        vendor_detection._document_text_sample = lambda _path, _limit=5000: pvud_text
        pvud_detection = vendor_detection.detect_vendor_for_file(Path("Statement 2026-06.pdf"))
    finally:
        vendor_detection._document_text_sample = original_pvud_text_sample
    if pvud_detection.get("vendor_key") != "pleasant_view_utility_district":
        failures.append(f"PVUD detector routed to {pvud_detection.get('vendor_key')}")
    pvud_config_path = VENDOR_DIR / "pleasant_view_utility_district.yaml"
    if not pvud_config_path.is_file():
        failures.append("PVUD vendor config is missing")
    else:
        pvud_config = yaml.safe_load(pvud_config_path.read_text(encoding="utf-8")) or {}
        if (pvud_config.get("accounting_mapping") or {}).get("default_gl_code") != "6955":
            failures.append("PVUD default GL must be 6955")
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
    stormwater_gl = default_gl_for_line(
        "Stormwater User Fee",
        vendor_config={"accounting_mapping": {"default_gl_code": "6955"}},
    )
    if stormwater_gl != "6995":
        failures.append(f"stormwater service should map to GL 6995, got {stormwater_gl}")

    inv_no = build_utility_invoice_number(
        account_number="341340.0094",
        service_period_end="04/30/2026",
    )
    if inv_no != "341340.0094 Apr 26":
        failures.append(f"canonical utility invoice-number format failed: {inv_no}")

    epb_text = """
    Billing Date: July 02, 2026 Page 1 of 2
    Electric Power Acct: 51-0801.038 4867
    Service Address: 21752 River Canyon Rd
    Apt J
    Chattanooga, TN 37405
    Rate Class: RESI BASE RATE
    Summary of New Charges
    Electric Power 149.79
    Total New Charges $ 149.79
    Payment Due Date Jul 17, 2026
    Customer Service 423-648-1EPB(1372) Billing Date: July 02, 2026 Page 2 of 2
    Previous KWH Meter Reading - Actual 06/01/2026 26947
    New KWH Meter Reading - Actual 07/01/2026 28089
    Total KWH Used This Period 1142
    Statement of New Charges
    Usage Charge $ 149.79
    Total Current Charges $ 149.79
    """
    epb_inv = _parse_epb_electric_power(
        WAVE2_SPECS["epb_fiber_optics"],
        epb_text,
        "epb-electric-sample.pdf",
    )[0]
    if epb_inv.account_number != "51-0801.038":
        failures.append(f"EPB electric account parser failed: {epb_inv.account_number}")
    epb_inv_no = build_utility_invoice_number(
        account_number=epb_inv.account_number,
        service_period_end=epb_inv.accounting_date,
    )
    if epb_inv_no != "51-0801.038 Jun 26":
        failures.append(f"EPB electric invoice-number policy failed: {epb_inv_no}")
    if epb_inv.accounting_date is None or epb_inv.accounting_date.strftime("%m/%d/%Y") != "06/30/2026":
        failures.append(f"EPB accounting date should close the prior month: {epb_inv.accounting_date}")
    epb_inv.invoice_number = epb_inv_no
    epb_inv.property_abbreviation = "RCC"
    epb_inv.line_items = [
        UtilityChargeLine(
            line.description,
            line.money,
            gl_account="6920",
            metadata=line.metadata,
        )
        for line in epb_inv.line_items
    ]
    epb_rows = _rows_for_invoice(epb_inv)
    if epb_rows[0]["Invoice Date"] != "07/02/2026" or epb_rows[0]["Accounting Date"] != "06/30/2026":
        failures.append(f"EPB invoice/accounting dates were not kept distinct: {epb_rows[0]}")
    if "Apt J" not in epb_rows[0]["Invoice Description"]:
        failures.append(f"EPB apartment context was stripped from description: {epb_rows[0]}")
    if "1142 kWh" not in epb_rows[0]["Line Item Description"]:
        failures.append(f"EPB usage detail was not retained: {epb_rows[0]}")

    ku_collective_text = """
    Mailed 6/2/26 for Collective Account # 3000-4466-5242
    AMOUNT DUE DUE DATE
    $91.35 6/26/26
    Service Address: 705B RED RIVER
    Collective Account Balance as of 6/2/26 $0.00
    Current Utility Charges Billed 91.35
    COLLECTIVE ACCOUNTS BILLED
    010 05/19/26 10,714 10,628 1.0000 86 26.80
    View Detailed Bill FEE 2.94
    3000-4296-9554 517 BALLARD DR APT 18 $29.74
    BILLED
    010 05/19/26 14,322 13,988 1.0000 334 55.50
    View Detailed Bill FEE 6.11
    3500-0693-7642 511 BALLARD DR APT 1 $61.61
    BILLED
    Total Current Charges Billed $91.35
    """
    ku_inv = _parse_kentucky_utilities(
        WAVE3_SPECS["kentucky_utilities"],
        ku_collective_text,
        "ku-collective-sample.pdf",
    )[0]
    if ku_inv.account_number != "3000-4466-5242":
        failures.append(f"KU collective master account parser failed: {ku_inv.account_number}")
    if len(ku_inv.line_items) != 2:
        failures.append(f"KU collective should produce detail-account line items, got {len(ku_inv.line_items)}")
    if sum((line.money for line in ku_inv.line_items), Decimal("0.00")) != Decimal("91.35"):
        failures.append("KU collective line items did not reconcile to source total")
    ku_inv.property_abbreviation = "BCA"
    ku_inv.invoice_number = "3000-4466-5242 Jun 26"
    ku_rows = _rows_for_invoice(ku_inv)
    if [row.get("Location") for row in ku_rows] != ["517-18", "511-1"]:
        failures.append(f"KU collective row locations were not preserved: {ku_rows}")
    if any("705B Red River" in str(row.get("Invoice Description") or "") for row in ku_rows):
        failures.append("KU collective descriptions repeated the master service address")
    if not str(ku_rows[0].get("Invoice Description") or "").endswith("517-18 Ballard Dr"):
        failures.append(f"KU collective unit description is not canonical: {ku_rows[0]}")
    if not str(ku_rows[0].get("Line Item Description") or "").endswith("Electric Service"):
        failures.append(f"KU collective line description is missing service detail: {ku_rows[0]}")

    chattanooga_text = """
    City of Chattanooga Wastewater Department
    BILLING DATE ACCOUNT NUMBER BILLING ID ACCOUNT NAME SERVICE ADDRESS
    06/08/2026 100021578-01 3652 00150712 Granite Heights Apts One Llc 1400 N Chamberlain AVE APT 60
    BILLING PERIOD CHARGES/ SEWER RATE
    DAYS METER NUMBER SIZE GALLONS BILLING SUMMARY
    FROM TO ADJUSTMENTS A1M4D-5/8
    04/27/2026 05/27/2026 30 000000000065003080" 5/8"" 500 $29.67 Amount on Previous Statement $19.78
    Payments Through 6/2/26. Thank you! -$21.76
    Balance Forward -$1.98
    Sewer Usage Charges $29.67
    Penalty $1.98
    Current Charges: $31.65
    Amount Due by 06/23/26: $29.67
    Amount Due on/after 06/24/26: $32.64
    """
    chattanooga_inv = _parse_chattanooga_wastewater(
        WAVE3_SPECS["city_of_chattanooga_wastewater_department"],
        chattanooga_text,
        "StatementView-sample.pdf",
    )[0]
    if chattanooga_inv.account_number != "100021578-01":
        failures.append(f"Chattanooga wastewater account parser failed: {chattanooga_inv.account_number}")
    if chattanooga_inv.service_address != "1400 N Chamberlain Ave Apt 60":
        failures.append(f"Chattanooga wastewater service address parser failed: {chattanooga_inv.service_address}")
    if sum((line.money for line in chattanooga_inv.line_items), Decimal("0.00")) != Decimal("29.67"):
        failures.append("Chattanooga wastewater should export only current sewer usage charges")
    original_text_sample = vendor_detection._document_text_sample
    try:
        vendor_detection._document_text_sample = lambda _path, _limit=5000: (
            "City of Chattanooga Wastewater Department\n"
            "Address Change/Move-Out: Contact TN American Water: (866) 736-6420\n"
            "Pay Online: www.sewerpayments.com/chattanooga\n"
            "Sewer Usage Charges $29.67\n"
        )
        chatt_detection = vendor_detection.detect_vendor_for_file(Path("StatementView-sample.pdf"))
    finally:
        vendor_detection._document_text_sample = original_text_sample
    if chatt_detection.get("vendor_key") != "city_of_chattanooga_wastewater_department":
        failures.append(f"Chattanooga detector routed to {chatt_detection.get('vendor_key')}")

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


def _assert_pennyrile_rules(failures: list[str]) -> None:
    config_path = VENDOR_DIR / "pennyrile_electric.yaml"
    if not config_path.is_file():
        failures.append("Pennyrile vendor config is missing")
        return

    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    extraction_rules = config.get("pdf_extraction_rules") or {}
    normalization_rules = extraction_rules.get("unit_normalization_rules") or []
    processor = batch_processor._import_pennyrile_processor()
    cases = {
        "GRIFFINGATE304APTB1": ("Griffin Gate 304 Apt B1", "B-01"),
        "GRIFFINGATE302APTA11": ("Griffin Gate 302 Apt A11", "A-11"),
        "GRIFFINGATEDR310APTF3": ("Griffin Gate 310 Apt F3", "F-03"),
        "GRIFFIN302APTA2": ("Griffin 302 Apt A2", "A-02"),
        "HOUSEMETER": ("Griffin Gate House Meter", ""),
    }
    for raw_value, expected in cases.items():
        actual = processor._normalize_unit(raw_value, normalization_rules)
        if actual != expected:
            failures.append(
                f"Pennyrile unit normalization failed for {raw_value}: "
                f"expected {expected}, got {actual}"
            )

    descriptions = extraction_rules.get("bucket_description_formats") or {}
    if "Balance Forward" not in descriptions.get("balance_forward", ""):
        failures.append("Pennyrile balance-forward description is mislabeled")


def _assert_cde_propark_rules(failures: list[str]) -> None:
    valid_gls = load_chart_of_accounts()
    spec = WAVE2_SPECS["cde_lightband"]

    def invoice(service_address: str) -> ParsedUtilityInvoice:
        parsed = ParsedUtilityInvoice(
            vendor_key=spec.key,
            vendor_display_name=spec.display_name,
            account_number="417402-027",
            invoice_number="",
            invoice_date=None,
            due_date=None,
            service_period_start=None,
            service_period_end=None,
            service_address=service_address,
            line_items=[],
        )
        _apply_cde_property_override(parsed)
        return parsed

    unit_invoice = invoice("850 PROFESSIONAL PARK DR A303")
    if unit_invoice.property_abbreviation != "OG-PPA" or unit_invoice.location != "A-303":
        failures.append("CDE ProPark compact unit did not map to OG-PPA/A-303")
    unit_gl = _best_gl(
        unit_invoice,
        spec,
        UtilityChargeLine(
            "Electric Energy Charge",
            "54.79",
            metadata={"cde_charge_block": "electric"},
        ),
        {"gl_account": "6915"},
        {},
        valid_gls,
    )
    if unit_gl != "6920":
        failures.append(f"CDE ProPark unit electric should use 6920, got {unit_gl}")

    common_invoice = invoice("850 PROFESSIONAL PK DR CLUBHS")
    common_gl = _best_gl(
        common_invoice,
        spec,
        UtilityChargeLine(
            "Electric Energy Charge",
            "313.73",
            metadata={"cde_charge_block": "electric"},
        ),
        {"gl_account": "6178"},
        {},
        valid_gls,
    )
    if common_invoice.location or not common_invoice.debug_info.get("property_level_service"):
        failures.append("CDE ProPark clubhouse should remain property-level")
    if common_gl != "6915":
        failures.append(f"CDE ProPark common electric should use 6915, got {common_gl}")
    clubhouse_description = normalize_service_address_for_description(
        "850 Professional Park Dr Clubhouse"
    )
    if not clubhouse_description.endswith("Clubhouse"):
        failures.append("CDE ProPark clubhouse context was stripped from the description")

    phone_gl = _best_gl(
        common_invoice,
        spec,
        UtilityChargeLine(
            "Standard Business Phone",
            "32.57",
            gl_account="6178",
            metadata={"cde_charge_block": "telecom"},
        ),
        {"gl_account": "6915"},
        {},
        valid_gls,
    )
    if phone_gl != "6178":
        failures.append(f"CDE ProPark phone service should use 6178, got {phone_gl}")

    prior_balance_only_text = """2021 Wilma Rudolph Blvd.
    www.clarksvillede.com
    Account #: 417402-025 Bill Summary
    Service: 850 PROFESSIONAL PARK DR A301
    Date Due: 07/21/26
    Service Period: 04/28/26 to 05/08/26
    Prior Balance 38.60
    <<ELECTRIC SUB-TOTAL>> 38.60
    CDE is not responsible for bills lost in the mail.
    """
    original_pdf_text_sample = vendor_detection._pdf_text_sample
    try:
        vendor_detection._pdf_text_sample = lambda _path, _limit=3500: prior_balance_only_text
        matched, _, _ = vendor_detection._looks_like_cde_lightband(Path("balance-only.pdf"))
    finally:
        vendor_detection._pdf_text_sample = original_pdf_text_sample
    if not matched:
        failures.append("CDE prior-balance-only bill was not routed deterministically")


def _assert_cde_harmony_rules(failures: list[str]) -> None:
    spec = WAVE2_SPECS["cde_lightband"]
    text = """Account #: 387667-001 Bill Summary
Statement Date: 06/30/26
Date Due: 07/21/26
Service: 841 PROFESSIONAL PARK DR OFC
Service Period: 05/28/26 to 06/26/26
CDE Lightband is not responsible for bills lost in the mail.
Electric Energy Charge 251.30
Sales Tax 17.59
<<ELECTRIC SUB-TOTAL>> 268.89
Account #: 387667-001 Bill Summary
Statement Date: 06/30/26
Date Due: 07/21/26
Service: 841 PROFESSIONAL PARK DR OFC
Service Period: 05/29/26 to 06/30/26
CDE Lightband is not responsible for bills lost in the mail.
SMB WIFI 06/20-07/20 19.95
1GB w/Dynamic IP Add only 06/20-07/20 80.05
Regulatory Cost Recovery Fee 1.00
<<TELECOM SUB-TOTAL>> 101.00
"""
    parsed = _parse_cde_lightband(spec, text, "387667-1-202606.pdf")
    if len(parsed) != 1:
        failures.append(f"CDE Harmony multi-page account should merge to one invoice, got {len(parsed)}")
        return

    invoice = parsed[0]
    if len(invoice.line_items) != 4:
        failures.append(f"CDE Harmony merged invoice should have four lines, got {len(invoice.line_items)}")
    line_total = sum((line.money for line in invoice.line_items), Decimal("0.00"))
    if line_total != Decimal("369.89"):
        failures.append(f"CDE Harmony merged invoice should reconcile to 369.89, got {line_total}")
    if invoice.debug_info.get("invoice_suffix"):
        failures.append("CDE Harmony merged invoice retained an Electric/Telecom suffix")

    _apply_cde_property_override(invoice)
    if invoice.property_abbreviation != "THSA" or invoice.location:
        failures.append("CDE Harmony office did not map to THSA property-level service")
    if invoice.debug_info.get("cde_service_context") != "office":
        failures.append("CDE Harmony office context was not preserved")

    valid_gls = load_chart_of_accounts()
    electric_gl = _best_gl(invoice, spec, invoice.line_items[0], {"gl_account": "6139"}, {}, valid_gls)
    telecom_gls = [
        _best_gl(invoice, spec, line, {"gl_account": "6915"}, {}, valid_gls)
        for line in invoice.line_items[1:]
    ]
    if electric_gl != "6915":
        failures.append(f"CDE Harmony office electric should use 6915, got {electric_gl}")
    if telecom_gls != ["6139", "6139", "6139"]:
        failures.append(f"CDE Harmony telecom GLs should remain 6139, got {telecom_gls}")

    invoice.invoice_number = "387667-001 Jun 26"
    rendered_rows = _rows_for_invoice(invoice)
    if [row["Line Item Number"] for row in rendered_rows] != ["1", "2", "3", "4"]:
        failures.append("CDE Harmony merged line-item numbering is not sequential")
    if any(row["Invoice Number"] != "387667-001 Jun 26" for row in rendered_rows):
        failures.append("CDE Harmony rows do not share one canonical invoice number")
    if any("841 Professional Park Dr Office" not in row["Invoice Description"] for row in rendered_rows):
        failures.append("CDE Harmony office context was stripped from the invoice description")

    house_meter = ParsedUtilityInvoice(
        vendor_key=spec.key,
        vendor_display_name=spec.display_name,
        account_number="387667-068",
        invoice_number="",
        invoice_date=None,
        due_date=None,
        service_period_start=None,
        service_period_end=None,
        service_address="841 PROFESSIONAL PARK DR HM",
        line_items=[],
    )
    _apply_cde_property_override(house_meter)
    if house_meter.debug_info.get("description_service_address") != "841 Professional Park Dr House Meter":
        failures.append("CDE Harmony house-meter description was not normalized explicitly")


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
