"""Phase PERF-1 — file-hash-keyed OCR result cache.

OCR is the single most expensive step in the pipeline for image-only
bills and scanned PDFs. Re-running Tesseract on the same file (e.g. an
operator re-clicks Process, or a single-file run after a batch run)
spends seconds redoing work whose output never changes.

This module caches OCR results by SHA-256 of the file bytes + the DPI
+ a schema version. The cache lives under
``webapp_data/cache/ocr/<hash>_<dpi>.json`` and is automatically
invalidated when the file's bytes change (new hash) or when the
schema version bumps (incompatible upgrade).

Design:
  * Cache values are plain JSON so they can be inspected/debugged.
  * Each entry is a `PdfExtractionResult`-shaped dict: `pages`,
    `extraction_method`, `confidence`, `warnings`. The caller
    re-hydrates into the dataclass.
  * Reads and writes are lock-free at the filesystem level — concurrent
    writes against the same hash are extremely unlikely (one batch at
    a time) and would just produce identical JSON.
  * The cache is opt-in via env: ``OCR_CACHE_DISABLED=1`` disables it
    entirely (for fixtures, smoke tests).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Optional


_LOG = logging.getLogger(__name__)
_SCHEMA_VERSION = "ocr_cache/v1"
_DISABLED = os.environ.get("OCR_CACHE_DISABLED", "").strip() == "1"


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _cache_dir() -> Path:
    return _project_root() / "webapp_data" / "cache" / "ocr"


def file_hash(path: Path, *, chunk: int = 1 << 20) -> str:
    """SHA-256 of the file's bytes. ~5 ms for a 2 MB PDF on SSD."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                buf = f.read(chunk)
                if not buf:
                    break
                h.update(buf)
    except Exception:
        return ""
    return h.hexdigest()


def cache_key(path: Path, dpi: int) -> str:
    h = file_hash(path)
    return f"{h}_{int(dpi)}" if h else ""


def lookup(path: Path, dpi: int) -> Optional[dict[str, Any]]:
    """Return the cached extraction payload for ``path`` at ``dpi``, or
    None if the file hasn't been OCR'd yet (or the cache is disabled)."""
    if _DISABLED:
        return None
    key = cache_key(path, dpi)
    if not key:
        return None
    fp = _cache_dir() / f"{key}.json"
    if not fp.is_file():
        return None
    try:
        with open(fp, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        _LOG.debug("OCR cache read failed for %s: %s", fp.name, e)
        return None
    if not isinstance(data, dict) or data.get("schema") != _SCHEMA_VERSION:
        return None
    return data


def store(path: Path, dpi: int, payload: dict[str, Any]) -> None:
    """Write an extraction payload to the cache. Failures are logged
    and ignored — cache writes must never break processing."""
    if _DISABLED:
        return
    key = cache_key(path, dpi)
    if not key:
        return
    out_dir = _cache_dir()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return
    fp = out_dir / f"{key}.json"
    body = dict(payload)
    body["schema"] = _SCHEMA_VERSION
    body["source_filename"] = path.name
    body.setdefault("dpi", int(dpi))
    try:
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(body, f, default=str)
    except Exception as e:
        _LOG.debug("OCR cache write failed for %s: %s", fp.name, e)


def cache_stats() -> dict[str, Any]:
    out_dir = _cache_dir()
    if not out_dir.is_dir():
        return {"enabled": not _DISABLED, "count": 0, "size_bytes": 0}
    total = 0
    count = 0
    for p in out_dir.glob("*.json"):
        try:
            total += p.stat().st_size
            count += 1
        except OSError:
            continue
    return {
        "enabled": not _DISABLED,
        "count": count,
        "size_bytes": total,
        "directory": str(out_dir),
    }
