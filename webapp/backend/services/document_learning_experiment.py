"""Privacy-safe local inventory and leakage-resistant split contracts.

This module is intentionally provider-free.  It observes only local file bytes
and decoded page pixels, writes detailed artifacts only below an ignored
runtime root, and exposes an aggregate report that contains no document
identifiers, hashes, paths, filenames, or source text.

The exact visual hashes in this module are equality keys, not perceptual
similarity keys.  Approximate image similarity must never authorize benchmark
reuse or permit related pages to cross evaluation splits.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from importlib import metadata
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .experiment_spend_controller import (
    ExperimentSpendController,
    SpendAuthorizationError,
)


INVENTORY_SCHEMA_VERSION = "document-learning-inventory/1.0"
SPLIT_SCHEMA_VERSION = "document-learning-split/1.0"
VISUAL_HASH_VERSION = "decoded-rgb-sha256/1.0"
DEFAULT_SPLIT_RATIOS: Mapping[str, float] = {
    "train": 0.70,
    "validation": 0.15,
    "test": 0.15,
}
IMAGE_EXTENSIONS = {
    ".bmp",
    ".gif",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}
_ALLOWED_PRIVATE_RUNTIME_DIRS = ("tmp", "webapp_data")
_FORBIDDEN_SAFE_KEY_PARTS = (
    "account_number",
    "bbox",
    "document_id",
    "evidence",
    "filename",
    "hash",
    "invoice_id",
    "invoice_number",
    "invoice_value",
    "model_response",
    "path",
    "provider_response",
    "raw_text",
    "source_text",
    "tenant",
    "vendor_identity",
    "vendor_name",
)


class ExperimentPathError(ValueError):
    """Raised when private experiment paths violate the storage boundary."""


class InventorySourceChangedError(RuntimeError):
    """Raised when source file identity changes during a read-only inventory."""


@dataclass(frozen=True)
class PrivatePathContract:
    project_root: Path
    source_root: Path
    runtime_root: Path


@dataclass(frozen=True)
class InventoryRunResult:
    dataset_version: str
    snapshot_root: Path
    git_safe_summary: Mapping[str, Any]


@dataclass(frozen=True)
class PreflightRunResult:
    private_report_path: Path
    git_safe_summary: Mapping[str, Any]


@dataclass(frozen=True)
class EligibilityRunResult:
    private_report_path: Path
    git_safe_summary: Mapping[str, Any]


@dataclass(frozen=True)
class SplitRunResult:
    split_root: Path
    split_sha256: str
    git_safe_summary: Mapping[str, Any]


@dataclass(frozen=True)
class CalibrationSampleResult:
    manifest_path: Path
    git_safe_summary: Mapping[str, Any]


def create_phase_a_calibration_sample(
    *, inventory_snapshot_root: Path, split_root: Path,
    experiment_runtime_root: Path, seed: str,
    maximum_documents: int = 100,
) -> CalibrationSampleResult:
    """Create a 100-document routing/cost sample without inventing labels.

    Evidence-backed split units are included first. Additional private source
    documents broaden observable media/parser coverage, but are explicitly
    marked coverage-only and never enter accuracy or learning denominators.
    """
    inventory_root = inventory_snapshot_root.resolve(strict=True)
    private_split_root = split_root.resolve(strict=True)
    runtime_root = experiment_runtime_root.resolve(strict=True)
    if not (
        _is_relative_to(inventory_root, runtime_root)
        and _is_relative_to(private_split_root, runtime_root)
    ):
        raise ExperimentPathError("calibration inputs must be private artifacts")
    if maximum_documents < 10 or maximum_documents > 100:
        raise ValueError("Phase A calibration allows between 10 and 100 documents")
    records = {
        str(row["document_id"]): row
        for row in _read_jsonl(inventory_root / "inventory.jsonl")
    }
    split_manifest = json.loads(
        (private_split_root / "split_manifest.json").read_text(encoding="utf-8")
    )
    assignments: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    for item in split_manifest.get("assignments") or []:
        document_id = str(item["representative_document_id"])
        if document_id not in records or document_id in selected_ids:
            continue
        selected_ids.add(document_id)
        assignments.append({
            "document_id": document_id,
            "evaluation_scope": "evidence_backed",
            "cohort": str(item["cohort"]),
            "unit_id": str(item["unit_id"]),
        })

    # Conservative local grouping: an observed invoice identity, exact file,
    # or exact page can make two documents related. Only one unlabeled member
    # is needed for routing/cost calibration; related siblings add spend but no
    # independent evidence.
    parent = {document_id: document_id for document_id in records}

    def find(value: str) -> str:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    indexes: list[dict[str, list[str]]] = [defaultdict(list) for _ in range(3)]
    for document_id, record in records.items():
        indexes[0][str(record.get("content_sha256") or "")].append(document_id)
        for value in record.get("exact_visual_page_hashes") or []:
            indexes[1][str(value)].append(document_id)
        for value in record.get("invoice_identity_fingerprints") or []:
            indexes[2][str(value)].append(document_id)
    for index in indexes:
        for key, members in index.items():
            if not key or len(members) < 2:
                continue
            for member in members[1:]:
                union(members[0], member)
    blocked_components = {find(document_id) for document_id in selected_ids}
    representative_by_component: dict[str, str] = {}
    for document_id, record in records.items():
        if document_id in selected_ids:
            continue
        if str(record.get("visual_hash_status") or "") != "verified":
            continue
        if str(record.get("extension") or "") not in ({".pdf"} | IMAGE_EXTENSIONS):
            continue
        component = find(document_id)
        if component in blocked_components:
            continue
        current = representative_by_component.get(component)
        if current is None or _calibration_priority(record, seed, document_id) < _calibration_priority(
            records[current], seed, current,
        ):
            representative_by_component[component] = document_id

    buckets: dict[str, list[str]] = defaultdict(list)
    for document_id in representative_by_component.values():
        record = records[document_id]
        buckets[_calibration_stratum(record)].append(document_id)
    for stratum, members in buckets.items():
        members.sort(key=lambda document_id: hashlib.sha256(
            f"{seed}:{stratum}:{document_id}".encode("utf-8")
        ).hexdigest())
    strata = sorted(buckets, key=lambda value: (
        _calibration_stratum_priority(value),
        hashlib.sha256(f"{seed}:{value}".encode("utf-8")).hexdigest(),
    ))
    while len(assignments) < maximum_documents:
        progressed = False
        for stratum in strata:
            if buckets[stratum] and len(assignments) < maximum_documents:
                document_id = buckets[stratum].pop(0)
                selected_ids.add(document_id)
                assignments.append({
                    "document_id": document_id,
                    "evaluation_scope": "coverage_only_unlabeled",
                    "cohort": "calibration_coverage",
                    "unit_id": "coverage-" + hashlib.sha256(
                        document_id.encode("utf-8")
                    ).hexdigest()[:24],
                })
                progressed = True
        if not progressed:
            break

    manifest = {
        "schema_version": "document-learning-phase-a-calibration/1.0",
        "dataset_version": inventory_root.name,
        "split_version": private_split_root.name,
        "seed_fingerprint": hashlib.sha256(seed.encode("utf-8")).hexdigest(),
        "maximum_documents": maximum_documents,
        "assignments": sorted(assignments, key=lambda item: item["unit_id"]),
        "answers_embedded": False,
        "coverage_only_documents_are_learning_eligible": False,
    }
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    version = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    output_root = runtime_root / "calibration" / f"phase-a-{version}"
    manifest_path = output_root / "calibration_manifest.json"
    _write_json_idempotent(manifest_path, manifest)
    selected_records = [records[str(item["document_id"])] for item in assignments]
    media_counts = Counter(
        str(record.get("probable_media_kind") or "unknown")
        for record in selected_records
    )
    safe = {
        "schema_version": "document-learning-phase-a-calibration-safe/1.0",
        "selected_documents": len(assignments),
        "authoritatively_labeled_documents": sum(
            item["evaluation_scope"] == "evidence_backed" for item in assignments
        ),
        "coverage_only_unlabeled_documents": sum(
            item["evaluation_scope"] == "coverage_only_unlabeled"
            for item in assignments
        ),
        "media_counts": dict(sorted(media_counts.items())),
        "active_deterministic_parser_documents": sum(
            record.get("deterministic_parser_status") == "active"
            for record in selected_records
        ),
        "multi_page_documents": sum(
            int(record.get("page_count") or 0) > 1 for record in selected_records
        ),
        "possible_multi_invoice_documents": sum(
            bool(record.get("appears_multi_invoice")) for record in selected_records
        ),
        "handwriting_ground_truth_documents": 0,
        "answers_embedded_in_manifest": False,
        "provider_calls": 0,
        "network_calls": 0,
    }
    assert_git_safe_summary(safe)
    _write_json_idempotent(output_root / "git_safe_summary.json", safe)
    _write_json_replace(
        runtime_root / "calibration" / "active_phase_a_calibration.json",
        {
            "schema_version": "document-learning-calibration-selection/1.0",
            "active_calibration_version": version,
        },
    )
    return CalibrationSampleResult(manifest_path, safe)


def create_phase_a_split(
    *, inventory_snapshot_root: Path, eligibility_path: Path,
    experiment_runtime_root: Path, seed: str, maximum_unique_invoices: int = 100,
) -> SplitRunResult:
    """Freeze five leakage-safe arms before any simulated correction."""
    inventory_root = inventory_snapshot_root.resolve(strict=True)
    runtime_root = experiment_runtime_root.resolve(strict=True)
    eligibility_file = eligibility_path.resolve(strict=True)
    if not _is_relative_to(inventory_root, runtime_root) or not _is_relative_to(
        eligibility_file, runtime_root,
    ):
        raise ExperimentPathError("split inputs must be private experiment artifacts")
    if maximum_unique_invoices < 10 or maximum_unique_invoices > 100:
        raise ValueError("Phase A allows between 10 and 100 unique invoices")
    records = {
        str(row["document_id"]): row
        for row in _read_jsonl(inventory_root / "inventory.jsonl")
    }
    eligibility = {
        str(row["document_id"]): row
        for row in _read_jsonl(eligibility_file)
        if row.get("eligibility_class") == "accepted_posted_resman_ground_truth"
    }
    units = _eligible_evaluation_units(records, eligibility, seed=seed)
    selected = _stratified_unit_selection(units, seed=seed, limit=maximum_unique_invoices)
    assignments = _assign_five_arms(selected, seed=seed)
    _assert_split_contract(assignments)

    dataset_version = inventory_root.name
    manifest = {
        "schema_version": "document-learning-phase-a-split/1.1",
        "dataset_version": dataset_version,
        "seed_fingerprint": hashlib.sha256(seed.encode("utf-8")).hexdigest(),
        "selection_limit": maximum_unique_invoices,
        "selected_unique_invoice_units": len(assignments),
        "leakage_grouping": [
            "exact_file_sha256", "exact_visual_page_sha256",
            "adjudicated_vendor_invoice_identity_without_total",
            "posted_history_occurrence",
        ],
        "similarity_axis": "canonical_accounting_family",
        "unrelated_axis": "canonical_accounting_family_disjoint_from_training",
        "assignments": [
            {
                "unit_id": item["unit_id"],
                "cohort": item["cohort"],
                "representative_document_id": item["representative_document_id"],
                "linked_document_ids": item["linked_document_ids"],
                "leakage_component_id": item["leakage_component_id"],
            }
            for item in sorted(assignments, key=lambda row: row["unit_id"])
        ],
        "holdout_answers_embedded": False,
    }
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    split_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    split_root = runtime_root / "splits" / f"phase-a-{split_sha256}"
    split_root.mkdir(parents=True, exist_ok=True)
    _write_json_idempotent(split_root / "split_manifest.json", manifest)
    labels_by_cohort: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in assignments:
        label = eligibility[item["representative_document_id"]]
        labels_by_cohort[item["cohort"]].append({
            "unit_id": item["unit_id"],
            "representative_document_id": item["representative_document_id"],
            "ground_truth": label["ground_truth"],
        })
    _write_jsonl_idempotent(
        split_root / "scopes" / "training_labels.jsonl",
        labels_by_cohort["training"],
    )
    _write_jsonl_idempotent(
        split_root / "scopes" / "benchmark_only_labels.jsonl",
        labels_by_cohort["benchmark_only"],
    )
    _write_jsonl_idempotent(
        split_root / "scopes" / "rule_simulation_labels.jsonl",
        labels_by_cohort["rule_simulation"],
    )
    _write_jsonl_idempotent(
        split_root / "hidden" / "holdout_labels.jsonl",
        [
            *labels_by_cohort["similar_holdout"],
            *labels_by_cohort["unrelated_holdout"],
        ],
    )
    selected_ids = {item["unit_id"] for item in assignments}
    _write_json_idempotent(
        split_root / "reserve_manifest.json",
        {
            "schema_version": "document-learning-reserve/1.0",
            "unit_ids": sorted(item["unit_id"] for item in units if item["unit_id"] not in selected_ids),
        },
    )
    counts = Counter(item["cohort"] for item in assignments)
    training_items = [item for item in assignments if item["cohort"] == "training"]
    unrelated_items = [
        item for item in assignments if item["cohort"] == "unrelated_holdout"
    ]
    training_vendors = {str(item["vendor_family"]) for item in training_items}
    unrelated_vendors = {str(item["vendor_family"]) for item in unrelated_items}
    training_layouts = {
        str(value) for item in training_items for value in item["layout_families"]
    }
    unrelated_layouts = {
        str(value) for item in unrelated_items for value in item["layout_families"]
    }
    safe = {
        "schema_version": "document-learning-phase-a-split-safe/1.1",
        "eligible_unique_invoice_units": len(units),
        "selected_unique_invoice_units": len(assignments),
        "reserve_unique_invoice_units": len(units) - len(assignments),
        "cohort_counts": dict(sorted(counts.items())),
        "leakage_components_crossing_cohorts": 0,
        "similar_holdout_without_training_family": 0,
        "unrelated_holdout_family_overlap": 0,
        "unrelated_holdout_vendor_family_overlap_count": len(
            training_vendors.intersection(unrelated_vendors)
        ),
        "unrelated_holdout_layout_family_overlap_count": len(
            training_layouts.intersection(unrelated_layouts)
        ),
        "modified_variant_groups_crossing_cohorts": 0,
        "holdout_answers_embedded_in_manifest": False,
        "provider_calls": 0,
        "network_calls": 0,
    }
    assert_git_safe_summary(safe)
    _write_json_idempotent(split_root / "git_safe_summary.json", safe)
    candidates = sorted(path.name.removeprefix("phase-a-") for path in (
        runtime_root / "splits"
    ).glob("phase-a-*") if path.is_dir())
    _write_json_replace(
        runtime_root / "splits" / "active_phase_a_split.json",
        {
            "schema_version": "document-learning-split-selection/1.0",
            "active_split_sha256": split_sha256,
            "candidates": [
                {
                    "split_sha256": value,
                    "status": "accepted" if value == split_sha256 else "rejected_superseded",
                }
                for value in candidates
            ],
        },
    )
    return SplitRunResult(split_root, split_sha256, safe)


def classify_ground_truth_eligibility(
    *, inventory_snapshot_root: Path, source_root: Path,
    experiment_runtime_root: Path, historical_tenant_id: str,
) -> EligibilityRunResult:
    """Match observed source facts to posted ResMan history without AI outputs."""
    inventory_root = inventory_snapshot_root.resolve(strict=True)
    runtime_root = experiment_runtime_root.resolve(strict=True)
    source = source_root.resolve(strict=True)
    if not _is_relative_to(inventory_root, runtime_root):
        raise ExperimentPathError("inventory snapshot must be inside the private experiment root")
    salt = (runtime_root / ".inventory_hmac_key").read_bytes()
    records = _read_jsonl(inventory_root / "inventory.jsonl")
    locators = {
        str(row["document_id"]): str(row["relative_path"])
        for row in _read_jsonl(inventory_root / "private_locators.jsonl")
    }
    if len(locators) != len(records):
        raise InventorySourceChangedError("inventory locator set is incomplete")

    # Eligibility must be bound to the exact bytes inventoried in Phase 1.
    # Reusing the same path is insufficient: a replaced file would otherwise
    # receive a historical label tied to stale evidence and stale page hashes.
    for record in records:
        document_id = str(record["document_id"])
        relative = locators.get(document_id, "")
        source_path = (source / relative).resolve(strict=True)
        try:
            source_path.relative_to(source)
        except ValueError as exc:
            raise InventorySourceChangedError("inventory locator escaped source root") from exc
        if _sha256_file(source_path) != str(record.get("content_sha256") or ""):
            raise InventorySourceChangedError(
                "source bytes changed after the Phase 1 inventory"
            )

    from .gl_catalog import load_gl_catalog
    from .gl_payability import is_payable_gl_account
    from .resman_context_data import DatasetKind, dataset_status, list_all_effective_records

    _catalog_version, catalog = load_gl_catalog()
    history_rows = list_all_effective_records(historical_tenant_id, DatasetKind.INVOICE_HISTORY)
    history_status = dataset_status(historical_tenant_id, DatasetKind.INVOICE_HISTORY)
    by_occurrence: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in history_rows:
        by_occurrence[str(row.get("invoice_occurrence_id") or "")].append(row)
    candidate_index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for occurrence_id, rows in by_occurrence.items():
        gl_codes = {str(row.get("gl_code") or "").strip() for row in rows}
        properties = {str(row.get("property_code") or "").strip() for row in rows}
        vendor_name = str(rows[0].get("vendor_name") or "").strip() if rows else ""
        invoice_number = str(rows[0].get("invoice_number") or "").strip() if rows else ""
        reconciled_amounts = _history_allocations_reconcile(rows)
        if (
            not rows
            or any(row.get("invoice_reconciliation_status") != "reconciled" for row in rows)
            or len(gl_codes) != 1
            or not next(iter(gl_codes), "")
            or len(properties) != 1
            or not next(iter(properties), "")
            or not vendor_name
            or not invoice_number
            or not reconciled_amounts
            or not all(is_payable_gl_account(code, catalog) for code in gl_codes)
        ):
            continue
        expected_gl = next(iter(gl_codes))
        candidate_index[_invoice_value_fingerprint(salt, invoice_number)].append({
            "occurrence_id": occurrence_id,
            "invoice_number": invoice_number,
            "invoice_total": str(rows[0].get("invoice_total") or "").strip(),
            "vendor_name": vendor_name,
            "invoice_date": str(rows[0].get("invoice_date") or "").strip(),
            "expected_gl": expected_gl,
            "expected_property": next(iter(properties)),
            "canonical_accounting_family": _catalog_accounting_family(catalog[expected_gl]),
            "allocations": rows,
        })

    output: list[dict[str, Any]] = []
    for record in records:
        document_id = str(record["document_id"])
        status = str(record.get("visual_hash_status") or "")
        if status in {"error", "not_applicable", "unavailable"}:
            output.append(_eligibility_row(record, "unsuitable_for_learning_evaluation"))
            continue
        candidates: dict[str, dict[str, Any]] = {}
        for fingerprint in record.get("invoice_identity_fingerprints") or []:
            for candidate in candidate_index.get(str(fingerprint), []):
                candidates[candidate["occurrence_id"]] = candidate
        if candidates:
            relative = locators.get(document_id, "")
            source_path = (source / relative).resolve(strict=True)
            source_path.relative_to(source)
            observed_text = _local_embedded_text(source_path)
            scored = sorted(
                (
                    (_history_match_evidence(candidate, observed_text), candidate)
                    for candidate in candidates.values()
                ),
                key=lambda pair: (
                    int(pair[0]["invoice_total_visible"])
                    + int(pair[0]["vendor_text_visible"])
                    + int(pair[0]["invoice_date_visible"]),
                    pair[1]["occurrence_id"],
                ),
                reverse=True,
            )
            # An exact invoice identity plus a visible amount and an
            # independent vendor/date corroborator is the minimum automated
            # authority. A generic invoice-number collision or a coincidental
            # amount somewhere in a packet must remain unlabelled.
            defensible = [
                (evidence, candidate) for evidence, candidate in scored
                if evidence["invoice_total_visible"]
                and (evidence["vendor_text_visible"] or evidence["invoice_date_visible"])
            ]
            single_observed_identity = int(
                record.get("possible_invoice_group_count") or 0
            ) == 1
            if len(defensible) == 1 and single_observed_identity:
                evidence, candidate = defensible[0]
                output.append({
                    **_eligibility_row(record, "accepted_posted_resman_ground_truth"),
                    "ground_truth": {
                        "authority": "published_resman_invoice_history",
                        "history_snapshot_id": (
                            history_status.current_snapshot.snapshot_id
                            if history_status.current_snapshot else None
                        ),
                        "source_document_sha256": record["content_sha256"],
                        "source_visual_page_hashes": record.get("exact_visual_page_hashes") or [],
                        "observed_invoice_number": candidate["invoice_number"],
                        "observed_invoice_total": candidate["invoice_total"],
                        "observed_vendor": candidate["vendor_name"],
                        "expected_property": candidate["expected_property"],
                        "expected_gl": candidate["expected_gl"],
                        "acceptable_gl_alternatives": [],
                        "expected_allocations": [
                            {
                                "allocation_index": row.get("allocation_index"),
                                "amount": row.get("allocation_amount"),
                                "description": row.get("allocation_description"),
                                "gl_code": row.get("gl_code"),
                                "property_code": row.get("property_code"),
                            }
                            for row in candidate["allocations"]
                        ],
                        "canonical_accounting_family": candidate[
                            "canonical_accounting_family"
                        ],
                        "vendor_family_fingerprint": _hmac_hex(
                            salt, "vendor:" + candidate["vendor_name"].casefold()
                        ),
                        "property_family_fingerprint": _hmac_hex(
                            salt, "property:" + candidate["expected_property"].casefold()
                        ),
                        "invoice_identity_fingerprint": _hmac_hex(
                            salt,
                            "invoice:"
                            + candidate["vendor_name"].casefold()
                            + "|"
                            + candidate["invoice_number"].casefold(),
                        ),
                        "history_occurrence_id": candidate["occurrence_id"],
                        "match_evidence": {
                            "invoice_identity_exact": True,
                            **evidence,
                            "single_observed_invoice_identity": True,
                        },
                        "allocation_count": len(candidate["allocations"]),
                        "reconciliation_status": "reconciled",
                        "reconciliation_tolerance": "0.01",
                        "expected_export_allowed_if_facts_are_complete": True,
                        "label_confidence": "high",
                        "adjudication_status": "accepted_external_posted_history",
                    },
                })
                continue
        category = (
            "deterministically_reconcilable_requires_independent_gl_label"
            if record.get("deterministic_parser_status") == "active"
            else "requires_human_adjudication"
        )
        output.append(_eligibility_row(record, category))

    canonical_output = "".join(
        json.dumps(row, sort_keys=True) + "\n" for row in output
    )
    eligibility_sha256 = hashlib.sha256(canonical_output.encode("utf-8")).hexdigest()
    report_path = (
        runtime_root / "eligibility" / f"eligibility-{eligibility_sha256}.jsonl"
    )
    _write_jsonl_idempotent(report_path, output)
    eligibility_candidates = sorted(
        path.stem.removeprefix("eligibility-")
        for path in (runtime_root / "eligibility").glob("eligibility-*.jsonl")
    )
    _write_json_replace(
        runtime_root / "eligibility" / "active_eligibility.json",
        {
            "schema_version": "document-learning-eligibility-selection/1.0",
            "active_eligibility_sha256": eligibility_sha256,
            "candidates": [
                {
                    "eligibility_sha256": value,
                    "status": (
                        "accepted" if value == eligibility_sha256
                        else "rejected_superseded"
                    ),
                }
                for value in eligibility_candidates
            ],
        },
    )
    counts = Counter(str(row["eligibility_class"]) for row in output)
    safe = {
        "schema_version": "document-learning-eligibility-safe/1.0",
        "documents": len(output),
        "eligibility_counts": dict(sorted(counts.items())),
        "defensible_posted_ground_truth_documents": counts.get(
            "accepted_posted_resman_ground_truth", 0,
        ),
        "prior_ai_outputs_used_as_truth": 0,
        "provider_calls": 0,
        "network_calls": 0,
    }
    assert_git_safe_summary(safe)
    return EligibilityRunResult(report_path, safe)


def run_phase0_preflight(
    *, project_root: Path, source_root: Path, runtime_root: Path,
    experiment_id: str, expected_branch: str,
) -> PreflightRunResult:
    """Verify repository/privacy/spend gates without opening document bytes."""
    contract = validate_private_paths(
        project_root=project_root, source_root=source_root, runtime_root=runtime_root,
    )
    if not experiment_id.startswith("exp-"):
        raise ValueError("experiment_id must use the exp-* namespace")
    branch = _git(contract.project_root, "branch", "--show-current").strip()
    if branch != expected_branch:
        raise RuntimeError("experiment branch mismatch")
    commit_sha = _git(contract.project_root, "rev-parse", "HEAD").strip()
    status_rows = [
        row for row in _git(contract.project_root, "status", "--porcelain=v1").splitlines()
        if row.strip()
    ]
    tracked_private = [
        row for row in _git(
            contract.project_root, "ls-files", "--",
            "Document Data - Reasoning Training",
            "Document Data - Reasoning Training/**",
        ).splitlines() if row.strip()
    ]
    if tracked_private:
        raise RuntimeError("private source content is already tracked")
    ignore_text = (contract.project_root / ".gitignore").read_text(
        encoding="utf-8", errors="replace",
    )
    source_ignore_present = "/Document Data - Reasoning Training/" in {
        row.strip() for row in ignore_text.splitlines()
    }
    if not source_ignore_present:
        raise RuntimeError("defensive private source ignore rule is missing")

    contract.runtime_root.mkdir(parents=True, exist_ok=True)
    _assert_runtime_git_ignored(contract.project_root, contract.runtime_root)
    salt = _load_or_create_salt(contract.runtime_root / ".inventory_hmac_key")
    source_snapshot, source_files = _source_snapshot(contract.source_root, salt)
    profile_rows = _configured_profile_presence()

    spend = ExperimentSpendController(
        contract.runtime_root / "preflight" / "synthetic-spend",
        experiment_id + "-preflight",
    )
    reservation = spend.reserve(
        phase="A", estimated_cost_usd="0.001", provider="openai",
        model_id="synthetic-no-network", profile_id="synthetic-dry-run",
        stage="phase0_spend_controller_dry_run",
    )
    spend.release_reserved(reservation.reservation_id, reason="synthetic_no_network")
    phase_b_blocked = False
    try:
        spend.reserve(
            phase="B", estimated_cost_usd="0.001", provider="openai",
            model_id="synthetic-no-network", profile_id="synthetic-dry-run",
            stage="phase0_unauthorized_phase_check",
        )
    except SpendAuthorizationError:
        phase_b_blocked = True
    if not phase_b_blocked:
        raise RuntimeError("phase B spend gate did not fail closed")

    private_payload = {
        "schema_version": "document-learning-preflight/1.0",
        "experiment_id": experiment_id,
        "branch": branch,
        "commit_sha": commit_sha,
        "worktree_clean": not status_rows,
        "worktree_change_count": len(status_rows),
        "source_path_fingerprint": _hmac_hex(salt, str(contract.source_root)),
        "runtime_path_fingerprint": _hmac_hex(salt, str(contract.runtime_root)),
        "source_metadata_snapshot_sha256": source_snapshot,
        "physical_file_count": len(source_files),
        "private_source_tracked_count": 0,
        "defensive_source_ignore_present": True,
        "runtime_parent_ignored": True,
        "provider_profiles": profile_rows,
        "spend_dry_run": {
            "network_calls": 0,
            "phase_a_projection_usd": spend.snapshot("A").projected_usd,
            "phase_b_blocked_before_acceptance": phase_b_blocked,
        },
    }
    report_path = contract.runtime_root / "preflight" / "preflight.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(private_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    safe = {
        "schema_version": "document-learning-preflight-safe/1.0",
        "branch_verified": True,
        "worktree_clean": not status_rows,
        "worktree_change_count": len(status_rows),
        "physical_files_detected": len(source_files),
        "private_files_tracked": 0,
        "private_source_ignore_verified": True,
        "private_runtime_ignore_verified": True,
        "configured_profile_count": len(profile_rows),
        "credentialed_profile_count": sum(bool(row["credentials_present"]) for row in profile_rows),
        "priced_profile_count": sum(bool(row["pricing_present"]) for row in profile_rows),
        "synthetic_spend_gate_passed": phase_b_blocked,
        "network_calls": 0,
    }
    assert_git_safe_summary(safe)
    return PreflightRunResult(report_path, safe)


def validate_private_paths(
    *, project_root: Path, source_root: Path, runtime_root: Path
) -> PrivatePathContract:
    """Validate the read-only corpus and private generated-artifact roots.

    The corpus must be physically outside the repository.  Generated detailed
    artifacts must be beneath the repository's ignored ``tmp`` or
    ``webapp_data`` roots.  This separation prevents a recursive inventory from
    consuming its own outputs and makes accidental staging fail closed.
    """

    project = project_root.expanduser().resolve(strict=True)
    source = source_root.expanduser().resolve(strict=True)
    if not source.is_dir():
        raise ExperimentPathError("source_root must be an existing directory")
    if _is_relative_to(source, project):
        raise ExperimentPathError("source_root must be outside the repository")

    runtime = runtime_root.expanduser().resolve(strict=False)
    allowed_parent: Path | None = None
    for directory in _ALLOWED_PRIVATE_RUNTIME_DIRS:
        candidate = (project / directory).resolve(strict=False)
        if _is_relative_to(runtime, candidate) and runtime != candidate:
            allowed_parent = candidate
            break
    if allowed_parent is None:
        raise ExperimentPathError(
            "runtime_root must be a child of the repository tmp or webapp_data directory"
        )
    if not _ignore_rule_present(project, allowed_parent.name):
        raise ExperimentPathError(
            f"private runtime parent {allowed_parent.name!r} is not declared ignored"
        )
    if _is_relative_to(source, runtime) or _is_relative_to(runtime, source):
        raise ExperimentPathError("source_root and runtime_root must not overlap")
    return PrivatePathContract(project, source, runtime)


def inventory_local_corpus(
    *,
    project_root: Path,
    source_root: Path,
    runtime_root: Path,
    split_seed: str = "document-learning-split-v1",
    split_ratios: Mapping[str, float] = DEFAULT_SPLIT_RATIOS,
) -> InventoryRunResult:
    """Inventory a private corpus without network or model calls.

    The detailed snapshot contains sensitive equality hashes and a private
    relative-path locator, so it is always written below ``runtime_root``.
    Callers may persist only ``git_safe_summary`` outside that boundary.
    """

    contract = validate_private_paths(
        project_root=project_root,
        source_root=source_root,
        runtime_root=runtime_root,
    )
    contract.runtime_root.mkdir(parents=True, exist_ok=True)
    salt = _load_or_create_salt(contract.runtime_root / ".inventory_hmac_key")
    before, files = _source_snapshot(contract.source_root, salt)

    records: list[dict[str, Any]] = []
    locators: list[dict[str, str]] = []
    parser_identities = _deterministic_parser_identities()
    for path in files:
        relative = path.relative_to(contract.source_root).as_posix()
        record = inspect_local_document(
            path=path, relative_path=relative, salt=salt,
            parser_identities=parser_identities,
        )
        records.append(record)
        locators.append({"document_id": record["document_id"], "relative_path": relative})

    after, _ = _source_snapshot(contract.source_root, salt)
    if before != after:
        raise InventorySourceChangedError("source corpus changed during inventory")

    groups = build_exact_leakage_groups(records)
    dataset_version = _dataset_version(records, groups)
    # Eligibility and the five-way experiment split belong to Phases 2–3.
    # Phase 1 persists exact duplicate groups only; it never exposes holdouts
    # or creates an accidental training assignment before ground truth exists.
    summary = build_git_safe_summary(records, groups, split_manifest=None)
    assert_git_safe_summary(summary)

    snapshot_root = contract.runtime_root / "snapshots" / dataset_version
    _persist_private_snapshot(
        snapshot_root=snapshot_root,
        records=records,
        locators=locators,
        groups=groups,
        split_manifest=None,
        safe_summary=summary,
    )
    return InventoryRunResult(dataset_version, snapshot_root, summary)


def inspect_local_document(
    *, path: Path, relative_path: str, salt: bytes,
    parser_identities: Mapping[str, tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Return content identities without retaining filename or source text."""

    content_sha256 = _sha256_file(path)
    document_id = "doc-" + _hmac_hex(salt, relative_path)[:24]
    extension = path.suffix.lower()
    stat = path.stat()
    page_result = _exact_page_facts(path, extension, salt=salt)
    parser = _match_deterministic_parser(relative_path, parser_identities or {})
    return {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "document_id": document_id,
        "source_locator_token": "loc-" + _hmac_hex(salt, "locator:" + relative_path)[:24],
        "content_sha256": content_sha256,
        "extension": extension or "[none]",
        "format_family": _format_family(extension),
        "size_bytes": stat.st_size,
        "page_count": page_result["page_count"],
        "page_geometry": page_result["page_geometry"],
        "page_pixel_dimensions": page_result["page_pixel_dimensions"],
        "page_rotations": page_result["page_rotations"],
        "exact_visual_page_hashes": page_result["page_hashes"],
        "layout_family_fingerprints": page_result["layout_family_fingerprints"],
        "embedded_text_available": page_result["embedded_text_available"],
        "embedded_text_page_count": page_result["embedded_text_page_count"],
        "probable_media_kind": page_result["probable_media_kind"],
        "invoice_identity_fingerprints": page_result["invoice_identity_fingerprints"],
        "possible_invoice_group_count": page_result["possible_invoice_group_count"],
        "appears_multi_invoice": page_result["appears_multi_invoice"],
        "financial_value_fingerprint": page_result["financial_value_fingerprint"],
        "metadata_family_fingerprint": "meta-" + _hmac_hex(
            salt, _metadata_family_basis(relative_path),
        )[:24],
        "deterministic_parser_status": parser[1] if parser else "not_detected_from_metadata",
        "deterministic_parser_fingerprint": (
            "parser-" + _hmac_hex(salt, parser[0])[:24] if parser else None
        ),
        "visual_hash_status": page_result["status"],
        "visual_hash_error_code": page_result["error_code"],
        "visual_hash_version": VISUAL_HASH_VERSION,
        "visual_renderer": page_result["renderer"],
    }


def build_exact_leakage_groups(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Group only exact byte or exact decoded-page matches.

    Sharing any exact page joins two documents.  This is intentionally strict:
    a repeated cover/detail page is enough to make cross-split leakage
    plausible.  No perceptual or catalog-derived similarity is used.
    """

    ids = sorted(str(record["document_id"]) for record in records)
    parent = {document_id: document_id for document_id in ids}
    reasons: dict[tuple[str, str], set[str]] = defaultdict(set)

    def find(value: str) -> str:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: str, right: str, reason: str) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            if root_left > root_right:
                root_left, root_right = root_right, root_left
            parent[root_right] = root_left
        reasons[tuple(sorted((left, right)))].add(reason)

    by_file_hash: dict[str, list[str]] = defaultdict(list)
    by_page_hash: dict[str, list[str]] = defaultdict(list)
    for record in records:
        document_id = str(record["document_id"])
        by_file_hash[str(record["content_sha256"])].append(document_id)
        for page_hash in record.get("exact_visual_page_hashes") or ():
            by_page_hash[str(page_hash)].append(document_id)
    for members in by_file_hash.values():
        _union_members(members, "exact_file_sha256", union)
    for members in by_page_hash.values():
        _union_members(members, "exact_visual_page_sha256", union)

    components: dict[str, list[str]] = defaultdict(list)
    for document_id in ids:
        components[find(document_id)].append(document_id)
    output: list[dict[str, Any]] = []
    for members in sorted((sorted(value) for value in components.values()), key=lambda row: row[0]):
        member_set = set(members)
        reason_codes = sorted(
            reason
            for pair, pair_reasons in reasons.items()
            if set(pair).issubset(member_set)
            for reason in pair_reasons
        )
        basis = "\0".join(members)
        output.append(
            {
                "group_id": "leak-" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24],
                "members": members,
                "member_count": len(members),
                "reason_codes": sorted(set(reason_codes)) or ["singleton"],
                "match_policy": "exact_only",
            }
        )
    return output


def deterministic_group_split(
    *,
    records: Sequence[Mapping[str, Any]],
    groups: Sequence[Mapping[str, Any]],
    dataset_version: str,
    seed: str,
    ratios: Mapping[str, float] = DEFAULT_SPLIT_RATIOS,
) -> dict[str, Any]:
    """Assign whole exact-leakage groups to deterministic dataset partitions."""

    normalized_ratios = _validate_ratios(ratios)
    record_ids = {str(record["document_id"]) for record in records}
    grouped_ids = [str(item) for group in groups for item in group["members"]]
    if set(grouped_ids) != record_ids or len(grouped_ids) != len(set(grouped_ids)):
        raise ValueError("leakage groups must cover every document exactly once")

    total = len(records)
    split_names = tuple(normalized_ratios)
    targets = {
        name: int(math.floor(total * normalized_ratios[name])) for name in split_names[:-1]
    }
    targets[split_names[-1]] = total - sum(targets.values())
    counts = {name: 0 for name in split_names}
    assignments: list[dict[str, Any]] = []
    ordered_groups = sorted(
        groups,
        key=lambda group: hashlib.sha256(
            f"{seed}\0{dataset_version}\0{group['group_id']}".encode("utf-8")
        ).hexdigest(),
    )
    for group in ordered_groups:
        size = int(group["member_count"])
        split = max(
            split_names,
            key=lambda name: (
                targets[name] - counts[name],
                -counts[name],
                -split_names.index(name),
            ),
        )
        counts[split] += size
        assignments.append(
            {
                "group_id": group["group_id"],
                "split": split,
                "document_ids": list(group["members"]),
                "document_count": size,
            }
        )
    return {
        "schema_version": SPLIT_SCHEMA_VERSION,
        "dataset_version": dataset_version,
        "seed_fingerprint": hashlib.sha256(seed.encode("utf-8")).hexdigest(),
        "ratios": dict(normalized_ratios),
        "target_document_counts": targets,
        "actual_document_counts": counts,
        "leakage_policy": "exact_file_or_exact_visual_page_grouped_before_split",
        "assignments": assignments,
    }


def build_git_safe_summary(
    records: Sequence[Mapping[str, Any]],
    groups: Sequence[Mapping[str, Any]],
    split_manifest: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build aggregate metadata safe to include in a repository report."""

    visual_status = Counter(str(record["visual_hash_status"]) for record in records)
    duplicate_groups = sum(int(group["member_count"]) > 1 for group in groups)
    duplicate_documents = sum(
        int(group["member_count"]) for group in groups if int(group["member_count"]) > 1
    )
    file_counts = Counter(str(record["content_sha256"]) for record in records)
    page_documents: dict[str, set[str]] = defaultdict(set)
    invoice_documents: dict[str, set[str]] = defaultdict(set)
    for record in records:
        document_id = str(record["document_id"])
        for page_hash in set(record.get("exact_visual_page_hashes") or []):
            page_documents[str(page_hash)].add(document_id)
        for identity in set(record.get("invoice_identity_fingerprints") or []):
            invoice_documents[str(identity)].add(document_id)
    related_invoice_groups = [members for members in invoice_documents.values() if len(members) > 1]
    estimated_unique_invoices = sum(
        max(1, max(
            int(next(
                record.get("possible_invoice_group_count") or 0
                for record in records if str(record["document_id"]) == member
            ))
            for member in group["members"]
        ))
        for group in groups
    )
    summary = {
        "schema_version": "document-learning-safe-summary/1.0",
        "documents": len(records),
        "unique_exact_files": len(file_counts),
        "bytes_total": sum(int(record["size_bytes"]) for record in records),
        "pages_total": sum(int(record.get("page_count") or 0) for record in records),
        "unique_exact_visual_pages": len(page_documents),
        "estimated_unique_invoices": estimated_unique_invoices,
        "format_family_counts": dict(
            sorted(Counter(str(record["format_family"]) for record in records).items())
        ),
        "extension_counts": dict(
            sorted(Counter(str(record["extension"]) for record in records).items())
        ),
        "visual_identity_status_counts": dict(sorted(visual_status.items())),
        "media_kind_counts": dict(sorted(Counter(
            str(record.get("probable_media_kind") or "unknown") for record in records
        ).items())),
        "single_page_documents": sum(int(record.get("page_count") or 0) == 1 for record in records),
        "multi_page_documents": sum(int(record.get("page_count") or 0) > 1 for record in records),
        "embedded_text_documents": sum(bool(record.get("embedded_text_available")) for record in records),
        "possible_multi_invoice_documents": sum(bool(record.get("appears_multi_invoice")) for record in records),
        "exact_file_duplicate_groups": sum(count > 1 for count in file_counts.values()),
        "exact_visual_page_duplicate_groups": sum(
            len(members) > 1 for members in page_documents.values()
        ),
        "exact_duplicate_groups": duplicate_groups,
        "documents_in_exact_leakage_groups": duplicate_documents,
        "related_invoice_version_candidate_groups": len(related_invoice_groups),
        "documents_in_related_invoice_candidates": len(set().union(*related_invoice_groups)) if related_invoice_groups else 0,
        "deterministic_parser_status_counts": dict(sorted(Counter(
            str(record.get("deterministic_parser_status") or "unknown") for record in records
        ).items())),
        "deterministic_parser_detected_documents": sum(
            str(record.get("deterministic_parser_status") or "").startswith("active")
            for record in records
        ),
        "metadata_family_count": len({
            str(record.get("metadata_family_fingerprint")) for record in records
            if record.get("metadata_family_fingerprint")
        }),
        "layout_family_count": len({
            str(value) for record in records
            for value in (record.get("layout_family_fingerprints") or [])
        }),
        "corrupt_or_unreadable_documents": sum(
            str(record.get("visual_hash_status")) == "error" for record in records
        ),
        "unsupported_documents": sum(
            str(record.get("visual_hash_status")) == "not_applicable" for record in records
        ),
        "network_calls": 0,
        "provider_calls": 0,
        "source_mutations": 0,
        "detailed_artifacts_private_only": True,
        "approximate_similarity_used_for_split": False,
        "final_learning_split_created": split_manifest is not None,
    }
    if split_manifest is not None:
        summary["split_document_counts"] = dict(split_manifest["actual_document_counts"])
    assert_git_safe_summary(summary)
    return summary


def assert_git_safe_summary(value: Any, *, _key: str = "") -> None:
    """Fail closed if a supposedly aggregate report gains a private field."""

    if isinstance(value, Mapping):
        for key, nested in value.items():
            lowered = str(key).lower()
            if any(part in lowered for part in _FORBIDDEN_SAFE_KEY_PARTS):
                raise ValueError(f"Git-safe summary contains forbidden field: {key}")
            assert_git_safe_summary(nested, _key=lowered)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            assert_git_safe_summary(nested, _key=_key)
    elif isinstance(value, str):
        if "\\" in value or ":/" in value or value.startswith("/"):
            raise ValueError("Git-safe summary contains a path-like value")


def render_git_safe_summary(summary: Mapping[str, Any]) -> str:
    """Render the already-validated aggregate summary as Markdown."""

    assert_git_safe_summary(summary)

    def rows(values: Mapping[str, Any]) -> str:
        return "\n".join(f"| {key} | {value} |" for key, value in sorted(values.items()))

    split_section = ""
    if summary.get("split_document_counts"):
        split_section = (
            "## Split\n\n| Split | Documents |\n|---|---:|\n"
            + rows(summary["split_document_counts"])
        )

    return f"""# Document Learning Corpus Inventory

This report contains aggregate metadata only. Detailed manifests, equality
hashes, private locators, document contents, crops, and provider responses are
excluded and remain under the ignored private runtime root.

- Documents: {summary['documents']}
- Pages: {summary['pages_total']}
- Bytes: {summary['bytes_total']}
- Exact duplicate groups: {summary['exact_duplicate_groups']}
- Documents in exact leakage groups: {summary['documents_in_exact_leakage_groups']}
- Network calls: 0
- Provider calls: 0
- Source mutations: 0
- Approximate similarity used for split: no
- Final learning split created: {'yes' if summary['final_learning_split_created'] else 'no'}

## Format families

| Family | Count |
|---|---:|
{rows(summary['format_family_counts'])}

{split_section}
"""


def _exact_page_facts(path: Path, extension: str, *, salt: bytes) -> dict[str, Any]:
    if extension == ".pdf":
        return _pdf_page_facts(path, salt=salt)
    if extension in IMAGE_EXTENSIONS:
        return _image_page_facts(path)
    return _empty_page_facts(
        status="not_applicable", error_code=None, renderer=None,
    )


def _empty_page_facts(
    *, status: str, error_code: str | None, renderer: str | None,
) -> dict[str, Any]:
    return {
        "page_count": None,
        "page_geometry": [],
        "page_pixel_dimensions": [],
        "page_rotations": [],
        "page_hashes": [],
        "layout_family_fingerprints": [],
        "embedded_text_available": False,
        "embedded_text_page_count": 0,
        "probable_media_kind": "unsupported",
        "invoice_identity_fingerprints": [],
        "possible_invoice_group_count": 0,
        "appears_multi_invoice": False,
        "financial_value_fingerprint": None,
        "status": status,
        "error_code": error_code,
        "renderer": renderer,
    }


def _pdf_page_facts(path: Path, *, salt: bytes) -> dict[str, Any]:
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return _empty_page_facts(
            status="unavailable", error_code="pdf_renderer_unavailable", renderer=None,
        )
    try:
        document = pdfium.PdfDocument(str(path))
        geometries: list[list[float]] = []
        pixel_dimensions: list[list[int]] = []
        rotations: list[int] = []
        hashes: list[str] = []
        layout_hashes: list[str] = []
        page_texts: list[str] = []
        for page_number in range(len(document)):
            page = document[page_number]
            width, height = page.get_size()
            image = page.render(scale=1.5, rotation=0).to_pil().convert("RGB")
            geometries.append([round(float(width), 4), round(float(height), 4)])
            pixel_dimensions.append([int(image.width), int(image.height)])
            try:
                rotations.append(int(page.get_rotation() or 0))
            except (AttributeError, TypeError, ValueError):
                rotations.append(0)
            hashes.append(_decoded_pixel_hash(image, geometries[-1]))
            layout_hashes.append(_coarse_layout_hash(image))
            try:
                text_page = page.get_textpage()
                page_texts.append(str(text_page.get_text_range() or ""))
                text_page.close()
            except Exception:
                page_texts.append("")
            image.close()
            page.close()
        document.close()
        try:
            renderer_version = metadata.version("pypdfium2")
        except metadata.PackageNotFoundError:
            renderer_version = "unknown"
        text_page_count = sum(len(_normalized_local_text(text)) >= 20 for text in page_texts)
        identity_values = sorted({
            value for text in page_texts for value in _invoice_identity_candidates(text)
        })
        amount_values = sorted({
            value for text in page_texts for value in _financial_amount_candidates(text)
        })
        if not page_texts:
            media_kind = "unknown"
        elif text_page_count == len(page_texts):
            media_kind = "digital"
        elif text_page_count == 0:
            media_kind = "scanned"
        else:
            media_kind = "mixed"
        return {
            "page_count": len(hashes),
            "page_geometry": geometries,
            "page_pixel_dimensions": pixel_dimensions,
            "page_rotations": rotations,
            "page_hashes": hashes,
            "layout_family_fingerprints": layout_hashes,
            "embedded_text_available": text_page_count > 0,
            "embedded_text_page_count": text_page_count,
            "probable_media_kind": media_kind,
            "invoice_identity_fingerprints": [
                "inv-" + _hmac_hex(salt, value)[:24] for value in identity_values
            ],
            "possible_invoice_group_count": len(identity_values),
            "appears_multi_invoice": len(identity_values) > 1,
            "financial_value_fingerprint": (
                "amt-" + _hmac_hex(salt, "|".join(amount_values))[:24]
                if amount_values else None
            ),
            "status": "verified",
            "error_code": None,
            "renderer": f"pypdfium2/{renderer_version}@108dpi",
        }
    except Exception as exc:  # detailed provider/file content is intentionally excluded
        return _empty_page_facts(
            status="error", error_code=type(exc).__name__, renderer="pypdfium2@108dpi",
        )


def _image_page_facts(path: Path) -> dict[str, Any]:
    try:
        from PIL import Image, ImageOps, ImageSequence

        geometries: list[list[float]] = []
        pixel_dimensions: list[list[int]] = []
        rotations: list[int] = []
        hashes: list[str] = []
        layout_hashes: list[str] = []
        with Image.open(path) as source:
            for frame in ImageSequence.Iterator(source):
                image = ImageOps.exif_transpose(frame).convert("RGB")
                geometry = [float(image.width), float(image.height)]
                geometries.append(geometry)
                pixel_dimensions.append([int(image.width), int(image.height)])
                rotations.append(_image_exif_rotation(frame))
                hashes.append(_decoded_pixel_hash(image, geometry))
                layout_hashes.append(_coarse_layout_hash(image))
        return {
            "page_count": len(hashes),
            "page_geometry": geometries,
            "page_pixel_dimensions": pixel_dimensions,
            "page_rotations": rotations,
            "page_hashes": hashes,
            "layout_family_fingerprints": layout_hashes,
            "embedded_text_available": False,
            "embedded_text_page_count": 0,
            "probable_media_kind": "scan_or_photo",
            "invoice_identity_fingerprints": [],
            "possible_invoice_group_count": 0,
            "appears_multi_invoice": False,
            "financial_value_fingerprint": None,
            "status": "verified",
            "error_code": None,
            "renderer": "pillow-decoder",
        }
    except Exception as exc:
        return _empty_page_facts(
            status="error", error_code=type(exc).__name__, renderer="pillow-decoder",
        )


def _decoded_pixel_hash(image: Any, geometry: Sequence[float]) -> str:
    digest = hashlib.sha256()
    digest.update(VISUAL_HASH_VERSION.encode("ascii"))
    digest.update(json.dumps(list(geometry), separators=(",", ":")).encode("ascii"))
    digest.update(f"{image.mode}:{image.width}x{image.height}".encode("ascii"))
    digest.update(image.tobytes())
    return digest.hexdigest()


def _coarse_layout_hash(image: Any) -> str:
    """Analysis-only layout family key; never authorizes fact reuse."""
    from PIL import ImageOps

    grayscale = ImageOps.autocontrast(ImageOps.grayscale(image)).resize((16, 16))
    values = list(
        grayscale.get_flattened_data()
        if hasattr(grayscale, "get_flattened_data") else grayscale.getdata()
    )
    average = sum(values) / max(1, len(values))
    bits = bytes(
        sum((1 << bit) if values[offset + bit] < average else 0 for bit in range(8))
        for offset in range(0, 256, 8)
    )
    aspect_bucket = round(float(image.width) / max(1.0, float(image.height)), 2)
    return "layout-" + hashlib.sha256(
        f"analysis-only:{aspect_bucket}:".encode("ascii") + bits
    ).hexdigest()[:24]


def _normalized_local_text(value: str) -> str:
    return " ".join(str(value or "").replace("\x00", " ").split())


def _invoice_identity_candidates(text: str) -> list[str]:
    normalized = _normalized_local_text(text)
    pattern = re.compile(
        r"(?i)\b(?:invoice|inv\.?|bill)\s*(?:number|no\.?|#)?\s*[:#-]?\s*"
        r"([A-Z0-9][A-Z0-9./-]{2,31})"
    )
    excluded = {"date", "total", "amount", "number", "no", "due"}
    return sorted({
        match.group(1).strip(" .:/-").casefold()
        for match in pattern.finditer(normalized)
        if match.group(1).strip(" .:/-").casefold() not in excluded
    })


def _financial_amount_candidates(text: str) -> list[str]:
    normalized = _normalized_local_text(text)
    values = re.findall(r"(?<!\w)\$?\s*(-?\d{1,3}(?:,\d{3})*\.\d{2})(?!\w)", normalized)
    return sorted({value.replace(",", "") for value in values})


def _image_exif_rotation(frame: Any) -> int:
    try:
        orientation = int(frame.getexif().get(274) or 1)
    except Exception:
        return 0
    return {3: 180, 6: 90, 8: 270}.get(orientation, 0)


def _metadata_family_basis(relative_path: str) -> str:
    path = Path(relative_path)
    parts = [*path.parts[-3:-1], path.stem]
    normalized = []
    for part in parts:
        value = re.sub(r"\d+", "#", str(part).casefold())
        value = re.sub(r"[^a-z#]+", " ", value)
        normalized.append(" ".join(value.split()))
    return "|".join(normalized)


def _invoice_value_fingerprint(salt: bytes, value: str) -> str:
    normalized = str(value or "").strip(" .:/-").casefold()
    return "inv-" + _hmac_hex(salt, normalized)[:24]


def _local_embedded_text(path: Path) -> str:
    if path.suffix.casefold() != ".pdf":
        return ""
    try:
        import pypdfium2 as pdfium
        document = pdfium.PdfDocument(str(path))
        texts: list[str] = []
        for page_number in range(len(document)):
            page = document[page_number]
            text_page = page.get_textpage()
            texts.append(str(text_page.get_text_range() or ""))
            text_page.close()
            page.close()
        document.close()
        return "\n".join(texts)
    except Exception:
        return ""


def _history_match_evidence(
    candidate: Mapping[str, Any], observed_text: str,
) -> dict[str, Any]:
    total = _amount_visible(observed_text, candidate.get("invoice_total"))
    vendor = _text_contains_identity(observed_text, candidate.get("vendor_name"))
    invoice_date = _date_visible(observed_text, candidate.get("invoice_date"))
    return {
        "invoice_total_visible": total,
        "vendor_text_visible": vendor,
        "invoice_date_visible": invoice_date,
        "match_score": 1 + int(total) + int(vendor) + int(invoice_date),
    }


def _history_allocations_reconcile(
    rows: Sequence[Mapping[str, Any]], *, tolerance: Decimal = Decimal("0.01"),
) -> bool:
    if not rows:
        return False
    try:
        total = _decimal_amount(rows[0].get("invoice_total"))
        allocations = sum(
            (_decimal_amount(row.get("allocation_amount")) for row in rows),
            Decimal("0"),
        )
    except (InvalidOperation, TypeError, ValueError):
        return False
    return abs(total - allocations) <= tolerance


def _decimal_amount(value: Any) -> Decimal:
    text = str(value if value is not None else "").replace(",", "").replace("$", "").strip()
    if not text:
        raise InvalidOperation
    result = Decimal(text)
    if not result.is_finite():
        raise InvalidOperation
    return result


def _catalog_accounting_family(metadata_value: Any) -> str:
    payload = {
        "gl_family": str(getattr(metadata_value, "gl_family", "") or "unknown"),
        "trade_families": sorted(
            str(item) for item in (getattr(metadata_value, "trade_families", None) or [])
        ),
        "compatible_work_modes": sorted(
            str(item)
            for item in (getattr(metadata_value, "compatible_work_modes", None) or [])
        ),
        "capital_context": str(
            getattr(metadata_value, "capital_context", "") or "unknown"
        ),
        "specificity": str(getattr(metadata_value, "specificity", "") or "unknown"),
    }
    return "accounting-family-" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]


def _amount_visible(observed_text: str, expected: Any) -> bool:
    try:
        target = Decimal(str(expected or "").replace(",", "").replace("$", "").strip())
    except (InvalidOperation, ValueError):
        return False
    for value in _financial_amount_candidates(observed_text):
        try:
            if Decimal(value) == target:
                return True
        except InvalidOperation:
            continue
    return False


def _text_contains_identity(observed_text: str, expected: Any) -> bool:
    expected_norm = re.sub(r"[^a-z0-9]+", "", str(expected or "").casefold())
    observed_norm = re.sub(r"[^a-z0-9]+", "", str(observed_text or "").casefold())
    return bool(expected_norm and len(expected_norm) >= 4 and expected_norm in observed_norm)


def _date_visible(observed_text: str, expected: Any) -> bool:
    raw = str(expected or "").strip()
    if not raw:
        return False
    candidates = {raw.casefold()}
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        candidates.update({
            parsed.strftime("%Y-%m-%d").casefold(),
            parsed.strftime("%m/%d/%Y").casefold(),
            parsed.strftime("%m/%d/%y").casefold(),
            parsed.strftime("%-m/%-d/%Y").casefold() if os.name != "nt" else "",
            f"{parsed.month}/{parsed.day}/{parsed.year}".casefold(),
        })
    observed = str(observed_text or "").casefold()
    return any(value and value in observed for value in candidates)


def _eligibility_row(record: Mapping[str, Any], category: str) -> dict[str, Any]:
    return {
        "schema_version": "document-learning-eligibility/1.1",
        "document_id": record["document_id"],
        "source_document_sha256": record["content_sha256"],
        "eligibility_class": category,
        "ground_truth": None,
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _deterministic_parser_identities() -> dict[str, tuple[str, str]]:
    try:
        from .deterministic_coverage import inventory
        rows = inventory()
    except Exception:
        return {}
    candidates: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for item in rows:
        status = "active" if item.status == "active" and item.processor_available else item.status
        for identity in [item.vendor_key, item.display_name, *item.aliases]:
            normalized = re.sub(r"[^a-z0-9]+", "", str(identity or "").casefold())
            if normalized:
                candidates[normalized].add((item.vendor_key, status))
    return {
        identity: next(iter(matches))
        for identity, matches in candidates.items() if len(matches) == 1
    }


def _calibration_stratum(record: Mapping[str, Any]) -> str:
    return "|".join([
        str(record.get("probable_media_kind") or "unknown"),
        "parser" if record.get("deterministic_parser_status") == "active" else "ai_fallback",
        "multi_page" if int(record.get("page_count") or 0) > 1 else "single_page",
        "packet" if record.get("appears_multi_invoice") else "single_invoice",
    ])


def _calibration_stratum_priority(value: str) -> int:
    # Rare/cost-sensitive visual and deterministic routes are sampled first;
    # the round-robin still keeps digital/unknown coverage in the same sample.
    if "scanned" in value or "image_scan_or_photo" in value:
        return 0
    if "|parser|" in f"|{value}|":
        return 1
    if "multi_page" in value:
        return 2
    return 3


def _calibration_priority(
    record: Mapping[str, Any], seed: str, document_id: str,
) -> tuple[Any, ...]:
    return (
        _calibration_stratum_priority(_calibration_stratum(record)),
        int(record.get("page_count") or 0),
        hashlib.sha256(f"{seed}:{document_id}".encode("utf-8")).hexdigest(),
    )


def _match_deterministic_parser(
    relative_path: str, identities: Mapping[str, tuple[str, str]],
) -> tuple[str, str] | None:
    path = Path(relative_path)
    for value in [*reversed(path.parts[:-1]), path.stem]:
        normalized = re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())
        if normalized in identities:
            return identities[normalized]
    return None


def _eligible_evaluation_units(
    records: Mapping[str, Mapping[str, Any]],
    eligibility: Mapping[str, Mapping[str, Any]],
    *, seed: str,
) -> list[dict[str, Any]]:
    ids = sorted(set(records).intersection(eligibility))
    parent = {document_id: document_id for document_id in ids}

    def find(value: str) -> str:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: str, right: str) -> None:
        a, b = find(left), find(right)
        if a != b:
            if a > b:
                a, b = b, a
            parent[b] = a

    # Collapse only byte- or pixel-exact copies. A modified financial variant
    # remains a separate unit, but receives the same leakage_component_id so
    # the split cannot place it in a different cohort.
    indexes: list[dict[str, list[str]]] = [defaultdict(list) for _ in range(2)]
    for document_id in ids:
        record = records[document_id]
        indexes[0][str(record.get("content_sha256") or "")].append(document_id)
        for value in record.get("exact_visual_page_hashes") or []:
            indexes[1][str(value)].append(document_id)
    for index in indexes:
        for key, members in index.items():
            if not key or len(members) < 2:
                continue
            for member in members[1:]:
                union(members[0], member)
    components: dict[str, list[str]] = defaultdict(list)
    for document_id in ids:
        components[find(document_id)].append(document_id)

    units: list[dict[str, Any]] = []
    for members in components.values():
        members = sorted(members)
        accounting_signatures = {
            (
                str((eligibility[item].get("ground_truth") or {}).get("expected_gl") or ""),
                str((eligibility[item].get("ground_truth") or {}).get("expected_property") or ""),
            )
            for item in members
        }
        if len(accounting_signatures) != 1:
            # Conflicting posted outcomes make the component ineligible for an
            # automated golden split; they belong in human adjudication.
            continue
        representative = min(
            members,
            key=lambda item: (
                str(records[item].get("probable_media_kind")) != "digital",
                str(records[item].get("deterministic_parser_status")) != "active",
                hashlib.sha256(f"{seed}:{item}".encode("utf-8")).hexdigest(),
            ),
        )
        record = records[representative]
        ground_truth = eligibility[representative]["ground_truth"]
        semantic_family = str(
            ground_truth.get("canonical_accounting_family")
            or "accounting-family-unknown"
        )
        variant_identity = str(
            ground_truth.get("invoice_identity_fingerprint")
            or ground_truth.get("history_occurrence_id")
            or representative
        )
        unit_id = "unit-" + hashlib.sha256("\0".join(members).encode("utf-8")).hexdigest()[:24]
        page_count = int(record.get("page_count") or 0)
        units.append({
            "unit_id": unit_id,
            "representative_document_id": representative,
            "linked_document_ids": members,
            "leakage_component_id": "linked-" + hashlib.sha256(
                variant_identity.encode("utf-8")
            ).hexdigest()[:24],
            "semantic_family": semantic_family,
            "vendor_family": str(
                ground_truth.get("vendor_family_fingerprint") or "vendor-unknown"
            ),
            "property_family": str(
                ground_truth.get("property_family_fingerprint") or "property-unknown"
            ),
            "layout_families": sorted(
                str(value) for value in (record.get("layout_family_fingerprints") or [])
            ),
            "metadata_family": str(
                record.get("metadata_family_fingerprint") or "metadata-unknown"
            ),
            "stratum": "|".join([
                str(record.get("probable_media_kind") or "unknown"),
                "parser" if record.get("deterministic_parser_status") == "active" else "ai_fallback",
                "multi_page" if page_count > 1 else "single_page",
                "packet" if record.get("appears_multi_invoice") else "single_invoice",
            ]),
        })
    return sorted(units, key=lambda item: item["unit_id"])


def _stratified_unit_selection(
    units: Sequence[Mapping[str, Any]], *, seed: str, limit: int,
) -> list[dict[str, Any]]:
    bundles_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw in units:
        item = dict(raw)
        bundles_by_id[str(item["leakage_component_id"])].append(item)
    buckets: dict[str, list[list[dict[str, Any]]]] = defaultdict(list)
    for bundle in bundles_by_id.values():
        bundle.sort(key=lambda item: item["unit_id"])
        key = "||".join(sorted({str(item["stratum"]) for item in bundle}))
        buckets[key].append(bundle)
    for key, bundles in buckets.items():
        bundles.sort(key=lambda bundle: hashlib.sha256(
            f"{seed}:{key}:{bundle[0]['leakage_component_id']}".encode("utf-8")
        ).hexdigest())
    selected: list[dict[str, Any]] = []
    keys = sorted(buckets, key=lambda key: hashlib.sha256(
        f"{seed}:stratum:{key}".encode("utf-8")
    ).hexdigest())
    while len(selected) < min(limit, len(units)):
        progressed = False
        for key in keys:
            if buckets[key] and len(selected) < limit:
                bundle = buckets[key].pop(0)
                if len(selected) + len(bundle) <= limit:
                    selected.extend(bundle)
                    progressed = True
        if not progressed:
            break
    return selected


def _assign_five_arms(
    selected: Sequence[Mapping[str, Any]], *, seed: str,
) -> list[dict[str, Any]]:
    count = len(selected)
    targets = {
        "training": round(count * 0.40),
        "similar_holdout": round(count * 0.20),
        "unrelated_holdout": round(count * 0.20),
        "benchmark_only": round(count * 0.10),
    }
    targets["rule_simulation"] = count - sum(targets.values())
    leakage_bundles: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw in selected:
        leakage_bundles[str(raw["leakage_component_id"])].append(dict(raw))
    by_family: dict[str, list[list[dict[str, Any]]]] = defaultdict(list)
    for bundle in leakage_bundles.values():
        bundle.sort(key=lambda item: item["unit_id"])
        family = "+".join(sorted({str(item["semantic_family"]) for item in bundle}))
        by_family[family].append(bundle)
    for family, bundles in by_family.items():
        bundles.sort(key=lambda bundle: hashlib.sha256(
            f"{seed}:{family}:{bundle[0]['leakage_component_id']}".encode("utf-8")
        ).hexdigest())

    family_order = sorted(by_family, key=lambda family: (
        sum(len(bundle) for bundle in by_family[family]),
        hashlib.sha256(f"{seed}:unrelated:{family}".encode("utf-8")).hexdigest(),
    ))
    unrelated_families: set[str] = set()
    unrelated_count = 0
    for family in family_order:
        size = sum(len(bundle) for bundle in by_family[family])
        if unrelated_count >= targets["unrelated_holdout"]:
            break
        if unrelated_count + size <= targets["unrelated_holdout"] or not unrelated_families:
            unrelated_families.add(family)
            unrelated_count += size

    assignments: list[dict[str, Any]] = []
    remaining: dict[str, list[list[dict[str, Any]]]] = {}
    for family, bundles in by_family.items():
        if family in unrelated_families:
            for bundle in bundles:
                assignments.extend(
                    {**row, "cohort": "unrelated_holdout"} for row in bundle
                )
        else:
            remaining[family] = list(bundles)

    training_families: set[str] = set()
    similar_count = 0
    training_count = 0
    for family in sorted(remaining, key=lambda value: hashlib.sha256(
        f"{seed}:paired:{value}".encode("utf-8")
    ).hexdigest()):
        bundles = remaining[family]
        if len(bundles) >= 2 and similar_count < targets["similar_holdout"]:
            train = bundles.pop(0)
            similar = bundles.pop(0)
            assignments.extend({**row, "cohort": "training"} for row in train)
            assignments.extend({**row, "cohort": "similar_holdout"} for row in similar)
            training_families.add(family)
            training_count += len(train)
            similar_count += len(similar)
    for family in sorted(training_families):
        while remaining[family] and similar_count < targets["similar_holdout"]:
            bundle = remaining[family].pop(0)
            assignments.extend({**row, "cohort": "similar_holdout"} for row in bundle)
            similar_count += len(bundle)

    leftovers = [
        bundle for family in sorted(remaining) for bundle in remaining[family]
    ]
    leftovers.sort(key=lambda bundle: hashlib.sha256(
        f"{seed}:leftover:{bundle[0]['leakage_component_id']}".encode("utf-8")
    ).hexdigest())
    while leftovers and training_count < targets["training"]:
        bundle = leftovers.pop(0)
        assignments.extend({**row, "cohort": "training"} for row in bundle)
        training_families.update(str(row["semantic_family"]) for row in bundle)
        training_count += len(bundle)
    for cohort in ("benchmark_only", "rule_simulation"):
        assigned = 0
        while leftovers and assigned < targets[cohort]:
            bundle = leftovers.pop(0)
            assignments.extend({**row, "cohort": cohort} for row in bundle)
            assigned += len(bundle)
    # Any imbalance caused by indivisible unrelated families remains training;
    # it cannot be silently discarded after selection.
    for bundle in leftovers:
        assignments.extend({**row, "cohort": "training"} for row in bundle)
    return assignments


def _assert_split_contract(assignments: Sequence[Mapping[str, Any]]) -> None:
    if not assignments:
        raise ValueError("Phase A split has no eligible units")
    units = [str(item["unit_id"]) for item in assignments]
    if len(units) != len(set(units)):
        raise ValueError("a unique invoice unit crossed cohorts")
    document_cohorts: dict[str, set[str]] = defaultdict(set)
    for item in assignments:
        for document_id in item["linked_document_ids"]:
            document_cohorts[str(document_id)].add(str(item["cohort"]))
    if any(len(cohorts) != 1 for cohorts in document_cohorts.values()):
        raise ValueError("a leakage-linked document crossed cohorts")
    leakage_cohorts: dict[str, set[str]] = defaultdict(set)
    for item in assignments:
        leakage_cohorts[str(item["leakage_component_id"])].add(str(item["cohort"]))
    if any(len(cohorts) != 1 for cohorts in leakage_cohorts.values()):
        raise ValueError("a modified financial variant crossed cohorts")
    training_families = {
        str(item["semantic_family"]) for item in assignments if item["cohort"] == "training"
    }
    similar_families = {
        str(item["semantic_family"])
        for item in assignments if item["cohort"] == "similar_holdout"
    }
    unrelated_families = {
        str(item["semantic_family"])
        for item in assignments if item["cohort"] == "unrelated_holdout"
    }
    if not similar_families.issubset(training_families):
        raise ValueError("similar holdout lacks a corresponding training concept family")
    if unrelated_families.intersection(training_families):
        raise ValueError("unrelated holdout overlaps a training concept family")
    required = {"training", "similar_holdout", "unrelated_holdout", "benchmark_only", "rule_simulation"}
    if required - {str(item["cohort"]) for item in assignments}:
        raise ValueError("all five experiment cohorts must be non-empty")


def _persist_private_snapshot(
    *,
    snapshot_root: Path,
    records: Sequence[Mapping[str, Any]],
    locators: Sequence[Mapping[str, Any]],
    groups: Sequence[Mapping[str, Any]],
    split_manifest: Mapping[str, Any] | None,
    safe_summary: Mapping[str, Any],
) -> None:
    snapshot_root.mkdir(parents=True, exist_ok=True)
    _write_jsonl_idempotent(snapshot_root / "inventory.jsonl", records)
    _write_jsonl_idempotent(snapshot_root / "private_locators.jsonl", locators)
    _write_json_idempotent(snapshot_root / "exact_leakage_groups.json", list(groups))
    if split_manifest is not None:
        _write_json_idempotent(snapshot_root / "split_manifest.json", split_manifest)
    _write_json_idempotent(snapshot_root / "git_safe_summary.json", safe_summary)
    _write_text_idempotent(
        snapshot_root / "git_safe_summary.md", render_git_safe_summary(safe_summary)
    )


def _dataset_version(
    records: Sequence[Mapping[str, Any]], groups: Sequence[Mapping[str, Any]]
) -> str:
    payload = {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "visual_hash_version": VISUAL_HASH_VERSION,
        "records": sorted(records, key=lambda row: str(row["document_id"])),
        "groups": sorted(groups, key=lambda row: str(row["group_id"])),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "corpus-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _source_snapshot(source_root: Path, salt: bytes) -> tuple[str, list[Path]]:
    files = sorted(
        (
            path
            for path in source_root.rglob("*")
            if path.is_file() and not path.is_symlink()
        ),
        key=lambda path: path.relative_to(source_root).as_posix().casefold(),
    )
    rows = []
    for path in files:
        stat = path.stat()
        relative = path.relative_to(source_root).as_posix()
        rows.append(
            {
                "locator": _hmac_hex(salt, relative),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest(), files


def _configured_profile_presence() -> list[dict[str, Any]]:
    """Return configuration booleans only; SecretStr values never leave memory."""
    from .provider_capabilities import ProfileLoader

    rows: list[dict[str, Any]] = []
    for profile in ProfileLoader().load():
        rows.append({
            "profile_id": profile.profile_id,
            "provider": profile.provider,
            "model_id": profile.model_id,
            "role": profile.role.value,
            "enabled": bool(profile.enabled),
            "credentials_present": bool(profile.credentials_present),
            "endpoint_configured": bool(profile.base_url_configured),
            "pricing_present": (
                profile.input_cost_usd_per_million is not None
                and profile.output_cost_usd_per_million is not None
            ),
        })
    return sorted(rows, key=lambda row: (str(row["role"]), str(row["profile_id"])))


def _git(project_root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=project_root, check=True,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return completed.stdout


def _assert_runtime_git_ignored(project_root: Path, runtime_root: Path) -> None:
    """Ask Git itself whether both the runtime and future children are ignored.

    Parsing ``.gitignore`` text is not authoritative because a later negation
    rule can re-include a private path.  No sentinel is written: check-ignore
    can evaluate a prospective child path without touching the filesystem.
    """
    relative = runtime_root.resolve(strict=False).relative_to(project_root.resolve())
    probes = [relative, relative / ".private-artifact-sentinel"]
    for probe in probes:
        completed = subprocess.run(
            ["git", "check-ignore", "-q", "--", str(probe)],
            cwd=project_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if completed.returncode != 0:
            raise ExperimentPathError(
                "private experiment runtime is not authoritatively ignored by Git"
            )
    status = _git(
        project_root, "status", "--porcelain=v1", "--untracked-files=all", "--",
        str(relative),
    )
    if status.strip():
        raise ExperimentPathError("private experiment runtime appears in Git status")


def _load_or_create_salt(path: Path) -> bytes:
    if path.exists():
        value = path.read_bytes()
        if len(value) != 32:
            raise ValueError("invalid private inventory HMAC key")
        return value
    value = secrets.token_bytes(32)
    path.write_bytes(value)
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hmac_hex(salt: bytes, value: str) -> str:
    return hmac.new(salt, value.encode("utf-8"), hashlib.sha256).hexdigest()


def _format_family(extension: str) -> str:
    if extension == ".pdf":
        return "pdf"
    if extension in IMAGE_EXTENSIONS:
        return "image"
    return "other"


def _union_members(members: Iterable[str], reason: str, union: Any) -> None:
    unique = sorted(set(members))
    if len(unique) < 2:
        return
    anchor = unique[0]
    for member in unique[1:]:
        union(anchor, member, reason)


def _validate_ratios(ratios: Mapping[str, float]) -> dict[str, float]:
    if tuple(ratios) != ("train", "validation", "test"):
        raise ValueError("split ratios must be ordered train, validation, test")
    values = {name: float(value) for name, value in ratios.items()}
    if any(value <= 0 or value >= 1 for value in values.values()):
        raise ValueError("every split ratio must be between zero and one")
    if not math.isclose(sum(values.values()), 1.0, abs_tol=1e-9):
        raise ValueError("split ratios must sum to one")
    return values


def _ignore_rule_present(project_root: Path, directory: str) -> bool:
    ignore_file = project_root / ".gitignore"
    if not ignore_file.is_file():
        return False
    accepted = {f"/{directory}/", f"{directory}/", f"/{directory}", directory}
    rules = {
        line.strip()
        for line in ignore_file.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    return bool(accepted.intersection(rules))


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _write_json_idempotent(path: Path, value: Any) -> None:
    text = json.dumps(value, indent=2, sort_keys=True) + "\n"
    _write_text_idempotent(path, text)


def _write_json_replace(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _write_jsonl_idempotent(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    text = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
    _write_text_idempotent(path, text)


def _write_text_idempotent(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_text(encoding="utf-8") != text:
            raise RuntimeError(f"immutable experiment artifact differs: {path.name}")
        return
    path.write_text(text, encoding="utf-8")
