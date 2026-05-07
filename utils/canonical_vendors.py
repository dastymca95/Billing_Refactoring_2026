"""Phase 2L — Canonical vendor-name lookup.

Resolves a vendor's *display name* from the project's hand-curated
``Vendors/Vendor List.csv`` rather than whatever string a particular
bill happens to print at the top of the PDF. ResMan import requires
the exact spelling from the Vendor List, so vendors processors must
not invent their own (e.g. "Columbia Power & Water Systems" with the
plural is wrong; the canonical entry is "Columbia Power and Water
System").

Lookup keys
-----------
* By ``vendor_key`` — the snake-cased slug used by vendor YAMLs.
  Resolution: snake-case the CSV's "Vendor" column and compare.
* By alias — falls back to the YAML's ``vendor_identity.aliases``
  list if provided.

Cached at module level. The CSV is small (~600 rows) and rarely
changes during a process lifecycle.
"""

from __future__ import annotations

import csv
import logging
import re
import threading
from pathlib import Path
from typing import Optional


_LOG = logging.getLogger(__name__)
_LOCK = threading.RLock()
_CACHE: Optional[dict[str, str]] = None
_CACHE_PATH: Optional[Path] = None


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _vendor_list_path() -> Path:
    return _project_root() / "Vendors" / "Vendor List.csv"


_KEY_RE = re.compile(r"[^a-z0-9]+")


def _to_key(s: str) -> str:
    return _KEY_RE.sub("_", (s or "").lower()).strip("_")


def _load() -> dict[str, str]:
    """Return a {snake_key: canonical_name} map."""
    global _CACHE, _CACHE_PATH
    with _LOCK:
        path = _vendor_list_path()
        if _CACHE is not None and _CACHE_PATH == path:
            return _CACHE
        out: dict[str, str] = {}
        if not path.is_file():
            _LOG.warning("Vendor List not found at %s", path)
            _CACHE = out
            _CACHE_PATH = path
            return out
        # Vendor List.csv is exported from Excel and often has stray
        # cp1252 / Latin-1 bytes (e.g. non-breaking spaces, smart
        # quotes). Fall through several encodings before giving up so
        # one bad byte doesn't bring the whole lookup down.
        for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
            try:
                with open(path, "r", encoding=enc, newline="") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        name = (row.get("Vendor") or "").strip()
                        if not name:
                            continue
                        out[_to_key(name)] = name
                if out:
                    break
                # Empty parse — try the next encoding.
                out = {}
            except UnicodeDecodeError:
                continue
            except Exception as e:  # pragma: no cover
                _LOG.warning("Failed to read Vendor List with %s: %s", enc, e)
                break
        _CACHE = out
        _CACHE_PATH = path
        return out


def canonical_vendor_name(
    *,
    vendor_key: str = "",
    aliases: Optional[list[str]] = None,
    fallback: str = "",
) -> str:
    """Resolve the canonical Vendor List spelling.

    Tries ``vendor_key`` first, then each alias (snake-cased), then
    returns ``fallback`` (typically whatever the bill printed).
    """
    table = _load()
    if vendor_key:
        hit = table.get(_to_key(vendor_key))
        if hit:
            return hit
    for alias in aliases or []:
        hit = table.get(_to_key(alias))
        if hit:
            return hit
    return fallback or vendor_key or ""
