"""Isolated Phase A baseline runner for the private learning experiment.

The module deliberately imports no application settings at import time.  The
runtime root and experiment tenant are established first, then the production
pipeline is imported and invoked in dry-run mode. Detailed artifacts remain in
the ignored experiment root; callers receive only aggregate Git-safe metrics.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import math
import os
import random
import shutil
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .document_learning_experiment import (
    ExperimentPathError,
    InventorySourceChangedError,
    _assert_runtime_git_ignored,
    _is_relative_to,
    _read_jsonl,
    _sha256_file,
    assert_git_safe_summary,
)
from .experiment_spend_controller import (
    ExperimentSpendController,
    activate_experiment_spend_gate,
)
from .controlled_external_experiment import (
    ControlledExternalGateTerminated,
    ControlledExternalController,
    ControlledGateExecutionState,
    ExperimentExecutionMode,
    activate_experiment_provider_context,
    activate_controlled_external,
    build_experiment_provider_context,
    controlled_document_scope,
)
from .reconciliation_observability import (
    ReconciliationObservation,
    ReconciliationState,
    ReconciliationStatus,
    SupplementaryVisualStatus,
    combine_reconciliation_observations,
    observe_reconciliation,
    unavailable_reconciliation,
)


RUN_CONTRACT_VERSION = "document-learning-phase-a-run/1.0"
PRICING_VERSION = "configured-private-rate-card/2026-07-18"


@dataclass(frozen=True)
class PhaseABaselineResult:
    private_run_root: Path
    git_safe_summary: Mapping[str, Any]


class PhaseATerminalDisposition(str, Enum):
    ACCEPTED = "accepted"
    REVIEW_REQUIRED = "review_required"
    UNSUPPORTED = "unsupported"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class SafeTerminalDisposition:
    disposition: PhaseATerminalDisposition
    sanitized_failure_code: str
    safe_terminal_stage: str
    review_required: bool
    unsupported: bool
    accepted: bool
    exportable: bool
    document_facts_exist: bool
    provenance_exists: bool
    intermediate_observation_exists: bool
    reconciliation_state: str
    reconciliation_ran: bool
    reconciliation_status: str
    reconciliation_source_stage: str
    reconciliation_before: str | None
    reconciliation_after: str | None
    reconciliation_delta_before: str | None
    reconciliation_delta_after: str | None
    supplementary_visual_status: str
    processing_failure_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "disposition": self.disposition.value,
            "sanitized_failure_code": self.sanitized_failure_code,
            "safe_terminal_stage": self.safe_terminal_stage,
            "review_required": self.review_required,
            "unsupported": self.unsupported,
            "accepted": self.accepted,
            "exportable": self.exportable,
            "document_facts_exist": self.document_facts_exist,
            "provenance_exists": self.provenance_exists,
            "intermediate_observation_exists": self.intermediate_observation_exists,
            "reconciliation_state": self.reconciliation_state,
            "reconciliation_ran": self.reconciliation_ran,
            "reconciliation_status": self.reconciliation_status,
            "reconciliation_source_stage": self.reconciliation_source_stage,
            "reconciliation_before": self.reconciliation_before,
            "reconciliation_after": self.reconciliation_after,
            "reconciliation_delta_before": self.reconciliation_delta_before,
            "reconciliation_delta_after": self.reconciliation_delta_after,
            "supplementary_visual_status": self.supplementary_visual_status,
            "processing_failure_count": self.processing_failure_count,
        }


class PrivateProviderTransferAuthorizationRequired(RuntimeError):
    """Raised before side effects when private-provider transfer is not authorized."""


def run_phase_a_baseline(
    *, project_root: Path, source_root: Path, experiment_runtime_root: Path,
    inventory_snapshot_root: Path, calibration_manifest_path: Path,
    split_root: Path, experiment_id: str,
    private_provider_transfer_authorized: bool = False,
    local_only: bool = False,
    local_model: str = "",
    local_base_url: str = "http://127.0.0.1:11434",
    local_profile_id: str = "",
    execution_mode: str | None = None,
    controlled_external_authorization_path: Path | None = None,
    expected_manifest_sha256: str | None = None,
    assignment_offset: int = 0,
    assignment_limit: int | None = None,
) -> PhaseABaselineResult:
    mode = ExperimentExecutionMode(
        execution_mode
        or (
            ExperimentExecutionMode.LOCAL_ONLY.value
            if local_only
            else ExperimentExecutionMode.CONTROLLED_EXTERNAL.value
        )
    )
    if mode is ExperimentExecutionMode.LOCAL_ONLY and not local_only:
        local_only = True
    if local_only and mode is not ExperimentExecutionMode.LOCAL_ONLY:
        raise ValueError("local_only_conflicts_with_execution_mode")
    if mode is ExperimentExecutionMode.CONTROLLED_EXTERNAL and not private_provider_transfer_authorized:
        raise PrivateProviderTransferAuthorizationRequired(
            "explicit informed authorization is required before private documents "
            "may be transmitted to configured external providers"
        )
    if local_only and not str(local_model or "").strip():
        raise ValueError("local_model_required")
    project = project_root.resolve(strict=True)
    source = source_root.resolve(strict=True)
    experiment_root = experiment_runtime_root.resolve(strict=True)
    inventory_root = inventory_snapshot_root.resolve(strict=True)
    calibration_path = calibration_manifest_path.resolve(strict=True)
    private_split_root = split_root.resolve(strict=True)
    if _is_relative_to(source, project):
        if not local_only:
            raise ExperimentPathError("private source must remain outside the repository")
        _assert_runtime_git_ignored(project, source)
    for private_input in (experiment_root, inventory_root, calibration_path, private_split_root):
        if not _is_relative_to(private_input, experiment_root):
            raise ExperimentPathError("Phase A inputs must remain in the private experiment root")
    if not experiment_id.startswith("exp-"):
        raise ValueError("Phase A requires an exp-* experiment identity")
    _assert_runtime_git_ignored(project, experiment_root)
    controlled_controller: ControlledExternalController | None = None
    if mode is ExperimentExecutionMode.CONTROLLED_EXTERNAL:
        if controlled_external_authorization_path is None:
            raise PrivateProviderTransferAuthorizationRequired(
                "a private, versioned informed-authorization record is required; "
                "the legacy boolean flag is not sufficient"
            )
        controlled_controller = ControlledExternalController(
            private_root=experiment_root,
            experiment_id=experiment_id,
            manifest_path=calibration_path,
            inventory_path=inventory_root / "inventory.jsonl",
            expected_manifest_sha256=expected_manifest_sha256,
            authorization_path=controlled_external_authorization_path,
        )
        # Validate the authorization and provider-policy confirmations before
        # creating a run directory or importing application settings.
        controlled_controller.require_private_authorization()

    run_id = datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%S%fZ")
    run_root = experiment_root / "phase_a" / "runs" / run_id
    run_root.mkdir(parents=True, exist_ok=False)
    isolated_runtime = run_root / "runtime"
    isolated_runtime.mkdir(parents=True)

    _configure_isolated_environment(
        isolated_runtime, experiment_id,
        local_only=local_only,
        local_model=local_model,
        local_base_url=local_base_url,
        local_profile_id=local_profile_id,
        execution_mode=mode,
    )
    if "webapp.backend.settings" in sys.modules:
        raise RuntimeError("application settings were imported before experiment isolation")

    # Import only after the environment boundary is authoritative.
    from webapp.backend import settings
    from . import (
        accounting_readiness,
        ai_invoice_processor,
        ai_provider,
        ai_runtime_trace,
        batch_processor,
        batch_store,
        row_normalizer,
        semantic_reasoning_gateway,
    )
    from .provider_capabilities import ModelProfileRole, ProfileLoader
    from .local_inference_guard import local_network_isolation
    from utils import extraction_trace

    if settings.WEBAPP_DATA_ROOT.resolve() != isolated_runtime.resolve():
        raise RuntimeError("experiment runtime isolation failed")
    profiles = ProfileLoader().load()
    selected_profiles = (
        _configure_verified_local_profiles(profiles)
        if local_only else _configure_verified_gemini_facts_profile(profiles)
    )
    vision_profile = next((
        profile for profile in profiles
        if profile.profile_id == selected_profiles["vision"]
    ), None)
    if vision_profile is None or vision_profile.profile_id != selected_profiles["vision"]:
        raise RuntimeError("verified economical provider routing could not be enforced")
    if not local_only and (
        vision_profile.provider != "gemini" or semantic_reasoning_gateway._enabled()
    ):
        raise RuntimeError("gemini_facts_only_topology_could_not_be_enforced")
    if not vision_profile.enabled or not vision_profile.credentials_present:
        raise RuntimeError("configured extraction providers are not enabled")

    inventory = {
        str(row["document_id"]): row
        for row in _read_jsonl(inventory_root / "inventory.jsonl")
    }
    locators = {
        str(row["document_id"]): str(row["relative_path"])
        for row in _read_jsonl(inventory_root / "private_locators.jsonl")
    }
    calibration = json.loads(calibration_path.read_text(encoding="utf-8"))
    assignments = list(calibration.get("assignments") or [])
    if not assignments or len(assignments) > 100:
        raise ValueError("invalid Phase A calibration sample size")
    start = max(0, int(assignment_offset or 0))
    stop = None if assignment_limit is None else start + max(1, int(assignment_limit))
    assignments = assignments[start:stop]
    if not assignments:
        raise ValueError("Phase A assignment slice is empty")

    selected_sources = _validate_selected_sources(
        source=source, assignments=assignments, inventory=inventory, locators=locators,
    )
    official_before = _official_runtime_fingerprint(project / "webapp_data")
    # One experiment-level ledger makes the Phase A cap cumulative across
    # failed attempts and operator-authorized reruns.
    controller = ExperimentSpendController(experiment_root, experiment_id)
    spend_before = _spend_reservation_ids(controller)
    controlled_event_offset = (
        controlled_controller.telemetry_path.stat().st_size
        if controlled_controller is not None
        and controlled_controller.telemetry_path.exists()
        else 0
    )
    source_map: dict[str, dict[str, Any]] = {}
    shard_results: list[dict[str, Any]] = []
    shard_failures: list[dict[str, str]] = []
    gate_failure: dict[str, Any] | None = None
    gate_execution = ControlledGateExecutionState(len(assignments))
    wall_started = time.perf_counter()

    try:
        network_boundary = local_network_isolation() if local_only else contextlib.nullcontext()
        controlled_boundary = (
            activate_controlled_external(controlled_controller)
            if controlled_controller is not None
            else contextlib.nullcontext()
        )
        with network_boundary, controlled_boundary, activate_experiment_spend_gate(
            controller, phase="A", pricing_version=PRICING_VERSION,
        ):
            shard_size = 1 if controlled_controller is not None else 10
            for shard_index, shard in enumerate(_chunks(assignments, shard_size), start=1):
                assignment_index = shard_index - 1
                if controlled_controller is not None and not gate_execution.claim(
                    assignment_index
                ):
                    break
                batch_id = batch_store.create_batch()
                batch_root = batch_store.get_batch_dir(batch_id)
                input_root = batch_store.get_input_dir(batch_id)
                metadata = {
                    "batch_id": batch_id,
                    "batch_name": f"Private Phase A shard {shard_index}",
                    "status": "idle",
                    "document_mode": "auto_detect",
                    "ai_fallback_enabled": True,
                    "ai_fallback_policy": "only_low_confidence",
                    "experiment_id": experiment_id,
                    "experiment_phase": "A",
                }
                (batch_root / "batch_metadata.json").write_text(
                    json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8",
                )
                for item in shard:
                    document_id = str(item["document_id"])
                    source_path = selected_sources[document_id]
                    staged_name = source_path.name
                    if staged_name in source_map:
                        raise RuntimeError("duplicate private basename cannot be staged safely")
                    destination = input_root / staged_name
                    shutil.copy2(source_path, destination)
                    if _sha256_file(destination) != str(inventory[document_id]["content_sha256"]):
                        raise InventorySourceChangedError("staged source hash mismatch")
                    source_map[staged_name] = {
                        "document_id": document_id,
                        "unit_id": str(item["unit_id"]),
                        "evaluation_scope": str(item["evaluation_scope"]),
                        "cohort": str(item["cohort"]),
                        "source_content_sha256": str(inventory[document_id]["content_sha256"]),
                    }
                extraction_trace.start_batch(batch_id)
                document_boundary = (
                    controlled_document_scope(
                        document_sha256=str(
                            inventory[str(shard[0]["document_id"])]["content_sha256"]
                        )
                    )
                    if controlled_controller is not None
                    else contextlib.nullcontext()
                )
                experiment_provider_context = None
                try:
                    if controlled_controller is not None:
                        document_sha256 = str(
                            inventory[str(shard[0]["document_id"])]["content_sha256"]
                        )
                        experiment_provider_context = build_experiment_provider_context(
                            controller=controlled_controller,
                            document_sha256=document_sha256,
                            authorized_provider=vision_profile.provider,
                            authorized_model=vision_profile.model_id,
                            authorized_profile_id=vision_profile.profile_id,
                            allowed_endpoint=_controlled_chat_endpoint(vision_profile.base_url),
                        )
                    # Existing processors can print private source names. The
                    # experiment discards console output and persists only safe
                    # request traces plus the private structured result.
                    previous_disable = logging.root.manager.disable
                    logging.disable(logging.CRITICAL)
                    try:
                        with open(os.devnull, "w", encoding="utf-8") as sink:
                            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                                with document_boundary:
                                    provider_boundary = (
                                        activate_experiment_provider_context(
                                            experiment_provider_context
                                        )
                                        if experiment_provider_context is not None
                                        else contextlib.nullcontext()
                                    )
                                    with provider_boundary:
                                        if controlled_controller is not None:
                                            source_file = next(iter(input_root.iterdir()))
                                            ai_payload = ai_invoice_processor.process_ai_vendor_files(
                                                batch_id=batch_id,
                                                vendor_key="unknown",
                                                files=[source_file],
                                                detection={
                                                    source_file.name: {
                                                        "vendor_key": "unknown",
                                                        "confidence": 0.0,
                                                        "reason": "phase_a_gemini_facts_only",
                                                        "supported_in_phase_1": False,
                                                        "processing_mode": "ai_assisted",
                                                    },
                                                },
                                                tracker=None,
                                                dry_run=True,
                                                experiment_provider_context=experiment_provider_context,
                                            )
                                        else:
                                            result = batch_processor.process_batch(
                                                batch_id, dry_run=True,
                                            )
                                            row_normalizer.normalize_result(result)
                    finally:
                        logging.disable(previous_disable)
                    if controlled_controller is not None:
                        result_path = batch_store.get_processed_dir(batch_id) / "_webapp_result.json"
                        result = _finalize_controlled_processor_result(
                            ai_payload,
                            result_path=result_path,
                            normalize_result=row_normalizer.normalize_result,
                            attach_readiness=lambda value: _attach_readiness(
                                value, accounting_readiness,
                            ),
                            assert_provenance=_assert_controlled_result_provenance,
                        )
                    else:
                        _attach_readiness(result, accounting_readiness)
                        result_path = batch_store.get_processed_dir(batch_id) / "_webapp_result.json"
                        result_path.write_text(
                            json.dumps(result, indent=2, sort_keys=True, default=str) + "\n",
                            encoding="utf-8",
                        )
                    shard_results.append(result)
                    if controlled_controller is not None:
                        gate_execution.complete(assignment_index)
                except ControlledExternalGateTerminated as exc:
                    gate_execution.terminate(exc.failure_code)
                    result_path = batch_store.get_processed_dir(batch_id) / "_webapp_result.json"
                    blocked_result = _persist_controlled_gate_failure_result(
                        result_path=result_path,
                        failure_code=exc.failure_code,
                    )
                    shard_results.append(blocked_result)
                    gate_execution.complete(assignment_index)
                    gate_failure = {
                        "failure_code": exc.failure_code,
                        "assignment_index": assignment_index,
                        "batch_id": batch_id,
                    }
                    controller.cancel_outstanding(reason=exc.failure_code)
                    break
                except Exception as exc:
                    shard_failures.append({
                        "batch_id": batch_id,
                        "failure_code": _safe_failure_code(exc),
                    })
                    if controlled_controller is not None:
                        gate_execution.complete(assignment_index)
                        break
                finally:
                    try:
                        extraction_trace.flush_batch(batch_id, batch_root / "trace")
                    finally:
                        extraction_trace.end_batch(batch_id)
                        extraction_trace.clear_batch(batch_id)
    except KeyboardInterrupt:
        controller.cancel_outstanding(reason="operator_interrupt")
        raise

    wall_seconds = time.perf_counter() - wall_started
    # Freeze raw baseline output before the evaluator is allowed to open hidden
    # holdout answers. This is the physical training/evaluation boundary.
    frozen_output = {
        "schema_version": RUN_CONTRACT_VERSION,
        "experiment_id": experiment_id,
        "calibration_version": calibration_path.parent.name,
        "split_version": private_split_root.name,
        "shard_results": shard_results,
        "shard_failures": shard_failures,
        "source_map": source_map,
        "controlled_gate_failure": gate_failure,
        "controlled_gate_execution": gate_execution.snapshot(),
    }
    frozen_path = run_root / "private_baseline_output.json"
    _write_immutable_json(frozen_path, frozen_output)
    frozen_sha256 = _sha256_file(frozen_path)
    if controller.snapshot("A").outstanding_reservation_ids:
        raise RuntimeError("Phase A ended with unsettled provider reservations")

    run_reservation_ids = _spend_reservation_ids(controller) - spend_before
    controlled_audit = _controlled_gate_audit(
        controlled_controller,
        event_offset=controlled_event_offset,
        run_reservation_ids=run_reservation_ids,
        spend_controller=controller,
    ) if controlled_controller is not None else {}

    metrics = _evaluate_frozen_baseline(
        frozen_path=frozen_path,
        expected_frozen_sha256=frozen_sha256,
        split_root=private_split_root,
        controller=controller,
        wall_seconds=wall_seconds,
        isolated_runtime=isolated_runtime,
        run_reservation_ids=run_reservation_ids,
        controlled_audit=controlled_audit,
    )
    if controlled_controller is not None:
        _finalize_controlled_gate_metrics(metrics)
    _write_immutable_json(run_root / "private_metrics.json", metrics)
    _write_private_html(run_root / "private_report.html", metrics)

    _validate_selected_sources(
        source=source, assignments=assignments, inventory=inventory, locators=locators,
    )
    official_after = _official_runtime_fingerprint(project / "webapp_data")
    if official_before != official_after:
        raise RuntimeError("official runtime changed during isolated experiment")

    safe = _git_safe_phase_a_summary(metrics, assignments, shard_failures)
    assert_git_safe_summary(safe)
    _write_immutable_json(run_root / "git_safe_summary.json", safe)
    return PhaseABaselineResult(run_root, safe)


def _configure_isolated_environment(
    runtime_root: Path,
    experiment_id: str,
    *,
    local_only: bool = False,
    local_model: str = "",
    local_base_url: str = "http://127.0.0.1:11434",
    local_profile_id: str = "",
    execution_mode: ExperimentExecutionMode = ExperimentExecutionMode.NORMAL,
) -> None:
    if local_only and execution_mode is ExperimentExecutionMode.NORMAL:
        execution_mode = ExperimentExecutionMode.LOCAL_ONLY
    values = {
        "INNER_VIEW_EXPERIMENT_MODE": "1",
        "INNER_VIEW_WEBAPP_DATA_ROOT": str(runtime_root),
        "INNER_VIEW_TENANT_ID": experiment_id,
        "INNER_VIEW_EXPERIMENT_AUTHORIZED_TENANT_ID": experiment_id,
        "INNER_VIEW_DEPLOYMENT_MODE": "production",
        "AI_VISION_NATIVE_PDF_ENABLED": "0",
        "AI_FAST_FIRST_GOLDEN_PARITY_APPROVED": "1",
        "AI_MAX_COST_PER_BATCH_USD": "200",
        "AI_FILE_WORKERS": "4",
        "AI_INVOICE_GROUP_WORKERS": "4",
        "ACCOUNTING_DECISION_ENGINE_V2": "1",
        "INNER_VIEW_EXPERIMENT_EXECUTION_MODE": execution_mode.value,
    }
    for key, value in values.items():
        os.environ[key] = value
    if local_only:
        local_values = {
            "INNER_VIEW_LOCAL_INFERENCE_ONLY": "1",
            "LOCAL_MULTIMODAL_MODEL": str(local_model).strip(),
            "LOCAL_MULTIMODAL_BASE_URL": str(local_base_url).strip(),
            "LOCAL_MULTIMODAL_PROFILE_ID": str(local_profile_id).strip(),
            "LOCAL_MULTIMODAL_TIMEOUT_SECONDS": "240",
            "LOCAL_MULTIMODAL_CONTEXT_TOKENS": "8192",
            "AI_ASSIST_ENABLED": "1",
            "AI_PROVIDER": "local_ollama",
            "AI_MODEL": str(local_model).strip(),
            "AI_API_KEY": "",
            "AI_BASE_URL": str(local_base_url).strip(),
            "AI_VISION_ENABLED": "1",
            "AI_VISION_PROVIDER": "local_ollama",
            "AI_VISION_MODEL": str(local_model).strip(),
            "AI_VISION_API_KEY": "",
            "AI_VISION_BASE_URL": str(local_base_url).strip(),
            "AI_VISION_MODE": "fallback_only",
            "AI_VISION_NATIVE_PDF_ENABLED": "0",
            "AI_FAST_FIRST_FACTS_ONLY_ENABLED": "0",
            # A 4 GiB GPU cannot host concurrent 2B vision contexts without
            # severe memory pressure.  Keep document order stable and permit
            # only one local inference group at a time.
            "AI_FILE_WORKERS": "1",
            "AI_INVOICE_GROUP_WORKERS": "1",
        }
        for key, value in local_values.items():
            os.environ[key] = value
    elif execution_mode is ExperimentExecutionMode.CONTROLLED_EXTERNAL:
        os.environ["INNER_VIEW_LOCAL_INFERENCE_ONLY"] = "0"
        os.environ["AI_VISION_MODE"] = "always"
        os.environ["AI_FAST_FIRST_FACTS_ONLY_ENABLED"] = "1"
        os.environ["AI_FAST_FIRST_GOLDEN_PARITY_APPROVED"] = "1"
        os.environ["AI_SEMANTIC_REASONING_ENABLED"] = "0"
        os.environ["AI_TEXT_ROUTING_PROFILE_ID"] = ""


def _configure_verified_local_profiles(profiles: Sequence[Any]) -> dict[str, str]:
    from .provider_capabilities import ModelProfileRole

    evaluation_profile_id = os.environ.get(
        "LOCAL_MULTIMODAL_PROFILE_ID", "",
    ).strip()
    expected = {
        "text": (
            ModelProfileRole.TEXT_EXTRACTION,
            f"{evaluation_profile_id}-text" if evaluation_profile_id else "local-text",
        ),
        "vision": (
            ModelProfileRole.MULTIMODAL_EXTRACTION,
            evaluation_profile_id or "local-vision",
        ),
        "accounting": (
            ModelProfileRole.ACCOUNTING_REASONING,
            f"{evaluation_profile_id}-accounting"
            if evaluation_profile_id else "local-accounting",
        ),
        "verification": (
            ModelProfileRole.INDEPENDENT_VERIFICATION,
            f"{evaluation_profile_id}-verification"
            if evaluation_profile_id else "local-verification",
        ),
    }
    selected: dict[str, str] = {}
    for name, (role, profile_id) in expected.items():
        profile = next(
            (
                item for item in profiles
                if item.role is role and item.profile_id == profile_id
                and item.provider == "local_ollama" and item.enabled
            ),
            None,
        )
        if profile is None:
            raise RuntimeError(f"verified_local_{name}_profile_unavailable")
        selected[name] = profile.profile_id
    ids = ",".join(sorted(selected.values()))
    os.environ["AI_COST_ROUTING_VERIFIED_PROFILE_IDS"] = ids
    os.environ["AI_TEXT_ROUTING_PROFILE_ID"] = selected["text"]
    os.environ["AI_VISION_ROUTING_PROFILE_ID"] = selected["vision"]
    return selected


def _configure_verified_gemini_facts_profile(profiles: Sequence[Any]) -> dict[str, str]:
    from .provider_capabilities import ModelProfileRole

    report_path = Path(str(os.environ.get("AI_PROVIDER_CAPABILITY_REPORT") or ""))
    if not report_path.is_file():
        raise RuntimeError("private capability report is unavailable")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    healthy = {
        str(row.get("profile_id"))
        for row in report.get("profiles") or []
        if row.get("health_status") == "healthy"
    }
    role_preferences = {
        "vision": (ModelProfileRole.MULTIMODAL_EXTRACTION, "gemini"),
    }
    selected: dict[str, str] = {}
    for name, (role, provider) in role_preferences.items():
        eligible = [
            profile for profile in profiles
            if profile.role is role and profile.provider == provider
            and profile.profile_id in healthy and profile.enabled
            and profile.credentials_present
            and profile.input_cost_usd_per_million is not None
            and profile.output_cost_usd_per_million is not None
        ]
        if not eligible:
            raise RuntimeError(f"verified_{name}_profile_unavailable")
        profile = min(eligible, key=lambda item: (
            float(item.input_cost_usd_per_million)
            + float(item.output_cost_usd_per_million),
            item.routing_priority,
            item.profile_id,
        ))
        selected[name] = profile.profile_id
    os.environ["AI_COST_ROUTING_VERIFIED_PROFILE_IDS"] = selected["vision"]
    os.environ["AI_TEXT_ROUTING_PROFILE_ID"] = ""
    os.environ["AI_VISION_ROUTING_PROFILE_ID"] = selected["vision"]
    selected_profile = next(item for item in profiles if item.profile_id == selected["vision"])
    os.environ["AI_VISION_ESCALATION_MODEL"] = selected_profile.model_id
    return selected


def _controlled_chat_endpoint(base_url: str) -> str:
    base = str(base_url or "").strip().rstrip("/")
    return base if base.endswith("/chat/completions") else f"{base}/chat/completions"


def _validate_selected_sources(
    *, source: Path, assignments: Sequence[Mapping[str, Any]],
    inventory: Mapping[str, Mapping[str, Any]], locators: Mapping[str, str],
) -> dict[str, Path]:
    selected: dict[str, Path] = {}
    for item in assignments:
        document_id = str(item["document_id"])
        record = inventory.get(document_id)
        relative = locators.get(document_id)
        if record is None or not relative:
            raise InventorySourceChangedError("selected source is missing from inventory")
        path = (source / relative).resolve(strict=True)
        try:
            path.relative_to(source)
        except ValueError as exc:
            raise InventorySourceChangedError("selected locator escaped source root") from exc
        if _sha256_file(path) != str(record.get("content_sha256") or ""):
            raise InventorySourceChangedError("selected source changed after inventory")
        selected[document_id] = path
    return selected


def _attach_readiness(result: dict[str, Any], accounting_readiness: Any) -> None:
    for invoice in result.get("all_invoices") or []:
        rows = list(invoice.get("rows") or [])
        decision = accounting_readiness.evaluate_rows(rows)
        invoice["_experiment_readiness"] = accounting_readiness.as_dict(decision)


_SAFE_PROCESSOR_FAILURE_CODES = {
    "ai_invoice_processing_not_configured",
    "ai_processing_failed",
    "ai_response_invalid_json",
    "ai_response_output_limit_exceeded",
    "ai_vision_not_configured",
    "controlled_provider_processing_failure",
    "controlled_local_execution_error",
    "controlled_provider_route_blocked",
    "initial_structured_response_invalid",
    "manual_review_required",
    "page_reconciliation_failed",
    "segmented_invoice_processing_failed",
    "supplementary_visual_evidence_unresolved",
    "supplementary_visual_evidence_contradiction",
    "supplementary_evidence_localization_unavailable",
    "supplementary_request_limit_reached",
    "total_reconciliation_failed",
    "visual_evidence_unavailable",
}
_REASON_PRIORITY = (
    "controlled_provider_route_blocked",
    "controlled_local_execution_error",
    "supplementary_request_limit_reached",
    "initial_structured_response_invalid",
    "supplementary_evidence_localization_unavailable",
    "supplementary_visual_evidence_contradiction",
    "supplementary_visual_evidence_unresolved",
    "visual_evidence_unavailable",
)
_SAFE_PROCESSING_STAGES = {
    "accounting_pipeline",
    "initialization",
    "local_observation_merge",
    "mapping_review",
    "normalization",
    "observed_facts_persistence",
    "processor_result",
    "reconciliation",
    "support_document_link",
}
_SAFE_SUMMARY_COUNTS = {
    "files_total", "files_unique", "files_deduplicated", "files_processed",
    "files_unique_processed", "files_unsupported", "processing_failures",
    "invoices_produced", "rows_total", "line_items", "manual_review_total",
    "invoices_flagged_for_review",
}


def _canonical_failure_code(value: Any) -> str:
    code = _known_processor_failure_code(value)
    return "processor_failure" if code in {None, "ai_processing_failed"} else code


def _known_processor_failure_code(value: Any) -> str | None:
    token = "".join(
        character if character.isalnum() else "_"
        for character in str(value or "").strip().casefold()
    ).strip("_")
    return token if token in _SAFE_PROCESSOR_FAILURE_CODES else None


def _canonical_review_codes(values: Sequence[Any]) -> list[str]:
    known: list[str] = []
    generic_present = False
    for value in values:
        token = "".join(
            character if character.isalnum() else "_"
            for character in str(value or "").strip().casefold()
        ).strip("_")
        if token in {"processor_failure", "ai_processing_failed"}:
            generic_present = True
            continue
        code = _known_processor_failure_code(token)
        if code is not None and code not in known:
            known.append(code)
    if known:
        priority = {code: index for index, code in enumerate(_REASON_PRIORITY)}
        return sorted(known, key=lambda code: (priority.get(code, len(priority)), code))
    return ["processor_failure"] if generic_present or not known else known


def _preferred_review_code(values: Sequence[Any]) -> str:
    codes = _canonical_review_codes(values)
    return next((code for code in _REASON_PRIORITY if code in codes), codes[0])


def _canonical_processing_stage(value: Any) -> str:
    token = "".join(
        character if character.isalnum() else "_"
        for character in str(value or "").strip().casefold()
    ).strip("_")
    return token if token in _SAFE_PROCESSING_STAGES else "processor_result"


def _processor_rows(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        row
        for invoice in payload.get("invoices") or []
        if isinstance(invoice, Mapping)
        for row in invoice.get("rows") or []
        if isinstance(row, Mapping)
    ]


def _document_facts_and_provenance(payload: Mapping[str, Any]) -> tuple[bool, bool]:
    rows = _processor_rows(payload)
    if not rows:
        return False, bool(
            isinstance(payload.get("supplementary_provenance"), Mapping)
            and payload.get("supplementary_provenance")
        ) or any(
            bool(item.get("provenance_exists"))
            for collection in (payload.get("manual_review_rows") or [], payload.get("unsupported_files") or [])
            for item in collection
            if isinstance(item, Mapping)
        )
    facts_rows: list[Mapping[str, Any]] = []
    provenance = False
    for row in rows:
        meta = row.get("_meta") if isinstance(row.get("_meta"), Mapping) else {}
        facts = meta.get("document_facts") if isinstance(meta.get("document_facts"), Mapping) else {}
        if facts:
            facts_rows.append(facts)
        source_text = meta.get("source_text") if isinstance(meta.get("source_text"), Mapping) else {}
        provenance = provenance or bool(
            facts.get("evidence")
            or any(
                isinstance(item, Mapping) and item.get("evidence")
                for item in facts.get("line_items") or []
            )
            or source_text.get("raw_description")
        )
    return len(facts_rows) == len(rows), provenance


def _intermediate_observation_exists(payload: Mapping[str, Any]) -> bool:
    return any(
        bool(item.get("intermediate_observation_exists"))
        and isinstance(item.get("intermediate_observation"), Mapping)
        for collection in (
            payload.get("manual_review_rows") or [],
            payload.get("unsupported_files") or [],
        )
        for item in collection
        if isinstance(item, Mapping)
    )


def _processor_reconciliation_observation(
    payload: Mapping[str, Any], *, facts_exist: bool,
) -> ReconciliationObservation:
    candidates: list[Mapping[str, Any]] = []
    if any(key in payload for key in (
        "reconciliation_observation", "reconciliation_status",
        "reconciliation_ran", "supplementary_reconciliation",
    )):
        candidates.append(payload)
    summary = payload.get("summary") if isinstance(payload.get("summary"), Mapping) else {}
    if any(key in summary for key in (
        "reconciliation_observation", "reconciliation_status", "reconciliation_ran",
    )):
        candidates.append(summary)
    for collection in (payload.get("manual_review_rows") or [], payload.get("unsupported_files") or []):
        for item in collection:
            if isinstance(item, Mapping) and any(key in item for key in (
                "reconciliation_observation", "reconciliation_status",
                "reconciliation_ran", "supplementary_visual_status",
            )):
                candidates.append(item)
    if not facts_exist:
        # A typed intermediate observation is not DocumentFacts, but its
        # deterministic reconciliation run is still real observability.  Read
        # only the explicitly serialized observation and never infer facts.
        intermediate = [
            candidate for candidate in candidates
            if candidate.get("intermediate_observation_exists") is True
            and isinstance(candidate.get("reconciliation_observation"), Mapping)
        ]
        if not intermediate:
            return unavailable_reconciliation()
        observations = [
            observe_reconciliation(candidate, facts_exist=True)
            for candidate in intermediate
        ]
        return combine_reconciliation_observations(
            observations, facts_exist=True,
        )
    for invoice in payload.get("invoices") or []:
        if not isinstance(invoice, Mapping):
            continue
        validation = (
            invoice.get("validation_summary")
            if isinstance(invoice.get("validation_summary"), Mapping)
            else {}
        )
        if validation:
            candidates.append(validation)
    observations = [
        observe_reconciliation(candidate, facts_exist=True)
        for candidate in candidates
    ]
    return combine_reconciliation_observations(
        observations, facts_exist=facts_exist,
    )


def _derive_terminal_disposition(payload: Mapping[str, Any]) -> SafeTerminalDisposition:
    summary = payload.get("summary") if isinstance(payload.get("summary"), Mapping) else {}
    processing_failures = max(0, int(summary.get("processing_failures") or 0))
    reviews = [item for item in payload.get("manual_review_rows") or [] if isinstance(item, Mapping)]
    unsupported_rows = [item for item in payload.get("unsupported_files") or [] if isinstance(item, Mapping)]
    facts_exist, provenance_exists = _document_facts_and_provenance(payload)
    intermediate_exists = _intermediate_observation_exists(payload)
    reconciliation = _processor_reconciliation_observation(
        payload, facts_exist=facts_exist,
    )


    invoices_exist = bool(payload.get("invoices"))

    raw_codes: list[Any] = []
    raw_stage: Any = summary.get("safe_terminal_stage")
    for item in (*unsupported_rows, *reviews):
        raw_codes.extend((item.get("reason_code"), item.get("reason")))
        if isinstance(item.get("reason_codes"), Sequence) \
                and not isinstance(item.get("reason_codes"), (str, bytes)):
            raw_codes.extend(item.get("reason_codes") or [])
        if raw_stage in (None, ""):
            raw_stage = item.get("safe_terminal_stage")

    canonical_codes = _canonical_review_codes(raw_codes)
    controlled_local_error = "controlled_local_execution_error" in canonical_codes
    accepted = (
        not controlled_local_error
        and processing_failures == 0
        and invoices_exist
        and facts_exist
        and not reviews
    )
    if controlled_local_error:
        disposition = PhaseATerminalDisposition.BLOCKED
    elif accepted:
        disposition = PhaseATerminalDisposition.ACCEPTED
    elif reviews and provenance_exists:
        disposition = PhaseATerminalDisposition.REVIEW_REQUIRED
    elif unsupported_rows:
        disposition = PhaseATerminalDisposition.UNSUPPORTED
    else:
        disposition = PhaseATerminalDisposition.BLOCKED

    return SafeTerminalDisposition(
        disposition=disposition,
        sanitized_failure_code=(
            "" if disposition is PhaseATerminalDisposition.ACCEPTED
            else _preferred_review_code(raw_codes)
        ),
        safe_terminal_stage=_canonical_processing_stage(raw_stage),
        review_required=disposition is PhaseATerminalDisposition.REVIEW_REQUIRED,
        unsupported=disposition is PhaseATerminalDisposition.UNSUPPORTED,
        accepted=accepted,
        exportable=False,
        document_facts_exist=facts_exist,
        provenance_exists=provenance_exists,
        intermediate_observation_exists=intermediate_exists,
        reconciliation_state=reconciliation.state.value,
        reconciliation_ran=reconciliation.reconciliation_ran,
        reconciliation_status=reconciliation.reconciliation_status.value,
        reconciliation_source_stage=reconciliation.reconciliation_source_stage,
        reconciliation_before=(
            reconciliation.reconciliation_before.value
            if reconciliation.reconciliation_before else None
        ),
        reconciliation_after=(
            reconciliation.reconciliation_after.value
            if reconciliation.reconciliation_after else None
        ),
        reconciliation_delta_before=(
            str(reconciliation.reconciliation_delta_before)
            if reconciliation.reconciliation_delta_before is not None else None
        ),
        reconciliation_delta_after=(
            str(reconciliation.reconciliation_delta_after)
            if reconciliation.reconciliation_delta_after is not None else None
        ),
        supplementary_visual_status=reconciliation.supplementary_visual_status.value,
        processing_failure_count=processing_failures,
    )


def _terminal_quality_metrics(
    terminal_dispositions: Sequence[Mapping[str, Any]],
    terminal_reason_sets: Sequence[set[str]],
) -> dict[str, Any]:
    """Return document-level denominators without inventing missing facts."""

    facts_count = sum(
        bool(item.get("document_facts_exist")) for item in terminal_dispositions
    )
    intermediate_count = sum(
        bool(item.get("intermediate_observation_exists"))
        for item in terminal_dispositions
    )
    transport_valid_count = facts_count + intermediate_count
    provenance_count = sum(
        bool(item.get("document_facts_exist")) and bool(item.get("provenance_exists"))
        for item in terminal_dispositions
    )
    reconciliation_count = sum(
        bool(item.get("document_facts_exist")) and bool(item.get("reconciliation_ran"))
        for item in terminal_dispositions
    )
    statuses = Counter(
        str(item.get("reconciliation_status") or "not_run")
        for item in terminal_dispositions
        if item.get("document_facts_exist")
    )
    states = Counter(
        str(item.get("reconciliation_state") or "not_run")
        for item in terminal_dispositions
    )
    canonical_reasons = Counter(
        str(item.get("sanitized_failure_code") or "")
        for item in terminal_dispositions
        if str(item.get("sanitized_failure_code") or "")
    )
    contradiction_count = sum(
        "supplementary_visual_evidence_contradiction" in codes
        for codes in terminal_reason_sets
    )
    limit_count = sum(
        "supplementary_request_limit_reached" in codes
        for codes in terminal_reason_sets
    )
    supplementary_attempted_count = sum(
        str(item.get("supplementary_visual_status") or "not_run") != "not_run"
        for item in terminal_dispositions
    )
    supplementary_resolved_count = sum(
        str(item.get("supplementary_visual_status") or "") == "resolved"
        and str(item.get("reconciliation_state") or "") == "ran_reconciled"
        for item in terminal_dispositions
    )
    unresolved_arithmetic_count = sum(
        bool(item.get("intermediate_observation_exists"))
        and item.get("accepted") is not True
        for item in terminal_dispositions
    )
    unusable_transport_count = sum(
        item.get("disposition") == PhaseATerminalDisposition.UNSUPPORTED.value
        and not item.get("intermediate_observation_exists")
        for item in terminal_dispositions
    )
    return {
        "transport_valid_observation_count": transport_valid_count,
        "transport_valid_observation_rate": _ratio(
            transport_valid_count, len(terminal_dispositions)
        ),
        "intermediate_unreconciled_observation_count": intermediate_count,
        "intermediate_unreconciled_observation_rate": _ratio(
            intermediate_count, len(terminal_dispositions)
        ),
        "document_facts_document_count": facts_count,
        "document_facts_coverage": _ratio(facts_count, len(terminal_dispositions)),
        "provenance_document_count": provenance_count,
        "provenance_coverage": _ratio(provenance_count, facts_count),
        "reconciliation_document_count": reconciliation_count,
        "reconciliation_coverage": _ratio(reconciliation_count, facts_count),
        "reconciliation_status_distribution": dict(sorted(statuses.items())),
        "reconciliation_state_distribution": dict(sorted(states.items())),
        "canonical_reason_distribution": dict(sorted(canonical_reasons.items())),
        "review_required_unresolved_arithmetic_count": unresolved_arithmetic_count,
        "unsupported_unusable_transport_count": unusable_transport_count,
        "supplementary_attempted_document_count": supplementary_attempted_count,
        "supplementary_resolved_document_count": supplementary_resolved_count,
        "supplementary_resolution_rate": _ratio(
            supplementary_resolved_count, supplementary_attempted_count
        ),
        "supplementary_contradiction_rate": _ratio(
            contradiction_count, supplementary_attempted_count
        ),
        "supplementary_limit_rate": _ratio(
            limit_count, supplementary_attempted_count
        ),
    }


def _safe_failed_processor_result(
    payload: Mapping[str, Any], disposition: SafeTerminalDisposition,
) -> dict[str, Any]:
    summary = payload.get("summary") if isinstance(payload.get("summary"), Mapping) else {}
    safe_summary = {
        key: max(0, int(summary.get(key) or 0))
        for key in _SAFE_SUMMARY_COUNTS
    }
    safe_summary["processing_mode"] = "ai_assisted"

    global_reason_values: list[Any] = []
    for item in (
        *(payload.get("manual_review_rows") or []),
        *(payload.get("unsupported_files") or []),
    ):
        if not isinstance(item, Mapping):
            continue
        global_reason_values.extend((item.get("reason_code"), item.get("reason")))
        if isinstance(item.get("reason_codes"), Sequence) \
                and not isinstance(item.get("reason_codes"), (str, bytes)):
            global_reason_values.extend(item.get("reason_codes") or [])
    global_reason_codes = _canonical_review_codes(global_reason_values)
    safe_summary.update({
        "intermediate_observation_exists": disposition.intermediate_observation_exists,
        "reconciliation_state": disposition.reconciliation_state,
        "reconciliation_ran": disposition.reconciliation_ran,
        "reconciliation_status": disposition.reconciliation_status,
        "reconciliation_source_stage": disposition.reconciliation_source_stage,
        "reconciliation_before": disposition.reconciliation_before,
        "reconciliation_after": disposition.reconciliation_after,
        "reconciliation_delta_before": disposition.reconciliation_delta_before,
        "reconciliation_delta_after": disposition.reconciliation_delta_after,
        "supplementary_visual_status": disposition.supplementary_visual_status,
    })

    safe_reviews = []
    for item in payload.get("manual_review_rows") or []:
        if not isinstance(item, Mapping):
            continue
        candidates = list(item.get("reason_codes") or [])
        if item.get("reason_code"):
            candidates.append(item.get("reason_code"))
        safe_review = {
            "review_required": True,
            "reason_codes": _canonical_review_codes([
                *global_reason_codes, *candidates,
            ]),
        }
        if item.get("intermediate_observation_exists"):
            safe_review.update({
                "normalization_outcome": item.get("normalization_outcome"),
                "intermediate_observation_exists": True,
                "intermediate_observation": item.get("intermediate_observation"),
                "initial_observation_revision": item.get("initial_observation_revision"),
                "supplementary_evidence_revisions": item.get(
                    "supplementary_evidence_revisions"
                ) or [],
                "observation_line_item_revisions": item.get(
                    "observation_line_item_revisions"
                ) or {},
                "eligible_supplementary_targets": item.get(
                    "eligible_supplementary_targets"
                ) or [],
                "provenance_exists": bool(item.get("provenance_exists")),
                "reconciliation_observation": item.get("reconciliation_observation"),
                "reconciliation_state": item.get("reconciliation_state"),
                "reconciliation_ran": item.get("reconciliation_ran"),
                "reconciliation_status": item.get("reconciliation_status"),
                "reconciliation_source_stage": item.get(
                    "reconciliation_source_stage"
                ),
                "reconciliation_before": item.get("reconciliation_before"),
                "reconciliation_after": item.get("reconciliation_after"),
                "reconciliation_delta_before": item.get(
                    "reconciliation_delta_before"
                ),
                "reconciliation_delta_after": item.get(
                    "reconciliation_delta_after"
                ),
                "supplementary_visual_status": item.get(
                    "supplementary_visual_status"
                ),
                "accepted": False,
                "export_allowed": False,
            })
        safe_reviews.append(safe_review)

    safe_unsupported = []
    for item in payload.get("unsupported_files") or []:
        if not isinstance(item, Mapping):
            continue
        safe_unsupported.append({
            "unsupported": True,
            "sanitized_failure_code": _preferred_review_code([
                *global_reason_codes, item.get("reason_code"), item.get("reason"),
            ]),
        })

    return {
        "success": False,
        "gate_passed": False,
        "export_allowed": False,
        "summary": safe_summary,
        "invoices": [],
        "all_invoices": [],
        "manual_review_rows": safe_reviews,
        "unsupported_files": safe_unsupported,
        "phase_a_terminal_disposition": disposition.to_dict(),
    }


def _persist_controlled_gate_failure_result(
    *, result_path: Path, failure_code: str,
) -> dict[str, Any]:
    """Persist one explicit non-exportable result before stopping the gate."""

    canonical = _canonical_failure_code(failure_code)
    if canonical in {
        "supplementary_request_limit_reached",
        "supplementary_evidence_localization_unavailable",
        "supplementary_visual_evidence_unresolved",
        "supplementary_visual_evidence_contradiction",
    }:
        terminal_kind = PhaseATerminalDisposition.REVIEW_REQUIRED
    elif canonical in {
        "initial_structured_response_invalid",
        "visual_evidence_unavailable",
    }:
        terminal_kind = PhaseATerminalDisposition.UNSUPPORTED
    else:
        terminal_kind = PhaseATerminalDisposition.BLOCKED
    disposition = SafeTerminalDisposition(
        disposition=terminal_kind,
        sanitized_failure_code=canonical,
        safe_terminal_stage="initialization",
        review_required=terminal_kind is PhaseATerminalDisposition.REVIEW_REQUIRED,
        unsupported=terminal_kind is PhaseATerminalDisposition.UNSUPPORTED,
        accepted=False,
        exportable=False,
        document_facts_exist=False,
        provenance_exists=False,
        intermediate_observation_exists=False,
        reconciliation_state=(
            ReconciliationState.UNAVAILABLE_DUE_TO_MISSING_FACTS.value
        ),
        reconciliation_ran=False,
        reconciliation_status=(
            ReconciliationStatus.UNAVAILABLE_DUE_TO_MISSING_FACTS.value
        ),
        reconciliation_source_stage="facts_validation",
        reconciliation_before=None,
        reconciliation_after=None,
        reconciliation_delta_before=None,
        reconciliation_delta_after=None,
        supplementary_visual_status=SupplementaryVisualStatus.NOT_RUN.value,
        processing_failure_count=0,
    )
    review_rows = []
    unsupported_rows = []
    if terminal_kind is PhaseATerminalDisposition.UNSUPPORTED:
        unsupported_rows.append({
            "reason_code": canonical,
            "reason": canonical,
        })
    else:
        review_rows.append({
            "reason_code": canonical,
            "reason_codes": [canonical],
        })
    result = _safe_failed_processor_result({
        "summary": {
            "files_total": 1,
            "files_unique": 1,
            "files_processed": 0,
            "processing_failures": 0,
        },
        "manual_review_rows": review_rows,
        "unsupported_files": unsupported_rows,
    }, disposition)
    result["gate_failure_reason"] = canonical
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(result, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return result


def _result_exportable(result: Mapping[str, Any]) -> bool:
    invoices = [item for item in result.get("all_invoices") or [] if isinstance(item, Mapping)]
    return bool(invoices) and all(
        bool((invoice.get("_experiment_readiness") or {}).get("export_allowed"))
        for invoice in invoices
    )


def _finalize_controlled_processor_result(
    payload: Mapping[str, Any], *, result_path: Path,
    normalize_result: Callable[[dict[str, Any]], Any],
    attach_readiness: Callable[[dict[str, Any]], Any],
    assert_provenance: Callable[[Mapping[str, Any]], Any],
) -> dict[str, Any]:
    """Persist a controlled result before its disposition can fail the gate."""

    disposition = _derive_terminal_disposition(payload)
    if disposition.accepted:
        result = {
            **dict(payload),
            "all_invoices": list(payload.get("invoices") or []),
        }
        normalize_result(result)
        attach_readiness(result)
        assert_provenance(result)
        disposition = SafeTerminalDisposition(
            **{
                **disposition.__dict__,
                "exportable": _result_exportable(result),
            }
        )
        result["phase_a_terminal_disposition"] = disposition.to_dict()
    else:
        result = _safe_failed_processor_result(payload, disposition)

    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(result, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return result


def _assert_controlled_result_provenance(result: Mapping[str, Any]) -> None:
    """Reject a produced row that lost all immutable source provenance."""

    for invoice in result.get("all_invoices") or []:
        for row in invoice.get("rows") or []:
            if not isinstance(row, Mapping):
                raise RuntimeError("controlled_row_schema_invalid")
            meta = row.get("_meta") if isinstance(row.get("_meta"), Mapping) else {}
            facts = (
                meta.get("document_facts")
                if isinstance(meta.get("document_facts"), Mapping)
                else {}
            )
            source_text = (
                meta.get("source_text")
                if isinstance(meta.get("source_text"), Mapping)
                else {}
            )
            if not (
                facts.get("evidence")
                or any(
                    isinstance(item, Mapping) and item.get("evidence")
                    for item in facts.get("line_items") or []
                )
                or source_text.get("raw_description")
            ):
                raise RuntimeError("controlled_source_provenance_missing")


def _spend_reservation_ids(controller: ExperimentSpendController) -> set[str]:
    payload = json.loads(controller.path.read_text(encoding="utf-8"))
    return set((payload.get("reservations") or {}).keys())


def _controlled_gate_audit(
    controller: ControlledExternalController,
    *,
    event_offset: int,
    run_reservation_ids: set[str],
    spend_controller: ExperimentSpendController,
) -> dict[str, Any]:
    """Summarize only this gate's safe dispatch evidence and fail closed."""

    events: list[dict[str, Any]] = []
    if controller.telemetry_path.exists():
        with controller.telemetry_path.open("rb") as handle:
            handle.seek(max(0, int(event_offset)))
            for raw in handle.read().decode("utf-8", "replace").splitlines():
                try:
                    event = json.loads(raw)
                except (TypeError, ValueError, json.JSONDecodeError):
                    events.append({"event": "invalid_controlled_telemetry"})
                    continue
                if isinstance(event, dict):
                    events.append(event)

    spend = json.loads(spend_controller.path.read_text(encoding="utf-8"))
    run_reservations = [
        value for key, value in (spend.get("reservations") or {}).items()
        if key in run_reservation_ids
    ]
    authorized = [row for row in events if row.get("event") == "dispatch_authorized"]
    blocked = [
        row for row in events
        if row.get("event") in {
            "dispatch_blocked", "controlled_provider_route_blocked",
        }
    ]
    provider_counts = Counter(str(row.get("provider") or "unknown") for row in run_reservations)
    hosts = sorted({str(row.get("host") or "") for row in authorized if row.get("host")})
    document_hashes = {
        str(row.get("document_sha256") or "")
        for row in (*authorized, *run_reservations)
        if row.get("document_sha256")
    }
    failures: list[str] = []
    if blocked:
        failures.append("unauthorized_dispatch_attempt")
    if any(provider != "gemini" for provider in provider_counts):
        failures.append("non_gemini_provider_reservation")
    if any(str(row.get("provider") or "") != "gemini" for row in authorized):
        failures.append("non_gemini_dispatch_authorized")
    if any(host != "generativelanguage.googleapis.com" for host in hosts):
        failures.append("unauthorized_provider_host")
    if any(digest not in controller.allowed_document_hashes for digest in document_hashes):
        failures.append("document_outside_frozen_manifest")
    if any(str(row.get("status") or "") != "settled" for row in run_reservations):
        failures.append("provider_call_not_settled_successfully")
    if any(not row.get("provider_reported_usage") for row in run_reservations):
        failures.append("provider_usage_not_recorded")
    if any(row.get("charged_cost_usd") in (None, "") for row in run_reservations):
        failures.append("provider_cost_not_recorded")
    if any(row.get("event") == "invalid_controlled_telemetry" for row in events):
        failures.append("controlled_telemetry_invalid")
    return {
        "passed": not failures,
        "failure_codes": sorted(set(failures)),
        "authorized_hostnames": hosts,
        "authorized_dispatches": len(authorized),
        "blocked_dispatches": len(blocked),
        "provider_reservations": dict(sorted(provider_counts.items())),
        "manifest_document_hashes_verified": len(document_hashes),
        "usage_recorded_calls": sum(bool(row.get("provider_reported_usage")) for row in run_reservations),
        "cost_recorded_calls": sum(row.get("charged_cost_usd") not in (None, "") for row in run_reservations),
    }


def _finalize_controlled_gate_metrics(metrics: dict[str, Any]) -> None:
    """Apply gate failures after result/disposition persistence is complete."""

    audit = dict(metrics.get("controlled_gate_audit") or {})
    failures = list(audit.get("failure_codes") or [])
    if int(metrics.get("processing_failures") or 0):
        failures.append("processing_failure")
    if int(metrics.get("schema_invalid_count") or 0):
        failures.append("structured_schema_invalid")
    if int(metrics.get("blocked_network_attempts") or 0):
        failures.append("unauthorized_or_invalid_dispatch_blocked")
    if (
        int(metrics.get("document_facts_document_count") or 0) > 0
        and float(metrics.get("provenance_coverage") or 0.0) < 1.0
    ):
        failures.append("source_provenance_lost")
    if float(metrics.get("false_safe_export_rate") or 0.0) > 0.0:
        failures.append("false_safe_export_detected")
    if Decimal(str(metrics.get("phase_a_cumulative_charged_usd") or "0")) > Decimal("10"):
        failures.append("phase_a_budget_exceeded")
    if int(metrics.get("recorded_disposition_count") or 0) < int(
        metrics.get("documents_attempted") or 0
    ):
        failures.append("document_without_recorded_disposition")
    audit["failure_codes"] = sorted(set(failures))
    audit["passed"] = not audit["failure_codes"]
    metrics["controlled_gate_audit"] = audit


def _evaluate_frozen_baseline(
    *, frozen_path: Path, expected_frozen_sha256: str, split_root: Path,
    controller: ExperimentSpendController, wall_seconds: float,
    isolated_runtime: Path | None = None,
    run_reservation_ids: set[str] | None = None,
    controlled_audit: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if _sha256_file(frozen_path) != expected_frozen_sha256:
        raise RuntimeError("frozen baseline changed before evaluation")
    frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
    labels: list[dict[str, Any]] = []
    for path in (
        split_root / "scopes" / "training_labels.jsonl",
        split_root / "scopes" / "benchmark_only_labels.jsonl",
        split_root / "scopes" / "rule_simulation_labels.jsonl",
        split_root / "hidden" / "holdout_labels.jsonl",
    ):
        labels.extend(_read_jsonl(path))
    source_map = frozen["source_map"]
    document_by_unit = {
        str(value["unit_id"]): str(value["document_id"])
        for value in source_map.values()
    }
    invoices_by_document: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unsupported_by_document: Counter[str] = Counter()
    terminal_dispositions: list[dict[str, Any]] = []
    terminal_reason_sets: list[set[str]] = []
    for result in frozen.get("shard_results") or []:
        terminal = result.get("phase_a_terminal_disposition")
        if isinstance(terminal, dict):
            terminal_dispositions.append(terminal)
            reason_values: list[Any] = [terminal.get("sanitized_failure_code")]
            for review in result.get("manual_review_rows") or []:
                if not isinstance(review, Mapping):
                    continue
                reason_values.extend((review.get("reason_code"), review.get("reason")))
                reason_values.extend(review.get("reason_codes") or [])
            terminal_reason_sets.append(set(
                _canonical_review_codes(reason_values)
                if any(value not in (None, "", []) for value in reason_values)
                else []
            ))
        for invoice in result.get("all_invoices") or []:
            filename = Path(str(invoice.get("source_file") or "")).name
            mapped = source_map.get(filename)
            if mapped:
                invoices_by_document[str(mapped["document_id"])].append(invoice)
        for item in result.get("unsupported_files") or []:
            filename = Path(str(item.get("filename") or "")).name
            mapped = source_map.get(filename)
            if mapped:
                unsupported_by_document[str(mapped["document_id"])] += 1
    terminal_counts = Counter(
        str(item.get("disposition") or "") for item in terminal_dispositions
    )
    terminal_processing_failures = sum(
        max(0, int(item.get("processing_failure_count") or 0))
        for item in terminal_dispositions
    )
    terminal_quality = _terminal_quality_metrics(
        terminal_dispositions, terminal_reason_sets,
    )

    observed_invoices = [
        invoice
        for candidates in invoices_by_document.values()
        for invoice in candidates
    ]
    observed_rows = [
        row
        for invoice in observed_invoices
        for row in (invoice.get("rows") or [])
        if isinstance(row, dict)
    ]
    observed_facts_count = 0
    observed_provenance_count = 0
    for row in observed_rows:
        meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
        facts = meta.get("document_facts") if isinstance(meta.get("document_facts"), dict) else {}
        observed_facts_count += bool(facts)
        observed_provenance_count += bool(
            facts.get("evidence")
            or any(
                isinstance(item, dict) and item.get("evidence")
                for item in facts.get("line_items") or []
            )
            or (meta.get("source_text") or {}).get("raw_description")
        )
    observed_reconciled = sum(
        _rows_reconcile(list(invoice.get("rows") or []))
        for invoice in observed_invoices
    )
    observed_field_total = len(observed_invoices) * 5
    observed_field_present = 0
    for invoice in observed_invoices:
        rows = list(invoice.get("rows") or [])
        first = rows[0] if rows else {}
        observed_field_present += sum(value not in (None, "", []) for value in (
            first.get("Vendor"), first.get("Invoice Number"),
            first.get("Invoice Date"), first.get("Invoice Total"), rows,
        ))
    readiness_distribution = Counter()
    for invoice in observed_invoices:
        readiness = dict(invoice.get("_experiment_readiness") or {})
        status = str(readiness.get("status") or "").strip() or (
            "ready" if readiness.get("export_allowed") else "blocked"
        )
        readiness_distribution[status] += 1

    invoice_metrics: list[dict[str, Any]] = []
    line_count = 0
    facts_count = 0
    provenance_count = 0
    missing_gl_count = 0
    review_count = 0
    false_safe_count = 0
    correct_abstention_count = 0
    extraction_field_total = 0
    extraction_field_present = 0
    reconciliation_count = 0
    selected_unit_ids = set(document_by_unit)
    for label in labels:
        unit_id = str(label["unit_id"])
        if unit_id not in selected_unit_ids:
            continue
        document_id = document_by_unit.get(unit_id)
        ground_truth = dict(label.get("ground_truth") or {})
        expected = {
            str(ground_truth.get("expected_gl") or "").strip(),
            *(str(value).strip() for value in ground_truth.get("acceptable_gl_alternatives") or []),
        } - {""}
        candidates = list(invoices_by_document.get(str(document_id), []))
        invoice = _match_labeled_invoice(candidates, ground_truth)
        if invoice is None:
            invoice_metrics.append({
                "unit_id": unit_id, "found": False, "top1": False,
                "top3": False, "mrr": 0.0, "export_allowed": False,
            })
            correct_abstention_count += 1
            continue
        rows = list(invoice.get("rows") or [])
        readiness = dict(invoice.get("_experiment_readiness") or {})
        export_allowed = bool(readiness.get("export_allowed"))
        row_ranks: list[int | None] = []
        row_correct: list[bool] = []
        row_top3: list[bool] = []
        for row in rows:
            line_count += 1
            meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
            facts = meta.get("document_facts") if isinstance(meta.get("document_facts"), dict) else {}
            facts_count += bool(facts)
            provenance_count += bool(
                facts.get("evidence")
                or any(
                    isinstance(item, dict) and item.get("evidence")
                    for item in facts.get("line_items") or []
                )
                or (meta.get("source_text") or {}).get("raw_description")
            )
            selected = str(row.get("GL Account") or "").strip()
            missing_gl_count += not bool(selected)
            row_correct.append(selected in expected)
            decision = meta.get("accounting_decision") if isinstance(meta.get("accounting_decision"), dict) else {}
            ranked = [
                str(item.get("gl_code") or "").strip()
                for item in decision.get("candidates_ranked") or []
                if isinstance(item, dict)
            ]
            rank = next((index for index, value in enumerate(ranked, 1) if value in expected), None)
            row_ranks.append(rank)
            row_top3.append(rank is not None and rank <= 3)
        first = rows[0] if rows else {}
        fields = [
            first.get("Vendor"), first.get("Invoice Number"), first.get("Invoice Date"),
            first.get("Invoice Total"), rows,
        ]
        extraction_field_total += len(fields)
        extraction_field_present += sum(value not in (None, "", []) for value in fields)
        reconciled = _rows_reconcile(rows)
        reconciliation_count += reconciled
        expected_property = str(ground_truth.get("expected_property") or "").strip()
        unsafe = (
            not rows
            or not all(row_correct)
            or not reconciled
            or bool(expected_property) and any(
                str(row.get("Property") or "").strip() != expected_property for row in rows
            )
            or bool(unsupported_by_document.get(str(document_id)))
        )
        if unsafe and export_allowed:
            false_safe_count += 1
        if unsafe and not export_allowed:
            correct_abstention_count += 1
        review_count += not export_allowed
        invoice_metrics.append({
            "unit_id": unit_id,
            "found": True,
            "top1": bool(rows) and all(row_correct),
            "top3": bool(rows) and all(row_top3),
            "mrr": (
                sum(1.0 / rank if rank else 0.0 for rank in row_ranks) / len(row_ranks)
                if row_ranks else 0.0
            ),
            "export_allowed": export_allowed,
            "unsafe": unsafe,
        })

    spend_state = json.loads(controller.path.read_text(encoding="utf-8"))
    all_reservations_map = dict(spend_state.get("reservations") or {})
    reservations = [
        value for key, value in all_reservations_map.items()
        if run_reservation_ids is None or key in run_reservation_ids
    ]
    cumulative_reservations = list(all_reservations_map.values())
    openai_cost = sum((
        _decimal(row.get("charged_cost_usd")) for row in reservations
        if str(row.get("provider") or "").casefold() == "openai"
    ), Decimal("0"))
    other_cost = sum((
        _decimal(row.get("charged_cost_usd")) for row in reservations
        if str(row.get("provider") or "").casefold() != "openai"
    ), Decimal("0"))
    estimated = sum(
        (_decimal(row.get("estimated_cost_usd")) for row in reservations),
        Decimal("0"),
    )
    actual_or_conservative = sum(
        (_decimal(row.get("charged_cost_usd")) for row in reservations),
        Decimal("0"),
    )
    cumulative_charged = sum(
        (_decimal(row.get("charged_cost_usd")) for row in cumulative_reservations),
        Decimal("0"),
    )
    successes = [row for row in invoice_metrics if row["found"]]
    top1_successes = sum(bool(row["top1"]) for row in invoice_metrics)
    trace_metrics = _local_trace_metrics(isolated_runtime)
    structured_total = (
        int(trace_metrics.get("schema_valid_count") or 0)
        + int(trace_metrics.get("schema_invalid_count") or 0)
    )
    return {
        "schema_version": RUN_CONTRACT_VERSION,
        "wall_seconds": round(wall_seconds, 3),
        "labeled_invoice_units": len(invoice_metrics),
        "matched_invoice_units": len(successes),
        "processing_failures": (
            len(frozen.get("shard_failures") or []) + terminal_processing_failures
        ),
        "provider_calls": len(reservations),
        **trace_metrics,
        "structured_response_validity_rate": _ratio(
            int(trace_metrics.get("schema_valid_count") or 0), structured_total,
        ),
        "provider_reported_usage_calls": sum(
            bool(row.get("provider_reported_usage")) for row in reservations
        ),
        "openai_cost_usd": str(openai_cost.quantize(Decimal("0.000001"))),
        "other_provider_cost_usd": str(other_cost.quantize(Decimal("0.000001"))),
        "estimated_total_cost_usd": str(estimated.quantize(Decimal("0.000001"))),
        "charged_total_cost_usd": str(actual_or_conservative.quantize(Decimal("0.000001"))),
        "phase_a_cumulative_charged_usd": str(cumulative_charged.quantize(Decimal("0.000001"))),
        "top1_accuracy": _ratio(top1_successes, len(invoice_metrics)),
        "top1_wilson_95": _wilson_interval(top1_successes, len(invoice_metrics)),
        "top3_recall": _ratio(sum(bool(row["top3"]) for row in invoice_metrics), len(invoice_metrics)),
        "mrr": round(
            sum(float(row["mrr"]) for row in invoice_metrics) / len(invoice_metrics), 6
        ) if invoice_metrics else None,
        "missing_gl_rate": _ratio(missing_gl_count, line_count),
        "review_rate": _ratio(review_count, len(invoice_metrics)),
        "false_safe_export_rate": _ratio(false_safe_count, len(invoice_metrics)),
        "false_safe_export_count": false_safe_count,
        "correct_abstention_rate": _ratio(correct_abstention_count, len(invoice_metrics)),
        "documents_attempted": len(source_map),
        "documents_accepted": len(invoices_by_document),
        "invoices_detected": len(observed_invoices),
        "extraction_completeness": _ratio(observed_field_present, observed_field_total),
        "reconciliation_rate": _ratio(observed_reconciled, len(observed_invoices)),
        **terminal_quality,
        "row_document_facts_coverage": _ratio(
            observed_facts_count, len(observed_rows),
        ),
        "row_provenance_coverage": _ratio(
            observed_provenance_count, len(observed_rows),
        ),
        "line_count": len(observed_rows),
        "unsupported_document_count": (
            sum(unsupported_by_document.values())
            + terminal_counts[PhaseATerminalDisposition.UNSUPPORTED.value]
        ),
        "review_required_document_count": terminal_counts[
            PhaseATerminalDisposition.REVIEW_REQUIRED.value
        ],
        "blocked_document_count": terminal_counts[
            PhaseATerminalDisposition.BLOCKED.value
        ],
        "terminal_disposition_count": len(terminal_dispositions),
        "recorded_disposition_count": max(
            len(terminal_dispositions),
            len(invoices_by_document) + sum(unsupported_by_document.values()),
        ),
        "readiness_distribution": dict(readiness_distribution),
        "controlled_gate_audit": dict(controlled_audit or {}),
        "private_invoice_metrics": invoice_metrics,
    }


def _local_trace_metrics(runtime_root: Path | None) -> dict[str, Any]:
    defaults = {
        "local_provider_calls": 0,
        "remote_provider_calls": 0,
        "blocked_network_attempts": 0,
        "local_latency_p50_ms": None,
        "local_latency_p95_ms": None,
        "local_latency_p99_ms": None,
        "local_peak_concurrency": 0,
        "local_cache_hits": 0,
        "local_media_bytes": 0,
        "local_media_pixels": 0,
        "remote_latency_p50_ms": None,
        "remote_latency_p95_ms": None,
        "remote_peak_concurrency": 0,
        "remote_media_bytes": 0,
        "remote_media_pixels": 0,
        "provider_attempt_counts": {},
        "schema_valid_count": 0,
        "schema_invalid_count": 0,
        "supplementary_verification_count": 0,
        "supplementary_unresolved_target_count": 0,
        "supplementary_unresolved_target_rate": None,
    }
    if runtime_root is None or not runtime_root.exists():
        return defaults
    attempts: list[dict[str, Any]] = []
    blocked = 0
    cache_hits = 0
    schema_results: Counter[str] = Counter()
    supplementary_verifications = 0
    supplementary_unresolved = 0
    for path in runtime_root.glob("batches/*/audit/ai_request_trace.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if event.get("event") == "provider_attempt":
                attempts.append(event)
            elif event.get("event") == "network_dispatch_blocked":
                blocked += 1
            elif event.get("event") == "cache" and event.get("cache_status") == "hit":
                cache_hits += 1
            elif event.get("event") == "schema_validation":
                schema_results[str(event.get("schema_result") or "unknown")] += 1
            elif event.get("event") == "supplementary_verification":
                supplementary_verifications += 1
                supplementary_unresolved += event.get("resolved") is not True
    local = [row for row in attempts if row.get("provider") == "local_ollama"]
    remote = [row for row in attempts if row.get("provider") != "local_ollama"]
    latencies = sorted(float(row.get("elapsed_ms") or 0.0) for row in local)
    remote_latencies = sorted(float(row.get("elapsed_ms") or 0.0) for row in remote)
    provider_attempt_counts = Counter(str(row.get("provider") or "unknown") for row in attempts)
    return {
        "local_provider_calls": len(local),
        "remote_provider_calls": len(remote),
        "blocked_network_attempts": blocked,
        "local_latency_p50_ms": _percentile(latencies, 0.50),
        "local_latency_p95_ms": _percentile(latencies, 0.95),
        "local_latency_p99_ms": _percentile(latencies, 0.99),
        "local_peak_concurrency": max(
            (int(row.get("provider_peak_concurrency") or 0) for row in local),
            default=0,
        ),
        "local_cache_hits": cache_hits,
        "local_media_bytes": sum(int(row.get("media_bytes") or 0) for row in local),
        "local_media_pixels": sum(int(row.get("media_pixels") or 0) for row in local),
        "remote_latency_p50_ms": _percentile(remote_latencies, 0.50),
        "remote_latency_p95_ms": _percentile(remote_latencies, 0.95),
        "remote_peak_concurrency": max(
            (int(row.get("provider_peak_concurrency") or 0) for row in remote),
            default=0,
        ),
        "remote_media_bytes": sum(int(row.get("media_bytes") or 0) for row in remote),
        "remote_media_pixels": sum(int(row.get("media_pixels") or 0) for row in remote),
        "provider_attempt_counts": dict(sorted(provider_attempt_counts.items())),
        "schema_valid_count": int(schema_results.get("valid", 0)),
        "schema_invalid_count": sum(
            count for result, count in schema_results.items()
            if result not in {"valid", "escalated"}
        ),
        "supplementary_verification_count": supplementary_verifications,
        "supplementary_unresolved_target_count": supplementary_unresolved,
        "supplementary_unresolved_target_rate": _ratio(
            supplementary_unresolved, supplementary_verifications,
        ),
    }


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    index = min(len(values) - 1, max(0, math.ceil(len(values) * fraction) - 1))
    return round(float(values[index]), 3)


def _match_labeled_invoice(
    invoices: Sequence[Mapping[str, Any]], ground_truth: Mapping[str, Any],
) -> dict[str, Any] | None:
    expected_number = _normalize_identity(ground_truth.get("observed_invoice_number"))
    expected_total = _decimal_or_none(ground_truth.get("observed_invoice_total"))
    matches: list[dict[str, Any]] = []
    for raw in invoices:
        invoice = dict(raw)
        rows = list(invoice.get("rows") or [])
        first = rows[0] if rows else {}
        number = _normalize_identity(
            first.get("Invoice Number") or invoice.get("invoice_number")
        )
        total = _decimal_or_none(first.get("Invoice Total") or invoice.get("total_amount"))
        if expected_number and number == expected_number:
            matches.append(invoice)
        elif expected_total is not None and total == expected_total:
            matches.append(invoice)
    return matches[0] if len(matches) == 1 else None


def _rows_reconcile(rows: Sequence[Mapping[str, Any]]) -> bool:
    if not rows:
        return False
    if rows[0].get("Invoice Total") in (None, ""):
        return False
    if any(row.get("Line Item Total") in (None, "") for row in rows):
        return False
    try:
        invoice_total = _decimal(rows[0].get("Invoice Total"))
        row_total = sum((_decimal(row.get("Line Item Total")) for row in rows), Decimal("0"))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return abs(invoice_total - row_total) <= Decimal("0.02")


def _git_safe_phase_a_summary(
    metrics: Mapping[str, Any], assignments: Sequence[Mapping[str, Any]],
    shard_failures: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": "document-learning-phase-a-safe/1.0",
        "classification": "INSUFFICIENT EVIDENCE",
        "selected_documents": len(assignments),
        "authoritatively_labeled_documents": sum(
            item.get("evaluation_scope") == "evidence_backed" for item in assignments
        ),
        "coverage_only_unlabeled_documents": sum(
            item.get("evaluation_scope") == "coverage_only_unlabeled" for item in assignments
        ),
        "provider_calls": int(metrics["provider_calls"]),
        "local_provider_calls": int(metrics.get("local_provider_calls") or 0),
        "remote_provider_calls": int(metrics.get("remote_provider_calls") or 0),
        "blocked_network_attempts": int(metrics.get("blocked_network_attempts") or 0),
        "local_latency_p50_ms": metrics.get("local_latency_p50_ms"),
        "local_latency_p95_ms": metrics.get("local_latency_p95_ms"),
        "local_latency_p99_ms": metrics.get("local_latency_p99_ms"),
        "local_peak_concurrency": int(metrics.get("local_peak_concurrency") or 0),
        "local_cache_hits": int(metrics.get("local_cache_hits") or 0),
        "remote_latency_p50_ms": metrics.get("remote_latency_p50_ms"),
        "remote_latency_p95_ms": metrics.get("remote_latency_p95_ms"),
        "remote_peak_concurrency": int(metrics.get("remote_peak_concurrency") or 0),
        "provider_attempt_counts": dict(metrics.get("provider_attempt_counts") or {}),
        "schema_valid_count": int(metrics.get("schema_valid_count") or 0),
        "schema_invalid_count": int(metrics.get("schema_invalid_count") or 0),
        "structured_response_validity_rate": metrics.get(
            "structured_response_validity_rate"
        ),
        "supplementary_verification_count": int(
            metrics.get("supplementary_verification_count") or 0
        ),
        "supplementary_unresolved_target_count": int(
            metrics.get("supplementary_unresolved_target_count") or 0
        ),
        "supplementary_unresolved_target_rate": metrics.get(
            "supplementary_unresolved_target_rate"
        ),
        "provider_reported_usage_calls": int(metrics["provider_reported_usage_calls"]),
        "openai_cost_usd": metrics["openai_cost_usd"],
        "other_provider_cost_usd": metrics["other_provider_cost_usd"],
        "wall_seconds": metrics["wall_seconds"],
        "documents_attempted": int(metrics.get("documents_attempted") or 0),
        "documents_accepted": int(metrics.get("documents_accepted") or 0),
        "invoices_detected": int(metrics.get("invoices_detected") or 0),
        "unsupported_document_count": int(metrics.get("unsupported_document_count") or 0),
        "review_required_document_count": int(
            metrics.get("review_required_document_count") or 0
        ),
        "blocked_document_count": int(metrics.get("blocked_document_count") or 0),
        "terminal_disposition_count": int(metrics.get("terminal_disposition_count") or 0),
        "recorded_disposition_count": int(metrics.get("recorded_disposition_count") or 0),
        "document_facts_document_count": int(
            metrics.get("document_facts_document_count") or 0
        ),
        "transport_valid_observation_count": int(
            metrics.get("transport_valid_observation_count") or 0
        ),
        "transport_valid_observation_rate": metrics.get(
            "transport_valid_observation_rate"
        ),
        "intermediate_unreconciled_observation_count": int(
            metrics.get("intermediate_unreconciled_observation_count") or 0
        ),
        "intermediate_unreconciled_observation_rate": metrics.get(
            "intermediate_unreconciled_observation_rate"
        ),
        "document_facts_coverage": metrics.get("document_facts_coverage"),
        "provenance_document_count": int(metrics.get("provenance_document_count") or 0),
        "provenance_coverage": metrics.get("provenance_coverage"),
        "reconciliation_document_count": int(
            metrics.get("reconciliation_document_count") or 0
        ),
        "reconciliation_coverage": metrics.get("reconciliation_coverage"),
        "reconciliation_status_distribution": dict(
            metrics.get("reconciliation_status_distribution") or {}
        ),
        "reconciliation_state_distribution": dict(
            metrics.get("reconciliation_state_distribution") or {}
        ),
        "canonical_reason_distribution": dict(
            metrics.get("canonical_reason_distribution") or {}
        ),
        "review_required_unresolved_arithmetic_count": int(
            metrics.get("review_required_unresolved_arithmetic_count") or 0
        ),
        "unsupported_unusable_transport_count": int(
            metrics.get("unsupported_unusable_transport_count") or 0
        ),
        "supplementary_attempted_document_count": int(
            metrics.get("supplementary_attempted_document_count") or 0
        ),
        "supplementary_resolved_document_count": int(
            metrics.get("supplementary_resolved_document_count") or 0
        ),
        "supplementary_resolution_rate": metrics.get(
            "supplementary_resolution_rate"
        ),
        "supplementary_contradiction_rate": metrics.get(
            "supplementary_contradiction_rate"
        ),
        "supplementary_limit_rate": metrics.get("supplementary_limit_rate"),
        "phase_a_cumulative_charged_usd": metrics.get("phase_a_cumulative_charged_usd"),
        "readiness_distribution": dict(metrics.get("readiness_distribution") or {}),
        "controlled_gate_passed": bool(
            (metrics.get("controlled_gate_audit") or {}).get("passed", True)
        ),
        "controlled_gate_failure_codes": list(
            (metrics.get("controlled_gate_audit") or {}).get("failure_codes") or []
        ),
        "top1_accuracy": metrics["top1_accuracy"],
        "top1_wilson_95": metrics["top1_wilson_95"],
        "top3_recall": metrics["top3_recall"],
        "mrr": metrics["mrr"],
        "missing_gl_rate": metrics["missing_gl_rate"],
        "false_safe_export_rate": metrics["false_safe_export_rate"],
        "false_safe_export_count": int(metrics.get("false_safe_export_count") or 0),
        "review_rate": metrics["review_rate"],
        "extraction_completeness": metrics["extraction_completeness"],
        "reconciliation_rate": metrics["reconciliation_rate"],
        "processing_failure_shards": len(shard_failures),
        "processing_failure_count": int(metrics.get("processing_failures") or 0),
        "private_artifacts_tracked": 0,
        "phase_b_started": False,
        "post_learning_metrics_available": False,
        "go_no_go": "NO_GO_REQUIRES_MORE_EVIDENCE_BACKED_LABELS",
    }


def _official_runtime_fingerprint(root: Path) -> str:
    watched = (
        root / "accounting_knowledge_core",
        root / "human_adjudication",
        root / "tenant_accounting",
        root / "resman_context",
    )
    digest = hashlib.sha256()
    for directory in watched:
        if not directory.exists():
            continue
        for path in sorted(item for item in directory.rglob("*") if item.is_file()):
            stat = path.stat()
            digest.update(str(path.relative_to(root)).encode("utf-8"))
            digest.update(str(stat.st_size).encode("ascii"))
            digest.update(str(stat.st_mtime_ns).encode("ascii"))
    return digest.hexdigest()


def _write_immutable_json(path: Path, value: Any) -> None:
    text = json.dumps(value, indent=2, sort_keys=True, default=str) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") != text:
        raise RuntimeError("immutable Phase A artifact differs")
    path.write_text(text, encoding="utf-8")


def _write_private_html(path: Path, metrics: Mapping[str, Any]) -> None:
    import html
    safe_rows = "".join(
        f"<tr><th>{html.escape(str(key))}</th><td>{html.escape(str(value))}</td></tr>"
        for key, value in metrics.items() if key != "private_invoice_metrics"
    )
    path.write_text(
        "<!doctype html><meta charset='utf-8'><title>Private Phase A</title>"
        "<h1>Private Phase A calibration</h1><table>" + safe_rows + "</table>",
        encoding="utf-8",
    )


def _chunks(values: Sequence[Mapping[str, Any]], size: int):
    for index in range(0, len(values), size):
        yield list(values[index:index + size])


def _safe_failure_code(exc: Exception) -> str:
    value = type(exc).__name__.casefold()
    return "".join(character if character.isalnum() else "_" for character in value).strip("_")


def _normalize_identity(value: Any) -> str:
    return "".join(character for character in str(value or "").casefold() if character.isalnum())


def _decimal(value: Any) -> Decimal:
    text = str(value if value is not None else "").replace(",", "").replace("$", "").strip()
    if not text:
        return Decimal("0")
    result = Decimal(text)
    if not result.is_finite():
        raise InvalidOperation
    return result


def _decimal_or_none(value: Any) -> Decimal | None:
    try:
        return _decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return None


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _wilson_interval(successes: int, total: int) -> list[float] | None:
    if total <= 0:
        return None
    z = 1.959963984540054
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denominator
    return [round(max(0.0, center - margin), 6), round(min(1.0, center + margin), 6)]


__all__ = ["PhaseABaselineResult", "run_phase_a_baseline"]
