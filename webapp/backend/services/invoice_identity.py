"""Canonical invoice identity shared by preview and invoice-scoped APIs."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence


@dataclass(frozen=True)
class InvoiceIdentity:
    invoice_index: int
    source_file: str
    source_page: int
    invoice_number: str
    group_id: str


def build_invoice_identities(invoices: Sequence[dict[str, Any]]) -> list[InvoiceIdentity]:
    """Return the same stable identities for cached and freshly processed data."""
    source_counts: dict[str, int] = {}
    for invoice in invoices:
        source = invoice_source_file(invoice)
        if source:
            source_counts[source] = source_counts.get(source, 0) + 1

    source_seen: dict[str, int] = {}
    identities: list[InvoiceIdentity] = []
    for invoice_index, invoice in enumerate(invoices):
        source = invoice_source_file(invoice)
        if source:
            source_seen[source] = source_seen.get(source, 0) + 1
        explicit_page = invoice_source_page(invoice)
        fallback_page = (
            source_seen.get(source, 1)
            if source and source_counts.get(source, 0) > 1
            else 1
        )
        source_page = explicit_page or fallback_page
        invoice_number = str(invoice.get("invoice_number") or "").strip()
        group_id = "::".join([
            source or "unknown-file",
            f"page-{source_page}",
            invoice_number or f"invoice-{invoice_index + 1}",
        ])
        identities.append(InvoiceIdentity(
            invoice_index=invoice_index,
            source_file=source,
            source_page=source_page,
            invoice_number=invoice_number,
            group_id=group_id,
        ))
    return identities


def invoice_source_file(invoice: dict[str, Any]) -> str:
    debug = invoice.get("debug_info") if isinstance(invoice.get("debug_info"), dict) else {}
    for key in ("source_file", "file_name", "filename"):
        value = invoice.get(key) or debug.get(key)
        if value:
            return str(value)
    return ""


def invoice_source_page(invoice: dict[str, Any]) -> int | None:
    debug = invoice.get("debug_info") if isinstance(invoice.get("debug_info"), dict) else {}
    for key in ("source_page", "source_page_number", "pdf_page_number", "page_number"):
        page = _positive_int(invoice.get(key) or debug.get(key))
        if page is not None:
            return page
    return None


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None


__all__ = [
    "InvoiceIdentity", "build_invoice_identities", "invoice_source_file",
    "invoice_source_page",
]
