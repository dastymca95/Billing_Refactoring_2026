"""Human-approved, vendor-neutral accounting constraints.

Rules in this store never select a final GL.  They constrain and/or add
semantically compatible candidates before ``AccountingDecisionEngine`` runs.
Drafts are inert until an operator explicitly approves them.
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .. import settings
from .accounting_contracts import GLCandidate, SemanticClassification
from .gl_catalog import load_gl_catalog


RULE_CONTRACT_VERSION = "operator-accounting-rule/1.0"
_LOCK = threading.RLock()


class RuleStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    DISABLED = "disabled"
    REJECTED = "rejected"


class AccountingRuleScope(BaseModel):
    """Reusable semantic scope; identity-specific patches are impossible."""

    model_config = ConfigDict(extra="forbid")
    document_family: str | None = None
    line_family: str | None = None
    trade_family: str | None = None
    work_mode: str | None = None
    description_terms: list[str] = Field(default_factory=list, max_length=20)
    term_match: Literal["any", "all"] = "any"

    @model_validator(mode="after")
    def require_scope(self):
        if not any((self.document_family, self.line_family, self.trade_family,
                    self.work_mode, self.description_terms)):
            raise ValueError("A reusable rule requires a semantic or source-text scope.")
        self.description_terms = _clean_terms(self.description_terms)
        return self


class AccountingRuleConstraint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allowed_gl_codes: list[str] = Field(default_factory=list, max_length=100)
    minimum_gl_code: str | None = None
    maximum_gl_code: str | None = None

    @model_validator(mode="after")
    def require_constraint(self):
        self.allowed_gl_codes = list(dict.fromkeys(
            str(value or "").strip() for value in self.allowed_gl_codes
            if str(value or "").strip()
        ))
        self.minimum_gl_code = _optional_code(self.minimum_gl_code)
        self.maximum_gl_code = _optional_code(self.maximum_gl_code)
        if not self.allowed_gl_codes and not self.minimum_gl_code and not self.maximum_gl_code:
            raise ValueError("A rule must constrain the payable GL set.")
        if (self.minimum_gl_code and self.maximum_gl_code
                and int(self.minimum_gl_code) > int(self.maximum_gl_code)):
            raise ValueError("minimum_gl_code cannot exceed maximum_gl_code.")
        return self


class AccountingRuleDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=3, max_length=120)
    description: str = Field(min_length=3, max_length=1000)
    scope: AccountingRuleScope
    constraint: AccountingRuleConstraint


class RuleAuditEvent(BaseModel):
    event: str
    actor: str
    at: datetime
    details: dict[str, Any] = Field(default_factory=dict)


class OperatorAccountingRule(AccountingRuleDraft):
    contract_version: str = RULE_CONTRACT_VERSION
    rule_id: str
    status: RuleStatus = RuleStatus.DRAFT
    created_at: datetime
    updated_at: datetime
    approved_by: str | None = None
    approved_at: datetime | None = None
    source_interaction_id: str | None = None
    audit: list[RuleAuditEvent] = Field(default_factory=list)


class RuleApplicationResult(BaseModel):
    candidates: list[GLCandidate]
    trace: dict[str, Any]


def list_rules(*, include_rejected: bool = True) -> list[OperatorAccountingRule]:
    with _LOCK:
        rules = _read_rules()
    if not include_rejected:
        rules = [rule for rule in rules if rule.status is not RuleStatus.REJECTED]
    return sorted(rules, key=lambda item: item.updated_at, reverse=True)


def get_rule(rule_id: str) -> OperatorAccountingRule:
    for rule in list_rules():
        if rule.rule_id == rule_id:
            return rule
    raise KeyError(rule_id)


def create_draft(
    draft: AccountingRuleDraft,
    *,
    source_interaction_id: str | None = None,
    actor: str = "accounting_assistant",
) -> OperatorAccountingRule:
    validate_draft(draft)
    now = _now()
    rule = OperatorAccountingRule(
        **draft.model_dump(),
        rule_id="oar_" + uuid.uuid4().hex[:12],
        created_at=now,
        updated_at=now,
        source_interaction_id=source_interaction_id,
        audit=[RuleAuditEvent(event="draft_created", actor=actor, at=now)],
    )
    with _LOCK:
        rules = _read_rules()
        rules.append(rule)
        _write_rules(rules)
    return rule


def decide_draft(rule_id: str, *, approve: bool, actor: str = "local_operator") -> OperatorAccountingRule:
    with _LOCK:
        rules = _read_rules()
        rule = _find(rules, rule_id)
        if rule.status is not RuleStatus.DRAFT:
            raise ValueError("Only draft rules can be approved or rejected.")
        now = _now()
        if approve:
            validate_draft(AccountingRuleDraft(
                title=rule.title,
                description=rule.description,
                scope=rule.scope,
                constraint=rule.constraint,
            ))
            rule.status = RuleStatus.ACTIVE
            rule.approved_by = actor
            rule.approved_at = now
            event = "rule_approved_and_activated"
        else:
            rule.status = RuleStatus.REJECTED
            event = "rule_rejected"
        rule.updated_at = now
        rule.audit.append(RuleAuditEvent(event=event, actor=actor, at=now))
        _write_rules(rules)
        return rule


def update_rule(
    rule_id: str,
    draft: AccountingRuleDraft,
    *,
    actor: str = "local_operator",
) -> OperatorAccountingRule:
    validate_draft(draft)
    with _LOCK:
        rules = _read_rules()
        rule = _find(rules, rule_id)
        before = rule.model_dump(mode="json")
        rule.title = draft.title
        rule.description = draft.description
        rule.scope = draft.scope
        rule.constraint = draft.constraint
        rule.updated_at = _now()
        rule.audit.append(RuleAuditEvent(
            event="rule_edited",
            actor=actor,
            at=rule.updated_at,
            details={"previous_version_sha256": _payload_hash(before)},
        ))
        _write_rules(rules)
        return rule


def set_rule_enabled(rule_id: str, *, enabled: bool, actor: str = "local_operator") -> OperatorAccountingRule:
    with _LOCK:
        rules = _read_rules()
        rule = _find(rules, rule_id)
        if rule.status not in {RuleStatus.ACTIVE, RuleStatus.DISABLED}:
            raise ValueError("Only approved rules can be enabled or disabled.")
        rule.status = RuleStatus.ACTIVE if enabled else RuleStatus.DISABLED
        rule.updated_at = _now()
        rule.audit.append(RuleAuditEvent(
            event="rule_enabled" if enabled else "rule_disabled",
            actor=actor,
            at=rule.updated_at,
        ))
        _write_rules(rules)
        return rule


def validate_draft(draft: AccountingRuleDraft) -> dict[str, Any]:
    _, catalog = load_gl_catalog()
    invalid = [code for code in draft.constraint.allowed_gl_codes
               if code not in catalog or not catalog[code].payable]
    if invalid:
        raise ValueError("Rule contains invalid or non-payable GL code(s): " + ", ".join(invalid))
    allowed = _allowed_catalog_codes(draft.constraint, catalog)
    if not allowed:
        raise ValueError("The rule leaves no payable GL accounts in the active chart.")
    return {"payable_codes_in_scope": len(allowed), "sample_codes": sorted(allowed)[:10]}


def apply_active_rules(
    *,
    row: dict[str, Any],
    semantics: SemanticClassification,
    catalog: dict[str, Any],
    candidates: list[GLCandidate],
    tenant_id: str | None = None,
) -> RuleApplicationResult:
    # Compatibility boundary: this pre-tenant store belongs only to the local
    # default tenant.  It must never leak a historical global rule into another
    # customer's accounting context.
    if tenant_id is not None:
        from .tenant_accounting_policies import default_tenant_id, validate_tenant_id

        resolved_tenant = validate_tenant_id(tenant_id)
        if resolved_tenant != default_tenant_id():
            return RuleApplicationResult(candidates=candidates, trace={
                "contract_version": RULE_CONTRACT_VERSION,
                "legacy_adapter": "skipped_for_non_default_tenant",
                "tenant_id": resolved_tenant,
                "matched_rule_ids": [],
                "candidate_constraint_applied": False,
                "selected_gl": None,
            })
    """Constrain candidates; never choose a GL and never touch the row."""
    matching = [rule for rule in list_rules(include_rejected=False)
                if rule.status is RuleStatus.ACTIVE and _matches(rule.scope, row, semantics)]
    if not matching:
        return RuleApplicationResult(candidates=candidates, trace={
            "contract_version": RULE_CONTRACT_VERSION,
            "matched_rule_ids": [],
            "candidate_constraint_applied": False,
        })

    allowed: set[str] | None = None
    for rule in matching:
        rule_allowed = _allowed_catalog_codes(rule.constraint, catalog)
        allowed = rule_allowed if allowed is None else allowed & rule_allowed
    allowed = allowed or set()
    filtered = [candidate for candidate in candidates if candidate.gl_code in allowed]
    existing = {candidate.gl_code for candidate in filtered}
    for code in sorted(allowed):
        if code in existing or not _semantically_compatible(catalog[code], semantics):
            continue
        filtered.append(GLCandidate(
            gl_code=code,
            gl_name=catalog[code].gl_name,
            source="manual_approved_accounting_rule",
            source_id=",".join(rule.rule_id for rule in matching),
            base_score=0.82,
            positive_evidence=[{
                "rule_id": rule.rule_id,
                "rule_title": rule.title,
                "approval": "human_approved",
            } for rule in matching],
            compatibility_results=[{"check": "operator_rule_scope", "passed": True}],
            rule_version=RULE_CONTRACT_VERSION,
        ))
    return RuleApplicationResult(candidates=filtered, trace={
        "contract_version": RULE_CONTRACT_VERSION,
        "matched_rule_ids": [rule.rule_id for rule in matching],
        "candidate_constraint_applied": True,
        "allowed_payable_code_count": len(allowed),
        "input_candidate_count": len(candidates),
        "output_candidate_count": len(filtered),
        "selected_gl": None,
    })


def _matches(scope: AccountingRuleScope, row: dict[str, Any], semantics: SemanticClassification) -> bool:
    checks = (
        (scope.document_family, semantics.document_family),
        (scope.line_family, semantics.line_family),
        (scope.trade_family, semantics.trade_family),
        (scope.work_mode, semantics.work_mode),
    )
    if any(expected and expected.casefold() != str(actual or "").casefold()
           for expected, actual in checks):
        return False
    if scope.description_terms:
        meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
        source = meta.get("source_text") if isinstance(meta.get("source_text"), dict) else {}
        text = " ".join(str(value or "") for value in (
            source.get("raw_activity"), source.get("raw_description"),
            source.get("raw_invoice_description"),
        )).casefold()
        hits = [term.casefold() in text for term in scope.description_terms]
        if not (all(hits) if scope.term_match == "all" else any(hits)):
            return False
    return True


def _semantically_compatible(account: Any, semantics: SemanticClassification) -> bool:
    trade = semantics.trade_family != "unknown" and semantics.trade_family in account.trade_families
    family = semantics.line_family != "unknown" and (
        semantics.line_family == account.gl_family
        or semantics.trade_family == account.gl_family
    )
    mode = not account.compatible_work_modes or semantics.work_mode in account.compatible_work_modes
    incompatible = semantics.work_mode in account.incompatible_work_modes
    return bool((trade or family) and mode and not incompatible)


def _allowed_catalog_codes(constraint: AccountingRuleConstraint, catalog: dict[str, Any]) -> set[str]:
    allowed = {code for code, account in catalog.items() if account.payable}
    if constraint.allowed_gl_codes:
        allowed &= set(constraint.allowed_gl_codes)
    if constraint.minimum_gl_code:
        allowed = {code for code in allowed if code.isdigit() and int(code) >= int(constraint.minimum_gl_code)}
    if constraint.maximum_gl_code:
        allowed = {code for code in allowed if code.isdigit() and int(code) <= int(constraint.maximum_gl_code)}
    return allowed


def _store_path() -> Path:
    return settings.WEBAPP_DATA_ROOT / "operator_accounting_rules" / "rules.json"


def _read_rules() -> list[OperatorAccountingRule]:
    path = _store_path()
    if not path.is_file():
        return []
    try:
        # A batch may evaluate hundreds of rows. Cache by file mtime so active
        # rules are parsed once per persisted version, while edits become
        # visible immediately after the atomic replace below.
        rules = _read_rules_version(str(path), path.stat().st_mtime_ns)
        return [rule.model_copy(deep=True) for rule in rules]
    except (OSError, ValueError, TypeError):
        return []


@lru_cache(maxsize=8)
def _read_rules_version(path_text: str, _mtime_ns: int) -> tuple[OperatorAccountingRule, ...]:
    payload = json.loads(Path(path_text).read_text(encoding="utf-8"))
    return tuple(OperatorAccountingRule(**item) for item in payload.get("rules", []))


def _write_rules(rules: list[OperatorAccountingRule]) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "contract_version": RULE_CONTRACT_VERSION,
        "updated_at": _now().isoformat(),
        "rules": [rule.model_dump(mode="json") for rule in rules],
    }, indent=2), encoding="utf-8")
    tmp.replace(path)
    _read_rules_version.cache_clear()


def _find(rules: list[OperatorAccountingRule], rule_id: str) -> OperatorAccountingRule:
    for rule in rules:
        if rule.rule_id == rule_id:
            return rule
    raise KeyError(rule_id)


def _clean_terms(values: list[str]) -> list[str]:
    return list(dict.fromkeys(str(value or "").strip().casefold()
                              for value in values if str(value or "").strip()))


def _optional_code(value: str | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    text = str(value).strip()
    if not text.isdigit():
        raise ValueError("GL range boundaries must be numeric chart codes.")
    return text


def _payload_hash(payload: dict[str, Any]) -> str:
    import hashlib
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "AccountingRuleConstraint", "AccountingRuleDraft", "AccountingRuleScope",
    "OperatorAccountingRule", "RuleApplicationResult", "RuleStatus",
    "apply_active_rules", "create_draft", "decide_draft", "get_rule",
    "list_rules", "set_rule_enabled", "update_rule", "validate_draft",
]
