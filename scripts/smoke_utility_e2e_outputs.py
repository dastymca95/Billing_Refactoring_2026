from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp.backend.api.batches import _write_metadata  # noqa: E402
from webapp.backend.api.processing import _record_revision_for_result  # noqa: E402
from webapp.backend.services import batch_processor, batch_store, row_normalizer  # noqa: E402
from webapp.backend.services.utility_processor_common import (  # noqa: E402
    load_chart_of_accounts,
    validate_utility_template_rows,
)


SCREENSHOT_DIR = ROOT / "docs" / "reports" / "phases" / "screenshots" / "phase_u4_utility_e2e_qa"
TEMPLATE_PATH = ROOT / "Output" / "Template.xlsx"
TRAINING_ROOT = ROOT / "Training Bills_Invoices"


@dataclass(frozen=True)
class UtilityE2ECase:
    key: str
    label: str
    relative_sample: str
    expected_vendor_key: str
    require_rows: bool = True
    community_master: bool = False
    allow_manual_review: bool = False
    note: str = ""


CASES: list[UtilityE2ECase] = [
    UtilityE2ECase(
        key="knoxville",
        label="Knoxville Utility Board",
        expected_vendor_key="knoxville_utilities_board",
        relative_sample="Electricity - Power/Knoxville Utilities Board/Bills_Training/00c52f0d-e38a-446b-b3db-a370ae046428.pdf",
        community_master=True,
    ),
    UtilityE2ECase(
        key="kentucky_utilities",
        label="Kentucky Utilities",
        expected_vendor_key="kentucky_utilities",
        relative_sample="Electricity - Power/Kentucky Utilities/Bills_Training/1d0db5ed-5617-4fd7-a5d0-5a782a138b74.pdf",
        community_master=True,
    ),
    UtilityE2ECase(
        key="clarksville_gas_water",
        label="Clarksville Gas and Water",
        expected_vendor_key="clarksville_gas_and_water",
        relative_sample="Water - Sewer/Clarksville Gas and Water/Bills_Training/0448f1c0-5658-47f2-b552-3664b3e47fd9.pdf",
    ),
    UtilityE2ECase(
        key="alabama_power",
        label="Alabama Power",
        expected_vendor_key="alabama_power",
        relative_sample="Electricity - Power/Alabama Power/Bills_Training/Bill - 2026-04-24T103149.660.PDF",
    ),
    UtilityE2ECase(
        key="epb_fiber",
        label="EPB Fiber Optics",
        expected_vendor_key="epb_fiber_optics",
        relative_sample="Electricity - Power/EPB Fiber Optics/Bills_Training/download (47).pdf",
    ),
    UtilityE2ECase(
        key="henderson",
        label="The City of Henderson",
        expected_vendor_key="the_city_of_henderson",
        relative_sample="Electricity - Power/City of Henderson/Bills_Training/UtilityBill (100).pdf",
    ),
    UtilityE2ECase(
        key="tennessee_american_water",
        label="Tennessee American Water",
        expected_vendor_key="tennessee_american_water",
        relative_sample="Water - Sewer/Tennessee American Water/Bills_Training/1026-210052442136 Apr 26.pdf",
    ),
    UtilityE2ECase(
        key="hwea",
        label="HWEA",
        expected_vendor_key="hopkinsville_water_environment_authority",
        relative_sample="Water - Sewer/Hopkinsville Water Environment Authority/Bills_Training/HWEA - 4-20-26.pdf",
        allow_manual_review=True,
        note="Legacy scanned multi-invoice bill. Dry-run skips Dropbox support links and OCR may leave selected units for review.",
    ),
    UtilityE2ECase(
        key="richmond",
        label="Richmond Utilities",
        expected_vendor_key="richmond_utilities",
        relative_sample="Water - Sewer/Richmond Utilities/Bills_Training/Richmond Utilities - Blue Country 4-6-26.pdf",
        allow_manual_review=True,
        note="Legacy scanned multi-page bill. Dry-run skips Dropbox support links; ambiguous building-only unit matches remain reviewable.",
    ),
    UtilityE2ECase(
        key="weakley_image",
        label="Weakley image bill",
        expected_vendor_key="weakley_county_municipal_electric_system",
        relative_sample="Electricity - Power/Weakley County Municipal Electric System/Bills_Training/24b4b317-787e-4f1d-bbf1-cbc2012371a4.jpg",
        require_rows=True,
        allow_manual_review=False,
        note="Image OCR route check. The deterministic Weakley parser should recover validated rows from the known QA image.",
    ),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--prepare-browser-fixtures",
        action="store_true",
        help="Create QA batches in webapp_data and cache dry-run preview results for browser screenshots.",
    )
    args = parser.parse_args()

    failures: list[str] = []
    warnings: list[str] = []
    results: list[dict[str, Any]] = []
    manifest_cases: list[dict[str, Any]] = []

    if not TEMPLATE_PATH.is_file():
        failures.append("Output/Template.xlsx is missing")
        return _finish(failures, warnings, results)
    template_mtime = TEMPLATE_PATH.stat().st_mtime_ns

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    if args.prepare_browser_fixtures:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

    for case in CASES:
        source = TRAINING_ROOT / case.relative_sample
        if not source.is_file():
            failures.append(f"{case.label}: sample file missing: {source}")
            continue
        try:
            result, batch_id = _run_case(case, source, run_id, args.prepare_browser_fixtures)
        except Exception as exc:  # pragma: no cover - smoke script guard
            failures.append(f"{case.label}: processing raised {type(exc).__name__}: {exc}")
            continue

        case_summary = _validate_case(case, result, failures, warnings)
        case_summary["batch_id"] = batch_id
        case_summary["sample_file"] = source.name
        case_summary["note"] = case.note
        results.append(case_summary)

        if args.prepare_browser_fixtures and batch_id:
            manifest_cases.append({
                "key": case.key,
                "label": case.label,
                "batch_id": batch_id,
                "expected_vendor_key": case.expected_vendor_key,
                "sample_file": source.name,
                "row_count": case_summary["rows"],
                "invoice_count": case_summary["invoices"],
                "manual_review_count": case_summary["manual_review"],
                "manual_review_reasons": case_summary["manual_review_reasons"],
                "community_master": case.community_master,
                "screenshot_prefix": _safe_name(case.key),
                "note": case.note,
            })

    if TEMPLATE_PATH.stat().st_mtime_ns != template_mtime:
        failures.append("Output/Template.xlsx was modified during U4 utility e2e smoke")

    if args.prepare_browser_fixtures:
        manifest = {
            "phase": "U4",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "screenshot_dir": str(SCREENSHOT_DIR),
            "cases": manifest_cases,
        }
        (SCREENSHOT_DIR / "fixture_manifest.json").write_text(
            json.dumps(manifest, indent=2),
            encoding="utf-8",
        )

    return _finish(failures, warnings, results)


def _run_case(
    case: UtilityE2ECase,
    source: Path,
    run_id: str,
    persist_for_browser: bool,
) -> tuple[dict[str, Any], str | None]:
    batch_id = batch_store.create_batch()
    batch_name = f"U4 QA - {case.label} - {run_id}"
    _write_metadata(
        batch_id,
        batch_name=batch_name,
        status="idle",
        document_mode="auto_detect",
        ai_fallback_enabled=False,
        ai_fallback_policy="never",
        phase="U4",
    )
    target = batch_store.get_input_dir(batch_id) / source.name
    shutil.copy2(source, target)

    try:
        result = batch_processor.process_batch(batch_id, dry_run=True)
        row_normalizer.normalize_result(result)
        cache = batch_store.get_processed_dir(batch_id) / "_webapp_result.json"
        cache.write_text(json.dumps(result, default=str, indent=2), encoding="utf-8")
        _record_revision_for_result(batch_id, result)
        if persist_for_browser:
            return result, batch_id
        return result, None
    finally:
        if not persist_for_browser:
            shutil.rmtree(batch_store.get_batch_dir(batch_id), ignore_errors=True)


def _validate_case(
    case: UtilityE2ECase,
    result: dict[str, Any],
    failures: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    summary = result.get("summary") or {}
    by_vendor = result.get("by_vendor") or {}
    detection = result.get("detection") or {}
    invoices = list(result.get("all_invoices") or [])
    review = list(result.get("all_manual_review") or [])
    rows = [row for inv in invoices for row in (inv.get("rows") or [])]
    row_count = len(rows)

    if summary.get("files_unsupported"):
        failures.append(f"{case.label}: unsupported files reported: {summary.get('files_unsupported')}")
    if case.expected_vendor_key not in by_vendor:
        failures.append(f"{case.label}: expected vendor {case.expected_vendor_key} not present in result")
    for filename, det in detection.items():
        if (det or {}).get("processing_mode") == "ai_assisted":
            failures.append(f"{case.label}: {filename} routed to AI-assisted mode during utility QA")
        if (det or {}).get("vendor_key") != case.expected_vendor_key:
            failures.append(
                f"{case.label}: {filename} detected as {(det or {}).get('vendor_key')} instead of {case.expected_vendor_key}"
            )

    if case.require_rows and row_count <= 0:
        failures.append(f"{case.label}: expected preview rows but produced none")
    if not case.allow_manual_review and review:
        failures.append(f"{case.label}: unexpected manual review rows: {len(review)}")

    if rows:
        validation = validate_utility_template_rows(
            rows,
            valid_gl_accounts=load_chart_of_accounts(),
        )
        if not validation.ok:
            failures.append(f"{case.label}: invalid utility rows: {validation.blocking_reasons}")
        if validation.warnings:
            warnings.append(f"{case.label}: row warnings: {validation.warnings}")
        _validate_invoice_totals(case, invoices, failures, warnings)
        if case.community_master:
            _validate_community_case(case, invoices, failures)

    return {
        "label": case.label,
        "vendor_key": case.expected_vendor_key,
        "invoices": len(invoices),
        "rows": row_count,
        "manual_review": len(review),
        "manual_review_reasons": _manual_review_reasons(review),
        "files_supported": summary.get("files_supported"),
        "files_unsupported": summary.get("files_unsupported"),
    }


def _manual_review_reasons(review_rows: list[dict[str, Any]]) -> list[str]:
    reasons: set[str] = set()
    for row in review_rows:
        raw_reasons = row.get("reasons") or row.get("reason") or []
        if isinstance(raw_reasons, str):
            raw_reasons = [raw_reasons]
        for reason in raw_reasons:
            if str(reason).strip():
                reasons.add(str(reason).strip())
    return sorted(reasons)


def _validate_invoice_totals(
    case: UtilityE2ECase,
    invoices: list[dict[str, Any]],
    failures: list[str],
    warnings: list[str],
) -> None:
    for index, inv in enumerate(invoices, start=1):
        expected = _decimal_or_none(inv.get("total_amount"))
        if expected is None:
            warnings.append(f"{case.label}: invoice {index} has no total_amount metadata")
            continue
        actual = sum(
            (_decimal_or_none(row.get("Amount")) or Decimal("0.00"))
            for row in (inv.get("rows") or [])
        )
        if actual.quantize(Decimal("0.01")) != expected.quantize(Decimal("0.01")):
            failures.append(
                f"{case.label}: invoice {index} rows total {actual} does not reconcile to {expected}"
            )


def _validate_community_case(
    case: UtilityE2ECase,
    invoices: list[dict[str, Any]],
    failures: list[str],
) -> None:
    if len(invoices) != 1:
        failures.append(f"{case.label}: community/master sample should produce one master invoice; got {len(invoices)}")
    for inv_index, inv in enumerate(invoices, start=1):
        numbers = [
            str(row.get("Line Item Number") or "").strip()
            for row in (inv.get("rows") or [])
        ]
        expected = [str(i) for i in range(1, len(numbers) + 1)]
        if numbers != expected:
            failures.append(
                f"{case.label}: invoice {inv_index} line item numbering is {numbers}, expected {expected}"
            )


def _decimal_or_none(value: Any) -> Decimal | None:
    try:
        if value is None or value == "":
            return None
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value.lower())


def _finish(failures: list[str], warnings: list[str], results: list[dict[str, Any]]) -> int:
    if results:
        print("U4 utility e2e output summary:")
        for item in results:
            print(
                f"  - {item['label']}: {item['invoices']} invoice(s), "
                f"{item['rows']} row(s), {item['manual_review']} review item(s)"
            )
    if warnings:
        print("Warnings:")
        for warning in warnings:
            print(f"  - {warning}")
    if failures:
        print("FAIL:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("PASS: U4 utility e2e golden outputs are valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
