"""Durable, human-approved invoice corrections.

Assistant proposals are inert until an operator approves them.  Approved
corrections are runtime data (never source-code rules) and are replayed only
for the same batch/invoice/observed line after a reprocess.  A GL correction
is exposed to AccountingDecisionEngine as a ``manual_approved`` candidate;
this module never writes a final GL decision itself.
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .. import settings
from .gl_catalog import load_gl_catalog
from .invoice_identity import build_invoice_identities


CORRECTION_CONTRACT_VERSION = "approved-invoice-correction/1.0"
_LOCK = threading.RLock()


class ApprovedInvoiceCorrection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: str = CORRECTION_CONTRACT_VERSION
    correction_id: str
    interaction_id: str
    batch_id: str
    invoice_group_id: str
    local_row_index: int = Field(ge=0)
    line_fingerprint: str
    field: Literal[
        "GL Account", "Property Abbreviation", "Location",
        "Invoice Description", "Line Item Description",
    ]
    new_value: str
    rationale: str
    evidence: list[str] = Field(default_factory=list)
    approved_by: str
    approved_at: datetime
    status: Literal["active", "revoked"] = "active"


class CorrectionReplayReport(BaseModel):
    batch_id: str
    matched: int = 0
    applied: int = 0
    unresolved: int = 0
    touched_invoice_groups: list[str] = Field(default_factory=list)


def _store_path() -> Path:
    return settings.WEBAPP_DATA_ROOT / "accounting_assistant" / "approved_corrections.json"


def _load() -> list[ApprovedInvoiceCorrection]:
    path = _store_path()
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return [ApprovedInvoiceCorrection(**item) for item in payload.get("items", [])]
    except (OSError, ValueError, TypeError):
        return []


def _save(items: list[ApprovedInvoiceCorrection]) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps({
        "contract_version": CORRECTION_CONTRACT_VERSION,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "items": [item.model_dump(mode="json") for item in items],
        "privacy": "local_runtime_only",
    }, indent=2), encoding="utf-8")
    temp.replace(path)


def list_corrections(*, batch_id: str | None = None) -> list[ApprovedInvoiceCorrection]:
    with _LOCK:
        items = _load()
    if batch_id:
        items = [item for item in items if item.batch_id == batch_id]
    return sorted(items, key=lambda item: item.approved_at, reverse=True)


def line_fingerprint(row: dict[str, Any]) -> str:
    """Hash immutable observed facts, never generated descriptions."""
    meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
    source = meta.get("source_text") if isinstance(meta.get("source_text"), dict) else {}
    material = {
        "line_item_id": str(meta.get("line_item_id") or row.get("Line Item Number") or ""),
        "raw_activity": source.get("raw_activity"),
        "raw_description": source.get("raw_description"),
        "raw_section_header": source.get("raw_section_header"),
        "amount": str(row.get("Amount") or ""),
        "quantity": str(row.get("Quantity") or ""),
        "unit_price": str(row.get("Unit Price") or ""),
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


def approve(
    *, batch_id: str, invoice_group_id: str, interaction_id: str,
    corrections: list[Any], result: dict[str, Any], actor: str,
) -> list[ApprovedInvoiceCorrection]:
    """Persist validated proposals for exactly the selected invoice."""
    rows = _invoice_rows(result, invoice_group_id)
    by_global_index = {global_index: (local_index, row)
                       for global_index, local_index, row in rows}
    now = datetime.now(timezone.utc)
    approved: list[ApprovedInvoiceCorrection] = []
    for proposed in corrections:
        row_index = int(_proposed_value(proposed, "row_index"))
        match = by_global_index.get(row_index)
        if match is None:
            raise ValueError(f"Proposed row {row_index} no longer belongs to the selected invoice.")
        local_index, row = match
        field = str(_proposed_value(proposed, "field"))
        new_value = str(_proposed_value(proposed, "new_value")).strip()
        rationale = str(_proposed_value(proposed, "rationale"))
        evidence = list(_proposed_value(proposed, "evidence", []))
        if field == "GL Account":
            _, catalog = load_gl_catalog()
            if new_value not in catalog or not catalog[new_value].payable:
                raise ValueError(f"GL {new_value!r} is not a payable catalog account.")
        fingerprint = line_fingerprint(row)
        stable = "|".join((interaction_id, invoice_group_id, fingerprint, field))
        approved.append(ApprovedInvoiceCorrection(
            correction_id="aic_" + hashlib.sha256(stable.encode("utf-8")).hexdigest()[:16],
            interaction_id=interaction_id,
            batch_id=batch_id,
            invoice_group_id=invoice_group_id,
            local_row_index=local_index,
            line_fingerprint=fingerprint,
            field=field,
            new_value=new_value,
            rationale=rationale,
            evidence=evidence,
            approved_by=actor,
            approved_at=now,
        ))
    with _LOCK:
        existing = _load()
        by_id = {item.correction_id: item for item in existing}
        for item in approved:
            by_id[item.correction_id] = item
        _save(list(by_id.values()))
    return approved


def apply_to_result(result: dict[str, Any], *, batch_id: str) -> CorrectionReplayReport:
    """Replay active corrections and rerun the central accounting selector."""
    with _LOCK:
        active = [item for item in _load()
                  if item.status == "active" and item.batch_id == batch_id]
    report = CorrectionReplayReport(batch_id=batch_id)
    if not active:
        return report

    touched: dict[int, tuple[list[dict[str, Any]], str]] = {}
    all_views: list[list[dict[str, Any]]] = [list(result.get("all_invoices") or [])]
    for payload in (result.get("by_vendor") or {}).values():
        all_views.append(list((payload or {}).get("invoices") or []))

    matched_ids: set[str] = set()
    for invoices in all_views:
        identities = build_invoice_identities(invoices)
        for identity, invoice in zip(identities, invoices):
            relevant = [item for item in active if item.invoice_group_id == identity.group_id]
            if not relevant:
                continue
            rows = list(invoice.get("rows") or [])
            for item in relevant:
                row = _match_row(rows, item)
                if row is None:
                    continue
                _apply_one(row, item)
                matched_ids.add(item.correction_id)
                report.applied += 1
                touched_rows, document_id = touched.setdefault(
                    id(rows), ([], identity.source_file or batch_id),
                )
                if not any(existing is row for existing in touched_rows):
                    touched_rows.append(row)
                if identity.group_id not in report.touched_invoice_groups:
                    report.touched_invoice_groups.append(identity.group_id)

    report.matched = len(matched_ids)
    report.unresolved = max(0, len(active) - report.matched)
    if touched:
        from .accounting_integration_bridges import RowAccountingV2Adapter
        from . import output_contract_validator

        for touched_rows, document_id in touched.values():
            RowAccountingV2Adapter().enrich_rows(touched_rows, {
                "document_id": document_id,
                "extraction_route": "approved_operator_correction_replay",
            })
            output_contract_validator.annotate_rows(touched_rows)
    return report


def _apply_one(row: dict[str, Any], item: ApprovedInvoiceCorrection) -> None:
    meta = row.setdefault("_meta", {})
    if item.field == "GL Account":
        # Candidate only. AccountingDecisionEngine remains the final selector.
        meta["approved_operator_gl_candidate"] = item.new_value
        meta["approved_operator_gl_evidence"] = {
            "correction_id": item.correction_id,
            "interaction_id": item.interaction_id,
            "rationale": item.rationale,
            "evidence": list(item.evidence),
            "approved_by": item.approved_by,
            "approved_at": item.approved_at.isoformat(),
        }
    else:
        row[item.field] = item.new_value
    audit = list(meta.get("approved_operator_corrections_applied") or [])
    event = {
        "correction_id": item.correction_id,
        "interaction_id": item.interaction_id,
        "field": item.field,
        "approved_by": item.approved_by,
        "approved_at": item.approved_at.isoformat(),
    }
    if event not in audit:
        audit.append(event)
    meta["approved_operator_corrections_applied"] = audit


def _match_row(rows: list[dict[str, Any]], item: ApprovedInvoiceCorrection) -> dict[str, Any] | None:
    if item.local_row_index < len(rows):
        row = rows[item.local_row_index]
        if line_fingerprint(row) == item.line_fingerprint:
            return row
    matches = [row for row in rows if line_fingerprint(row) == item.line_fingerprint]
    return matches[0] if len(matches) == 1 else None


def _invoice_rows(
    result: dict[str, Any], invoice_group_id: str,
) -> list[tuple[int, int, dict[str, Any]]]:
    invoices = list(result.get("all_invoices") or [])
    identities = build_invoice_identities(invoices)
    output: list[tuple[int, int, dict[str, Any]]] = []
    global_index = 0
    for identity, invoice in zip(identities, invoices):
        for local_index, row in enumerate(invoice.get("rows") or []):
            if identity.group_id == invoice_group_id:
                output.append((global_index, local_index, row))
            global_index += 1
    if not output:
        raise KeyError(invoice_group_id)
    return output


def _proposed_value(proposed: Any, key: str, default: Any = None) -> Any:
    if isinstance(proposed, dict):
        return proposed.get(key, default)
    return getattr(proposed, key, default)


__all__ = [
    "ApprovedInvoiceCorrection", "CorrectionReplayReport", "apply_to_result",
    "approve", "line_fingerprint", "list_corrections",
]
