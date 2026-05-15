from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp.backend.api.batches import _write_metadata  # noqa: E402
from webapp.backend.services import batch_processor, batch_store, row_normalizer  # noqa: E402
from webapp.backend.services.description_builder import (  # noqa: E402
    build_invoice_description,
    build_line_item_description,
)
from webapp.backend.services.output_contract_validator import validate_row_contract  # noqa: E402
from webapp.backend.services.utility_processor_common import (  # noqa: E402
    load_chart_of_accounts,
    validate_utility_template_rows,
)
from utils.text_normalization import normalize_service_address_for_description  # noqa: E402


TEMPLATE_PATH = ROOT / "Output" / "Template.xlsx"
TRAINING_ROOT = ROOT / "Training Bills_Invoices"


TARGETED_CASES = [
    (
        "Alabama Power",
        TRAINING_ROOT / "Electricity - Power" / "Alabama Power" / "Bills_Training" / "Bill - 2026-04-24T103149.660.PDF",
    ),
    (
        "Tennessee American Water",
        TRAINING_ROOT / "Water - Sewer" / "Tennessee American Water" / "Bills_Training" / "1026-210052442136 Apr 26.pdf",
    ),
]


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    before_output_mtime = TEMPLATE_PATH.stat().st_mtime_ns if TEMPLATE_PATH.is_file() else None
    failures: list[str] = []
    summaries: list[dict[str, Any]] = []

    _assert_builder_contract(failures)
    _assert_validator_contract(failures)

    for label, source in TARGETED_CASES:
        if not source.is_file():
            failures.append(f"{label}: training sample missing: {source}")
            continue
        try:
            result = _process_sample(source)
        except Exception as exc:  # pragma: no cover - smoke guard
            failures.append(f"{label}: processing raised {type(exc).__name__}: {exc}")
            continue
        summary = _validate_processed_rows(label, result, failures)
        summaries.append(summary)

    if before_output_mtime is not None and TEMPLATE_PATH.stat().st_mtime_ns != before_output_mtime:
        failures.append("Output/Template.xlsx was modified during description contract smoke")

    for summary in summaries:
        print(
            f"{summary['label']}: {summary['invoices']} invoice(s), "
            f"{summary['rows']} row(s), descriptions ok"
        )

    if failures:
        print("FAIL:")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("PASS: canonical description contract is enforced.")
    return 0


def _assert_builder_contract(failures: list[str]) -> None:
    invoice = {
        "category": "utilities",
        "service_period_start": "03/26/2026",
        "service_period_end": "04/27/2026",
        "service_address": "1050 DENZIL DR, HOPKINSVILLE, KY 42240",
        "property_name": "Aspen Meadow",
    }
    inv_desc = build_invoice_description(invoice)
    expected = "03/26/26-04/27/26 - 1050 Denzil Dr"
    if inv_desc.description != expected:
        failures.append(f"description builder used wrong target: {inv_desc.description!r}")
    if "Aspen Meadow" in inv_desc.description:
        failures.append("description builder used property name while service address was present")
    if inv_desc.review_flags:
        failures.append(f"description builder returned unexpected review flags: {inv_desc.review_flags}")

    with_unit = dict(invoice)
    with_unit["unit_number"] = "1200-6"
    unit_desc = build_invoice_description(with_unit).description
    if unit_desc != "03/26/26-04/27/26 - 1200-6 1050 Denzil Dr":
        failures.append(f"description builder failed unit/address composition: {unit_desc!r}")

    line_desc = build_line_item_description(
        invoice,
        {"source_line_description": "FUEL RECOVERY ADJUSTMENT"},
    ).description
    if line_desc != "03/26/26-04/27/26 - 1050 Denzil Dr - Fuel Recovery Adjustment":
        failures.append(f"line description builder failed Proper Case: {line_desc!r}")

    fallback = build_invoice_description(
        {
            "category": "utilities",
            "service_period_start": "03/26/2026",
            "service_period_end": "04/27/2026",
            "property_name": "Aspen Meadow",
        }
    )
    if "service_address_missing_or_unresolved" not in fallback.review_flags:
        failures.append("utility property-name fallback did not flag missing service address")


def _assert_validator_contract(failures: list[str]) -> None:
    valid_gls = load_chart_of_accounts()
    bad_row = {
        "Invoice Number": "341340.0094 Apr 26",
        "Bill or Credit": "Bill",
        "Invoice Date": "04/30/2026",
        "Accounting Date": "04/30/2026",
        "Vendor": "Alabama Power",
        "Invoice Description": "03/26/26-04/27/26 - Aspen Meadow",
        "Line Item Number": "1",
        "Property Abbreviation": "AMA",
        "Location": "",
        "GL Account": "6910",
        "Line Item Description": "03/26/26-04/27/26 - Aspen Meadow - Electric Service",
        "Amount": "100.00",
        "Expense Type": "General",
        "Is Replacement Reserve": "false",
        "Due Date": "05/15/2026",
        "_meta": {
            "service_address": "1050 Denzil Dr",
            "matched_property_name": "Aspen Meadow",
        },
    }
    flags = validate_row_contract(bad_row, valid_gl_accounts=valid_gls)
    expected = {
        "invoice_description_missing_service_address",
        "invoice_description_uses_property_instead_of_service_address",
    }
    if not expected.issubset(set(flags)):
        failures.append(f"row contract did not flag property-name description: {flags}")


def _process_sample(source: Path) -> dict[str, Any]:
    batch_id = batch_store.create_batch()
    try:
        _write_metadata(
            batch_id,
            batch_name=f"QA1 Contract - {source.stem}",
            status="idle",
            document_mode="auto_detect",
            ai_fallback_enabled=False,
            ai_fallback_policy="never",
            phase="QA1",
        )
        target = batch_store.get_input_dir(batch_id) / source.name
        shutil.copy2(source, target)
        result = batch_processor.process_batch(batch_id, dry_run=True)
        row_normalizer.normalize_result(result)
        return result
    finally:
        shutil.rmtree(batch_store.get_batch_dir(batch_id), ignore_errors=True)


def _validate_processed_rows(
    label: str,
    result: dict[str, Any],
    failures: list[str],
) -> dict[str, Any]:
    rows = [
        row
        for invoice in (result.get("all_invoices") or [])
        for row in (invoice.get("rows") or [])
    ]
    if not rows:
        failures.append(f"{label}: no rows produced")
        return {"label": label, "invoices": 0, "rows": 0}

    validation = validate_utility_template_rows(rows, valid_gl_accounts=load_chart_of_accounts())
    if not validation.ok:
        failures.append(f"{label}: generated rows violate description contract: {validation.blocking_reasons}")

    for idx, row in enumerate(rows, start=1):
        meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
        service_address = normalize_service_address_for_description(meta.get("service_address") or "")
        prop_name = str(meta.get("matched_property_name") or meta.get("property_name") or "").strip()
        invoice_description = str(row.get("Invoice Description") or "")
        if service_address and service_address not in invoice_description:
            failures.append(
                f"{label}: row {idx} invoice description does not include service address "
                f"{service_address!r}: {invoice_description!r}"
            )
        if service_address and prop_name and prop_name in invoice_description and service_address not in invoice_description:
            failures.append(
                f"{label}: row {idx} used property name instead of service address: {invoice_description!r}"
            )

    return {
        "label": label,
        "invoices": len(result.get("all_invoices") or []),
        "rows": len(rows),
        "summary": result.get("summary") or {},
        "detection": result.get("detection") or {},
    }


if __name__ == "__main__":
    raise SystemExit(main())
