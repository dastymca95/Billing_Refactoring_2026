"""Seed a safe webapp QA fixture batch.

This creates a processed-looking batch without invoking vendor processors,
Dropbox, OCR, AI, or real bill files. It is intended for browser visual QA
and Playwright smoke tests.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from webapp.backend.services import batch_store  # noqa: E402


FIXTURE_NAME = "QA Visual Fixture"


def _metadata_path(batch_id: str) -> Path:
    return batch_store.get_batch_dir(batch_id) / "batch_metadata.json"


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _find_existing_fixture() -> str | None:
    for item in batch_store.list_batches():
        batch_id = item["batch_id"]
        meta = _read_json(_metadata_path(batch_id))
        if meta.get("qa_fixture") is True and meta.get("batch_name") == FIXTURE_NAME:
            cache_path = batch_store.get_processed_dir(batch_id) / "_webapp_result.json"
            if cache_path.is_file():
                return batch_id
    return None


def _row(
    invoice_number: str,
    line_number: int,
    amount: float,
    gl_account: str,
    description: str,
    *,
    flagged: bool = False,
) -> dict:
    reasons = ["qa_visual_fixture_review"] if flagged else []
    return {
        "Invoice Number": invoice_number,
        "Bill or Credit": "Bill",
        "Invoice Date": "05/02/2026",
        "Accounting Date": "05/02/2026",
        "Vendor": "QA Utility Vendor",
        "Invoice Description": description,
        "Line Item Number": line_number,
        "Property Abbreviation": "QA",
        "Location": f"QA-{line_number}",
        "GL Account": gl_account,
        "Line Item Description": description,
        "Amount": amount,
        "Expense Type": "General",
        "Is Replacement Reserve": False,
        "Due Date": "05/30/2026",
        "Reference Number": f"QA-{invoice_number}-{line_number}",
        "Document Url": "https://example.invalid/qa-support-document.pdf",
        "_meta": {
            "manual_review_reasons": reasons,
            "match_strategy": "qa_fixture",
            "match_confidence": "high",
            "service_period_source": "qa_fixture",
            "service_period_inferred": False,
            "support_document_status": "qa_fixture",
        },
    }


def seed_fixture(*, force_new: bool = False) -> dict:
    existing = None if force_new else _find_existing_fixture()
    if existing:
        return {"batch_id": existing, "created": False}

    batch_id = batch_store.create_batch()
    now = datetime.now().isoformat(timespec="seconds")
    bdir = batch_store.get_batch_dir(batch_id)
    input_dir = batch_store.get_input_dir(batch_id)
    processed_dir = batch_store.get_processed_dir(batch_id)

    (input_dir / "qa_visual_fixture.txt").write_text(
        "QA visual fixture placeholder. This is not a real bill.\n",
        encoding="utf-8",
    )

    metadata = {
        "batch_id": batch_id,
        "batch_name": FIXTURE_NAME,
        "created_at": now,
        "updated_at": now,
        "document_mode": "auto_detect",
        "ai_fallback_enabled": False,
        "ai_fallback_policy": "never",
        "qa_fixture": True,
    }
    _metadata_path(batch_id).write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    invoices = [
        {
            "source_file": "qa_visual_fixture.txt",
            "invoice_number": "QA-1001",
            "rows": [
                _row("QA-1001", 1, 120.25, "6955", "QA service charge"),
                _row("QA-1001", 2, 8.75, "6995", "QA adjustment", flagged=True),
            ],
        },
        {
            "source_file": "qa_visual_fixture.txt",
            "invoice_number": "QA-1002",
            "rows": [
                _row("QA-1002", 1, 210.10, "6955", "QA water usage"),
                _row("QA-1002", 2, 45.90, "6940", "QA sewer usage"),
            ],
        },
        {
            "source_file": "qa_visual_fixture.txt",
            "invoice_number": "QA-1003",
            "rows": [
                _row("QA-1003", 1, 33.00, "6956", "QA service fee"),
                _row("QA-1003", 2, 12.00, "6955", "QA billing fee"),
            ],
        },
    ]
    manual_review = [
        {
            "source_file": "qa_visual_fixture.txt",
            "account_number": "QA-ACCOUNT",
            "invoice_number": "QA-1001",
            "invoice_date": "05/02/2026",
            "property_abbreviation": "QA",
            "location": "QA-2",
            "service_address": "100 QA Fixture Way",
            "total_amount": 129.00,
            "line_count": 2,
            "reasons": ["qa_visual_fixture_review"],
            "match_strategy": "qa_fixture",
            "match_confidence": "high",
            "service_period_source": "qa_fixture",
        }
    ]
    result = {
        "batch_id": batch_id,
        "summary": {
            "files_total": 1,
            "files_supported": 1,
            "files_unsupported": 0,
            "invoices_total": len(invoices),
            "manual_review_total": len(manual_review),
        },
        "by_vendor": {
            "qa_visual_fixture": {
                "summary": {
                    "run_date": datetime.now().date().isoformat(),
                    "files_processed": 1,
                    "invoices_produced": len(invoices),
                    "line_items": sum(len(inv["rows"]) for inv in invoices),
                    "invoices_flagged_for_review": len(manual_review),
                    "output_folder": str(processed_dir / "qa_visual_fixture"),
                }
            }
        },
        "detection": {
            "qa_visual_fixture.txt": {
                "vendor_key": "qa_visual_fixture",
                "confidence": 1.0,
                "reason": "QA fixture seed",
            }
        },
        "unsupported_files": [],
        "all_invoices": invoices,
        "all_manual_review": manual_review,
    }
    (processed_dir / "_webapp_result.json").write_text(
        json.dumps(result, indent=2),
        encoding="utf-8",
    )
    return {"batch_id": batch_id, "created": True}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force-new", action="store_true", help="Create a new fixture even if one already exists.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args()

    result = seed_fixture(force_new=args.force_new)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        action = "created" if result["created"] else "reused"
        print(f"{action} {FIXTURE_NAME}: {result['batch_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
