from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp.backend import settings  # noqa: E402
from webapp.backend.services.output_contract_validator import validate_row_contract  # noqa: E402
from webapp.backend.services.utility_processor_common import load_chart_of_accounts  # noqa: E402


BATCHES_ROOT = ROOT / "webapp_data" / "batches"
QA_ROOT = ROOT / "webapp_data" / "qa"
VENDORS_DIR = ROOT / "config" / "vendors"
OUTPUT_TEMPLATE = ROOT / "Output" / "Template.xlsx"


def main() -> int:
    failures: list[str] = []
    warnings: list[str] = []
    before_output_mtime = OUTPUT_TEMPLATE.stat().st_mtime_ns if OUTPUT_TEMPLATE.is_file() else None
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    active_vendors = _active_deterministic_vendors()
    batch_inventory = _inventory_batches(warnings)
    preview_summary = _summarize_cached_previews(batch_inventory)
    detection_summary = _summarize_detection(batch_inventory)

    payload = {
        "phase": "QA-1",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "active_deterministic_vendor_count": len(active_vendors),
        "active_deterministic_vendors": active_vendors,
        "batch_count": len(batch_inventory),
        "preview_summary": preview_summary,
        "detection_summary": detection_summary,
        "warnings": warnings,
        "batches": batch_inventory,
        "notes": [
            "Cached preview violations are reported as inventory because older batches may predate QA-1.",
            "Fresh deterministic utility reprocessing is covered by smoke_utility_e2e_outputs.py and smoke_utility_processors.py.",
            "No Dropbox calls, AI calls, source-file writes, or Output/Template.xlsx writes are performed by this script.",
        ],
    }
    QA_ROOT.mkdir(parents=True, exist_ok=True)
    output_path = QA_ROOT / f"full_batch_regression_{timestamp}.json"
    output_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    if before_output_mtime is not None and OUTPUT_TEMPLATE.stat().st_mtime_ns != before_output_mtime:
        failures.append("Output/Template.xlsx was modified during full batch regression inventory")

    print(f"Full batch regression inventory written: {output_path}")
    print(
        f"Batches: {len(batch_inventory)} | cached previews: {preview_summary['batches_with_preview']} | "
        f"cached rows: {preview_summary['rows_total']} | cached contract flags: {preview_summary['contract_flags_total']}"
    )
    if warnings:
        print("Warnings:")
        for warning in warnings[:20]:
            print(f"  - {warning}")
        if len(warnings) > 20:
            print(f"  - ... {len(warnings) - 20} more warning(s)")

    if failures:
        print("FAIL:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("PASS: full batch inventory and cached preview contract audit completed.")
    return 0


def _active_deterministic_vendors() -> list[str]:
    vendors: list[str] = []
    for path in sorted(VENDORS_DIR.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        overlay = data.get("utility_processing") or {}
        identity = data.get("vendor_identity") or {}
        status = str(overlay.get("status") or identity.get("status") or "").lower()
        mode = str(overlay.get("processing_mode") or data.get("processing_mode") or "").lower()
        if status == "active" and mode == "deterministic":
            vendors.append(path.stem)
    return vendors


def _inventory_batches(warnings: list[str]) -> list[dict[str, Any]]:
    if not BATCHES_ROOT.is_dir():
        return []
    valid_gls = load_chart_of_accounts()
    batches: list[dict[str, Any]] = []
    for batch_dir in sorted(BATCHES_ROOT.iterdir(), key=lambda p: p.name):
        if not batch_dir.is_dir() or not batch_dir.name.startswith("batch_"):
            continue
        meta = _read_json(batch_dir / "batch_metadata.json", warnings)
        progress = _read_json(batch_dir / "progress.json", warnings)
        preview_path = batch_dir / "processed" / "_webapp_result.json"
        preview = _read_json(preview_path, warnings) if preview_path.is_file() else None
        files = _inventory_files(batch_dir, meta, preview if isinstance(preview, dict) else {}, warnings)
        preview_info = _preview_info(preview, valid_gls) if isinstance(preview, dict) else {}
        revisions_dir = batch_dir / "revisions"
        revision_index = _read_json(revisions_dir / "index.json", warnings) if (revisions_dir / "index.json").is_file() else {}

        batches.append(
            {
                "batch_id": batch_dir.name,
                "batch_name": meta.get("batch_name") or meta.get("name") or "",
                "status": meta.get("status") or progress.get("status") or "",
                "file_count": len(files),
                "files": files,
                "detected_vendors": sorted(
                    {
                        str((file.get("detection") or {}).get("vendor_key") or "")
                        for file in files
                        if (file.get("detection") or {}).get("vendor_key")
                    }
                ),
                "preview_exists": preview_path.is_file(),
                "preview_rows": int(preview_info.get("rows_total") or 0),
                "preview_invoices": int(preview_info.get("invoices_total") or 0),
                "manual_review_count": int(preview_info.get("manual_review_total") or 0),
                "contract_flags_total": int(preview_info.get("contract_flags_total") or 0),
                "contract_flags_by_type": preview_info.get("contract_flags_by_type") or {},
                "single_invoice_data_exists": bool(preview_info.get("invoices_total")),
                "revisions_count": len(revision_index.get("revisions") or []) if isinstance(revision_index, dict) else 0,
                "processed_at": (preview or {}).get("processed_at") if isinstance(preview, dict) else "",
            }
        )
    return batches


def _inventory_files(
    batch_dir: Path,
    meta: dict[str, Any],
    preview: dict[str, Any],
    warnings: list[str],
) -> list[dict[str, Any]]:
    input_dir = batch_dir / "input"
    cache = meta.get("file_detection_cache") if isinstance(meta.get("file_detection_cache"), dict) else {}
    files: list[dict[str, Any]] = []
    if not input_dir.is_dir():
        return files
    for path in sorted(input_dir.rglob("*")):
        if not path.is_file():
            continue
        # Vendor staging subfolders are processor internals. Keep them in the
        # inventory, but do not double-count staged copies when the root upload
        # exists under the same basename.
        detection = cache.get(path.name) or (preview.get("detection") or {}).get(path.name) or {}
        if not detection:
            detection = {"vendor_key": "", "reason": "detection_not_cached"}
        files.append(
            {
                "filename": path.name,
                "relative_path": str(path.relative_to(batch_dir)),
                "extension": path.suffix.lower(),
                "size_bytes": path.stat().st_size,
                "detection": detection,
            }
        )
    return files


def _preview_info(preview: dict[str, Any], valid_gls: dict[str, str]) -> dict[str, Any]:
    invoices = list(preview.get("all_invoices") or [])
    manual_review = list(preview.get("all_manual_review") or [])
    rows = [
        row
        for invoice in invoices
        for row in (invoice.get("rows") or [])
        if isinstance(row, dict)
    ]
    counter: Counter[str] = Counter()
    for row in rows:
        for flag in validate_row_contract(row, valid_gl_accounts=valid_gls):
            counter[flag] += 1
    return {
        "invoices_total": len(invoices),
        "rows_total": len(rows),
        "manual_review_total": len(manual_review),
        "contract_flags_total": sum(counter.values()),
        "contract_flags_by_type": dict(sorted(counter.items())),
    }


def _summarize_cached_previews(batches: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "batches_with_preview": sum(1 for b in batches if b.get("preview_exists")),
        "rows_total": sum(int(b.get("preview_rows") or 0) for b in batches),
        "manual_review_total": sum(int(b.get("manual_review_count") or 0) for b in batches),
        "contract_flags_total": sum(int(b.get("contract_flags_total") or 0) for b in batches),
    }


def _summarize_detection(batches: list[dict[str, Any]]) -> dict[str, Any]:
    counter: Counter[str] = Counter()
    for batch in batches:
        for vendor in batch.get("detected_vendors") or []:
            counter[vendor] += 1
    return dict(counter.most_common())


def _read_json(path: Path, warnings: list[str]) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        warnings.append(f"{path}: failed to read JSON: {type(exc).__name__}: {exc}")
        return {}


if __name__ == "__main__":
    raise SystemExit(main())
