"""Private, versioned operator preferences for document processing routes.

This module records *requested* routing policy only.  It does not detect a
vendor, execute a deterministic processor, call an AI provider, or decide
accounting readiness.  The processing orchestrator remains responsible for
turning the resolved request into an executable route.

The cost-safe default is ``auto_cost_safe``.  In that mode an orchestrator is
expected to prefer an available deterministic processor and consider AI only
when no deterministic route is available.  ``deterministic_only`` explicitly
forbids an AI fallback.  ``ai_fallback_allowed`` is an explicit operator opt-in
to a route where AI may be used.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import batch_store


CONTRACT_VERSION = "processing-route-policy/1.0"
DEFAULT_MODE = "auto_cost_safe"
STORE_DIRECTORY = "routing"
STORE_FILENAME = "processing_route_policy.json"
_LOCK = threading.RLock()


class ProcessingRouteMode(str, Enum):
    """Operator-selectable route policy.

    These values describe authorization to choose a route; they do not claim
    that a processor or provider is available.
    """

    AUTO_COST_SAFE = "auto_cost_safe"
    DETERMINISTIC_ONLY = "deterministic_only"
    AI_FALLBACK_ALLOWED = "ai_fallback_allowed"


class ProcessingRouteScope(str, Enum):
    BATCH = "batch"
    DOCUMENT = "document"
    PAGE = "page"


class ProcessingRouteOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: ProcessingRouteScope
    requested_mode: ProcessingRouteMode
    filename: str | None = None
    page: int | None = Field(default=None, ge=1)
    actor: str = Field(min_length=1, max_length=200)
    updated_at: datetime

    @model_validator(mode="after")
    def validate_scope_identity(self) -> "ProcessingRouteOverride":
        if self.scope == ProcessingRouteScope.BATCH:
            if self.filename is not None or self.page is not None:
                raise ValueError("A batch override cannot contain a filename or page.")
        elif self.scope == ProcessingRouteScope.DOCUMENT:
            if self.filename is None or self.page is not None:
                raise ValueError("A document override requires only a filename.")
        elif self.filename is None or self.page is None:
            raise ValueError("A page override requires a filename and page.")
        return self


class ProcessingRouteAuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    action: Literal["set", "clear", "reset_exceptions", "apply_bulk"]
    scope: ProcessingRouteScope
    actor: str = Field(min_length=1, max_length=200)
    occurred_at: datetime
    requested_mode: ProcessingRouteMode | None = None
    previous_mode: ProcessingRouteMode | None = None
    filename: str | None = None
    page: int | None = Field(default=None, ge=1)
    cleared_document_overrides: int = Field(default=0, ge=0)
    cleared_page_overrides: int = Field(default=0, ge=0)


class ProcessingRoutePolicy(BaseModel):
    """Durable per-batch policy and its complete local audit history."""

    model_config = ConfigDict(extra="forbid")

    contract_version: str = CONTRACT_VERSION
    batch_id: str
    batch_override: ProcessingRouteOverride | None = None
    document_overrides: list[ProcessingRouteOverride] = Field(default_factory=list)
    page_overrides: list[ProcessingRouteOverride] = Field(default_factory=list)
    audit: list[ProcessingRouteAuditEvent] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_contract_and_uniqueness(self) -> "ProcessingRoutePolicy":
        if self.contract_version != CONTRACT_VERSION:
            raise ValueError(
                f"Unsupported processing route contract: {self.contract_version!r}."
            )
        if self.batch_override and self.batch_override.scope != ProcessingRouteScope.BATCH:
            raise ValueError("batch_override must have batch scope.")
        document_keys: set[str] = set()
        for item in self.document_overrides:
            if item.scope != ProcessingRouteScope.DOCUMENT or item.filename is None:
                raise ValueError("document_overrides may contain only document overrides.")
            key = _filename_key(item.filename)
            if key in document_keys:
                raise ValueError("Duplicate document route override.")
            document_keys.add(key)
        page_keys: set[tuple[str, int]] = set()
        for item in self.page_overrides:
            if item.scope != ProcessingRouteScope.PAGE or item.filename is None or item.page is None:
                raise ValueError("page_overrides may contain only page overrides.")
            key = (_filename_key(item.filename), item.page)
            if key in page_keys:
                raise ValueError("Duplicate page route override.")
            page_keys.add(key)
        return self


class ProcessingRouteResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: str = CONTRACT_VERSION
    batch_id: str
    filename: str | None = None
    page: int | None = Field(default=None, ge=1)
    requested_mode: ProcessingRouteMode
    inherited_from: Literal["page", "document", "batch", "default"]
    configured_by: str | None = None
    configured_at: datetime | None = None


class ProcessingRoutePolicyError(RuntimeError):
    """Raised when a persisted route policy cannot be trusted."""


def get_policy(batch_id: str) -> ProcessingRoutePolicy:
    """Load one batch policy, returning an in-memory default when absent."""
    with _LOCK:
        return _load(batch_id)


def resolve_requested_mode(
    batch_id: str,
    *,
    filename: str | None = None,
    page: int | None = None,
) -> ProcessingRouteResolution:
    """Resolve ``page > document > batch > default`` without side effects."""
    safe_filename = _validate_filename(filename) if filename is not None else None
    safe_page = _validate_page(page) if page is not None else None
    if safe_page is not None and safe_filename is None:
        raise ValueError("A page can be resolved only with a filename.")
    policy = get_policy(batch_id)
    chosen: ProcessingRouteOverride | None = None
    inherited_from: Literal["page", "document", "batch", "default"] = "default"
    if safe_filename is not None and safe_page is not None:
        chosen = _find_page(policy, safe_filename, safe_page)
        if chosen:
            inherited_from = "page"
    if chosen is None and safe_filename is not None:
        chosen = _find_document(policy, safe_filename)
        if chosen:
            inherited_from = "document"
    if chosen is None and policy.batch_override is not None:
        chosen = policy.batch_override
        inherited_from = "batch"
    return ProcessingRouteResolution(
        batch_id=batch_id,
        filename=safe_filename,
        page=safe_page,
        requested_mode=(chosen.requested_mode if chosen else ProcessingRouteMode(DEFAULT_MODE)),
        inherited_from=inherited_from,
        configured_by=chosen.actor if chosen else None,
        configured_at=chosen.updated_at if chosen else None,
    )


def set_route_mode(
    batch_id: str,
    *,
    scope: ProcessingRouteScope | str,
    mode: ProcessingRouteMode | str,
    actor: str,
    filename: str | None = None,
    page: int | None = None,
    reset_exceptions: bool = False,
) -> ProcessingRoutePolicy:
    """Set an override at one scope.

    ``reset_exceptions`` is valid only for batch scope and atomically removes
    document/page exceptions as part of the same write.
    """
    parsed_scope = ProcessingRouteScope(scope)
    parsed_mode = ProcessingRouteMode(mode)
    safe_actor = _validate_actor(actor)
    safe_filename, safe_page = _validate_scope(parsed_scope, filename, page)
    if reset_exceptions and parsed_scope != ProcessingRouteScope.BATCH:
        raise ValueError("reset_exceptions is available only for batch scope.")
    with _LOCK:
        policy = _load(batch_id)
        now = _now()
        previous = _find_override(policy, parsed_scope, safe_filename, safe_page)
        override = ProcessingRouteOverride(
            scope=parsed_scope,
            requested_mode=parsed_mode,
            filename=safe_filename,
            page=safe_page,
            actor=safe_actor,
            updated_at=now,
        )
        if parsed_scope == ProcessingRouteScope.BATCH:
            policy.batch_override = override
        elif parsed_scope == ProcessingRouteScope.DOCUMENT:
            policy.document_overrides = [
                item for item in policy.document_overrides
                if _filename_key(item.filename or "") != _filename_key(safe_filename or "")
            ]
            policy.document_overrides.append(override)
        else:
            policy.page_overrides = [
                item for item in policy.page_overrides
                if not (
                    _filename_key(item.filename or "") == _filename_key(safe_filename or "")
                    and item.page == safe_page
                )
            ]
            policy.page_overrides.append(override)
        cleared_documents = 0
        cleared_pages = 0
        action: Literal["set", "apply_bulk"] = "set"
        if reset_exceptions:
            cleared_documents = len(policy.document_overrides)
            cleared_pages = len(policy.page_overrides)
            policy.document_overrides = []
            policy.page_overrides = []
            action = "apply_bulk"
        policy.updated_at = now
        policy.audit.append(ProcessingRouteAuditEvent(
            event_id=_event_id(),
            action=action,
            scope=parsed_scope,
            actor=safe_actor,
            occurred_at=now,
            requested_mode=parsed_mode,
            previous_mode=previous.requested_mode if previous else None,
            filename=safe_filename,
            page=safe_page,
            cleared_document_overrides=cleared_documents,
            cleared_page_overrides=cleared_pages,
        ))
        _write(policy)
        return policy


def set_batch_mode(
    batch_id: str,
    mode: ProcessingRouteMode | str,
    *,
    actor: str,
    reset_exceptions: bool = False,
) -> ProcessingRoutePolicy:
    return set_route_mode(
        batch_id, scope=ProcessingRouteScope.BATCH, mode=mode,
        actor=actor, reset_exceptions=reset_exceptions,
    )


def set_document_mode(
    batch_id: str,
    filename: str,
    mode: ProcessingRouteMode | str,
    *,
    actor: str,
) -> ProcessingRoutePolicy:
    return set_route_mode(
        batch_id, scope=ProcessingRouteScope.DOCUMENT, mode=mode,
        actor=actor, filename=filename,
    )


def set_page_mode(
    batch_id: str,
    filename: str,
    page: int,
    mode: ProcessingRouteMode | str,
    *,
    actor: str,
) -> ProcessingRoutePolicy:
    return set_route_mode(
        batch_id, scope=ProcessingRouteScope.PAGE, mode=mode,
        actor=actor, filename=filename, page=page,
    )


def apply_bulk_mode(
    batch_id: str,
    mode: ProcessingRouteMode | str,
    *,
    actor: str,
) -> ProcessingRoutePolicy:
    """Set the batch policy and reset every document/page exception."""
    return set_batch_mode(batch_id, mode, actor=actor, reset_exceptions=True)


def clear_route_mode(
    batch_id: str,
    *,
    scope: ProcessingRouteScope | str,
    actor: str,
    filename: str | None = None,
    page: int | None = None,
) -> ProcessingRoutePolicy:
    """Clear one explicit override so the next-highest scope is inherited."""
    parsed_scope = ProcessingRouteScope(scope)
    safe_actor = _validate_actor(actor)
    safe_filename, safe_page = _validate_scope(parsed_scope, filename, page)
    with _LOCK:
        policy = _load(batch_id)
        previous = _find_override(policy, parsed_scope, safe_filename, safe_page)
        if parsed_scope == ProcessingRouteScope.BATCH:
            policy.batch_override = None
        elif parsed_scope == ProcessingRouteScope.DOCUMENT:
            policy.document_overrides = [
                item for item in policy.document_overrides
                if _filename_key(item.filename or "") != _filename_key(safe_filename or "")
            ]
        else:
            policy.page_overrides = [
                item for item in policy.page_overrides
                if not (
                    _filename_key(item.filename or "") == _filename_key(safe_filename or "")
                    and item.page == safe_page
                )
            ]
        now = _now()
        policy.updated_at = now
        policy.audit.append(ProcessingRouteAuditEvent(
            event_id=_event_id(), action="clear", scope=parsed_scope,
            actor=safe_actor, occurred_at=now,
            previous_mode=previous.requested_mode if previous else None,
            filename=safe_filename, page=safe_page,
        ))
        _write(policy)
        return policy


def reset_exceptions(batch_id: str, *, actor: str) -> ProcessingRoutePolicy:
    """Clear all document and page overrides while preserving batch mode."""
    safe_actor = _validate_actor(actor)
    with _LOCK:
        policy = _load(batch_id)
        cleared_documents = len(policy.document_overrides)
        cleared_pages = len(policy.page_overrides)
        policy.document_overrides = []
        policy.page_overrides = []
        now = _now()
        policy.updated_at = now
        policy.audit.append(ProcessingRouteAuditEvent(
            event_id=_event_id(), action="reset_exceptions",
            scope=ProcessingRouteScope.BATCH, actor=safe_actor, occurred_at=now,
            requested_mode=(policy.batch_override.requested_mode if policy.batch_override else None),
            cleared_document_overrides=cleared_documents,
            cleared_page_overrides=cleared_pages,
        ))
        _write(policy)
        return policy


def _store_path(batch_id: str) -> Path:
    return batch_store.get_batch_dir(batch_id) / STORE_DIRECTORY / STORE_FILENAME


def _load(batch_id: str) -> ProcessingRoutePolicy:
    path = _store_path(batch_id)
    if not path.is_file():
        now = _now()
        return ProcessingRoutePolicy(batch_id=batch_id, created_at=now, updated_at=now)
    try:
        raw = path.read_text(encoding="utf-8")
        policy = ProcessingRoutePolicy.model_validate_json(raw)
    except (OSError, ValueError) as exc:
        raise ProcessingRoutePolicyError(
            "The processing route policy is unreadable or violates its contract."
        ) from exc
    if policy.batch_id != batch_id:
        raise ProcessingRoutePolicyError("The processing route policy belongs to another batch.")
    return policy


def _write(policy: ProcessingRoutePolicy) -> None:
    path = _store_path(policy.batch_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{STORE_FILENAME}.{uuid.uuid4().hex}.tmp"
    try:
        payload = json.dumps(
            policy.model_dump(mode="json"), ensure_ascii=False, indent=2,
            sort_keys=True,
        )
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _find_override(
    policy: ProcessingRoutePolicy,
    scope: ProcessingRouteScope,
    filename: str | None,
    page: int | None,
) -> ProcessingRouteOverride | None:
    if scope == ProcessingRouteScope.BATCH:
        return policy.batch_override
    if scope == ProcessingRouteScope.DOCUMENT:
        return _find_document(policy, filename or "")
    return _find_page(policy, filename or "", page or 0)


def _find_document(policy: ProcessingRoutePolicy, filename: str) -> ProcessingRouteOverride | None:
    key = _filename_key(filename)
    return next(
        (item for item in policy.document_overrides if _filename_key(item.filename or "") == key),
        None,
    )


def _find_page(
    policy: ProcessingRoutePolicy, filename: str, page: int,
) -> ProcessingRouteOverride | None:
    key = _filename_key(filename)
    return next(
        (
            item for item in policy.page_overrides
            if _filename_key(item.filename or "") == key and item.page == page
        ),
        None,
    )


def _validate_scope(
    scope: ProcessingRouteScope,
    filename: str | None,
    page: int | None,
) -> tuple[str | None, int | None]:
    if scope == ProcessingRouteScope.BATCH:
        if filename is not None or page is not None:
            raise ValueError("Batch scope does not accept filename or page.")
        return None, None
    safe_filename = _validate_filename(filename)
    if scope == ProcessingRouteScope.DOCUMENT:
        if page is not None:
            raise ValueError("Document scope does not accept page.")
        return safe_filename, None
    return safe_filename, _validate_page(page)


def _validate_filename(filename: str | None) -> str:
    value = str(filename or "").strip()
    if not value or len(value) > 255:
        raise ValueError("A safe filename is required.")
    if value in {".", ".."} or Path(value).name != value or any(char in value for char in ("/", "\\", "\x00")):
        raise ValueError("Only a filename may be stored; paths are forbidden.")
    return value


def _filename_key(filename: str) -> str:
    return filename.casefold()


def _validate_page(page: int | None) -> int:
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ValueError("Page must be a positive one-based integer.")
    return page


def _validate_actor(actor: str) -> str:
    value = str(actor or "").strip()
    if not value or len(value) > 200 or "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError("A valid audit actor is required.")
    return value


def _event_id() -> str:
    return "rte_" + uuid.uuid4().hex[:20]


def _now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "CONTRACT_VERSION", "DEFAULT_MODE", "ProcessingRouteAuditEvent",
    "ProcessingRouteMode", "ProcessingRouteOverride", "ProcessingRoutePolicy",
    "ProcessingRoutePolicyError", "ProcessingRouteResolution", "ProcessingRouteScope",
    "apply_bulk_mode", "clear_route_mode", "get_policy", "reset_exceptions",
    "resolve_requested_mode", "set_batch_mode", "set_document_mode", "set_page_mode",
    "set_route_mode",
]
