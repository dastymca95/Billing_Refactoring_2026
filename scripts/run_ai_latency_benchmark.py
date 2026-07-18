"""Run an accounting-safe AI batch benchmark in an isolated runtime root."""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return round(ordered[index], 3)


def _trace_summary(trace_path: Path) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    if trace_path.is_file():
        for line in trace_path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(event, dict):
                events.append(event)
    attempts = [event for event in events if event.get("event") == "provider_attempt"]
    cache_events = [event for event in events if event.get("event") == "cache"]
    latencies = [float(event.get("elapsed_ms") or 0) for event in attempts]
    calls = Counter(
        (
            str(event.get("provider") or "unknown"),
            str(event.get("profile_id") or "unknown"),
            str(event.get("model") or "unknown"),
        )
        for event in attempts
    )
    return {
        "provider_attempts": len(attempts),
        "provider_calls": [
            {"provider": key[0], "profile": key[1], "model": key[2], "count": count}
            for key, count in sorted(calls.items())
        ],
        "provider_latency_ms_p50": _percentile(latencies, 0.50),
        "provider_latency_ms_p95": _percentile(latencies, 0.95),
        "provider_latency_ms_total": round(sum(latencies), 3),
        "provider_semaphore_wait_ms": round(
            sum(float(event.get("provider_semaphore_wait_ms") or 0) for event in attempts), 3
        ),
        "peak_concurrency": max(
            [int(event.get("provider_peak_concurrency") or 0) for event in attempts] or [0]
        ),
        "estimated_cost_usd": round(
            sum(float(event.get("estimated_cost_usd") or 0) for event in attempts), 6
        ),
        "media_bytes": sum(int(event.get("media_bytes") or 0) for event in attempts),
        "media_pixels": sum(int(event.get("media_pixels") or 0) for event in attempts),
        "cache_hits": sum(event.get("cache_status") == "hit" for event in cache_events),
        "cache_misses": sum(event.get("cache_status") == "miss" for event in cache_events),
        "cache_by_layer": dict(Counter(
            f"{event.get('cache_layer') or 'unknown'}:{event.get('cache_status') or 'unknown'}"
            for event in cache_events
        )),
        "circuit_breaker_events": [
            {
                key: event.get(key)
                for key in (
                    "provider", "model", "endpoint_surface", "capability",
                    "action", "http_status", "failure_code",
                )
            }
            for event in events
            if event.get("event") == "provider_circuit_breaker"
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("batch_id")
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    args = parser.parse_args()

    runtime_root = args.runtime_root.resolve()
    expected_batch = runtime_root / "batches" / args.batch_id
    if not expected_batch.is_dir() or not (expected_batch / "input").is_dir():
        raise SystemExit("Isolated benchmark batch is not provisioned.")

    from webapp.backend import settings

    settings.WEBAPP_DATA_ROOT = runtime_root
    settings.BATCHES_ROOT = runtime_root / "batches"

    from webapp.backend.api import processing
    from webapp.backend.services import (
        approved_invoice_corrections,
        batch_processor,
        row_normalizer,
    )

    started = time.perf_counter()
    process_started = time.perf_counter()
    result = batch_processor.process_batch(args.batch_id, dry_run=True)
    process_seconds = time.perf_counter() - process_started
    normalize_started = time.perf_counter()
    row_normalizer.normalize_result(result)
    processing._apply_learned_corrections_to_result(result)
    approved_invoice_corrections.apply_to_result(result, batch_id=args.batch_id)
    normalize_seconds = time.perf_counter() - normalize_started
    elapsed_seconds = time.perf_counter() - started

    args.result.parent.mkdir(parents=True, exist_ok=True)
    args.result.write_text(json.dumps(result, default=str, indent=2), encoding="utf-8")
    trace_path = runtime_root / "batches" / args.batch_id / "audit" / "ai_request_trace.jsonl"
    invoices = list(result.get("all_invoices") or [])
    rows = [row for invoice in invoices for row in list(invoice.get("rows") or [])]
    metrics = {
        "batch_id": args.batch_id,
        "runtime_root": str(runtime_root),
        "dry_run": True,
        "elapsed_seconds": elapsed_seconds,
        "process_seconds": process_seconds,
        "normalize_seconds": normalize_seconds,
        "invoice_count": len(invoices),
        "row_count": len(rows),
        "manual_review_count": len(result.get("all_manual_review") or []),
        "rows_with_document_facts": sum(
            isinstance((row.get("_meta") or {}).get("document_facts"), dict) for row in rows
        ),
        "rows_with_source_text": sum(
            isinstance((row.get("_meta") or {}).get("source_text"), dict) for row in rows
        ),
        **_trace_summary(trace_path),
    }
    args.metrics.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    print(json.dumps(metrics, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
