"""Replay normalization/accounting from saved facts with network access denied."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class ExternalProviderCallBlocked(RuntimeError):
    pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("batch_id")
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--golden", type=Path)
    args = parser.parse_args()

    runtime_root = args.runtime_root.resolve()
    if not (runtime_root / "batches" / args.batch_id / "input").is_dir():
        raise SystemExit("Replay root does not contain the requested batch input.")

    from webapp.backend import settings

    settings.WEBAPP_DATA_ROOT = runtime_root
    settings.BATCHES_ROOT = runtime_root / "batches"
    # A downstream replay is not permitted to obtain new semantic facts. Cache
    # misses remain manual-review outcomes and never become provider requests.
    os.environ["AI_SEMANTIC_REASONING_ENABLED"] = "0"

    attempted_requests: list[str] = []
    original_urlopen = urllib.request.urlopen

    def blocked_urlopen(request: Any, *_args: Any, **_kwargs: Any):
        attempted_requests.append(type(request).__name__)
        raise ExternalProviderCallBlocked("external_provider_calls_disabled_for_replay")

    urllib.request.urlopen = blocked_urlopen
    try:
        from webapp.backend.api import processing
        from webapp.backend.services import (
            accounting_readiness,
            approved_invoice_corrections,
            batch_processor,
            row_normalizer,
        )

        started = time.perf_counter()
        result = batch_processor.process_batch(args.batch_id, dry_run=True)
        row_normalizer.normalize_result(result)
        processing._apply_learned_corrections_to_result(result)
        approved_invoice_corrections.apply_to_result(result, batch_id=args.batch_id)
        elapsed = time.perf_counter() - started
    finally:
        urllib.request.urlopen = original_urlopen

    args.result.parent.mkdir(parents=True, exist_ok=True)
    args.result.write_text(json.dumps(result, default=str, indent=2), encoding="utf-8")
    invoices = list(result.get("all_invoices") or [])
    replay_readiness = {
        str(item.get("invoice_number") or ""): accounting_readiness.as_dict(
            accounting_readiness.evaluate_rows(item.get("rows") or [])
        )
        for item in invoices
    }
    golden_readiness: dict[str, dict[str, Any]] = {}
    if args.golden:
        golden_payload = json.loads(args.golden.read_text(encoding="utf-8"))
        golden_readiness = {
            str(item.get("invoice_number") or ""): accounting_readiness.as_dict(
                accounting_readiness.evaluate_rows(item.get("rows") or [])
            )
            for item in golden_payload.get("all_invoices") or []
        }
    false_safe = [
        invoice_id for invoice_id, golden_status in golden_readiness.items()
        if not golden_status.get("export_allowed")
        and replay_readiness.get(invoice_id, {}).get("export_allowed")
    ]
    metrics = {
        "batch_id": args.batch_id,
        "runtime_root": str(runtime_root),
        "elapsed_seconds": round(elapsed, 6),
        "invoice_count": len(invoices),
        "row_count": sum(len(item.get("rows") or []) for item in invoices),
        "external_provider_network_attempts": len(attempted_requests),
        "provider_calls_executed": 0,
        "readiness": {
            invoice_id: {
                "status": value.get("status"),
                "export_allowed": value.get("export_allowed"),
                "blocker_count": len(value.get("blockers") or []),
            }
            for invoice_id, value in replay_readiness.items()
        },
        "false_safe_export_count": len(false_safe),
        "false_safe_export_invoices": false_safe,
        "identical_readiness_safety": bool(golden_readiness) and all(
            replay_readiness.get(invoice_id, {}).get("status") == value.get("status")
            and replay_readiness.get(invoice_id, {}).get("export_allowed") == value.get("export_allowed")
            for invoice_id, value in golden_readiness.items()
        ),
    }
    args.metrics.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    print(json.dumps(metrics, indent=2))
    if attempted_requests:
        raise SystemExit("Offline replay attempted an external provider request.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
