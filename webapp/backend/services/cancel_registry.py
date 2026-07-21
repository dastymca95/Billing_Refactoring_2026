"""Phase 1N — process cancellation registry.

A tiny in-memory map of {batch_id: ProgressTracker} that lets the cancel
endpoint flag a running tracker without poking through the filesystem.
The vendor processors don't import from here directly — they get a
plain `should_cancel_callback` callable through `run_context`. The
batch processor wraps this registry into that callable.

Keeping the registry in its own module avoids a circular import between
`services.batch_processor` and `api.processing`.
"""

from __future__ import annotations

import threading
from typing import Optional, Protocol


class ProgressTracker(Protocol):
    """Minimal cancellation surface used by this registry."""

    def request_cancel(self) -> None: ...

    def is_cancel_requested(self) -> bool: ...


_LOCK = threading.Lock()
_TRACKERS: dict[str, ProgressTracker] = {}


def register(batch_id: str, tracker: ProgressTracker) -> None:
    """Register the tracker for a running batch. Safe to call multiple
    times — later calls overwrite (e.g. if a previous run wasn't cleaned
    up due to a crash)."""
    with _LOCK:
        _TRACKERS[batch_id] = tracker


def unregister(batch_id: str) -> None:
    with _LOCK:
        _TRACKERS.pop(batch_id, None)


def get_tracker(batch_id: str) -> Optional[ProgressTracker]:
    with _LOCK:
        return _TRACKERS.get(batch_id)


def request_cancel(batch_id: str) -> bool:
    """Tell the tracker to stop. Returns True if a tracker was found
    (caller can convert that to a 200 response); False if no run is
    active for that batch (caller may return 404 / 409)."""
    tracker = get_tracker(batch_id)
    if tracker is None:
        return False
    try:
        tracker.request_cancel()
    except Exception:
        # The tracker is best-effort; even if persistence fails we
        # still return True because the in-memory flag is set.
        pass
    return True


def is_cancel_requested(batch_id: str) -> bool:
    tracker = get_tracker(batch_id)
    if tracker is None:
        return False
    try:
        return bool(tracker.is_cancel_requested())
    except Exception:
        return False
