"""Fail-closed network and privacy boundary for the Phase A experiment.

This module is deliberately orthogonal to normal application routing.  It is
active only when ``INNER_VIEW_EXPERIMENT_EXECUTION_MODE=CONTROLLED_EXTERNAL``
and an explicit controller is bound to the current execution context.

Private source bytes may be sent only to the approved Gemini visual endpoint.
All semantic normalization and candidate generation remain local. Gemini is
never an accounting or export authority.
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from .gemini_facts_transport import gemini_response_format
from .gemini_supplementary_verification import (
    SupplementaryTarget,
    SupplementaryTargetType,
    supplementary_response_format,
)


CONTROLLED_EXTERNAL_CONTRACT_VERSION = "controlled-external-phase-a/1.1"
CONTROLLED_EXTERNAL_MODE_ENV = "INNER_VIEW_EXPERIMENT_EXECUTION_MODE"
ALLOWED_PROVIDER_HOSTS: Mapping[str, frozenset[str]] = {
    "gemini": frozenset({"generativelanguage.googleapis.com"}),
}
ALLOWED_GEMINI_PATHS = frozenset({"/v1beta/openai/chat/completions"})
MAXIMUM_PHASE_A_DOCUMENTS = 100

PRIVATE_AUTHORIZATION_TEXT = """I authorize the Innerview Phase A document-learning experiment to transmit only the documents in the frozen Phase A manifest, with a maximum of 100 documents, to the paid Gemini API under the operator-confirmed Vision project (Paid, Tier 1 Prepay), exclusively for facts-only visual extraction. I understand that source documents may contain PII, addresses, account numbers, financial amounts, property information, and confidential business data. I do not authorize DeepSeek, OpenAI, Claude, any other provider, arbitrary fallback, source documents or derived private facts to another provider, Gemini Files API storage, grounding, explicit caching, stored Interactions, ground truth, holdout labels, human corrections, Phase B, Phase C, the full corpus, or private artifacts entering Git. The cumulative Phase A provider budget, including synthetic preflight usage, is USD 10. Semantic normalization and candidate generation remain local; AccountingDecisionEngine remains the only final GL authority and AccountingReadiness remains the only export authority."""
PRIVATE_AUTHORIZATION_SHA256 = hashlib.sha256(
    PRIVATE_AUTHORIZATION_TEXT.encode("utf-8")
).hexdigest()

_FORBIDDEN_KEYS = {
    "absolute_path", "account_number", "answer", "answers", "authorization",
    "benchmark_label", "correction", "corrections", "credential", "credentials",
    "expected_gl", "filename", "file_name", "full_path", "ground_truth",
    "holdout", "holdout_label", "label", "labels", "local_path", "path",
    "person_name", "reviewer_answer", "source_path",
}
_GEMINI_FORBIDDEN_KEYS = (_FORBIDDEN_KEYS - {"account_number"}) | {
    "cache", "cached_content", "explicit_cache", "file", "file_data",
    "file_id", "file_uri", "files", "grounding", "grounding_config",
    "interaction", "interactions", "store", "stored_interaction", "tool",
    "tools",
    "accounting_policy", "candidate_gl_codes", "gl_catalog", "gl_reference",
    "readiness", "tenant_learning", "vendor_reference",
}
_BINARY_PREFIXES = ("data:image/", "data:application/pdf", "JVBER", "iVBOR")


class ExperimentExecutionMode(str, Enum):
    NORMAL = "NORMAL"
    LOCAL_ONLY = "LOCAL_ONLY"
    CONTROLLED_EXTERNAL = "CONTROLLED_EXTERNAL"


class ControlledExternalBlocked(RuntimeError):
    """Raised before transport when a controlled-external invariant fails."""

    def __init__(self, failure_code: str) -> None:
        super().__init__(failure_code)
        self.failure_code = failure_code


class ControlledExternalGateTerminated(ControlledExternalBlocked):
    """Fatal controlled-gate condition that application fallbacks must not catch."""


class ControlledCallPurpose(str, Enum):
    INITIAL_EXTRACTION = "initial_extraction"
    SUPPLEMENTARY_VERIFICATION = "supplementary_verification"
    OTHER_VISUAL = "other_visual"


_CONTROLLED_TELEMETRY_EVENTS = {
    "controlled_provider_route_blocked", "dispatch_authorized",
}
_CONTROLLED_TELEMETRY_RESULTS = {"authorized", "blocked", "unknown"}
_CONTROLLED_TELEMETRY_FAILURES = {
    "controlled_external_context_required", "controlled_external_cost_estimate_missing",
    "controlled_external_document_scope_missing", "controlled_external_pricing_indeterminate",
    "controlled_external_provider_not_allowed", "controlled_provider_route_blocked",
    "supplementary_request_limit_reached", "unknown_controlled_failure",
}


def _controlled_purpose_from_stage(stage: object) -> str:
    """Reduce any caller stage to a finite provider-purpose category."""

    normalized = str(stage or "").strip().casefold()
    if "supplementary" in normalized:
        return ControlledCallPurpose.SUPPLEMENTARY_VERIFICATION.value
    if normalized:
        return ControlledCallPurpose.INITIAL_EXTRACTION.value
    return "unknown"


def _controlled_safe_identifier(value: object) -> str:
    candidate = str(value or "").strip()
    if (
        re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,159}", candidate)
        and not re.search(r"(?:authorization|bearer|api[_-]?key|secret)", candidate, re.I)
        and not re.search(r"\d{7,}", candidate)
    ):
        return candidate
    return ""


def _safe_cost(value: object) -> float | None:
    try:
        parsed = float(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed is None or parsed < 0 or parsed != parsed or parsed == float("inf"):
        return None
    return round(parsed, 8)


class ControlledCallPermitState(str, Enum):
    RESERVED = "reserved"
    RELEASED_FOR_CACHE_HIT = "released_for_cache_hit"
    CONSUMED_FOR_DISPATCH = "consumed_for_dispatch"
    CANCELED_BEFORE_DISPATCH = "canceled_before_dispatch"


@dataclass(frozen=True)
class ControlledCallPermit:
    purpose: ControlledCallPurpose
    ordinal: int
    budget_id: str = field(repr=False)
    token: str = field(repr=False)


class ControlledDocumentCallBudget:
    """One atomic call counter shared by every helper for one source document."""

    def __init__(self, *, maximum_initial: int = 1, maximum_supplementary: int = 2) -> None:
        self.maximum_initial = max(0, int(maximum_initial))
        self.maximum_supplementary = max(0, int(maximum_supplementary))
        self._initial = 0
        self._supplementary = 0
        self._lock = threading.Lock()
        self._budget_id = secrets.token_hex(16)
        self._purposes: dict[str, ControlledCallPurpose] = {}
        self._states: dict[str, ControlledCallPermitState] = {}

    def reserve(self, purpose: ControlledCallPurpose) -> ControlledCallPermit:
        with self._lock:
            if purpose is ControlledCallPurpose.INITIAL_EXTRACTION:
                if self._initial >= self.maximum_initial:
                    raise ControlledExternalGateTerminated(
                        "controlled_provider_route_blocked"
                    )
                self._initial += 1
                return self._new_permit(purpose, self._initial)
            if purpose is ControlledCallPurpose.SUPPLEMENTARY_VERIFICATION:
                if self._supplementary >= self.maximum_supplementary:
                    raise ControlledExternalGateTerminated(
                        "supplementary_request_limit_reached"
                    )
                self._supplementary += 1
                return self._new_permit(purpose, self._supplementary)
            failure = (
                "supplementary_request_limit_reached"
                if self._supplementary >= self.maximum_supplementary
                else "controlled_provider_route_blocked"
            )
            raise ControlledExternalGateTerminated(failure)

    def _new_permit(
        self, purpose: ControlledCallPurpose, ordinal: int,
    ) -> ControlledCallPermit:
        token = secrets.token_hex(16)
        self._purposes[token] = purpose
        self._states[token] = ControlledCallPermitState.RESERVED
        return ControlledCallPermit(
            purpose=purpose, ordinal=ordinal,
            budget_id=self._budget_id, token=token,
        )

    def consume(
        self, permit: ControlledCallPermit, purpose: ControlledCallPurpose,
    ) -> None:
        """Consume exactly once a permit emitted by this document budget."""

        with self._lock:
            if (
                permit.budget_id != self._budget_id
                or permit.purpose is not purpose
                or self._purposes.get(permit.token) is not purpose
                or self._states.get(permit.token)
                is not ControlledCallPermitState.RESERVED
            ):
                raise ControlledExternalGateTerminated(
                    "controlled_provider_route_blocked"
                )
            self._states[permit.token] = (
                ControlledCallPermitState.CONSUMED_FOR_DISPATCH
            )

    def release(self, permit: ControlledCallPermit) -> None:
        """Backward-compatible strict cache-hit release."""

        self.release_for_cache_hit(permit)

    def release_for_cache_hit(self, permit: ControlledCallPermit) -> None:
        """Return one reserved permit after an exact cache hit."""

        self._transition_unused(
            permit, ControlledCallPermitState.RELEASED_FOR_CACHE_HIT
        )

    def cancel_before_dispatch(self, permit: ControlledCallPermit) -> None:
        """Return a slot after a local failure that preceded dispatch."""

        self._transition_unused(
            permit, ControlledCallPermitState.CANCELED_BEFORE_DISPATCH
        )

    def _transition_unused(
        self, permit: ControlledCallPermit, target: ControlledCallPermitState,
    ) -> None:
        if target not in {
            ControlledCallPermitState.RELEASED_FOR_CACHE_HIT,
            ControlledCallPermitState.CANCELED_BEFORE_DISPATCH,
        }:
            raise ControlledExternalGateTerminated(
                "controlled_provider_route_blocked"
            )

        with self._lock:
            purpose = self._purposes.get(permit.token)
            if (
                permit.budget_id != self._budget_id
                or purpose is None
                or purpose is not permit.purpose
                or self._states.get(permit.token)
                is not ControlledCallPermitState.RESERVED
            ):
                raise ControlledExternalGateTerminated(
                    "controlled_provider_route_blocked"
                )
            self._states[permit.token] = target
            if purpose is ControlledCallPurpose.INITIAL_EXTRACTION:
                self._initial = max(0, self._initial - 1)
            elif purpose is ControlledCallPurpose.SUPPLEMENTARY_VERIFICATION:
                self._supplementary = max(0, self._supplementary - 1)

    def permit_state(
        self, permit: ControlledCallPermit,
    ) -> ControlledCallPermitState:
        with self._lock:
            if (
                permit.budget_id != self._budget_id
                or self._purposes.get(permit.token) is not permit.purpose
                or permit.token not in self._states
            ):
                raise ControlledExternalGateTerminated(
                    "controlled_provider_route_blocked"
                )
            return self._states[permit.token]

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "initial_used": self._initial,
                "initial_remaining": max(0, self.maximum_initial - self._initial),
                "supplementary_used": self._supplementary,
                "supplementary_remaining": max(
                    0, self.maximum_supplementary - self._supplementary
                ),
            }


class ControlledCallPermitLifecycle:
    """Own one reserved permit until cache release or central dispatch.

    The lifecycle never creates a permit. Construction succeeds only for a
    permit already reserved by the topology preflight, which makes
    use-before-reserve and nested counter creation deterministic failures.
    """

    def __init__(
        self, budget: ControlledDocumentCallBudget,
        permit: ControlledCallPermit,
    ) -> None:
        if budget.permit_state(permit) is not ControlledCallPermitState.RESERVED:
            raise ControlledExternalGateTerminated(
                "controlled_provider_route_blocked"
            )
        self._budget = budget
        self._permit = permit

    @property
    def state(self) -> ControlledCallPermitState:
        return self._budget.permit_state(self._permit)

    def permit_for_dispatch(self) -> ControlledCallPermit:
        if self.state is not ControlledCallPermitState.RESERVED:
            raise ControlledExternalGateTerminated(
                "controlled_provider_route_blocked"
            )
        return self._permit

    def release_for_cache_hit(self) -> None:
        self._budget.release_for_cache_hit(self._permit)

    def __enter__(self) -> "ControlledCallPermitLifecycle":
        if self.state is not ControlledCallPermitState.RESERVED:
            raise ControlledExternalGateTerminated(
                "controlled_provider_route_blocked"
            )
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        state = self.state
        if state is ControlledCallPermitState.RESERVED:
            self._budget.cancel_before_dispatch(self._permit)
            if exc_type is None:
                raise ControlledExternalGateTerminated(
                    "controlled_local_execution_error"
                )
        return False


@dataclass(frozen=True)
class ExperimentProviderContext:
    """Explicit, credential-free provider topology for one controlled document."""

    execution_mode: ExperimentExecutionMode
    authorized_provider: str
    authorized_model: str
    authorized_profile_id: str
    allowed_endpoint: str
    manifest_sha256: str
    document_sha256: str
    fallback_allowed: bool = False
    maximum_initial_calls: int = 1
    maximum_supplementary_calls: int = 2
    call_budget: ControlledDocumentCallBudget = field(
        default_factory=ControlledDocumentCallBudget, repr=False, compare=False
    )

    @property
    def remaining_call_budget(self) -> dict[str, int]:
        return self.call_budget.snapshot()


class ControlledGateExecutionState:
    """Deterministic cancellation state for sequential or bounded-worker schedulers."""

    def __init__(self, assignment_count: int) -> None:
        self.assignment_count = max(0, int(assignment_count))
        self._lock = threading.Lock()
        self._started: set[int] = set()
        self._completed: set[int] = set()
        self._failure_code = ""

    def claim(self, assignment_index: int) -> bool:
        with self._lock:
            if (
                self._failure_code
                or assignment_index < 0
                or assignment_index >= self.assignment_count
                or assignment_index in self._started
            ):
                return False
            self._started.add(assignment_index)
            return True

    def complete(self, assignment_index: int) -> None:
        with self._lock:
            if assignment_index in self._started:
                self._completed.add(assignment_index)

    def terminate(self, failure_code: str) -> None:
        with self._lock:
            if not self._failure_code:
                self._failure_code = str(failure_code or "controlled_provider_route_blocked")

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return bool(self._failure_code)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "assignment_count": self.assignment_count,
                "started_indices": sorted(self._started),
                "completed_indices": sorted(self._completed),
                "not_started_indices": [
                    index for index in range(self.assignment_count)
                    if index not in self._started
                ],
                "failure_code": self._failure_code or None,
                "cancelled": bool(self._failure_code),
            }


class PrivateTransferAuthorization(BaseModel):
    """Operator-owned authorization record stored only in the private root."""

    model_config = ConfigDict(extra="forbid")

    contract_version: str = CONTROLLED_EXTERNAL_CONTRACT_VERSION
    experiment_id: str
    manifest_sha256: str
    authorization_sha256: str
    authorization_text_accepted: bool
    gemini_account_settings_reviewed: bool
    gemini_paid_project_operator_confirmed: bool
    operator_confirmed_project_name: str
    operator_confirmed_plan: str
    sensitive_private_transfer_risk_accepted: bool
    provider_retention_risk_accepted: bool
    operator_id: str
    accepted_at: datetime


class ControlledDocumentScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    experiment_id: str
    document_sha256: str
    opaque_document_id: str
    synthetic: bool = False


class ControlledExternalController:
    """Frozen-manifest, authorization and private-telemetry authority."""

    def __init__(
        self,
        *,
        private_root: Path,
        experiment_id: str,
        manifest_path: Path,
        inventory_path: Path | None = None,
        expected_manifest_sha256: str | None = None,
        authorization_path: Path | None = None,
    ) -> None:
        self.private_root = Path(private_root).resolve(strict=True)
        self.experiment_id = _safe_experiment_id(experiment_id)
        self.manifest_path = Path(manifest_path).resolve(strict=True)
        self.inventory_path = (
            Path(inventory_path).resolve(strict=True) if inventory_path else None
        )
        for value in (self.manifest_path, self.inventory_path):
            if value is not None and not _is_within(value, self.private_root):
                raise ControlledExternalBlocked("controlled_input_outside_private_root")
        self.manifest_sha256 = _sha256_file(self.manifest_path)
        if expected_manifest_sha256 and not hmac.compare_digest(
            self.manifest_sha256, _require_sha256(expected_manifest_sha256)
        ):
            raise ControlledExternalBlocked("frozen_manifest_hash_mismatch")
        self.allowed_document_hashes = self._load_allowed_document_hashes()
        self.authorization_path = (
            Path(authorization_path).resolve() if authorization_path else None
        )
        if self.authorization_path and not _is_within(
            self.authorization_path, self.private_root
        ):
            raise ControlledExternalBlocked("authorization_outside_private_root")
        self.telemetry_path = (
            self.private_root / "telemetry" / "controlled_external_events.jsonl"
        )
        self.secret_path = self.private_root / "private" / "opaque_id_secret.bin"

    def _load_allowed_document_hashes(self) -> frozenset[str]:
        payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        assignments = list(payload.get("assignments") or [])
        if not assignments or len(assignments) > MAXIMUM_PHASE_A_DOCUMENTS:
            raise ControlledExternalBlocked("invalid_frozen_phase_a_manifest_size")
        if bool(payload.get("answers_embedded")):
            raise ControlledExternalBlocked("frozen_manifest_contains_answers")
        inventory: dict[str, str] = {}
        if self.inventory_path is not None:
            with self.inventory_path.open("r", encoding="utf-8") as handle:
                for raw in handle:
                    if not raw.strip():
                        continue
                    row = json.loads(raw)
                    document_id = str(row.get("document_id") or "")
                    content_hash = str(row.get("content_sha256") or "")
                    if document_id and content_hash:
                        inventory[document_id] = _require_sha256(content_hash)
        selected: set[str] = set()
        for item in assignments:
            document_id = str(item.get("document_id") or "")
            content_hash = str(item.get("content_sha256") or "")
            if not content_hash:
                content_hash = inventory.get(document_id, "")
            if not content_hash:
                raise ControlledExternalBlocked("manifest_document_hash_unresolved")
            selected.add(_require_sha256(content_hash))
        if len(selected) != len(assignments):
            raise ControlledExternalBlocked("frozen_manifest_contains_duplicate_documents")
        return frozenset(selected)

    def require_private_authorization(self) -> PrivateTransferAuthorization:
        if self.authorization_path is None or not self.authorization_path.is_file():
            raise ControlledExternalBlocked("private_transfer_authorization_missing")
        authorization = PrivateTransferAuthorization.model_validate_json(
            self.authorization_path.read_text(encoding="utf-8")
        )
        if authorization.experiment_id != self.experiment_id:
            raise ControlledExternalBlocked("authorization_experiment_mismatch")
        if not hmac.compare_digest(
            authorization.manifest_sha256, self.manifest_sha256
        ):
            raise ControlledExternalBlocked("authorization_manifest_mismatch")
        if not hmac.compare_digest(
            authorization.authorization_sha256, PRIVATE_AUTHORIZATION_SHA256
        ) or not authorization.authorization_text_accepted:
            raise ControlledExternalBlocked("informed_authorization_not_accepted")
        if not all((
            authorization.gemini_account_settings_reviewed,
            authorization.gemini_paid_project_operator_confirmed,
            authorization.sensitive_private_transfer_risk_accepted,
            authorization.provider_retention_risk_accepted,
        )):
            raise ControlledExternalBlocked("provider_account_policy_confirmation_missing")
        if (
            authorization.operator_confirmed_project_name != "Vision"
            or authorization.operator_confirmed_plan != "Paid, Tier 1 Prepay"
        ):
            raise ControlledExternalBlocked("operator_confirmed_project_mismatch")
        return authorization

    def assert_document_allowed(self, document_sha256: str) -> str:
        digest = _require_sha256(document_sha256)
        if digest not in self.allowed_document_hashes:
            raise ControlledExternalBlocked("document_outside_frozen_manifest")
        return digest

    def opaque_document_id(self, document_sha256: str) -> str:
        digest = _require_sha256(document_sha256)
        secret = self._private_secret()
        return "doc_" + hmac.new(secret, digest.encode("ascii"), hashlib.sha256).hexdigest()[:24]

    def _private_secret(self) -> bytes:
        if not self.secret_path.exists():
            self.secret_path.parent.mkdir(parents=True, exist_ok=True)
            self.secret_path.write_bytes(secrets.token_bytes(32))
        value = self.secret_path.read_bytes()
        if len(value) < 32:
            raise ControlledExternalBlocked("opaque_id_secret_invalid")
        return value

    def record_event(self, **payload: Any) -> None:
        event = str(payload.get("event") or "")
        result = str(payload.get("result") or "").casefold()
        failure_code = str(payload.get("failure_code") or "")
        document_sha256 = str(payload.get("document_sha256") or "").casefold()
        opaque_document_id = str(payload.get("opaque_document_id") or "").casefold()
        reservation_id = str(payload.get("reservation_id") or "")
        purpose = str(payload.get("purpose") or "").casefold()
        safe: dict[str, Any] = {
            "event": event if event in _CONTROLLED_TELEMETRY_EVENTS else "unknown",
            "provider": "gemini" if _provider_name(payload.get("provider")) == "gemini" else "unknown",
            "model": _controlled_safe_identifier(payload.get("model")),
            "profile_id": _controlled_safe_identifier(payload.get("profile_id")),
            "document_sha256": (
                document_sha256 if re.fullmatch(r"[a-f0-9]{64}", document_sha256) else ""
            ),
            "opaque_document_id": (
                opaque_document_id
                if re.fullmatch(r"doc_[a-f0-9]{24}", opaque_document_id) else ""
            ),
            "purpose": (
                purpose
                if purpose in {item.value for item in ControlledCallPurpose} | {"unknown"}
                else "unknown"
            ),
            "result": result if result in _CONTROLLED_TELEMETRY_RESULTS else "unknown",
            "failure_code": (
                failure_code
                if failure_code in _CONTROLLED_TELEMETRY_FAILURES else "unknown_controlled_failure"
            ) if failure_code else "",
            "reservation_id": (
                reservation_id
                if re.fullmatch(r"[A-Fa-f0-9-]{8,80}", reservation_id) else ""
            ),
            "estimated_cost_usd": _safe_cost(payload.get("estimated_cost_usd")),
            "actual_cost_usd": _safe_cost(payload.get("actual_cost_usd")),
            "host": (
                "generativelanguage.googleapis.com"
                if str(payload.get("host") or "").casefold()
                == "generativelanguage.googleapis.com" else ""
            ),
        }
        safe.update({
            "contract_version": CONTROLLED_EXTERNAL_CONTRACT_VERSION,
            "experiment_id": self.experiment_id,
            "at": datetime.now(timezone.utc).isoformat(),
        })
        self.telemetry_path.parent.mkdir(parents=True, exist_ok=True)
        with self.telemetry_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(safe, sort_keys=True, separators=(",", ":")) + "\n")


_ACTIVE_CONTROLLER: contextvars.ContextVar[ControlledExternalController | None] = (
    contextvars.ContextVar("innerview_controlled_external_controller", default=None)
)
_ACTIVE_DOCUMENT: contextvars.ContextVar[ControlledDocumentScope | None] = (
    contextvars.ContextVar("innerview_controlled_external_document", default=None)
)
_ACTIVE_PROVIDER_CONTEXT: contextvars.ContextVar[ExperimentProviderContext | None] = (
    contextvars.ContextVar("innerview_controlled_external_provider_context", default=None)
)


def execution_mode() -> ExperimentExecutionMode:
    value = str(os.environ.get(CONTROLLED_EXTERNAL_MODE_ENV) or "").strip().upper()
    if value:
        try:
            return ExperimentExecutionMode(value)
        except ValueError as exc:
            raise ControlledExternalBlocked("unknown_experiment_execution_mode") from exc
    if _truthy(os.environ.get("INNER_VIEW_LOCAL_INFERENCE_ONLY")):
        return ExperimentExecutionMode.LOCAL_ONLY
    return ExperimentExecutionMode.NORMAL


def controlled_external_active() -> bool:
    return execution_mode() is ExperimentExecutionMode.CONTROLLED_EXTERNAL


def current_controller() -> ControlledExternalController | None:
    return _ACTIVE_CONTROLLER.get()


def current_document_scope() -> ControlledDocumentScope | None:
    return _ACTIVE_DOCUMENT.get()


def current_experiment_provider_context() -> ExperimentProviderContext | None:
    return _ACTIVE_PROVIDER_CONTEXT.get()


def build_experiment_provider_context(
    *, controller: ControlledExternalController, document_sha256: str,
    authorized_provider: str, authorized_model: str, authorized_profile_id: str,
    allowed_endpoint: str,
) -> ExperimentProviderContext:
    """Build the one authorized, credential-free topology for a document."""

    digest = controller.assert_document_allowed(document_sha256)
    provider = _provider_name(authorized_provider)
    parsed = urlparse(str(allowed_endpoint or ""))
    if (
        provider != "gemini"
        or parsed.scheme.lower() != "https"
        or parsed.hostname not in ALLOWED_PROVIDER_HOSTS["gemini"]
        or parsed.path not in ALLOWED_GEMINI_PATHS
        or bool(parsed.params or parsed.query or parsed.fragment)
    ):
        raise ControlledExternalGateTerminated("controlled_provider_route_blocked")
    if not str(authorized_model or "").strip() or not str(
        authorized_profile_id or ""
    ).strip():
        raise ControlledExternalGateTerminated("controlled_provider_route_blocked")
    return ExperimentProviderContext(
        execution_mode=ExperimentExecutionMode.CONTROLLED_EXTERNAL,
        authorized_provider="gemini",
        authorized_model=str(authorized_model).strip(),
        authorized_profile_id=str(authorized_profile_id).strip(),
        allowed_endpoint=str(allowed_endpoint).strip(),
        manifest_sha256=controller.manifest_sha256,
        document_sha256=digest,
        fallback_allowed=False,
        maximum_initial_calls=1,
        maximum_supplementary_calls=2,
        call_budget=ControlledDocumentCallBudget(
            maximum_initial=1, maximum_supplementary=2,
        ),
    )


@contextlib.contextmanager
def activate_controlled_external(
    controller: ControlledExternalController,
) -> Iterator[ControlledExternalController]:
    if execution_mode() is not ExperimentExecutionMode.CONTROLLED_EXTERNAL:
        raise ControlledExternalBlocked("controlled_external_mode_not_enabled")
    token = _ACTIVE_CONTROLLER.set(controller)
    try:
        yield controller
    finally:
        _ACTIVE_CONTROLLER.reset(token)


@contextlib.contextmanager
def activate_experiment_provider_context(
    provider_context: ExperimentProviderContext,
) -> Iterator[ExperimentProviderContext]:
    """Bind an explicitly constructed topology; never derive one from defaults."""

    controller = current_controller()
    scope = current_document_scope()
    if controller is None or scope is None:
        raise ControlledExternalGateTerminated(
            "controlled_external_context_required"
        )
    if (
        provider_context.execution_mode is not ExperimentExecutionMode.CONTROLLED_EXTERNAL
        or provider_context.manifest_sha256 != controller.manifest_sha256
        or provider_context.document_sha256 != scope.document_sha256
        or provider_context.fallback_allowed
        or provider_context.maximum_initial_calls != 1
        or provider_context.maximum_supplementary_calls != 2
        or provider_context.call_budget.maximum_initial != 1
        or provider_context.call_budget.maximum_supplementary != 2
    ):
        raise ControlledExternalGateTerminated(
            "controlled_provider_route_blocked"
        )
    token = _ACTIVE_PROVIDER_CONTEXT.set(provider_context)
    try:
        yield provider_context
    finally:
        _ACTIVE_PROVIDER_CONTEXT.reset(token)


def require_experiment_provider_context(
    provider_context: ExperimentProviderContext | None,
) -> ExperimentProviderContext:
    """Require the exact context object passed by the controlled runner."""

    if not controlled_external_active():
        if provider_context is None:
            raise ControlledExternalBlocked("controlled_external_mode_not_enabled")
        return provider_context
    active = current_experiment_provider_context()
    if provider_context is None or active is None or provider_context is not active:
        _terminate_controlled_route(
            failure_code="controlled_external_context_required",
            provider="unknown", stage="context_validation",
        )
    return provider_context


@contextlib.contextmanager
def controlled_document_scope(
    *, document_sha256: str, synthetic: bool = False,
) -> Iterator[ControlledDocumentScope]:
    controller = current_controller()
    if controller is None:
        raise ControlledExternalBlocked("controlled_external_controller_missing")
    digest = _require_sha256(document_sha256)
    if not synthetic:
        controller.assert_document_allowed(digest)
        controller.require_private_authorization()
    scope = ControlledDocumentScope(
        experiment_id=controller.experiment_id,
        document_sha256=digest,
        opaque_document_id=controller.opaque_document_id(digest),
        synthetic=synthetic,
    )
    token = _ACTIVE_DOCUMENT.set(scope)
    try:
        yield scope
    finally:
        _ACTIVE_DOCUMENT.reset(token)


def assert_controlled_external_dispatch_allowed(
    *, provider: str, url: str, stage: str, payload: Mapping[str, Any],
    provider_context: ExperimentProviderContext | None = None,
    call_purpose: ControlledCallPurpose = ControlledCallPurpose.OTHER_VISUAL,
    profile_id: str = "",
    call_permit: ControlledCallPermit | None = None,
) -> ControlledCallPermit | None:
    """Validate a request immediately before spend reservation and transport."""
    if not controlled_external_active():
        return None
    controller = current_controller()
    scope = current_document_scope()
    if controller is None:
        raise ControlledExternalGateTerminated("controlled_external_controller_missing")
    if scope is None:
        raise ControlledExternalGateTerminated("controlled_external_document_scope_missing")
    context = require_experiment_provider_context(provider_context)
    normalized_provider = _provider_name(provider)
    model = str(payload.get("model") or "").strip()
    if (
        normalized_provider != context.authorized_provider
        or model != context.authorized_model
        or not _controlled_profile_id_allowed(profile_id, context)
        or str(url or "").strip() != context.allowed_endpoint
        or context.document_sha256 != scope.document_sha256
        or context.manifest_sha256 != controller.manifest_sha256
        or context.fallback_allowed
    ):
        _terminate_controlled_route(
            failure_code="controlled_provider_route_blocked",
            provider=normalized_provider, stage=stage,
        )
    from . import ai_runtime_trace
    cost_context = ai_runtime_trace.current_context()
    if float(cost_context.estimated_cost_usd or 0.0) <= 0:
        _terminate_controlled_route(
            failure_code="controlled_external_cost_estimate_missing",
            provider=normalized_provider, stage=stage,
        )
    if (
        float(cost_context.input_cost_usd_per_million or 0.0) <= 0
        or float(cost_context.output_cost_usd_per_million or 0.0) <= 0
    ):
        _terminate_controlled_route(
            failure_code="controlled_external_pricing_indeterminate",
            provider=normalized_provider, stage=stage,
        )
    allowed_hosts = ALLOWED_PROVIDER_HOSTS.get(normalized_provider)
    parsed = urlparse(str(url or ""))
    if (
        not allowed_hosts
        or parsed.scheme.lower() != "https"
        or parsed.hostname not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or parsed.path not in ALLOWED_GEMINI_PATHS
        or bool(parsed.params or parsed.query or parsed.fragment)
    ):
        _terminate_controlled_route(
            failure_code="controlled_provider_route_blocked",
            provider=normalized_provider, stage=stage,
        )
    if not scope.synthetic:
        controller.assert_document_allowed(scope.document_sha256)
        controller.require_private_authorization()
    if normalized_provider == "gemini":
        try:
            _validate_gemini_payload(
                payload, private=not scope.synthetic, stage=str(stage or "unknown"),
            )
        except ControlledExternalBlocked:
            _terminate_controlled_route(
                failure_code="controlled_provider_route_blocked",
                provider=normalized_provider, stage=stage,
            )
    else:  # defensive even though host validation already rejects it
        _terminate_controlled_route(
            failure_code="controlled_provider_route_blocked",
            provider=normalized_provider, stage=stage,
        )
    try:
        permit = call_permit or context.call_budget.reserve(call_purpose)
        context.call_budget.consume(permit, call_purpose)
    except ControlledExternalGateTerminated as exc:
        _terminate_controlled_route(
            failure_code=exc.failure_code,
            provider=normalized_provider, stage=stage,
        )
    controller.record_event(
        event="dispatch_authorized", provider=normalized_provider,
        model=str(payload.get("model") or "")[:160],
        document_sha256=scope.document_sha256,
        opaque_document_id=scope.opaque_document_id,
        purpose=call_purpose.value, result="authorized",
        host=str(parsed.hostname or "")[:160],
    )
    return permit


def preflight_controlled_provider_route(
    *, provider_context: ExperimentProviderContext | None, provider: str,
    model: str, profile_id: str, endpoint: str,
    call_purpose: ControlledCallPurpose, stage: str,
) -> ControlledCallPermit | None:
    """Reject a wrong topology before prompts, images, or request JSON are built."""

    if not controlled_external_active():
        return None
    context = require_experiment_provider_context(provider_context)
    if (
        _provider_name(provider) != context.authorized_provider
        or str(model or "").strip() != context.authorized_model
        or not _controlled_profile_id_allowed(profile_id, context)
        or str(endpoint or "").strip() != context.allowed_endpoint
        or context.fallback_allowed
        or call_purpose is ControlledCallPurpose.OTHER_VISUAL
    ):
        _terminate_controlled_route(
            failure_code=(
                "supplementary_request_limit_reached"
                if call_purpose is ControlledCallPurpose.OTHER_VISUAL
                and context.remaining_call_budget["supplementary_remaining"] == 0
                else "controlled_provider_route_blocked"
            ),
            provider=provider, stage=stage,
        )
    try:
        return context.call_budget.reserve(call_purpose)
    except ControlledExternalGateTerminated as exc:
        _terminate_controlled_route(
            failure_code=exc.failure_code,
            provider=provider, stage=stage,
        )


def _terminate_controlled_route(
    *, failure_code: str, provider: str, stage: str,
) -> None:
    """Persist one safe fatal signal and abort before spend or transport."""

    code = str(failure_code or "controlled_provider_route_blocked")
    controller = current_controller()
    scope = current_document_scope()
    normalized_provider = _provider_name(provider) or "unknown"
    if controller is not None:
        controller.record_event(
            event="controlled_provider_route_blocked",
            provider=normalized_provider,
            document_sha256=(scope.document_sha256 if scope else ""),
            opaque_document_id=(scope.opaque_document_id if scope else ""),
            purpose=_controlled_purpose_from_stage(stage), result="blocked",
            failure_code=code,
        )
    try:
        from . import ai_runtime_trace
        ai_runtime_trace.record_controlled_provider_route_blocked(
            provider=normalized_provider, stage=stage, failure_code=code,
        )
    except Exception:
        pass
    raise ControlledExternalGateTerminated(code)


def _controlled_profile_id_allowed(
    profile_id: str, context: ExperimentProviderContext,
) -> bool:
    value = str(profile_id or "").strip()
    base = context.authorized_profile_id
    return value == base or any(
        value.startswith(f"{base}:{suffix}:")
        for suffix in ("facts-only", "supplementary")
    )


def controlled_urlopen(request: urllib.request.Request, *, timeout: int):
    """Open an authorized request and prohibit all redirects in controlled mode."""
    if not controlled_external_active():
        return urllib.request.urlopen(request, timeout=timeout)

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, _req, _fp, _code, _msg, _headers, _newurl):
            _terminate_controlled_route(
                failure_code="controlled_provider_route_blocked",
                provider="gemini", stage="redirect_validation",
            )

    return urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout)


def build_deepseek_minimized_facts(
    *, lines: Sequence[Mapping[str, Any]], document_family: str = "",
) -> dict[str, Any]:
    """Retained import adapter; DeepSeek is not authorized by this contract."""
    if controlled_external_active():
        raise ControlledExternalBlocked("controlled_external_provider_not_allowed")
    scope = current_document_scope()
    if scope is None:
        raise ControlledExternalBlocked("controlled_external_document_scope_missing")
    minimized_lines: list[dict[str, Any]] = []
    for index, line in enumerate(lines, start=1):
        normalized = str(
            line.get("normalized_description")
            or line.get("normalized_activity")
            or line.get("source_text")
            or ""
        )
        minimized_lines.append({
            "line_id": f"line_{index:04d}",
            "normalized_text": _redact_sensitive_text(normalized),
            "quantity": _safe_number(line.get("quantity")),
            "unit_price": _safe_number(line.get("unit_price")),
            "amount": _safe_number(line.get("amount")),
            "current_line_family": _safe_taxonomy_value(
                (line.get("current_semantics") or {}).get("line_family")
            ),
            "current_trade_family": _safe_taxonomy_value(
                (line.get("current_semantics") or {}).get("trade_family")
            ),
            "current_work_mode": _safe_taxonomy_value(
                (line.get("current_semantics") or {}).get("work_mode")
            ),
        })
    return {
        "schema_version": "controlled-deepseek-derived-facts/1.0",
        "experiment_document_id": scope.opaque_document_id,
        "document_family": _safe_taxonomy_value(document_family),
        "lines": minimized_lines,
    }


def spend_document_context() -> tuple[str, str]:
    """Return private-ledger-safe document hash and purpose for reservations."""
    if not controlled_external_active():
        return "", ""
    scope = current_document_scope()
    if scope is None:
        raise ControlledExternalBlocked("controlled_external_document_scope_missing")
    try:
        from . import ai_runtime_trace
        purpose = _controlled_purpose_from_stage(
            ai_runtime_trace.current_context().stage,
        )
    except Exception:
        purpose = "unknown"
    return scope.document_sha256, purpose


def _validate_gemini_payload(
    payload: Mapping[str, Any], *, private: bool, stage: str,
) -> None:
    # The response schema is compared byte-for-structure below.  Do not scan
    # schema property names as if they were outbound source/label values (for
    # example, a visible amount component legitimately has a ``label`` key).
    _assert_no_forbidden_keys(
        {key: value for key, value in payload.items() if key != "response_format"},
        _GEMINI_FORBIDDEN_KEYS,
    )
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    lowered = serialized.casefold()
    if any(token in lowered for token in (
        "expected gl", "selected gl", "readiness decision", "holdout label",
    )):
        raise ControlledExternalBlocked("gemini_payload_contains_accounting_or_labels")
    if private:
        has_image = any(
            isinstance(value, str) and value.startswith("data:image/")
            for value in _walk_values(payload)
        )
        if not has_image:
            raise ControlledExternalBlocked("gemini_private_request_requires_visual_source")
    if not bool(payload.get("response_format")):
        raise ControlledExternalBlocked("gemini_typed_schema_required")
    if payload.get("response_format") != _allowed_gemini_response_format(stage):
        raise ControlledExternalBlocked("gemini_typed_schema_required")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ControlledExternalBlocked("gemini_messages_required")
    if any(
        not isinstance(message, Mapping)
        or str(message.get("role") or "") not in {"system", "user"}
        for message in messages
    ):
        raise ControlledExternalBlocked("gemini_conversation_history_forbidden")


def _allowed_gemini_response_format(stage: str) -> dict[str, Any]:
    prefix = "controlled_gemini_supplementary:"
    normalized = str(stage or "").strip()
    if not normalized.startswith(prefix):
        return gemini_response_format()
    target_value = normalized.removeprefix(prefix)
    try:
        target_type = SupplementaryTargetType(target_value)
    except ValueError as exc:
        raise ControlledExternalBlocked(
            "controlled_supplementary_target_not_allowed"
        ) from exc
    return supplementary_response_format(SupplementaryTarget(
        target_type=target_type,
    ))


def _validate_deepseek_payload(payload: Mapping[str, Any]) -> None:
    _assert_no_forbidden_keys(payload, _FORBIDDEN_KEYS)
    _assert_no_binary_or_path(payload)
    messages = payload.get("messages") or []
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, Mapping):
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        try:
            embedded = json.loads(content)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        _assert_no_forbidden_keys(embedded, _FORBIDDEN_KEYS)
        _assert_no_binary_or_path(embedded)


def _assert_no_binary_or_path(value: Any) -> None:
    for item in _walk_values(value):
        if isinstance(item, (bytes, bytearray, memoryview)):
            raise ControlledExternalBlocked("deepseek_source_binary_forbidden")
        if isinstance(item, str) and item.lstrip().startswith(_BINARY_PREFIXES):
            raise ControlledExternalBlocked("deepseek_source_binary_forbidden")
        if isinstance(item, str) and _looks_like_private_path(item):
            raise ControlledExternalBlocked("deepseek_local_path_or_filename_forbidden")


def _assert_no_forbidden_keys(value: Any, forbidden: set[str]) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]+", "_", str(key).casefold()).strip("_")
            if normalized in forbidden:
                raise ControlledExternalBlocked("controlled_payload_forbidden_field")
            _assert_no_forbidden_keys(item, forbidden)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_no_forbidden_keys(item, forbidden)


def _walk_values(value: Any):
    if isinstance(value, Mapping):
        for item in value.values():
            yield from _walk_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_values(item)
    else:
        yield value


def _redact_sensitive_text(value: str) -> str:
    text = " ".join(str(value or "").split())[:600]
    text = re.sub(r"\b[A-Za-z]:[\\/][^\s,;]+", "[redacted-path]", text)
    text = re.sub(r"\\\\[^\s,;]+", "[redacted-path]", text)
    text = re.sub(
        r"\b[^\s,;]+\.(?:pdf|png|jpe?g|tiff?|xlsx?|csv)\b",
        "[redacted-filename]", text, flags=re.I,
    )
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[redacted-email]", text)
    text = re.sub(r"\b(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}\b", "[redacted-phone]", text)
    text = re.sub(r"\b(?:account|acct|a/c)\s*[#:]?\s*[A-Z0-9-]{4,}\b", "[redacted-account]", text, flags=re.I)
    text = re.sub(
        r"\b\d{1,6}\s+[A-Za-z0-9.'-]+(?:\s+[A-Za-z0-9.'-]+){0,3}\s+"
        r"(?:street|st|road|rd|avenue|ave|lane|ln|drive|dr|boulevard|blvd|court|ct)\b[^,;]*",
        "[redacted-address]", text, flags=re.I,
    )
    # Conservative person-name minimization. Losing a product/vendor phrase is
    # preferable to leaking a human identity into candidate-only reasoning.
    text = re.sub(r"\b[A-Z][a-z]{2,}\s+[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})?\b", "[redacted-name]", text)
    return text


def _safe_number(value: Any) -> str | None:
    if value is None or value == "":
        return None
    text = str(value).strip()
    return text if re.fullmatch(r"-?\d+(?:\.\d{1,6})?", text) else None


def _safe_taxonomy_value(value: Any) -> str:
    text = re.sub(r"[^a-z0-9_.-]+", "_", str(value or "").strip().casefold())
    return text[:80]


def _looks_like_private_path(value: str) -> bool:
    text = str(value or "").strip()
    lowered = text.casefold()
    return bool(
        re.match(r"^[a-z]:[\\/]", lowered)
        or lowered.startswith(("/users/", "/home/", "\\\\"))
        or re.search(r"\.(?:pdf|png|jpe?g|tiff?|xlsx?|csv)(?:$|[?#])", lowered)
    )


def _provider_name(value: str) -> str:
    normalized = str(value or "").strip().casefold().replace("_", "-")
    if normalized in {"google", "google-gemini", "gemini"}:
        return "gemini"
    return normalized


def _safe_experiment_id(value: str) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"exp-[A-Za-z0-9_-]{1,76}", text):
        raise ControlledExternalBlocked("invalid_experiment_id")
    return text


def _require_sha256(value: str) -> str:
    digest = str(value or "").strip().casefold()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ControlledExternalBlocked("sha256_required")
    return digest


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _truthy(value: Any) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


__all__ = [
    "ALLOWED_GEMINI_PATHS", "ALLOWED_PROVIDER_HOSTS", "CONTROLLED_EXTERNAL_CONTRACT_VERSION",
    "CONTROLLED_EXTERNAL_MODE_ENV", "ControlledCallPermit",
    "ControlledCallPermitLifecycle", "ControlledCallPermitState",
    "ControlledCallPurpose",
    "ControlledDocumentCallBudget", "ControlledDocumentScope",
    "ControlledExternalBlocked", "ControlledExternalController",
    "ControlledExternalGateTerminated", "ControlledGateExecutionState",
    "ExperimentExecutionMode", "ExperimentProviderContext", "MAXIMUM_PHASE_A_DOCUMENTS",
    "PRIVATE_AUTHORIZATION_SHA256", "PRIVATE_AUTHORIZATION_TEXT",
    "PrivateTransferAuthorization", "activate_controlled_external",
    "activate_experiment_provider_context", "build_experiment_provider_context",
    "assert_controlled_external_dispatch_allowed", "build_deepseek_minimized_facts",
    "controlled_document_scope", "controlled_external_active",
    "controlled_urlopen", "current_controller", "current_document_scope",
    "current_experiment_provider_context", "execution_mode",
    "preflight_controlled_provider_route", "require_experiment_provider_context",
    "spend_document_context",
]
