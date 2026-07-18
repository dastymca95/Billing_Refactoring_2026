"""Phase PERF-1 — Lightweight, thread-safe performance timer.

A minimal-overhead timing collector that the batch_processor + ancillary
services use to record per-step durations without spamming logs. The
timings are kept in process memory under `_BATCH_TIMINGS` and flushed
to disk (``audit/performance.json``) at the end of each batch run.

Design constraints
------------------
* **Cheap**: each ``perf_step`` call must add < 0.1 ms overhead so we
  can sprinkle them around hot paths without skewing the measurement.
* **Thread-safe**: vendor processors can run on background threads
  (cross-batch queue + cancel) — the timer's append must be safe.
* **Side-effect free if disabled**: when ``PERF_TIMER_DISABLED=1`` is
  set, the context manager becomes a no-op so production deployments
  can opt out cheaply.
* **Never logs sensitive content**: only ``step`` + ``ms`` + optional
  small ``meta`` dict are stored. Callers must NOT pass full invoice
  bodies or API keys.

Usage
-----

>>> from webapp.backend.services.perf_timer import perf_step, get_batch_timings
>>> with perf_step("ocr.tesseract", batch_id="b1", meta={"page": 1}):
...     do_ocr()
>>> get_batch_timings("b1")
[{'step': 'ocr.tesseract', 'ms': 142.3, 'meta': {'page': 1}, 't': 1715...}]
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

_LOG = logging.getLogger(__name__)
_LOCK = threading.RLock()
_BATCH_TIMINGS: dict[str, list[dict[str, Any]]] = {}
_DISABLED = os.environ.get("PERF_TIMER_DISABLED", "").strip() == "1"


def is_enabled() -> bool:
    return not _DISABLED


def record(batch_id: str, step: str, ms: float,
           meta: dict[str, Any] | None = None) -> None:
    """Append a single timing entry for ``batch_id``."""
    if _DISABLED or not batch_id:
        return
    entry: dict[str, Any] = {
        "step": step,
        "ms": round(float(ms), 3),
        "t": time.time(),
    }
    if meta:
        # Defensive copy + size cap so a misbehaving caller can't blow
        # up memory by stuffing the entire OCR text into meta.
        safe_meta: dict[str, Any] = {}
        for k, v in meta.items():
            if k.startswith("_"):
                continue
            if isinstance(v, str):
                safe_meta[k] = v[:200]
            elif isinstance(v, (int, float, bool)) or v is None:
                safe_meta[k] = v
            else:
                safe_meta[k] = repr(v)[:200]
        entry["meta"] = safe_meta
    with _LOCK:
        _BATCH_TIMINGS.setdefault(batch_id, []).append(entry)


@contextmanager
def perf_step(step: str, *, batch_id: str | None = None,
              meta: dict[str, Any] | None = None) -> Iterator[None]:
    """Context manager that records elapsed wall-clock for ``step``."""
    if _DISABLED or not batch_id:
        yield
        return
    t0 = time.perf_counter()
    try:
        yield
    finally:
        ms = (time.perf_counter() - t0) * 1000.0
        record(batch_id, step, ms, meta=meta)


def get_batch_timings(batch_id: str) -> list[dict[str, Any]]:
    with _LOCK:
        return list(_BATCH_TIMINGS.get(batch_id) or [])


def summarize(batch_id: str) -> dict[str, Any]:
    """Aggregate per-step timings for `batch_id` into a dashboard-ready
    summary: total_ms, slowest steps, AI/OCR/render breakdowns."""
    entries = get_batch_timings(batch_id)
    if not entries:
        return {
            "batch_id": batch_id,
            "total_ms": 0.0,
            "step_count": 0,
            "by_step": {},
            "slowest_steps": [],
            "warnings": ["No timings recorded for this batch."],
        }
    by_step: dict[str, dict[str, Any]] = {}
    for e in entries:
        s = e["step"]
        if s not in by_step:
            by_step[s] = {"count": 0, "total_ms": 0.0, "max_ms": 0.0}
        by_step[s]["count"] += 1
        by_step[s]["total_ms"] = round(by_step[s]["total_ms"] + e["ms"], 3)
        by_step[s]["max_ms"] = round(max(by_step[s]["max_ms"], e["ms"]), 3)
    total_ms = round(sum(b["total_ms"] for b in by_step.values()), 3)
    slowest = sorted(
        ({"step": s, **b} for s, b in by_step.items()),
        key=lambda x: x["total_ms"],
        reverse=True,
    )[:10]
    warnings: list[str] = []
    # Heuristic flags — useful in audit reports without false positives.
    for s, b in by_step.items():
        if s.startswith("ai.") and b["max_ms"] > 8000:
            warnings.append(
                f"{s} max {b['max_ms']:.0f} ms — exceeds 8s soft cap.",
            )
        if s.startswith("ocr.") and b["max_ms"] > 5000:
            warnings.append(
                f"{s} max {b['max_ms']:.0f} ms — exceeds 5s soft cap.",
            )
    return {
        "batch_id": batch_id,
        "total_ms": total_ms,
        "step_count": len(entries),
        "by_step": by_step,
        "slowest_steps": slowest,
        "warnings": warnings,
    }


def flush_to_disk(batch_id: str, audit_dir: Path) -> Path | None:
    """Persist the current batch's timings + summary.

    PERF-2 writes both:
    * ``performance.json`` for dashboard-style summaries.
    * ``performance.jsonl`` for append-friendly profiling and diffing.

    Both files are rewritten from the current in-memory snapshot so
    repeated endpoint calls do not duplicate rows.
    """
    if _DISABLED:
        return None
    entries = get_batch_timings(batch_id)
    if not entries:
        return None
    audit_dir.mkdir(parents=True, exist_ok=True)
    out = audit_dir / "performance.json"
    out_jsonl = audit_dir / "performance.jsonl"
    payload = {
        "schema": "perf_timer/v2",
        "batch_id": batch_id,
        "summary": summarize(batch_id),
        "entries": entries,
    }
    try:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)
        with open(out_jsonl, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps({"batch_id": batch_id, **entry}, default=str))
                f.write("\n")
            f.write(json.dumps({
                "batch_id": batch_id,
                "step": "summary",
                "summary": payload["summary"],
                "t": time.time(),
            }, default=str))
            f.write("\n")
    except Exception as e:
        _LOG.warning("perf_timer flush failed for %s: %s", batch_id, e)
        return None
    return out


def read_jsonl(path: Path, *, limit: int = 500) -> list[dict[str, Any]]:
    """Read a persisted performance JSONL file defensively."""
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    rows.append(parsed)
    except OSError:
        return []
    if limit > 0 and len(rows) > limit:
        return rows[-limit:]
    return rows


def clear_batch(batch_id: str) -> None:
    with _LOCK:
        _BATCH_TIMINGS.pop(batch_id, None)


def all_batch_ids() -> list[str]:
    with _LOCK:
        return list(_BATCH_TIMINGS.keys())
