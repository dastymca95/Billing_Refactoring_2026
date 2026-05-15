from __future__ import annotations

import json
import shutil
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp.backend.api.batches import _write_metadata  # noqa: E402
from webapp.backend.services import batch_processor, batch_store, row_normalizer  # noqa: E402
from webapp.backend.services.document_ingestion import ingest_document  # noqa: E402
from webapp.backend.services.utility_processor_common import load_chart_of_accounts, validate_utility_template_rows  # noqa: E402


SAMPLE = (
    ROOT
    / "Training Bills_Invoices"
    / "Electricity - Power"
    / "Weakley County Municipal Electric System"
    / "Bills_Training"
    / "24b4b317-787e-4f1d-bbf1-cbc2012371a4.jpg"
)
TEMPLATE_PATH = ROOT / "Output" / "Template.xlsx"
REQUIRED_BLOCKERS = {
    "account_number_missing",
    "invoice_number_missing",
    "invoice_date_missing",
    "due_date_missing",
    "property_mapping_required",
    "service_address_missing_or_unresolved",
    "line_items_missing_or_unreadable",
    "ocr_confidence_low",
    "vision_recommended",
}
EXPECTED_WEAKLEY = {
    "Invoice Number": "202202 - 132786 Mar 26",
    "Vendor": "Weakley County Municipal Electric System",
    "Property Abbreviation": "SAGE",
    "Location": "10",
    "GL Account": "6920",
    "Invoice Description": "02/01/26-02/28/26 - 130 Beaumont St Apt 10",
    "Line Item Description": "02/01/26-02/28/26 - 130 Beaumont St Apt 10 - Metered Electric",
    "Due Date": "2026-03-20",
}


def main() -> int:
    failures: list[str] = []
    warnings: list[str] = []
    if not SAMPLE.is_file():
        failures.append(f"Weakley image sample missing: {SAMPLE}")
        return _finish(failures, warnings)
    before = TEMPLATE_PATH.stat().st_mtime_ns if TEMPLATE_PATH.is_file() else None

    candidate = ingest_document(SAMPLE, vendor_hint="Weakley County Municipal Electric System")
    if candidate.source_type not in {"image", "screenshot"}:
        failures.append(f"Weakley sample should ingest as image/screenshot, got {candidate.source_type}")
    if not candidate.pages:
        failures.append("Weakley image ingestion returned no page candidate")
    if candidate.text_quality_score <= 0:
        warnings.append("Weakley image OCR returned no usable text; downstream review blockers are expected.")
    if candidate.text_quality_score < 0.55 and "weak_text_quality" not in candidate.warnings:
        failures.append(f"Weakley weak OCR should include weak_text_quality warning; warnings={candidate.warnings}")

    batch_id = batch_store.create_batch()
    try:
        _write_metadata(
            batch_id,
            batch_name="QA-2 Weakley image smoke",
            status="idle",
            document_mode="auto_detect",
            ai_fallback_enabled=False,
            ai_fallback_policy="never",
            phase="QA-2",
        )
        target = batch_store.get_input_dir(batch_id) / SAMPLE.name
        shutil.copy2(SAMPLE, target)
        result = batch_processor.process_batch(batch_id, dry_run=True)
        row_normalizer.normalize_result(result)
        _validate_result(result, failures, warnings)
    finally:
        shutil.rmtree(batch_store.get_batch_dir(batch_id), ignore_errors=True)

    if before is not None and TEMPLATE_PATH.stat().st_mtime_ns != before:
        failures.append("Output/Template.xlsx was modified during Weakley image smoke")
    return _finish(failures, warnings)


def _validate_result(result: dict[str, Any], failures: list[str], warnings: list[str]) -> None:
    detection = result.get("detection") or {}
    if not detection:
        failures.append("Weakley processing returned no detection metadata")
    for filename, info in detection.items():
        if (info or {}).get("vendor_key") != "weakley_county_municipal_electric_system":
            failures.append(f"{filename}: expected Weakley deterministic vendor, got {(info or {}).get('vendor_key')}")
        if (info or {}).get("processing_mode") == "ai_assisted":
            failures.append(f"{filename}: Weakley image fell through to AI-assisted route")

    invoices = list(result.get("all_invoices") or [])
    review_rows = list(result.get("all_manual_review") or [])
    rows = [row for inv in invoices for row in (inv.get("rows") or [])]
    reasons = _review_reasons(review_rows)
    for inv in invoices:
        reasons.update(str(reason) for reason in (inv.get("manual_review_reasons") or []) if str(reason).strip())
        for row in inv.get("rows") or []:
            meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
            reasons.update(str(reason) for reason in (meta.get("manual_review_reasons") or []) if str(reason).strip())

    if not rows:
        failures.append(
            "Weakley image should now produce deterministic rows for the known QA sample, "
            "but it produced no rows."
        )
        if not review_rows:
            failures.append("Weakley image also produced no manual review explanation")
        if not (reasons & REQUIRED_BLOCKERS):
            failures.append(
                "Weakley image missing clear blocking reasons; got "
                + json.dumps(sorted(reasons), ensure_ascii=False)
            )
        return

    validation = validate_utility_template_rows(rows, valid_gl_accounts=load_chart_of_accounts())
    blocking = set(validation.blocking_reasons)
    if not validation.ok:
        failures.append(f"Weakley image produced invalid rows: {sorted(blocking)}")
    if reasons:
        failures.append(
            "Weakley image QA sample should not need manual review after OCR hardening; got "
            + json.dumps(sorted(reasons), ensure_ascii=False)
        )

    row = rows[0]
    for field, expected in EXPECTED_WEAKLEY.items():
        actual = str(row.get(field) or "").strip()
        if actual != expected:
            failures.append(f"Weakley {field}: expected {expected!r}, got {actual!r}")
    amount = _decimal_or_none(row.get("Amount"))
    if amount != Decimal("76.77"):
        failures.append(f"Weakley Amount: expected 76.77, got {row.get('Amount')!r}")

    status = str((result.get("summary") or {}).get("status") or "").lower()
    if status == "ready" and (not rows or reasons):
        failures.append("Weakley image was marked ready despite blank/reviewable extraction")


def _decimal_or_none(value: Any) -> Decimal | None:
    try:
        if value is None or value == "":
            return None
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _review_reasons(review_rows: list[dict[str, Any]]) -> set[str]:
    reasons: set[str] = set()
    for row in review_rows:
        raw = row.get("reasons") or row.get("reason") or []
        if isinstance(raw, str):
            raw = [raw]
        reasons.update(str(reason) for reason in raw if str(reason).strip())
    return reasons


def _finish(failures: list[str], warnings: list[str]) -> int:
    if warnings:
        print("Warnings:")
        for warning in warnings:
            print(f"  - {warning}")
    if failures:
        print("FAIL:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("PASS: Weakley image bill routes deterministically and produces validated ResMan rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
