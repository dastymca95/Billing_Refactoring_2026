"""Billing V2 contract helpers.

This module is intentionally thin: Billing V2 should not fork the existing
vendor processors. It audits the deterministic registry and exposes a
normalised document-link preparation step that can run before export.
"""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import batch_processor, batch_store, row_normalizer


RESULT_CACHE_NAME = "_webapp_result.json"


@dataclass(frozen=True)
class ProcessorAuditEntry:
    vendor_key: str
    entrypoint: str
    module: str
    deterministic: bool
    available: bool
    error: str = ""


def deterministic_processor_audit() -> dict[str, Any]:
    """Return every deterministic processor currently registered.

    The registry is still owned by ``batch_processor`` so legacy behavior stays
    untouched. Billing V2 uses this as its deterministic-first inventory.
    """

    entries: list[ProcessorAuditEntry] = []
    for vendor_key, (loader, entrypoint) in sorted(
        batch_processor._PROCESSOR_LOADERS.items()  # type: ignore[attr-defined]
    ):
        module_name = ""
        available = False
        error = ""
        try:
            module = loader()
            module_name = getattr(module, "__name__", "")
            available = callable(getattr(module, entrypoint, None))
            if not available:
                error = f"entrypoint_missing:{entrypoint}"
        except Exception as exc:  # pragma: no cover - defensive audit path
            module_name = getattr(loader, "__name__", "")
            error = f"{type(exc).__name__}: {exc}"
        entries.append(
            ProcessorAuditEntry(
                vendor_key=vendor_key,
                entrypoint=entrypoint,
                module=module_name,
                deterministic=True,
                available=available,
                error=error,
            )
        )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(entries),
        "available_count": sum(1 for entry in entries if entry.available),
        "processors": [entry.__dict__ for entry in entries],
        "ai_fallback_module": _module_available("webapp.backend.services.ai_invoice_processor"),
    }


def prepare_document_links(batch_id: str) -> dict[str, Any]:
    """Ensure cached preview rows have deterministic source document links.

    Row normalization already provides the cross-vendor local webapp link
    fallback. Running it here makes the lifecycle explicit for Billing V2:
    upload/process creates or reserves internal row links before export, while
    export may later upgrade those links to Dropbox.
    """

    bdir = batch_store.get_batch_dir(batch_id)
    cache_path = batch_store.get_processed_dir(batch_id) / RESULT_CACHE_NAME
    if not cache_path.is_file():
        return {
            "batch_id": batch_id,
            "prepared": False,
            "reason": "no_processed_preview",
            "rows_total": 0,
            "rows_with_links": 0,
            "rows_missing_links": 0,
            "links": _empty_link_counts(),
        }

    result = json.loads(cache_path.read_text(encoding="utf-8")) or {}
    before = _link_snapshot(result)
    row_normalizer.normalize_result(result)
    after = _link_snapshot(result)

    if after != before:
        _atomic_write_json(cache_path, result)

    return {
        "batch_id": batch_id,
        "prepared": True,
        "changed": after != before,
        "cache_path": str(cache_path),
        "rows_total": after["rows_total"],
        "rows_with_links": after["rows_with_links"],
        "rows_missing_links": after["rows_missing_links"],
        "links": after["links"],
        "audit_dir": str(bdir / "audit"),
    }


def _module_available(module_name: str) -> dict[str, Any]:
    try:
        importlib.import_module(module_name)
        return {"module": module_name, "available": True}
    except Exception as exc:  # pragma: no cover - defensive audit path
        return {
            "module": module_name,
            "available": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _empty_link_counts() -> dict[str, int]:
    return {
        "local_webapp": 0,
        "dropbox": 0,
        "external": 0,
        "missing": 0,
    }


def _link_snapshot(result: dict[str, Any]) -> dict[str, Any]:
    rows = _iter_rows(result)
    counts = _empty_link_counts()
    rows_total = 0
    for row in rows:
        rows_total += 1
        url = str(row.get("Document Url") or "").strip()
        counts[_link_kind(url)] += 1
    return {
        "rows_total": rows_total,
        "rows_with_links": rows_total - counts["missing"],
        "rows_missing_links": counts["missing"],
        "links": counts,
    }


def _iter_rows(result: dict[str, Any]):
    for invoice in result.get("all_invoices") or []:
        if not isinstance(invoice, dict):
            continue
        for row in invoice.get("rows") or []:
            if isinstance(row, dict):
                yield row


def _link_kind(url: str) -> str:
    if not url:
        return "missing"
    try:
        parsed = urlparse(url)
    except Exception:
        return "external"
    host = (parsed.hostname or parsed.netloc or "").lower()
    if host in {"localhost", "127.0.0.1", "::1"}:
        return "local_webapp"
    if host == "dropbox.com" or host.endswith(".dropbox.com") or host.endswith(".dropboxusercontent.com"):
        return "dropbox"
    return "external"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)
