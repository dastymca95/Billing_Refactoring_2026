"""Phase 2K — Learned Corrections store.

Stores operator-curated corrections that future processing runs can
consult as a fallback layer. Lives at:

    webapp_data/learned_corrections/<vendor_key>.json

One file per vendor; each file is a list of correction entries. The
file schema is intentionally explicit and stable so future processors
(or different vendors) can read it without coupling to this module.

Two correction kinds today:

  * ``value_override`` — replace a column value when a trigger
    matches. Trigger can be by `account_number` or by the original
    detected_text the trace recorded. Useful for "this account always
    needs Invoice Description = X".
  * ``region_remap`` — operator drew a new bbox for an extraction
    field. Future runs of similar bills (same vendor, similar layout)
    can read text from that bbox instead of the regex-based
    extraction.

Both kinds support ``scope: cell|document|batch|vendor``. ``vendor``
scope means "apply on every future bill from this vendor" and is the
one most processors can consume cheaply.

Design notes
------------
* No layout fingerprinting yet — vendor + (optional) account_number
  is the dedup key. A signature scheme can be added later without
  breaking the file shape.
* Writes are atomic: full file replace via tempfile.
* Read failures degrade to "no corrections" so a corrupt sidecar can't
  break an unrelated processing run.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional


_LOG = logging.getLogger(__name__)
_LOCK = threading.RLock()


# ---------------------------------------------------------------------------
# Filesystem layout
# ---------------------------------------------------------------------------


def _store_root() -> Path:
    """Resolve the on-disk root. Mirrors how `batch_store` resolves its
    own root — relative to the project's `webapp_data/` folder."""
    # webapp_data lives next to the project root; we walk up from this
    # module to find it.
    here = Path(__file__).resolve()
    # webapp/backend/services/learned_corrections.py → walk up 3
    project = here.parents[3]
    root = project / "webapp_data" / "learned_corrections"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _vendor_path(vendor_key: str) -> Path:
    safe = _safe_vendor_key(vendor_key)
    return _store_root() / f"{safe}.json"


_VENDOR_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_vendor_key(vendor_key: str) -> str:
    s = (vendor_key or "unknown").strip().lower()
    s = _VENDOR_SAFE_RE.sub("_", s)
    return s[:120] or "unknown"


# ---------------------------------------------------------------------------
# Data shape
# ---------------------------------------------------------------------------


@dataclass
class CorrectionTrigger:
    """When does this correction fire."""

    # If set, only fires for invoices with this exact account number.
    account_number: str = ""
    # If set, fires when the source PDF page is this filename (for
    # document-scope corrections).
    source_file: str = ""
    # If set, fires only when the existing detected text contains this
    # substring (case-insensitive). Useful for "if Invoice Description
    # currently equals the vendor name, fix it".
    contains_text: str = ""
    # The column the operator was looking at when they recorded the
    # correction. Often used for value_override.
    column: str = ""


@dataclass
class CorrectionAction:
    """What the correction does when it fires.

    Exactly one of `set_column_value` / `region_bbox` is set.
    """

    # Direct value override.
    set_column: str = ""
    set_value: Optional[str] = None
    # Region remap: a bbox the processor should read text from to
    # populate a specific field.
    field_key: str = ""             # e.g. "service_address"
    bbox: Optional[dict[str, float]] = None  # normalized 0..1
    page: int = 1


@dataclass
class CorrectionEntry:
    """One persisted learned correction."""

    correction_id: str
    vendor_key: str
    kind: str                       # "value_override" | "region_remap"
    scope: str = "vendor"           # "cell" | "document" | "batch" | "vendor"
    trigger: dict[str, Any] = field(default_factory=dict)
    action: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    created_from: dict[str, Any] = field(default_factory=dict)
    note: str = ""


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------


def _load_raw(vendor_key: str) -> list[dict[str, Any]]:
    p = _vendor_path(vendor_key)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("corrections"), list):
            return data["corrections"]
    except (OSError, ValueError) as e:
        _LOG.warning("learned_corrections load failed for %s: %s", vendor_key, e)
    return []


def _save_raw(vendor_key: str, entries: list[dict[str, Any]]) -> None:
    p = _vendor_path(vendor_key)
    tmp = p.with_suffix(p.suffix + ".tmp")
    payload = {
        "vendor_key": _safe_vendor_key(vendor_key),
        "updated_at": _now(),
        "corrections": entries,
    }
    tmp.write_text(
        json.dumps(payload, indent=2, default=str),
        encoding="utf-8",
    )
    tmp.replace(p)


def _now() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).isoformat(timespec="seconds")


def _new_id() -> str:
    return "lc_" + uuid.uuid4().hex[:10]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_corrections(vendor_key: str = "") -> list[dict[str, Any]]:
    """Return all corrections, optionally filtered by vendor."""
    with _LOCK:
        if vendor_key:
            return list(_load_raw(vendor_key))
        # All vendors: walk the store dir.
        root = _store_root()
        out: list[dict[str, Any]] = []
        for fp in sorted(root.glob("*.json")):
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    items = data.get("corrections") or []
                else:
                    items = data if isinstance(data, list) else []
                out.extend(items)
            except (OSError, ValueError):
                continue
        return out


def add_correction(
    *,
    vendor_key: str,
    kind: str,
    scope: str = "vendor",
    trigger: Optional[dict[str, Any]] = None,
    action: Optional[dict[str, Any]] = None,
    created_from: Optional[dict[str, Any]] = None,
    note: str = "",
) -> dict[str, Any]:
    """Persist a new correction. Returns the stored entry (with id)."""
    if kind not in ("value_override", "region_remap"):
        raise ValueError(f"Unknown correction kind: {kind!r}")
    if scope not in ("cell", "document", "batch", "vendor"):
        raise ValueError(f"Unknown scope: {scope!r}")
    entry = CorrectionEntry(
        correction_id=_new_id(),
        vendor_key=_safe_vendor_key(vendor_key),
        kind=kind,
        scope=scope,
        trigger=dict(trigger or {}),
        action=dict(action or {}),
        created_at=_now(),
        created_from=dict(created_from or {}),
        note=str(note or ""),
    )
    with _LOCK:
        existing = _load_raw(vendor_key)
        existing.append(asdict(entry))
        _save_raw(vendor_key, existing)
    return asdict(entry)


def delete_correction(vendor_key: str, correction_id: str) -> bool:
    """Drop one correction by id. Returns True if removed."""
    with _LOCK:
        existing = _load_raw(vendor_key)
        kept = [e for e in existing if e.get("correction_id") != correction_id]
        if len(kept) == len(existing):
            return False
        _save_raw(vendor_key, kept)
        return True


# ---------------------------------------------------------------------------
# Application: applied to a result dict at the end of processing.
# Vendor processors may also call this directly if they want learned
# corrections to be visible inside their own debug logs.
# ---------------------------------------------------------------------------


def apply_value_overrides_to_rows(
    rows: list[dict[str, Any]],
    vendor_key: str,
    *,
    inv_account_lookup: Optional[dict[int, str]] = None,
) -> int:
    """Apply ``value_override`` corrections to a list of preview rows.

    Mutates rows in place. Returns the number of rows touched.
    Triggers are matched best-effort:

      * ``account_number`` — matches any row whose row index is in
        ``inv_account_lookup`` AND whose account equals the trigger.
      * ``column`` + ``contains_text`` — matches when the *current*
        cell value contains the trigger text (case-insensitive).
      * ``column`` only — matches every row (vendor-wide override).

    Region-remap corrections are NOT applied here — they need the raw
    PDF and word boxes, which only the vendor processor has. Those
    corrections are surfaced via ``list_corrections`` for the
    processor to consume during extraction.
    """
    corrections = list_corrections(vendor_key)
    if not corrections:
        return 0
    overrides = [c for c in corrections if c.get("kind") == "value_override"]
    if not overrides:
        return 0
    inv_account_lookup = inv_account_lookup or {}
    touched = 0
    for r_idx, row in enumerate(rows):
        for c in overrides:
            trig = c.get("trigger") or {}
            act = c.get("action") or {}
            col = (act.get("set_column") or trig.get("column") or "").strip()
            if not col:
                continue
            if col not in row:
                continue
            # account match (if set)
            want_acct = (trig.get("account_number") or "").strip()
            if want_acct:
                got_acct = inv_account_lookup.get(r_idx) or ""
                if got_acct != want_acct:
                    continue
            # contains_text match (if set)
            want_text = (trig.get("contains_text") or "").strip().lower()
            if want_text:
                cur = str(row.get(col) or "").lower()
                if want_text not in cur:
                    continue
            new_val = act.get("set_value")
            if new_val is None:
                continue
            if col == "GL Account":
                from . import ai_mapping_review

                account = ai_mapping_review.validate_gl_account(str(new_val))
                if not account or not ai_mapping_review.is_payable_gl_account(account):
                    continue
                new_val = account["gl_code"]
            row[col] = new_val
            meta = row.setdefault("_meta", {})
            applied = list(meta.get("learned_corrections_applied") or [])
            applied.append({
                "correction_id": c.get("correction_id"),
                "scope": c.get("scope"),
                "column": col,
            })
            meta["learned_corrections_applied"] = applied
            touched += 1
    return touched
