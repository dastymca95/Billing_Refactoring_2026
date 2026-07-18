"""AI-assisted vendor and GL mapping review endpoints."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..services import ai_mapping_review, batch_store, revisions as revisions_service


router = APIRouter(prefix="/api/ai-review", tags=["ai-review"])
batch_router = APIRouter(prefix="/api/batches", tags=["ai-review"])


def _result_cache_path(batch_id: str) -> Path:
    return batch_store.get_processed_dir(batch_id) / "_webapp_result.json"


def _load_result(batch_id: str) -> tuple[Path, dict[str, Any]]:
    try:
        path = _result_cache_path(batch_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Batch not found: {batch_id}")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="No processed preview is available.")
    try:
        return path, json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"Could not read preview cache: {exc}")


def _save_result(batch_id: str, path: Path, result: dict[str, Any]) -> None:
    path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    current = revisions_service.current_revision_id(batch_id)
    if current:
        try:
            revisions_service.overwrite_snapshot(batch_id, current, result=result)
        except (FileNotFoundError, ValueError):
            pass


def _record_ai_review(batch_id: str, kind: str, applied: int, details: dict[str, Any]) -> None:
    if applied <= 0:
        return
    from ..services import operator_activity_log
    operator_activity_log.record(
        batch_id=batch_id,
        event_type=f"ai_review_{kind}_applied",
        source="ai",
        actor="local_operator",
        summary=f"Applied AI-assisted {kind.replace('_', ' ')} to {applied} row{'s' if applied != 1 else ''}.",
        details={**details, "applied_rows": applied, "human_confirmed": True},
    )


def _iter_rows(result: dict[str, Any]):
    flat = 0
    for inv in result.get("all_invoices") or []:
        for row in inv.get("rows") or []:
            yield "all_invoices", flat, inv, row
            flat += 1
    for vendor_key, payload in (result.get("by_vendor") or {}).items():
        for inv in (payload or {}).get("invoices") or []:
            for row in inv.get("rows") or []:
                yield f"by_vendor:{vendor_key}", None, inv, row


def _row_signature(invoice: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
    return {
        "source_file": meta.get("source_file")
        or invoice.get("source_file")
        or (invoice.get("debug_info") or {}).get("source_file"),
        "invoice_number": row.get("Invoice Number") or invoice.get("invoice_number"),
        "line_item_number": row.get("Line Item Number"),
        "description": row.get("Line Item Description"),
        "amount": row.get("Amount"),
    }


def _signature_matches(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return all(str(left.get(k) or "") == str(right.get(k) or "") for k in left)


def _remove_meta_flag(row: dict[str, Any], code: str, reason_contains: str) -> None:
    meta = row.setdefault("_meta", {})
    flags = [f for f in (meta.get("ai_validation_flags") or []) if f != code]
    meta["ai_validation_flags"] = flags
    reasons = [
        r
        for r in (meta.get("manual_review_reasons") or [])
        if reason_contains.lower() not in str(r).lower()
    ]
    meta["manual_review_reasons"] = reasons


def _update_manual_review_items(
    result: dict[str, Any],
    *,
    code: str,
    reason_contains: str,
    source_file: str | None = None,
) -> None:
    for item in result.get("all_manual_review") or []:
        if source_file and item.get("source_file") != source_file:
            continue
        item["reason_codes"] = [
            c for c in (item.get("reason_codes") or []) if c != code
        ]
        item["reasons"] = [
            r
            for r in (item.get("reasons") or [])
            if reason_contains.lower() not in str(r).lower()
        ]
        if not item.get("reasons"):
            item["message"] = "AI review items resolved by operator."


def _money(value: Any) -> float:
    try:
        return round(float(str(value).replace("$", "").replace(",", "").strip()), 2)
    except (TypeError, ValueError):
        return 0.0


def _row_base_amount(row: dict[str, Any]) -> float:
    meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
    prov = meta.get("ai_provenance") if isinstance(meta.get("ai_provenance"), dict) else {}
    base = prov.get("base_amount")
    return _money(base if base not in (None, "") else row.get("Amount"))


def _row_invoice_total(row: dict[str, Any]) -> float:
    meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
    prov = meta.get("ai_provenance") if isinstance(meta.get("ai_provenance"), dict) else {}
    return _money(prov.get("invoice_total") or row.get("Invoice Total"))


def _set_row_amount(row: dict[str, Any], amount: float, allocated: float = 0.0) -> None:
    base_amount = _row_base_amount(row)
    row["Amount"] = round(amount, 2)
    qty = _money(row.get("Quantity")) or 1.0
    row["Unit Price"] = round(amount / qty, 2) if qty else round(amount, 2)
    meta = row.setdefault("_meta", {})
    prov = meta.setdefault("ai_provenance", {})
    prov["base_amount"] = base_amount
    prov["allocated_tax_amount"] = round(allocated, 2)


def _apply_tax_policy_to_rows(rows: list[dict[str, Any]], policy: str) -> None:
    if not rows:
        return
    base_amounts = [_row_base_amount(row) for row in rows]
    base_total = round(sum(base_amounts), 2)
    invoice_total = next((total for total in (_row_invoice_total(row) for row in rows) if total > 0), 0.0)
    if policy == "distribute_proportionally" and invoice_total > 0 and abs(base_total) > 0.009:
        adjustment = round(invoice_total - base_total, 2)
        running = 0.0
        for idx, (row, base) in enumerate(zip(rows, base_amounts)):
            is_last = idx == len(rows) - 1
            share = round(adjustment - running, 2) if is_last else round(adjustment * (max(base, 0) / base_total), 2)
            if not is_last:
                running = round(running + share, 2)
            _set_row_amount(row, base + share, share)
    elif policy in {"exclude_tax", "manual_review", "separate_tax_line"}:
        for row, base in zip(rows, base_amounts):
            _set_row_amount(row, base, 0.0)


@router.get("/vendor-candidates")
def vendor_candidates_endpoint(detected_vendor: str, limit: int = 6) -> dict:
    return ai_mapping_review.vendor_candidates(detected_vendor, limit=limit)


@router.get("/gl-candidates")
def gl_candidates_endpoint(
    line_item_description: str,
    vendor_name: str = "",
    ai_suggested_gl: str = "",
    limit: int = 6,
) -> dict:
    return ai_mapping_review.gl_candidates(
        line_item_description=line_item_description,
        vendor_name=vendor_name,
        ai_suggested_gl=ai_suggested_gl,
        limit=limit,
    )


@router.get("/property-candidates")
def property_candidates_endpoint(
    query: str = "",
    service_address: str = "",
    limit: int = 8,
) -> dict:
    return ai_mapping_review.property_candidates(
        query=query,
        service_address=service_address,
        limit=limit,
    )


@router.get("/location-candidates")
def location_candidates_endpoint(
    property_abbreviation: str,
    query: str = "",
    limit: int = 20,
) -> dict:
    return ai_mapping_review.location_candidates(
        property_abbreviation=property_abbreviation,
        query=query,
        limit=limit,
    )


@router.get("/learned-mappings")
def learned_mappings_endpoint() -> dict:
    return ai_mapping_review.load_learned_mappings()


class ApplyVendorMappingBody(BaseModel):
    detected_vendor: str
    selected_vendor_name: str
    vendor_id: str = ""
    row_index: int | None = None
    save_for_future: bool = True
    apply_scope: Literal["current_invoice", "batch"] = "current_invoice"


class ApplyGlMappingBody(BaseModel):
    row_index: int
    gl_account: str
    gl_name: str = ""
    save_for_future: bool = True
    apply_to_similar: bool = False
    pattern: str = ""


class ApplyPropertyLocationBody(BaseModel):
    row_index: int
    property_abbreviation: str
    location: str = ""
    service_address: str = ""
    save_for_future: bool = False
    apply_scope: Literal["current_invoice", "batch"] = "current_invoice"
    leave_location_blank: bool = False


class ApplyTaxPolicyBody(BaseModel):
    row_index: int
    policy: Literal["manual_review", "distribute_proportionally", "separate_tax_line", "exclude_tax"]


@batch_router.post("/{batch_id}/ai-review/vendor-mapping")
def apply_vendor_mapping_endpoint(batch_id: str, body: ApplyVendorMappingBody) -> dict:
    selected = ai_mapping_review.resolve_vendor_name(body.selected_vendor_name)
    if not selected:
        raise HTTPException(status_code=400, detail="Selected vendor is not in Vendor List.")
    path, result = _load_result(batch_id)
    learned = None
    if body.save_for_future:
        try:
            learned = ai_mapping_review.save_vendor_mapping(
                detected_vendor=body.detected_vendor,
                resman_vendor_name=selected["vendor_name"],
                vendor_id=body.vendor_id or selected.get("vendor_id", ""),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    target_source_file: str | None = None
    if body.apply_scope == "current_invoice" and body.row_index is not None:
        for view, flat, inv, row in _iter_rows(result):
            if view == "all_invoices" and flat == body.row_index:
                target_source_file = _row_signature(inv, row).get("source_file")
                break

    applied = 0
    for _view, _flat, inv, row in _iter_rows(result):
        meta = row.setdefault("_meta", {})
        current_vendor = str(row.get("Vendor") or meta.get("ai_detected_vendor") or "")
        source_file = _row_signature(inv, row).get("source_file")
        if target_source_file and source_file != target_source_file:
            continue
        if (
            ai_mapping_review.normalize_key(current_vendor)
            != ai_mapping_review.normalize_key(body.detected_vendor)
            and body.apply_scope != "batch"
        ):
            continue
        if body.apply_scope == "batch" and current_vendor and (
            ai_mapping_review.normalize_key(current_vendor)
            != ai_mapping_review.normalize_key(body.detected_vendor)
        ):
            continue
        row["Vendor"] = selected["vendor_name"]
        provenance = list(meta.get("ai_mapping_provenance") or [])
        provenance.append({
            "kind": "vendor_mapping",
            "detected_vendor": body.detected_vendor,
            "resman_vendor_name": selected["vendor_name"],
            "confirmed_by": "user",
        })
        meta["ai_mapping_provenance"] = provenance
        _remove_meta_flag(row, "vendor_mapping_required", "vendor")
        _remove_meta_flag(row, "vendor_mapping_not_found", "vendor")
        applied += 1

    _update_manual_review_items(
        result,
        code="vendor_mapping_required",
        reason_contains="vendor",
        source_file=target_source_file,
    )
    _update_manual_review_items(
        result,
        code="vendor_mapping_not_found",
        reason_contains="vendor",
        source_file=target_source_file,
    )
    _save_result(batch_id, path, result)
    _record_ai_review(batch_id, "vendor_mapping", applied, {
        "selected_vendor_name": selected["vendor_name"], "scope": body.apply_scope,
    })
    return {
        "batch_id": batch_id,
        "applied_rows": applied,
        "selected_vendor_name": selected["vendor_name"],
        "saved_mapping": learned,
    }


@batch_router.post("/{batch_id}/ai-review/property-location")
def apply_property_location_endpoint(
    batch_id: str,
    body: ApplyPropertyLocationBody,
) -> dict:
    selected = ai_mapping_review.validate_property_location(
        property_abbreviation=body.property_abbreviation,
        location="" if body.leave_location_blank else body.location,
    )
    if not selected:
        raise HTTPException(status_code=400, detail="Selected property/location is not valid.")
    path, result = _load_result(batch_id)
    target_source_file: str | None = None
    for view, flat, inv, row in _iter_rows(result):
        if view == "all_invoices" and flat == body.row_index:
            target_source_file = _row_signature(inv, row).get("source_file")
            break
    if target_source_file is None:
        raise HTTPException(status_code=404, detail="Template row not found.")

    learned = None
    if body.save_for_future and body.service_address:
        try:
            learned = ai_mapping_review.save_property_mapping(
                service_address=body.service_address,
                property_abbreviation=selected["property_abbreviation"],
                location="" if body.leave_location_blank else selected.get("location", ""),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    applied = 0
    for _view, _flat, inv, row in _iter_rows(result):
        source_file = _row_signature(inv, row).get("source_file")
        if body.apply_scope == "current_invoice" and source_file != target_source_file:
            continue
        row["Property Abbreviation"] = selected["property_abbreviation"]
        row["Location"] = "" if body.leave_location_blank else selected.get("location", "")
        meta = row.setdefault("_meta", {})
        provenance = list(meta.get("ai_mapping_provenance") or [])
        provenance.append({
            "kind": "property_location_mapping",
            "property_abbreviation": row["Property Abbreviation"],
            "location": row["Location"],
            "confirmed_by": "user",
        })
        meta["ai_mapping_provenance"] = provenance
        _remove_meta_flag(row, "property_mapping_required", "property")
        _remove_meta_flag(row, "property_abbreviation_missing", "property")
        _remove_meta_flag(row, "location_unresolved", "location")
        applied += 1

    _update_manual_review_items(
        result,
        code="property_mapping_required",
        reason_contains="property",
        source_file=target_source_file if body.apply_scope == "current_invoice" else None,
    )
    _update_manual_review_items(
        result,
        code="location_unresolved",
        reason_contains="location",
        source_file=target_source_file if body.apply_scope == "current_invoice" else None,
    )
    _save_result(batch_id, path, result)
    _record_ai_review(batch_id, "property_location", applied, {
        "property_abbreviation": selected["property_abbreviation"],
        "location": "" if body.leave_location_blank else selected.get("location", ""),
        "scope": body.apply_scope,
    })
    return {
        "batch_id": batch_id,
        "applied_rows": applied,
        "property_abbreviation": selected["property_abbreviation"],
        "location": "" if body.leave_location_blank else selected.get("location", ""),
        "saved_mapping": learned,
    }


@batch_router.post("/{batch_id}/ai-review/gl-mapping")
def apply_gl_mapping_endpoint(batch_id: str, body: ApplyGlMappingBody) -> dict:
    account = ai_mapping_review.validate_gl_account(body.gl_account)
    if not account:
        raise HTTPException(status_code=400, detail="Selected GL account is not valid.")
    path, result = _load_result(batch_id)

    target: tuple[dict[str, Any], dict[str, Any]] | None = None
    target_sig: dict[str, Any] | None = None
    for view, flat, inv, row in _iter_rows(result):
        if view == "all_invoices" and flat == body.row_index:
            target = (inv, row)
            target_sig = _row_signature(inv, row)
            break
    if not target or not target_sig:
        raise HTTPException(status_code=404, detail="Template row not found.")

    target_description = str(target_sig.get("description") or "")
    target_meta = target[1].get("_meta") if isinstance(target[1].get("_meta"), dict) else {}
    target_vendor = str(target[1].get("Vendor") or target_meta.get("ai_detected_vendor") or "")
    pattern = body.pattern.strip() or target_description
    learned = None
    if body.save_for_future:
        try:
            learned = ai_mapping_review.save_gl_mapping(
                vendor_name=target_vendor,
                pattern=pattern,
                gl_account=account["gl_code"],
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    applied = 0
    for _view, _flat, inv, row in _iter_rows(result):
        sig = _row_signature(inv, row)
        if body.apply_to_similar:
            same_vendor = ai_mapping_review.normalize_key(str(row.get("Vendor") or "")) == ai_mapping_review.normalize_key(target_vendor)
            same_text = ai_mapping_review.normalize_key(pattern) in ai_mapping_review.normalize_key(str(sig.get("description") or ""))
            if not (same_vendor and same_text):
                continue
        elif not _signature_matches(sig, target_sig):
            continue
        row["GL Account"] = account["gl_code"]
        meta = row.setdefault("_meta", {})
        provenance = list(meta.get("ai_mapping_provenance") or [])
        provenance.append({
            "kind": "gl_mapping",
            "line_item_description": sig.get("description"),
            "gl_account": account["gl_code"],
            "gl_name": account["gl_name"],
            "confirmed_by": "user",
        })
        meta["ai_mapping_provenance"] = provenance
        _remove_meta_flag(row, "gl_mapping_required", "gl")
        _remove_meta_flag(row, "ambiguous_gl_mapping", "gl")
        applied += 1

    source_file = target_sig.get("source_file")
    _update_manual_review_items(
        result,
        code="gl_mapping_required",
        reason_contains="gl",
        source_file=source_file,
    )
    _update_manual_review_items(
        result,
        code="ambiguous_gl_mapping",
        reason_contains="gl",
        source_file=source_file,
    )
    _save_result(batch_id, path, result)
    _record_ai_review(batch_id, "gl_mapping", applied, {
        "gl_account": account["gl_code"], "apply_to_similar": body.apply_to_similar,
    })
    return {
        "batch_id": batch_id,
        "applied_rows": applied,
        "gl_account": account["gl_code"],
        "gl_name": account["gl_name"],
        "saved_mapping": learned,
    }


@batch_router.post("/{batch_id}/ai-review/tax-policy")
def apply_tax_policy_endpoint(batch_id: str, body: ApplyTaxPolicyBody) -> dict:
    path, result = _load_result(batch_id)
    target_source_file: str | None = None
    for view, flat, inv, row in _iter_rows(result):
        if view == "all_invoices" and flat == body.row_index:
            target_source_file = _row_signature(inv, row).get("source_file")
            break
    if target_source_file is None:
        raise HTTPException(status_code=404, detail="Template row not found.")

    rows_by_view: dict[str, list[dict[str, Any]]] = {}
    applied = 0
    for _view, _flat, inv, row in _iter_rows(result):
        if _row_signature(inv, row).get("source_file") != target_source_file:
            continue
        rows_by_view.setdefault(_view, []).append(row)
        meta = row.setdefault("_meta", {})
        meta["ai_tax_handling"] = body.policy
        provenance = list(meta.get("ai_mapping_provenance") or [])
        provenance.append({
            "kind": "tax_policy",
            "policy": body.policy,
            "confirmed_by": "user",
        })
        meta["ai_mapping_provenance"] = provenance
        if body.policy != "manual_review":
            _remove_meta_flag(row, "tax_handling_requires_review", "tax")
        applied += 1
    for rows in rows_by_view.values():
        _apply_tax_policy_to_rows(rows, body.policy)
    if body.policy != "manual_review":
        _update_manual_review_items(
            result,
            code="tax_handling_requires_review",
            reason_contains="tax",
            source_file=target_source_file,
        )
    _save_result(batch_id, path, result)
    _record_ai_review(batch_id, "tax_policy", applied, {"policy": body.policy})
    return {"batch_id": batch_id, "applied_rows": applied, "policy": body.policy}
