"""Tenant-isolated accounting knowledge assembled without collapsing authority.

The core is a read/aggregation boundary.  Historical observations, exact
benchmark truth, approved retrieval examples and governed policies remain
separate stores.  Only candidate evidence is exposed to production accounting;
benchmarks are evaluation-only and no method here selects a GL or authorizes an
export.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .. import settings
from . import context_intelligence, human_adjudication, tenant_accounting_policies
from .canonical_semantics import resolve_canonical_concept
from .tenant_accounting_policies import TenantPolicyStatus, validate_tenant_id


CONTRACT_VERSION = "accounting-knowledge-core/1.0"
HISTORICAL_PROFILE_VERSION = "historical-profile/1.0"
CORRECTION_LEDGER_VERSION = "human-correction-ledger/1.0"
BENCHMARK_STORE_VERSION = "benchmark-example/1.0"
LEARNING_STORE_VERSION = "approved-learning-example/1.0"
RULE_STORE_VERSION = "governed-rule/1.0"
FINAL_ACCOUNTING_VERSION = "final-accounting-event/1.0"
_LOCK = threading.RLock()


class KnowledgeProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    store: str
    contract_version: str
    source_id: str
    immutable: bool = True
    tenant_id: str


class HistoricalPrior(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimension: Literal["vendor", "property", "vendor_property"]
    gl_code: str
    count: int = Field(ge=0)
    amount: str
    share: float = Field(ge=0, le=1)
    snapshot_id: str
    authoritative: bool = False


class HumanCorrectionLedgerEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: str = CORRECTION_LEDGER_VERSION
    revision_id: str
    tenant_id: str
    batch_id: str
    invoice_group_id: str
    row_identity: str
    field: str
    original_posted_value: Any = None
    original_ai_value: Any = None
    corrected_human_value: Any = None
    final_approved_value: Any = None
    evidence_fingerprint: str
    reviewer_id: str
    rationale: str
    created_at: datetime


class BenchmarkExample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: str = BENCHMARK_STORE_VERSION
    benchmark_example_id: str
    revision_id: str
    tenant_id: str
    evidence_fingerprint: str
    field: str
    accepted_value: Any = None
    evaluation_only: bool = True
    immutable: bool = True


class ApprovedLearningExample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: str = LEARNING_STORE_VERSION
    learning_example_id: str
    revision_id: str
    tenant_id: str
    canonical_concept: str
    document_family: str
    line_family: str
    trade_family: str
    work_mode: str
    gl_code: str
    evidence_fingerprint: str
    candidate_only: bool = True


class GovernedRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: str = RULE_STORE_VERSION
    rule_id: str
    tenant_id: str
    version: int
    title: str
    status: str
    allowed_gl_codes: list[str]
    scope: dict[str, Any]
    simulation_id: str | None = None
    candidate_constraint_only: bool = True


class KnowledgeContradiction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    source_ids: list[str] = Field(default_factory=list)
    requires_review: bool = False


class LineKnowledgeContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: str = CONTRACT_VERSION
    tenant_id: str
    line_item_id: str
    canonical_concept: str | None = None
    document_evidence: dict[str, Any] = Field(default_factory=dict)
    historical_profile_state: Literal["ready", "stale", "not_generated", "unavailable"] = "not_generated"
    historical_vendor_priors: list[HistoricalPrior] = Field(default_factory=list)
    historical_property_priors: list[HistoricalPrior] = Field(default_factory=list)
    vendor_property_joint_priors: list[HistoricalPrior] = Field(default_factory=list)
    similar_approved_learning_examples: list[ApprovedLearningExample] = Field(default_factory=list)
    active_governed_rules: list[GovernedRule] = Field(default_factory=list)
    contradictions: list[KnowledgeContradiction] = Field(default_factory=list)
    confidence: float = Field(default=0, ge=0, le=1)
    provenance: list[KnowledgeProvenance] = Field(default_factory=list)
    benchmark_examples_visible_to_production: int = 0
    selection_authority: bool = False
    export_authority: bool = False


class KnowledgeImpactEstimate(BaseModel):
    contract_version: str = CONTRACT_VERSION
    invoice_corrections: int
    benchmark_examples: int
    learning_examples: int
    learning_duplicates_avoided: int
    rule_proposals: int
    affected_rows: int
    requires_bulk_scope_confirmation: bool
    statements: list[str]


class FinalAccountingEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: str = FINAL_ACCOUNTING_VERSION
    event_id: str
    event_key: str
    tenant_id: str
    batch_id: str
    invoice_id: str | None = None
    row_identity: str
    gl_code: str
    original_posted_value: Any = None
    original_ai_value: Any = None
    corrected_human_value: Any = None
    final_approved_value: Any = None
    readiness_snapshot_id: str
    event_type: Literal["approved_export", "posted"] = "approved_export"
    created_at: datetime


class KnowledgeAnalytics(BaseModel):
    contract_version: str = CONTRACT_VERSION
    tenant_id: str
    historical_gl_distribution: dict[str, int]
    approved_export_gl_distribution: dict[str, int]
    posted_gl_distribution: dict[str, int]
    # Backward-compatible adapter.  This now means acknowledged posting only;
    # approved exports are intentionally reported by the separate field above.
    final_posted_gl_distribution: dict[str, int]
    ai_prediction_distribution: dict[str, int]
    human_correction_distribution: dict[str, int]
    disagreement_rate: float
    approved_benchmark_count: int
    approved_learning_count: int
    active_rule_count: int
    rule_coverage: float
    correction_drift_over_time: list[dict[str, Any]]
    promotion_thresholds: dict[str, Any]
    promotion_candidates: list[dict[str, Any]]
    provenance: list[KnowledgeProvenance]


class PromotionThresholds(BaseModel):
    min_repeated_corrections: int = Field(default=3, ge=2, le=100)
    min_distinct_invoices: int = Field(default=3, ge=2, le=100)
    min_learning_examples_for_rule: int = Field(default=2, ge=1, le=100)

    @classmethod
    def from_environment(cls) -> "PromotionThresholds":
        return cls(
            min_repeated_corrections=int(os.environ.get("KNOWLEDGE_MIN_REPEATED_CORRECTIONS", "3")),
            min_distinct_invoices=int(os.environ.get("KNOWLEDGE_MIN_DISTINCT_INVOICES", "3")),
            min_learning_examples_for_rule=int(os.environ.get("KNOWLEDGE_MIN_LEARNING_FOR_RULE", "2")),
        )


class HistoricalProfileStore:
    """Read-only adapter over immutable Cross-Report snapshots."""

    def priors(
        self, tenant_id: str, vendor: str | None, property_code: str | None,
    ) -> tuple[
        list[HistoricalPrior], list[HistoricalPrior], list[HistoricalPrior],
        Literal["ready", "stale", "not_generated", "unavailable"],
    ]:
        tenant_id = validate_tenant_id(tenant_id)
        try:
            raw_state = str(context_intelligence.status(tenant_id).get("state") or "unavailable")
        except (FileNotFoundError, KeyError, OSError, ValueError):
            raw_state = "unavailable"
        state: Literal["ready", "stale", "not_generated", "unavailable"] = (
            raw_state if raw_state in {"ready", "stale", "not_generated"} else "unavailable"
        )  # type: ignore[assignment]
        # A stale snapshot is still auditable history, but it must never feed a
        # current production candidate.  Missing history is neutral for novel vendors.
        if state != "ready":
            return [], [], [], state
        vendor_items = context_intelligence.historical_gl_evidence(tenant_id, vendor, None)
        joint_items = context_intelligence.historical_gl_evidence(tenant_id, vendor, property_code)
        property_items: list[dict[str, Any]] = []
        if property_code:
            try:
                page = context_intelligence.list_matrix(
                    tenant_id, dimension="properties", search=property_code, page_size=25,
                )
                profile = next((item for item in page.items if _norm(item.get("property_code")) == _norm(property_code)
                                or _norm(item.get("property_name")) == _norm(property_code)), None)
                if profile:
                    snapshot_id = page.snapshot_id
                    property_items = [{
                        "gl_code": item.get("key"), "count": item.get("count", 0),
                        "amount": item.get("amount", "0.00"), "share": item.get("share", 0),
                        "snapshot_id": snapshot_id,
                    } for item in profile.get("gl_usage") or []]
            except (FileNotFoundError, KeyError, ValueError):
                property_items = []
        return (
            _historical_models("vendor", vendor_items),
            _historical_models("property", property_items),
            _historical_models("vendor_property", joint_items if property_code else []),
            state,
        )


class HumanCorrectionLedgerStore:
    """Append-only view over immutable adjudication revisions."""

    def entries(self, tenant_id: str) -> list[HumanCorrectionLedgerEntry]:
        return [_ledger_entry(item) for item in human_adjudication.list_revisions(validate_tenant_id(tenant_id))]


class BenchmarkExampleStore:
    """Evaluation-only ground truth; never queried by candidate assembly."""

    def approved(self, tenant_id: str) -> list[BenchmarkExample]:
        tenant_id = validate_tenant_id(tenant_id)
        events = human_adjudication.list_governance_events(tenant_id)
        approved = {event.revision_id for event in events
                    if event.event_type == "benchmark_approved" and event.status == "approved"}
        return [BenchmarkExample(
            benchmark_example_id="bench_" + revision.revision_id,
            revision_id=revision.revision_id, tenant_id=tenant_id,
            evidence_fingerprint=revision.evidence.evidence_fingerprint,
            field=revision.field, accepted_value=revision.corrected_value,
        ) for revision in human_adjudication.list_revisions(tenant_id)
            if revision.revision_id in approved]


class ApprovedLearningExampleStore:
    def similar(self, *, tenant_id: str, canonical_concept: str | None,
                document_family: str, line_family: str, trade_family: str,
                work_mode: str) -> list[ApprovedLearningExample]:
        raw = human_adjudication.approved_learning_candidates(
            tenant_id=validate_tenant_id(tenant_id), canonical_concept=canonical_concept,
            document_family=document_family, line_family=line_family,
            trade_family=trade_family, work_mode=work_mode,
        )
        seen: set[tuple[str, str, str, str, str, str]] = set()
        output: list[ApprovedLearningExample] = []
        for item in raw:
            key = (
                str(item.get("canonical_concept") or ""),
                str(item.get("document_family") or ""),
                str(item.get("line_family") or ""),
                str(item.get("trade_family") or ""),
                str(item.get("work_mode") or ""),
                str(item.get("gl_code") or ""),
            )
            if key in seen or not all(key):
                continue
            seen.add(key)
            output.append(ApprovedLearningExample(
                learning_example_id="learn_" + str(item.get("revision_id")),
                revision_id=str(item.get("revision_id")), tenant_id=tenant_id,
                canonical_concept=key[0], document_family=key[1], line_family=key[2],
                trade_family=key[3], work_mode=key[4], gl_code=key[5],
                evidence_fingerprint=str(item.get("evidence_fingerprint") or ""),
            ))
        return output


class GovernedRuleStore:
    def matching(self, tenant_id: str, row: dict[str, Any], semantics: dict[str, Any]) -> list[GovernedRule]:
        raw = tenant_accounting_policies.active_policy_evidence(
            validate_tenant_id(tenant_id), row=row, semantics_payload=semantics,
        )
        return [GovernedRule.model_validate(item) for item in raw]


class AccountingKnowledgeCore:
    def __init__(self) -> None:
        self.historical = HistoricalProfileStore()
        self.corrections = HumanCorrectionLedgerStore()
        self.benchmarks = BenchmarkExampleStore()
        self.learning = ApprovedLearningExampleStore()
        self.rules = GovernedRuleStore()

    def line_context(self, *, tenant_id: str, row: dict[str, Any],
                     semantics_payload: dict[str, Any] | None = None,
                     canonical_concept: str | None = None) -> LineKnowledgeContext:
        tenant_id = validate_tenant_id(tenant_id)
        row_tenant = tenant_accounting_policies.tenant_id_for_row(row)
        if row_tenant != tenant_id:
            raise PermissionError("Accounting knowledge cannot cross tenant boundaries.")
        meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
        semantics = semantics_payload or (
            meta.get("semantic_classification") if isinstance(meta.get("semantic_classification"), dict) else {}
        )
        source = meta.get("source_text") if isinstance(meta.get("source_text"), dict) else {}
        text = str(meta.get("normalized_source_description") or source.get("raw_description")
                   or row.get("Line Item Description") or "")
        canonical = resolve_canonical_concept(
            text, line_family=str(semantics.get("line_family") or "unknown"),
            trade_family=str(semantics.get("trade_family") or "unknown"),
            work_mode=str(semantics.get("work_mode") or "unknown"),
        )
        vendor, prop, joint, historical_state = self.historical.priors(
            tenant_id, _optional(row.get("Vendor")), _optional(row.get("Property Abbreviation")),
        )
        learning = self.learning.similar(
            tenant_id=tenant_id, canonical_concept=canonical_concept or canonical.concept_id,
            document_family=str(semantics.get("document_family") or "unknown"),
            line_family=str(semantics.get("line_family") or "unknown"),
            trade_family=str(semantics.get("trade_family") or "unknown"),
            work_mode=str(semantics.get("work_mode") or "unknown"),
        )
        rules = self.rules.matching(tenant_id, row, semantics)
        contradictions = _contradictions(vendor, prop, joint, learning, rules)
        if historical_state == "stale":
            contradictions.append(KnowledgeContradiction(
                code="historical_profile_stale",
                message="Historical priors are stale and were excluded from candidate evidence.",
                requires_review=False,
            ))
        provenance: list[KnowledgeProvenance] = []
        for prior in [*vendor, *prop, *joint]:
            item = KnowledgeProvenance(store="HistoricalProfile", contract_version=HISTORICAL_PROFILE_VERSION,
                source_id=prior.snapshot_id, tenant_id=tenant_id)
            if item not in provenance:
                provenance.append(item)
        provenance.extend(KnowledgeProvenance(store="ApprovedLearningExample",
            contract_version=LEARNING_STORE_VERSION, source_id=item.revision_id,
            tenant_id=tenant_id) for item in learning)
        provenance.extend(KnowledgeProvenance(store="GovernedRule", contract_version=RULE_STORE_VERSION,
            source_id=item.rule_id, tenant_id=tenant_id) for item in rules)
        evidence = {
            "source_text": source,
            "document_facts": meta.get("document_facts") if isinstance(meta.get("document_facts"), dict) else {},
            "trace_ids": list(meta.get("trace_ids") or []),
            "source_page": meta.get("source_page"),
            "immutable": True,
        }
        confidence_inputs = [item.share for item in [*joint, *vendor, *prop][:6]]
        confidence_inputs.extend(1.0 for _ in learning)
        confidence_inputs.extend(1.0 for _ in rules)
        confidence = (sum(confidence_inputs) / len(confidence_inputs)) if confidence_inputs else 0.0
        if contradictions:
            confidence *= 0.65
        return LineKnowledgeContext(
            tenant_id=tenant_id,
            line_item_id=str(meta.get("line_item_id") or row.get("Line Item Number") or "unknown"),
            canonical_concept=canonical_concept or canonical.concept_id,
            document_evidence=evidence,
            historical_profile_state=historical_state,
            historical_vendor_priors=vendor,
            historical_property_priors=prop,
            vendor_property_joint_priors=joint,
            similar_approved_learning_examples=learning,
            active_governed_rules=rules,
            contradictions=contradictions,
            confidence=max(0.0, min(1.0, confidence)),
            provenance=provenance,
        )

    def impact(self, *, tenant_id: str, rows: list[dict[str, Any]],
               edits_by_index: dict[int, dict[str, Any]], add_to_benchmark: bool,
               approve_learning: bool, propose_rule: bool) -> KnowledgeImpactEstimate:
        validate_tenant_id(tenant_id)
        correction_count = sum(len(fields) for fields in edits_by_index.values())
        learning_keys: set[tuple[str, str, str]] = set()
        gl_changes = 0
        affected = 0
        for index, fields in edits_by_index.items():
            if index < 0 or index >= len(rows):
                continue
            affected += 1
            context = self.line_context(tenant_id=tenant_id, row=rows[index])
            if "GL Account" in fields:
                gl_changes += 1
                learning_keys.add((context.canonical_concept or "unknown", "GL Account", str(fields["GL Account"])))
        learning_examples = len(learning_keys) if approve_learning else 0
        return KnowledgeImpactEstimate(
            invoice_corrections=correction_count,
            benchmark_examples=correction_count if add_to_benchmark else 0,
            learning_examples=learning_examples,
            learning_duplicates_avoided=max(0, gl_changes - learning_examples) if approve_learning else 0,
            rule_proposals=(len(learning_keys) if propose_rule else 0),
            affected_rows=affected,
            requires_bulk_scope_confirmation=(affected > 1 and any((add_to_benchmark, approve_learning, propose_rule))),
            statements=[
                "Invoice corrections affect only this tenant and batch overlay.",
                "Benchmark entries are immutable evaluation data and have zero production effect.",
                "Learning examples add candidate evidence only; AccountingDecisionEngine remains final authority.",
                "Rule proposals require simulation and authorized approval before constraining candidates.",
            ],
        )

    def analytics(self, tenant_id: str) -> KnowledgeAnalytics:
        tenant_id = validate_tenant_id(tenant_id)
        historical = Counter()
        provenance: list[KnowledgeProvenance] = []
        try:
            if context_intelligence.status(tenant_id).get("state") != "ready":
                raise FileNotFoundError("Historical profile is unavailable or stale.")
            page = context_intelligence.list_matrix(tenant_id, dimension="vendors", page=1, page_size=250)
            profiles = list(page.items)
            page_number = 2
            while len(profiles) < page.total:
                next_page = context_intelligence.list_matrix(
                    tenant_id, dimension="vendors", page=page_number, page_size=250,
                )
                if not next_page.items:
                    break
                profiles.extend(next_page.items)
                page_number += 1
            for profile in profiles:
                for item in profile.get("gl_usage") or []:
                    historical[str(item.get("key"))] += int(item.get("count") or 0)
            provenance.append(KnowledgeProvenance(store="HistoricalProfile",
                contract_version=HISTORICAL_PROFILE_VERSION, source_id=page.snapshot_id,
                tenant_id=tenant_id))
            allocation_count = sum(int(item.get("allocation_count") or 0) for item in profiles)
        except (FileNotFoundError, KeyError, ValueError):
            allocation_count = 0
        entries = self.corrections.entries(tenant_id)
        ai = Counter(str(item.original_ai_value) for item in entries
                     if item.field == "GL Account" and item.original_ai_value not in (None, ""))
        human = Counter(str(item.corrected_human_value) for item in entries
                        if item.field == "GL Account" and item.corrected_human_value not in (None, ""))
        comparable_gl_entries = [
            item for item in entries
            if item.field == "GL Account"
            and item.original_ai_value not in (None, "")
            and item.corrected_human_value not in (None, "")
        ]
        disagreements = sum(
            str(item.original_ai_value).strip() != str(item.corrected_human_value).strip()
            for item in comparable_gl_entries
        )
        events = human_adjudication.list_governance_events(tenant_id)
        revisions_by_id = {item.revision_id: item for item in human_adjudication.list_revisions(tenant_id)}
        benchmark_count = len({
            (
                revisions_by_id[item.revision_id].evidence.evidence_fingerprint,
                revisions_by_id[item.revision_id].field,
                _text_value(revisions_by_id[item.revision_id].corrected_value),
            )
            for item in events
            if item.event_type == "benchmark_approved" and item.status == "approved"
            and item.revision_id in revisions_by_id
        })
        learning_count = len({
            _learning_identity(revisions_by_id[item.revision_id])
            for item in events
            if item.event_type == "learning_approved" and item.status == "approved"
            and item.revision_id in revisions_by_id
        })
        policies = [item for item in tenant_accounting_policies.list_policies(tenant_id, include_rejected=False)
                    if item.status is TenantPolicyStatus.ACTIVE]
        matched = sum((item.latest_simulation.matched_lines if item.latest_simulation else 0) for item in policies)
        finalized = _read_final_events(tenant_id)
        approved_export_dist = Counter(
            item.gl_code for item in finalized if item.event_type == "approved_export"
        )
        posted_dist = Counter(item.gl_code for item in finalized if item.event_type == "posted")
        by_month = Counter(item.created_at.strftime("%Y-%m") for item in entries)
        thresholds = PromotionThresholds.from_environment()
        promotion_groups: dict[tuple[str, str], list[HumanCorrectionLedgerEntry]] = {}
        for entry in entries:
            revision = revisions_by_id.get(entry.revision_id)
            if entry.field == "GL Account" and revision and revision.canonical_concept:
                promotion_groups.setdefault(
                    (revision.canonical_concept, str(entry.corrected_human_value)), [],
                ).append(entry)
        approved_learning_ids = {item.revision_id for item in events
                                 if item.event_type == "learning_approved" and item.status == "approved"}
        promotion_candidates = []
        for (concept, gl_code), grouped in sorted(promotion_groups.items()):
            invoices = {item.invoice_group_id for item in grouped}
            approved_count = sum(item.revision_id in approved_learning_ids for item in grouped)
            promotion_candidates.append({
                "canonical_concept": concept, "gl_code": gl_code,
                "correction_count": len(grouped), "distinct_invoice_count": len(invoices),
                "approved_learning_count": approved_count,
                "eligible_for_learning_review": (
                    len(grouped) >= thresholds.min_repeated_corrections
                    and len(invoices) >= thresholds.min_distinct_invoices
                ),
                "eligible_for_rule_simulation": approved_count >= thresholds.min_learning_examples_for_rule,
                "automatic_promotion": False,
            })
        return KnowledgeAnalytics(
            tenant_id=tenant_id,
            historical_gl_distribution=dict(sorted(historical.items())),
            approved_export_gl_distribution=dict(sorted(approved_export_dist.items())),
            posted_gl_distribution=dict(sorted(posted_dist.items())),
            final_posted_gl_distribution=dict(sorted(posted_dist.items())),
            ai_prediction_distribution=dict(sorted(ai.items())),
            human_correction_distribution=dict(sorted(human.items())),
            disagreement_rate=(disagreements / len(comparable_gl_entries) if comparable_gl_entries else 0.0),
            approved_benchmark_count=benchmark_count,
            approved_learning_count=learning_count,
            active_rule_count=len(policies),
            rule_coverage=(min(1.0, matched / allocation_count) if allocation_count else 0.0),
            correction_drift_over_time=[{
                "window_type": "calendar_month_utc",
                "window_start": f"{month}-01T00:00:00Z",
                "month": month,
                "corrections": count,
            } for month, count in sorted(by_month.items())],
            promotion_thresholds=thresholds.model_dump(mode="json"),
            promotion_candidates=promotion_candidates,
            provenance=provenance,
        )


def record_approved_export(*, tenant_id: str, batch_id: str,
                           rows: list[dict[str, Any]], readiness: dict[str, Any]) -> int:
    """Append final values only after a readiness-authorized workbook write."""
    tenant_id = validate_tenant_id(tenant_id)
    if not isinstance(readiness, dict):
        raise ValueError("A complete AccountingReadiness payload is required.")
    if readiness.get("export_allowed") is not True:
        raise ValueError("Final accounting events require export_allowed=true.")
    readiness_snapshot_id = str(readiness.get("snapshot_id") or "").strip()
    if not readiness_snapshot_id or not str(readiness.get("contract_version") or "").strip():
        raise ValueError("AccountingReadiness contract_version and snapshot_id are required.")
    # Keep the idempotency check and append in the same critical section so
    # concurrent export retries cannot duplicate a final-value event.
    with _LOCK:
        existing = {item.event_key for item in _read_final_events(tenant_id)}
        items: list[FinalAccountingEvent] = []
        for index, row in enumerate(rows):
            if tenant_accounting_policies.tenant_id_for_row(row) != tenant_id:
                raise PermissionError("Final accounting events cannot cross tenants.")
            gl_code = str(row.get("GL Account") or "").strip()
            if not gl_code:
                continue
            meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
            identity = str(meta.get("line_item_id") or row.get("Line Item Number") or index)
            key = _hash([tenant_id, batch_id, identity, gl_code, readiness_snapshot_id])
            if key in existing:
                continue
            applied = meta.get("human_adjudication_applied") if isinstance(meta.get("human_adjudication_applied"), dict) else {}
            gl_revision = applied.get("GL Account") if isinstance(applied.get("GL Account"), dict) else {}
            items.append(FinalAccountingEvent(
                event_id="fae_" + uuid.uuid4().hex[:16], event_key=key, tenant_id=tenant_id,
                batch_id=batch_id, invoice_id=_optional(row.get("Invoice Number")),
                row_identity=identity, gl_code=gl_code,
                original_posted_value=meta.get("original_posted_values", {}).get("GL Account")
                    if isinstance(meta.get("original_posted_values"), dict) else None,
                original_ai_value=gl_revision.get("original_ai_value"),
                corrected_human_value=gl_revision.get("corrected_value"),
                final_approved_value=gl_code, readiness_snapshot_id=readiness_snapshot_id,
                created_at=_now(),
            ))
            existing.add(key)
        if items:
            _append_final_events(tenant_id, items)
        return len(items)


def _historical_models(dimension: str, items: list[dict[str, Any]]) -> list[HistoricalPrior]:
    output: list[HistoricalPrior] = []
    for item in items:
        code = str(item.get("gl_code") or "").strip()
        snapshot = str(item.get("snapshot_id") or "").strip()
        if code and snapshot:
            output.append(HistoricalPrior(
                dimension=dimension, gl_code=code, count=int(item.get("count") or 0),
                amount=str(item.get("amount") or "0.00"), share=float(item.get("share") or 0),
                snapshot_id=snapshot,
            ))
    return output


def _ledger_entry(item: human_adjudication.HumanAdjudicationRevision) -> HumanCorrectionLedgerEntry:
    return HumanCorrectionLedgerEntry(
        revision_id=item.revision_id, tenant_id=item.tenant_id, batch_id=item.batch_id,
        invoice_group_id=item.invoice_group_id, row_identity=item.source_line_fingerprint,
        field=item.field, original_posted_value=getattr(item, "original_posted_value", None),
        original_ai_value=item.original_ai_value, corrected_human_value=item.corrected_value,
        final_approved_value=getattr(item, "final_approved_value", None),
        evidence_fingerprint=item.evidence.evidence_fingerprint,
        reviewer_id=item.reviewer_id, rationale=item.rationale, created_at=item.created_at,
    )


def _text_value(value: Any) -> str:
    return " ".join(str(value or "").split())


def _learning_identity(
    item: human_adjudication.HumanAdjudicationRevision,
) -> tuple[str, str, str, str, str, str, str]:
    """Semantic identity used for counts and retrieval deduplication."""
    return (
        item.field,
        item.document_family or "",
        item.canonical_concept or "",
        item.line_family or "",
        item.trade_family or "",
        item.work_mode or "",
        _text_value(item.corrected_value),
    )


def _contradictions(vendor: list[HistoricalPrior], prop: list[HistoricalPrior],
                    joint: list[HistoricalPrior], learning: list[ApprovedLearningExample],
                    rules: list[GovernedRule]) -> list[KnowledgeContradiction]:
    output: list[KnowledgeContradiction] = []
    tops = [("vendor", vendor[0].gl_code if vendor else None),
            ("property", prop[0].gl_code if prop else None),
            ("vendor_property", joint[0].gl_code if joint else None)]
    values = {value for _, value in tops if value}
    if len(values) > 1:
        output.append(KnowledgeContradiction(code="historical_priors_disagree",
            message="Vendor, property and joint historical frequencies do not agree.",
            source_ids=sorted(values), requires_review=False))
    allowed = set.intersection(*(set(rule.allowed_gl_codes) for rule in rules)) if rules else set()
    evidence_codes = values | {item.gl_code for item in learning}
    if rules and not allowed:
        output.append(KnowledgeContradiction(code="active_governed_rules_conflict",
            message="Matching active governed rules have no common allowed GL.",
            source_ids=[item.rule_id for item in rules], requires_review=True))
    incompatible = sorted(evidence_codes - allowed) if allowed else []
    if incompatible and rules:
        output.append(KnowledgeContradiction(code="governed_rule_conflicts_with_observed_evidence",
            message="Observed history or approved examples include GLs outside the active governed constraint.",
            source_ids=incompatible, requires_review=True))
    return output


def _final_path(tenant_id: str) -> Path:
    return settings.WEBAPP_DATA_ROOT / "accounting_knowledge_core" / validate_tenant_id(tenant_id) / "final_accounting_events.jsonl"


def _read_final_events(tenant_id: str) -> list[FinalAccountingEvent]:
    path = _final_path(tenant_id)
    if not path.is_file():
        return []
    output = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            output.append(FinalAccountingEvent.model_validate_json(line))
    return output


def _append_final_events(tenant_id: str, items: list[FinalAccountingEvent]) -> None:
    path = _final_path(tenant_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK, path.open("a", encoding="utf-8") as handle:
        for item in items:
            handle.write(item.model_dump_json() + "\n")


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _norm(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _optional(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "AccountingKnowledgeCore", "ApprovedLearningExample", "BenchmarkExample",
    "FinalAccountingEvent", "GovernedRule", "HistoricalPrior",
    "HumanCorrectionLedgerEntry", "KnowledgeAnalytics", "KnowledgeImpactEstimate",
    "LineKnowledgeContext", "record_approved_export",
]
