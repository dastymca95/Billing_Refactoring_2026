"""Canonical Rules Studio API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..services import canonical_rules_studio as svc


router = APIRouter(prefix="/api/canonical-rules", tags=["canonical-rules"])


class PatchBody(BaseModel):
    patch: dict[str, Any] = {}


class ValidateBody(BaseModel):
    config: dict[str, Any] | None = None
    category: str | None = None
    patch: dict[str, Any] | None = None


class TestBenchBody(BaseModel):
    test_case: str = "capital_waste"
    fixture_key: str | None = None
    category: str | None = None
    draft_patch: dict[str, Any] | None = None
    run_all: bool = False


@router.get("")
def list_rules() -> dict[str, Any]:
    return svc.list_payload()


@router.post("/validate")
def validate(body: ValidateBody) -> dict[str, Any]:
    try:
        return svc.validate_request(body.model_dump(exclude_none=True))
    except svc.CanonicalRulesStudioError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/restore")
def restore() -> dict[str, Any]:
    try:
        return svc.restore_latest_backup()
    except svc.CanonicalRulesStudioError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/import-preview")
def import_preview() -> dict[str, Any]:
    try:
        return svc.import_preview_from_excel()
    except svc.CanonicalRulesStudioError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/import-apply")
def import_apply() -> dict[str, Any]:
    try:
        return svc.apply_import_from_excel()
    except svc.CanonicalRulesStudioError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/test-bench")
def test_bench(body: TestBenchBody) -> dict[str, Any]:
    try:
        return svc.run_test_bench(body.model_dump(exclude_none=True))
    except svc.CanonicalRulesStudioError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/test-fixtures")
def test_fixtures() -> dict[str, Any]:
    try:
        return svc.list_test_fixtures()
    except svc.CanonicalRulesStudioError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/{category}")
def get_category(category: str) -> dict[str, Any]:
    try:
        return svc.category_payload(category)
    except svc.CanonicalRulesStudioError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.patch("/{category}")
def patch_category(category: str, body: PatchBody) -> dict[str, Any]:
    try:
        result = svc.apply_category_patch(category, body.patch or {})
        return {
            "result": result,
            "category": svc.category_payload(category),
        }
    except svc.CanonicalRulesStudioError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
