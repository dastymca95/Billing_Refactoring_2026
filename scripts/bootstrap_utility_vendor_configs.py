from __future__ import annotations

import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
TRAINING_ROOT = ROOT / "Training Bills_Invoices"
VENDORS_DIR = ROOT / "config" / "vendors"
OLD_SCRIPTS = ROOT / "Old Scripts"


UTILITY_VENDORS: dict[str, dict[str, str]] = {
    "alabama_power": {
        "folder": "Electricity - Power/Alabama Power",
        "old_script": "Alabama_Power.py",
        "status": "partial_reference_old_script",
    },
    "atmos_energy_auto_pay": {
        "folder": "Gas/Atmos Energy Auto Pay",
        "processor": "process_atmos_energy_auto_pay.py",
        "status": "active",
    },
    "birmingham_water_works": {
        "folder": "Water - Sewer/Birmingham Water Works",
        "status": "needs_processor",
    },
    "cde_lightband": {
        "folder": "Electricity - Power/CDE Lightband",
        "old_script": "CDE Light Band.py",
        "status": "partial_reference_old_script",
    },
    "city_of_chattanooga_wastewater_department": {
        "folder": "Water - Sewer/City of Chattanooga Wastewater Department",
        "status": "needs_processor",
    },
    "city_of_martin": {
        "folder": "Water - Sewer/City of Martin",
        "status": "needs_processor",
    },
    "city_of_union_city": {
        "folder": "Water - Sewer/City of Union City",
        "status": "needs_processor",
    },
    "city_of_mcminnville_water_sewer_dept": {
        "folder": "Water - Sewer/City of McMinnville Water & Sewer Dept",
        "status": "needs_processor",
    },
    "clarksville_gas_and_water": {
        "folder": "Water - Sewer/Clarksville Gas and Water",
        "status": "needs_processor",
    },
    "columbia_power_and_water_system": {
        "folder": "Electricity - Power/Columbia Power and Water System",
        "processor": "process_columbia_power_and_water_system.py",
        "old_script": "CPWS.py",
        "status": "active",
    },
    "epb_fiber_optics": {
        "folder": "Electricity - Power/EPB Fiber Optics",
        "old_script": "EPB_Fiber.py",
        "status": "partial_reference_old_script",
    },
    "guardian_water_power": {
        "folder": "Water - Sewer/Guardian Water & Power",
        "status": "needs_processor",
    },
    "hardin_county_water_district_no_2": {
        "folder": "Water - Sewer/Hardin County Water District No. 2",
        "processor": "process_hardin_county_water_district_no_2.py",
        "old_script": "Hardin CWD2.py",
        "status": "active",
    },
    "hopkinsville_electric_system": {
        "folder": "Electricity - Power/Hopkinsville Electric System",
        "status": "needs_processor",
    },
    "hopkinsville_water_environment_authority": {
        "folder": "Water - Sewer/Hopkinsville Water Environment Authority",
        "processor": "process_hopkinsville_water_environment_authority.py",
        "old_script": "HWEA Test.py",
        "status": "active",
    },
    "kentucky_utilities": {
        "folder": "Electricity - Power/Kentucky Utilities",
        "status": "needs_processor",
        "community_billing": "true",
    },
    "knoxville_utilities_board": {
        "folder": "Electricity - Power/Knoxville Utilities Board",
        "status": "needs_processor",
        "community_billing": "true",
    },
    "mcminnville_electric_system": {
        "folder": "Electricity - Power/McMinnville Electric System",
        "processor": "process_mcminnville_electric_system.py",
        "status": "active",
    },
    "nolin_recc_smarthub": {
        "folder": "Electricity - Power/Nolin RECC Smarthub",
        "old_script": "Nolin REC.py",
        "status": "partial_reference_old_script",
    },
    "pennyrile_electric": {
        "folder": "Electricity - Power/Pennyrile Electric",
        "processor": "process_pennyrile_electric.py",
        "old_script": "Pennyrile Bills.py",
        "status": "active",
    },
    "richmond_utilities": {
        "folder": "Water - Sewer/Richmond Utilities",
        "processor": "process_richmond_utilities.py",
        "status": "active",
    },
    "shelbyville_power_system": {
        "folder": "Electricity - Power/Shelbyville Power System",
        "processor": "process_shelbyville_power_system.py",
        "old_script": "Shelbyville Power.py",
        "status": "active",
    },
    "tennessee_american_water": {
        "folder": "Water - Sewer/Tennessee American Water",
        "status": "needs_processor",
    },
    "the_city_of_henderson": {
        "folder": "Electricity - Power/City of Henderson",
        "old_script": "Henderson Bills.py",
        "status": "partial_reference_old_script",
    },
    "union_city_energy_authority": {
        "folder": "Electricity - Power/Union City Energy Authority",
        "status": "needs_processor",
    },
    "weakley_county_municipal_electric_system": {
        "folder": "Electricity - Power/Weakley County Municipal Electric System",
        "status": "needs_processor",
    },
}


def _count_files(path: Path) -> dict[str, int]:
    counts = {"pdf": 0, "image": 0, "spreadsheet": 0, "other": 0}
    if not path.is_dir():
        return counts
    for item in path.rglob("*"):
        if not item.is_file():
            continue
        suffix = item.suffix.lower()
        if suffix == ".pdf":
            counts["pdf"] += 1
        elif suffix in {".png", ".jpg", ".jpeg", ".tif", ".tiff"}:
            counts["image"] += 1
        elif suffix in {".xlsx", ".xls", ".csv"}:
            counts["spreadsheet"] += 1
        else:
            counts["other"] += 1
    return counts


def _training_folder(path: Path) -> Path:
    for name in ("Bills_Training", "Training_Bills", "Bills Training", "Training Bills"):
        child = path / name
        if child.is_dir():
            return child
    return path


def build_overlay(vendor_key: str, spec: dict[str, str]) -> dict:
    folder = TRAINING_ROOT / spec["folder"]
    training = _training_folder(folder)
    counts = _count_files(training)
    has_files = sum(counts.values()) > 0
    status = spec["status"]
    if not has_files and status == "needs_processor":
        status = "needs_more_training"
    old_script = spec.get("old_script", "")
    processor = spec.get("processor", "")
    return {
        "phase": "U1",
        "status": status,
        "processing_mode": "deterministic",
        "training_folder": str(training.relative_to(ROOT)).replace("\\", "/") if training.exists() else spec["folder"],
        "training_file_counts": counts,
        "current_processor": processor or None,
        "old_script_reference": old_script or None,
        "old_script_use_policy": "reference_only_do_not_copy_credentials_or_paths",
        "accepted_file_types": ["pdf", "png", "jpg", "jpeg", "xlsx", "csv", "docx"],
        "document_extraction": {
            "digital_pdf_text": True,
            "scanned_pdf_ocr_fallback": True,
            "image_ocr_fallback": True,
            "ai_not_required_for_deterministic_path": True,
        },
        "canonical_rules": {
            "category": "utilities",
            "required_fields_source": "config/canonical_rules.yaml:utility_processing.required_fields",
            "invoice_number_rule": "account_number_service_period_unless_vendor_rule_overrides",
            "description_rule": "service_period_address_or_property",
        },
        "tax_allocation_rules": {
            "default": "allocate_proportionally",
            "standalone_tax_lines": "forbidden",
            "rounding": "apply_remainder_to_largest_taxable_line",
        },
        "fee_rules": {
            "connection_reconnection_gl": "6956",
            "late_fee_gl_policy": "underlying_service_or_vendor_default_never_6956",
            "previous_balance_policy": "ignore_unless_vendor_rule_explicitly_allows_current_payable",
            "payment_lines_policy": "never_export_as_expense",
        },
        "property_location_rules": {
            "property_abbreviation_required": True,
            "location_valid_unit_only": True,
            "raw_address_in_location": "forbidden",
            "property_level_blank_location_allowed": True,
        },
        "community_billing_rules": {
            "enabled": spec.get("community_billing") == "true",
            "master_invoice_strategy": "one_per_master_account",
            "line_item_numbering": "sequential_per_master_invoice",
        },
        "manual_review_triggers": [
            "property_mapping_required",
            "gl_mapping_required",
            "amount_reconciliation_failed",
            "ocr_confidence_low",
            "vendor_processor_not_active",
        ],
        "change_log": [
            {
                "phase": "U1",
                "change": "Added shared deterministic utility processing contract.",
            }
        ],
    }


def main() -> int:
    updated = 0
    missing: list[str] = []
    for vendor_key, spec in UTILITY_VENDORS.items():
        path = VENDORS_DIR / f"{vendor_key}.yaml"
        if not path.is_file():
            missing.append(vendor_key)
            continue
        text = path.read_text(encoding="utf-8")
        if "\nutility_processing:" in text or text.startswith("utility_processing:"):
            continue
        overlay = {"utility_processing": build_overlay(vendor_key, spec)}
        path.write_text(
            text.rstrip()
            + "\n\n# Phase U1 shared utility-processing overlay.\n"
            + yaml.safe_dump(overlay, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        updated += 1
    print(f"Updated {updated} vendor YAML file(s).")
    if missing:
        print(f"Missing vendor YAMLs: {', '.join(sorted(missing))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
