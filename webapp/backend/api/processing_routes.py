"""Operator-controlled processing route policy endpoints.

The API exposes the persisted route *authorization* together with a runtime
decision for every document in the batch.  It never executes a deterministic
processor or an AI provider.  Only filenames are serialized; filesystem paths
remain private to the backend.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ..services import batch_processor, batch_store, processing_route_policy
from ..services.processing_route_gate import (
    ProcessingRouteDecision,
    decide_processing_route,
)
from ..services.processing_route_policy import (
    ProcessingRouteMode,
    ProcessingRoutePolicy,
    ProcessingRoutePolicyError,
    ProcessingRouteResolution,
    ProcessingRouteScope,
)
from ..services.vendor_detection import detect_vendor_for_file, fast_detection_context


router = APIRouter(prefix="/api/batches", tags=["processing_routes"])
CONTRACT_VERSION = "processing-route-api/1.0"
POLICY_VERSION_PREFIX = "prp_sha256_"
DEFAULT_ACTOR = "local_operator"


class ProcessingRoutePatch(BaseModel):
    """One explicit route-policy mutation.

    ``mode=None`` clears the selected override.  ``reset_exceptions`` is valid
    only while setting batch scope and is intentionally rejected for a clear,
    because combining those operations would make audit intent ambiguous.
    """

    model_config = ConfigDict(extra="forbid")

    scope: ProcessingRouteScope
    mode: ProcessingRouteMode | None = None
    filename: str | None = None
    page: int | None = Field(default=None, ge=1)
    actor: str = Field(default=DEFAULT_ACTOR, min_length=1, max_length=200)
    reset_exceptions: bool = False
    expected_policy_version: str | None = Field(default=None, min_length=1, max_length=128)


class RouteDetection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vendor_key: str
    confidence: float
    reason: str


class BatchRouteSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolution: ProcessingRouteResolution


class DocumentRouteSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str
    detection: RouteDetection
    decision: ProcessingRouteDecision


class PageRouteSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str
    page: int
    decision: ProcessingRouteDecision


class SanitizedRouteAuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    action: str
    scope: str
    actor: str
    occurred_at: str
    requested_mode: str | None = None
    previous_mode: str | None = None
    filename: str | None = None
    page: int | None = None
    cleared_document_overrides: int = 0
    cleared_page_overrides: int = 0


class ProcessingRoutesSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: str = CONTRACT_VERSION
    policy_version: str
    batch: BatchRouteSnapshot
    documents: list[DocumentRouteSnapshot]
    pages: list[PageRouteSnapshot]
    audit: list[SanitizedRouteAuditEvent]


@router.get(
    "/{batch_id}/processing-routes",
    response_model=ProcessingRoutesSnapshot,
)
def get_processing_routes(batch_id: str) -> ProcessingRoutesSnapshot:
    """Return policy resolution and side-effect-free route decisions."""

    return _build_snapshot(batch_id)


@router.patch(
    "/{batch_id}/processing-routes",
    response_model=ProcessingRoutesSnapshot,
)
def patch_processing_routes(
    batch_id: str,
    body: ProcessingRoutePatch,
) -> ProcessingRoutesSnapshot:
    """Set or clear one scoped policy with optimistic concurrency."""

    try:
        files = batch_store.list_files_in_batch(batch_id)
        current = processing_route_policy.get_policy(batch_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Batch not found.") from exc
    except ProcessingRoutePolicyError as exc:
        raise HTTPException(status_code=409, detail="Processing route policy is invalid.") from exc

    current_version = _policy_version(current)
    if (
        body.expected_policy_version is not None
        and body.expected_policy_version != current_version
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "processing_route_policy_version_conflict",
                "current_policy_version": current_version,
            },
        )

    safe_filename = _resolve_batch_filename(body.filename, files)
    if body.scope != ProcessingRouteScope.BATCH and safe_filename is None:
        raise HTTPException(status_code=422, detail="The selected document is not in this batch.")
    if body.scope == ProcessingRouteScope.BATCH and body.filename is not None:
        raise HTTPException(status_code=422, detail="Batch scope does not accept a filename.")
    if body.mode is None and body.reset_exceptions:
        raise HTTPException(
            status_code=422,
            detail="reset_exceptions requires a batch mode to be set.",
        )

    try:
        if body.mode is None:
            processing_route_policy.clear_route_mode(
                batch_id,
                scope=body.scope,
                actor=body.actor,
                filename=safe_filename,
                page=body.page,
            )
        else:
            processing_route_policy.set_route_mode(
                batch_id,
                scope=body.scope,
                mode=body.mode,
                actor=body.actor,
                filename=safe_filename,
                page=body.page,
                reset_exceptions=body.reset_exceptions,
            )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ProcessingRoutePolicyError as exc:
        raise HTTPException(status_code=409, detail="Processing route policy is invalid.") from exc

    return _build_snapshot(batch_id)


def _build_snapshot(batch_id: str) -> ProcessingRoutesSnapshot:
    try:
        files = batch_store.list_files_in_batch(batch_id)
        policy = processing_route_policy.get_policy(batch_id)
        batch_resolution = processing_route_policy.resolve_requested_mode(batch_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Batch not found.") from exc
    except ProcessingRoutePolicyError as exc:
        raise HTTPException(status_code=409, detail="Processing route policy is invalid.") from exc

    detections: dict[str, RouteDetection] = {}
    documents: list[DocumentRouteSnapshot] = []
    for path in sorted(files, key=lambda item: item.name.casefold()):
        detection = _detect(path)
        detections[path.name.casefold()] = detection
        resolution = processing_route_policy.resolve_requested_mode(
            batch_id,
            filename=path.name,
        )
        documents.append(DocumentRouteSnapshot(
            filename=path.name,
            detection=detection,
            decision=_decision(resolution, detection),
        ))

    pages: list[PageRouteSnapshot] = []
    for override in sorted(
        policy.page_overrides,
        key=lambda item: ((item.filename or "").casefold(), item.page or 0),
    ):
        filename = override.filename or ""
        detection = detections.get(filename.casefold())
        if detection is None:
            # Keep a stale override auditable without probing or exposing any
            # old path that may no longer belong to the batch.
            detection = RouteDetection(
                vendor_key="unknown",
                confidence=0.0,
                reason="document_not_present_in_batch",
            )
        resolution = processing_route_policy.resolve_requested_mode(
            batch_id,
            filename=filename,
            page=override.page,
        )
        pages.append(PageRouteSnapshot(
            filename=filename,
            page=override.page or 1,
            decision=_decision(resolution, detection),
        ))

    return ProcessingRoutesSnapshot(
        policy_version=_policy_version(policy),
        batch=BatchRouteSnapshot(resolution=batch_resolution),
        documents=documents,
        pages=pages,
        audit=[_sanitize_audit(event.model_dump(mode="json")) for event in policy.audit],
    )


def _detect(path: Any) -> RouteDetection:
    try:
        # Route discovery is a UI/read endpoint. It may reuse cached OCR, but
        # it must never start fresh OCR/Vision work merely because a popover
        # opened; the processing run performs the full detection pass.
        with fast_detection_context():
            raw = detect_vendor_for_file(path)
        return RouteDetection(
            vendor_key=str(raw.get("vendor_key") or "unknown"),
            confidence=float(raw.get("confidence") or 0.0),
            reason=_safe_text(raw.get("reason"), fallback="vendor_detection_returned_no_reason"),
        )
    except Exception as exc:
        return RouteDetection(
            vendor_key="unknown",
            confidence=0.0,
            reason=f"vendor_detection_failed:{type(exc).__name__}",
        )


def _decision(
    resolution: ProcessingRouteResolution,
    detection: RouteDetection,
) -> ProcessingRouteDecision:
    registration = batch_processor._PROCESSOR_LOADERS.get(detection.vendor_key)
    processor_id = (
        f"{detection.vendor_key}.{registration[1]}"
        if registration is not None
        else None
    )
    return decide_processing_route(
        resolution,
        vendor_key=detection.vendor_key,
        deterministic_available=registration is not None,
        processor_id=processor_id,
    )


def _policy_version(policy: ProcessingRoutePolicy) -> str:
    # Exclude ephemeral timestamps created for an absent in-memory policy so
    # repeated GETs remain stable.  Overrides/audit retain their own timestamps
    # and event IDs, so every persisted mutation changes the version.
    material = policy.model_dump(
        mode="json",
        exclude={"created_at", "updated_at"},
    )
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return POLICY_VERSION_PREFIX + hashlib.sha256(encoded).hexdigest()


def _resolve_batch_filename(filename: str | None, files: list[Any]) -> str | None:
    if filename is None:
        return None
    value = str(filename).strip()
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
    ):
        raise HTTPException(status_code=422, detail="Only a filename may be selected; paths are forbidden.")
    return next((path.name for path in files if path.name.casefold() == value.casefold()), None)


def _sanitize_audit(raw: dict[str, Any]) -> SanitizedRouteAuditEvent:
    actor = _safe_text(raw.get("actor"), fallback=DEFAULT_ACTOR)
    if _looks_like_path(actor):
        actor = "redacted_operator"
    filename = raw.get("filename")
    if filename is not None and ("/" in str(filename) or "\\" in str(filename)):
        filename = None
    return SanitizedRouteAuditEvent(
        event_id=str(raw.get("event_id") or ""),
        action=str(raw.get("action") or ""),
        scope=str(raw.get("scope") or ""),
        actor=actor,
        occurred_at=str(raw.get("occurred_at") or ""),
        requested_mode=(str(raw["requested_mode"]) if raw.get("requested_mode") else None),
        previous_mode=(str(raw["previous_mode"]) if raw.get("previous_mode") else None),
        filename=(str(filename) if filename else None),
        page=raw.get("page"),
        cleared_document_overrides=int(raw.get("cleared_document_overrides") or 0),
        cleared_page_overrides=int(raw.get("cleared_page_overrides") or 0),
    )


def _safe_text(value: Any, *, fallback: str) -> str:
    text = str(value or "").replace("\x00", " ").replace("\r", " ").replace("\n", " ").strip()
    if not text:
        return fallback
    if _looks_like_path(text):
        return fallback
    return text[:500]


def _looks_like_path(value: str) -> bool:
    return bool(
        re.search(r"(?:^|\s)[A-Za-z]:[\\/]", value)
        or value.startswith("\\\\")
        or value.startswith("/")
    )


__all__ = [
    "CONTRACT_VERSION",
    "ProcessingRoutePatch",
    "ProcessingRoutesSnapshot",
    "router",
]
