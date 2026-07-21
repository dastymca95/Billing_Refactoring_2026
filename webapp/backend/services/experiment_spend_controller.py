"""Fail-closed private spend ledger for document-learning experiments.

The controller authorizes *experiment harness* dispatches only.  It never
contains credentials, prompts, source paths, filenames, response bodies or
headers.  A reservation must exist before a provider call is constructed.
"""
from __future__ import annotations

import json
import contextlib
import contextvars
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, Iterator
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field


SPEND_CONTRACT_VERSION = "document-learning-spend/1.1"
PHASE_CAPS_USD = {
    "A": Decimal("10.00"),
    "B": Decimal("40.00"),
    "C": Decimal("200.00"),
}
ALERT_THRESHOLDS = (Decimal("0.50"), Decimal("0.75"), Decimal("0.90"), Decimal("1.00"))
_LOCK = threading.RLock()
_CANCEL_CALLBACKS: dict[tuple[str, str], Callable[[], bool]] = {}
_ACTIVE_GATE: contextvars.ContextVar["ExperimentSpendGate | None"] = contextvars.ContextVar(
    "innerview_document_learning_spend_gate", default=None,
)


class ExperimentPhase(str, Enum):
    A = "A"
    B = "B"
    C = "C"


class SpendAuthorizationError(RuntimeError):
    """Raised before dispatch when a phase or budget gate is not satisfied."""


class ExperimentSpendGate(BaseModel):
    """In-process binding used only while an experiment invokes providers."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    controller: Any
    phase: ExperimentPhase
    pricing_version: str


class SpendReservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reservation_id: str
    phase: ExperimentPhase
    provider: str
    model_id: str
    profile_id: str
    stage: str
    document_sha256: str = ""
    purpose: str = ""
    estimated_cost_usd: str
    status: str = "reserved"
    actual_cost_usd: str | None = None
    charged_cost_usd: str | None = None
    provider_reported_usage: bool = False
    usage: dict[str, int | float | str] = Field(default_factory=dict)
    failure_code: str | None = None
    created_at: datetime
    settled_at: datetime | None = None


class SpendCostAccountingView(BaseModel):
    """Derived accounting view that never rewrites the persistent ledger.

    A failed request without provider usage is conservatively charged at the
    reservation estimate for budget safety.  That charge must not be reported
    as an actual provider cost.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    estimated_reserved_cost_usd: str
    estimated_safety_charge_usd: str
    actual_provider_cost_usd: str | None
    usage_reported: bool
    settlement_status: str


class SpendSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: str = SPEND_CONTRACT_VERSION
    experiment_id: str
    canceled: bool
    cumulative_charged_usd: str
    active_reserved_usd: str
    projected_usd: str
    openai_cumulative_charged_usd: str
    openai_active_reserved_usd: str
    openai_projected_usd: str
    current_phase: ExperimentPhase
    current_phase_cap_usd: str
    phase_a_accepted: bool
    phase_b_accepted: bool
    alerts_emitted: list[int]
    by_provider_profile: dict[str, dict[str, str | int]]
    outstanding_reservation_ids: list[str]


def spend_cost_accounting_view(reservation: SpendReservation) -> SpendCostAccountingView:
    """Classify ledger amounts without mutating historical reservations."""

    estimated = _format_money(_money(reservation.estimated_cost_usd))
    actual = (
        _format_money(_money(reservation.actual_cost_usd))
        if reservation.actual_cost_usd is not None
        else None
    )
    charged = (
        _format_money(_money(reservation.charged_cost_usd))
        if reservation.charged_cost_usd is not None
        else "0.000000"
    )
    usage_reported = bool(reservation.provider_reported_usage)

    if reservation.status == "reserved":
        settlement_status = "reserved_not_dispatched"
        safety_charge = "0.000000"
    elif reservation.status == "dispatched":
        settlement_status = "dispatched_unsettled"
        safety_charge = "0.000000"
    elif reservation.status == "aborted_before_dispatch":
        settlement_status = "aborted_before_dispatch"
        safety_charge = "0.000000"
    elif actual is not None and usage_reported:
        settlement_status = "verified_provider_cost"
        safety_charge = "0.000000"
    elif actual is not None:
        settlement_status = "provider_cost_without_usage_report"
        safety_charge = "0.000000"
    elif reservation.status == "failed":
        settlement_status = "failed_without_usage_safety_charged"
        safety_charge = charged
    elif reservation.status == "settled":
        settlement_status = "settled_without_usage_safety_charged"
        safety_charge = charged
    else:
        settlement_status = "unclassified_without_provider_cost"
        safety_charge = charged

    return SpendCostAccountingView(
        estimated_reserved_cost_usd=estimated,
        estimated_safety_charge_usd=safety_charge,
        actual_provider_cost_usd=actual,
        usage_reported=usage_reported,
        settlement_status=settlement_status,
    )


class ExperimentSpendController:
    """Persistent pre-dispatch budget authority for one private experiment."""

    def __init__(self, private_root: Path, experiment_id: str) -> None:
        self.private_root = Path(private_root).resolve()
        self.experiment_id = _safe_identifier(experiment_id)
        self.path = self.private_root / "telemetry" / "spend_ledger.json"
        self.events_path = self.private_root / "telemetry" / "spend_events.jsonl"
        self.lock_path = self.private_root / "telemetry" / "spend_ledger.lock"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._ledger_lock():
            if not self.path.exists():
                self._write_state(self._initial_state())

    def reserve(
        self,
        *,
        phase: ExperimentPhase | str,
        estimated_cost_usd: Decimal | float | str,
        provider: str,
        model_id: str,
        profile_id: str,
        stage: str,
        document_sha256: str = "",
        purpose: str = "",
    ) -> SpendReservation:
        phase = ExperimentPhase(phase)
        estimate = _money(estimated_cost_usd)
        if estimate <= 0:
            raise SpendAuthorizationError("provider_dispatch_requires_positive_cost_estimate")
        with self._ledger_lock():
            state = self._read_state()
            self._require_phase_authorized(state, phase)
            if state["canceled"]:
                raise SpendAuthorizationError("experiment_dispatch_canceled")
            charged, active = _totals(state)
            openai_charged, openai_active = _totals(
                state, capped_provider="openai"
            )
            cap = PHASE_CAPS_USD[phase.value]
            projected = charged + active + estimate
            openai_projected = openai_charged + openai_active + (
                estimate if reservation_provider(provider) == "openai" else Decimal("0")
            )
            if projected > cap or openai_projected > cap:
                self._append_event({
                    "event": "dispatch_blocked", "phase": phase.value,
                    "failure_code": "phase_cost_cap_would_be_exceeded",
                    "estimated_cost_usd": _format_money(estimate),
                    "projected_usd": _format_money(projected),
                    "openai_projected_usd": _format_money(openai_projected),
                    "cap_usd": _format_money(cap),
                })
                raise SpendAuthorizationError("phase_cost_cap_would_be_exceeded")
            reservation = SpendReservation(
                reservation_id="dlr_" + uuid.uuid4().hex[:20],
                phase=phase,
                provider=_safe_label(provider),
                model_id=_safe_label(model_id),
                profile_id=_safe_label(profile_id),
                stage=_safe_label(stage),
                document_sha256=_safe_sha256(document_sha256),
                purpose=_safe_label(purpose),
                estimated_cost_usd=_format_money(estimate),
                created_at=_now(),
            )
            state["reservations"][reservation.reservation_id] = reservation.model_dump(mode="json")
            self._emit_threshold_alerts(state, phase, projected)
            self._write_state(state)
            self._append_event({
                "event": "dispatch_reserved", "phase": phase.value,
                "reservation_id": reservation.reservation_id,
                "provider": reservation.provider, "model_id": reservation.model_id,
                "profile_id": reservation.profile_id, "stage": reservation.stage,
                "document_sha256": reservation.document_sha256,
                "purpose": reservation.purpose,
                "estimated_cost_usd": reservation.estimated_cost_usd,
            })
            return reservation

    def settle(
        self,
        reservation_id: str,
        *,
        actual_cost_usd: Decimal | float | str | None,
        usage: dict[str, int | float | str] | None = None,
        provider_reported_usage: bool = False,
        failure_code: str | None = None,
    ) -> SpendReservation:
        with self._ledger_lock():
            state = self._read_state()
            raw = state["reservations"].get(reservation_id)
            if raw is None:
                raise KeyError("unknown_spend_reservation")
            reservation = SpendReservation.model_validate(raw)
            if reservation.status == "reserved":
                raise SpendAuthorizationError("reservation_must_be_marked_dispatched_before_settlement")
            if reservation.status not in {"dispatched", "cancel_requested"}:
                return reservation
            estimate = _money(reservation.estimated_cost_usd)
            actual = _money(actual_cost_usd) if actual_cost_usd is not None else None
            charged = actual if actual is not None else estimate
            reservation.status = "failed" if failure_code else "settled"
            reservation.actual_cost_usd = _format_money(actual) if actual is not None else None
            reservation.charged_cost_usd = _format_money(charged)
            reservation.provider_reported_usage = bool(provider_reported_usage)
            reservation.usage = _safe_usage(usage or {})
            reservation.failure_code = _safe_label(failure_code) if failure_code else None
            reservation.settled_at = _now()
            state["reservations"][reservation_id] = reservation.model_dump(mode="json")
            _CANCEL_CALLBACKS.pop((self.experiment_id, reservation_id), None)
            cumulative, active = _totals(state)
            if cumulative + active > PHASE_CAPS_USD[reservation.phase.value]:
                state["canceled"] = True
                state["cancel_reason"] = "settled_cost_exceeded_phase_cap"
            self._write_state(state)
            self._append_event({
                "event": "dispatch_settled", "phase": reservation.phase.value,
                "reservation_id": reservation_id, "provider": reservation.provider,
                "model_id": reservation.model_id, "profile_id": reservation.profile_id,
                "document_sha256": reservation.document_sha256,
                "purpose": reservation.purpose,
                "estimated_cost_usd": reservation.estimated_cost_usd,
                "actual_cost_usd": reservation.actual_cost_usd,
                "charged_cost_usd": reservation.charged_cost_usd,
                "provider_reported_usage": reservation.provider_reported_usage,
                "failure_code": reservation.failure_code,
            })
            return reservation

    def mark_dispatched(
        self, reservation_id: str, *, cancel_callback: Callable[[], bool] | None = None,
    ) -> SpendReservation:
        """Mark the exact boundary immediately before network dispatch."""
        with self._ledger_lock():
            state = self._read_state()
            raw = state["reservations"].get(reservation_id)
            if raw is None:
                raise KeyError("unknown_spend_reservation")
            reservation = SpendReservation.model_validate(raw)
            if state["canceled"]:
                raise SpendAuthorizationError("experiment_dispatch_canceled")
            if reservation.status != "reserved":
                raise SpendAuthorizationError("reservation_not_dispatchable")
            reservation.status = "dispatched"
            state["reservations"][reservation_id] = reservation.model_dump(mode="json")
            self._write_state(state)
            if cancel_callback is not None:
                _CANCEL_CALLBACKS[(self.experiment_id, reservation_id)] = cancel_callback
            self._append_event({
                "event": "dispatch_started", "phase": reservation.phase.value,
                "reservation_id": reservation_id, "provider": reservation.provider,
                "model_id": reservation.model_id, "profile_id": reservation.profile_id,
                "document_sha256": reservation.document_sha256,
                "purpose": reservation.purpose,
            })
            return reservation

    def release_reserved(self, reservation_id: str, *, reason: str) -> SpendReservation:
        """Release work that failed locally before reaching the network boundary."""
        with self._ledger_lock():
            state = self._read_state()
            raw = state["reservations"].get(reservation_id)
            if raw is None:
                raise KeyError("unknown_spend_reservation")
            reservation = SpendReservation.model_validate(raw)
            if reservation.status != "reserved":
                raise SpendAuthorizationError("only_undispatched_reservations_can_be_released")
            reservation.status = "aborted_before_dispatch"
            reservation.charged_cost_usd = "0.000000"
            reservation.failure_code = _safe_label(reason)
            reservation.settled_at = _now()
            state["reservations"][reservation_id] = reservation.model_dump(mode="json")
            self._write_state(state)
            self._append_event({
                "event": "dispatch_aborted_before_network",
                "phase": reservation.phase.value,
                "reservation_id": reservation_id,
                "failure_code": reservation.failure_code,
            })
            return reservation

    def cancel_outstanding(self, *, reason: str) -> int:
        """Prevent new work and request cancellation of dispatched calls."""
        with self._ledger_lock():
            state = self._read_state()
            state["canceled"] = True
            state["cancel_reason"] = _safe_label(reason)
            canceled = 0
            cancel_requested = 0
            now = _now()
            for key, raw in list(state["reservations"].items()):
                reservation = SpendReservation.model_validate(raw)
                if reservation.status == "reserved":
                    reservation.status = "canceled"
                    reservation.charged_cost_usd = "0.000000"
                    reservation.settled_at = now
                    canceled += 1
                elif reservation.status == "dispatched":
                    reservation.status = "cancel_requested"
                    # A dispatched request may already incur provider charges.
                    # Retain the conservative reservation until settlement.
                    cancel_requested += 1
                    callback = _CANCEL_CALLBACKS.get((self.experiment_id, key))
                    if callback is not None:
                        try:
                            callback()
                        except Exception:
                            pass
                        finally:
                            _CANCEL_CALLBACKS.pop((self.experiment_id, key), None)
                else:
                    continue
                state["reservations"][key] = reservation.model_dump(mode="json")
            self._write_state(state)
            self._append_event({
                "event": "experiment_canceled", "reason": state["cancel_reason"],
                "queued_canceled": canceled,
                "dispatched_cancel_requested": cancel_requested,
            })
            return canceled + cancel_requested

    def reauthorize_dispatch_after_operator_approval(
        self,
        *,
        phase: ExperimentPhase | str,
        expected_cancel_reason: str,
        actor: str,
        authorization_reference: str,
    ) -> None:
        """Reopen a canceled experiment only under explicit, audited authority.

        Historical cancel and settlement events remain append-only.  This
        transition cannot clear outstanding work, change charges, accept a
        phase report, or bypass the phase cap.
        """

        phase = ExperimentPhase(phase)
        expected_reason = _safe_label(expected_cancel_reason)
        safe_actor = _safe_label(actor)
        safe_reference = _safe_label(authorization_reference)
        if not expected_reason or not safe_actor or not safe_reference:
            raise SpendAuthorizationError("explicit_reauthorization_metadata_required")
        with self._ledger_lock():
            state = self._read_state()
            if not state.get("canceled"):
                raise SpendAuthorizationError("experiment_is_not_canceled")
            actual_reason = str(state.get("cancel_reason") or "")
            if actual_reason != expected_reason:
                raise SpendAuthorizationError("experiment_cancel_reason_changed")
            outstanding = [
                SpendReservation.model_validate(raw)
                for raw in state["reservations"].values()
                if SpendReservation.model_validate(raw).status
                in {"reserved", "dispatched", "cancel_requested"}
            ]
            if outstanding:
                raise SpendAuthorizationError(
                    "experiment_reauthorization_requires_no_outstanding_requests"
                )
            charged, active = _totals(state)
            cap = PHASE_CAPS_USD[phase.value]
            if charged + active >= cap:
                raise SpendAuthorizationError("phase_cost_cap_exhausted")
            state["canceled"] = False
            state["cancel_reason"] = None
            state["last_reauthorization"] = {
                "phase": phase.value,
                "previous_cancel_reason": actual_reason,
                "actor": safe_actor,
                "authorization_reference": safe_reference,
                "at": _now().isoformat(),
            }
            self._write_state(state)
            self._append_event({
                "event": "experiment_dispatch_reauthorized",
                "phase": phase.value,
                "previous_cancel_reason": actual_reason,
                "actor": safe_actor,
                "authorization_reference": safe_reference,
                "cumulative_charged_usd": _format_money(charged),
                "phase_cap_usd": _format_money(cap),
            })

    def accept_phase_report(self, phase: ExperimentPhase | str, *, report_sha256: str, actor: str) -> None:
        """Record explicit operator acceptance; it never runs automatically."""
        phase = ExperimentPhase(phase)
        if phase is ExperimentPhase.C:
            raise ValueError("phase_c_has_no_automatic_successor")
        report_hash = str(report_sha256 or "").strip().lower()
        if len(report_hash) != 64 or any(ch not in "0123456789abcdef" for ch in report_hash):
            raise ValueError("phase_report_sha256_required")
        with self._ledger_lock():
            state = self._read_state()
            if phase is ExperimentPhase.B and not state["phase_a_accepted"]:
                raise SpendAuthorizationError("phase_b_acceptance_requires_phase_a_acceptance")
            if any(
                SpendReservation.model_validate(item).status in {"reserved", "dispatched", "cancel_requested"}
                for item in state["reservations"].values()
            ):
                raise SpendAuthorizationError("phase_report_cannot_be_accepted_with_outstanding_requests")
            key = "phase_a_accepted" if phase is ExperimentPhase.A else "phase_b_accepted"
            state[key] = True
            state[f"{key}_report_sha256"] = report_hash
            state[f"{key}_actor"] = _safe_label(actor)
            state[f"{key}_at"] = _now().isoformat()
            self._write_state(state)
            self._append_event({
                "event": "phase_report_accepted", "phase": phase.value,
                "report_sha256": report_hash, "actor": _safe_label(actor),
            })

    def snapshot(self, phase: ExperimentPhase | str = ExperimentPhase.A) -> SpendSnapshot:
        phase = ExperimentPhase(phase)
        with self._ledger_lock():
            state = self._read_state()
            charged, active = _totals(state)
            openai_charged, openai_active = _totals(
                state, capped_provider="openai"
            )
            grouped: dict[str, dict[str, Decimal | int]] = {}
            outstanding: list[str] = []
            for raw in state["reservations"].values():
                item = SpendReservation.model_validate(raw)
                key = f"{item.provider}:{item.profile_id}:{item.model_id}"
                target = grouped.setdefault(key, {
                    "requests": 0, "estimated": Decimal("0"),
                    "actual": Decimal("0"), "charged": Decimal("0"),
                })
                target["requests"] = int(target["requests"]) + 1
                target["estimated"] = Decimal(target["estimated"]) + _money(item.estimated_cost_usd)
                if item.actual_cost_usd is not None:
                    target["actual"] = Decimal(target["actual"]) + _money(item.actual_cost_usd)
                if item.charged_cost_usd is not None:
                    target["charged"] = Decimal(target["charged"]) + _money(item.charged_cost_usd)
                if item.status in {"reserved", "dispatched", "cancel_requested"}:
                    outstanding.append(item.reservation_id)
            rendered = {
                key: {
                    "requests": int(value["requests"]),
                    "estimated_cost_usd": _format_money(Decimal(value["estimated"])),
                    "actual_cost_usd": _format_money(Decimal(value["actual"])),
                    "charged_cost_usd": _format_money(Decimal(value["charged"])),
                }
                for key, value in sorted(grouped.items())
            }
            return SpendSnapshot(
                experiment_id=self.experiment_id,
                canceled=bool(state["canceled"]),
                cumulative_charged_usd=_format_money(charged),
                active_reserved_usd=_format_money(active),
                projected_usd=_format_money(charged + active),
                openai_cumulative_charged_usd=_format_money(openai_charged),
                openai_active_reserved_usd=_format_money(openai_active),
                openai_projected_usd=_format_money(openai_charged + openai_active),
                current_phase=phase,
                current_phase_cap_usd=_format_money(PHASE_CAPS_USD[phase.value]),
                phase_a_accepted=bool(state["phase_a_accepted"]),
                phase_b_accepted=bool(state["phase_b_accepted"]),
                alerts_emitted=sorted(int(item) for item in state["alerts_emitted"].get(phase.value, [])),
                by_provider_profile=rendered,
                outstanding_reservation_ids=sorted(outstanding),
            )

    @contextlib.contextmanager
    def _ledger_lock(self) -> Iterator[None]:
        """Serialize the JSON authority across threads and local processes."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            with self.lock_path.open("a+b") as handle:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    try:
                        yield
                    finally:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _require_phase_authorized(self, state: dict[str, Any], phase: ExperimentPhase) -> None:
        if phase is ExperimentPhase.B and not state["phase_a_accepted"]:
            raise SpendAuthorizationError("phase_b_requires_explicit_phase_a_acceptance")
        if phase is ExperimentPhase.C and not state["phase_b_accepted"]:
            raise SpendAuthorizationError("phase_c_requires_explicit_phase_b_acceptance")

    def _emit_threshold_alerts(
        self, state: dict[str, Any], phase: ExperimentPhase, projected: Decimal,
    ) -> None:
        cap = PHASE_CAPS_USD[phase.value]
        emitted = set(int(item) for item in state["alerts_emitted"].setdefault(phase.value, []))
        for threshold in ALERT_THRESHOLDS:
            percent = int(threshold * 100)
            if projected >= cap * threshold and percent not in emitted:
                emitted.add(percent)
                self._append_event({
                    "event": "budget_alert", "phase": phase.value,
                    "threshold_percent": percent,
                    "projected_usd": _format_money(projected),
                    "cap_usd": _format_money(cap),
                })
        state["alerts_emitted"][phase.value] = sorted(emitted)

    def _initial_state(self) -> dict[str, Any]:
        return {
            "contract_version": SPEND_CONTRACT_VERSION,
            "experiment_id": self.experiment_id,
            "canceled": False,
            "cancel_reason": None,
            "phase_a_accepted": False,
            "phase_b_accepted": False,
            "alerts_emitted": {"A": [], "B": [], "C": []},
            "reservations": {},
            "created_at": _now().isoformat(),
        }

    def _read_state(self) -> dict[str, Any]:
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if payload.get("contract_version") != SPEND_CONTRACT_VERSION:
            raise SpendAuthorizationError("unknown_spend_contract_version")
        if payload.get("experiment_id") != self.experiment_id:
            raise SpendAuthorizationError("spend_ledger_experiment_mismatch")
        return payload

    def _write_state(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def _append_event(self, payload: dict[str, Any]) -> None:
        event = {
            "contract_version": SPEND_CONTRACT_VERSION,
            "experiment_id": self.experiment_id,
            "at": _now().isoformat(),
            **payload,
        }
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")


def _totals(
    state: dict[str, Any], *, capped_provider: str | None = None,
) -> tuple[Decimal, Decimal]:
    charged = Decimal("0")
    active = Decimal("0")
    for raw in state.get("reservations", {}).values():
        item = SpendReservation.model_validate(raw)
        if capped_provider and reservation_provider(item.provider) != capped_provider:
            continue
        if item.status in {"reserved", "dispatched", "cancel_requested"}:
            active += _money(item.estimated_cost_usd)
        elif item.charged_cost_usd is not None:
            charged += _money(item.charged_cost_usd)
    return charged, active


def reservation_provider(value: str) -> str:
    normalized = str(value or "").strip().casefold().replace("_", "-")
    return "openai" if normalized in {"openai", "open-ai"} else normalized


@contextlib.contextmanager
def activate_experiment_spend_gate(
    controller: ExperimentSpendController,
    *,
    phase: ExperimentPhase | str,
    pricing_version: str,
) -> Iterator[ExperimentSpendGate]:
    """Make provider dispatch fail closed for the bounded experiment scope."""
    version = _safe_label(pricing_version)
    if not version:
        raise ValueError("pricing_version_required")
    gate = ExperimentSpendGate(
        controller=controller,
        phase=ExperimentPhase(phase),
        pricing_version=version,
    )
    token = _ACTIVE_GATE.set(gate)
    try:
        yield gate
    finally:
        _ACTIVE_GATE.reset(token)


def current_experiment_spend_gate() -> ExperimentSpendGate | None:
    return _ACTIVE_GATE.get()


def _money(value: Decimal | float | str | None) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("invalid_cost_value")
    if not number.is_finite() or number < 0:
        raise ValueError("invalid_cost_value")
    return number.quantize(Decimal("0.000001"))


def _format_money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.000001')):.6f}"


def _safe_identifier(value: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 80 or any(not (ch.isalnum() or ch in "-_") for ch in text):
        raise ValueError("invalid_experiment_id")
    return text


def _safe_label(value: Any) -> str:
    text = " ".join(str(value or "").split())
    lowered = text.casefold()
    if (
        "\\" in text
        or re.match(r"^[a-z]:", lowered)
        or lowered.startswith(("/", "./", "../", "~/"))
        or "/../" in lowered
        or re.search(r"\.(?:pdf|png|jpe?g|tiff?|csv|xlsx?)$", lowered)
    ):
        raise ValueError("private_path_or_filename_not_allowed_in_spend_label")
    return text[:160]


def _safe_usage(value: dict[str, int | float | str]) -> dict[str, int | float | str]:
    allowed = {
        "input_tokens", "output_tokens", "total_tokens", "cached_input_tokens",
        "images", "pages", "bytes", "pixels", "provider_request_count",
    }
    return {key: item for key, item in value.items() if key in allowed and isinstance(item, (int, float, str))}


def _safe_sha256(value: Any) -> str:
    text = str(value or "").strip().casefold()
    if text and not re.fullmatch(r"[0-9a-f]{64}", text):
        raise ValueError("invalid_document_sha256")
    return text


def _now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "ALERT_THRESHOLDS", "ExperimentPhase", "ExperimentSpendController",
    "ExperimentSpendGate", "activate_experiment_spend_gate",
    "current_experiment_spend_gate",
    "PHASE_CAPS_USD", "SPEND_CONTRACT_VERSION", "SpendAuthorizationError",
    "SpendCostAccountingView", "SpendReservation", "SpendSnapshot",
    "spend_cost_accounting_view",
]
