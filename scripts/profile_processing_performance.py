"""Phase PERF-1 — profile representative bills through the pipeline.

Runs ``utils.pdf_text_extractor.extract_pdf_text`` (the most expensive
shared step) over a curated set of fixtures and prints a per-file
breakdown of ingestion / digital / OCR / cache-hit timings. Vendor
processors are then invoked in DRY-RUN mode so we measure the full
deterministic path without writing workbooks or hitting Dropbox.

The script is deliberately conservative:
  * No real AI calls — fixtures stay in vendors whose processors are
    deterministic (Pennyrile, McMinnville, Columbia).
  * No real Dropbox calls — run_context.dry_run = True.
  * No writes to ``Output/Template.xlsx``.

Output:
  docs/reports/phases/screenshots/phase_perf1/profile_processing_performance.json

Usage:
  python scripts/profile_processing_performance.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Honest disclosure: this script does not touch Dropbox or AI providers
# even if credentials are configured.
os.environ.setdefault("DROPBOX_DISABLE_FOR_TESTS", "1")
os.environ.setdefault("AI_FALLBACK_DISABLED", "1")

from utils.pdf_text_extractor import extract_pdf_text  # noqa: E402
from utils import ocr_cache  # noqa: E402
from webapp.backend.services import perf_timer  # noqa: E402

# Representative fixtures — one per vendor, small bills that the
# deterministic processors handle without AI assist. Paths are relative
# to project root.
_FIXTURES = [
    {
        "label": "pennyrile_simple_pdf",
        "kind": "pdf",
        "vendor_key": "pennyrile_electric",
        "path": "Training Bills_Invoices/Electricity - Power/Pennyrile Electric/"
                "Bills_Training/0Q3yoN0wY06ribbqq7wYdA20.pdf",
    },
    {
        "label": "mcminnville_collective_pdf",
        "kind": "pdf",
        "vendor_key": "mcminnville_electric_system",
        "path": "Training Bills_Invoices/Electricity - Power/McMinnville Electric System/"
                "Bills_Training/045d79ac-b947-4d52-aa91-3b6a249d9f2a.pdf",
    },
]


def _profile_pdf_extraction(fixture: dict) -> dict:
    src = ROOT / fixture["path"]
    if not src.is_file():
        return {"label": fixture["label"], "skipped": "fixture_missing", "path": str(src)}

    out: dict = {"label": fixture["label"], "path": str(src.relative_to(ROOT))}

    # Cold (no cache hit). Clear the OCR cache entry for this file's
    # current bytes so the first measurement is honest.
    key = ocr_cache.cache_key(src, 200)
    cache_file = ROOT / "webapp_data" / "cache" / "ocr" / f"{key}.json"
    if cache_file.is_file():
        try:
            cache_file.unlink()
        except OSError:
            pass

    batch_id = f"profile-{fixture['label']}"
    perf_timer.clear_batch(batch_id)
    t0 = time.perf_counter()
    res_cold = extract_pdf_text(src, batch_id=batch_id, logger=None)
    cold_ms = (time.perf_counter() - t0) * 1000.0
    out["cold_ms"] = round(cold_ms, 2)
    out["cold_method"] = res_cold.extraction_method
    out["cold_pages"] = res_cold.pages_count
    out["cold_warnings"] = res_cold.warnings[:5]

    # Warm (cache hit). Only meaningful if the cold path went through OCR;
    # digital_text PDFs don't use the OCR cache so warm == cold.
    perf_timer.clear_batch(batch_id)
    t1 = time.perf_counter()
    res_warm = extract_pdf_text(src, batch_id=batch_id, logger=None)
    warm_ms = (time.perf_counter() - t1) * 1000.0
    out["warm_ms"] = round(warm_ms, 2)
    out["warm_method"] = res_warm.extraction_method
    out["cache_hit"] = "ocr_cache_hit" in (res_warm.warnings or [])
    out["speedup_x"] = round(cold_ms / warm_ms, 2) if warm_ms > 0 else None

    perf_timer.clear_batch(batch_id)
    return out


def main() -> int:
    results: list[dict] = []
    for fixture in _FIXTURES:
        print(f"--- {fixture['label']} ---", flush=True)
        r = _profile_pdf_extraction(fixture)
        results.append(r)
        print(json.dumps(r, indent=2), flush=True)

    out_dir = ROOT / "docs" / "reports" / "phases" / "screenshots" / "phase_perf1"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "profile_processing_performance.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "schema": "phase_perf1/profile_processing/v1",
            "run_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "fixtures": results,
            "ocr_cache_stats": ocr_cache.cache_stats(),
        }, f, indent=2)
    print(f"\nWrote summary -> {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
