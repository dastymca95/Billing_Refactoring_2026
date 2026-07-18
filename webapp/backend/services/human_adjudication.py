"""Tenant-private human adjudication for the live Invoice Processor.

The observed document remains immutable.  This module stores append-only
correction revisions and governance events, then exposes the latest approved
invoice correction as a downstream overlay.  Benchmark, learning and rule
authorization are deliberately separate decisions.
"""
from __future__ import annotations

import hashlib
import copy
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .. import settings
from .canonical_semantics import resolve_canonical_concept
from .gl_catalog import load_gl_catalog
from .invoice_identity import build_invoice_identities
from .tenant_accounting_policies import (
    PolicySimulationLine,
    TenantPolicyAction,
    TenantPolicyDraft,
    TenantPolicyScope,
    TenantPolicyType,
    create_policy_draft,
    decide_policy,
    default_tenant_id,
    resolve_tenant_context,
    simulate_policy,
    tenant_id_for_row,
    validate_tenant_id,
)


ADJUDICATION_CONTRACT_VERSION = "human-invoice-adjudication/1.0"
GOVERNANCE_EVENT_VERSION = "human-adjudication-governance/1.0"
_LOCK = threading.RLock()


class ReviewerRole(str, Enum):
    PROPERTY_MANAGER = "property_manager"
    ACCOUNTANT_AP = "accountant_ap"
    ACCOUNTING_MANAGER_CONTROLLER = "accounting_manager_controller"
    PLATFORM_ADMIN = "platform_admin"


_ROLE_RANK = {
    ReviewerRole.PROPERTY_MANAGER: 10,
    ReviewerRole.ACCOUNTANT_AP: 20,
    ReviewerRole.ACCOUNTING_MANAGER_CONTROLLER: 30,
    ReviewerRole.PLATFORM_ADMIN: 40,
}


class AuthorizationScope(str, Enum):
    INVOICE_CORRECTION = "invoice_correction"
    BENCHMARK_SUBMISSION = "benchmark_submission"
    LEARNING_APPROVAL = "learning_approval"
    RULE_PROPOSAL = "rule_proposal"
    RULE_APPROVAL = "rule_approval"
    SHARED_KNOWLEDGE_PROMOTION = "shared_knowledge_promotion"


class ActorContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviewer_id: str = Field(min_length=1, max_length=160)
    role: ReviewerRole
    tenant_id: str

    def require(self, scope: AuthorizationScope) -> None:
        required = {
            AuthorizationScope.INVOICE_CORRECTION: ReviewerRole.PROPERTY_MANAGER,
            AuthorizationScope.BENCHMARK_SUBMISSION: ReviewerRole.PROPERTY_MANAGER,
            AuthorizationScope.LEARNING_APPROVAL: ReviewerRole.ACCOUNTANT_AP,
            AuthorizationScope.RULE_PROPOSAL: ReviewerRole.PROPERTY_MANAGER,
            AuthorizationScope.RULE_APPROVAL: ReviewerRole.ACCOUNTING_MANAGER_CONTROLLER,
            AuthorizationScope.SHARED_KNOWLEDGE_PROMOTION: ReviewerRole.PLATFORM_ADMIN,
        }[scope]
        if _ROLE_RANK[self.role] < _ROLE_RANK[required]:
            raise PermissionError(
                f"Role {self.role.value} is not authorized for {scope.value}; "
                f"minimum role is {required.value}."
            )


class EvidenceBox(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: float
    y: float
    w: float
    h: float


class SourceEvidenceSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_file: str | None = None
    source_document_sha256: str | None = None
    page: int | None = None
    trace_ids: list[str] = Field(default_factory=list)
    bounding_boxes: list[EvidenceBox] = Field(default_factory=list)
    observed_text: list[str] = Field(default_factory=list)
    evidence_fingerprint: str


class ProcessingVersionSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    extractor_version: str | None = None
    schema_version: str | None = None
    prompt_version: str | None = None
    provider: str | None = None
    model_id: str | None = None
    profile_id: str | None = None


class AdjudicationOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rationale: str = Field(min_length=3, max_length=4000)
    add_to_benchmark: bool = False
    approve_learning_example: bool = False
    propose_reusable_rule: bool = False


class HumanAdjudicationRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: str = ADJUDICATION_CONTRACT_VERSION
    revision_id: str
    correction_key: str
    revision_number: int = Field(ge=1)
    supersedes_revision_id: str | None = None
    tenant_id: str
    batch_id: str
    invoice_group_id: str
    invoice_number: str | None = None
    source_line_fingerprint: str
    global_row_index: int = Field(ge=0)
    local_row_index: int = Field(ge=0)
    field: str
    original_ai_value: Any = None
    previous_value: Any = None
    corrected_value: Any = None
    evidence: SourceEvidenceSnapshot
    versions: ProcessingVersionSnapshot
    confidence: float | None = Field(default=None, ge=0, le=1)
    alternatives: list[Any] = Field(default_factory=list)
    rationale: str
    reviewer_id: str
    reviewer_role: ReviewerRole
    authorization_scopes: list[AuthorizationScope]
    canonical_concept: str | None = None
    document_family: str | None = None
    line_family: str | None = None
    trade_family: str | None = None
    work_mode: str | None = None
    created_at: datetime


class GovernanceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: str = GOVERNANCE_EVENT_VERSION
    event_id: str
    tenant_id: str
    revision_id: str
    event_type: str
    status: str
    actor: str
    actor_role: ReviewerRole
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class AdjudicationApplyReport(BaseModel):
    batch_id: str
    recorded: int = 0
    applied: int = 0
    unresolved: int = 0
    revision_ids: list[str] = Field(default_factory=list)
    benchmark_submissions: int = 0
    learning_approvals: int = 0
    rule_proposals: int = 0


def runtime_actor_context() -> ActorContext:
    """Temporary deployment adapter until authenticated claims own context.

    Browser-supplied roles are intentionally ignored.  Production requires
    explicit server-side identity/role configuration and therefore cannot
    silently fall back to a privileged local user.
    """
    deployment = os.environ.get("INNER_VIEW_DEPLOYMENT_MODE", "local").strip().lower()
    reviewer_id = os.environ.get("INNER_VIEW_LOCAL_REVIEWER_ID", "").strip()
    role_text = os.environ.get("INNER_VIEW_LOCAL_REVIEWER_ROLE", "").strip().lower()
    if deployment in {"production", "prod"} and (not reviewer_id or not role_text):
        raise PermissionError(
            "Authenticated reviewer identity and role claims are required in production."
        )
    reviewer_id = reviewer_id or "local_operator"
    role_text = role_text or ReviewerRole.ACCOUNTING_MANAGER_CONTROLLER.value
    try:
        role = ReviewerRole(role_text)
    except ValueError as exc:
        raise PermissionError("Configured reviewer role is invalid.") from exc
    return ActorContext(
        reviewer_id=reviewer_id,
        role=role,
        tenant_id=resolve_tenant_context(None),
    )


def record_manual_edits(
    *, result: dict[str, Any], batch_id: str,
    edits_by_index: dict[int, dict[str, Any]], options: AdjudicationOptions,
    actor: ActorContext,
) -> AdjudicationApplyReport:
    """Record immutable revisions, then apply their invoice-only overlay."""
    actor.require(AuthorizationScope.INVOICE_CORRECTION)
    if options.add_to_benchmark:
        actor.require(AuthorizationScope.BENCHMARK_SUBMISSION)
    if options.approve_learning_example:
        actor.require(AuthorizationScope.LEARNING_APPROVAL)
    if options.propose_reusable_rule:
        actor.require(AuthorizationScope.RULE_PROPOSAL)

    targets = _flatten_rows(result)
    existing = list_revisions(actor.tenant_id, batch_id=batch_id)
    latest = _latest_by_key(existing)
    now = _now()
    revisions: list[HumanAdjudicationRevision] = []
    for row_index in sorted(edits_by_index):
        if row_index not in targets:
            raise ValueError(f"Row {row_index} is not present in the active batch preview.")
        invoice_group_id, local_index, invoice, row = targets[row_index]
        row_tenant = tenant_id_for_row(row)
        if row_tenant != actor.tenant_id:
            raise PermissionError("A correction cannot cross tenant boundaries.")
        for field, corrected in edits_by_index[row_index].items():
            if not isinstance(field, str) or field.startswith("_"):
                raise ValueError("Only visible invoice fields may be corrected.")
            if isinstance(corrected, (dict, list, tuple, set)):
                raise ValueError("A cell correction must be a scalar value.")
            source_fingerprint = _source_line_fingerprint(row)
            correction_key = _correction_key(
                actor.tenant_id, batch_id, invoice_group_id, source_fingerprint, field,
            )
            prior = latest.get(correction_key)
            semantics = _semantic_context(row)
            scopes = [AuthorizationScope.INVOICE_CORRECTION]
            if options.add_to_benchmark:
                scopes.append(AuthorizationScope.BENCHMARK_SUBMISSION)
            if options.approve_learning_example:
                scopes.append(AuthorizationScope.LEARNING_APPROVAL)
            if options.propose_reusable_rule and field == "GL Account":
                scopes.append(AuthorizationScope.RULE_PROPOSAL)
            revision = HumanAdjudicationRevision(
                revision_id="har_" + uuid.uuid4().hex[:16],
                correction_key=correction_key,
                revision_number=(prior.revision_number + 1 if prior else 1),
                supersedes_revision_id=(prior.revision_id if prior else None),
                tenant_id=actor.tenant_id,
                batch_id=batch_id,
                invoice_group_id=invoice_group_id,
                invoice_number=_text(invoice.get("invoice_number") or row.get("Invoice Number")) or None,
                source_line_fingerprint=source_fingerprint,
                global_row_index=row_index,
                local_row_index=local_index,
                field=field,
                original_ai_value=(prior.original_ai_value if prior else row.get(field)),
                previous_value=row.get(field),
                corrected_value=corrected,
                evidence=_source_evidence(batch_id, row, field),
                versions=_versions(row),
                confidence=_confidence(row, field),
                alternatives=_alternatives(row, field),
                rationale=options.rationale,
                reviewer_id=actor.reviewer_id,
                reviewer_role=actor.role,
                authorization_scopes=scopes,
                canonical_concept=semantics["canonical_concept"],
                document_family=semantics["document_family"],
                line_family=semantics["line_family"],
                trade_family=semantics["trade_family"],
                work_mode=semantics["work_mode"],
                created_at=now,
            )
            revisions.append(revision)
            latest[correction_key] = revision

    if not revisions:
        return AdjudicationApplyReport(batch_id=batch_id)
    _append_jsonl(_revisions_path(actor.tenant_id), revisions)

    events: list[GovernanceEvent] = []
    for revision in revisions:
        if options.add_to_benchmark:
            events.append(_event(revision, "benchmark_submitted", "pending_approval", actor))
        if options.approve_learning_example:
            events.append(_event(revision, "learning_approved", "approved", actor, {
                "retrieval_scope": "tenant_private",
                "selection_authority": False,
            }))
        if options.propose_reusable_rule and revision.field == "GL Account":
            policy_id = _create_rule_draft(revision, actor)
            events.append(_event(revision, "rule_proposed", "pending_approval", actor, {
                "tenant_policy_id": policy_id,
                "activation": "requires_separate_controller_approval",
            }))
    if events:
        _append_jsonl(_events_path(actor.tenant_id), events)

    report = apply_to_result(result, batch_id=batch_id, tenant_id=actor.tenant_id)
    report.recorded = len(revisions)
    report.revision_ids = [item.revision_id for item in revisions]
    report.benchmark_submissions = sum(e.event_type == "benchmark_submitted" for e in events)
    report.learning_approvals = sum(e.event_type == "learning_approved" for e in events)
    report.rule_proposals = sum(e.event_type == "rule_proposed" for e in events)
    return report


def apply_to_result(
    result: dict[str, Any], *, batch_id: str, tenant_id: str | None = None,
) -> AdjudicationApplyReport:
    tenant_id = validate_tenant_id(tenant_id or default_tenant_id())
    latest = _latest_by_key(list_revisions(tenant_id, batch_id=batch_id))
    report = AdjudicationApplyReport(batch_id=batch_id)
    if not latest:
        return report
    governance = _governance_by_revision(tenant_id)
    touched: dict[int, tuple[list[dict[str, Any]], str]] = {}
    matched: set[str] = set()
    for group_id, _, invoice, row in _all_result_rows(result):
        if tenant_id_for_row(row) != tenant_id:
            continue
        meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
        if not group_id:
            continue
        fingerprint = _source_line_fingerprint(row)
        relevant = [item for item in latest.values()
                    if item.invoice_group_id == group_id
                    and item.source_line_fingerprint == fingerprint]
        for revision in relevant:
            _apply_revision(row, revision, governance.get(revision.revision_id, []))
            matched.add(revision.revision_id)
            report.applied += 1
            rows = invoice.get("rows") or []
            source_file = str(meta.get("source_file") or invoice.get("source_file") or batch_id)
            touched.setdefault(id(rows), (rows, source_file))
    report.unresolved = max(0, len(latest) - len(matched))
    if touched:
        from .accounting_integration_bridges import RowAccountingV2Adapter
        from . import output_contract_validator

        for rows, document_id in touched.values():
            immutable_layers = []
            for row in rows:
                meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
                immutable_layers.append((
                    row,
                    "source_text" in meta,
                    copy.deepcopy(meta.get("source_text")),
                    "document_facts" in meta,
                    copy.deepcopy(meta.get("document_facts")),
                ))
            RowAccountingV2Adapter().enrich_rows(rows, {
                "document_id": document_id,
                "extraction_route": "human_adjudication_replay",
            })
            # The accounting adapter may add compatibility fields while
            # rebuilding its downstream contracts.  Human adjudication must
            # not mutate the observed source layer, so restore it byte-for-byte.
            for row, had_source, source_text, had_facts, document_facts in immutable_layers:
                meta = row.setdefault("_meta", {})
                if had_source:
                    meta["source_text"] = source_text
                else:
                    meta.pop("source_text", None)
                if had_facts:
                    meta["document_facts"] = document_facts
                else:
                    meta.pop("document_facts", None)
            output_contract_validator.annotate_rows(rows)
    return report


def list_revisions(
    tenant_id: str, *, batch_id: str | None = None,
    invoice_group_id: str | None = None,
) -> list[HumanAdjudicationRevision]:
    tenant_id = validate_tenant_id(tenant_id)
    items = _read_jsonl(_revisions_path(tenant_id), HumanAdjudicationRevision)
    if batch_id:
        items = [item for item in items if item.batch_id == batch_id]
    if invoice_group_id:
        items = [item for item in items if item.invoice_group_id == invoice_group_id]
    return sorted(items, key=lambda item: (item.created_at, item.revision_number), reverse=True)


def list_governance_events(
    tenant_id: str, *, revision_id: str | None = None,
) -> list[GovernanceEvent]:
    tenant_id = validate_tenant_id(tenant_id)
    items = _read_jsonl(_events_path(tenant_id), GovernanceEvent)
    if revision_id:
        items = [item for item in items if item.revision_id == revision_id]
    return sorted(items, key=lambda item: item.created_at, reverse=True)


def decide_benchmark(
    revision_id: str, *, approve: bool, actor: ActorContext,
) -> GovernanceEvent:
    actor.require(AuthorizationScope.LEARNING_APPROVAL)
    revision = _revision_for_actor(revision_id, actor)
    prior = list_governance_events(actor.tenant_id, revision_id=revision_id)
    if not any(item.event_type == "benchmark_submitted" for item in prior):
        raise ValueError("This correction was not submitted to the benchmark.")
    event = _event(
        revision,
        "benchmark_approved" if approve else "benchmark_rejected",
        "approved" if approve else "rejected",
        actor,
        {"ground_truth_scope": "exact_evidence_only", "immutable_revision": True},
    )
    _append_jsonl(_events_path(actor.tenant_id), [event])
    return event


def approve_learning(revision_id: str, *, actor: ActorContext) -> GovernanceEvent:
    actor.require(AuthorizationScope.LEARNING_APPROVAL)
    revision = _revision_for_actor(revision_id, actor)
    event = _event(revision, "learning_approved", "approved", actor, {
        "retrieval_scope": "tenant_private", "selection_authority": False,
    })
    _append_jsonl(_events_path(actor.tenant_id), [event])
    return event


def approve_rule(revision_id: str, *, actor: ActorContext) -> GovernanceEvent:
    actor.require(AuthorizationScope.RULE_APPROVAL)
    revision = _revision_for_actor(revision_id, actor)
    events = list_governance_events(actor.tenant_id, revision_id=revision_id)
    proposed = next((item for item in events if item.event_type == "rule_proposed"), None)
    if proposed is None:
        raise ValueError("This correction has no reusable rule proposal.")
    policy_id = str(proposed.details.get("tenant_policy_id") or "")
    if not policy_id:
        raise ValueError("The rule proposal is missing its tenant policy draft.")
    line = PolicySimulationLine(
        line_id=revision.source_line_fingerprint,
        raw_description=" ".join(revision.evidence.observed_text) or revision.canonical_concept,
        document_family=revision.document_family,
        line_family=revision.line_family,
        trade_family=revision.trade_family,
        work_mode=revision.work_mode,
        current_gl=_text(revision.corrected_value) or None,
        candidate_gl_codes=[_text(revision.corrected_value)],
    )
    simulate_policy(actor.tenant_id, policy_id, [line], actor=actor.reviewer_id)
    decided = decide_policy(
        actor.tenant_id, policy_id, approve=True, actor=actor.reviewer_id,
    )
    event = _event(revision, "rule_approved", "approved", actor, {
        "tenant_policy_id": policy_id,
        "tenant_policy_status": decided.status.value,
        "selection_authority": False,
    })
    _append_jsonl(_events_path(actor.tenant_id), [event])
    return event


def approved_learning_candidates(
    *, tenant_id: str, canonical_concept: str | None,
    document_family: str, line_family: str, trade_family: str, work_mode: str,
) -> list[dict[str, Any]]:
    """Return tenant-private candidate evidence; never a selected GL."""
    if not canonical_concept:
        return []
    governance = _governance_by_revision(validate_tenant_id(tenant_id))
    output: list[dict[str, Any]] = []
    for revision in _latest_by_key(list_revisions(tenant_id)).values():
        if revision.field != "GL Account" or revision.canonical_concept != canonical_concept:
            continue
        if revision.work_mode != work_mode or revision.line_family != line_family:
            continue
        events = governance.get(revision.revision_id, [])
        if not any(item.event_type == "learning_approved" and item.status == "approved"
                   for item in events):
            continue
        output.append({
            "gl_code": _text(revision.corrected_value),
            "revision_id": revision.revision_id,
            "canonical_concept": canonical_concept,
            "document_family": document_family,
            "line_family": line_family,
            "trade_family": trade_family,
            "work_mode": work_mode,
            "evidence_fingerprint": revision.evidence.evidence_fingerprint,
            "selection_authority": False,
        })
    return output


def source_evidence_for_cell(
    *, batch_id: str, row: dict[str, Any], field: str,
) -> SourceEvidenceSnapshot:
    """Public read-only adapter used by the Invoice Processor panel."""
    return _source_evidence(batch_id, row, field)


def _apply_revision(
    row: dict[str, Any], revision: HumanAdjudicationRevision,
    events: list[GovernanceEvent],
) -> None:
    meta = row.setdefault("_meta", {})
    if revision.field == "GL Account":
        meta["approved_operator_gl_candidate"] = _text(revision.corrected_value)
        meta["approved_operator_gl_evidence"] = {
            "revision_id": revision.revision_id,
            "rationale": revision.rationale,
            "reviewer_id": revision.reviewer_id,
            "tenant_id": revision.tenant_id,
            "evidence_fingerprint": revision.evidence.evidence_fingerprint,
        }
    else:
        row[revision.field] = revision.corrected_value
    event_types = {item.event_type for item in events if item.status == "approved"}
    badges = ["manually_corrected"]
    if "benchmark_approved" in event_types:
        badges.append("benchmark_approved")
    if "learning_approved" in event_types:
        badges.append("learning_approved")
    if "rule_approved" in event_types:
        badges.append("governed_by_rule")
    field_badges = dict(meta.get("human_adjudication_badges") or {})
    field_badges[revision.field] = badges
    meta["human_adjudication_badges"] = field_badges
    applied = dict(meta.get("human_adjudication_applied") or {})
    applied[revision.field] = {
        "revision_id": revision.revision_id,
        "revision_number": revision.revision_number,
        "reviewer_id": revision.reviewer_id,
        "created_at": revision.created_at.isoformat(),
        "rationale": revision.rationale,
    }
    meta["human_adjudication_applied"] = applied


def _create_rule_draft(revision: HumanAdjudicationRevision, actor: ActorContext) -> str:
    code = _text(revision.corrected_value)
    _, catalog = load_gl_catalog()
    if revision.field != "GL Account" or code not in catalog or not catalog[code].payable:
        raise ValueError("A reusable accounting rule requires a valid payable GL correction.")
    if not revision.line_family or revision.line_family == "unknown":
        raise ValueError("A reusable rule requires a resolved semantic line family.")
    scope = TenantPolicyScope(
        document_family=revision.document_family,
        line_family=revision.line_family,
        trade_family=(revision.trade_family if revision.trade_family != "unknown" else None),
        work_mode=(revision.work_mode if revision.work_mode != "unknown" else None),
    )
    concept = revision.canonical_concept or revision.line_family
    draft = TenantPolicyDraft(
        title=f"Proposed accounting treatment for {concept}",
        description=(
            "Evidence-backed reusable semantic constraint proposed from an Invoice "
            "Processor correction. It remains inactive until controller approval."
        ),
        policy_type=TenantPolicyType.SEMANTIC_GL,
        scope=scope,
        action=TenantPolicyAction(allowed_gl_codes=[code]),
    )
    policy = create_policy_draft(
        actor.tenant_id, draft, source_interaction_id=revision.revision_id,
        actor=actor.reviewer_id,
    )
    return policy.policy_id


def _source_evidence(batch_id: str, row: dict[str, Any], field: str) -> SourceEvidenceSnapshot:
    meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
    source_file = Path(str(meta.get("source_file") or "")).name or None
    trace_ids = [str(value) for value in meta.get("trace_ids") or []]
    boxes: list[EvidenceBox] = []
    texts: list[str] = []
    if source_file:
        for trace in _trace_items(batch_id, source_file):
            if trace_ids and trace.get("trace_id") not in trace_ids:
                continue
            feeds = list(trace.get("feeds_columns") or [])
            if feeds and field not in feeds:
                continue
            bbox = trace.get("bbox") if isinstance(trace.get("bbox"), dict) else None
            if bbox and all(key in bbox for key in ("x", "y", "w", "h")):
                boxes.append(EvidenceBox(**{key: float(bbox[key]) for key in ("x", "y", "w", "h")}))
            text = _text(trace.get("detected_text"))
            if text:
                texts.append(text)
    source = meta.get("source_text") if isinstance(meta.get("source_text"), dict) else {}
    if not texts:
        texts = [_text(value) for value in (
            source.get("raw_activity"), source.get("raw_description"),
            source.get("raw_invoice_description"),
        ) if _text(value)]
    source_sha = _source_document_hash(batch_id, source_file) if source_file else None
    material = {
        "source_document_sha256": source_sha,
        "page": meta.get("source_page"),
        "trace_ids": trace_ids,
        "boxes": [box.model_dump() for box in boxes],
        "observed_text": texts,
    }
    return SourceEvidenceSnapshot(
        source_file=source_file,
        source_document_sha256=source_sha,
        page=_int(meta.get("source_page")),
        trace_ids=trace_ids,
        bounding_boxes=boxes,
        observed_text=list(dict.fromkeys(texts)),
        evidence_fingerprint=_hash(material),
    )


def _semantic_context(row: dict[str, Any]) -> dict[str, str | None]:
    meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
    semantic = meta.get("semantic_classification") if isinstance(meta.get("semantic_classification"), dict) else {}
    source = meta.get("source_text") if isinstance(meta.get("source_text"), dict) else {}
    raw = _text(source.get("raw_description") or source.get("raw_activity"))
    resolved = resolve_canonical_concept(
        raw,
        line_family=_text(semantic.get("line_family")) or "unknown",
        trade_family=_text(semantic.get("trade_family")) or "unknown",
        work_mode=_text(semantic.get("work_mode")) or "unknown",
    )
    return {
        "canonical_concept": resolved.concept_id,
        "document_family": _text(semantic.get("document_family")) or "invoice",
        "line_family": _text(semantic.get("line_family")) or "unknown",
        "trade_family": _text(semantic.get("trade_family")) or "unknown",
        "work_mode": _text(semantic.get("work_mode")) or "unknown",
    }


def _versions(row: dict[str, Any]) -> ProcessingVersionSnapshot:
    meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
    facts = meta.get("document_facts") if isinstance(meta.get("document_facts"), dict) else {}
    decision = meta.get("accounting_decision") if isinstance(meta.get("accounting_decision"), dict) else {}
    return ProcessingVersionSnapshot(
        extractor_version=_optional(meta.get("extractor_version") or meta.get("extraction_version")),
        schema_version=_optional(facts.get("schema_version") or meta.get("schema_version")),
        prompt_version=_optional(meta.get("prompt_version")),
        provider=_optional(meta.get("extraction_provider") or meta.get("provider")),
        model_id=_optional(facts.get("extraction_model") or meta.get("extraction_model")),
        profile_id=_optional(meta.get("extraction_profile") or decision.get("decision_source")),
    )


def _confidence(row: dict[str, Any], field: str) -> float | None:
    meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
    value = (meta.get("ai_gl_accounting_confidence") if field == "GL Account"
             else meta.get("ai_confidence"))
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, number))


def _alternatives(row: dict[str, Any], field: str) -> list[Any]:
    meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
    if field == "GL Account":
        decision = meta.get("accounting_decision") if isinstance(meta.get("accounting_decision"), dict) else {}
        return [item.get("gl_code") for item in decision.get("candidates_ranked") or []
                if isinstance(item, dict) and item.get("gl_code")][:10]
    return [item.get("value") for item in meta.get("ai_unresolved_visual_field_candidates") or []
            if isinstance(item, dict) and item.get("field") == field][:10]


def _flatten_rows(result: dict[str, Any]) -> dict[int, tuple[str, int, dict[str, Any], dict[str, Any]]]:
    invoices = list(result.get("all_invoices") or [])
    identities = build_invoice_identities(invoices)
    output: dict[int, tuple[str, int, dict[str, Any], dict[str, Any]]] = {}
    index = 0
    for identity, invoice in zip(identities, invoices):
        for local_index, row in enumerate(invoice.get("rows") or []):
            meta = row.setdefault("_meta", {})
            meta.setdefault("invoice_group_id", identity.group_id)
            output[index] = (identity.group_id, local_index, invoice, row)
            index += 1
    return output


def _all_result_rows(result: dict[str, Any]):
    views: list[list[dict[str, Any]]] = [list(result.get("all_invoices") or [])]
    for payload in (result.get("by_vendor") or {}).values():
        if isinstance(payload, dict):
            views.append(list(payload.get("invoices") or []))
    for invoices in views:
        identities = build_invoice_identities(invoices)
        for identity, invoice in zip(identities, invoices):
            for local_index, row in enumerate(invoice.get("rows") or []):
                meta = row.setdefault("_meta", {})
                meta.setdefault("invoice_group_id", identity.group_id)
                yield identity.group_id, local_index, invoice, row


def _source_line_fingerprint(row: dict[str, Any]) -> str:
    meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
    source = meta.get("source_text") if isinstance(meta.get("source_text"), dict) else {}
    facts = meta.get("document_facts") if isinstance(meta.get("document_facts"), dict) else {}
    line_facts = (facts.get("line_items") or [{}])[0] if isinstance(facts.get("line_items"), list) else {}
    material = {
        "line_item_id": meta.get("line_item_id") or row.get("Line Item Number"),
        "source_file": Path(str(meta.get("source_file") or "")).name,
        "source_page": meta.get("source_page"),
        "raw_activity": source.get("raw_activity"),
        "raw_description": source.get("raw_description"),
        "amount": line_facts.get("amount") if isinstance(line_facts, dict) else None,
        "quantity": line_facts.get("quantity") if isinstance(line_facts, dict) else None,
        "unit_price": line_facts.get("unit_price") if isinstance(line_facts, dict) else None,
    }
    if all(value in (None, "") for value in material.values()):
        material = {"row": {key: row.get(key) for key in sorted(row) if key != "_meta"}}
    return _hash(material)


def _correction_key(tenant: str, batch: str, group: str, fingerprint: str, field: str) -> str:
    return _hash({"tenant": tenant, "batch": batch, "group": group,
                  "line": fingerprint, "field": field})


def _latest_by_key(items: list[HumanAdjudicationRevision]) -> dict[str, HumanAdjudicationRevision]:
    latest: dict[str, HumanAdjudicationRevision] = {}
    for item in sorted(items, key=lambda value: (value.created_at, value.revision_number)):
        latest[item.correction_key] = item
    return latest


def _revision_for_actor(revision_id: str, actor: ActorContext) -> HumanAdjudicationRevision:
    revision = next((item for item in list_revisions(actor.tenant_id)
                     if item.revision_id == revision_id), None)
    if revision is None:
        raise KeyError(revision_id)
    if revision.tenant_id != actor.tenant_id:
        raise PermissionError("A governance decision cannot cross tenant boundaries.")
    return revision


def _event(
    revision: HumanAdjudicationRevision, event_type: str, status: str,
    actor: ActorContext, details: dict[str, Any] | None = None,
) -> GovernanceEvent:
    return GovernanceEvent(
        event_id="hage_" + uuid.uuid4().hex[:16],
        tenant_id=actor.tenant_id,
        revision_id=revision.revision_id,
        event_type=event_type,
        status=status,
        actor=actor.reviewer_id,
        actor_role=actor.role,
        details=dict(details or {}),
        created_at=_now(),
    )


def _governance_by_revision(tenant_id: str) -> dict[str, list[GovernanceEvent]]:
    output: dict[str, list[GovernanceEvent]] = {}
    for item in list_governance_events(tenant_id):
        output.setdefault(item.revision_id, []).append(item)
    return output


def _root(tenant_id: str) -> Path:
    return settings.WEBAPP_DATA_ROOT / "human_adjudication" / validate_tenant_id(tenant_id)


def _revisions_path(tenant_id: str) -> Path:
    return _root(tenant_id) / "revisions.jsonl"


def _events_path(tenant_id: str) -> Path:
    return _root(tenant_id) / "governance_events.jsonl"


def _append_jsonl(path: Path, items: list[BaseModel]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK, path.open("a", encoding="utf-8") as handle:
        for item in items:
            handle.write(json.dumps(item.model_dump(mode="json"), ensure_ascii=False, default=str) + "\n")


def _read_jsonl(path: Path, model: type[BaseModel]) -> list[Any]:
    if not path.is_file():
        return []
    output: list[Any] = []
    with _LOCK:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
    for line in lines:
        try:
            output.append(model(**json.loads(line)))
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
    return output


def _trace_items(batch_id: str, source_file: str) -> list[dict[str, Any]]:
    import re
    from . import batch_store

    name = Path(source_file).name
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:200] or "unknown"
    path = batch_store.get_batch_dir(batch_id) / "trace" / f"{safe}.json"
    if not path.is_file():
        return []
    try:
        return list(json.loads(path.read_text(encoding="utf-8")).get("items") or [])
    except (OSError, ValueError, TypeError):
        return []


def _source_document_hash(batch_id: str, source_file: str | None) -> str | None:
    if not source_file:
        return None
    from . import batch_store
    path = batch_store.get_input_dir(batch_id) / Path(source_file).name
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, default=str,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _optional(value: Any) -> str | None:
    text = _text(value)
    return text or None


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


__all__ = [
    "ADJUDICATION_CONTRACT_VERSION", "ActorContext", "AdjudicationApplyReport",
    "AdjudicationOptions", "AuthorizationScope", "GovernanceEvent",
    "HumanAdjudicationRevision", "ReviewerRole", "apply_to_result",
    "approve_learning", "approve_rule", "approved_learning_candidates",
    "decide_benchmark", "list_governance_events", "list_revisions",
    "record_manual_edits", "runtime_actor_context", "source_evidence_for_cell",
]
