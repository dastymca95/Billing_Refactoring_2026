"""
Lightweight per-batch progress tracker.

Vendor processors take an optional `progress_callback` argument. When the
webapp's batch processor wires a tracker in, every callback writes a small
JSON snapshot to `webapp_data/batches/<batch_id>/progress.json`. The
frontend polls `GET /api/batches/{batch_id}/progress` every ~750ms while
processing.

Design goals:
  * **Non-blocking for the CLI.** When `progress_callback` is None (the
    CLI default), processors do nothing. Adding progress doesn't slow the
    CLI down.
  * **Single small file.** ~1 KB JSON, atomic write via tempfile rename.
  * **Crash-safe.** If a processor dies mid-batch, the last snapshot stays
    on disk and the frontend can read "status: failed" with the last step.
  * **Backward compatible.** All fields are optional; the frontend reads
    only what it knows about.

Public API:
    ProgressUpdate(percent, current_step, current_file, ...)   — typed event
    ProgressTracker(progress_path).update(**fields)            — atomic write
    make_callback(tracker)                                     — closure for processors

Snapshot shape:
    {
      "batch_id": "...",
      "status": "idle"|"uploading"|"processing"|"completed"|"failed",
      "percent": 0..100,
      "current_step": "OCR page 7 of 14",
      "current_file": "HWEA - Aspen.pdf",
      "files_total": 14,
      "files_done": 3,
      "pages_total": 28,
      "pages_done": 17,
      "invoices_created": 9,
      "rows_created": 11,
      "warnings_count": 2,
      "error_message": "",
      "started_at": "2026-05-02T11:33:01",
      "updated_at": "2026-05-02T11:33:09"
    }
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional


_LOG = logging.getLogger("utils.progress_tracker")


@dataclass
class ProgressStage:
    """One stage in the processing timeline. Optional — vendor processors
    that never call `tracker.start_stage()` produce snapshots without a
    `stages` list, and the frontend tolerates the missing key."""
    key: str
    label: str
    status: str = "pending"   # pending | running | completed | warning | failed | skipped
    detail: str = ""
    percent: float = 0.0
    started_at: str = ""
    completed_at: str = ""
    warnings_count: int = 0


@dataclass
class ProgressSnapshot:
    """In-memory progress state. Vendor processors mutate this via
    `ProgressTracker.update(...)` and the tracker persists it to JSON."""
    batch_id: str = ""
    status: str = "idle"
    percent: float = 0.0
    current_step: str = ""
    current_file: str = ""
    files_total: int = 0
    files_done: int = 0
    pages_total: int = 0
    pages_done: int = 0
    invoices_created: int = 0
    rows_created: int = 0
    warnings_count: int = 0
    error_message: str = ""
    started_at: str = ""
    updated_at: str = ""
    # Phase 1H — processing timeline stages. Optional list; when empty
    # the frontend hides the timeline panel and shows only the bar.
    stages: list[ProgressStage] = field(default_factory=list)
    # Phase 1N — cooperative cancellation. The web app's cancel
    # endpoint sets `cancel_requested=True`; vendor processors that
    # accept `should_cancel_callback` poll this between files / pages
    # and stop gracefully.
    cancel_requested: bool = False
    cancelled_at: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Promote any free-form `extra` keys to the top level so the
        # frontend can read vendor-specific fields without a nested lookup.
        extra = d.pop("extra", {}) or {}
        d.update(extra)
        return d


class ProgressTracker:
    """Persists `ProgressSnapshot` to disk on every update.

    `progress_path` should be the per-batch JSON file
    (e.g. `webapp_data/batches/<id>/progress.json`). Writes are atomic via
    `tempfile + os.replace` so a reader can never see a half-written file.
    """

    def __init__(self, progress_path: Path, batch_id: str = ""):
        self.path = Path(progress_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.snapshot = ProgressSnapshot(
            batch_id=batch_id,
            status="idle",
            started_at=datetime.now().isoformat(timespec="seconds"),
            updated_at=datetime.now().isoformat(timespec="seconds"),
        )
        # Phase 1W bugfix — serialize concurrent flushes so the
        # worker thread, the cancel endpoint, and any other writer
        # don't race on `os.replace`. Without this Windows raises
        # `[WinError 5] Access is denied` because two threads can
        # overlap a temp-file → real-file rename.
        self._flush_lock = threading.Lock()
        self._flush()

    def update(self, **fields: Any) -> None:
        """Apply field overrides and persist. Unknown fields go into
        `snapshot.extra` and are surfaced flattened in the JSON output."""
        known = {f for f in self.snapshot.__dataclass_fields__.keys()}
        for k, v in fields.items():
            if k in known:
                setattr(self.snapshot, k, v)
            else:
                self.snapshot.extra[k] = v
        self.snapshot.updated_at = datetime.now().isoformat(timespec="seconds")
        # Auto-clamp percent so a typo doesn't ship 1500%.
        if self.snapshot.percent < 0:
            self.snapshot.percent = 0.0
        if self.snapshot.percent > 100:
            self.snapshot.percent = 100.0
        self._flush()

    # ---- Phase 1N — cooperative cancellation -------------------------
    def request_cancel(self) -> None:
        """Mark the run as cancellation-requested. Vendor processors
        polling `is_cancel_requested()` will stop at the next safe
        checkpoint; the wrapper marks the snapshot `status="cancelled"`
        once they return."""
        if self.snapshot.cancel_requested:
            return
        self.snapshot.cancel_requested = True
        # Don't immediately switch status to "cancelled" — keep
        # "cancelling" visible until the worker finishes its current
        # checkpoint and returns. The wrapper will call `cancelled()`
        # once the run actually stops.
        if self.snapshot.status not in {"completed", "failed", "cancelled"}:
            self.snapshot.status = "cancelling"
            self.snapshot.current_step = "Cancelling processing…"
        self._flush()

    def is_cancel_requested(self) -> bool:
        return self.snapshot.cancel_requested

    def cancelled(self, **summary_fields: Any) -> None:
        """Finalise the run as cancelled. Called by the worker wrapper
        after vendor processors return having stopped early."""
        # Auto-close any running stages so the timeline isn't stuck.
        for stage in self.snapshot.stages:
            if stage.status == "running":
                stage.status = "skipped"
                stage.completed_at = datetime.now().isoformat(timespec="seconds")
                stage.detail = "Cancelled"
        self.snapshot.cancelled_at = datetime.now().isoformat(timespec="seconds")
        self.update(
            status="cancelled",
            percent=100.0,
            current_step="Processing cancelled",
            **summary_fields,
        )

    def fail(self, message: str) -> None:
        # If a stage is currently running, mark it failed so the
        # timeline reflects where the run stopped.
        for stage in self.snapshot.stages:
            if stage.status == "running":
                stage.status = "failed"
                stage.completed_at = datetime.now().isoformat(timespec="seconds")
                stage.detail = message[:200]
        self.update(status="failed", error_message=message, percent=100.0,
                    current_step=f"Failed: {message[:120]}")

    def complete(self, **summary_fields: Any) -> None:
        # Auto-close any running stages.
        for stage in self.snapshot.stages:
            if stage.status == "running":
                stage.status = "completed"
                stage.completed_at = datetime.now().isoformat(timespec="seconds")
        self.update(status="completed", percent=100.0,
                    current_step="Done", **summary_fields)

    # ---- Phase 1H — processing timeline stages -----------------------
    def declare_stages(self, stages: list[tuple[str, str]]) -> None:
        """Declare the full stage list up-front so the UI can render
        them as `pending` placeholders before any actual work fires.
        `stages` is a list of `(key, label)` tuples."""
        self.snapshot.stages = [
            ProgressStage(key=k, label=l) for k, l in stages
        ]
        self._flush()

    def start_stage(self, key: str, *, detail: str = "", label: str = "") -> None:
        """Mark a declared stage as running, or append a new stage if
        the key wasn't declared. Sets `started_at` once."""
        stage = self._find_or_create_stage(key, label)
        if not stage.started_at:
            stage.started_at = datetime.now().isoformat(timespec="seconds")
        stage.status = "running"
        if detail:
            stage.detail = detail
        self.snapshot.current_step = label or stage.label
        self._flush()

    def update_stage(
        self,
        key: str,
        *,
        detail: Optional[str] = None,
        percent: Optional[float] = None,
        warnings_count: Optional[int] = None,
    ) -> None:
        """Refine the running stage's detail line / percent / warning
        count without changing its status."""
        stage = self._find_or_create_stage(key, "")
        if detail is not None:
            stage.detail = detail
            self.snapshot.current_step = f"{stage.label}: {detail}" if detail else stage.label
        if percent is not None:
            stage.percent = max(0.0, min(100.0, float(percent)))
        if warnings_count is not None:
            stage.warnings_count = int(warnings_count)
        self._flush()

    def complete_stage(self, key: str, *, detail: str = "") -> None:
        stage = self._find_or_create_stage(key, "")
        stage.status = "completed"
        stage.completed_at = datetime.now().isoformat(timespec="seconds")
        if detail:
            stage.detail = detail
        stage.percent = 100.0
        self._flush()

    def warn_stage(self, key: str, *, detail: str = "") -> None:
        stage = self._find_or_create_stage(key, "")
        stage.status = "warning"
        if detail:
            stage.detail = detail
        stage.warnings_count += 1
        self._flush()

    def skip_stage(self, key: str, *, detail: str = "") -> None:
        stage = self._find_or_create_stage(key, "")
        stage.status = "skipped"
        if detail:
            stage.detail = detail
        self._flush()

    def fail_stage(self, key: str, *, detail: str = "") -> None:
        stage = self._find_or_create_stage(key, "")
        stage.status = "failed"
        stage.completed_at = datetime.now().isoformat(timespec="seconds")
        if detail:
            stage.detail = detail
        self._flush()

    def _find_or_create_stage(self, key: str, label: str) -> ProgressStage:
        for stage in self.snapshot.stages:
            if stage.key == key:
                return stage
        new = ProgressStage(key=key, label=label or key.replace("_", " ").title())
        self.snapshot.stages.append(new)
        return new

    def _flush(self) -> None:
        """Atomic-ish persist. Wraps temp-file → real-file rename in
        a per-tracker lock + Windows-friendly retry loop.

        Why the retry: on Windows, `os.replace` raises
        `PermissionError [WinError 5] Access is denied` when another
        process briefly holds the destination file open. The most
        common offender is Windows Defender / Search Indexer / OneDrive
        scanning the freshly-written progress.json. We retry a handful
        of times with short backoff before giving up — and even then
        we *swallow* the error rather than crash the worker, because
        a missed progress write is non-fatal: the next update will
        succeed and the operator never notices.
        """
        # Build the JSON payload outside the lock so we don't pin
        # other writers waiting on what is otherwise an atomic step.
        data = self.snapshot.to_dict()
        tmp_dir = str(self.path.parent)
        tmp_name: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", dir=tmp_dir, prefix=".progress_", suffix=".tmp",
                encoding="utf-8", delete=False,
            ) as tmp:
                json.dump(data, tmp, indent=2)
                tmp_name = tmp.name
        except OSError as e:
            _LOG.warning("progress_tracker: temp write failed (%s); skipping flush", e)
            return

        # Serialize the rename + retry on Windows lock contention.
        with self._flush_lock:
            for attempt in range(6):
                try:
                    os.replace(tmp_name, self.path)
                    return
                except PermissionError as e:
                    # WinError 5 — destination briefly locked by AV /
                    # search indexer / sync. Backoff and try again.
                    if attempt == 5:
                        _LOG.warning(
                            "progress_tracker: os.replace failed after 6 retries (%s); "
                            "dropping this update", e,
                        )
                        try:
                            if tmp_name and os.path.exists(tmp_name):
                                os.unlink(tmp_name)
                        except OSError:
                            pass
                        return
                    time.sleep(0.05 * (attempt + 1))  # 50, 100, 150, 200, 250 ms
                except OSError as e:
                    _LOG.warning(
                        "progress_tracker: os.replace failed (%s); dropping this update", e,
                    )
                    try:
                        if tmp_name and os.path.exists(tmp_name):
                            os.unlink(tmp_name)
                    except OSError:
                        pass
                    return


def make_callback(tracker: Optional[ProgressTracker]) -> Optional[Callable[..., None]]:
    """Return a callable that vendor processors can pass to internal helpers.
    Returns None when `tracker` is None so processors can write
    `if progress_callback is None: continue` cheaply."""
    if tracker is None:
        return None

    def cb(**fields: Any) -> None:
        try:
            tracker.update(**fields)
        except Exception:
            # Progress is non-critical; never let a tracker bug kill processing.
            pass

    return cb


def load_snapshot(progress_path: Path) -> Optional[dict[str, Any]]:
    """Read the on-disk snapshot. Returns None if the file is missing or
    can't be parsed (e.g. mid-write — caller can poll again)."""
    p = Path(progress_path)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
