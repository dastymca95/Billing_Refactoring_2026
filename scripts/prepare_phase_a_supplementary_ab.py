"""Build the private byte-identical paired Phase A supplementary bundle offline."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.review_phase_a_supplementary_evidence import (  # noqa: E402
    _assert_private_output_root, _batch_id_for_source, _experiment_root,
    _field_for_target, _find_saved_observed_facts, _read_jsonl, _render_pages,
    _run_manifest, _sha256, _single_directory, _source_root,
)
from webapp.backend.services import document_ingestion  # noqa: E402
from webapp.backend.services.experiment_spend_controller import ExperimentSpendController  # noqa: E402
from webapp.backend.services.gemini_supplementary_verification import (  # noqa: E402
    SupplementaryTarget, SupplementaryTargetType, build_minimized_initial_summary,
    build_supplementary_prompt, supplementary_response_format,
)
from webapp.backend.services.supplementary_ab_experiment import (  # noqa: E402
    AB_FREEZE_VERSION, ARM_B_MODEL_ID, ExcludedLocalizationTarget,
    FrozenCropReference, FrozenPacketRecord, FrozenPairedManifest,
    SyntheticCapabilityProbeContract, build_paired_references,
    calculate_paired_budget, canonical_json_bytes, canonical_json_sha256,
    frame_packet_bytes, git_safe_summary, sha256_bytes,
)
from webapp.backend.services.supplementary_evidence_planner import (  # noqa: E402
    build_evidence_packet, build_supplementary_evidence_plan,
)


OUTPUT_NAME = "paired-bundle-v1"
SETTINGS = {
    "temperature": 0,
    "max_output_tokens": 2048,
    "candidate_count": 1,
    "response_mime_type": "application/json",
    "retry_count": 0,
    "fallback": False,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Freeze paired supplementary packets offline")
    parser.add_argument("--experiment-root", type=Path)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--run-id", default="run-20260720T173724423377Z")
    args = parser.parse_args()
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    experiment_root = _experiment_root(args.experiment_root)
    source_root = _source_root(args.source_root)
    _assert_private_output_root(experiment_root)
    run = (experiment_root / "phase_a" / "runs" / args.run_id).resolve(strict=True)
    runs_root = (experiment_root / "phase_a" / "runs").resolve(strict=True)
    run.relative_to(runs_root)
    manifest_path, run_manifest = _run_manifest_with_path(run)
    source_map = dict(run_manifest.get("source_map") or {})
    if len(source_map) != 5:
        raise SystemExit("paired_ab_source_assignment_count_invalid")
    historical = _historical_target_events(run)
    eligible_events = [item for item in historical if item.get("outcome") == "packet_validated"]
    excluded_events = [item for item in historical if item.get("outcome") == "packet_rejected_locally"]
    if len(eligible_events) != 5 or len(excluded_events) != 2:
        raise SystemExit("paired_ab_historical_eligibility_inventory_invalid")

    inventory_root = _single_directory(experiment_root / "snapshots", "corpus-*")
    inventory = {str(row["document_id"]): row for row in _read_jsonl(inventory_root / "inventory.jsonl")}
    locators = {str(row["document_id"]): str(row["relative_path"]) for row in _read_jsonl(inventory_root / "private_locators.jsonl")}

    output_root = experiment_root / "phase_a" / "supplementary_ab" / OUTPUT_NAME
    if output_root.exists():
        if (output_root / "COMPLETE").is_file():
            raise SystemExit("paired_ab_immutable_bundle_already_exists")
        output_root.relative_to(experiment_root / "phase_a" / "supplementary_ab")
        shutil.rmtree(output_root)
    output_root.mkdir(parents=True, exist_ok=False)

    records: list[FrozenPacketRecord] = []
    historical_by_batch = {str(item.get("batch_id")): item for item in eligible_events}
    source_items = list(source_map.items())
    for position, (_private_name, source_record) in enumerate(source_items, start=1):
        document_id = str(source_record["document_id"])
        expected_sha = str(source_record["source_content_sha256"])
        if str(inventory[document_id].get("content_sha256") or "") != expected_sha:
            raise SystemExit("paired_ab_inventory_hash_mismatch")
        source = (source_root / locators[document_id]).resolve(strict=True)
        source.relative_to(source_root.resolve(strict=True))
        if _sha256(source) != expected_sha:
            raise SystemExit("paired_ab_source_hash_mismatch")
        batch_id = _batch_id_for_source(run, expected_sha, position=position)
        event = historical_by_batch.get(batch_id)
        if event is None:
            raise SystemExit("paired_ab_eligible_target_assignment_missing")
        initial_facts = _find_saved_observed_facts(run, expected_sha, batch_id=batch_id)
        # Two historically fact-invalid documents intentionally have no strict
        # DocumentFacts artifact.  The frozen A/B target is visual evidence,
        # so use only the deterministic local layout for those packets rather
        # than regenerating facts, calling a provider, or inventing values.
        if initial_facts is None:
            initial_facts = {
                "line_items": [], "evidence": [],
                "page_reconciliations": [], "warnings": [],
            }
        generation_one = _generate_packet(source, expected_sha, initial_facts, event)
        generation_two = _generate_packet(source, expected_sha, initial_facts, event)
        equal = _generation_fingerprint(generation_one) == _generation_fingerprint(generation_two)
        # Freeze generation one even if local rendering is non-deterministic;
        # both future arms always reference these exact first-generation bytes.
        records.append(_persist_generation(output_root, position, generation_one, equal))

    references = tuple(reference for record in records for reference in build_paired_references(record))
    exclusions = tuple(ExcludedLocalizationTarget(
        target_id=str(item.get("plan_id") or _event_id(item)),
        target_category=str(item.get("target_category") or "unknown"),
    ) for item in excluded_events)
    probe_image = b"P5\n2 2\n255\n\x00\x80\x80\xff"
    probe_prompt = b"Return JSON confirming that the synthetic checkerboard is visible."
    probe_schema = canonical_json_bytes({
        "type": "object", "properties": {"visible": {"type": "boolean"}},
        "required": ["visible"], "additionalProperties": False,
    })
    probe = SyntheticCapabilityProbeContract(
        image_sha256=sha256_bytes(probe_image), prompt_sha256=sha256_bytes(probe_prompt),
        schema_sha256=sha256_bytes(probe_schema),
    )
    paired_manifest = FrozenPairedManifest(
        source_run_id=run.name, source_manifest_sha256=_sha256(manifest_path),
        packet_records=tuple(records), arm_references=references,
        excluded_localization_targets=exclusions, capability_probe=probe,
    )

    input_tokens, output_tokens = _historical_usage(run)
    baseline = json.loads((run / "private_baseline_output.json").read_text(encoding="utf-8"))
    controller = ExperimentSpendController(experiment_root, str(baseline["experiment_id"]))
    cumulative = Decimal(controller.snapshot("A").cumulative_charged_usd)
    budget = calculate_paired_budget(
        expected_input_tokens_per_arm=input_tokens,
        expected_output_tokens_per_arm=output_tokens,
        phase_a_cumulative_spend_usd=cumulative,
        observed_packet_total_pixels=sum(item.total_pixels for item in records),
        observed_prompt_total_bytes=sum(item.prompt_byte_length for item in records),
    )
    private_payload = {
        "manifest": paired_manifest.model_dump(mode="json"),
        "execution_policy": {
            "max_requests": 10, "max_per_packet_per_arm": 1,
            "models": ["gemini-3.1-flash-lite", "gemini-3.5-flash"],
            "authorized_host": "generativelanguage.googleapis.com",
            "retries": 0, "fallback": False, "other_routes": False,
        },
        "budget": budget.model_dump(mode="json"),
        "execution_authorized": False,
        "capability_probe_authorized": False,
        "provider_calls": 0,
    }
    _write_new_json(output_root / "paired_manifest.json", private_payload)
    safe = git_safe_summary(paired_manifest, budget)
    _write_new_json(output_root / "git_safe_summary.json", safe)
    (output_root / "COMPLETE").write_text(AB_FREEZE_VERSION + "\n", encoding="utf-8")
    print(json.dumps(safe, sort_keys=True))
    return 0


def _generate_packet(source: Path, source_sha: str, initial_facts: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    candidate = document_ingestion.ingest_document(source, allow_ocr=True, allow_vision_hint=False)
    layout = candidate.to_dict()
    page_refs = _render_pages(source)
    target_value = str(event["target_category"])
    target = SupplementaryTarget(
        target_type=SupplementaryTargetType(target_value), page_number=1,
        field_name=_field_for_target(target_value),
        local_trigger_codes=["paired_ab_frozen_target"],
    )
    opaque_id = "doc_" + hashlib.sha256(source_sha.encode("ascii")).hexdigest()[:24]
    plan = build_supplementary_evidence_plan(
        opaque_document_id=opaque_id, target=target,
        initial_facts=initial_facts, document_layout=layout,
    )
    packet = build_evidence_packet(plan, page_images=page_refs)
    minimized = build_minimized_initial_summary(initial_facts, target)
    prompt = build_supplementary_prompt(
        opaque_document_id=opaque_id, target=target, minimized_summary=minimized,
        evidence_plan_summary=plan.provider_summary(),
    ).encode("utf-8")
    schema = canonical_json_bytes(supplementary_response_format(target))
    crops = []
    framed = []
    for ordinal, image in enumerate(packet.images):
        content = base64.b64decode(image.data_url.split(",", 1)[1])
        metadata = {
            "crop_id": image.crop_id, "role": image.role.value, "category": image.category.value,
            "ordinal": ordinal, "mime_type": "image/jpeg", "width": image.width,
            "height": image.height, "pixel_count": image.pixel_count,
            "sha256": sha256_bytes(content), "byte_length": len(content),
        }
        crops.append((metadata, content)); framed.append((metadata, content))
    return {
        "source_sha": source_sha, "plan": plan, "prompt": prompt, "schema": schema,
        "crops": crops, "packet": frame_packet_bytes(framed),
    }


def _persist_generation(root: Path, position: int, value: dict[str, Any], equal: bool) -> FrozenPacketRecord:
    plan = value["plan"]
    packet_id = "packet_" + sha256_bytes((value["source_sha"] + plan.plan_id).encode())[:24]
    directory_name = f"packet_{position:02d}_{packet_id[-8:]}"
    directory = root / directory_name; directory.mkdir()
    crop_refs = []
    for metadata, content in value["crops"]:
        ordinal = int(metadata["ordinal"]); relative = f"{directory_name}/crop_{ordinal:02d}.jpg"
        (root / relative).write_bytes(content)
        crop_refs.append(FrozenCropReference(**metadata, relative_blob_path=relative))
    paths = {
        "packet": f"{directory_name}/packet.bin",
        "prompt": f"{directory_name}/prompt.utf8",
        "schema": f"{directory_name}/schema.json",
    }
    (root / paths["packet"]).write_bytes(value["packet"])
    (root / paths["prompt"]).write_bytes(value["prompt"])
    (root / paths["schema"]).write_bytes(value["schema"])
    planner_fingerprint = canonical_json_sha256({
        "plan": plan.model_dump(mode="json"), "planner_version": plan.plan_version,
        "localizer_version": plan.localizer_version,
    })
    return FrozenPacketRecord(
        packet_id=packet_id, plan_id=plan.plan_id,
        target_category=plan.target_category, target_subtype=plan.target_subtype.value,
        opaque_source_sha256=value["source_sha"],
        relative_packet_path=paths["packet"], packet_sha256=sha256_bytes(value["packet"]),
        packet_byte_length=len(value["packet"]),
        total_pixels=sum(item.pixel_count for item in crop_refs), crops=tuple(crop_refs),
        relative_prompt_path=paths["prompt"], prompt_sha256=sha256_bytes(value["prompt"]),
        prompt_byte_length=len(value["prompt"]), relative_schema_path=paths["schema"],
        schema_sha256=sha256_bytes(value["schema"]), schema_byte_length=len(value["schema"]),
        generation_settings_fingerprint=canonical_json_sha256(SETTINGS),
        planner_fingerprint=planner_fingerprint, offline_regeneration_equal=equal,
    )


def _generation_fingerprint(value: dict[str, Any]) -> str:
    return canonical_json_sha256({
        "plan": value["plan"].model_dump(mode="json"),
        "packet": sha256_bytes(value["packet"]), "prompt": sha256_bytes(value["prompt"]),
        "schema": sha256_bytes(value["schema"]),
        "crops": [sha256_bytes(content) for _, content in value["crops"]],
    })


def _run_manifest_with_path(run: Path) -> tuple[Path, dict[str, Any]]:
    selected = _run_manifest(run)
    for path in run.glob("*.json"):
        try: payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception: continue
        if payload == selected:
            return path, selected
    raise SystemExit("paired_ab_manifest_file_unavailable")


def _historical_target_events(run: Path) -> list[dict[str, Any]]:
    events = []
    for path in run.rglob("ai_request_trace.jsonl"):
        for item in _read_jsonl(path):
            if item.get("event") == "supplementary_evidence_plan" and item.get("outcome") in {
                "packet_validated", "packet_rejected_locally",
            }:
                events.append(item)
    return events


def _historical_usage(run: Path) -> tuple[int, int]:
    prompt = completion = 0
    for path in run.rglob("ai_request_trace.jsonl"):
        for item in _read_jsonl(path):
            if item.get("event") != "provider_usage" or "supplementary" not in str(item.get("stage") or ""):
                continue
            usage = item.get("usage") if isinstance(item.get("usage"), dict) else {}
            prompt += int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            completion += int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    if prompt <= 0:
        raise SystemExit("paired_ab_usage_estimate_unavailable")
    return prompt, completion


def _event_id(item: dict[str, Any]) -> str:
    return "target_" + canonical_json_sha256({
        "batch": item.get("batch_id"), "category": item.get("target_category"),
        "subtype": item.get("target_subtype"),
    })[:24]


def _write_new_json(path: Path, value: dict[str, Any]) -> None:
    if path.exists():
        raise FileExistsError("immutable_paired_ab_artifact_exists")
    path.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
