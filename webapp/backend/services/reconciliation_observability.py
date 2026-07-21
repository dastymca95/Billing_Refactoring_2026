"""Typed, accounting-neutral reconciliation observability.

This module records whether arithmetic reconciliation ran and what it found.
It deliberately does not decide visual completeness, accounting readiness, GL
selection, or export authorization.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict


class ReconciliationState(str, Enum):
    NOT_RUN = "not_run"
    RAN_RECONCILED = "ran_reconciled"
    RAN_UNRECONCILED = "ran_unreconciled"
    RAN_INCONCLUSIVE = "ran_inconclusive"
    UNAVAILABLE_DUE_TO_MISSING_FACTS = "unavailable_due_to_missing_facts"


class ReconciliationStatus(str, Enum):
    NOT_RUN = "not_run"
    RECONCILED = "reconciled"
    UNRECONCILED = "unreconciled"
    INCONCLUSIVE = "inconclusive"
    UNAVAILABLE_DUE_TO_MISSING_FACTS = "unavailable_due_to_missing_facts"


class SupplementaryVisualStatus(str, Enum):
    NOT_RUN = "not_run"
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    CONTRADICTION = "contradiction"
    REQUEST_LIMIT_REACHED = "request_limit_reached"


class ReconciliationObservation(BaseModel):
    """Safe state propagated from validation to a terminal disposition."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: ReconciliationState
    reconciliation_ran: bool
    reconciliation_status: ReconciliationStatus
    reconciliation_source_stage: str
    reconciliation_before: ReconciliationStatus | None = None
    reconciliation_after: ReconciliationStatus | None = None
    reconciliation_delta_before: Decimal | None = None
    reconciliation_delta_after: Decimal | None = None
    supplementary_visual_status: SupplementaryVisualStatus


def unavailable_reconciliation() -> ReconciliationObservation:
    return ReconciliationObservation(
        state=ReconciliationState.UNAVAILABLE_DUE_TO_MISSING_FACTS,
        reconciliation_ran=False,
        reconciliation_status=ReconciliationStatus.UNAVAILABLE_DUE_TO_MISSING_FACTS,
        reconciliation_source_stage="facts_validation",
        supplementary_visual_status=SupplementaryVisualStatus.NOT_RUN,
    )


def observe_reconciliation(
    payload: Mapping[str, Any], *, facts_exist: bool = True,
) -> ReconciliationObservation:
    """Derive one typed observation without conflating visual resolution.

    Supplementary verification can leave a visual target unresolved while the
    independently observed arithmetic remains reconciled.  Both facts are
    retained here instead of collapsing them into one boolean.
    """

    if not facts_exist:
        return unavailable_reconciliation()

    existing = payload.get("reconciliation_observation")
    if isinstance(existing, Mapping):
        try:
            return ReconciliationObservation.model_validate(existing)
        except (TypeError, ValueError):
            # Fall through to deterministic derivation from the underlying
            # validation fields; never trust an invalid serialized summary.
            pass

    supplementary = payload.get("supplementary_reconciliation")
    if isinstance(supplementary, Mapping):
        before = _status_from_value(supplementary.get("before"))
        after = _status_from_value(supplementary.get("after"))
        status = after or ReconciliationStatus.INCONCLUSIVE
        return _observation(
            status=status,
            source_stage="supplementary_visual_verification",
            before=before,
            after=after,
            delta_before=_delta_from_value(supplementary.get("before")),
            delta_after=_delta_from_value(supplementary.get("after")),
            supplementary_status=_supplementary_status(payload, supplementary),
        )

    summary = (
        payload.get("validation_summary")
        if isinstance(payload.get("validation_summary"), Mapping)
        else payload
    )
    status = _status_from_value(summary.get("reconciliation_status"))
    if status is None and "total_reconciliation_passed" in summary:
        status = (
            ReconciliationStatus.RECONCILED
            if summary.get("total_reconciliation_passed") is True
            else ReconciliationStatus.UNRECONCILED
        )
    if status is None and summary.get("reconciliation_ran") is True:
        status = ReconciliationStatus.INCONCLUSIVE
    if status is None:
        return ReconciliationObservation(
            state=ReconciliationState.NOT_RUN,
            reconciliation_ran=False,
            reconciliation_status=ReconciliationStatus.NOT_RUN,
            reconciliation_source_stage="local_validation",
            supplementary_visual_status=_supplementary_status(payload, {}),
        )
    return _observation(
        status=status,
        source_stage=str(summary.get("reconciliation_source_stage") or "local_validation"),
        before=_status_from_value(summary.get("reconciliation_before")),
        after=_status_from_value(summary.get("reconciliation_after")) or status,
        delta_before=_delta_from_value(summary.get("reconciliation_delta_before")),
        delta_after=_delta_from_value(summary.get("reconciliation_delta_after")),
        supplementary_status=_supplementary_status(payload, {}),
    )


def combine_reconciliation_observations(
    observations: Sequence[ReconciliationObservation], *, facts_exist: bool,
) -> ReconciliationObservation:
    """Combine invoice-level observations for one terminal document result."""

    if not facts_exist:
        return unavailable_reconciliation()
    usable = [
        item for item in observations
        if item.state not in {
            ReconciliationState.NOT_RUN,
            ReconciliationState.UNAVAILABLE_DUE_TO_MISSING_FACTS,
        }
    ]
    if not usable:
        return ReconciliationObservation(
            state=ReconciliationState.NOT_RUN,
            reconciliation_ran=False,
            reconciliation_status=ReconciliationStatus.NOT_RUN,
            reconciliation_source_stage="local_validation",
            supplementary_visual_status=_highest_supplementary_status(observations),
        )
    statuses = {item.reconciliation_status for item in usable}
    if ReconciliationStatus.UNRECONCILED in statuses:
        status = ReconciliationStatus.UNRECONCILED
    elif ReconciliationStatus.INCONCLUSIVE in statuses or len(statuses) != 1:
        status = ReconciliationStatus.INCONCLUSIVE
    else:
        status = ReconciliationStatus.RECONCILED
    return _observation(
        status=status,
        source_stage=(
            "supplementary_visual_verification"
            if any(
                item.reconciliation_source_stage == "supplementary_visual_verification"
                for item in usable
            )
            else "local_validation"
        ),
        before=next((item.reconciliation_before for item in usable if item.reconciliation_before), None),
        after=status,
        delta_before=next((
            item.reconciliation_delta_before for item in usable
            if item.reconciliation_delta_before is not None
        ), None),
        delta_after=next((
            item.reconciliation_delta_after for item in reversed(usable)
            if item.reconciliation_delta_after is not None
        ), None),
        supplementary_status=_highest_supplementary_status(observations),
    )


def _observation(
    *, status: ReconciliationStatus, source_stage: str,
    before: ReconciliationStatus | None, after: ReconciliationStatus | None,
    delta_before: Decimal | None, delta_after: Decimal | None,
    supplementary_status: SupplementaryVisualStatus,
) -> ReconciliationObservation:
    state = {
        ReconciliationStatus.RECONCILED: ReconciliationState.RAN_RECONCILED,
        ReconciliationStatus.UNRECONCILED: ReconciliationState.RAN_UNRECONCILED,
        ReconciliationStatus.INCONCLUSIVE: ReconciliationState.RAN_INCONCLUSIVE,
    }.get(status, ReconciliationState.NOT_RUN)
    return ReconciliationObservation(
        state=state,
        reconciliation_ran=state in {
            ReconciliationState.RAN_RECONCILED,
            ReconciliationState.RAN_UNRECONCILED,
            ReconciliationState.RAN_INCONCLUSIVE,
        },
        reconciliation_status=status,
        reconciliation_source_stage=source_stage,
        reconciliation_before=before,
        reconciliation_after=after,
        reconciliation_delta_before=delta_before,
        reconciliation_delta_after=delta_after,
        supplementary_visual_status=supplementary_status,
    )


def _delta_from_value(value: Any) -> Decimal | None:
    if isinstance(value, Mapping):
        value = value.get("difference")
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        result = Decimal(str(value).replace("$", "").replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _status_from_value(value: Any) -> ReconciliationStatus | None:
    if isinstance(value, Mapping):
        if value.get("reconciled") is True:
            return ReconciliationStatus.RECONCILED
        if value.get("reconciled") is False:
            return ReconciliationStatus.UNRECONCILED
        value = value.get("status")
    if value is True:
        return ReconciliationStatus.RECONCILED
    if value is False:
        return ReconciliationStatus.UNRECONCILED
    token = str(value or "").strip().casefold()
    if token in {"reconciled", "passed", "match", "matched", "ok"}:
        return ReconciliationStatus.RECONCILED
    if token in {"unreconciled", "mismatch", "failed", "difference"}:
        return ReconciliationStatus.UNRECONCILED
    if token in {"inconclusive", "unknown", "not_computed"}:
        return ReconciliationStatus.INCONCLUSIVE
    if token == "not_run":
        return ReconciliationStatus.NOT_RUN
    if token == "unavailable_due_to_missing_facts":
        return ReconciliationStatus.UNAVAILABLE_DUE_TO_MISSING_FACTS
    return None


def _supplementary_status(
    payload: Mapping[str, Any], supplementary: Mapping[str, Any],
) -> SupplementaryVisualStatus:
    codes = {
        str(value or "").strip().casefold()
        for value in (
            *(payload.get("warnings") or []),
            *(payload.get("manual_review_codes") or []),
        )
    }
    revisions = [
        item for item in payload.get("supplementary_evidence_revisions") or []
        if isinstance(item, Mapping)
    ]
    observations = [
        item.get("observation") for item in revisions
        if isinstance(item.get("observation"), Mapping)
    ]
    if "supplementary_visual_evidence_contradiction" in codes or any(
        item.get("contradiction_flag") is True for item in observations
    ):
        return SupplementaryVisualStatus.CONTRADICTION
    if "supplementary_request_limit_reached" in codes:
        return SupplementaryVisualStatus.REQUEST_LIMIT_REACHED
    if "supplementary_visual_evidence_unresolved" in codes or any(
        item.get("unresolved_flag") is True for item in observations
    ) or supplementary.get("resolved") is False:
        return SupplementaryVisualStatus.UNRESOLVED
    if supplementary or revisions:
        return SupplementaryVisualStatus.RESOLVED
    return SupplementaryVisualStatus.NOT_RUN


def _highest_supplementary_status(
    observations: Sequence[ReconciliationObservation],
) -> SupplementaryVisualStatus:
    priority = (
        SupplementaryVisualStatus.CONTRADICTION,
        SupplementaryVisualStatus.REQUEST_LIMIT_REACHED,
        SupplementaryVisualStatus.UNRESOLVED,
        SupplementaryVisualStatus.RESOLVED,
        SupplementaryVisualStatus.NOT_RUN,
    )
    present = {item.supplementary_visual_status for item in observations}
    return next((item for item in priority if item in present), SupplementaryVisualStatus.NOT_RUN)


__all__ = [
    "ReconciliationObservation",
    "ReconciliationState",
    "ReconciliationStatus",
    "SupplementaryVisualStatus",
    "combine_reconciliation_observations",
    "observe_reconciliation",
    "unavailable_reconciliation",
]
