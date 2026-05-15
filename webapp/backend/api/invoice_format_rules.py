"""Invoice format rule configuration API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..services import invoice_format_rules as svc
from ..services.ai_invoice_processor import load_references
from ..services.template_rules import get_template_rules


router = APIRouter(prefix="/api/invoice-format-rules", tags=["invoice-format-rules"])


class SaveBody(BaseModel):
    config: dict[str, Any]


class PreviewBody(BaseModel):
    config: dict[str, Any] | None = None
    sample: dict[str, Any] = Field(default_factory=dict)


@router.get("")
def get_rules() -> dict[str, Any]:
    references = load_references()
    template_rules = get_template_rules()
    return {
        "config": svc.load_config(),
        "references": svc.references_payload(references),
        "template_columns": template_rules.get("columns", []),
        "variables": svc.VARIABLES,
        "presets": svc.PRESETS,
        "scope_types": [
            {"value": "general", "label": "General"},
            {"value": "vendor", "label": "Specific vendor"},
            {"value": "vendor_group", "label": "Vendor group"},
            {"value": "property", "label": "Specific property"},
            {"value": "property_group", "label": "Property group"},
            {"value": "gl_account", "label": "Specific GL account"},
            {"value": "gl_group", "label": "GL group"},
        ],
    }


@router.put("")
def save_rules(body: SaveBody) -> dict[str, Any]:
    try:
        config = svc.save_config(body.config or {})
    except svc.InvoiceFormatRulesError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "config": config}


@router.post("/preview")
def preview(body: PreviewBody) -> dict[str, Any]:
    try:
        return {"preview": svc.preview(body.config, body.sample or {})}
    except svc.InvoiceFormatRulesError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
