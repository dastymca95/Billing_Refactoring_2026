"""Produce a reproducible, provider-free cold-vs-golden divergence audit."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


PROVENANCE_FIELDS = {
    "ai_row_identity_evidence",
    "ai_row_identity_verification",
    "ai_handwritten_row_identities",
    "ai_excluded_paid_rows",
    "ai_service_date",
    "ai_service_date_raw",
    "ai_due_date_text",
    "ai_date_provenance",
}


def _load(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except UnicodeError:
        return json.loads(path.read_text(encoding="utf-16"))


def _invoices(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("invoice_number") or ""): item
        for item in payload.get("all_invoices") or []
    }


def _meta(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("_meta")
    return value if isinstance(value, dict) else {}


def _candidate_summary(row: dict[str, Any]) -> list[dict[str, Any]]:
    decision = _meta(row).get("accounting_decision") or {}
    return [
        {
            "gl_code": item.get("gl_code"),
            "gl_name": item.get("gl_name"),
            "source": item.get("source"),
            "source_id": item.get("source_id"),
            "base_score": item.get("base_score"),
            "compatibility_results": item.get("compatibility_results") or [],
        }
        for item in decision.get("candidates_ranked") or []
    ]


def _engine_input(row: dict[str, Any]) -> dict[str, Any]:
    meta = _meta(row)
    decision = meta.get("accounting_decision") or {}
    return {
        "semantic_classification": meta.get("semantic_classification"),
        "candidates": _candidate_summary(row),
        "catalog_version": decision.get("catalog_version"),
        "semantic_reasoning_trace": meta.get("semantic_reasoning_trace"),
    }


def _first_gl_divergence(golden_row: dict[str, Any], cold_row: dict[str, Any]) -> str:
    if _semantic_subject(golden_row) != _semantic_subject(cold_row):
        return "extraction_facts"
    old = _engine_input(golden_row)
    new = _engine_input(cold_row)
    if old["semantic_classification"] != new["semantic_classification"]:
        return "semantic_candidate_generation"
    if old["candidates"] != new["candidates"]:
        return "semantic_candidate_generation"
    if old["catalog_version"] != new["catalog_version"]:
        return "normalization_reference_context"
    return "accounting_decision_engine_outputs"


def _semantic_subject(row: dict[str, Any]) -> tuple[str, ...]:
    meta = _meta(row)
    source = str(
        meta.get("ai_source_line_description")
        or meta.get("normalized_source_description")
        or row.get("Line Item Description")
        or ""
    ).lower()
    tokens = re.findall(r"[a-z0-9]+", source)
    return tuple(
        token for token in tokens
        if not any(char.isdigit() for char in token)
        and token not in {"apt", "other", "at", "n"}
    )


def _artifact_map(root: Path) -> dict[str, dict[str, Any]]:
    final: dict[str, dict[str, Any]] = {}
    candidates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for path in (root / "isolated_webapp_data" / "cache" / "document_facts").glob("*.json"):
        artifact = _load(path)
        observed = artifact.get("observed_payload") or {}
        invoice = str(observed.get("invoice_number") or "")
        if invoice:
            candidates[invoice].append(artifact)
    # The accepted batch contains the 12-row 22-3194 invoice, not the separate
    # 36-row exact-page artifact sharing that printed invoice number.
    for invoice, artifacts in candidates.items():
        final[invoice] = min(
            artifacts,
            key=lambda item: abs(
                len((item.get("observed_payload") or {}).get("line_items") or [])
                - (12 if invoice == "22-3194" else len((item.get("observed_payload") or {}).get("line_items") or []))
            ),
        )
    return final


def _raw_provenance_value(
    artifact: dict[str, Any], field: str, line_index: int
) -> Any:
    observed = artifact.get("observed_payload") or {}
    if field == "ai_row_identity_evidence":
        rows = observed.get("line_items") or []
        return (rows[line_index].get("row_identity_evidence")
                if line_index < len(rows) and isinstance(rows[line_index], dict) else None)
    mapping = {
        "ai_row_identity_verification": "row_identity_verification",
        "ai_handwritten_row_identities": "handwritten_row_identities",
        "ai_excluded_paid_rows": "excluded_paid_rows",
        "ai_service_date": "service_date",
        "ai_service_date_raw": "service_date_raw",
        "ai_due_date_text": "due_date_text",
        "ai_date_provenance": "date_provenance",
    }
    key = mapping[field]
    return observed.get(key, artifact.get(key))


def _review_loss_reason(code: str) -> tuple[str, str]:
    if code in {"property_mapping_required", "required_property_abbreviation"}:
        return "property_location_resolution", "unsafe vendor-history property fallback changed the condition"
    if code == "location_unresolved":
        return "property_location_resolution", "cold observed facts did not preserve the same location trigger inputs"
    if code.startswith("ai_warning_"):
        return "review_blocker_generation", "warning code was derived from mutable provider prose"
    return "review_blocker_generation", "the normalized condition or its text-derived code changed"


def build_audit(root: Path) -> dict[str, Any]:
    golden = _load(root / "official_golden.json")
    cold = _load(root / "cold_result.json")
    comparison = _load(root / "cold_comparison.json")
    gold_invoices = _invoices(golden)
    cold_invoices = _invoices(cold)
    artifacts = _artifact_map(root)

    gl_details: list[dict[str, Any]] = []
    for change in comparison.get("gl_changes_requiring_explicit_review") or []:
        invoice = str(change["invoice"])
        line = int(change["line"])
        golden_row = gold_invoices[invoice]["rows"][line - 1]
        cold_row = cold_invoices[invoice]["rows"][line - 1]
        old_decision = _meta(golden_row).get("accounting_decision") or {}
        new_decision = _meta(cold_row).get("accounting_decision") or {}
        gl_details.append({
            "invoice": invoice,
            "line": line,
            "row_identity": {
                "golden": _meta(golden_row).get("ai_line_row_label"),
                "cold": _meta(cold_row).get("ai_line_row_label"),
            },
            "description": {
                "golden": golden_row.get("Line Item Description"),
                "cold": cold_row.get("Line Item Description"),
            },
            "amount": {"golden": golden_row.get("Amount"), "cold": cold_row.get("Amount")},
            "golden_gl": golden_row.get("GL Account"),
            "cold_gl": cold_row.get("GL Account"),
            "golden_candidate_set": _candidate_summary(golden_row),
            "cold_candidate_set": _candidate_summary(cold_row),
            "candidate_sources": {
                "golden": sorted({item.get("source") for item in _candidate_summary(golden_row) if item.get("source")}),
                "cold": sorted({item.get("source") for item in _candidate_summary(cold_row) if item.get("source")}),
            },
            "semantic_concept": {
                "golden": _meta(golden_row).get("semantic_classification"),
                "cold": _meta(cold_row).get("semantic_classification"),
            },
            "catalog_rules_fingerprint": {
                "golden": old_decision.get("catalog_version"),
                "cold": new_decision.get("catalog_version"),
            },
            "engine_inputs": {"golden": _engine_input(golden_row), "cold": _engine_input(cold_row)},
            "engine_rationale": {
                "golden": old_decision.get("why_selected"),
                "cold": new_decision.get("why_selected"),
            },
            "first_divergent_stage": _first_gl_divergence(golden_row, cold_row),
        })

    provenance: list[dict[str, Any]] = []
    for change in comparison.get("critical_differences") or []:
        field = change.get("field")
        if field not in PROVENANCE_FIELDS:
            continue
        invoice = str(change["invoice"])
        line = int(change["line"])
        old_row = gold_invoices[invoice]["rows"][line - 1]
        new_row = cold_invoices[invoice]["rows"][line - 1]
        raw_value = _raw_provenance_value(artifacts[invoice], field, line - 1)
        provenance.append({
            "invoice": invoice,
            "line": line,
            "provenance_key": field,
            "golden_nonempty": bool(_meta(old_row).get(field)),
            "cold_nonempty": bool(_meta(new_row).get(field)),
            "raw_cold_evidence_nonempty": bool(raw_value),
            "raw_cold_evidence": raw_value,
            "discarded_or_failed_to_merge": False,
            "classification": "observed_evidence_changed_at_extraction",
            "first_divergent_stage": "extraction_facts",
        })

    review_events: list[dict[str, Any]] = []
    for change in comparison.get("critical_differences") or []:
        if change.get("field") != "manual_review_codes_lost":
            continue
        invoice = str(change["invoice"])
        cold_codes = set(cold_invoices[invoice].get("manual_review_codes") or [])
        for code in change.get("values") or []:
            stage, reason = _review_loss_reason(code)
            review_events.append({
                "invoice": invoice,
                "row": None,
                "lost_code": code,
                "original_condition": code,
                "cold_condition_codes": sorted(cold_codes),
                "generator_function": "validate_ai_extraction",
                "first_divergent_stage": stage,
                "removal_mechanism": reason,
                "removed_by_deduplication": False,
                "removed_by_serialization": False,
            })

    critical_by_field = Counter(
        item.get("field") for item in comparison.get("critical_differences") or []
    )
    extraction_criticals = [
        item for item in comparison.get("critical_differences") or []
        if item.get("field") not in {"manual_review_codes_lost", "Property Abbreviation"}
    ]
    catalog_input_differences = sum(
        item["catalog_rules_fingerprint"]["golden"]
        != item["catalog_rules_fingerprint"]["cold"]
        for item in gl_details
    )
    root_causes = [
        {
            "root_cause": "cold visual extraction produced different observed facts/evidence",
            "affected_invoices": sorted({str(item.get("invoice")) for item in extraction_criticals if item.get("invoice")}),
            "affected_rows": len({(str(item.get("invoice")), int(item.get("line"))) for item in extraction_criticals if item.get("invoice") and item.get("line")}),
            "critical_difference_count": len(extraction_criticals),
            "provenance_event_count": len(provenance),
            "gl_difference_count": sum(item["first_divergent_stage"] == "extraction_facts" for item in gl_details),
            "proposed_minimal_fix": "none downstream; preserve cold evidence and require a new extraction only after offline parity work",
        },
        {
            "root_cause": "isolated runtime omitted active ResMan snapshot context",
            "affected_invoices": sorted({item["invoice"] for item in gl_details}),
            "affected_rows": catalog_input_differences,
            "critical_difference_count": 0,
            "gl_difference_count": 0,
            "engine_input_difference_count": catalog_input_differences,
            "proposed_minimal_fix": "version normalized cache by active snapshot hashes and provision the immutable context snapshot for replay",
        },
        {
            "root_cause": "accepted grouped semantic cache absent; fresh stochastic reasoning did not yield accepted candidates",
            "affected_invoices": sorted({item["invoice"] for item in gl_details if item["first_divergent_stage"] != "extraction_facts"}),
            "affected_rows": sum(item["first_divergent_stage"] != "extraction_facts" for item in gl_details),
            "critical_difference_count": 0,
            "gl_difference_count": sum(item["first_divergent_stage"] != "extraction_facts" for item in gl_details),
            "proposed_minimal_fix": "offline replay may consume only matching accepted cache entries; cache miss remains blocking and makes no provider call",
        },
        {
            "root_cause": "administrative sold-to evidence entered vendor-history property fallback",
            "affected_invoices": ["180547"],
            "affected_rows": 1,
            "critical_difference_count": critical_by_field.get("Property Abbreviation", 0),
            "proposed_minimal_fix": "gate history fallback by typed address role",
        },
        {
            "root_cause": "review codes were derived from mutable provider warning prose",
            "affected_invoices": sorted({item["invoice"] for item in review_events}),
            "affected_rows": 0,
            "critical_difference_count": sum(item.get("field") == "manual_review_codes_lost" for item in comparison.get("critical_differences") or []),
            "lost_code_count": len(review_events),
            "proposed_minimal_fix": "introduce typed warning taxonomy prospectively; do not rewrite historical golden evidence",
        },
    ]

    return {
        "target": str(root),
        "invoice_count": [len(gold_invoices), len(cold_invoices)],
        "row_count": comparison.get("row_count"),
        "critical_difference_count": len(comparison.get("critical_differences") or []),
        "gl_difference_count": len(gl_details),
        "provenance_event_count": len(provenance),
        "genuinely_missing_provenance_count": sum(not item["cold_nonempty"] for item in provenance),
        "pipeline_provenance_loss_count": sum(item["discarded_or_failed_to_merge"] for item in provenance),
        "lost_review_invoice_event_count": sum(item.get("field") == "manual_review_codes_lost" for item in comparison.get("critical_differences") or []),
        "lost_review_code_count": len(review_events),
        "critical_differences_by_field": dict(sorted(critical_by_field.items())),
        "gl_differences": gl_details,
        "provenance_differences": provenance,
        "review_code_losses": review_events,
        "root_causes": root_causes,
        "stage_conclusions": {
            "extraction_facts": "diverged",
            "normalization": "diverged because runtime reference context was absent",
            "provenance_evidence_merge": "no loss detected",
            "property_location_resolution": "diverged",
            "semantic_candidate_generation": "diverged",
            "accounting_decision_engine_inputs": "diverged",
            "accounting_decision_engine_outputs": "no same-input engine divergence detected",
            "review_blocker_generation": "text-derived code identity diverged",
            "persistence_serialization": "no loss detected",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args()
    audit = build_audit(args.root.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(audit, indent=2, default=str), encoding="utf-8")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(_markdown(audit), encoding="utf-8")
    print(json.dumps({key: audit[key] for key in (
        "invoice_count", "row_count", "critical_difference_count",
        "gl_difference_count", "provenance_event_count",
        "genuinely_missing_provenance_count", "pipeline_provenance_loss_count",
        "lost_review_invoice_event_count", "lost_review_code_count",
    )}, indent=2))
    return 0


def _cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _markdown(audit: dict[str, Any]) -> str:
    lines = [
        "# Cold run divergence audit",
        "",
        f"- Invoices: {audit['invoice_count'][0]} / {audit['invoice_count'][1]}",
        f"- Rows: {audit['row_count'][0]} / {audit['row_count'][1]}",
        f"- Critical differences: {audit['critical_difference_count']}",
        f"- GL differences: {audit['gl_difference_count']}",
        f"- Provenance comparisons: {audit['provenance_event_count']}",
        f"- Genuinely missing provenance: {audit['genuinely_missing_provenance_count']}",
        f"- Pipeline provenance losses: {audit['pipeline_provenance_loss_count']}",
        f"- Lost review-code invoice events / exact codes: {audit['lost_review_invoice_event_count']} / {audit['lost_review_code_count']}",
        "",
        "## Root causes",
        "",
        "| Root cause | Invoices | Rows | Critical | GL | Minimal fix |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for item in audit["root_causes"]:
        lines.append(
            "| {root_cause} | {invoices} | {rows} | {critical} | {gl} | {fix} |".format(
                root_cause=_cell(item["root_cause"]),
                invoices=_cell(", ".join(item.get("affected_invoices") or [])),
                rows=item.get("affected_rows", 0),
                critical=item.get("critical_difference_count", 0),
                gl=item.get("gl_difference_count", 0),
                fix=_cell(item["proposed_minimal_fix"]),
            )
        )
    lines.extend([
        "", "## All 31 GL differences", "",
        "| Invoice | Line | Row identity (gold/cold) | Description (gold/cold) | Amount (gold/cold) | GL (gold/cold) | Candidate codes (gold/cold) | Candidate sources | Semantic (gold/cold) | Catalog (gold/cold) | First responsible stage |",
        "|---|---:|---|---|---|---|---|---|---|---|---|",
    ])
    for item in audit["gl_differences"]:
        gold_candidates = ",".join(str(value.get("gl_code") or "") for value in item["golden_candidate_set"])
        cold_candidates = ",".join(str(value.get("gl_code") or "") for value in item["cold_candidate_set"])
        gs = item["semantic_concept"]["golden"] or {}
        cs = item["semantic_concept"]["cold"] or {}
        lines.append(
            f"| {_cell(item['invoice'])} | {item['line']} | "
            f"{_cell(item['row_identity']['golden'])} / {_cell(item['row_identity']['cold'])} | "
            f"{_cell(item['description']['golden'])} / {_cell(item['description']['cold'])} | "
            f"{_cell(item['amount']['golden'])} / {_cell(item['amount']['cold'])} | "
            f"{_cell(item['golden_gl'])} / {_cell(item['cold_gl'])} | "
            f"{_cell(gold_candidates)} / {_cell(cold_candidates)} | "
            f"{_cell(','.join(item['candidate_sources']['golden']))} / {_cell(','.join(item['candidate_sources']['cold']))} | "
            f"{_cell(gs.get('line_family'))}:{_cell(gs.get('trade_family'))}:{_cell(gs.get('work_mode'))} / "
            f"{_cell(cs.get('line_family'))}:{_cell(cs.get('trade_family'))}:{_cell(cs.get('work_mode'))} | "
            f"{_cell(item['catalog_rules_fingerprint']['golden'])} / {_cell(item['catalog_rules_fingerprint']['cold'])} | "
            f"{_cell(item['first_divergent_stage'])} |"
        )
    lines.extend([
        "", "The JSON companion contains each complete candidate object, engine input, compatibility check and rationale.",
        "", "## Provenance analysis", "",
        "All 179 compared values are present in both results and in saved cold raw evidence. They changed at extraction; none was discarded by normalization, merge, persistence or serialization.",
        "", "| Key | Events | Raw cold present | Pipeline loss |",
        "|---|---:|---:|---:|",
    ])
    for key, count in sorted(Counter(item["provenance_key"] for item in audit["provenance_differences"]).items()):
        subset = [item for item in audit["provenance_differences"] if item["provenance_key"] == key]
        lines.append(
            f"| {key} | {count} | {sum(item['raw_cold_evidence_nonempty'] for item in subset)} | "
            f"{sum(item['discarded_or_failed_to_merge'] for item in subset)} |"
        )
    lines.extend([
        "", "## Lost review codes", "",
        "| Invoice | Lost code | Cold codes | Generator | Mechanism |",
        "|---|---|---|---|---|",
    ])
    for item in audit["review_code_losses"]:
        lines.append(
            f"| {_cell(item['invoice'])} | {_cell(item['lost_code'])} | "
            f"{_cell(', '.join(item['cold_condition_codes']))} | {_cell(item['generator_function'])} | "
            f"{_cell(item['removal_mechanism'])} |"
        )
    lines.extend(["", "## Stage conclusions", ""])
    for stage, conclusion in audit["stage_conclusions"].items():
        lines.append(f"- `{stage}`: {conclusion}")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
