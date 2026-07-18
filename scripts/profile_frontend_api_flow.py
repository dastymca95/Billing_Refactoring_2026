"""Phase PERF-2 - profile common frontend API flow.

This script times the requests the React app performs while switching
into a batch. It does not process, export, call Dropbox, or call AI.

Environment:
  BILLING_API_BASE=http://localhost:8001
  BILLING_PROFILE_BATCH_ID=<optional batch id>

Output:
  docs/reports/phases/screenshots/phase_perf2/profile_frontend_api_flow.json
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "docs" / "reports" / "phases" / "screenshots" / "phase_perf2"
BASE = os.environ.get("BILLING_API_BASE", "http://localhost:8001").rstrip("/")

os.environ.setdefault("DROPBOX_DISABLE_FOR_TESTS", "1")
os.environ.setdefault("AI_FALLBACK_DISABLED", "1")


def _get_json(path: str, timeout: float = 20.0) -> Any:
    req = urllib.request.Request(
        f"{BASE}{path}",
        headers={"Accept": "application/json"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as res:
        raw = res.read().decode("utf-8")
        return json.loads(raw) if raw else None


def _timed(label: str, path: str) -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        payload = _get_json(path)
        ms = (time.perf_counter() - t0) * 1000.0
        size_hint = 0
        try:
            size_hint = len(json.dumps(payload))
        except Exception:
            pass
        return {
            "label": label,
            "path": path,
            "ok": True,
            "ms": round(ms, 2),
            "json_chars": size_hint,
        }
    except Exception as exc:
        return {
            "label": label,
            "path": path,
            "ok": False,
            "ms": round((time.perf_counter() - t0) * 1000.0, 2),
            "error": f"{type(exc).__name__}: {exc}",
        }


def _choose_batch(batches: list[dict[str, Any]]) -> str | None:
    requested = os.environ.get("BILLING_PROFILE_BATCH_ID", "").strip()
    if requested:
        return requested
    for batch in batches:
        if int(batch.get("files_count") or 0) > 0:
            return str(batch.get("batch_id") or "")
    return str(batches[0].get("batch_id") or "") if batches else None


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / "profile_frontend_api_flow.json"
    report: dict[str, Any] = {
        "schema": "phase_perf2/frontend_api_flow/v1",
        "run_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "base_url": BASE,
        "requests": [],
        "parallel_requests": [],
    }

    try:
        batches_payload = _get_json("/api/batches", timeout=10.0)
    except urllib.error.URLError as exc:
        report["error"] = f"backend_unavailable: {exc}"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        print(json.dumps(report, indent=2), flush=True)
        return 0

    batches = list((batches_payload or {}).get("batches") or [])
    batch_id = _choose_batch(batches)
    report["batch_count"] = len(batches)
    report["batch_id"] = batch_id
    report["requests"].append(_timed("list_batches", "/api/batches"))
    if not batch_id:
        report["error"] = "no_batches_available"
    else:
        status_step = _timed("get_batch", f"/api/batches/{batch_id}")
        report["requests"].append(status_step)
        parallel = [
            ("preview", f"/api/batches/{batch_id}/preview"),
            ("manual_review", f"/api/batches/{batch_id}/manual-review"),
            ("revisions", f"/api/batches/{batch_id}/revisions"),
            ("performance", f"/api/batches/{batch_id}/performance"),
        ]
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(_timed, label, path) for label, path in parallel]
            for fut in as_completed(futures):
                report["parallel_requests"].append(fut.result())
        report["parallel_total_ms"] = round((time.perf_counter() - t0) * 1000.0, 2)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(json.dumps(report, indent=2), flush=True)
    print(f"\nWrote summary -> {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
