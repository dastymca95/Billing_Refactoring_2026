"""Run bounded, field-only verification over private evidence crops.

Outputs remain private under the supplied benchmark directory.  A verifier
observation is never promoted to human-adjudicated gold.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from webapp.backend.services import ai_provider
from webapp.backend.services.evidence_benchmark import (
    EvidenceBackedGoldenContract,
    VerifierObservation,
)
from webapp.backend.services.provider_capabilities import ModelProfileRole, ProfileLoader


class FieldObservation(BaseModel):
    field: str
    raw_value: str | None = None
    alternatives: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    legible: bool
    explanation: str


class CropObservation(BaseModel):
    observations: list[FieldObservation]


ALIASES = {
    "ai_handwritten_row_identities": "row_identity",
    "ai_row_identity_evidence": "row_identity",
    "ai_row_identity_verification": "row_identity",
    "Location": "row_identity",
    "ai_excluded_paid_rows": "paid_crossed_out_status",
    "Line Item Description": "line_item_concept",
    "Amount": "amount",
    "Quantity": "quantity",
    "Unit Price": "unit_price",
    "Invoice Date": "invoice_date",
    "ai_service_date": "service_date",
    "ai_service_date_raw": "service_date",
    "Due Date": "due_date_text_or_date",
    "Property Abbreviation": "property",
}


def _data_url(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _profile(profile_id: str):
    profiles = [profile for profile in ProfileLoader().load()
                if profile.profile_id == profile_id
                and profile.role is ModelProfileRole.INDEPENDENT_VERIFICATION]
    if not profiles:
        raise RuntimeError("independent_verification_profile_missing")
    profile = profiles[0]
    if not profile.enabled or not profile.credentials_present or not profile.vision:
        raise RuntimeError("independent_verification_profile_unavailable")
    return profile


def _verify_group(profile, benchmark_root: Path, group: dict[str, Any]) -> dict[str, Any]:
    request_id = f"targeted-verifier-{uuid.uuid4().hex}"
    fields = group["fields"]
    prompt = {
        "task": "read_only_the_listed_disputed_fields_from_this_crop",
        "rules": [
            "Read pixels only; do not use accounting catalogs, vendor history, or plausibility.",
            "Do not choose a candidate merely because it looks like a valid apartment or GL.",
            "If ambiguous, return null raw_value, alternatives, and low confidence.",
            "Preserve literal visible wording and marks, including PAID or crossed-out evidence.",
        ],
        "disputed_fields": fields,
        "candidate_values_are_non_authoritative": group["candidates"],
        "response_schema": {
            "observations": [{
                "field": "exact disputed field", "raw_value": "literal pixels or null",
                "alternatives": ["other visible reading"], "confidence": "0..1",
                "legible": "boolean", "explanation": "brief visual explanation",
            }],
        },
    }
    content = [
        {"type": "text", "text": json.dumps(prompt, separators=(",", ":"))},
        {"type": "image_url", "image_url": {"url": _data_url(benchmark_root / group["crop_ref"])}},
    ]
    payload = {
        "model": profile.model_id,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": (
                "You are an independent visual evidence verifier. Return strict JSON only. "
                "You do not adjudicate ground truth and you do not perform accounting reasoning."
            )},
            {"role": "user", "content": content},
        ],
        **ai_provider._completion_controls(profile.provider, min(1800, 350 + len(fields) * 180)),
    }
    started = time.perf_counter()
    raw = ai_provider._send_chat_completion(
        provider=profile.provider, payload=payload, vision=True,
        api_key_override=profile.api_key.get_secret_value() if profile.api_key else None,
        base_url_override=profile.base_url, timeout_seconds_override=profile.timeout_seconds,
        max_attempts_override=profile.max_retries + 1,
    )
    parsed = CropObservation(**ai_provider._extract_json_object(raw))
    requested = set(fields)
    returned = {item.field for item in parsed.observations}
    if returned != requested:
        raise ValueError("verifier_field_set_mismatch")
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return {
        "request_id": request_id, "profile_id": profile.profile_id,
        "provider": profile.provider, "model_id": profile.model_id,
        "verification_independence": profile.verification_independence,
        "crop_sha256": group["crop_sha256"], "crop_ref": group["crop_ref"],
        "elapsed_ms": elapsed_ms, "observed_at": datetime.now(timezone.utc).isoformat(),
        "observations": [item.model_dump(mode="json") for item in parsed.observations],
    }


def _groups(queue: dict[str, Any]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for task in queue["tasks"]:
        if not task["is_visual_field"]:
            continue
        crop_sha = task["evidence"]["crop_sha256"]
        group = grouped.setdefault(crop_sha, {
            "crop_sha256": crop_sha, "crop_ref": task["evidence"]["crop_ref"],
            "fields": set(), "candidates": {}, "task_ids": [],
        })
        field = ALIASES[task["field"]]
        group["fields"].add(field)
        group["task_ids"].append(task["task_id"])
        group["candidates"].setdefault(field, [])
        for value in (task.get("cold_candidate"), task.get("prior_candidate")):
            if value not in (None, "") and value not in group["candidates"][field]:
                group["candidates"][field].append(value)
    result = []
    for group in grouped.values():
        group["fields"] = sorted(group["fields"])
        result.append(group)
    return sorted(result, key=lambda item: item["crop_ref"])


def _merge_contract(root: Path, queue: dict[str, Any], results: list[dict[str, Any]]) -> None:
    contract_path = root / "golden_contract.pending.json"
    contract = EvidenceBackedGoldenContract(**json.loads(contract_path.read_text(encoding="utf-8")))
    result_by_crop = {item["crop_sha256"]: item for item in results}
    invoice_by_id = {invoice.invoice_id: invoice for invoice in contract.invoices}
    for task in queue["tasks"]:
        result = result_by_crop.get(task["evidence"]["crop_sha256"])
        if result is None or not task["is_visual_field"]:
            continue
        target_name = ALIASES[task["field"]]
        observation = next((item for item in result["observations"]
                            if item["field"] == target_name), None)
        if observation is None:
            continue
        invoice = invoice_by_id[task["invoice_id"]]
        if task.get("line") and target_name in {
            "row_identity", "paid_crossed_out_status", "line_item_concept",
            "amount", "quantity", "unit_price",
        }:
            row = invoice.rows[int(task["line"]) - 1]
            target = {
                "row_identity": row.row_identity,
                "paid_crossed_out_status": row.paid_crossed_out_status,
                "line_item_concept": row.line_item_concept,
                "amount": row.amount,
                "quantity": row.amount,
                "unit_price": row.amount,
            }[target_name]
        else:
            target = invoice.header_fields.get(target_name)
            if target is None and target_name == "due_date_text_or_date":
                target = invoice.header_fields["due_date_text"]
            if target is None:
                continue
        if any(item.request_id == result["request_id"] for item in target.verifier_observations):
            continue
        target.verifier_observations.append(VerifierObservation(
            verifier_id=result["profile_id"], observed_at=result["observed_at"],
            raw_value=observation["raw_value"], alternatives=observation["alternatives"],
            confidence=observation["confidence"], evidence=task["evidence"],
            request_id=result["request_id"],
        ))
    contract_path.write_text(json.dumps(contract.model_dump(mode="json"), indent=2, sort_keys=True), encoding="utf-8")
    by_crop = {item["crop_sha256"]: item for item in results}
    workspace_tasks = []
    for task in queue["tasks"]:
        verifier = by_crop.get(task["evidence"]["crop_sha256"])
        workspace_tasks.append({
            **task,
            "verifier_observations": verifier.get("observations", []) if verifier else [],
            "verifier_request_id": verifier.get("request_id") if verifier else None,
            "human_review": {
                "reviewer_id": None,
                "decision": None,
                "accepted_value": None,
                "acceptable_alternatives": [],
                "rationale": None,
                "reviewed_at": None,
            },
        })
    (root / "human_adjudication_workspace.pending.json").write_text(
        json.dumps({
            "schema_version": "human-evidence-adjudication-workspace/1.0",
            "batch_id": queue["batch_id"],
            "status": "pending_human_review",
            "verifier_is_ground_truth": False,
            "tasks": workspace_tasks,
        }, indent=2, sort_keys=True), encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark-root", type=Path, required=True)
    parser.add_argument("--profile-id", default="anthropic-verification")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--merge-existing-only", action="store_true")
    parser.add_argument("--retry-failures-only", action="store_true")
    args = parser.parse_args()
    root = args.benchmark_root.resolve()
    queue_path = root / "visual_disagreement_queue.json"
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    groups = _groups(queue)
    if args.merge_existing_only:
        existing = json.loads((root / "targeted_verifier_results.json").read_text(encoding="utf-8"))
        _merge_contract(root, queue, existing.get("results") or [])
        print(json.dumps({"merged_existing_results": len(existing.get("results") or []),
                          "failed_crop_count": len(existing.get("failures") or [])}, indent=2))
        return 0
    existing_results: list[dict[str, Any]] = []
    if args.retry_failures_only:
        existing = json.loads((root / "targeted_verifier_results.json").read_text(encoding="utf-8"))
        failed_hashes = {item["crop_sha256"] for item in existing.get("failures") or []}
        groups = [group for group in groups if group["crop_sha256"] in failed_hashes]
        existing_results = list(existing.get("results") or [])
    profile = _profile(args.profile_id)
    results = []
    failures = []
    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 4))) as pool:
        futures = {pool.submit(_verify_group, profile, root, group): group for group in groups}
        for future in as_completed(futures):
            group = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                failures.append({"crop_sha256": group["crop_sha256"],
                                 "crop_ref": group["crop_ref"],
                                 "failure_code": type(exc).__name__})
    results = existing_results + results
    results.sort(key=lambda item: item["crop_ref"])
    output = {
        "schema_version": "targeted-visual-verification-results/1.0",
        "batch_id": queue["batch_id"], "profile_id": profile.profile_id,
        "verification_independence": profile.verification_independence,
        "requested_crop_count": len(_groups(queue)), "successful_crop_count": len(results),
        "failed_crop_count": len(failures), "results": results, "failures": failures,
    }
    (root / "targeted_verifier_results.json").write_text(
        json.dumps(output, indent=2, sort_keys=True), encoding="utf-8"
    )
    _merge_contract(root, queue, results)
    print(json.dumps({key: output[key] for key in (
        "batch_id", "profile_id", "verification_independence",
        "requested_crop_count", "successful_crop_count", "failed_crop_count",
    )}, indent=2))
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
