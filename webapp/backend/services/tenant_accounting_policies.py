"""Tenant-isolated, human-approved accounting policy engine.

Policies are declarative runtime data, never vendor-specific Python branches.
They may resolve a tenant vendor entity and constrain GL candidates, but only
``AccountingDecisionEngine`` can select a final GL and only
``AccountingReadiness`` can authorize export.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import unicodedata
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .. import settings
from .accounting_contracts import GLCandidate, SemanticClassification
from .gl_catalog import load_gl_catalog


TENANT_POLICY_CONTRACT_VERSION = "tenant-accounting-policy/1.0"
VENDOR_ENTITY_CONTRACT_VERSION = "tenant-vendor-entity/1.0"
POLICY_SIMULATION_VERSION = "tenant-policy-simulation/1.0"
_LOCK = threading.RLock()


class TenantPolicyStatus(str, Enum):
    DRAFT = "draft"
    SIMULATED = "simulated"
    ACTIVE = "active"
    DISABLED = "disabled"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class TenantPolicyType(str, Enum):
    SEMANTIC_GL = "semantic_gl"
    VENDOR_SERVICE_GL = "vendor_service_gl"


class VendorEntityDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    canonical_name: str = Field(min_length=2, max_length=200)
    erp_vendor_id: str | None = Field(default=None, max_length=200)
    aliases: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def normalize_identity(self):
        self.canonical_name = self.canonical_name.strip()
        self.erp_vendor_id = _optional_text(self.erp_vendor_id)
        self.aliases = _clean_display_values([self.canonical_name, *self.aliases])
        return self


class VendorEntityAuditEvent(BaseModel):
    event: str
    actor: str
    at: datetime
    details: dict[str, Any] = Field(default_factory=dict)


class TenantVendorEntity(VendorEntityDraft):
    contract_version: str = VENDOR_ENTITY_CONTRACT_VERSION
    tenant_id: str
    vendor_entity_id: str
    created_at: datetime
    updated_at: datetime
    audit: list[VendorEntityAuditEvent] = Field(default_factory=list)


class VendorResolution(BaseModel):
    tenant_id: str
    observed_vendor: str | None
    vendor_entity_id: str | None
    canonical_name: str | None
    erp_vendor_id: str | None
    matched_alias: str | None
    resolved: bool
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class TenantPolicyScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vendor_entity_id: str | None = None
    property_ids: list[str] = Field(default_factory=list, max_length=100)
    document_family: str | None = None
    line_family: str | None = None
    trade_family: str | None = None
    work_mode: str | None = None
    description_terms: list[str] = Field(default_factory=list, max_length=40)
    term_match: Literal["any", "all"] = "any"

    @model_validator(mode="after")
    def require_scope(self):
        self.vendor_entity_id = _optional_text(self.vendor_entity_id)
        self.property_ids = _clean_terms(self.property_ids)
        self.description_terms = _clean_terms(self.description_terms)
        if not any((
            self.vendor_entity_id, self.property_ids, self.document_family,
            self.line_family, self.trade_family, self.work_mode,
            self.description_terms,
        )):
            raise ValueError("A tenant policy requires at least one reusable scope condition.")
        return self


class TenantPolicyAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allowed_gl_codes: list[str] = Field(default_factory=list, max_length=100)
    expected_amount: Decimal | None = None
    amount_tolerance: Decimal = Decimal("0.01")
    amount_mismatch_behavior: Literal["review", "warning"] = "review"

    @model_validator(mode="after")
    def validate_action(self):
        self.allowed_gl_codes = list(dict.fromkeys(
            str(code or "").strip() for code in self.allowed_gl_codes
            if str(code or "").strip()
        ))
        if not self.allowed_gl_codes:
            raise ValueError("A tenant accounting policy requires at least one allowed GL code.")
        if self.expected_amount is not None and self.expected_amount < 0:
            raise ValueError("expected_amount cannot be negative.")
        if self.amount_tolerance < 0:
            raise ValueError("amount_tolerance cannot be negative.")
        return self


class TenantPolicyDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=3, max_length=160)
    description: str = Field(min_length=3, max_length=1500)
    policy_type: TenantPolicyType
    scope: TenantPolicyScope
    action: TenantPolicyAction

    @model_validator(mode="after")
    def validate_policy_type(self):
        if (self.policy_type is TenantPolicyType.VENDOR_SERVICE_GL
                and not self.scope.vendor_entity_id):
            raise ValueError("vendor_service_gl requires vendor_entity_id.")
        return self


class PolicyAuditEvent(BaseModel):
    event: str
    actor: str
    at: datetime
    details: dict[str, Any] = Field(default_factory=dict)


class PolicySimulationLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    line_id: str
    observed_vendor: str | None = None
    property_id: str | None = None
    raw_description: str | None = None
    document_family: str | None = None
    line_family: str | None = None
    trade_family: str | None = None
    work_mode: str | None = None
    amount: Decimal | None = None
    current_gl: str | None = None
    candidate_gl_codes: list[str] = Field(default_factory=list, max_length=100)


class PolicySimulationReport(BaseModel):
    contract_version: str = POLICY_SIMULATION_VERSION
    simulation_id: str
    tenant_id: str
    policy_id: str
    policy_version: int
    snapshot_id: str
    evaluated_lines: int
    matched_lines: int
    would_constrain_lines: int
    unchanged_lines: int
    amount_mismatches: int
    blocking_conflicts: int
    missing_vendor_identity: int
    examples: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    simulated_at: datetime
    simulated_by: str


class TenantAccountingPolicy(TenantPolicyDraft):
    contract_version: str = TENANT_POLICY_CONTRACT_VERSION
    tenant_id: str
    policy_id: str
    version: int = 1
    status: TenantPolicyStatus = TenantPolicyStatus.DRAFT
    created_at: datetime
    updated_at: datetime
    approved_by: str | None = None
    approved_at: datetime | None = None
    source_interaction_id: str | None = None
    latest_simulation: PolicySimulationReport | None = None
    audit: list[PolicyAuditEvent] = Field(default_factory=list)


class TenantPolicyApplicationResult(BaseModel):
    candidates: list[GLCandidate]
    trace: dict[str, Any]


def default_tenant_id() -> str:
    raw = os.environ.get("INNER_VIEW_TENANT_ID", "").strip()
    deployment = os.environ.get("INNER_VIEW_DEPLOYMENT_MODE", "local").strip().lower()
    if raw:
        return validate_tenant_id(raw)
    if deployment in {"production", "prod"}:
        raise RuntimeError("INNER_VIEW_TENANT_ID is required in production until auth claims own tenant context.")
    return "local-default"


def tenant_id_for_row(row: dict[str, Any]) -> str:
    meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
    return validate_tenant_id(str(meta.get("tenant_id") or default_tenant_id()))


def resolve_tenant_context(requested_tenant_id: str | None = None) -> str:
    """Resolve the temporary deployment tenant adapter without cross-tenant override."""
    configured = default_tenant_id()
    requested = validate_tenant_id(requested_tenant_id) if requested_tenant_id else configured
    deployment = os.environ.get("INNER_VIEW_DEPLOYMENT_MODE", "local").strip().lower()
    if deployment in {"production", "prod"} and requested != configured:
        raise PermissionError(
            "Requested tenant does not match the authenticated deployment tenant context."
        )
    return requested


def validate_tenant_id(value: str) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", text):
        raise ValueError("tenant_id must be a 1-64 character URL-safe identifier.")
    return text


def create_vendor_entity(
    tenant_id: str,
    draft: VendorEntityDraft,
    *,
    actor: str = "local_operator",
) -> TenantVendorEntity:
    tenant_id = validate_tenant_id(tenant_id)
    now = _now()
    entity = TenantVendorEntity(
        **draft.model_dump(),
        tenant_id=tenant_id,
        vendor_entity_id="tve_" + uuid.uuid4().hex[:12],
        created_at=now,
        updated_at=now,
        audit=[VendorEntityAuditEvent(event="vendor_entity_created", actor=actor, at=now)],
    )
    with _LOCK:
        entities = _read_vendor_entities(tenant_id)
        occupied = {
            _normalize_identity(alias): item.vendor_entity_id
            for item in entities for alias in item.aliases
        }
        conflicts = sorted({alias for alias in entity.aliases if (
            _normalize_identity(alias) in occupied
            and occupied[_normalize_identity(alias)] != entity.vendor_entity_id
        )})
        if conflicts:
            raise ValueError("Vendor alias already belongs to another entity: " + ", ".join(conflicts))
        entities.append(entity)
        _write_vendor_entities(tenant_id, entities)
    return entity


def list_vendor_entities(tenant_id: str) -> list[TenantVendorEntity]:
    tenant_id = validate_tenant_id(tenant_id)
    with _LOCK:
        return sorted(_read_vendor_entities(tenant_id), key=lambda item: item.updated_at, reverse=True)


def get_vendor_entity(tenant_id: str, vendor_entity_id: str) -> TenantVendorEntity:
    for entity in list_vendor_entities(tenant_id):
        if entity.vendor_entity_id == vendor_entity_id:
            return entity
    raise KeyError(vendor_entity_id)


def resolve_vendor_entity(tenant_id: str, observed_vendor: str | None) -> VendorResolution:
    tenant_id = validate_tenant_id(tenant_id)
    observed = str(observed_vendor or "").strip()
    key = _normalize_identity(observed)
    if not key:
        return VendorResolution(
            tenant_id=tenant_id, observed_vendor=None, vendor_entity_id=None,
            canonical_name=None, erp_vendor_id=None, matched_alias=None, resolved=False,
        )
    matches: list[tuple[TenantVendorEntity, str]] = []
    for entity in list_vendor_entities(tenant_id):
        for alias in entity.aliases:
            if _normalize_identity(alias) == key:
                matches.append((entity, alias))
    if len(matches) != 1:
        return VendorResolution(
            tenant_id=tenant_id, observed_vendor=observed, vendor_entity_id=None,
            canonical_name=None, erp_vendor_id=None, matched_alias=None, resolved=False,
            evidence=[{"match_count": len(matches), "method": "exact_normalized_alias"}],
        )
    entity, alias = matches[0]
    return VendorResolution(
        tenant_id=tenant_id,
        observed_vendor=observed,
        vendor_entity_id=entity.vendor_entity_id,
        canonical_name=entity.canonical_name,
        erp_vendor_id=entity.erp_vendor_id,
        matched_alias=alias,
        resolved=True,
        evidence=[{
            "method": "exact_normalized_alias",
            "vendor_entity_id": entity.vendor_entity_id,
            "alias": alias,
            "authoritative": "human_approved_tenant_configuration",
        }],
    )


def create_policy_draft(
    tenant_id: str,
    draft: TenantPolicyDraft,
    *,
    source_interaction_id: str | None = None,
    actor: str = "accounting_assistant",
) -> TenantAccountingPolicy:
    tenant_id = validate_tenant_id(tenant_id)
    validate_policy_draft(tenant_id, draft)
    now = _now()
    policy = TenantAccountingPolicy(
        **draft.model_dump(),
        tenant_id=tenant_id,
        policy_id="tap_" + uuid.uuid4().hex[:12],
        created_at=now,
        updated_at=now,
        source_interaction_id=source_interaction_id,
        audit=[PolicyAuditEvent(event="policy_draft_created", actor=actor, at=now)],
    )
    with _LOCK:
        policies = _read_policies(tenant_id)
        policies.append(policy)
        _write_policies(tenant_id, policies)
    return policy


def list_policies(
    tenant_id: str, *, include_rejected: bool = True,
) -> list[TenantAccountingPolicy]:
    tenant_id = validate_tenant_id(tenant_id)
    with _LOCK:
        policies = _read_policies(tenant_id)
    if not include_rejected:
        policies = [item for item in policies if item.status is not TenantPolicyStatus.REJECTED]
    return sorted(policies, key=lambda item: item.updated_at, reverse=True)


def get_policy(tenant_id: str, policy_id: str) -> TenantAccountingPolicy:
    for policy in list_policies(tenant_id):
        if policy.policy_id == policy_id:
            return policy
    raise KeyError(policy_id)


def update_policy_draft(
    tenant_id: str,
    policy_id: str,
    draft: TenantPolicyDraft,
    *,
    actor: str = "local_operator",
) -> TenantAccountingPolicy:
    tenant_id = validate_tenant_id(tenant_id)
    validate_policy_draft(tenant_id, draft)
    with _LOCK:
        policies = _read_policies(tenant_id)
        policy = _find_policy(policies, policy_id)
        if policy.status not in {
            TenantPolicyStatus.DRAFT, TenantPolicyStatus.SIMULATED,
            TenantPolicyStatus.ACTIVE, TenantPolicyStatus.DISABLED,
        }:
            raise ValueError("This policy status cannot be edited.")
        previous = policy.model_dump(mode="json")
        policy.title = draft.title
        policy.description = draft.description
        policy.policy_type = draft.policy_type
        policy.scope = draft.scope
        policy.action = draft.action
        policy.version += 1
        policy.status = TenantPolicyStatus.DRAFT
        policy.latest_simulation = None
        policy.approved_by = None
        policy.approved_at = None
        policy.updated_at = _now()
        policy.audit.append(PolicyAuditEvent(
            event="policy_edited_simulation_invalidated",
            actor=actor,
            at=policy.updated_at,
            details={"previous_version_sha256": _payload_hash(previous)},
        ))
        _write_policies(tenant_id, policies)
        return policy


def simulate_policy(
    tenant_id: str,
    policy_id: str,
    lines: list[PolicySimulationLine],
    *,
    actor: str = "local_operator",
) -> TenantAccountingPolicy:
    tenant_id = validate_tenant_id(tenant_id)
    with _LOCK:
        policies = _read_policies(tenant_id)
        policy = _find_policy(policies, policy_id)
        if policy.status not in {
            TenantPolicyStatus.DRAFT, TenantPolicyStatus.SIMULATED,
            TenantPolicyStatus.ACTIVE, TenantPolicyStatus.DISABLED,
        }:
            raise ValueError("This policy cannot be simulated in its current status.")
        report = _build_simulation(tenant_id, policy, lines, actor)
        policy.latest_simulation = report
        policy.status = TenantPolicyStatus.SIMULATED
        policy.updated_at = report.simulated_at
        policy.audit.append(PolicyAuditEvent(
            event="policy_simulated",
            actor=actor,
            at=report.simulated_at,
            details={
                "simulation_id": report.simulation_id,
                "snapshot_id": report.snapshot_id,
                "matched_lines": report.matched_lines,
                "blocking_conflicts": report.blocking_conflicts,
            },
        ))
        _write_policies(tenant_id, policies)
        return policy


def decide_policy(
    tenant_id: str,
    policy_id: str,
    *,
    approve: bool,
    actor: str = "local_operator",
) -> TenantAccountingPolicy:
    tenant_id = validate_tenant_id(tenant_id)
    with _LOCK:
        policies = _read_policies(tenant_id)
        policy = _find_policy(policies, policy_id)
        if not approve and policy.status in {TenantPolicyStatus.DRAFT, TenantPolicyStatus.SIMULATED}:
            policy.status = TenantPolicyStatus.REJECTED
            event = "policy_rejected"
        elif approve:
            if policy.status is not TenantPolicyStatus.SIMULATED or policy.latest_simulation is None:
                raise ValueError("A current successful simulation is required before approval.")
            if policy.latest_simulation.policy_version != policy.version:
                raise ValueError("Policy changed after simulation; simulate the current version again.")
            if policy.latest_simulation.evaluated_lines < 1 or policy.latest_simulation.matched_lines < 1:
                raise ValueError("A policy requires at least one matching historical line before activation.")
            if policy.latest_simulation.blocking_conflicts:
                raise ValueError("Policy simulation has blocking conflicts and cannot be activated.")
            policy.status = TenantPolicyStatus.ACTIVE
            policy.approved_by = actor
            policy.approved_at = _now()
            event = "policy_approved_and_activated"
        else:
            raise ValueError("Only draft or simulated policies can be rejected.")
        policy.updated_at = _now()
        policy.audit.append(PolicyAuditEvent(event=event, actor=actor, at=policy.updated_at))
        _write_policies(tenant_id, policies)
        return policy


def set_policy_enabled(
    tenant_id: str,
    policy_id: str,
    *,
    enabled: bool,
    actor: str = "local_operator",
) -> TenantAccountingPolicy:
    tenant_id = validate_tenant_id(tenant_id)
    with _LOCK:
        policies = _read_policies(tenant_id)
        policy = _find_policy(policies, policy_id)
        if policy.status not in {TenantPolicyStatus.ACTIVE, TenantPolicyStatus.DISABLED}:
            raise ValueError("Only approved policies can be enabled or disabled.")
        policy.status = TenantPolicyStatus.ACTIVE if enabled else TenantPolicyStatus.DISABLED
        policy.updated_at = _now()
        policy.audit.append(PolicyAuditEvent(
            event="policy_enabled" if enabled else "policy_disabled",
            actor=actor,
            at=policy.updated_at,
        ))
        _write_policies(tenant_id, policies)
        return policy


def validate_policy_draft(tenant_id: str, draft: TenantPolicyDraft) -> dict[str, Any]:
    tenant_id = validate_tenant_id(tenant_id)
    _, catalog = load_gl_catalog()
    invalid = [code for code in draft.action.allowed_gl_codes
               if code not in catalog or not catalog[code].payable]
    if invalid:
        raise ValueError("Policy contains invalid or non-payable GL code(s): " + ", ".join(invalid))
    if draft.scope.vendor_entity_id:
        get_vendor_entity(tenant_id, draft.scope.vendor_entity_id)
    return {
        "tenant_id": tenant_id,
        "payable_codes": list(draft.action.allowed_gl_codes),
        "vendor_entity_validated": bool(draft.scope.vendor_entity_id),
    }


def apply_active_policies(
    *,
    tenant_id: str,
    row: dict[str, Any],
    semantics: SemanticClassification,
    catalog: dict[str, Any],
    candidates: list[GLCandidate],
) -> TenantPolicyApplicationResult:
    """Apply approved tenant policy constraints without selecting a GL."""
    tenant_id = validate_tenant_id(tenant_id)
    vendor = resolve_vendor_entity(tenant_id, str(row.get("Vendor") or ""))
    matching = [
        policy for policy in list_policies(tenant_id, include_rejected=False)
        if policy.status is TenantPolicyStatus.ACTIVE
        and _policy_matches(policy, row, semantics, vendor)
    ]
    base_trace = {
        "contract_version": TENANT_POLICY_CONTRACT_VERSION,
        "tenant_id": tenant_id,
        "vendor_resolution": vendor.model_dump(mode="json"),
        "matched_policy_ids": [policy.policy_id for policy in matching],
        "candidate_constraint_applied": False,
        "policy_conflict_blocking": False,
        "selected_gl": None,
    }
    if not matching:
        return TenantPolicyApplicationResult(candidates=candidates, trace=base_trace)

    allowed_sets = [set(policy.action.allowed_gl_codes) for policy in matching]
    allowed = set.intersection(*allowed_sets) if allowed_sets else set()
    amount_mismatches = [
        policy.policy_id for policy in matching
        if _amount_mismatch(policy, row)
    ]
    review_mismatches = [
        policy.policy_id for policy in matching
        if policy.policy_id in amount_mismatches
        and policy.action.amount_mismatch_behavior == "review"
    ]
    warning_mismatches = [
        policy.policy_id for policy in matching
        if policy.policy_id in amount_mismatches
        and policy.action.amount_mismatch_behavior == "warning"
    ]
    if not allowed:
        return TenantPolicyApplicationResult(candidates=[], trace={
            **base_trace,
            "candidate_constraint_applied": True,
            "policy_conflict_blocking": True,
            "conflicting_policy_ids": [policy.policy_id for policy in matching],
            "amount_mismatch_policy_ids": amount_mismatches,
            "input_candidate_count": len(candidates),
            "output_candidate_count": 0,
        })

    filtered: list[GLCandidate] = []
    for candidate in candidates:
        if candidate.gl_code not in allowed:
            continue
        adapted = candidate.model_copy(deep=True)
        if "tenant_policy" not in adapted.source:
            adapted.source += "+approved_deterministic_rule+tenant_policy"
        adapted.positive_evidence.extend({
            "policy_id": policy.policy_id,
            "policy_version": policy.version,
            "tenant_id": tenant_id,
            "approval": "human_approved_after_simulation",
        } for policy in matching)
        adapted.base_score = max(adapted.base_score, 0.88)
        adapted.rule_version = TENANT_POLICY_CONTRACT_VERSION
        filtered.append(adapted)
    existing = {candidate.gl_code for candidate in filtered}
    for code in sorted(allowed):
        account = catalog.get(code)
        if code in existing or account is None or not account.payable:
            continue
        if not _policy_candidate_compatible(account, semantics, matching):
            continue
        filtered.append(GLCandidate(
            gl_code=code,
            gl_name=account.gl_name,
            source="approved_deterministic_rule+tenant_policy",
            source_id=",".join(policy.policy_id for policy in matching),
            base_score=0.88,
            positive_evidence=[{
                "policy_id": policy.policy_id,
                "policy_version": policy.version,
                "tenant_id": tenant_id,
                "approval": "human_approved_after_simulation",
            } for policy in matching],
            compatibility_results=[{"check": "tenant_policy_scope", "passed": True}],
            rule_version=TENANT_POLICY_CONTRACT_VERSION,
        ))
    if not filtered:
        return TenantPolicyApplicationResult(candidates=[], trace={
            **base_trace,
            "candidate_constraint_applied": True,
            "policy_conflict_blocking": True,
            "conflicting_policy_ids": [policy.policy_id for policy in matching],
            "allowed_payable_codes": sorted(allowed),
            "amount_mismatch_policy_ids": amount_mismatches,
            "policy_review_required": bool(review_mismatches),
            "policy_warning_required": bool(warning_mismatches),
            "input_candidate_count": len(candidates),
            "output_candidate_count": 0,
        })
    return TenantPolicyApplicationResult(candidates=filtered, trace={
        **base_trace,
        "candidate_constraint_applied": True,
        "allowed_payable_codes": sorted(allowed),
        "amount_mismatch_policy_ids": amount_mismatches,
        "policy_review_required": bool(review_mismatches),
        "policy_warning_required": bool(warning_mismatches),
        "input_candidate_count": len(candidates),
        "output_candidate_count": len(filtered),
    })


def _build_simulation(
    tenant_id: str,
    policy: TenantAccountingPolicy,
    lines: list[PolicySimulationLine],
    actor: str,
) -> PolicySimulationReport:
    matched = constrained = unchanged = amount_mismatches = conflicts = missing_vendor = 0
    examples: list[dict[str, Any]] = []
    _, catalog = load_gl_catalog()
    for line in lines:
        vendor = resolve_vendor_entity(tenant_id, line.observed_vendor)
        row = {
            "Vendor": line.observed_vendor,
            "Property Abbreviation": line.property_id,
            "Amount": line.amount,
            "_meta": {"source_text": {"raw_description": line.raw_description}},
        }
        semantics = _simulation_semantics(line)
        if policy.scope.vendor_entity_id and not vendor.resolved:
            missing_vendor += 1
        if not _policy_matches(policy, row, semantics, vendor):
            continue
        matched += 1
        candidate_codes = set(line.candidate_gl_codes)
        allowed = set(policy.action.allowed_gl_codes)
        resulting = candidate_codes & allowed
        can_add_compatible_candidate = any(
            code in catalog
            and catalog[code].payable
            and _policy_candidate_compatible(catalog[code], semantics, [policy])
            for code in allowed
        )
        would_constrain = bool(candidate_codes and resulting != candidate_codes)
        blocking_conflict = bool(candidate_codes and not resulting and not can_add_compatible_candidate)
        if blocking_conflict:
            conflicts += 1
        elif would_constrain:
            constrained += 1
        else:
            unchanged += 1
        amount_mismatch = _amount_mismatch(policy, row)
        if amount_mismatch:
            amount_mismatches += 1
        if len(examples) < 20:
            examples.append({
                "line_id": line.line_id,
                "current_gl": line.current_gl,
                "candidate_gl_codes": sorted(candidate_codes),
                "allowed_gl_codes": sorted(allowed),
                "would_constrain": would_constrain,
                "blocking_conflict": blocking_conflict,
                "compatible_candidate_can_be_added": can_add_compatible_candidate,
                "amount_mismatch": amount_mismatch,
                "vendor_entity_id": vendor.vendor_entity_id,
            })
    material = {
        "tenant_id": tenant_id,
        "policy": policy.model_dump(mode="json", exclude={"audit", "latest_simulation"}),
        "lines": [line.model_dump(mode="json") for line in lines],
    }
    snapshot_id = _payload_hash(material)
    now = _now()
    return PolicySimulationReport(
        simulation_id="tps_" + uuid.uuid4().hex[:12],
        tenant_id=tenant_id,
        policy_id=policy.policy_id,
        policy_version=policy.version,
        snapshot_id=snapshot_id,
        evaluated_lines=len(lines),
        matched_lines=matched,
        would_constrain_lines=constrained,
        unchanged_lines=unchanged,
        amount_mismatches=amount_mismatches,
        blocking_conflicts=conflicts,
        missing_vendor_identity=missing_vendor,
        examples=examples,
        simulated_at=now,
        simulated_by=actor,
    )


def _policy_matches(
    policy: TenantAccountingPolicy,
    row: dict[str, Any],
    semantics: SemanticClassification,
    vendor: VendorResolution,
) -> bool:
    scope = policy.scope
    if scope.vendor_entity_id and scope.vendor_entity_id != vendor.vendor_entity_id:
        return False
    property_value = _normalize_identity(str(row.get("Property Abbreviation") or ""))
    if scope.property_ids and property_value not in set(scope.property_ids):
        return False
    checks = (
        (scope.document_family, semantics.document_family),
        (scope.line_family, semantics.line_family),
        (scope.trade_family, semantics.trade_family),
        (scope.work_mode, semantics.work_mode),
    )
    if any(expected and _normalize_identity(expected) != _normalize_identity(str(actual or ""))
           for expected, actual in checks):
        return False
    if scope.description_terms:
        meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
        source = meta.get("source_text") if isinstance(meta.get("source_text"), dict) else {}
        text = _normalize_identity(" ".join(str(value or "") for value in (
            source.get("raw_activity"), source.get("raw_description"),
            source.get("raw_invoice_description"),
        )))
        hits = [term in text for term in scope.description_terms]
        if not (all(hits) if scope.term_match == "all" else any(hits)):
            return False
    return True


def _amount_mismatch(policy: TenantAccountingPolicy, row: dict[str, Any]) -> bool:
    expected = policy.action.expected_amount
    if expected is None:
        return False
    try:
        actual = Decimal(str(row.get("Amount")))
    except Exception:
        return True
    return abs(actual - expected) > policy.action.amount_tolerance


def _semantically_compatible(account: Any, semantics: SemanticClassification) -> bool:
    trade = semantics.trade_family != "unknown" and semantics.trade_family in account.trade_families
    family = semantics.line_family != "unknown" and (
        semantics.line_family == account.gl_family or semantics.trade_family == account.gl_family
    )
    mode = not account.compatible_work_modes or semantics.work_mode in account.compatible_work_modes
    incompatible = semantics.work_mode in account.incompatible_work_modes
    return bool((trade or family) and mode and not incompatible)


def _policy_candidate_compatible(
    account: Any,
    semantics: SemanticClassification,
    policies: list[TenantAccountingPolicy],
) -> bool:
    if _semantically_compatible(account, semantics):
        return True
    if semantics.work_mode in account.incompatible_work_modes:
        return False
    # Some tenant charts have not yet been semantically enriched. A simulated,
    # human-approved compound policy may bridge missing catalog metadata only
    # when it carries line-level semantic/source conditions in addition to any
    # vendor identity. Vendor identity alone is never sufficient.
    if account.gl_family != "unknown":
        return False
    return any(any((
        policy.scope.description_terms,
        policy.scope.line_family,
        policy.scope.trade_family,
        policy.scope.work_mode,
    )) for policy in policies)


def _simulation_semantics(line: PolicySimulationLine) -> SemanticClassification:
    return SemanticClassification(
        semantic_version="tenant-policy-simulation/1.0",
        line_item_id=line.line_id,
        document_family=line.document_family or "unknown",
        line_family=line.line_family or "unknown",
        trade_family=line.trade_family or "unknown",
        work_mode=line.work_mode or "unknown",
        recurrence="unknown",
        capital_context="unknown",
        confidence=1.0,
    )


def _tenant_root(tenant_id: str) -> Path:
    return settings.WEBAPP_DATA_ROOT / "tenant_accounting" / validate_tenant_id(tenant_id)


def _policies_path(tenant_id: str) -> Path:
    return _tenant_root(tenant_id) / "policies.json"


def _vendors_path(tenant_id: str) -> Path:
    return _tenant_root(tenant_id) / "vendor_entities.json"


def _read_policies(tenant_id: str) -> list[TenantAccountingPolicy]:
    path = _policies_path(tenant_id)
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return [TenantAccountingPolicy(**item) for item in payload.get("policies", [])]
    except (OSError, ValueError, TypeError):
        return []


def _write_policies(tenant_id: str, policies: list[TenantAccountingPolicy]) -> None:
    path = _policies_path(tenant_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "contract_version": TENANT_POLICY_CONTRACT_VERSION,
        "tenant_id": tenant_id,
        "updated_at": _now().isoformat(),
        "policies": [item.model_dump(mode="json") for item in policies],
    }, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_vendor_entities(tenant_id: str) -> list[TenantVendorEntity]:
    path = _vendors_path(tenant_id)
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return [TenantVendorEntity(**item) for item in payload.get("vendor_entities", [])]
    except (OSError, ValueError, TypeError):
        return []


def _write_vendor_entities(tenant_id: str, entities: list[TenantVendorEntity]) -> None:
    path = _vendors_path(tenant_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "contract_version": VENDOR_ENTITY_CONTRACT_VERSION,
        "tenant_id": tenant_id,
        "updated_at": _now().isoformat(),
        "vendor_entities": [item.model_dump(mode="json") for item in entities],
    }, indent=2), encoding="utf-8")
    tmp.replace(path)


def _find_policy(policies: list[TenantAccountingPolicy], policy_id: str) -> TenantAccountingPolicy:
    for policy in policies:
        if policy.policy_id == policy_id:
            return policy
    raise KeyError(policy_id)


def _normalize_identity(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "").casefold())
    plain = "".join(char for char in normalized if not unicodedata.combining(char))
    return " ".join(re.findall(r"[a-z0-9]+", plain))


def _clean_terms(values: list[str]) -> list[str]:
    return list(dict.fromkeys(_normalize_identity(value) for value in values if _normalize_identity(value)))


def _clean_display_values(values: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        key = _normalize_identity(text)
        if text and key not in seen:
            seen.add(key)
            output.append(text)
    return output


def _optional_text(value: str | None) -> str | None:
    text = str(value or "").strip()
    return text or None


def _payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "POLICY_SIMULATION_VERSION", "TENANT_POLICY_CONTRACT_VERSION",
    "VENDOR_ENTITY_CONTRACT_VERSION", "PolicySimulationLine",
    "PolicySimulationReport", "TenantAccountingPolicy", "TenantPolicyAction",
    "TenantPolicyApplicationResult", "TenantPolicyDraft", "TenantPolicyScope",
    "TenantPolicyStatus", "TenantPolicyType", "TenantVendorEntity",
    "VendorEntityDraft", "VendorResolution", "apply_active_policies",
    "create_policy_draft", "create_vendor_entity", "decide_policy",
    "default_tenant_id", "get_policy", "get_vendor_entity", "list_policies",
    "list_vendor_entities", "resolve_vendor_entity", "set_policy_enabled",
    "simulate_policy", "tenant_id_for_row", "update_policy_draft",
    "validate_policy_draft", "validate_tenant_id",
]
