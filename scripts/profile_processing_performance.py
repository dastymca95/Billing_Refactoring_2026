"""Phase PERF-2 - profile representative bills through the pipeline.

The script is deliberately conservative:
  * No real AI calls: ``AI_FALLBACK_DISABLED=1`` is set.
  * No real Dropbox calls: ``DROPBOX_DISABLE_FOR_TESTS=1`` is set and
    vendor processors are invoked with ``dry_run=True``.
  * No writes to ``Output/Template.xlsx``.

Output:
  docs/reports/phases/screenshots/phase_perf2/profile_processing_performance.json

Usage:
  python scripts/profile_processing_performance.py
"""

from __future__ import annotations

import json
import gc
import logging
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DROPBOX_DISABLE_FOR_TESTS", "1")
os.environ.setdefault("AI_FALLBACK_DISABLED", "1")

from utils import ocr_cache  # noqa: E402
from utils.pdf_text_extractor import extract_pdf_text  # noqa: E402
from webapp.backend.services import batch_processor, batch_store, perf_timer  # noqa: E402


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


def _sum_steps(entries: list[dict], *needles: str) -> float:
    total = 0.0
    for entry in entries:
        step = str(entry.get("step") or "").lower()
        if any(needle in step for needle in needles):
            total += float(entry.get("ms") or 0.0)
    return round(total, 2)


def _profile_pdf_extraction(fixture: dict) -> dict:
    src = ROOT / fixture["path"]
    if not src.is_file():
        return {"label": fixture["label"], "skipped": "fixture_missing", "path": str(src)}

    out: dict = {"label": fixture["label"], "path": str(src.relative_to(ROOT))}
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
    cold_method = str(res_cold.extraction_method or "")
    out.update(
        {
            "ingestion_ms": round(cold_ms, 2),
            "ocr_ms": round(cold_ms, 2) if "ocr" in cold_method.lower() else 0.0,
            "ai_ms": 0.0,
            "vision_ms": 0.0,
            "reasoner_ms": 0.0,
            "validation_ms": 0.0,
            "preview_write_ms": 0.0,
            "total_ms": round(cold_ms, 2),
            "cold_method": cold_method,
            "cold_pages": res_cold.pages_count,
            "cold_warnings": res_cold.warnings[:5],
        },
    )

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


def _profile_processor_dry_run(fixture: dict) -> dict:
    src = ROOT / fixture["path"]
    if not src.is_file():
        return {"label": fixture["label"], "skipped": "fixture_missing", "path": str(src)}

    batch_id = batch_store.create_batch()
    bdir = batch_store.get_batch_dir(batch_id)
    (bdir / "profile_perf2_tmp.txt").write_text(
        "Temporary batch created by scripts/profile_processing_performance.py\n",
        encoding="utf-8",
    )
    shutil.copy2(src, batch_store.get_input_dir(batch_id) / src.name)
    perf_timer.clear_batch(batch_id)
    t0 = time.perf_counter()
    try:
        result = batch_processor.process_batch(
            batch_id,
            dry_run=True,
            finalize_progress=False,
        )
        total_ms = (time.perf_counter() - t0) * 1000.0
        entries = perf_timer.get_batch_timings(batch_id)
        invoices = result.get("all_invoices") or []
        return {
            "label": fixture["label"],
            "batch_id": batch_id,
            "file": src.name,
            "ingestion_ms": _sum_steps(entries, "vendor.detect", "detect"),
            "ocr_ms": _sum_steps(entries, "ocr"),
            "ai_ms": _sum_steps(entries, "ai"),
            "vision_ms": _sum_steps(entries, "vision"),
            "reasoner_ms": _sum_steps(entries, "reasoner", "canonical"),
            "validation_ms": _sum_steps(entries, "validation", "row_normalizer"),
            "preview_write_ms": _sum_steps(entries, "preview", "revision"),
            "processor_ms": _sum_steps(entries, "processor."),
            "total_ms": round(total_ms, 2),
            "invoices": len(invoices),
            "rows": sum(len((inv or {}).get("rows") or []) for inv in invoices),
            "manual_review": len(result.get("all_manual_review") or []),
            "slowest_steps": perf_timer.summarize(batch_id).get("slowest_steps", [])[:8],
        }
    except Exception as exc:
        return {
            "label": fixture["label"],
            "batch_id": batch_id,
            "error": f"{type(exc).__name__}: {exc}",
            "total_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        }
    finally:
        perf_timer.clear_batch(batch_id)
        logging.shutdown()
        gc.collect()
        for _ in range(3):
            shutil.rmtree(bdir, ignore_errors=True)
            if not bdir.exists():
                break
            time.sleep(0.25)


def main() -> int:
    extraction_results: list[dict] = []
    processor_results: list[dict] = []
    for fixture in _FIXTURES:
        print(f"--- {fixture['label']} ---", flush=True)
        extraction = _profile_pdf_extraction(fixture)
        extraction_results.append(extraction)
        print(json.dumps(extraction, indent=2), flush=True)
        processor = _profile_processor_dry_run(fixture)
        processor_results.append(processor)
        print(json.dumps(processor, indent=2), flush=True)

    out_dir = ROOT / "docs" / "reports" / "phases" / "screenshots" / "phase_perf2"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "profile_processing_performance.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "schema": "phase_perf2/profile_processing/v1",
                "run_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "extraction_fixtures": extraction_results,
                "processor_dry_run_fixtures": processor_results,
                "ocr_cache_stats": ocr_cache.cache_stats(),
            },
            f,
            indent=2,
        )
    print(f"\nWrote summary -> {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
