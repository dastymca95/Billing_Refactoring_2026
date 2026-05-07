"""Phase 2J — Extraction Trace registry.

Captures provenance metadata for each value the vendor processors
extract from a bill/PDF: which page, the bbox in normalized 0-1
coordinates, the matched text, the rule strategy, the confidence, and
which template rows/columns the value feeds. The frontend consumes
this through `GET /api/batches/<id>/documents/<file>/trace` to paint
overlay boxes on the document viewer.

Design notes
------------
* The registry is process-wide and *batch-scoped*. The webapp calls
  `start_batch(batch_id)` before a run and `flush_batch(...)` at the
  end; vendor processors call `record(...)` from inside their
  per-page extraction code with no awareness of the batch lifecycle.
* `record()` is idempotent and tolerant — failures must never poison
  a real extraction run, so anything unexpected is swallowed.
* Bboxes are normalized at capture time (`bbox / (page_width,
  page_height)`) so the frontend can multiply by its rendered page
  dimensions without caring whether the source was digital or OCR.
* `trace_id` is a short ulid-ish string the vendor processor stuffs
  into the corresponding row's `_meta.trace_ids` so the UI can map
  rows ↔ regions in both directions.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable, Optional


_LOG = logging.getLogger(__name__)


@dataclass
class TraceItem:
    """One extracted region linked to one or more template rows."""

    trace_id: str
    source_file: str
    page: int
    # Normalized 0..1 of page width/height, top-left origin.
    bbox: dict[str, float]
    field_key: str
    field_label: str
    source_type: str = "digital_text"   # digital_text | ocr | derived
    rule_id: str = ""                   # named pattern / strategy
    match_strategy: str = ""            # regex | layout_label | layout_table
    confidence: float = 1.0             # 0..1
    feeds_rows: list[str] = field(default_factory=list)
    feeds_columns: list[str] = field(default_factory=list)
    detected_text: str = ""


# ---------------------------------------------------------------------------
# Process-wide registry (batch-scoped)
# ---------------------------------------------------------------------------

_LOCK = threading.RLock()
# Maps batch_id -> {source_file: [TraceItem, ...]}
_TRACES: dict[str, dict[str, list[TraceItem]]] = {}
_ACTIVE_BATCH: dict[int, str] = {}      # thread_id -> batch_id


def start_batch(batch_id: str) -> None:
    """Register `batch_id` as the active batch for the current thread.

    Vendor processors run in the same thread as the webapp request
    handler (or background worker), so per-thread tracking is enough
    to keep parallel runs from stomping on each other."""
    with _LOCK:
        _ACTIVE_BATCH[threading.get_ident()] = batch_id
        _TRACES.setdefault(batch_id, {})


def end_batch(batch_id: str) -> None:
    """Clear thread-local pointer to `batch_id`. Stored items remain
    available for `flush_batch`."""
    with _LOCK:
        tid = threading.get_ident()
        if _ACTIVE_BATCH.get(tid) == batch_id:
            _ACTIVE_BATCH.pop(tid, None)


def _current_batch_id() -> Optional[str]:
    return _ACTIVE_BATCH.get(threading.get_ident())


def _next_trace_id() -> str:
    """A short, opaque, URL-safe id."""
    return "tr_" + uuid.uuid4().hex[:10]


def record(
    *,
    source_file: str,
    page: int,
    field_key: str,
    field_label: str,
    bbox: Optional[dict[str, float]] = None,
    pixel_bbox: Optional[dict[str, float]] = None,
    page_width: float = 0,
    page_height: float = 0,
    detected_text: str = "",
    rule_id: str = "",
    match_strategy: str = "",
    confidence: float = 1.0,
    source_type: str = "digital_text",
    feeds_rows: Optional[Iterable[str]] = None,
    feeds_columns: Optional[Iterable[str]] = None,
) -> Optional[str]:
    """Record an extraction trace. Returns the new ``trace_id`` (or
    None if no batch is active or no usable bbox was provided).

    Pass either ``bbox`` (already normalized 0..1) OR ``pixel_bbox``
    + ``page_width``/``page_height`` (we normalize for you).
    """
    try:
        batch_id = _current_batch_id()
        if not batch_id:
            return None
        norm = _normalize_bbox(
            bbox, pixel_bbox, page_width=page_width, page_height=page_height
        )
        if norm is None:
            return None
        item = TraceItem(
            trace_id=_next_trace_id(),
            source_file=str(source_file or ""),
            page=int(page or 1),
            bbox=norm,
            field_key=str(field_key or ""),
            field_label=str(field_label or ""),
            source_type=str(source_type or "digital_text"),
            rule_id=str(rule_id or ""),
            match_strategy=str(match_strategy or ""),
            confidence=float(_clamp01(confidence)),
            detected_text=str(detected_text or ""),
            feeds_rows=list(feeds_rows or []),
            feeds_columns=list(feeds_columns or []),
        )
        with _LOCK:
            store = _TRACES.setdefault(batch_id, {}).setdefault(
                item.source_file, []
            )
            store.append(item)
        return item.trace_id
    except Exception:  # pragma: no cover — never poison the real run
        _LOG.exception("extraction_trace.record failed")
        return None


def attach_rows(trace_id: str, *, rows: Iterable[str]) -> None:
    """Attach (or extend) the ``feeds_rows`` list on a recorded trace.

    Vendor processors capture the trace at extraction time but only
    learn the row keys later, in the row builder. This decouples the
    two without forcing them to share a return type."""
    if not trace_id:
        return
    rows_list = [r for r in rows if r]
    if not rows_list:
        return
    try:
        with _LOCK:
            for store in _TRACES.values():
                for items in store.values():
                    for it in items:
                        if it.trace_id == trace_id:
                            for r in rows_list:
                                if r not in it.feeds_rows:
                                    it.feeds_rows.append(r)
                            return
    except Exception:  # pragma: no cover
        _LOG.exception("extraction_trace.attach_rows failed")


def items_for(batch_id: str, source_file: str) -> list[TraceItem]:
    """Return the recorded traces for one document. Empty if none."""
    with _LOCK:
        return list((_TRACES.get(batch_id) or {}).get(source_file) or [])


def all_items(batch_id: str) -> dict[str, list[TraceItem]]:
    """Return all traces for a batch keyed by source filename."""
    with _LOCK:
        return {
            k: list(v) for k, v in (_TRACES.get(batch_id) or {}).items()
        }


def clear_batch(batch_id: str) -> None:
    """Drop in-memory state for one batch. Persisted files survive."""
    with _LOCK:
        _TRACES.pop(batch_id, None)


def flush_batch(batch_id: str, trace_dir: Path) -> int:
    """Persist all in-memory traces for ``batch_id`` to disk.

    One JSON file per source document under ``trace_dir/``. Returns the
    number of files written. The webapp calls this right after the
    vendor processor finishes so the trace API can serve the data
    even after a backend restart."""
    written = 0
    try:
        trace_dir.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            store = _TRACES.get(batch_id) or {}
            for source_file, items in store.items():
                if not items:
                    continue
                fname = _safe_filename(source_file) + ".json"
                payload = {
                    "source_file": source_file,
                    "trace_count": len(items),
                    "items": [asdict(i) for i in items],
                }
                (trace_dir / fname).write_text(
                    json.dumps(payload, indent=2, default=str),
                    encoding="utf-8",
                )
                written += 1
    except Exception:  # pragma: no cover
        _LOG.exception("extraction_trace.flush_batch failed")
    return written


# ---------------------------------------------------------------------------
# Lookup helper — find a substring's bbox inside a page's word list.
# ---------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")


def find_text_bbox(
    needle: str,
    words: list[dict[str, Any]],
) -> Optional[dict[str, float]]:
    """Locate ``needle`` inside the word list and return the union
    pixel-bbox (left/top/width/height) of the matching contiguous
    word run, or None if nothing close matches.

    Strategy: greedy contiguous match on normalized lowercase tokens
    from the page's words. Handles word splits / punctuation but not
    cross-line wraps (which are rare for the fields we trace).
    """
    if not needle or not words:
        return None
    target = _norm(needle)
    if not target:
        return None
    target_tokens = target.split()
    if not target_tokens:
        return None
    norm_words = [(_norm(w.get("text") or ""), w) for w in words]
    norm_words = [(t, w) for (t, w) in norm_words if t]
    n = len(norm_words)
    m = len(target_tokens)

    # Pass 1: exact contiguous run.
    for i in range(n - m + 1):
        ok = True
        for j in range(m):
            if norm_words[i + j][0] != target_tokens[j]:
                ok = False
                break
        if ok:
            return _union_bbox(w for (_, w) in norm_words[i : i + m])

    # Pass 2: rolling-window prefix match (handles target tokens that
    # were split across multiple word objects, e.g. "$" and "12.34").
    joined = " ".join(t for (t, _) in norm_words)
    pos = joined.find(target)
    if pos < 0:
        return None
    # Walk the words to find which slice corresponds to [pos, pos+len(target)).
    end = pos + len(target)
    cursor = 0
    start_i: Optional[int] = None
    end_i: Optional[int] = None
    for idx, (tok, _) in enumerate(norm_words):
        tok_start = cursor
        tok_end = cursor + len(tok)
        if start_i is None and tok_end > pos:
            start_i = idx
        if end_i is None and tok_end >= end:
            end_i = idx
            break
        cursor = tok_end + 1  # account for the joining space
    if start_i is None or end_i is None:
        return None
    return _union_bbox(w for (_, w) in norm_words[start_i : end_i + 1])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _normalize_bbox(
    bbox: Optional[dict[str, float]],
    pixel_bbox: Optional[dict[str, float]],
    *,
    page_width: float,
    page_height: float,
) -> Optional[dict[str, float]]:
    if bbox:
        return {
            "x": _clamp01(bbox.get("x", 0)),
            "y": _clamp01(bbox.get("y", 0)),
            "w": _clamp01(bbox.get("w", 0)),
            "h": _clamp01(bbox.get("h", 0)),
        }
    if not pixel_bbox or page_width <= 0 or page_height <= 0:
        return None
    try:
        x = float(pixel_bbox.get("left", 0)) / page_width
        y = float(pixel_bbox.get("top", 0)) / page_height
        w = float(pixel_bbox.get("width", 0)) / page_width
        h = float(pixel_bbox.get("height", 0)) / page_height
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    return {
        "x": _clamp01(x),
        "y": _clamp01(y),
        "w": _clamp01(w),
        "h": _clamp01(h),
    }


def _union_bbox(words: Iterable[dict[str, Any]]) -> Optional[dict[str, float]]:
    xs0, ys0, xs1, ys1 = [], [], [], []
    for w in words:
        try:
            x0 = float(w.get("left", 0))
            y0 = float(w.get("top", 0))
            x1 = x0 + float(w.get("width", 0))
            y1 = y0 + float(w.get("height", 0))
        except (TypeError, ValueError):
            continue
        xs0.append(x0); ys0.append(y0); xs1.append(x1); ys1.append(y1)
    if not xs0:
        return None
    return {
        "left": min(xs0),
        "top": min(ys0),
        "width": max(xs1) - min(xs0),
        "height": max(ys1) - min(ys0),
    }


def _norm(s: str) -> str:
    return _WS_RE.sub(" ", (s or "").lower().strip())


def _clamp01(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    if f < 0:
        return 0.0
    if f > 1:
        return 1.0
    return f


def _safe_filename(name: str) -> str:
    """Make a source filename safe to use as a flat filesystem name."""
    s = (name or "unknown").strip().replace("\\", "/").split("/")[-1]
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)[:200] or "unknown"
