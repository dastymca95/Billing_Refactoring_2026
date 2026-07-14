"""Phase 3.5 representative accounting benchmark contracts and metrics."""
from __future__ import annotations

import hashlib
import json
import os
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping

from pydantic import BaseModel, Field, model_validator


class LabelStatus(str, Enum):
    UNLABELED = "unlabeled"
    PARTIAL = "partial"
    GOLD = "gold"


class AdjudicationStatus(str, Enum):
    PENDING_SECOND_REVIEW = "pending_second_review"
    CONFLICT = "conflict"
    ADJUDICATED = "adjudicated_gold"


class LineLabel(BaseModel):
    line_item_id: str
    raw_description_redacted: str | None = None
    amount: str | None = None
    location_redacted: str | None = None
    line_family: str
    trade_family: str
    work_mode: str
    expected_gl: str | None = None
    acceptable_gl_alternatives: list[str] = Field(default_factory=list)


class ReviewerLabel(BaseModel):
    reviewer_id: str
    labeled_at: str
    label_confidence: float = Field(ge=0, le=1)
    document_family: str
    vendor_redacted: str | None = None
    property_redacted: str | None = None
    invoice_number_redacted: str | None = None
    invoice_date: str | None = None
    due_date: str | None = None
    total_amount: str | None = None
    line_items: list[LineLabel] = Field(default_factory=list)
    should_review: bool
    should_block: bool


class BenchmarkLabel(BaseModel):
    schema_version: str = "representative-label/1.0"
    case_id: str
    status: LabelStatus
    document_class: str
    known_vendor: bool
    complexity: str
    value_cohort: str
    first_review: ReviewerLabel | None = None
    second_review: ReviewerLabel | None = None
    adjudication_status: AdjudicationStatus
    adjudicated_gold: ReviewerLabel | None = None

    @model_validator(mode="after")
    def validate_workflow(self):
        if self.status is LabelStatus.GOLD:
            if not self.first_review or not self.second_review or not self.adjudicated_gold:
                raise ValueError("gold labels require first review, second review, and adjudicated gold")
            if self.adjudication_status is not AdjudicationStatus.ADJUDICATED:
                raise ValueError("gold labels must be adjudicated")
        if self.adjudication_status is AdjudicationStatus.CONFLICT and not self.second_review:
            raise ValueError("conflict requires a second review")
        return self


class DatasetEntry(BaseModel):
    case_id: str
    document_class: str
    source: str
    document_ref: str
    label_ref: str
    private: bool
    content_sha256: str | None = None

    @model_validator(mode="after")
    def validate_reference(self):
        ref = Path(self.document_ref)
        if ref.is_absolute() or ".." in ref.parts:
            raise ValueError("document_ref must be a safe relative reference")
        if self.private and not self.document_ref.startswith("private/"):
            raise ValueError("private documents must use private/ references")
        return self


class RepresentativeManifest(BaseModel):
    schema_version: str = "representative-manifest/1.0"
    entries: list[DatasetEntry]

    @model_validator(mode="after")
    def unique_cases(self):
        ids = [entry.case_id for entry in self.entries]
        if len(ids) != len(set(ids)):
            raise ValueError("case_id values must be unique")
        return self


def private_root() -> Path | None:
    raw = os.environ.get("INNER_VIEW_PRIVATE_BENCHMARK_ROOT", "").strip()
    return Path(raw).resolve() if raw else None


def resolve_document(entry: DatasetEntry, public_root: Path) -> Path:
    if entry.private:
        root = private_root()
        if root is None:
            raise FileNotFoundError("INNER_VIEW_PRIVATE_BENCHMARK_ROOT is not configured")
        path = (root / Path(entry.document_ref).relative_to("private")).resolve()
        path.relative_to(root)
    else:
        path = (public_root / entry.document_ref).resolve()
        path.relative_to(public_root.resolve())
    return path


@dataclass(frozen=True)
class EvaluationRecord:
    case_id: str
    document_class: str
    known_vendor: bool
    complexity: str
    value_cohort: str
    route_expected: str
    route_actual: str
    fields_correct: Mapping[str, bool]
    semantics_correct: Mapping[str, bool]
    expected_gls: tuple[str, ...]
    ranked_gls: tuple[str, ...]
    should_review: bool
    review_actual: bool
    should_block: bool
    blocked_actual: bool
    ready_actual: bool
    decision_confidence: float | None
    ai_calls: int
    latency_ms: int
    cost_usd: float
    error: str | None = None


def summarize(records: Iterable[EvaluationRecord]) -> dict[str, Any]:
    rows = list(records)
    high_confidence_errors = []
    for row in rows:
        gl_ok = _top_k(row, 1)
        if row.decision_confidence is not None and row.decision_confidence >= .85 and not gl_ok:
            high_confidence_errors.append({"case_id": row.case_id, "confidence": row.decision_confidence,
                                           "expected_gls": list(row.expected_gls), "ranked_gls": list(row.ranked_gls)})
    summary = {
        "schema_version": "representative-results/1.0",
        "documents": len(rows),
        "field_accuracy": _accuracy_map(rows, "fields_correct"),
        "semantic_accuracy": _accuracy_map(rows, "semantics_correct"),
        "gl_top_1": _mean([_top_k(row, 1) for row in rows]),
        "gl_top_3": _mean([_top_k(row, 3) for row in rows]),
        "false_ready_rate": _mean([row.ready_actual and row.should_block for row in rows]),
        "false_block_rate": _mean([row.blocked_actual and not row.should_block for row in rows]),
        "review_precision": _ratio(sum(row.review_actual and row.should_review for row in rows),
                                    sum(row.review_actual for row in rows)),
        "review_recall": _ratio(sum(row.review_actual and row.should_review for row in rows),
                                 sum(row.should_review for row in rows)),
        "route_accuracy": _mean([row.route_actual == row.route_expected for row in rows]),
        "ai_calls": sum(row.ai_calls for row in rows),
        "latency_ms_p50": _percentile([row.latency_ms for row in rows], .50),
        "latency_ms_p95": _percentile([row.latency_ms for row in rows], .95),
        "cost_per_document_usd": _ratio(sum(row.cost_usd for row in rows), len(rows)),
        "cost_per_successful_gl_usd": _ratio(sum(row.cost_usd for row in rows),
                                              sum(_top_k(row, 1) for row in rows)),
        "high_confidence_errors": high_confidence_errors,
        "processing_failures": sum(row.error is not None for row in rows),
    }
    summary["cohorts"] = cohort_analysis(rows)
    return summary


def cohort_analysis(rows: list[EvaluationRecord]) -> dict[str, Any]:
    dimensions = {
        "document_class": lambda row: row.document_class,
        "vendor": lambda row: "known" if row.known_vendor else "unknown",
        "complexity": lambda row: row.complexity,
        "route": lambda row: "ai" if row.route_actual.startswith("ai_") else "deterministic",
        "work": lambda row: _work_cohort(row),
        "value": lambda row: row.value_cohort,
    }
    output: dict[str, Any] = {}
    for dimension, getter in dimensions.items():
        groups: dict[str, list[EvaluationRecord]] = defaultdict(list)
        for row in rows:
            groups[getter(row)].append(row)
        output[dimension] = {key: {"documents": len(group), "gl_top_1": _mean([_top_k(r, 1) for r in group]),
                                           "false_ready_rate": _mean([r.ready_actual and r.should_block for r in group]),
                                           "latency_ms_p50": _percentile([r.latency_ms for r in group], .5)}
                             for key, group in sorted(groups.items())}
    return output


def _work_cohort(row: EvaluationRecord) -> str:
    return "materials" if "material" in row.document_class.lower() else "service"


def _top_k(row: EvaluationRecord, k: int) -> bool:
    accepted = set(row.expected_gls)
    return bool(accepted.intersection(row.ranked_gls[:k]))


def _accuracy_map(rows: list[EvaluationRecord], attr: str) -> dict[str, float | None]:
    keys = sorted({key for row in rows for key in getattr(row, attr)})
    return {key: _mean([getattr(row, attr)[key] for row in rows if key in getattr(row, attr)]) for key in keys}


def _mean(values: list[bool | int | float]) -> float | None:
    return sum(values) / len(values) if values else None


def _ratio(numerator: int | float, denominator: int | float) -> float | None:
    return numerator / denominator if denominator else None


def _percentile(values: list[int], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * fraction)))
    return float(ordered[index])
