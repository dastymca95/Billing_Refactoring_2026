"""Compare accounting-safe golden outputs while ignoring runtime telemetry."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROW_COLUMNS = (
    "Invoice Number", "Bill or Credit", "Invoice Date", "Accounting Date",
    "Vendor", "Invoice Description", "Line Item Number", "Property Abbreviation",
    "Location", "GL Account", "Line Item Description", "Amount", "Expense Type",
    "Is Replacement Reserve", "Due Date", "Quantity", "Unit Price", "Tax",
)
META_FIELDS = (
    "ai_source_line_description", "normalized_source_description",
    "ai_line_activity", "ai_line_row_label", "ai_line_location_candidate",
    "ai_row_identity_evidence", "row_identity_needs_confirmation",
    "ai_service_date", "ai_service_date_raw", "ai_payment_terms", "ai_due_date_text",
    "ai_date_provenance", "ai_handwritten_row_identities", "ai_excluded_paid_rows",
    "ai_validation_flags", "manual_review_reasons", "document_facts",
    "semantic_classification", "accounting_decision",
)
HARD_ROW_FIELDS = (
    "Invoice Number", "Bill or Credit", "Invoice Date", "Accounting Date",
    "Vendor", "Invoice Description", "Line Item Number", "Property Abbreviation",
    "Location", "Line Item Description", "Amount", "Expense Type",
    "Is Replacement Reserve", "Due Date", "Quantity", "Unit Price", "Tax",
)
HARD_PROVENANCE_FIELDS = (
    "ai_row_identity_evidence", "ai_row_identity_verification",
    "ai_handwritten_row_identities", "ai_excluded_paid_rows",
    "ai_service_date", "ai_service_date_raw", "ai_due_date_text",
    "ai_date_provenance",
)


def projection(payload: dict[str, Any]) -> dict[str, Any]:
    invoices = []
    for invoice in payload.get("all_invoices") or []:
        rows = []
        for row in invoice.get("rows") or []:
            meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
            rows.append({
                "columns": {key: row.get(key) for key in ROW_COLUMNS},
                "evidence_and_decision": {key: meta.get(key) for key in META_FIELDS},
            })
        invoices.append({
            "source_file": invoice.get("source_file"),
            "source_page": invoice.get("source_page"),
            "invoice_number": invoice.get("invoice_number"),
            "invoice_date": invoice.get("invoice_date"),
            "total_amount": invoice.get("total_amount"),
            "manual_review_codes": invoice.get("manual_review_codes") or [],
            "manual_review_reasons": invoice.get("manual_review_reasons") or [],
            "validation_summary": invoice.get("validation_summary") or {},
            "rows": rows,
        })
    return {
        "invoices": invoices,
        "manual_review": payload.get("all_manual_review") or [],
    }


def differences(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    left = projection(before)
    right = projection(after)
    if left == right:
        return []
    issues: list[str] = []
    left_invoices = left["invoices"]
    right_invoices = right["invoices"]
    if len(left_invoices) != len(right_invoices):
        issues.append(f"invoice_count:{len(left_invoices)}->{len(right_invoices)}")
    for index, (a, b) in enumerate(zip(left_invoices, right_invoices), start=1):
        identity = f"invoice[{index}]/{a.get('invoice_number')}"
        for key in ("source_file", "source_page", "invoice_number", "invoice_date", "total_amount",
                    "manual_review_codes", "manual_review_reasons", "validation_summary"):
            if a.get(key) != b.get(key):
                issues.append(f"{identity}.{key}")
        if len(a["rows"]) != len(b["rows"]):
            issues.append(f"{identity}.row_count:{len(a['rows'])}->{len(b['rows'])}")
        for row_index, (row_a, row_b) in enumerate(zip(a["rows"], b["rows"]), start=1):
            if row_a != row_b:
                changed = [
                    key for key in ("columns", "evidence_and_decision")
                    if row_a.get(key) != row_b.get(key)
                ]
                issues.append(f"{identity}.row[{row_index}]:{','.join(changed)}")
    if left["manual_review"] != right["manual_review"]:
        issues.append("manual_review_payload")
    return issues


def safety_report(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Report safety invariants separately from expected runtime telemetry.

    GL changes are never silently accepted: they are listed individually for
    explicit accounting review, while loss of rows, amounts, source evidence,
    blockers, or review codes fails hard-safety parity.
    """
    before_invoices = {
        str(item.get("invoice_number") or ""): item
        for item in before.get("all_invoices") or []
    }
    after_invoices = {
        str(item.get("invoice_number") or ""): item
        for item in after.get("all_invoices") or []
    }
    critical: list[dict[str, Any]] = []
    gl_changes: list[dict[str, Any]] = []
    before_ids = set(before_invoices)
    after_ids = set(after_invoices)
    if before_ids != after_ids:
        critical.append({
            "field": "invoice_identities",
            "lost": sorted(before_ids - after_ids),
            "added": sorted(after_ids - before_ids),
        })
    for invoice_id in sorted(before_ids & after_ids):
        old = before_invoices[invoice_id]
        new = after_invoices[invoice_id]
        old_rows = old.get("rows") or []
        new_rows = new.get("rows") or []
        for field in ("invoice_date", "total_amount"):
            if old.get(field) != new.get(field):
                critical.append({
                    "invoice": invoice_id, "field": field,
                    "before": old.get(field), "after": new.get(field),
                })
        if len(old_rows) != len(new_rows):
            critical.append({
                "invoice": invoice_id, "field": "row_count",
                "before": len(old_rows), "after": len(new_rows),
            })
        lost_codes = sorted(
            set(old.get("manual_review_codes") or [])
            - set(new.get("manual_review_codes") or [])
        )
        if lost_codes:
            critical.append({
                "invoice": invoice_id,
                "field": "manual_review_codes_lost",
                "values": lost_codes,
            })
        for line_number, (old_row, new_row) in enumerate(
            zip(old_rows, new_rows), start=1
        ):
            for field in HARD_ROW_FIELDS:
                if old_row.get(field) != new_row.get(field):
                    critical.append({
                        "invoice": invoice_id, "line": line_number,
                        "field": field, "before": old_row.get(field),
                        "after": new_row.get(field),
                    })
            old_meta = old_row.get("_meta") if isinstance(old_row.get("_meta"), dict) else {}
            new_meta = new_row.get("_meta") if isinstance(new_row.get("_meta"), dict) else {}
            for field in HARD_PROVENANCE_FIELDS:
                if old_meta.get(field) != new_meta.get(field):
                    critical.append({
                        "invoice": invoice_id, "line": line_number,
                        "field": field,
                    })
            old_gl = str(old_row.get("GL Account") or "")
            new_gl = str(new_row.get("GL Account") or "")
            if old_gl != new_gl:
                gl_changes.append({
                    "invoice": invoice_id,
                    "line": line_number,
                    "description": new_row.get("Line Item Description"),
                    "amount": new_row.get("Amount"),
                    "before_gl": old_gl,
                    "after_gl": new_gl,
                })
    return {
        "hard_safety_parity": not critical,
        "critical_differences": critical,
        "gl_changes_requiring_explicit_review": gl_changes,
        "invoice_count": [len(before_invoices), len(after_invoices)],
        "row_count": [
            sum(len(item.get("rows") or []) for item in before_invoices.values()),
            sum(len(item.get("rows") or []) for item in after_invoices.values()),
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    before = json.loads(args.before.read_text(encoding="utf-8"))
    after = json.loads(args.after.read_text(encoding="utf-8"))
    result = differences(before, after)
    safety = safety_report(before, after)
    output = {
        "exact_payload_parity": not result,
        "strict_differences": result,
        **safety,
    }
    print(json.dumps(output, indent=2) if args.json else output)
    return 0 if safety["hard_safety_parity"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
