"""Phase 2D — Template revision history per batch.

Every successful Process run produces a *revision*: a frozen snapshot of
the result cache (`processed/_webapp_result.json`) plus a small
manifest entry. Revisions live next to the cache:

    webapp_data/batches/<batch_id>/revisions/
        <rev_id>.json                  # frozen copy of the cache
        index.json                     # ordered manifest, newest first

The webapp's `_webapp_result.json` is the *active* revision pointer; we
keep it in lock-step with whatever revision is currently selected.
Switching revisions copies the snapshot back over the active cache so
the existing `/preview` and `/manual-review` endpoints (which read from
`_webapp_result.json`) keep working with no changes.

Concurrency: revisions are append-only on success; a second concurrent
write to the same batch is prevented by the existing single-flight
processing lock in `processing_queue.py`. Reads are best-effort and
tolerate partial state.

Safety:
  * `rev_id` is generated server-side with the format
    `rev_<UTC ISO compact>` so operators can't inject a path.
  * Listing rejects entries whose resolved path escapes the
    `revisions/` directory.
  * Activation is path-traversal-checked before any file copy.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import shutil
from pathlib import Path
from typing import Any

from . import batch_store


_REV_ID_RE = re.compile(r"^rev_[0-9TZ_-]+$")


def _revisions_dir(batch_id: str) -> Path:
    """Return the per-batch revisions folder, creating it lazily."""
    bdir = batch_store.get_batch_dir(batch_id)
    rdir = bdir / "revisions"
    rdir.mkdir(parents=True, exist_ok=True)
    return rdir


def _index_path(batch_id: str) -> Path:
    return _revisions_dir(batch_id) / "index.json"


def _active_cache_path(batch_id: str) -> Path:
    return batch_store.get_processed_dir(batch_id) / "_webapp_result.json"


def _generate_rev_id() -> str:
    now = _dt.datetime.now(tz=_dt.timezone.utc)
    return "rev_" + now.strftime("%Y%m%dT%H%M%SZ")


def _read_index(batch_id: str) -> list[dict[str, Any]]:
    p = _index_path(batch_id)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except (OSError, ValueError):
        pass
    return []


def _write_index(batch_id: str, entries: list[dict[str, Any]]) -> None:
    p = _index_path(batch_id)
    p.write_text(
        json.dumps(entries, indent=2, default=str),
        encoding="utf-8",
    )


def _check_rev_id(rev_id: str) -> str:
    if not rev_id or not _REV_ID_RE.match(rev_id):
        raise ValueError("Invalid revision id.")
    return rev_id


def record_revision(
    batch_id: str,
    *,
    result: dict[str, Any],
    export_name: str | None = None,
    status: str = "completed",
) -> dict[str, Any]:
    """Snapshot a finished process result as a new revision.

    Caller is the webapp processing layer right after the result cache
    has been written (so the active cache + the snapshot are byte-for-
    byte identical at creation time). Returns the new manifest entry.
    """
    rev_id = _generate_rev_id()
    rdir = _revisions_dir(batch_id)
    snap_path = rdir / f"{rev_id}.json"
    snap_path.write_text(
        json.dumps(result, indent=2, default=str),
        encoding="utf-8",
    )

    summary = (result or {}).get("summary") or {}
    invoices_count = (
        summary.get("invoices_total")
        or len((result or {}).get("all_invoices") or [])
    )
    rows_count = sum(
        len((inv or {}).get("rows") or [])
        for inv in ((result or {}).get("all_invoices") or [])
    )
    files_count = summary.get("files_total") or 0
    manual_review_count = (
        summary.get("manual_review_total")
        or len((result or {}).get("all_manual_review") or [])
    )

    entry = {
        "revision_id": rev_id,
        "created_at": _dt.datetime.now(tz=_dt.timezone.utc).isoformat(
            timespec="seconds"
        ),
        "status": status,
        "export_name": export_name,
        "files_count": int(files_count),
        "invoices_count": int(invoices_count),
        "rows_count": int(rows_count),
        "manual_review_count": int(manual_review_count),
        "source_batch_id": batch_id,
        "snapshot_filename": snap_path.name,
    }

    entries = _read_index(batch_id)
    entries.insert(0, entry)  # newest first
    _write_index(batch_id, entries)
    return entry


def list_revisions(batch_id: str) -> list[dict[str, Any]]:
    """Return the manifest, newest first. Filters entries whose snapshot
    file is missing so the UI never shows a broken revision."""
    rdir = _revisions_dir(batch_id)
    entries = _read_index(batch_id)
    out: list[dict[str, Any]] = []
    for e in entries:
        snap = e.get("snapshot_filename")
        if not isinstance(snap, str):
            continue
        if not _REV_ID_RE.match(e.get("revision_id") or ""):
            continue
        snap_path = (rdir / snap).resolve()
        try:
            snap_path.relative_to(rdir.resolve())
        except ValueError:
            continue  # path-traversal attempt: ignore
        if not snap_path.is_file():
            continue
        out.append(e)
    return out


def activate_revision(batch_id: str, rev_id: str) -> dict[str, Any]:
    """Make `rev_id` the active revision. Returns the manifest entry."""
    rev_id = _check_rev_id(rev_id)
    rdir = _revisions_dir(batch_id)
    snap_path = (rdir / f"{rev_id}.json").resolve()
    try:
        snap_path.relative_to(rdir.resolve())
    except ValueError:
        raise ValueError("Invalid revision path.")
    if not snap_path.is_file():
        raise FileNotFoundError(f"Revision snapshot not found: {rev_id}")

    active = _active_cache_path(batch_id)
    active.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(snap_path, active)

    # Find the manifest entry; raise if missing so the API can 404.
    for entry in list_revisions(batch_id):
        if entry["revision_id"] == rev_id:
            return entry
    raise FileNotFoundError(f"Revision not in index: {rev_id}")


def overwrite_snapshot(
    batch_id: str,
    rev_id: str,
    *,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Replace a revision's snapshot bytes with the supplied result dict.

    Used by the "save edits" flow: the operator's edits have just been
    merged into the active cache; we mirror those bytes into the
    revision file so re-activating that revision restores the edited
    state. The manifest entry's counts are refreshed but `created_at`
    and `revision_id` stay put — saving an edit is NOT a new revision.
    """
    rev_id = _check_rev_id(rev_id)
    rdir = _revisions_dir(batch_id)
    snap_path = (rdir / f"{rev_id}.json").resolve()
    try:
        snap_path.relative_to(rdir.resolve())
    except ValueError:
        raise ValueError("Invalid revision path.")
    if not snap_path.is_file():
        raise FileNotFoundError(f"Revision snapshot not found: {rev_id}")

    snap_path.write_text(
        json.dumps(result, indent=2, default=str),
        encoding="utf-8",
    )

    summary = (result or {}).get("summary") or {}
    invoices_count = (
        summary.get("invoices_total")
        or len((result or {}).get("all_invoices") or [])
    )
    rows_count = sum(
        len((inv or {}).get("rows") or [])
        for inv in ((result or {}).get("all_invoices") or [])
    )
    files_count = summary.get("files_total") or 0
    manual_review_count = (
        summary.get("manual_review_total")
        or len((result or {}).get("all_manual_review") or [])
    )

    entries = _read_index(batch_id)
    updated: dict[str, Any] | None = None
    for e in entries:
        if e.get("revision_id") == rev_id:
            e["invoices_count"] = int(invoices_count)
            e["rows_count"] = int(rows_count)
            e["files_count"] = int(files_count)
            e["manual_review_count"] = int(manual_review_count)
            e["edited_at"] = _dt.datetime.now(tz=_dt.timezone.utc).isoformat(
                timespec="seconds"
            )
            updated = e
            break
    if updated is None:
        raise FileNotFoundError(f"Revision not in index: {rev_id}")
    _write_index(batch_id, entries)
    return updated


def delete_revision(batch_id: str, rev_id: str) -> dict[str, Any]:
    """Remove a revision from the manifest and delete its snapshot.

    Returns the deleted entry. Raises ``FileNotFoundError`` if the
    revision is unknown. The active cache (`_webapp_result.json`) is
    NOT touched: if the deleted revision happened to be the most recent
    snapshot, the operator simply sees the same preview until they
    activate another revision or run a new process.
    """
    rev_id = _check_rev_id(rev_id)
    rdir = _revisions_dir(batch_id)
    snap_path = (rdir / f"{rev_id}.json").resolve()
    try:
        snap_path.relative_to(rdir.resolve())
    except ValueError:
        raise ValueError("Invalid revision path.")

    entries = _read_index(batch_id)
    deleted: dict[str, Any] | None = None
    remaining: list[dict[str, Any]] = []
    for e in entries:
        if e.get("revision_id") == rev_id:
            deleted = e
        else:
            remaining.append(e)
    if deleted is None and not snap_path.is_file():
        raise FileNotFoundError(f"Revision not found: {rev_id}")

    # Snapshot file is best-effort: missing on disk is fine, manifest
    # entry being gone is the source of truth.
    try:
        snap_path.unlink(missing_ok=True)
    except OSError:
        pass

    _write_index(batch_id, remaining)
    return deleted or {"revision_id": rev_id}


def current_revision_id(batch_id: str) -> str | None:
    """Best-effort: the newest revision is the current one unless the
    operator chose another (which we record by stamping `current` in
    the manifest entry). For now we just return the newest."""
    entries = list_revisions(batch_id)
    if not entries:
        return None
    return entries[0].get("revision_id")
