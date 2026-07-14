"""Deterministic, versioned authority for accounting readiness and export."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable

from pydantic import BaseModel, Field

from .template_rules import get_template_rules
from .utility_processor_common import load_chart_of_accounts


CONTRACT_VERSION = "accounting-readiness/1.0"


class ReadinessStatus(str, Enum):
    READY = "ready"
    NEEDS_REVIEW = "needs_review"
    BLOCKED = "blocked"


class ReadinessSeverity(str, Enum):
    BLOCKING = "blocking"
    NON_BLOCKING = "non_blocking"
    INFO = "info"


class ReadinessIssue(BaseModel):
    code: str
    severity: ReadinessSeverity
    scope: str
    invoice_id: str | None = None
    line_item_id: str | None = None
    field: str | None = None
    message: str
    source: str
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    resolution_required: bool = False
    resolved: bool = False
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    resolution_evidence: dict[str, Any] | None = None


class AccountingReadiness(BaseModel):
    contract_version: str = CONTRACT_VERSION
    snapshot_id: str
    status: ReadinessStatus
    export_allowed: bool
    blockers: list[ReadinessIssue] = Field(default_factory=list)
    non_blocking_issues: list[ReadinessIssue] = Field(default_factory=list)
    validated_fields: dict[str, bool] = Field(default_factory=dict)
    reconciliation_status: str
    duplicate_status: str
    evaluated_at: datetime


def _dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json") if hasattr(model, "model_dump") else model.dict()


def _invoice_id(row: dict[str, Any], index: int) -> str:
    meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
    return str(meta.get("invoice_group_id") or meta.get("invoice_number") or row.get("Invoice Number") or f"invoice-{index + 1}")


def _finite_amount(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _snapshot(rows: list[dict[str, Any]]) -> str:
    material = []
    for row in rows:
        material.append({k: v for k, v in row.items() if k not in {"accounting_readiness", "_readiness"}})
    raw = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256((CONTRACT_VERSION + "\n" + raw).encode("utf-8")).hexdigest()


def _issue(*, code: str, invoice_id: str, row_index: int | None, field: str | None,
           message: str, source: str = "accounting_readiness") -> ReadinessIssue:
    return ReadinessIssue(
        code=code,
        severity=ReadinessSeverity.BLOCKING,
        scope="line_item" if row_index is not None else "invoice",
        invoice_id=invoice_id,
        line_item_id=str(row_index) if row_index is not None else None,
        field=field,
        message=message,
        source=source,
        evidence=[{"row_index": row_index, "field": field}] if row_index is not None else [],
        resolution_required=True,
    )


def _reconciliation_for_group(group: list[dict[str, Any]]) -> tuple[str, ReadinessIssue | None]:
    first = group[0]
    meta = first.get("_meta") if isinstance(first.get("_meta"), dict) else {}
    provenance = meta.get("ai_provenance") if isinstance(meta.get("ai_provenance"), dict) else {}
    invoice_id = _invoice_id(first, 0)
    explicit = meta.get("total_reconciliation_passed")
    if explicit is None:
        explicit = provenance.get("total_reconciliation_passed")
    expected = provenance.get("invoice_total")
    actual = sum(float(row.get("Amount")) for row in group if _finite_amount(row.get("Amount")))
    if explicit is False or (expected is not None and _finite_amount(expected) and abs(float(expected) - actual) > 0.01):
        issue = _issue(code="total_mismatch", invoice_id=invoice_id, row_index=None,
                       field="Amount", message="Line amounts do not reconcile to the invoice total.")
        issue.evidence = [{"invoice_total": expected, "line_total": round(actual, 2), "explicit_passed": explicit}]
        return "failed", issue
    return ("passed" if explicit is True or expected is not None else "not_applicable"), None


def evaluate_rows(rows: Iterable[dict[str, Any]], *, duplicate_status: str = "not_detected") -> AccountingReadiness:
    """Evaluate export rows without AI confidence or extraction-warning inputs."""
    row_list = [dict(row) for row in rows if isinstance(row, dict)]
    blockers: list[ReadinessIssue] = []
    non_blocking: list[ReadinessIssue] = []
    required = list(get_template_rules().get("required_columns") or [])
    valid_gl = load_chart_of_accounts()
    groups: dict[str, list[dict[str, Any]]] = {}
    field_results: dict[str, bool] = {column: True for column in required}
    field_results.update({"Property Abbreviation": True, "GL Account": True, "Amount": True})

    if not row_list:
        blockers.append(_issue(code="no_export_rows", invoice_id="batch", row_index=None,
                               field=None, message="There are no rows to export."))

    for index, row in enumerate(row_list):
        invoice_id = _invoice_id(row, index)
        groups.setdefault(invoice_id, []).append(row)
        for column in required:
            if not str(row.get(column) if row.get(column) is not None else "").strip():
                field_results[column] = False
                blockers.append(_issue(code=f"required_field_missing:{column}", invoice_id=invoice_id,
                                       row_index=index, field=column, message=f"{column} is required for export."))
        prop = str(row.get("Property Abbreviation") or "").strip()
        if not prop and "Property Abbreviation" not in required:
            field_results["Property Abbreviation"] = False
            blockers.append(_issue(code="property_missing", invoice_id=invoice_id, row_index=index,
                                   field="Property Abbreviation", message="Property is required for export."))
        gl = str(row.get("GL Account") or "").strip()
        if not gl or not gl.isdigit() or (valid_gl and gl not in valid_gl):
            field_results["GL Account"] = False
            if not any(i.line_item_id == str(index) and i.field == "GL Account" for i in blockers):
                blockers.append(_issue(code="gl_invalid", invoice_id=invoice_id, row_index=index,
                                       field="GL Account", message="GL Account must be a valid chart account."))
        if not _finite_amount(row.get("Amount")):
            field_results["Amount"] = False
            if not any(i.line_item_id == str(index) and i.field == "Amount" for i in blockers):
                blockers.append(_issue(code="amount_invalid", invoice_id=invoice_id, row_index=index,
                                       field="Amount", message="Amount must be a finite number."))

        meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
        for warning in meta.get("vision_warnings") or meta.get("ai_warnings") or []:
            non_blocking.append(ReadinessIssue(code="extraction_warning", severity=ReadinessSeverity.NON_BLOCKING,
                scope="line_item", invoice_id=invoice_id, line_item_id=str(index), field=None,
                message=str(warning), source="vision_or_ocr", evidence=[{"warning": str(warning)}]))

    reconciliation_states: list[str] = []
    for group in groups.values():
        state, issue = _reconciliation_for_group(group)
        reconciliation_states.append(state)
        if issue:
            blockers.append(issue)
    reconciliation = "failed" if "failed" in reconciliation_states else ("passed" if reconciliation_states and all(s == "passed" for s in reconciliation_states) else "not_applicable")
    if duplicate_status in {"duplicate", "unresolved"}:
        blockers.append(_issue(code="duplicate_unresolved", invoice_id="batch", row_index=None,
                               field=None, message="Duplicate review must be resolved before export."))
    status = ReadinessStatus.BLOCKED if blockers else (ReadinessStatus.NEEDS_REVIEW if non_blocking else ReadinessStatus.READY)
    return AccountingReadiness(snapshot_id=_snapshot(row_list), status=status,
        export_allowed=not blockers, blockers=blockers, non_blocking_issues=non_blocking,
        validated_fields=field_results, reconciliation_status=reconciliation,
        duplicate_status=duplicate_status, evaluated_at=datetime.now(timezone.utc))


def as_dict(readiness: AccountingReadiness) -> dict[str, Any]:
    return _dump(readiness)


def evaluate_and_record(batch_id: str, rows: Iterable[dict[str, Any]]) -> AccountingReadiness:
    """Evaluate and persist the latest decision plus backend resolution evidence."""
    from . import batch_store

    decision = evaluate_rows(rows)
    path = batch_store.get_batch_dir(batch_id) / "audit" / "accounting_readiness.json"
    previous: dict[str, Any] = {}
    if path.is_file():
        try:
            previous = json.loads(path.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            previous = {}
    current_keys = {(issue.code, issue.invoice_id, issue.line_item_id) for issue in decision.blockers}
    for raw in previous.get("blockers") or []:
        key = (raw.get("code"), raw.get("invoice_id"), raw.get("line_item_id"))
        if key in current_keys:
            continue
        try:
            resolved = ReadinessIssue(**raw)
        except Exception:
            continue
        resolved.severity = ReadinessSeverity.INFO
        resolved.resolved = True
        resolved.resolved_by = "backend_validation"
        resolved.resolved_at = decision.evaluated_at
        resolved.resolution_evidence = {
            "previous_snapshot_id": previous.get("snapshot_id"),
            "current_snapshot_id": decision.snapshot_id,
            "validation": "blocker_condition_no_longer_present",
        }
        decision.non_blocking_issues.append(resolved)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(str(path) + ".tmp")
    temp.write_text(json.dumps(as_dict(decision), indent=2), encoding="utf-8")
    temp.replace(path)
    return decision


__all__ = ["AccountingReadiness", "ReadinessIssue", "ReadinessSeverity", "ReadinessStatus",
           "CONTRACT_VERSION", "as_dict", "evaluate_and_record", "evaluate_rows"]
