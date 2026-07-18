"""Tenant-scoped, file-first ResMan context data hub.

Raw reports are immutable evidence.  Published snapshots contain only a
minimal canonical projection and manual changes are stored as auditable
overlays so they survive later report imports.  Nothing in this module
selects a GL, authorizes export, or creates learned rules.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .. import settings
from .tenant_accounting_policies import validate_tenant_id


CONTRACT_VERSION = "resman-context-data/1.1"
MAX_UPLOAD_BYTES = 75 * 1024 * 1024
_LOCK = threading.RLock()


class DatasetKind(str, Enum):
    VENDORS = "vendors"
    PROPERTIES_UNITS = "properties_units"
    GL_ACCOUNTS = "gl_accounts"
    GENERAL_LEDGER = "general_ledger"
    INVOICE_HISTORY = "invoice_history"


class VendorResolutionStatus(str, Enum):
    EXACT = "exact"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"
    MISSING_SOURCE_NAME = "missing_source_name"


class ReconciliationStatus(str, Enum):
    MATCHED_TO_LEDGER = "matched_to_ledger"
    MATCHED_TO_INVOICE_HISTORY = "matched_to_invoice_history"
    POSTING_DATE_DIFFERENCE = "posting_date_difference"
    AMOUNT_MISMATCH = "amount_mismatch"
    GL_MISMATCH = "gl_mismatch"
    PROPERTY_MISMATCH = "property_mismatch"
    INVOICE_ONLY = "invoice_only"
    LEDGER_ONLY = "ledger_only"
    INVOICE_HISTORY_UNAVAILABLE = "invoice_history_unavailable"


class ImportIssue(BaseModel):
    code: str
    severity: Literal["error", "warning", "info"]
    message: str
    source_row: int | None = None


class ImportPreview(BaseModel):
    contract_version: str = CONTRACT_VERSION
    import_id: str
    tenant_id: str
    dataset: DatasetKind
    original_filename: str
    sha256: str
    size_bytes: int
    parsed_records: int
    added_records: int
    changed_records: int
    removed_records: int
    unchanged_records: int
    sample_records: list[dict[str, Any]] = Field(default_factory=list)
    issues: list[ImportIssue] = Field(default_factory=list)
    excluded_sensitive_columns: list[str] = Field(default_factory=list)
    status: Literal["preview_ready", "invalid"]
    created_at: datetime


class DatasetSnapshot(BaseModel):
    contract_version: str = CONTRACT_VERSION
    snapshot_id: str
    import_id: str
    tenant_id: str
    dataset: DatasetKind
    original_filename: str
    sha256: str
    record_count: int
    created_at: datetime
    activated_at: datetime
    active: bool


class DatasetStatus(BaseModel):
    contract_version: str = CONTRACT_VERSION
    tenant_id: str
    dataset: DatasetKind
    current_snapshot: DatasetSnapshot | None = None
    effective_record_count: int = 0
    manual_overlay_count: int = 0
    staged_import_count: int = 0


class RecordPage(BaseModel):
    contract_version: str = CONTRACT_VERSION
    tenant_id: str
    dataset: DatasetKind
    page: int
    page_size: int
    total: int
    items: list[dict[str, Any]]


class VendorRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company: str = Field(min_length=1, max_length=240)
    abbreviation: str | None = Field(default=None, max_length=100)
    customer_number: str | None = Field(default=None, max_length=120)
    status: str | None = Field(default=None, max_length=80)
    active: bool = True
    general_contact: str | None = Field(default=None, max_length=240)
    general_address: str | None = Field(default=None, max_length=300)
    general_city: str | None = Field(default=None, max_length=120)
    general_state: str | None = Field(default=None, max_length=40)
    general_zip: str | None = Field(default=None, max_length=30)
    general_phone: str | None = Field(default=None, max_length=80)
    general_email: str | None = Field(default=None, max_length=240)
    workflow: str | None = Field(default=None, max_length=120)
    default_gl: str | None = Field(default=None, max_length=80)
    insurances: list[dict[str, str | None]] = Field(default_factory=list, max_length=50)
    notes: str | None = Field(default=None, max_length=2000)


class PropertyUnitRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_type: Literal["property", "unit"]
    property_name: str = Field(min_length=1, max_length=240)
    property_code: str | None = Field(default=None, max_length=100)
    unit_number: str | None = Field(default=None, max_length=100)
    unit_type: str | None = Field(default=None, max_length=120)
    unit_status: str | None = Field(default=None, max_length=80)
    square_feet: str | None = Field(default=None, max_length=40)
    lease_status: str | None = Field(default=None, max_length=80)
    market_rent: str | None = Field(default=None, max_length=40)
    active: bool = True
    notes: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def require_unit_number(self):
        if self.entity_type == "unit" and not str(self.unit_number or "").strip():
            raise ValueError("unit_number is required for unit records")
        return self


class GLAccountRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gl_code: str = Field(min_length=1, max_length=80)
    gl_name: str = Field(min_length=1, max_length=240)
    account_type: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    payable: bool = False
    active: bool = True
    notes: str | None = Field(default=None, max_length=2000)


class LedgerRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_code: str | None = Field(default=None, max_length=80)
    account_name: str | None = Field(default=None, max_length=240)
    transaction_date: str | None = Field(default=None, max_length=40)
    reference: str | None = Field(default=None, max_length=160)
    property_code: str | None = Field(default=None, max_length=100)
    counterparty_name: str | None = Field(default=None, max_length=240)
    description: str | None = Field(default=None, max_length=1000)
    debit: str | None = Field(default=None, max_length=50)
    credit: str | None = Field(default=None, max_length=50)
    balance: str | None = Field(default=None, max_length=50)
    notes: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def require_accounting_content(self):
        if not any((
            self.account_code, self.transaction_date, self.reference,
            self.counterparty_name, self.description, self.debit, self.credit,
        )):
            raise ValueError("A ledger record requires transaction or accounting evidence.")
        return self


class InvoiceHistoryRecord(BaseModel):
    """One observable ResMan invoice allocation with its parent header facts."""

    model_config = ConfigDict(extra="forbid")
    invoice_occurrence_id: str = Field(min_length=1, max_length=80)
    allocation_index: int = Field(ge=1)
    vendor_name: str = Field(min_length=1, max_length=240)
    invoice_number: str = Field(min_length=1, max_length=160)
    invoice_date: str = Field(min_length=1, max_length=40)
    accounting_date: str | None = Field(default=None, max_length=40)
    due_date: str | None = Field(default=None, max_length=40)
    invoice_description: str | None = Field(default=None, max_length=1000)
    invoice_total: str = Field(min_length=1, max_length=50)
    po_number: str | None = Field(default=None, max_length=160)
    batch: str | None = Field(default=None, max_length=160)
    property_code: str = Field(min_length=1, max_length=100)
    gl_code: str = Field(min_length=1, max_length=80)
    allocation_description: str | None = Field(default=None, max_length=1000)
    allocation_amount: str = Field(min_length=1, max_length=50)
    allocation_count: int = Field(ge=1)
    invoice_reconciliation_status: Literal["reconciled", "total_mismatch"]
    notes: str | None = Field(default=None, max_length=2000)


class RecordMutation(BaseModel):
    tenant_id: str | None = None
    payload: dict[str, Any]
    actor: str = Field(default="local_operator", min_length=1, max_length=120)


@dataclass(frozen=True)
class _ParsedRecord:
    natural_key: str
    payload: dict[str, Any]
    source_row: int

    @property
    def row_hash(self) -> str:
        return hashlib.sha256(_json(self.payload).encode("utf-8")).hexdigest()


def stage_import(
    tenant_id: str,
    dataset: DatasetKind,
    original_filename: str,
    content: bytes,
) -> ImportPreview:
    tenant_id = validate_tenant_id(tenant_id)
    dataset = DatasetKind(dataset)
    filename = Path(original_filename or "").name
    if not filename or Path(filename).suffix.casefold() != ".csv":
        raise ValueError("A ResMan CSV file is required.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError(f"CSV exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit.")
    if not content:
        raise ValueError("CSV is empty.")

    import_id = "rmi_" + uuid.uuid4().hex[:16]
    raw_dir = _tenant_root(tenant_id) / "raw" / dataset.value
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / f"{import_id}_{_safe_filename(filename)}"
    raw_path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    created_at = _now()

    issues: list[ImportIssue] = []
    incoming: dict[str, str] = {}
    samples: list[dict[str, Any]] = []
    seen: set[str] = set()
    with _connect() as staging_db:
        staging_db.execute("DELETE FROM staged_rows WHERE import_id=?", (import_id,))
        insert_sql = """INSERT INTO staged_rows
            (import_id, tenant_id, dataset, natural_key, payload_json, row_hash, source_row)
            VALUES (?, ?, ?, ?, ?, ?, ?)"""
        batch: list[tuple[Any, ...]] = []
        try:
            for record in _iter_report(raw_path, dataset, issues):
                if record.natural_key in seen:
                    issues.append(ImportIssue(
                        code="duplicate_natural_key",
                        severity="warning",
                        message="A duplicate canonical record was merged or ignored.",
                        source_row=record.source_row,
                    ))
                    continue
                seen.add(record.natural_key)
                incoming[record.natural_key] = record.row_hash
                batch.append((
                    import_id, tenant_id, dataset.value, record.natural_key,
                    _json(record.payload), record.row_hash, record.source_row,
                ))
                if len(batch) >= 1000:
                    staging_db.executemany(insert_sql, batch)
                    batch.clear()
                if len(samples) < 5:
                    samples.append(record.payload)
            if batch:
                staging_db.executemany(insert_sql, batch)
        except (csv.Error, UnicodeError, ValueError) as exc:
            issues.append(ImportIssue(
                code="report_parse_failed", severity="error",
                message=f"The ResMan report could not be normalized: {type(exc).__name__}.",
            ))

    if not incoming:
        issues.append(ImportIssue(
            code="no_canonical_records", severity="error",
            message="No canonical records were found after the ResMan report header.",
        ))
    current = _current_snapshot_hashes(tenant_id, dataset)
    added = len(incoming.keys() - current.keys())
    removed = len(current.keys() - incoming.keys())
    shared = incoming.keys() & current.keys()
    changed = sum(incoming[key] != current[key] for key in shared)
    unchanged = len(shared) - changed
    excluded = _excluded_sensitive_columns(dataset)
    if excluded:
        issues.append(ImportIssue(
            code="sensitive_columns_excluded",
            severity="info",
            message="High-risk payment and tax identifiers remain only in the private raw snapshot.",
        ))
    status = "invalid" if any(issue.severity == "error" for issue in issues) else "preview_ready"
    preview = ImportPreview(
        import_id=import_id,
        tenant_id=tenant_id,
        dataset=dataset,
        original_filename=filename,
        sha256=digest,
        size_bytes=len(content),
        parsed_records=len(incoming),
        added_records=added,
        changed_records=changed,
        removed_records=removed,
        unchanged_records=unchanged,
        sample_records=samples,
        issues=issues[:100],
        excluded_sensitive_columns=excluded,
        status=status,
        created_at=created_at,
    )
    with _connect() as db:
        db.execute(
            """INSERT INTO imports
               (import_id, tenant_id, dataset, filename, sha256, size_bytes,
                raw_path, status, preview_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (import_id, tenant_id, dataset.value, filename, digest, len(content),
             str(raw_path), status, _json(preview.model_dump(mode="json")),
             created_at.isoformat()),
        )
    return preview


def publish_import(tenant_id: str, dataset: DatasetKind, import_id: str) -> DatasetSnapshot:
    tenant_id = validate_tenant_id(tenant_id)
    dataset = DatasetKind(dataset)
    with _LOCK, _connect() as db:
        row = db.execute(
            "SELECT * FROM imports WHERE import_id=? AND tenant_id=? AND dataset=?",
            (import_id, tenant_id, dataset.value),
        ).fetchone()
        if row is None:
            raise KeyError(import_id)
        preview = ImportPreview.model_validate_json(row["preview_json"])
        if preview.status != "preview_ready":
            raise ValueError("An invalid import cannot be published.")
        raw_path = Path(row["raw_path"])
        if not raw_path.is_file():
            raise FileNotFoundError("The private staged CSV is no longer available.")
        if _sha256_path(raw_path) != row["sha256"]:
            raise ValueError("The staged raw CSV changed after preview; upload it again.")

        snapshot_id = "rms_" + uuid.uuid4().hex[:16]
        now = _now()
        db.execute("BEGIN IMMEDIATE")
        count = db.execute(
            "SELECT COUNT(*) AS n FROM staged_rows WHERE import_id=? AND tenant_id=? AND dataset=?",
            (import_id, tenant_id, dataset.value),
        ).fetchone()["n"]
        if count == 0 or count != preview.parsed_records:
            raise ValueError("The staged canonical rows are incomplete; upload the report again.")
        db.execute(
            """INSERT INTO snapshot_rows
               (snapshot_id, tenant_id, dataset, natural_key, payload_json, row_hash, source_row)
               SELECT ?, tenant_id, dataset, natural_key, payload_json, row_hash, source_row
                 FROM staged_rows WHERE import_id=?""",
            (snapshot_id, import_id),
        )
        db.execute(
            """INSERT INTO snapshots
               (snapshot_id, import_id, tenant_id, dataset, filename, sha256,
                record_count, created_at, activated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (snapshot_id, import_id, tenant_id, dataset.value, row["filename"],
             row["sha256"], count, now.isoformat(), now.isoformat()),
        )
        db.execute(
            """INSERT INTO current_snapshots (tenant_id, dataset, snapshot_id)
               VALUES (?, ?, ?)
               ON CONFLICT(tenant_id, dataset) DO UPDATE SET snapshot_id=excluded.snapshot_id""",
            (tenant_id, dataset.value, snapshot_id),
        )
        db.execute(
            "UPDATE imports SET status='published', published_at=? WHERE import_id=?",
            (now.isoformat(), import_id),
        )
        db.execute("DELETE FROM staged_rows WHERE import_id=?", (import_id,))
        _rebuild_current_records(db, tenant_id, dataset, snapshot_id)
        _audit(db, tenant_id, dataset, "snapshot_published", "local_operator", {
            "snapshot_id": snapshot_id, "import_id": import_id,
            "sha256": row["sha256"], "record_count": count,
        })
    _invalidate_consumers(dataset)
    return get_snapshot(tenant_id, dataset, snapshot_id)


def activate_snapshot(
    tenant_id: str, dataset: DatasetKind, snapshot_id: str, *, actor: str,
) -> DatasetSnapshot:
    tenant_id = validate_tenant_id(tenant_id)
    dataset = DatasetKind(dataset)
    with _LOCK, _connect() as db:
        exists = db.execute(
            "SELECT 1 FROM snapshots WHERE snapshot_id=? AND tenant_id=? AND dataset=?",
            (snapshot_id, tenant_id, dataset.value),
        ).fetchone()
        if not exists:
            raise KeyError(snapshot_id)
        now = _now().isoformat()
        db.execute(
            """INSERT INTO current_snapshots (tenant_id, dataset, snapshot_id)
               VALUES (?, ?, ?)
               ON CONFLICT(tenant_id, dataset) DO UPDATE SET snapshot_id=excluded.snapshot_id""",
            (tenant_id, dataset.value, snapshot_id),
        )
        db.execute("UPDATE snapshots SET activated_at=? WHERE snapshot_id=?", (now, snapshot_id))
        _rebuild_current_records(db, tenant_id, dataset, snapshot_id)
        _audit(db, tenant_id, dataset, "snapshot_activated", actor, {"snapshot_id": snapshot_id})
    _invalidate_consumers(dataset)
    return get_snapshot(tenant_id, dataset, snapshot_id)


def list_snapshots(tenant_id: str, dataset: DatasetKind) -> list[DatasetSnapshot]:
    tenant_id = validate_tenant_id(tenant_id)
    dataset = DatasetKind(dataset)
    with _connect() as db:
        current = db.execute(
            "SELECT snapshot_id FROM current_snapshots WHERE tenant_id=? AND dataset=?",
            (tenant_id, dataset.value),
        ).fetchone()
        active_id = current["snapshot_id"] if current else None
        rows = db.execute(
            "SELECT * FROM snapshots WHERE tenant_id=? AND dataset=? ORDER BY created_at DESC",
            (tenant_id, dataset.value),
        ).fetchall()
    return [_snapshot_from_row(row, row["snapshot_id"] == active_id) for row in rows]


def get_snapshot(tenant_id: str, dataset: DatasetKind, snapshot_id: str) -> DatasetSnapshot:
    snapshots = list_snapshots(tenant_id, dataset)
    for item in snapshots:
        if item.snapshot_id == snapshot_id:
            return item
    raise KeyError(snapshot_id)


def dataset_status(tenant_id: str, dataset: DatasetKind) -> DatasetStatus:
    tenant_id = validate_tenant_id(tenant_id)
    dataset = DatasetKind(dataset)
    snapshots = list_snapshots(tenant_id, dataset)
    current = next((item for item in snapshots if item.active), None)
    with _connect() as db:
        effective = db.execute(
            "SELECT COUNT(*) AS n FROM current_records WHERE tenant_id=? AND dataset=? AND deleted=0",
            (tenant_id, dataset.value),
        ).fetchone()["n"]
        overlays = db.execute(
            "SELECT COUNT(*) AS n FROM record_overlays WHERE tenant_id=? AND dataset=?",
            (tenant_id, dataset.value),
        ).fetchone()["n"]
        staged = db.execute(
            "SELECT COUNT(*) AS n FROM imports WHERE tenant_id=? AND dataset=? AND status IN ('preview_ready','invalid')",
            (tenant_id, dataset.value),
        ).fetchone()["n"]
    return DatasetStatus(
        tenant_id=tenant_id, dataset=dataset, current_snapshot=current,
        effective_record_count=effective, manual_overlay_count=overlays,
        staged_import_count=staged,
    )


def all_statuses(tenant_id: str) -> list[DatasetStatus]:
    return [dataset_status(tenant_id, dataset) for dataset in DatasetKind]


def list_records(
    tenant_id: str,
    dataset: DatasetKind,
    *,
    page: int = 1,
    page_size: int = 50,
    search: str = "",
) -> RecordPage:
    tenant_id = validate_tenant_id(tenant_id)
    dataset = DatasetKind(dataset)
    page = max(1, int(page))
    page_size = max(1, min(250, int(page_size)))
    where = "tenant_id=? AND dataset=? AND deleted=0"
    params: list[Any] = [tenant_id, dataset.value]
    if search.strip():
        where += " AND payload_json LIKE ?"
        params.append("%" + search.strip() + "%")
    with _connect() as db:
        total = db.execute(f"SELECT COUNT(*) AS n FROM current_records WHERE {where}", params).fetchone()["n"]
        rows = db.execute(
            f"""SELECT natural_key, payload_json, source_kind, source_snapshot_id,
                       updated_at FROM current_records WHERE {where}
                  ORDER BY natural_key LIMIT ? OFFSET ?""",
            [*params, page_size, (page - 1) * page_size],
        ).fetchall()
        vendor_rows = []
        if dataset in {DatasetKind.GENERAL_LEDGER, DatasetKind.INVOICE_HISTORY}:
            vendor_rows = db.execute(
                """SELECT natural_key, payload_json, source_snapshot_id
                     FROM current_records
                    WHERE tenant_id=? AND dataset=? AND deleted=0""",
                (tenant_id, DatasetKind.VENDORS.value),
            ).fetchall()
        property_rows = []
        gl_rows = []
        ledger_rows = []
        invoice_history_rows = []
        invoice_history_available = False
        if dataset is DatasetKind.INVOICE_HISTORY:
            property_rows = db.execute(
                "SELECT payload_json FROM current_records WHERE tenant_id=? AND dataset=? AND deleted=0",
                (tenant_id, DatasetKind.PROPERTIES_UNITS.value),
            ).fetchall()
            gl_rows = db.execute(
                "SELECT payload_json FROM current_records WHERE tenant_id=? AND dataset=? AND deleted=0",
                (tenant_id, DatasetKind.GL_ACCOUNTS.value),
            ).fetchall()
            invoice_numbers = [json.loads(row["payload_json"]).get("invoice_number") for row in rows]
            invoice_numbers = sorted({_text(value) for value in invoice_numbers if _text(value)})
            if invoice_numbers:
                placeholders = ",".join("?" for _ in invoice_numbers)
                ledger_rows = db.execute(
                    f"""SELECT payload_json FROM current_records
                          WHERE tenant_id=? AND dataset=? AND deleted=0
                            AND json_extract(payload_json, '$.reference') IN ({placeholders})""",
                    [tenant_id, DatasetKind.GENERAL_LEDGER.value, *invoice_numbers],
                ).fetchall()
        elif dataset is DatasetKind.GENERAL_LEDGER:
            invoice_history_available = db.execute(
                "SELECT 1 FROM current_snapshots WHERE tenant_id=? AND dataset=?",
                (tenant_id, DatasetKind.INVOICE_HISTORY.value),
            ).fetchone() is not None
            references = [json.loads(row["payload_json"]).get("reference") for row in rows]
            references = sorted({_text(value) for value in references if _text(value)})
            if references:
                placeholders = ",".join("?" for _ in references)
                invoice_history_rows = db.execute(
                    f"""SELECT payload_json FROM current_records
                          WHERE tenant_id=? AND dataset=? AND deleted=0
                            AND json_extract(payload_json, '$.invoice_number') IN ({placeholders})""",
                    [tenant_id, DatasetKind.INVOICE_HISTORY.value, *references],
                ).fetchall()
    vendor_index, ambiguous_vendor_keys = _vendor_resolution_index(vendor_rows)
    items = []
    for row in rows:
        payload = json.loads(row["payload_json"])
        if dataset is DatasetKind.GENERAL_LEDGER:
            payload.update(_resolve_ledger_vendor(
                payload.get("counterparty_name"), vendor_index, ambiguous_vendor_keys,
            ))
            payload.update(_enrich_ledger_invoice_history(
                payload, invoice_history_rows, available=invoice_history_available,
            ))
        elif dataset is DatasetKind.INVOICE_HISTORY:
            payload.update(_enrich_invoice_history(
                payload, vendor_index, ambiguous_vendor_keys,
                property_rows, gl_rows, ledger_rows,
            ))
        payload["_record"] = {
            "natural_key": row["natural_key"],
            "source_kind": row["source_kind"],
            "source_snapshot_id": row["source_snapshot_id"],
            "updated_at": row["updated_at"],
        }
        items.append(payload)
    return RecordPage(
        tenant_id=tenant_id, dataset=dataset, page=page,
        page_size=page_size, total=total, items=items,
    )


def resolve_ledger_vendor(tenant_id: str, source_name: str | None) -> dict[str, Any]:
    """Resolve a raw ledger ``Name`` only against exact active vendor identity.

    ResMan's General Ledger report calls the field ``Name`` because it may be a
    vendor, tenant, bank, summary, or other counterparty.  This adapter keeps
    that source fact intact and returns a separate, non-authoritative identity
    resolution contract.
    """
    tenant_id = validate_tenant_id(tenant_id)
    with _connect() as db:
        vendor_rows = db.execute(
            """SELECT natural_key, payload_json, source_snapshot_id
                 FROM current_records
                WHERE tenant_id=? AND dataset=? AND deleted=0""",
            (tenant_id, DatasetKind.VENDORS.value),
        ).fetchall()
    index, ambiguous = _vendor_resolution_index(vendor_rows)
    return _resolve_ledger_vendor(source_name, index, ambiguous)


def list_all_effective_records(tenant_id: str, dataset: DatasetKind) -> list[dict[str, Any]]:
    tenant_id = validate_tenant_id(tenant_id)
    dataset = DatasetKind(dataset)
    with _connect() as db:
        rows = db.execute(
            "SELECT payload_json FROM current_records WHERE tenant_id=? AND dataset=? AND deleted=0",
            (tenant_id, dataset.value),
        ).fetchall()
    return [json.loads(row["payload_json"]) for row in rows]


def create_record(
    tenant_id: str, dataset: DatasetKind, payload: dict[str, Any], *, actor: str,
) -> dict[str, Any]:
    tenant_id = validate_tenant_id(tenant_id)
    dataset = DatasetKind(dataset)
    canonical = _validate_payload(dataset, payload)
    natural_key = _manual_natural_key(dataset, canonical)
    with _LOCK, _connect() as db:
        exists = db.execute(
            "SELECT 1 FROM current_records WHERE tenant_id=? AND dataset=? AND natural_key=? AND deleted=0",
            (tenant_id, dataset.value, natural_key),
        ).fetchone()
        if exists:
            raise ValueError("A record with the same canonical identity already exists.")
        _upsert_overlay(db, tenant_id, dataset, natural_key, "upsert", canonical, actor)
        _apply_overlay_to_current(db, tenant_id, dataset, natural_key, "upsert", canonical)
        _audit(db, tenant_id, dataset, "record_created", actor, {"natural_key": natural_key})
    _invalidate_consumers(dataset)
    return _record_response(tenant_id, dataset, natural_key)


def update_record(
    tenant_id: str,
    dataset: DatasetKind,
    natural_key: str,
    patch: dict[str, Any],
    *,
    actor: str,
) -> dict[str, Any]:
    tenant_id = validate_tenant_id(tenant_id)
    dataset = DatasetKind(dataset)
    existing = _get_current_payload(tenant_id, dataset, natural_key)
    if existing is None:
        raise KeyError(natural_key)
    merged = {**existing, **patch}
    canonical = _validate_payload(dataset, merged)
    with _LOCK, _connect() as db:
        prior = db.execute(
            "SELECT action, payload_json FROM record_overlays WHERE tenant_id=? AND dataset=? AND natural_key=?",
            (tenant_id, dataset.value, natural_key),
        ).fetchone()
        if prior and prior["action"] == "upsert":
            action = "upsert"
            overlay_payload: dict[str, Any] = canonical
        else:
            prior_payload = json.loads(prior["payload_json"]) if prior and prior["payload_json"] else {}
            prior_patch = prior_payload.get("patch") if isinstance(prior_payload, dict) else {}
            action = "patch"
            overlay_payload = {
                "patch": {**(prior_patch or {}), **patch},
                "fallback": canonical,
            }
        _upsert_overlay(db, tenant_id, dataset, natural_key, action, overlay_payload, actor)
        _apply_overlay_to_current(db, tenant_id, dataset, natural_key, action, overlay_payload)
        _audit(db, tenant_id, dataset, "record_updated", actor, {
            "natural_key": natural_key, "changed_fields": sorted(patch),
        })
    _invalidate_consumers(dataset)
    return _record_response(tenant_id, dataset, natural_key)


def delete_record(
    tenant_id: str, dataset: DatasetKind, natural_key: str, *, actor: str,
) -> dict[str, Any]:
    tenant_id = validate_tenant_id(tenant_id)
    dataset = DatasetKind(dataset)
    if _get_current_payload(tenant_id, dataset, natural_key) is None:
        raise KeyError(natural_key)
    with _LOCK, _connect() as db:
        _upsert_overlay(db, tenant_id, dataset, natural_key, "delete", None, actor)
        _apply_overlay_to_current(db, tenant_id, dataset, natural_key, "delete", None)
        _audit(db, tenant_id, dataset, "record_soft_deleted", actor, {"natural_key": natural_key})
    _invalidate_consumers(dataset)
    return {"deleted": True, "natural_key": natural_key, "audit_preserved": True}


def find_property_by_name(tenant_id: str, property_name: str) -> dict[str, Any] | None:
    key = _norm(property_name)
    if not key:
        return None
    rows = list_all_effective_records(tenant_id, DatasetKind.PROPERTIES_UNITS)
    exact = [row for row in rows if row.get("entity_type") == "property" and _norm(row.get("property_name")) == key]
    if len(exact) == 1:
        return exact[0]
    partial = [row for row in rows if row.get("entity_type") == "property" and (
        key in _norm(row.get("property_name")) or _norm(row.get("property_name")) in key
    )]
    return partial[0] if len(partial) == 1 else None


def find_vendor(tenant_id: str, observed_name: str) -> dict[str, Any] | None:
    key = _norm(observed_name)
    if not key:
        return None
    rows = list_all_effective_records(tenant_id, DatasetKind.VENDORS)
    matches = [row for row in rows if key in {
        _norm(row.get("company")), _norm(row.get("abbreviation")),
    }]
    return matches[0] if len(matches) == 1 else None


def current_snapshot_fingerprint(tenant_id: str, dataset: DatasetKind) -> str | None:
    snapshots = list_snapshots(tenant_id, dataset)
    current = next((item for item in snapshots if item.active), None)
    return current.sha256 if current else None


def _iter_report(path: Path, dataset: DatasetKind, issues: list[ImportIssue]) -> Iterator[_ParsedRecord]:
    if dataset is DatasetKind.VENDORS:
        yield from _parse_vendors(path, issues)
    elif dataset is DatasetKind.PROPERTIES_UNITS:
        yield from _parse_properties_units(path, issues)
    elif dataset is DatasetKind.GL_ACCOUNTS:
        yield from _parse_gl_accounts(path, issues)
    elif dataset is DatasetKind.GENERAL_LEDGER:
        yield from _parse_general_ledger(path, issues)
    elif dataset is DatasetKind.INVOICE_HISTORY:
        yield from _parse_invoice_history(path, issues)


def _parse_vendors(path: Path, issues: list[ImportIssue]) -> Iterator[_ParsedRecord]:
    rows = _read_all_rows(path)
    header_index = _find_header(rows, {"Company", "Company Abbreviation", "Status", "Active"})
    if header_index is None:
        raise ValueError("Vendor List header was not found")
    headers = [_clean_header(value) for value in rows[header_index][1]]
    merged: dict[str, tuple[dict[str, Any], int]] = {}
    for row_number, values in rows[header_index + 1:]:
        source = _row_dict(headers, values)
        company = _text(source.get("Company"))
        abbreviation = _text(source.get("Company Abbreviation"))
        if not company:
            continue
        natural = "vendor:" + _norm(abbreviation or company)
        insurance = {
            "type": _text(source.get("Insur Type")),
            "provider": _text(source.get("Insur Provider")),
            "policy_number": _text(source.get("Insur Policy #")),
            "coverage": _money(source.get("Insur Coverage")),
            "expiration": _date(source.get("Insur Expiration")),
        }
        prior = merged.get(natural)
        if prior:
            payload, first_row = prior
            if any(insurance.values()) and insurance not in payload["insurances"]:
                payload["insurances"].append(insurance)
            continue
        payload = VendorRecord(
            company=company,
            abbreviation=abbreviation,
            customer_number=_text(source.get("Customer #")),
            status=_text(source.get("Status")),
            active=_bool(source.get("Active"), default=True),
            general_contact=_text(source.get("General Contact")),
            general_address=_text(source.get("General Address")),
            general_city=_text(source.get("General City")),
            general_state=_text(source.get("General State")),
            general_zip=_text(source.get("General Zip")),
            general_phone=_text(source.get("General Work Phone")),
            general_email=_text(source.get("General Email")),
            workflow=_text(source.get("Workflow")),
            default_gl=_text(source.get("Default GL")),
            insurances=[insurance] if any(insurance.values()) else [],
        ).model_dump(mode="json")
        merged[natural] = (payload, row_number)
    for natural, (payload, row_number) in merged.items():
        yield _ParsedRecord(natural, payload, row_number)


def _parse_properties_units(path: Path, issues: list[ImportIssue]) -> Iterator[_ParsedRecord]:
    rows = _read_all_rows(path)
    property_codes = _legacy_property_codes()
    current_property: str | None = None
    emitted_properties: set[str] = set()
    headers: list[str] | None = None
    pending_section: str | None = None
    for row_number, values in rows:
        nonempty = [value.strip() for value in values if value.strip()]
        if len(nonempty) == 1:
            pending_section = nonempty[0]
            continue
        cleaned = [_clean_header(value) for value in values]
        if {"Unit", "Unit Type", "Unit Status"}.issubset(set(cleaned)):
            if not pending_section or pending_section.casefold() in {"all units"}:
                raise ValueError("A property section name is missing before the unit header")
            current_property = pending_section
            headers = cleaned
            property_code = property_codes.get(_norm(current_property))
            property_key = "property:" + _norm(current_property)
            if property_key not in emitted_properties:
                emitted_properties.add(property_key)
                yield _ParsedRecord(
                    property_key,
                    PropertyUnitRecord(
                        entity_type="property", property_name=current_property,
                        property_code=property_code,
                    ).model_dump(mode="json"),
                    row_number - 1,
                )
            continue
        if not current_property or not headers or not values or not _text(values[0]):
            continue
        source = _row_dict(headers, values)
        unit_number = _text(source.get("Unit"))
        if not unit_number:
            continue
        natural = f"unit:{_norm(current_property)}:{_norm(unit_number)}"
        payload = PropertyUnitRecord(
            entity_type="unit",
            property_name=current_property,
            property_code=property_codes.get(_norm(current_property)),
            unit_number=unit_number,
            unit_type=_text(source.get("Unit Type")),
            unit_status=_text(source.get("Unit Status")),
            square_feet=_number(source.get("Sq Ft")),
            lease_status=_text(source.get("Lease Status")),
            market_rent=_money(source.get("Market Rent")),
            active=_text(source.get("Unit Status")).casefold() not in {"inactive", "deleted"},
        ).model_dump(mode="json")
        yield _ParsedRecord(natural, payload, row_number)


def _parse_gl_accounts(path: Path, issues: list[ImportIssue]) -> Iterator[_ParsedRecord]:
    rows = _read_all_rows(path)
    header_index = _find_header(rows, {"Number", "Name", "Type", "Description"})
    if header_index is None:
        raise ValueError("Chart Of Accounts header was not found")
    headers = [_clean_header(value) for value in rows[header_index][1]]
    for row_number, values in rows[header_index + 1:]:
        source = _row_dict(headers, values)
        code = _text(source.get("Number"))
        name = _text(source.get("Name"))
        if not code or not name:
            continue
        account_type = _text(source.get("Type"))
        payable = "expense" in account_type.casefold() and "asset" not in account_type.casefold()
        payload = GLAccountRecord(
            gl_code=code, gl_name=name, account_type=account_type or None,
            description=_text(source.get("Description")) or None,
            payable=payable,
        ).model_dump(mode="json")
        yield _ParsedRecord("gl:" + _norm(code), payload, row_number)


def _parse_general_ledger(path: Path, issues: list[ImportIssue]) -> Iterator[_ParsedRecord]:
    rows = _read_all_rows(path)
    header_index = _find_header(rows, {"Date", "Reference", "Property", "Name", "Description", "Debit", "Credit", "Balance"})
    if header_index is None:
        raise ValueError("General Ledger transaction header was not found")
    headers = [_clean_header(value) for value in rows[header_index][1]]
    account_code: str | None = None
    account_name: str | None = None
    occurrences: dict[str, int] = {}
    for row_number, values in rows[header_index + 1:]:
        source = _row_dict(headers, values)
        raw_date = _text(source.get("Date"))
        transaction_date = _date(raw_date)
        if not transaction_date:
            if raw_date and "ending balance" not in raw_date.casefold():
                match = re.match(r"^([^\s]+)\s+(.+)$", raw_date)
                if match:
                    account_code, account_name = match.group(1).strip(), match.group(2).strip()
            continue
        payload = LedgerRecord(
            account_code=account_code,
            account_name=account_name,
            transaction_date=transaction_date,
            reference=_text(source.get("Reference")) or None,
            property_code=_text(source.get("Property")) or None,
            counterparty_name=_text(source.get("Name")) or None,
            description=_text(source.get("Description")) or None,
            debit=_money(source.get("Debit")),
            credit=_money(source.get("Credit")),
            balance=_money(source.get("Balance")),
        ).model_dump(mode="json")
        digest = hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()[:24]
        occurrence = occurrences.get(digest, 0) + 1
        occurrences[digest] = occurrence
        yield _ParsedRecord(f"ledger:{digest}:{occurrence}", payload, row_number)


def _parse_invoice_history(path: Path, issues: list[ImportIssue]) -> Iterator[_ParsedRecord]:
    rows = _read_all_rows(path)
    header_index = next((
        index for index, (_row_number, values) in enumerate(rows)
        if _clean_header(values[0] if values else "") == "Number"
        and len(values) > 1 and "Invoice Date" in _clean_header(values[1])
    ), None)
    if header_index is None:
        raise ValueError("Invoice Detail header was not found")

    vendor_name: str | None = None
    current: dict[str, Any] | None = None
    allocations: list[tuple[int, dict[str, Any]]] = []
    occurrences: dict[str, int] = {}

    def flush() -> Iterator[_ParsedRecord]:
        nonlocal current, allocations
        if current is None:
            return
        if not allocations:
            issues.append(ImportIssue(
                code="invoice_without_allocations", severity="error",
                message=f"Invoice {current['invoice_number']} has no allocation rows.",
                source_row=current["source_row"],
            ))
            current = None
            return
        allocation_sum = sum((Decimal(item["allocation_amount"]) for _row, item in allocations), Decimal("0"))
        invoice_total = Decimal(current["invoice_total"])
        status = "reconciled" if allocation_sum == invoice_total else "total_mismatch"
        if status == "total_mismatch":
            issues.append(ImportIssue(
                code="invoice_allocation_total_mismatch", severity="warning",
                message=(f"Invoice {current['invoice_number']} total {invoice_total:.2f} "
                         f"does not equal allocations {allocation_sum:.2f}."),
                source_row=current["source_row"],
            ))
        identity = _json({key: current.get(key) for key in (
            "vendor_name", "invoice_number", "invoice_date", "accounting_date", "invoice_total",
        )})
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
        occurrence = occurrences.get(digest, 0) + 1
        occurrences[digest] = occurrence
        occurrence_id = f"inv-{digest}-{occurrence}"
        count = len(allocations)
        for index, (source_row, allocation) in enumerate(allocations, start=1):
            payload = InvoiceHistoryRecord(
                **{key: value for key, value in current.items() if key != "source_row"},
                **allocation,
                invoice_occurrence_id=occurrence_id,
                allocation_index=index,
                allocation_count=count,
                invoice_reconciliation_status=status,
            ).model_dump(mode="json")
            yield _ParsedRecord(f"invoice:{digest}:{occurrence}:allocation:{index}", payload, source_row)
        current = None
        allocations = []

    for row_number, values in rows[header_index + 1:]:
        values = [*values, *([""] * max(0, 10 - len(values)))]
        nonempty_indexes = [index for index, value in enumerate(values[:10]) if _text(value)]
        if not nonempty_indexes:
            continue
        if _text(values[0]).casefold() == "total" and _money(values[7]) is not None:
            yield from flush()
            continue
        # A vendor section is a single human-provided label in the first column.
        if nonempty_indexes == [0] and not _date(values[1]):
            yield from flush()
            vendor_name = _text(values[0])
            continue
        invoice_date = _date(values[1])
        invoice_total = _money(values[7])
        if _text(values[0]) and invoice_date and invoice_total is not None:
            yield from flush()
            if not vendor_name:
                issues.append(ImportIssue(
                    code="invoice_vendor_section_missing", severity="error",
                    message="Invoice header appears before a vendor section.", source_row=row_number,
                ))
                continue
            current = {
                "source_row": row_number,
                "vendor_name": vendor_name,
                "invoice_number": _text(values[0]),
                "invoice_date": invoice_date,
                "accounting_date": _date(values[3]),
                "due_date": _date(values[4]),
                "invoice_description": _text(values[5]) or None,
                "invoice_total": invoice_total,
                "batch": _text(values[8]) or None,
                "po_number": _text(values[9]) or None,
            }
            allocations = []
            continue
        if current is not None:
            amount = _money(values[6])
            property_code = _text(values[1])
            gl_code = _text(values[2])
            if amount is not None and property_code and gl_code:
                allocations.append((row_number, {
                    "property_code": property_code,
                    "gl_code": gl_code,
                    "allocation_description": _text(values[4]) or None,
                    "allocation_amount": amount,
                }))
                continue
        issues.append(ImportIssue(
            code="unrecognized_invoice_detail_row", severity="warning",
            message="Row was preserved in the raw report but could not be classified.",
            source_row=row_number,
        ))
    yield from flush()


def _read_all_rows(path: Path) -> list[tuple[int, list[str]]]:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                return [(number, [str(value or "") for value in row])
                        for number, row in enumerate(csv.reader(handle), start=1)]
        except UnicodeDecodeError as exc:
            last_error = exc
    raise UnicodeError("CSV encoding is not supported") from last_error


def _find_header(rows: list[tuple[int, list[str]]], required: set[str]) -> int | None:
    for index, (_number, values) in enumerate(rows):
        headers = {_clean_header(value) for value in values}
        if required.issubset(headers):
            return index
    return None


def _row_dict(headers: list[str], values: list[str]) -> dict[str, str]:
    return {header: values[index].strip() if index < len(values) else ""
            for index, header in enumerate(headers) if header}


def _clean_header(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


def _excluded_sensitive_columns(dataset: DatasetKind) -> list[str]:
    if dataset is DatasetKind.VENDORS:
        return ["ACH Routing #", "ACH Account #", "Recipient ID", "1099 tax identifiers"]
    if dataset is DatasetKind.PROPERTIES_UNITS:
        return ["Residents", "Lease Start", "Lease End", "Deposits"]
    return []


def _legacy_property_codes() -> dict[str, str]:
    """Temporary adapter for the existing approved property directory.

    The raw All Units report does not include ResMan property abbreviations.
    Exact normalized name matches may carry the approved code forward; an
    ambiguous or missing match remains blank for manual review.
    """
    path = settings.RUNTIME_ASSET_ROOT / "Properties" / "Properties.csv"
    if not path.is_file():
        path = settings.PROJECT_ROOT / "Properties" / "Properties.csv"
    if not path.is_file():
        return {}
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                result: dict[str, str] = {}
                for row in csv.DictReader(handle):
                    name = _text(row.get("Property Name"))
                    code = _text(row.get("Property Abbreviation"))
                    if name and code:
                        result.setdefault(_norm(name), code)
                return result
        except UnicodeDecodeError:
            continue
    return {}


def _validate_payload(dataset: DatasetKind, payload: dict[str, Any]) -> dict[str, Any]:
    models = {
        DatasetKind.VENDORS: VendorRecord,
        DatasetKind.PROPERTIES_UNITS: PropertyUnitRecord,
        DatasetKind.GL_ACCOUNTS: GLAccountRecord,
        DatasetKind.GENERAL_LEDGER: LedgerRecord,
        DatasetKind.INVOICE_HISTORY: InvoiceHistoryRecord,
    }
    return models[dataset].model_validate(payload).model_dump(mode="json")


def _vendor_resolution_index(
    rows: Iterable[sqlite3.Row],
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    index: dict[str, dict[str, Any]] = {}
    ambiguous: set[str] = set()
    for row in rows:
        payload = json.loads(row["payload_json"])
        if not bool(payload.get("active", True)):
            continue
        candidate = {
            "vendor_name": _text(payload.get("company")),
            "vendor_abbreviation": _text(payload.get("abbreviation")) or None,
            "vendor_key": row["natural_key"],
            "vendor_snapshot_id": row["source_snapshot_id"],
        }
        for matched_field in ("company", "abbreviation"):
            key = _norm(payload.get(matched_field))
            if not key:
                continue
            prior = index.get(key)
            if prior and prior["vendor_key"] != candidate["vendor_key"]:
                ambiguous.add(key)
                index.pop(key, None)
                continue
            if key not in ambiguous:
                index[key] = {**candidate, "matched_field": matched_field}
    return index, ambiguous


def _resolve_ledger_vendor(
    source_name: str | None,
    index: dict[str, dict[str, Any]],
    ambiguous: set[str],
) -> dict[str, Any]:
    observed = _text(source_name)
    key = _norm(observed)
    base = {
        "resolved_vendor_name": None,
        "resolved_vendor_abbreviation": None,
        "resolved_vendor_key": None,
        "vendor_resolution_source": "resman_vendor_master_exact",
        "vendor_resolution_evidence": [],
    }
    if not key:
        return {**base, "vendor_resolution_status": VendorResolutionStatus.MISSING_SOURCE_NAME.value}
    if key in ambiguous:
        return {
            **base,
            "vendor_resolution_status": VendorResolutionStatus.AMBIGUOUS.value,
            "vendor_resolution_evidence": [{
                "observed_name": observed,
                "match_type": "exact_normalized_but_non_unique",
                "authoritative": False,
            }],
        }
    match = index.get(key)
    if not match:
        return {
            **base,
            "vendor_resolution_status": VendorResolutionStatus.UNRESOLVED.value,
            "vendor_resolution_evidence": [{
                "observed_name": observed,
                "match_type": "no_exact_vendor_master_match",
                "authoritative": False,
            }],
        }
    return {
        **base,
        "resolved_vendor_name": match["vendor_name"],
        "resolved_vendor_abbreviation": match["vendor_abbreviation"],
        "resolved_vendor_key": match["vendor_key"],
        "vendor_resolution_status": VendorResolutionStatus.EXACT.value,
        "vendor_resolution_evidence": [{
            "observed_name": observed,
            "matched_field": match["matched_field"],
            "vendor_snapshot_id": match["vendor_snapshot_id"],
            "match_type": "exact_normalized",
            "authoritative": "published_resman_vendor_master",
        }],
    }


def _enrich_invoice_history(
    payload: dict[str, Any],
    vendor_index: dict[str, dict[str, Any]],
    ambiguous_vendor_keys: set[str],
    property_rows: Iterable[sqlite3.Row],
    gl_rows: Iterable[sqlite3.Row],
    ledger_rows: Iterable[sqlite3.Row],
) -> dict[str, Any]:
    """Add validation and ledger evidence without changing source facts."""
    vendor = _resolve_ledger_vendor(
        payload.get("vendor_name"), vendor_index, ambiguous_vendor_keys,
    )
    properties = {
        _norm(item.get("property_code"))
        for row in property_rows for item in [json.loads(row["payload_json"])]
        if item.get("property_code")
    }
    gl_catalog = {
        _norm(item.get("gl_code")): item
        for row in gl_rows for item in [json.loads(row["payload_json"])]
        if item.get("gl_code")
    }
    property_valid = _norm(payload.get("property_code")) in properties
    gl = gl_catalog.get(_norm(payload.get("gl_code")))
    gl_valid = bool(gl and gl.get("active", True))
    gl_payable = bool(gl_valid and gl.get("payable"))
    ledger_payloads = [json.loads(row["payload_json"]) for row in ledger_rows]
    status, evidence = _reconcile_invoice_allocation(payload, ledger_payloads)
    return {
        "vendor_validation_status": vendor["vendor_resolution_status"],
        "resolved_vendor_key": vendor["resolved_vendor_key"],
        "property_valid": property_valid,
        "gl_valid": gl_valid,
        "gl_payable": gl_payable,
        "ledger_reconciliation_status": status.value,
        "ledger_reconciliation_evidence": evidence,
        "reference_validation_evidence": {
            "vendor": vendor["vendor_resolution_evidence"],
            "property": {"observed": payload.get("property_code"), "exact_master_match": property_valid},
            "gl": {"observed": payload.get("gl_code"), "exact_chart_match": gl_valid, "payable": gl_payable},
            "authoritative_for_gl_selection": False,
        },
    }


def _reconcile_invoice_allocation(
    invoice: dict[str, Any], ledger_rows: Iterable[dict[str, Any]],
) -> tuple[ReconciliationStatus, list[dict[str, Any]]]:
    vendor_key = _norm(invoice.get("vendor_name"))
    reference = _text(invoice.get("invoice_number"))
    candidates = [row for row in ledger_rows if (
        _norm(row.get("counterparty_name")) == vendor_key
        and _text(row.get("reference")) == reference
    )]
    if not candidates:
        return ReconciliationStatus.INVOICE_ONLY, [{
            "match_type": "no_exact_vendor_and_reference_match",
            "authoritative": False,
        }]
    expected_property = _norm(invoice.get("property_code"))
    expected_gl = _norm(invoice.get("gl_code"))
    expected_amount = _signed_money(invoice.get("allocation_amount"))
    expected_date = invoice.get("accounting_date")

    def matches(row: dict[str, Any], *, prop=True, gl=True, amount=True, date=True) -> bool:
        return (
            (not prop or _norm(row.get("property_code")) == expected_property)
            and (not gl or _norm(row.get("account_code")) == expected_gl)
            and (not amount or _ledger_signed_amount(row) == expected_amount)
            and (not date or row.get("transaction_date") == expected_date)
        )

    exact = next((row for row in candidates if matches(row)), None)
    if exact:
        status, matched = ReconciliationStatus.MATCHED_TO_LEDGER, exact
    else:
        date_only = next((row for row in candidates if matches(row, date=False)), None)
        amount_only = next((row for row in candidates if matches(row, amount=False)), None)
        gl_only = next((row for row in candidates if matches(row, gl=False)), None)
        property_only = next((row for row in candidates if matches(row, prop=False)), None)
        if date_only:
            status, matched = ReconciliationStatus.POSTING_DATE_DIFFERENCE, date_only
        elif amount_only:
            status, matched = ReconciliationStatus.AMOUNT_MISMATCH, amount_only
        elif gl_only:
            status, matched = ReconciliationStatus.GL_MISMATCH, gl_only
        elif property_only:
            status, matched = ReconciliationStatus.PROPERTY_MISMATCH, property_only
        else:
            return ReconciliationStatus.INVOICE_ONLY, [{
                "match_type": "vendor_and_reference_only",
                "candidate_count": len(candidates),
                "authoritative": False,
            }]
    return status, [{
        "match_type": status.value,
        "ledger_date": matched.get("transaction_date"),
        "ledger_property": matched.get("property_code"),
        "ledger_gl": matched.get("account_code"),
        "ledger_amount": str(_ledger_signed_amount(matched)),
        "authoritative": "published_general_ledger",
    }]


def _enrich_ledger_invoice_history(
    ledger: dict[str, Any], invoice_rows: Iterable[sqlite3.Row], *, available: bool,
) -> dict[str, Any]:
    if not available:
        return {
            "invoice_history_reconciliation_status": ReconciliationStatus.INVOICE_HISTORY_UNAVAILABLE.value,
            "invoice_history_reconciliation_evidence": [],
        }
    candidates = [json.loads(row["payload_json"]) for row in invoice_rows]
    candidates = [row for row in candidates if (
        _norm(row.get("vendor_name")) == _norm(ledger.get("counterparty_name"))
        and _text(row.get("invoice_number")) == _text(ledger.get("reference"))
    )]
    for invoice in candidates:
        status, evidence = _reconcile_invoice_allocation(invoice, [ledger])
        if status is ReconciliationStatus.MATCHED_TO_LEDGER:
            return {
                "invoice_history_reconciliation_status": ReconciliationStatus.MATCHED_TO_INVOICE_HISTORY.value,
                "invoice_history_reconciliation_evidence": evidence,
            }
    return {
        "invoice_history_reconciliation_status": ReconciliationStatus.LEDGER_ONLY.value,
        "invoice_history_reconciliation_evidence": [{
            "match_type": "no_exact_invoice_allocation_match",
            "candidate_count": len(candidates),
            "authoritative": False,
        }],
    }


def _signed_money(value: Any) -> Decimal | None:
    normalized = _money(value)
    return Decimal(normalized) if normalized is not None else None


def _ledger_signed_amount(row: dict[str, Any]) -> Decimal | None:
    debit = _signed_money(row.get("debit"))
    credit = _signed_money(row.get("credit"))
    if debit not in {None, Decimal("0.00")}:
        return debit
    if credit is not None:
        return -credit
    return None


def _manual_natural_key(dataset: DatasetKind, payload: dict[str, Any]) -> str:
    if dataset is DatasetKind.VENDORS:
        identity = payload.get("abbreviation") or payload.get("company")
        prefix = "vendor"
    elif dataset is DatasetKind.PROPERTIES_UNITS:
        identity = payload.get("property_name")
        if payload.get("entity_type") == "unit":
            identity = f"{identity}:{payload.get('unit_number')}"
            prefix = "unit"
        else:
            prefix = "property"
    elif dataset is DatasetKind.GL_ACCOUNTS:
        identity, prefix = payload.get("gl_code"), "gl"
    elif dataset is DatasetKind.INVOICE_HISTORY:
        identity = uuid.uuid4().hex
        prefix = "invoice-manual"
    else:
        identity, prefix = uuid.uuid4().hex, "ledger-manual"
    return f"{prefix}:{_norm(identity)}"


def _get_current_payload(tenant_id: str, dataset: DatasetKind, natural_key: str) -> dict[str, Any] | None:
    with _connect() as db:
        row = db.execute(
            "SELECT payload_json FROM current_records WHERE tenant_id=? AND dataset=? AND natural_key=? AND deleted=0",
            (tenant_id, dataset.value, natural_key),
        ).fetchone()
    return json.loads(row["payload_json"]) if row else None


def _record_response(tenant_id: str, dataset: DatasetKind, natural_key: str) -> dict[str, Any]:
    payload = _get_current_payload(tenant_id, dataset, natural_key)
    if payload is None:
        raise KeyError(natural_key)
    payload["_record"] = {"natural_key": natural_key, "source_kind": "manual_overlay"}
    return payload


def _upsert_overlay(
    db: sqlite3.Connection,
    tenant_id: str,
    dataset: DatasetKind,
    natural_key: str,
    action: str,
    payload: dict[str, Any] | None,
    actor: str,
) -> None:
    now = _now().isoformat()
    db.execute(
        """INSERT INTO record_overlays
           (tenant_id, dataset, natural_key, action, payload_json, actor, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(tenant_id, dataset, natural_key) DO UPDATE SET
             action=excluded.action, payload_json=excluded.payload_json,
             actor=excluded.actor, updated_at=excluded.updated_at""",
        (tenant_id, dataset.value, natural_key, action,
         _json(payload) if payload is not None else None, actor, now, now),
    )


def _apply_overlay_to_current(
    db: sqlite3.Connection,
    tenant_id: str,
    dataset: DatasetKind,
    natural_key: str,
    action: str,
    payload: dict[str, Any] | None,
) -> None:
    now = _now().isoformat()
    if action == "delete":
        db.execute(
            "UPDATE current_records SET deleted=1, source_kind='manual_overlay', updated_at=? WHERE tenant_id=? AND dataset=? AND natural_key=?",
            (now, tenant_id, dataset.value, natural_key),
        )
        return
    assert payload is not None
    effective_payload = payload
    if action == "patch":
        current = db.execute(
            "SELECT payload_json FROM current_records WHERE tenant_id=? AND dataset=? AND natural_key=? AND deleted=0",
            (tenant_id, dataset.value, natural_key),
        ).fetchone()
        base = json.loads(current["payload_json"]) if current else payload.get("fallback", {})
        effective_payload = {**base, **(payload.get("patch") or {})}
        effective_payload = _validate_payload(dataset, effective_payload)
    row_hash = hashlib.sha256(_json(effective_payload).encode("utf-8")).hexdigest()
    db.execute(
        """INSERT INTO current_records
           (tenant_id, dataset, natural_key, payload_json, row_hash, source_kind,
            source_snapshot_id, deleted, updated_at)
           VALUES (?, ?, ?, ?, ?, 'manual_overlay', NULL, 0, ?)
           ON CONFLICT(tenant_id, dataset, natural_key) DO UPDATE SET
             payload_json=excluded.payload_json, row_hash=excluded.row_hash,
             source_kind='manual_overlay', deleted=0, updated_at=excluded.updated_at""",
        (tenant_id, dataset.value, natural_key, _json(effective_payload), row_hash, now),
    )


def _rebuild_current_records(
    db: sqlite3.Connection, tenant_id: str, dataset: DatasetKind, snapshot_id: str,
) -> None:
    db.execute("DELETE FROM current_records WHERE tenant_id=? AND dataset=?", (tenant_id, dataset.value))
    now = _now().isoformat()
    db.execute(
        """INSERT INTO current_records
           (tenant_id, dataset, natural_key, payload_json, row_hash, source_kind,
            source_snapshot_id, deleted, updated_at)
           SELECT tenant_id, dataset, natural_key, payload_json, row_hash,
                  'resman_import', snapshot_id, 0, ?
             FROM snapshot_rows WHERE snapshot_id=?""",
        (now, snapshot_id),
    )
    overlays = db.execute(
        "SELECT * FROM record_overlays WHERE tenant_id=? AND dataset=?",
        (tenant_id, dataset.value),
    ).fetchall()
    for overlay in overlays:
        payload = json.loads(overlay["payload_json"]) if overlay["payload_json"] else None
        _apply_overlay_to_current(
            db, tenant_id, dataset, overlay["natural_key"], overlay["action"], payload,
        )


def _current_snapshot_hashes(tenant_id: str, dataset: DatasetKind) -> dict[str, str]:
    with _connect() as db:
        current = db.execute(
            "SELECT snapshot_id FROM current_snapshots WHERE tenant_id=? AND dataset=?",
            (tenant_id, dataset.value),
        ).fetchone()
        if not current:
            return {}
        rows = db.execute(
            "SELECT natural_key, row_hash FROM snapshot_rows WHERE snapshot_id=?",
            (current["snapshot_id"],),
        ).fetchall()
    return {row["natural_key"]: row["row_hash"] for row in rows}


def _snapshot_from_row(row: sqlite3.Row, active: bool) -> DatasetSnapshot:
    return DatasetSnapshot(
        snapshot_id=row["snapshot_id"], import_id=row["import_id"],
        tenant_id=row["tenant_id"], dataset=DatasetKind(row["dataset"]),
        original_filename=row["filename"], sha256=row["sha256"],
        record_count=row["record_count"], created_at=row["created_at"],
        activated_at=row["activated_at"], active=active,
    )


def _audit(
    db: sqlite3.Connection,
    tenant_id: str,
    dataset: DatasetKind,
    event: str,
    actor: str,
    details: dict[str, Any],
) -> None:
    db.execute(
        "INSERT INTO audit_events (event_id, tenant_id, dataset, event, actor, details_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("rae_" + uuid.uuid4().hex[:16], tenant_id, dataset.value, event,
         actor, _json(details), _now().isoformat()),
    )


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    path = settings.WEBAPP_DATA_ROOT / "resman_context" / "resman_context.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=60)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA foreign_keys=ON")
        _ensure_schema(db)
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS imports (
          import_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, dataset TEXT NOT NULL,
          filename TEXT NOT NULL, sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
          raw_path TEXT NOT NULL, status TEXT NOT NULL, preview_json TEXT NOT NULL,
          created_at TEXT NOT NULL, published_at TEXT
        );
        CREATE TABLE IF NOT EXISTS snapshots (
          snapshot_id TEXT PRIMARY KEY, import_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
          dataset TEXT NOT NULL, filename TEXT NOT NULL, sha256 TEXT NOT NULL,
          record_count INTEGER NOT NULL, created_at TEXT NOT NULL, activated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_snapshots_tenant_dataset ON snapshots(tenant_id, dataset);
        CREATE TABLE IF NOT EXISTS snapshot_rows (
          snapshot_id TEXT NOT NULL, tenant_id TEXT NOT NULL, dataset TEXT NOT NULL,
          natural_key TEXT NOT NULL, payload_json TEXT NOT NULL, row_hash TEXT NOT NULL,
          source_row INTEGER NOT NULL,
          PRIMARY KEY(snapshot_id, natural_key)
        );
        CREATE INDEX IF NOT EXISTS idx_snapshot_rows_snapshot ON snapshot_rows(snapshot_id);
        CREATE TABLE IF NOT EXISTS staged_rows (
          import_id TEXT NOT NULL, tenant_id TEXT NOT NULL, dataset TEXT NOT NULL,
          natural_key TEXT NOT NULL, payload_json TEXT NOT NULL, row_hash TEXT NOT NULL,
          source_row INTEGER NOT NULL,
          PRIMARY KEY(import_id, natural_key)
        );
        CREATE INDEX IF NOT EXISTS idx_staged_rows_import ON staged_rows(import_id);
        CREATE TABLE IF NOT EXISTS current_snapshots (
          tenant_id TEXT NOT NULL, dataset TEXT NOT NULL, snapshot_id TEXT NOT NULL,
          PRIMARY KEY(tenant_id, dataset)
        );
        CREATE TABLE IF NOT EXISTS record_overlays (
          tenant_id TEXT NOT NULL, dataset TEXT NOT NULL, natural_key TEXT NOT NULL,
          action TEXT NOT NULL, payload_json TEXT, actor TEXT NOT NULL,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          PRIMARY KEY(tenant_id, dataset, natural_key)
        );
        CREATE TABLE IF NOT EXISTS current_records (
          tenant_id TEXT NOT NULL, dataset TEXT NOT NULL, natural_key TEXT NOT NULL,
          payload_json TEXT NOT NULL, row_hash TEXT NOT NULL, source_kind TEXT NOT NULL,
          source_snapshot_id TEXT, deleted INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
          PRIMARY KEY(tenant_id, dataset, natural_key)
        );
        CREATE INDEX IF NOT EXISTS idx_current_records_page
          ON current_records(tenant_id, dataset, deleted, natural_key);
        CREATE TABLE IF NOT EXISTS audit_events (
          event_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, dataset TEXT NOT NULL,
          event TEXT NOT NULL, actor TEXT NOT NULL, details_json TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        """
    )


def _tenant_root(tenant_id: str) -> Path:
    return settings.WEBAPP_DATA_ROOT / "resman_context" / validate_tenant_id(tenant_id)


def _invalidate_consumers(dataset: DatasetKind) -> None:
    try:
        from .context_intelligence import invalidate_candidate_cache
        invalidate_candidate_cache()
    except Exception:
        pass
    if dataset is DatasetKind.GL_ACCOUNTS:
        try:
            from .gl_catalog import load_gl_catalog
            load_gl_catalog.cache_clear()
        except Exception:
            pass
    if dataset is DatasetKind.PROPERTIES_UNITS:
        try:
            from utils import property_lookup
            property_lookup._PROPERTY_BY_NAME = None
            property_lookup._INDEX_BY_PROP_UNIT = None
        except Exception:
            pass
    if dataset is DatasetKind.VENDORS:
        try:
            from utils import canonical_vendors
            canonical_vendors._CACHE = None
        except Exception:
            pass


def _safe_filename(value: str) -> str:
    name = Path(value).name
    return re.sub(r"[^A-Za-z0-9._ -]+", "_", name)[:180] or "resman-report.csv"


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _norm(value: Any) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").casefold()).strip("-")
    return text[:180]


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


def _bool(value: Any, *, default: bool) -> bool:
    text = _text(value).casefold()
    if text in {"yes", "true", "1", "active", "approved"}:
        return True
    if text in {"no", "false", "0", "inactive", "disabled"}:
        return False
    return default


def _number(value: Any) -> str | None:
    text = _text(value).replace(",", "")
    if not text:
        return None
    try:
        return format(Decimal(text), "f")
    except InvalidOperation:
        return text


def _money(value: Any) -> str | None:
    text = _text(value).replace("$", "").replace(",", "").replace("(", "-").replace(")", "")
    if not text:
        return None
    try:
        return format(Decimal(text).quantize(Decimal("0.01")), "f")
    except InvalidOperation:
        return None


def _date(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    for pattern in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            continue
    return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "CONTRACT_VERSION", "DatasetKind", "DatasetSnapshot", "DatasetStatus",
    "ImportPreview", "RecordMutation", "RecordPage", "activate_snapshot",
    "all_statuses", "create_record", "current_snapshot_fingerprint",
    "dataset_status", "delete_record", "find_property_by_name", "find_vendor",
    "get_snapshot", "list_all_effective_records", "list_records", "list_snapshots",
    "publish_import", "resolve_ledger_vendor", "stage_import", "update_record",
]
