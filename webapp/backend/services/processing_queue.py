"""Phase 2D — Cross-batch processing queue.

A single global worker thread runs at most one batch at a time. New
process requests join a FIFO queue and start automatically when the
running batch finishes. The previous behaviour (per-batch single-flight
via `_RUNNING_LOCK` in `processing.py`) becomes a special case of this:
re-submitting the *same* batch while it's running or queued is a no-op
that returns the existing position.

Public API:

    submit(batch_id, runner)            ->  {"status": "running"|"queued", "position": int}
    cancel(batch_id)                    ->  bool   (queued: removed; running: cancel-requested)
    status()                            ->  {"running": str|None, "queued": [str]}
    is_running(batch_id)                ->  bool
    is_queued(batch_id)                 ->  bool

`runner(batch_id)` is the actual worker function that performs the
processing. The queue calls it on its single worker thread; the runner
is responsible for writing results, progress, and cleaning up.

Cancellation is delegated to `cancel_registry.request_cancel(batch_id)`
for the running item; queued items are simply removed from the FIFO
without ever entering the runner.

Thread-safety: all state mutations go through `_LOCK`. The condition
variable `_COND` wakes the worker when the queue becomes non-empty.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Callable, Deque, Iterable, Literal

from . import cancel_registry


_LOG = logging.getLogger("processing_queue")


# ----------------------------------------------------------------------------
# Internal state
# ----------------------------------------------------------------------------
_LOCK = threading.RLock()
_COND = threading.Condition(_LOCK)

_QUEUE: Deque[str] = deque()
_RUNNING: str | None = None
_RUNNERS: dict[str, Callable[[str], None]] = {}

# A persistent worker thread, lazily started on first submit.
_WORKER: threading.Thread | None = None


def _worker_loop() -> None:
    global _RUNNING
    while True:
        with _COND:
            while not _QUEUE:
                _COND.wait()
            batch_id = _QUEUE.popleft()
            runner = _RUNNERS.pop(batch_id, None)
            _RUNNING = batch_id
        # Run outside the lock so /status calls aren't blocked while
        # processing (which can take minutes).
        try:
            if runner is not None:
                runner(batch_id)
        except Exception as e:  # pragma: no cover - belt-and-braces
            _LOG.exception("Queue runner raised for batch %s: %s", batch_id, e)
        finally:
            with _COND:
                if _RUNNING == batch_id:
                    _RUNNING = None
                # Notify in case anyone is waiting on a status change.
                _COND.notify_all()


def _ensure_worker() -> None:
    """Lazily spin up the persistent worker thread."""
    global _WORKER
    if _WORKER is not None and _WORKER.is_alive():
        return
    t = threading.Thread(
        target=_worker_loop,
        name="processing-queue-worker",
        daemon=True,
    )
    t.start()
    _WORKER = t


# ----------------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------------


def submit(batch_id: str, runner: Callable[[str], None]) -> dict[str, object]:
    """Enqueue (or fast-start) a batch for processing.

    Idempotent: if `batch_id` is already running or queued, returns the
    existing position without scheduling a duplicate.
    """
    if not batch_id or not isinstance(batch_id, str):
        raise ValueError("batch_id must be a non-empty string")

    with _COND:
        _ensure_worker()
        # Already running.
        if _RUNNING == batch_id:
            return {
                "status": "running",
                "position": 0,
                "running": _RUNNING,
                "queued": list(_QUEUE),
            }
        # Already queued.
        if batch_id in _QUEUE:
            position = list(_QUEUE).index(batch_id) + 1
            return {
                "status": "queued",
                "position": position,
                "running": _RUNNING,
                "queued": list(_QUEUE),
            }
        # New submission.
        _QUEUE.append(batch_id)
        _RUNNERS[batch_id] = runner
        _COND.notify_all()

        if _RUNNING is None and len(_QUEUE) == 1:
            # Worker will pick it up immediately. Report as "queued"
            # with position 1 so the API contract is consistent — the
            # status endpoint will show it as running once the worker
            # picks it up.
            position = 1
        else:
            position = list(_QUEUE).index(batch_id) + 1
        return {
            "status": "queued",
            "position": position,
            "running": _RUNNING,
            "queued": list(_QUEUE),
        }


def cancel(batch_id: str) -> dict[str, object]:
    """Remove a queued batch or request cancellation of a running one."""
    with _COND:
        if _RUNNING == batch_id:
            cancel_registry.request_cancel(batch_id)
            return {
                "batch_id": batch_id,
                "result": "cancel_requested",
            }
        if batch_id in _QUEUE:
            try:
                _QUEUE.remove(batch_id)
            except ValueError:
                pass
            _RUNNERS.pop(batch_id, None)
            return {
                "batch_id": batch_id,
                "result": "removed_from_queue",
            }
    return {
        "batch_id": batch_id,
        "result": "not_running_or_queued",
    }


def status() -> dict[str, object]:
    with _LOCK:
        return {
            "running": _RUNNING,
            "queued": list(_QUEUE),
        }


def state_for(batch_id: str) -> dict[str, object]:
    """Compact per-batch state for the BatchExplorer UI."""
    with _LOCK:
        if _RUNNING == batch_id:
            return {"state": "running", "position": 0}
        if batch_id in _QUEUE:
            return {
                "state": "queued",
                "position": list(_QUEUE).index(batch_id) + 1,
            }
        return {"state": "idle", "position": None}


def is_running(batch_id: str) -> bool:
    with _LOCK:
        return _RUNNING == batch_id


def is_queued(batch_id: str) -> bool:
    with _LOCK:
        return batch_id in _QUEUE


def all_states() -> dict[str, dict[str, object]]:
    """Snapshot of every known live state. Useful for batch-list APIs."""
    with _LOCK:
        out: dict[str, dict[str, object]] = {}
        if _RUNNING:
            out[_RUNNING] = {"state": "running", "position": 0}
        for i, b in enumerate(_QUEUE, start=1):
            out[b] = {"state": "queued", "position": i}
        return out


def reset_for_tests() -> None:
    """Test-only: clear all state. Never call from production code."""
    global _RUNNING, _WORKER
    with _COND:
        _QUEUE.clear()
        _RUNNERS.clear()
        _RUNNING = None
        _COND.notify_all()


__all__ = [
    "submit",
    "cancel",
    "status",
    "state_for",
    "is_running",
    "is_queued",
    "all_states",
    "reset_for_tests",
]
