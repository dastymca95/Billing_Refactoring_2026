"""Private, append-only operator and AI activity trail.

This runtime log is the audit surface for the invoice workspace.  It records
who initiated a change and where it came from; it is not accounting evidence,
does not alter decisions, and is never used to authorize export.
"""
from __future__ import annotations

import json
import hashlib
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .. import settings


ACTIVITY_CONTRACT_VERSION = "operator-activity/1.0"
_LOCK = threading.RLock()
_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


class OperatorActivityEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: str = ACTIVITY_CONTRACT_VERSION
    event_id: str
    batch_id: str
    invoice_group_id: str | None = None
    event_type: str
    source: Literal["manual", "ai", "rule", "system"]
    actor: str
    summary: str
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


def record(
    *, batch_id: str, event_type: str, source: Literal["manual", "ai", "rule", "system"],
    actor: str, summary: str, invoice_group_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> OperatorActivityEvent:
    event = OperatorActivityEvent(
        event_id="oae_" + uuid.uuid4().hex[:16],
        batch_id=str(batch_id),
        invoice_group_id=str(invoice_group_id) if invoice_group_id else None,
        event_type=str(event_type),
        source=source,
        actor=str(actor or "local_operator"),
        summary=str(summary),
        details=dict(details or {}),
        created_at=datetime.now(timezone.utc),
    )
    path = _batch_path(event.batch_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.model_dump(mode="json"), ensure_ascii=False) + "\n")
    return event


def list_events(
    *, batch_id: str | None = None, invoice_group_id: str | None = None,
    limit: int = 500,
) -> list[OperatorActivityEvent]:
    paths = [_batch_path(batch_id)] if batch_id else sorted(_root().glob("*.jsonl"))
    events: list[OperatorActivityEvent] = []
    with _LOCK:
        for path in paths:
            if not path.is_file():
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    event = OperatorActivityEvent(**json.loads(line))
                except (ValueError, TypeError):
                    continue
                if invoice_group_id and event.invoice_group_id != invoice_group_id:
                    continue
                events.append(event)
    if batch_id:
        events.extend(_approved_correction_events(
            batch_id=batch_id,
            invoice_group_id=invoice_group_id,
            existing=events,
        ))
    events.sort(key=lambda item: item.created_at, reverse=True)
    return events[:max(1, min(int(limit), 2000))]


def _approved_correction_events(
    *, batch_id: str, invoice_group_id: str | None,
    existing: list[OperatorActivityEvent],
) -> list[OperatorActivityEvent]:
    """Expose pre-activity-log approvals from their durable runtime evidence.

    Approved corrections created before ``operator-activity/1.0`` already
    contain the interaction, actor, invoice and approval timestamp needed for
    an audit entry.  Deriving the missing event at read time keeps that legacy
    history visible without mutating the append-only log or fabricating data.
    """
    from .approved_invoice_corrections import list_corrections

    recorded_interactions = {
        str(event.details.get("interaction_id"))
        for event in existing
        if event.event_type == "ai_corrections_applied"
        and event.details.get("interaction_id")
    }
    grouped: dict[tuple[str, str], list[Any]] = {}
    for correction in list_corrections(batch_id=batch_id):
        if correction.status != "active":
            continue
        if invoice_group_id and correction.invoice_group_id != invoice_group_id:
            continue
        if correction.interaction_id in recorded_interactions:
            continue
        grouped.setdefault(
            (correction.interaction_id, correction.invoice_group_id), [],
        ).append(correction)

    derived: list[OperatorActivityEvent] = []
    for (interaction_id, group_id), corrections in grouped.items():
        corrections.sort(key=lambda item: item.approved_at)
        actor = corrections[-1].approved_by
        approved_at = max(item.approved_at for item in corrections)
        stable = hashlib.sha256(
            f"{batch_id}|{interaction_id}|approved-corrections".encode("utf-8")
        ).hexdigest()[:16]
        count = len(corrections)
        derived.append(OperatorActivityEvent(
            event_id=f"oae_legacy_{stable}",
            batch_id=batch_id,
            invoice_group_id=group_id,
            event_type="ai_corrections_applied",
            source="ai",
            actor=actor,
            summary=(
                f"Approved and applied {count} AI-proposed correction"
                f"{'s' if count != 1 else ''}."
            ),
            details={
                "interaction_id": interaction_id,
                "correction_count": count,
                "derived_from": "approved_invoice_corrections",
                "legacy_adapter": True,
            },
            created_at=approved_at,
        ))
    return derived


def _root() -> Path:
    return settings.WEBAPP_DATA_ROOT / "operator_activity"


def _batch_path(batch_id: str) -> Path:
    safe = _SAFE.sub("_", str(batch_id or "unknown")).strip("._")[:160] or "unknown"
    return _root() / f"{safe}.jsonl"


__all__ = ["ACTIVITY_CONTRACT_VERSION", "OperatorActivityEvent", "list_events", "record"]
