"""Historical synthetic preflight for the superseded Gemini + DeepSeek plan.

This is not the current Gemini-only controlled-external entry point and is not
imported by production. It is retained solely to reproduce the original
non-private preflight facts; current guards reject its DeepSeek branch.

The script never opens a source document, locator file, label file or holdout
answer. Provider response bodies remain in memory and are not persisted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SYNTHETIC_PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/"
    "x8AAusB9Y9Zl1sAAAAASUVORK5CYII="
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Synthetic-only controlled external provider preflight"
    )
    parser.add_argument(
        "--experiment-root", type=Path,
        default=PROJECT_ROOT / "tmp" / "document-learning-simulation",
    )
    parser.add_argument("--experiment-id", default="exp-document-learning-simulation")
    args = parser.parse_args()

    root = args.experiment_root.resolve(strict=True)
    manifest = _exactly_one((root / "calibration").glob("phase-a-*/calibration_manifest.json"))
    inventory = _exactly_one((root / "snapshots").glob("corpus-*/inventory.jsonl"))

    # Import settings only to load private environment configuration. No
    # credential value is printed, serialized, or attached to the result.
    from webapp.backend import settings  # noqa: F401
    from webapp.backend.services import ai_provider, ai_runtime_trace
    from webapp.backend.services.controlled_external_experiment import (
        ControlledExternalBlocked,
        ControlledExternalController,
        activate_controlled_external,
        assert_controlled_external_dispatch_allowed,
        build_deepseek_minimized_facts,
        controlled_document_scope,
    )
    from webapp.backend.services.experiment_spend_controller import (
        ExperimentSpendController,
        SpendAuthorizationError,
        activate_experiment_spend_gate,
    )
    from webapp.backend.services.provider_capabilities import (
        ModelProfileRole,
        ProfileLoader,
    )

    os.environ["INNER_VIEW_EXPERIMENT_EXECUTION_MODE"] = "CONTROLLED_EXTERNAL"
    controller = ControlledExternalController(
        private_root=root,
        experiment_id=args.experiment_id,
        manifest_path=manifest,
        inventory_path=inventory,
    )
    spend = ExperimentSpendController(root, args.experiment_id)
    profiles = ProfileLoader().load()
    gemini = _profile(
        profiles, provider="gemini", role=ModelProfileRole.MULTIMODAL_EXTRACTION
    )
    deepseek = _profile(
        profiles, provider="deepseek", role=ModelProfileRole.ACCOUNTING_REASONING
    )
    synthetic_digest = hashlib.sha256(b"innerview-controlled-external-preflight-v1").hexdigest()
    results: dict[str, Any] = {
        "contract_version": "controlled-external-preflight/1.0",
        "private_documents_transmitted": 0,
        "provider_responses_persisted": 0,
        "manifest_document_count": len(controller.allowed_document_hashes),
        "manifest_hash_verified": True,
        "proofs": {},
    }

    with activate_controlled_external(controller), controlled_document_scope(
        document_sha256=synthetic_digest, synthetic=True,
    ), activate_experiment_spend_gate(
        spend, phase="A", pricing_version="configured-private-rate-card/2026-07-18",
    ):
        results["proofs"]["gemini_synthetic_call"] = _gemini_call(
            gemini, ai_provider, ai_runtime_trace
        )
        results["proofs"]["deepseek_synthetic_call"] = _deepseek_call(
            deepseek, ai_provider, ai_runtime_trace,
            build_deepseek_minimized_facts,
        )
        results["proofs"].update(_local_boundary_proofs(
            controller=controller,
            digest=synthetic_digest,
            ai_runtime_trace=ai_runtime_trace,
            assert_dispatch=assert_controlled_external_dispatch_allowed,
            build_minimized=build_deepseek_minimized_facts,
            blocked_type=ControlledExternalBlocked,
        ))

    results["proofs"].update(_spend_proofs(
        root=root,
        controller_type=ExperimentSpendController,
        blocked_type=SpendAuthorizationError,
    ))
    results["proofs"].update(_authority_proofs())
    results["proofs"]["private_artifacts_outside_git"] = _is_ignored(root)
    snapshot = spend.snapshot("A")
    results["combined_spend"] = {
        "cap_usd": snapshot.current_phase_cap_usd,
        "charged_usd": snapshot.cumulative_charged_usd,
        "reserved_usd": snapshot.active_reserved_usd,
        "alerts_emitted": snapshot.alerts_emitted,
        "by_provider_profile": snapshot.by_provider_profile,
    }
    all_passed = all(
        bool(value.get("passed")) if isinstance(value, dict) else bool(value)
        for value in results["proofs"].values()
    )
    results["status"] = (
        "synthetic_preflight_passed_private_dispatch_not_authorized"
        if all_passed else "synthetic_preflight_failed"
    )
    output = root / "preflight" / "controlled_external_preflight_summary.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(results, indent=2, sort_keys=True))
    return 0 if all_passed else 1


def _profile(profiles, *, provider: str, role):
    eligible = [
        item for item in profiles
        if item.provider == provider and item.role is role and item.enabled
        and item.credentials_present and item.model_id and item.base_url
        and item.input_cost_usd_per_million is not None
        and item.output_cost_usd_per_million is not None
    ]
    if not eligible:
        raise RuntimeError(f"{provider}_{role.value}_profile_unavailable")
    return min(eligible, key=lambda item: (
        float(item.input_cost_usd_per_million)
        + float(item.output_cost_usd_per_million),
        item.routing_priority,
        item.profile_id,
    ))


def _estimate(profile, payload: dict[str, Any], output_tokens: int) -> float:
    input_tokens = max(1, len(json.dumps(payload, separators=(",", ":"))) // 4)
    return max(0.000001, round(
        input_tokens * float(profile.input_cost_usd_per_million) / 1_000_000
        + output_tokens * float(profile.output_cost_usd_per_million) / 1_000_000,
        6,
    ))


def _gemini_call(profile, ai_provider, trace) -> dict[str, Any]:
    payload = {
        "model": profile.model_id,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": "Return strict JSON with observable visual facts only."},
            {"role": "user", "content": [
                {"type": "text", "text": (
                    "This is a synthetic one-pixel image. Return "
                    '{"synthetic":true,"visual_input_received":true}.'
                )},
                {"type": "image_url", "image_url": {"url": SYNTHETIC_PNG}},
            ]},
        ],
        **ai_provider._completion_controls(profile.provider, 128),
    }
    estimate = _estimate(profile, payload, 128)
    try:
        with trace.operation(
            batch_id="", stage="vision", provider=profile.provider,
            model=profile.model_id, profile_id=profile.profile_id,
        ):
            trace.update_context(
                estimated_cost_usd=estimate,
                input_cost_usd_per_million=float(profile.input_cost_usd_per_million),
                output_cost_usd_per_million=float(profile.output_cost_usd_per_million),
                media_bytes=68, media_pixels=1,
            )
            text = ai_provider._send_chat_completion(
                provider=profile.provider,
                payload=payload,
                vision=True,
                api_key_override=profile.api_key.get_secret_value(),
                base_url_override=profile.base_url,
                timeout_seconds_override=profile.timeout_seconds,
                max_attempts_override=1,
            )
            parsed = ai_provider._extract_json_object(text)
        return {
            "passed": isinstance(parsed, dict), "provider": "gemini",
            "profile_id": profile.profile_id, "model_id": profile.model_id,
            "estimated_cost_usd": estimate,
        }
    except ai_provider.AIProviderError as exc:
        return {
            "passed": False, "provider": "gemini", "profile_id": profile.profile_id,
            "model_id": profile.model_id, "failure_code": exc.failure_code,
        }


def _deepseek_call(profile, ai_provider, trace, build_minimized) -> dict[str, Any]:
    facts = build_minimized(lines=[{
        "source_text": "synthetic monthly network access",
        "quantity": "1", "amount": "10.00",
        "current_semantics": {"line_family": "unknown", "work_mode": "unknown"},
    }])
    user = {
        "task": "candidate_only_semantic_classification",
        "derived_facts": facts,
        "response_schema": {
            "line_family": "string", "trade_family": "string",
            "work_mode": "string", "confidence": "0..1",
        },
    }
    payload = {
        "model": profile.model_id,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": (
                "Return strict JSON candidate-only semantic support. Do not select a final account."
            )},
            {"role": "user", "content": json.dumps(user)},
        ],
        **ai_provider._completion_controls(profile.provider, 128),
    }
    estimate = _estimate(profile, payload, 128)
    try:
        with trace.operation(
            batch_id="", stage="accounting_semantic_reasoning",
            provider=profile.provider, model=profile.model_id,
            profile_id=profile.profile_id,
        ):
            trace.update_context(
                estimated_cost_usd=estimate,
                input_cost_usd_per_million=float(profile.input_cost_usd_per_million),
                output_cost_usd_per_million=float(profile.output_cost_usd_per_million),
            )
            text = ai_provider._send_chat_completion(
                provider=profile.provider,
                payload=payload,
                api_key_override=profile.api_key.get_secret_value(),
                base_url_override=profile.base_url,
                timeout_seconds_override=profile.timeout_seconds,
                max_attempts_override=1,
            )
            parsed = ai_provider._extract_json_object(text)
        return {
            "passed": isinstance(parsed, dict), "provider": "deepseek",
            "profile_id": profile.profile_id, "model_id": profile.model_id,
            "estimated_cost_usd": estimate,
        }
    except ai_provider.AIProviderError as exc:
        return {
            "passed": False, "provider": "deepseek", "profile_id": profile.profile_id,
            "model_id": profile.model_id, "failure_code": exc.failure_code,
        }


def _local_boundary_proofs(
    *, controller, digest, ai_runtime_trace, assert_dispatch, build_minimized,
    blocked_type,
) -> dict[str, bool]:
    from webapp.backend.services.controlled_external_experiment import (
        controlled_document_scope,
    )
    proofs: dict[str, bool] = {}
    payload = {
        "model": "synthetic",
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": "{}"}],
    }
    try:
        with ai_runtime_trace.operation(
            batch_id="", stage="synthetic", provider="gemini",
            model="synthetic", profile_id="synthetic",
        ):
            ai_runtime_trace.update_context(
                estimated_cost_usd=0.001,
                input_cost_usd_per_million=1.0,
                output_cost_usd_per_million=1.0,
            )
            assert_dispatch(
                provider="gemini", url="https://unauthorized.invalid/chat/completions",
                stage="synthetic", payload=payload,
            )
        proofs["unauthorized_host_blocked"] = False
    except blocked_type:
        proofs["unauthorized_host_blocked"] = True
    try:
        with controlled_document_scope(document_sha256="f" * 64):
            pass
        proofs["outside_manifest_blocked"] = False
    except blocked_type:
        proofs["outside_manifest_blocked"] = True
    binary_payload = {
        **payload,
        "messages": [{"role": "user", "content": json.dumps({"source": SYNTHETIC_PNG})}],
    }
    try:
        with ai_runtime_trace.operation(
            batch_id="", stage="accounting_semantic_reasoning", provider="deepseek",
            model="synthetic", profile_id="synthetic",
        ):
            ai_runtime_trace.update_context(
                estimated_cost_usd=0.001,
                input_cost_usd_per_million=1.0,
                output_cost_usd_per_million=1.0,
            )
            assert_dispatch(
                provider="deepseek", url="https://api.deepseek.com/chat/completions",
                stage="accounting_semantic_reasoning", payload=binary_payload,
            )
        proofs["deepseek_source_binary_blocked"] = False
    except blocked_type:
        proofs["deepseek_source_binary_blocked"] = True
    minimized = build_minimized(lines=[{
        "source_text": r"C:\\private\\client.pdf John Smith account 123456",
        "filename": "client.pdf", "local_path": r"C:\\private\\client.pdf",
        "holdout_label": "hidden-answer", "ground_truth": "hidden-answer",
    }])
    rendered = json.dumps(minimized, sort_keys=True)
    proofs["holdout_labels_removed"] = "hidden-answer" not in rendered
    proofs["paths_and_filenames_removed"] = (
        "client.pdf" not in rendered and "C:\\private" not in rendered
    )
    try:
        with ai_runtime_trace.operation(
            batch_id="", stage="fallback", provider="openai",
            model="forbidden", profile_id="fallback",
        ):
            ai_runtime_trace.update_context(
                estimated_cost_usd=0.001,
                input_cost_usd_per_million=1.0,
                output_cost_usd_per_million=1.0,
            )
            assert_dispatch(
                provider="openai", url="https://api.openai.com/v1/chat/completions",
                stage="fallback", payload=payload,
            )
        proofs["no_remote_fallback"] = False
    except blocked_type:
        proofs["no_remote_fallback"] = True
    return proofs


def _spend_proofs(*, root: Path, controller_type, blocked_type) -> dict[str, bool]:
    proof_root = root / "preflight" / "synthetic-spend-proof-v1"
    controller = controller_type(proof_root, "exp-controlled-spend-proof")
    first = controller.reserve(
        phase="A", estimated_cost_usd="0.000001", provider="gemini",
        model_id="synthetic", profile_id="synthetic", stage="preflight",
    )
    controller.release_reserved(first.reservation_id, reason="proof_only")
    reopened = controller_type(proof_root, "exp-controlled-spend-proof")
    state = json.loads(reopened.path.read_text(encoding="utf-8"))
    survives = first.reservation_id in state.get("reservations", {})
    full = reopened.reserve(
        phase="A", estimated_cost_usd="10.000000", provider="gemini",
        model_id="synthetic", profile_id="synthetic", stage="preflight",
    )
    blocked = False
    try:
        reopened.reserve(
            phase="A", estimated_cost_usd="0.000001", provider="deepseek",
            model_id="synthetic", profile_id="synthetic", stage="preflight",
        )
    except blocked_type:
        blocked = True
    finally:
        reopened.release_reserved(full.reservation_id, reason="proof_only")
    return {
        "spend_reservation_survives_rerun": survives,
        "budget_exhaustion_blocks_before_dispatch": blocked,
    }


def _authority_proofs() -> dict[str, bool]:
    from decimal import Decimal
    from webapp.backend.services.accounting_contracts import (
        DocumentFacts, GLCandidate, LineItemFacts,
    )
    from webapp.backend.services.accounting_decision_engine import (
        AccountingDecisionEngine,
    )
    from webapp.backend.services.accounting_readiness import evaluate_rows
    from webapp.backend.services.gl_catalog import load_gl_catalog
    from webapp.backend.services.semantic_classifier import classify_line

    line = LineItemFacts(
        line_item_id="synthetic-line", raw_description="legal service",
        amount=Decimal("10.00"),
    )
    facts = DocumentFacts(
        document_id="synthetic-document", invoice_id="synthetic-invoice",
        line_items=[line], extraction_route="synthetic_preflight",
    )
    semantics = classify_line(line, document_id=facts.document_id)
    _, catalog = load_gl_catalog()
    candidate = GLCandidate(
        gl_code="6205", gl_name=catalog["6205"].gl_name,
        source="synthetic_candidate", source_id="synthetic", base_score=0.95,
        rule_version="synthetic/1",
    )
    decision = AccountingDecisionEngine().decide(
        facts, semantics, catalog, [candidate], {},
    )
    readiness = evaluate_rows([{
        "Invoice Number": "SYNTHETIC", "Vendor": "Synthetic",
        "Property Abbreviation": "SYN", "GL Account": "", "Amount": 10,
        "_meta": {
            "invoice_group_id": "synthetic-invoice",
            "accounting_decision": decision.model_dump(mode="json"),
        },
    }])
    return {
        "accounting_decision_engine_final_gl_authority": (
            decision.decision_source == "AccountingDecisionEngine"
            and decision.selected_gl_code == "6205"
        ),
        "accounting_readiness_export_authority": (
            readiness.export_allowed is False
            and any(issue.field == "GL Account" for issue in readiness.blockers)
        ),
    }


def _is_ignored(path: Path) -> bool:
    import subprocess
    completed = subprocess.run(
        ["git", "check-ignore", "-q", str(path)],
        cwd=PROJECT_ROOT, capture_output=True, check=False,
    )
    return completed.returncode == 0


def _exactly_one(values):
    items = list(values)
    if len(items) != 1:
        raise RuntimeError("exactly_one_frozen_artifact_required")
    return items[0].resolve(strict=True)


if __name__ == "__main__":
    raise SystemExit(main())
