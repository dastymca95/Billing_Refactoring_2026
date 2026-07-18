"""Cross-dataset onboarding intelligence built from published ResMan evidence.

Statistics are deterministic and versioned. Recommendations are proposals for
human review: they never activate rules, select a GL, or authorize export.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .. import settings
from . import resman_context_data as hub
from . import deterministic_coverage
from .deterministic_coverage import DeterministicCoverage
from .tenant_accounting_policies import validate_tenant_id


CONTRACT_VERSION = "context-intelligence/1.1"
ANALYTICS_VERSION = "vendor-property-gl-matrix/1.0"
REQUIRED_DATASETS = tuple(hub.DatasetKind)
_LOCK = threading.RLock()


class FrequencyItem(BaseModel):
    key: str
    label: str
    count: int = Field(ge=0)
    amount: str
    share: float = Field(ge=0, le=1)


class VendorContextProfile(BaseModel):
    vendor_key: str
    vendor_name: str
    vendor_abbreviation: str | None = None
    active: bool = True
    invoice_count: int = 0
    allocation_count: int = 0
    ledger_posting_count: int = 0
    ledger_total_amount: str = "0.00"
    active_months: int = 0
    history_span_months: int = 0
    total_amount: str = "0.00"
    average_invoice_amount: str = "0.00"
    top_gl_share: float = 0
    top_property_share: float = 0
    gl_usage: list[FrequencyItem] = Field(default_factory=list)
    property_usage: list[FrequencyItem] = Field(default_factory=list)
    property_gl_usage: dict[str, list[FrequencyItem]] = Field(default_factory=dict)
    first_accounting_date: str | None = None
    last_accounting_date: str | None = None
    statistical_score: float = 0
    recommended_mode: Literal[
        "deterministic_candidate", "review_candidate", "variable", "insufficient_history"
    ] = "insufficient_history"
    recommendation_reasons: list[str] = Field(default_factory=list)
    governance_status: Literal["unreviewed", "approved_candidate", "excluded", "needs_review"] = "unreviewed"
    reviewer_notes: str | None = None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    deterministic_coverage: DeterministicCoverage | None = None


class PropertyContextProfile(BaseModel):
    property_key: str
    property_name: str
    property_code: str | None = None
    invoice_count: int = 0
    allocation_count: int = 0
    ledger_posting_count: int = 0
    total_amount: str = "0.00"
    gl_usage: list[FrequencyItem] = Field(default_factory=list)
    vendor_usage: list[FrequencyItem] = Field(default_factory=list)


class ContextIntelligenceSnapshot(BaseModel):
    contract_version: str = CONTRACT_VERSION
    analytics_version: str = ANALYTICS_VERSION
    snapshot_id: str
    tenant_id: str
    generated_at: datetime
    generated_by: str
    source_hashes: dict[str, str]
    vendor_count: int
    property_count: int
    invoice_count: int
    allocation_count: int
    gl_account_count: int
    ledger_record_count: int
    deterministic_candidate_count: int
    review_candidate_count: int
    vendors: list[VendorContextProfile]
    properties: list[PropertyContextProfile]
    audit: list[dict[str, Any]] = Field(default_factory=list)


class MatrixPage(BaseModel):
    contract_version: str = CONTRACT_VERSION
    snapshot_id: str
    tenant_id: str
    page: int
    page_size: int
    total: int
    items: list[dict[str, Any]]


class GovernanceUpdate(BaseModel):
    governance_status: Literal["unreviewed", "approved_candidate", "excluded", "needs_review"]
    reviewer_notes: str | None = Field(default=None, max_length=4000)
    actor: str = Field(default="local_operator", min_length=1, max_length=120)


def status(tenant_id: str) -> dict[str, Any]:
    tenant_id = validate_tenant_id(tenant_id)
    report = _read_report(tenant_id)
    current_hashes, missing = _source_hashes(tenant_id)
    if report is None:
        return {
            "contract_version": CONTRACT_VERSION,
            "tenant_id": tenant_id,
            "state": "not_generated",
            "required_datasets": [item.value for item in REQUIRED_DATASETS],
            "missing_datasets": missing,
            "current_source_hashes": current_hashes,
            "snapshot": None,
        }
    stale = report.source_hashes != current_hashes or bool(missing)
    return {
        "contract_version": CONTRACT_VERSION,
        "tenant_id": tenant_id,
        "state": "stale" if stale else "ready",
        "required_datasets": [item.value for item in REQUIRED_DATASETS],
        "missing_datasets": missing,
        "current_source_hashes": current_hashes,
        "snapshot": _snapshot_summary(report),
    }


def scan_resman(tenant_id: str, *, actor: str = "local_operator") -> ContextIntelligenceSnapshot:
    tenant_id = validate_tenant_id(tenant_id)
    source_hashes, missing = _source_hashes(tenant_id)
    if missing:
        raise ValueError("Cannot scan ResMan context; missing published datasets: " + ", ".join(missing))

    vendors = hub.list_all_effective_records(tenant_id, hub.DatasetKind.VENDORS)
    properties = hub.list_all_effective_records(tenant_id, hub.DatasetKind.PROPERTIES_UNITS)
    gl_accounts = hub.list_all_effective_records(tenant_id, hub.DatasetKind.GL_ACCOUNTS)
    ledger = hub.list_all_effective_records(tenant_id, hub.DatasetKind.GENERAL_LEDGER)
    invoices = hub.list_all_effective_records(tenant_id, hub.DatasetKind.INVOICE_HISTORY)

    prior = _read_report(tenant_id)
    prior_overrides = {
        item.vendor_key: item for item in (prior.vendors if prior else [])
        if item.governance_status != "unreviewed" or item.reviewer_notes
    }
    gl_labels = {str(item.get("gl_code")): str(item.get("gl_name") or item.get("gl_code")) for item in gl_accounts}
    vendor_profiles = _build_vendor_profiles(vendors, invoices, ledger, gl_labels, prior_overrides)
    property_profiles = _build_property_profiles(properties, invoices, ledger, gl_labels)
    report = ContextIntelligenceSnapshot(
        snapshot_id="cis_" + uuid.uuid4().hex[:16],
        tenant_id=tenant_id,
        generated_at=_now(),
        generated_by=actor,
        source_hashes=source_hashes,
        vendor_count=len(vendor_profiles),
        property_count=len(property_profiles),
        invoice_count=len({item.get("invoice_occurrence_id") for item in invoices}),
        allocation_count=len(invoices),
        gl_account_count=len(gl_accounts),
        ledger_record_count=len(ledger),
        deterministic_candidate_count=sum(item.recommended_mode == "deterministic_candidate" for item in vendor_profiles),
        review_candidate_count=sum(item.recommended_mode == "review_candidate" for item in vendor_profiles),
        vendors=vendor_profiles,
        properties=property_profiles,
        audit=[*(prior.audit if prior else []), {
            "event": "resman_context_scanned",
            "actor": actor,
            "at": _now().isoformat(),
            "source_hashes": source_hashes,
            "rules_activated": False,
        }],
    )
    with _LOCK:
        _write_report(report)
        invalidate_candidate_cache()
    return report


def list_matrix(
    tenant_id: str, *, dimension: Literal["vendors", "properties"] = "vendors",
    page: int = 1, page_size: int = 50, search: str = "", mode: str = "",
) -> MatrixPage:
    report = _require_report(tenant_id)
    source = report.vendors if dimension == "vendors" else report.properties
    needle = search.strip().casefold()
    items = [item for item in source if not needle or needle in json.dumps(
        item.model_dump(mode="json"), ensure_ascii=False,
    ).casefold()]
    if dimension == "vendors" and mode:
        items = [item for item in items if item.recommended_mode == mode]
    if dimension == "vendors":
        items.sort(key=lambda item: (-item.statistical_score, -item.invoice_count, item.vendor_name.casefold()))
    else:
        items.sort(key=lambda item: (-item.allocation_count, item.property_name.casefold()))
    page = max(1, page)
    page_size = max(1, min(250, page_size))
    start = (page - 1) * page_size
    return MatrixPage(
        snapshot_id=report.snapshot_id, tenant_id=report.tenant_id,
        page=page, page_size=page_size, total=len(items),
        items=[item.model_dump(mode="json") for item in items[start:start + page_size]],
    )


def vendor_detail(tenant_id: str, vendor_key: str) -> VendorContextProfile:
    report = _require_report(tenant_id)
    match = next((item for item in report.vendors if item.vendor_key == vendor_key), None)
    if match is None:
        raise KeyError(vendor_key)
    return match


def property_detail(tenant_id: str, property_key: str) -> PropertyContextProfile:
    report = _require_report(tenant_id)
    match = next((item for item in report.properties if item.property_key == property_key), None)
    if match is None:
        raise KeyError(property_key)
    return match


def update_vendor_governance(
    tenant_id: str, vendor_key: str, update: GovernanceUpdate,
) -> VendorContextProfile:
    with _LOCK:
        report = _require_report(tenant_id)
        position = next((index for index, item in enumerate(report.vendors) if item.vendor_key == vendor_key), None)
        if position is None:
            raise KeyError(vendor_key)
        current = report.vendors[position]
        changed = current.model_copy(update={
            "governance_status": update.governance_status,
            "reviewer_notes": (update.reviewer_notes or "").strip() or None,
            "reviewed_by": update.actor,
            "reviewed_at": _now(),
        })
        report.vendors[position] = changed
        report.audit.append({
            "event": "vendor_governance_updated", "actor": update.actor,
            "at": _now().isoformat(), "vendor_key": vendor_key,
            "from": current.governance_status, "to": update.governance_status,
            "rules_activated": False,
        })
        _write_report(report)
        invalidate_candidate_cache()
        return changed


def historical_gl_evidence(
    tenant_id: str, vendor_name: str | None, property_code: str | None = None,
    *, limit: int = 3,
) -> list[dict[str, Any]]:
    """Return current, exact-vendor historical candidates; never a selection."""
    if not vendor_name:
        return []
    serialized = _historical_gl_evidence_cached(
        validate_tenant_id(tenant_id), _norm(vendor_name), _norm(property_code),
        max(1, min(8, limit)),
    )
    return json.loads(serialized)


@lru_cache(maxsize=4096)
def _historical_gl_evidence_cached(
    tenant_id: str, vendor_name: str, property_code: str, limit: int,
) -> str:
    report = _read_report(tenant_id)
    if report is None:
        return "[]"
    hashes, missing = _source_hashes(tenant_id)
    if missing or hashes != report.source_hashes:
        return "[]"
    profile = next((item for item in report.vendors if _norm(item.vendor_name) == vendor_name
                    or _norm(item.vendor_abbreviation) == vendor_name), None)
    if profile is None:
        return "[]"
    if profile.governance_status == "excluded":
        return "[]"
    frequencies = profile.property_gl_usage.get(property_code, []) if property_code else []
    if not frequencies:
        frequencies = profile.gl_usage
    return json.dumps([{
        "gl_code": item.key,
        "count": item.count,
        "amount": item.amount,
        "share": item.share,
        "vendor_key": profile.vendor_key,
        "snapshot_id": report.snapshot_id,
        "recommended_mode": profile.recommended_mode,
        "governance_status": profile.governance_status,
        "authoritative": False,
    } for item in frequencies[:limit]], sort_keys=True)


def invalidate_candidate_cache() -> None:
    _historical_gl_evidence_cached.cache_clear()


def _build_vendor_profiles(
    vendor_rows: list[dict[str, Any]], invoice_rows: list[dict[str, Any]],
    ledger_rows: list[dict[str, Any]], gl_labels: dict[str, str],
    prior_overrides: dict[str, VendorContextProfile],
) -> list[VendorContextProfile]:
    by_vendor: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in invoice_rows:
        by_vendor[_norm(row.get("vendor_name"))].append(row)
    ledger_by_vendor: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in ledger_rows:
        ledger_by_vendor[_norm(row.get("counterparty_name"))].append(row)
    profiles: list[VendorContextProfile] = []
    seen: set[str] = set()
    for vendor in vendor_rows:
        name = str(vendor.get("company") or "").strip()
        abbreviation = str(vendor.get("abbreviation") or "").strip() or None
        key = "vendor:" + _norm(abbreviation or name)
        normalized_names = {_norm(name), _norm(abbreviation)} - {""}
        rows = [row for normalized in normalized_names for row in by_vendor.get(normalized, [])]
        postings = [row for normalized in normalized_names for row in ledger_by_vendor.get(normalized, [])]
        profiles.append(_vendor_profile(key, name, abbreviation, bool(vendor.get("active", True)), rows, postings, gl_labels, prior_overrides.get(key)))
        seen.update(normalized_names)
    for normalized, rows in by_vendor.items():
        if normalized in seen or not rows:
            continue
        name = str(rows[0].get("vendor_name") or "Unknown vendor")
        key = "observed-vendor:" + normalized
        profiles.append(_vendor_profile(key, name, None, True, rows, ledger_by_vendor.get(normalized, []), gl_labels, prior_overrides.get(key)))
    return profiles


def _vendor_profile(
    key: str, name: str, abbreviation: str | None, active: bool,
    rows: list[dict[str, Any]], ledger_rows: list[dict[str, Any]],
    gl_labels: dict[str, str], prior: VendorContextProfile | None,
) -> VendorContextProfile:
    invoices = {str(row.get("invoice_occurrence_id")) for row in rows if row.get("invoice_occurrence_id")}
    invoice_dates = sorted(str(row.get("accounting_date") or row.get("invoice_date"))[:10]
                           for row in rows if row.get("accounting_date") or row.get("invoice_date"))
    months = sorted({value[:7] for value in invoice_dates})
    span = _month_span(months[0], months[-1]) if months else 0
    total = sum((_decimal(row.get("allocation_amount")) for row in rows), Decimal("0"))
    gl_usage = _frequencies(rows, "gl_code", "gl_code", labels=gl_labels)
    property_usage = _frequencies(rows, "property_code", "property_code")
    property_gl: dict[str, list[FrequencyItem]] = {}
    for prop in {str(row.get("property_code") or "") for row in rows} - {""}:
        property_gl[_norm(prop)] = _frequencies(
            [row for row in rows if _norm(row.get("property_code")) == _norm(prop)], "gl_code", "gl_code", labels=gl_labels,
        )
    ledger_total = sum((_ledger_amount(row) for row in ledger_rows), Decimal("0"))
    top_gl_share = gl_usage[0].share if gl_usage else 0
    top_property_share = property_usage[0].share if property_usage else 0
    invoice_count = len(invoices)
    coverage = len(months) / span if span else 0
    score = round(
        .40 * top_gl_share
        + .25 * min(1, invoice_count / 12)
        + .20 * min(1, len(months) / 6)
        + .15 * coverage,
        4,
    ) if rows else 0
    if invoice_count >= 6 and len(months) >= 3 and top_gl_share >= .85 and score >= .70:
        mode = "deterministic_candidate"
    elif invoice_count >= 3 and top_gl_share >= .65:
        mode = "review_candidate"
    elif invoice_count:
        mode = "variable"
    else:
        mode = "insufficient_history"
    reasons = _recommendation_reasons(invoice_count, len(months), top_gl_share, coverage, mode)
    return VendorContextProfile(
        vendor_key=key, vendor_name=name, vendor_abbreviation=abbreviation, active=active,
        invoice_count=invoice_count, allocation_count=len(rows), active_months=len(months),
        ledger_posting_count=len(ledger_rows), ledger_total_amount=_money(ledger_total),
        history_span_months=span, total_amount=_money(total),
        average_invoice_amount=_money(total / invoice_count if invoice_count else Decimal("0")),
        top_gl_share=top_gl_share, top_property_share=top_property_share,
        gl_usage=gl_usage, property_usage=property_usage, property_gl_usage=property_gl,
        first_accounting_date=invoice_dates[0] if invoice_dates else None,
        last_accounting_date=invoice_dates[-1] if invoice_dates else None,
        statistical_score=score, recommended_mode=mode, recommendation_reasons=reasons,
        governance_status=prior.governance_status if prior else "unreviewed",
        reviewer_notes=prior.reviewer_notes if prior else None,
        reviewed_by=prior.reviewed_by if prior else None,
        reviewed_at=prior.reviewed_at if prior else None,
        deterministic_coverage=deterministic_coverage.resolve_vendor(name, abbreviation),
    )


def _build_property_profiles(
    property_rows: list[dict[str, Any]], invoice_rows: list[dict[str, Any]],
    ledger_rows: list[dict[str, Any]], gl_labels: dict[str, str],
) -> list[PropertyContextProfile]:
    masters = [item for item in property_rows if item.get("entity_type") == "property"]
    by_property: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in invoice_rows:
        by_property[_norm(row.get("property_code"))].append(row)
    ledger_by_property: Counter[str] = Counter(_norm(row.get("property_code")) for row in ledger_rows if row.get("property_code"))
    profiles: list[PropertyContextProfile] = []
    seen: set[str] = set()
    for item in masters:
        code = str(item.get("property_code") or "").strip() or None
        name = str(item.get("property_name") or code or "Unknown property")
        normalized = _norm(code or name)
        rows = by_property.get(normalized, [])
        profiles.append(_property_profile("property:" + normalized, name, code, rows, ledger_by_property[normalized], gl_labels))
        seen.add(normalized)
    for normalized, rows in by_property.items():
        if normalized in seen or not rows:
            continue
        code = str(rows[0].get("property_code") or "") or None
        profiles.append(_property_profile("observed-property:" + normalized, code or normalized, code, rows, ledger_by_property[normalized], gl_labels))
    return profiles


def _property_profile(
    key: str, name: str, code: str | None, rows: list[dict[str, Any]],
    ledger_posting_count: int, gl_labels: dict[str, str],
) -> PropertyContextProfile:
    invoices = {str(row.get("invoice_occurrence_id")) for row in rows if row.get("invoice_occurrence_id")}
    total = sum((_decimal(row.get("allocation_amount")) for row in rows), Decimal("0"))
    return PropertyContextProfile(
        property_key=key, property_name=name, property_code=code,
        invoice_count=len(invoices), allocation_count=len(rows), ledger_posting_count=ledger_posting_count,
        total_amount=_money(total),
        gl_usage=_frequencies(rows, "gl_code", "gl_code", labels=gl_labels),
        vendor_usage=_frequencies(rows, "vendor_name", "vendor_name"),
    )


def _frequencies(
    rows: list[dict[str, Any]], key_field: str, label_field: str,
    *, labels: dict[str, str] | None = None,
) -> list[FrequencyItem]:
    counts: Counter[str] = Counter()
    amounts: dict[str, Decimal] = defaultdict(Decimal)
    labels_by_key: dict[str, str] = {}
    for row in rows:
        raw = str(row.get(key_field) or "").strip()
        if not raw:
            continue
        key = raw if key_field == "gl_code" else _norm(raw)
        counts[key] += 1
        amounts[key] += _decimal(row.get("allocation_amount"))
        labels_by_key[key] = str((labels or {}).get(raw) or row.get(label_field) or raw).strip()
    total_count = sum(counts.values())
    return [FrequencyItem(
        key=key, label=labels_by_key[key], count=count, amount=_money(amounts[key]),
        share=round(count / total_count, 4) if total_count else 0,
    ) for key, count in counts.most_common()]


def _recommendation_reasons(
    invoice_count: int, active_months: int, top_gl_share: float, coverage: float, mode: str,
) -> list[str]:
    if not invoice_count:
        return ["No published invoice history is available for this vendor."]
    reasons = [
        f"{invoice_count} historical invoices across {active_months} active months.",
        f"The most frequent GL represents {top_gl_share:.0%} of observed allocations.",
        f"Observed monthly coverage is {coverage:.0%} of the history span.",
    ]
    if mode == "deterministic_candidate":
        reasons.append("Volume, recurrence, and GL concentration support human review for a deterministic profile.")
    elif mode == "review_candidate":
        reasons.append("The pattern is promising but should be reviewed before proposing a deterministic profile.")
    else:
        reasons.append("The observed pattern is variable or too limited for deterministic treatment.")
    return reasons


def _source_hashes(tenant_id: str) -> tuple[dict[str, str], list[str]]:
    hashes: dict[str, str] = {}
    missing: list[str] = []
    for dataset in REQUIRED_DATASETS:
        value = hub.current_snapshot_fingerprint(tenant_id, dataset)
        if value:
            hashes[dataset.value] = value
        else:
            missing.append(dataset.value)
    return hashes, missing


def _require_report(tenant_id: str) -> ContextIntelligenceSnapshot:
    report = _read_report(validate_tenant_id(tenant_id))
    if report is None:
        raise FileNotFoundError("Context Intelligence has not been generated. Scan ResMan first.")
    return report


def _snapshot_summary(report: ContextIntelligenceSnapshot) -> dict[str, Any]:
    return report.model_dump(mode="json", exclude={"vendors", "properties", "audit"})


def _report_path(tenant_id: str) -> Path:
    return settings.WEBAPP_DATA_ROOT / "context_intelligence" / validate_tenant_id(tenant_id) / "current.json"


def _read_report(tenant_id: str) -> ContextIntelligenceSnapshot | None:
    path = _report_path(tenant_id)
    if not path.is_file():
        return None
    return ContextIntelligenceSnapshot.model_validate_json(path.read_text(encoding="utf-8"))


def _write_report(report: ContextIntelligenceSnapshot) -> None:
    path = _report_path(report.tenant_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = report.model_dump_json(indent=2)
    descriptor, temporary = tempfile.mkstemp(prefix="context-intelligence-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0").replace(",", ""))
    except InvalidOperation:
        return Decimal("0")


def _ledger_amount(row: dict[str, Any]) -> Decimal:
    debit = _decimal(row.get("debit"))
    credit = _decimal(row.get("credit"))
    return debit - credit


def _money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


def _month_span(first: str, last: str) -> int:
    first_year, first_month = (int(item) for item in first.split("-")[:2])
    last_year, last_month = (int(item) for item in last.split("-")[:2])
    return max(1, (last_year - first_year) * 12 + last_month - first_month + 1)


def _norm(value: Any) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").casefold()).strip("-")[:180]


def _now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "ANALYTICS_VERSION", "CONTRACT_VERSION", "ContextIntelligenceSnapshot",
    "GovernanceUpdate", "MatrixPage", "PropertyContextProfile", "VendorContextProfile",
    "historical_gl_evidence", "invalidate_candidate_cache", "list_matrix", "property_detail", "scan_resman", "status",
    "update_vendor_governance", "vendor_detail",
]
