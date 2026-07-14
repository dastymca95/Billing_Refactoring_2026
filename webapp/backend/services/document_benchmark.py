"""Sanitized, model-neutral document reasoning benchmark framework."""
from __future__ import annotations

import json
import statistics
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


FIELDS = ("vendor", "invoice_number", "invoice_date", "due_date", "property", "location",
          "line_item_count", "amount", "total_reconciled", "document_family")


@dataclass(frozen=True)
class BenchmarkCase:
    case_id: str
    document_class: str
    document_path: str
    expected_path: str
    sanitized: bool
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    route: str
    model_id: str | None
    field_correct: Mapping[str, bool]
    gl_total: int
    gl_filled: int
    gl_correct: int
    false_ready: bool
    review_expected: bool
    review_actual: bool
    processing_time_ms: int
    estimated_cost_usd: float
    error: str | None = None


def load_manifest(path: Path) -> list[BenchmarkCase]:
    data = json.loads(path.read_text(encoding="utf-8"))
    cases = [BenchmarkCase(**item) for item in data.get("cases", [])]
    if any(not case.sanitized for case in cases):
        raise ValueError("benchmark manifests may only reference sanitized cases")
    if len({case.case_id for case in cases}) != len(cases):
        raise ValueError("benchmark case_id values must be unique")
    return cases


def summarize(results: Iterable[CaseResult]) -> dict[str, Any]:
    rows = list(results)
    total_fields = {field: 0 for field in FIELDS}
    correct_fields = {field: 0 for field in FIELDS}
    for row in rows:
        for field in FIELDS:
            if field in row.field_correct:
                total_fields[field] += 1
                correct_fields[field] += int(row.field_correct[field])
    gl_total = sum(row.gl_total for row in rows)
    gl_filled = sum(row.gl_filled for row in rows)
    gl_correct = sum(row.gl_correct for row in rows)
    expected_reviews = sum(row.review_expected for row in rows)
    true_reviews = sum(row.review_expected and row.review_actual for row in rows)
    return {
        "schema_version": "document-benchmark/1.0",
        "cases": len(rows),
        "field_accuracy": {field: (correct_fields[field] / total_fields[field] if total_fields[field] else None)
                           for field in FIELDS},
        "gl_fill_rate": gl_filled / gl_total if gl_total else None,
        "gl_correctness_rate": gl_correct / gl_total if gl_total else None,
        "blank_gl_rate": (gl_total - gl_filled) / gl_total if gl_total else None,
        "review_recall": true_reviews / expected_reviews if expected_reviews else None,
        "false_ready_rate": sum(row.false_ready for row in rows) / len(rows) if rows else None,
        "processing_time_ms_p50": statistics.median([row.processing_time_ms for row in rows]) if rows else None,
        "estimated_cost_usd": round(sum(row.estimated_cost_usd for row in rows), 6),
        "routes": {route: sum(row.route == route for row in rows) for route in sorted({row.route for row in rows})},
        "failures": sum(row.error is not None for row in rows),
    }


def compare(current: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    keys = ("gl_fill_rate", "gl_correctness_rate", "blank_gl_rate", "false_ready_rate",
            "processing_time_ms_p50", "estimated_cost_usd", "failures")
    return {"schema_version": "document-benchmark-comparison/1.0",
            "delta": {key: _delta(current.get(key), candidate.get(key)) for key in keys}}


def _delta(left: Any, right: Any) -> float | None:
    if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
        return None
    return right - left


def write_results(path: Path, results: Iterable[CaseResult], summary: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"results": [asdict(item) for item in results], "summary": dict(summary)}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
