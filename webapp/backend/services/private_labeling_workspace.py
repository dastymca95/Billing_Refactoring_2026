"""Private Phase 3.7 triage and blind reviewer-1 labeling domain service."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping

from .gl_payability import is_payable_gl_account


TRIAGE_DECISIONS = {
    "keep_for_labeling", "replace_with_reserve", "exclude_unadjudicable", "wrong_cohort",
    "duplicate_missed", "pages_belong_to_same_document", "pages_should_be_split",
    "needs_manual_rotation", "needs_better_source",
}
FINAL_KEEP_DECISIONS = {"keep_for_labeling", "needs_manual_rotation"}
EXCLUSION_DECISIONS = {"replace_with_reserve", "exclude_unadjudicable", "duplicate_missed", "needs_better_source"}
FORBIDDEN_BLIND_KEYS = {
    "accounting_decision", "selected_gl", "suggested_gl", "ai_confidence", "inner_view_output",
    "historical_resman_value", "reviewer_2", "reviewer_2_label", "model_output",
}
REQUIRED_DOCUMENT_FIELDS = {
    "document_family", "vendor_name", "vendor_normalization", "invoice_number", "invoice_date",
    "due_date", "property", "service_address", "bill_or_credit", "total", "expected_route",
    "document_completeness", "reviewer_confidence",
}
REQUIRED_LINE_FIELDS = {
    "line_item_number", "raw_description", "normalized_description", "quantity", "unit_price",
    "amount", "tax", "location_unit", "line_family", "trade_family", "work_mode",
    "capital_context", "expected_gl", "acceptable_alternative_gls", "should_review",
    "should_block", "reasoning_notes", "evidence",
}


class WorkspaceError(RuntimeError):
    pass


class FrozenDatasetError(WorkspaceError):
    pass


class LabelValidationError(WorkspaceError):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PrivateLabelingWorkspace:
    def __init__(self, root: Path, gl_catalog: Mapping[str, Any] | Iterable[Any]) -> None:
        self.root = root.resolve()
        self.gl_catalog = gl_catalog
        self.selection_dir = self.root / "selection"
        self.inventory_dir = self.root / "inventory"
        self.labels_dir = self.root / "labels" / "reviewer_1"
        self.labels_dir.mkdir(parents=True, exist_ok=True)
        self.selected_path = self.selection_dir / "selected_120.json"
        self.reserve_path = self.selection_dir / "reserve_20.json"
        self.decisions_path = self.selection_dir / "tier_d_triage_decisions.jsonl"
        self.replacements_path = self.selection_dir / "replacement_history.json"
        self.transforms_path = self.selection_dir / "preview_transformations.json"
        self._inventory = {row["benchmark_id"]: row for row in self._read_jsonl(self.inventory_dir / "private_inventory.jsonl")}
        duplicate_payload = self._read_json(self.inventory_dir / "duplicate_groups.json", {"groups": []})
        self._duplicate_by_member: dict[str, set[str]] = {}
        for group in duplicate_payload.get("groups", []):
            members = set(group.get("members", []))
            for member in members:
                self._duplicate_by_member.setdefault(member, set()).update(members)

    def selected(self) -> list[dict[str, Any]]:
        return list(self._read_json(self.selected_path, {"selection": []}).get("selection", []))

    def reserve(self) -> list[dict[str, Any]]:
        return list(self._read_json(self.reserve_path, {"selection": []}).get("selection", []))

    def tier_d_queue(self) -> list[dict[str, Any]]:
        latest = self.latest_triage_decisions()
        return [self.blind_document_payload(row["benchmark_id"]) | {
                    "triage_status": latest.get(row["benchmark_id"], {}).get("new_status", "pending")
                } for row in self.selected() if row.get("quality_tier") == "D"]

    def blind_document_payload(self, benchmark_id: str) -> dict[str, Any]:
        selected = next((row for row in self.selected() if row["benchmark_id"] == benchmark_id), None)
        if selected is None:
            raise WorkspaceError("benchmark_id is not selected")
        inventory = self._inventory.get(benchmark_id, {})
        payload = {
            "benchmark_id": benchmark_id,
            "cohort": selected.get("selection_cohort"),
            "page_count": inventory.get("page_count"),
            "quality_tier": inventory.get("complexity_tier"),
            "quality_metrics": {"ocr_quality": inventory.get("estimated_ocr_quality"),
                                "blur_score": inventory.get("blur_score"),
                                "contrast_score": inventory.get("contrast_score"),
                                "orientation": inventory.get("orientation")},
            "inventory_warnings": inventory.get("inventory_warnings", []),
            "duplicate_information": {"group_member_count": len(self._duplicate_by_member.get(benchmark_id, {benchmark_id}))},
            "preview_url": f"/api/private-workspace/document/{benchmark_id}/preview",
            "preview_rotation_degrees": self.preview_rotation(benchmark_id),
            "reserve_candidates": self.replacement_candidates(benchmark_id, limit=5),
        }
        self._assert_blind(payload)
        return payload

    def private_document_path(self, benchmark_id: str) -> Path:
        item = self._inventory.get(benchmark_id)
        if not item:
            raise WorkspaceError("unknown benchmark_id")
        relative = Path(item["private_relative_path"])
        candidate = (self.root / relative).resolve()
        candidate.relative_to(self.root)
        return candidate

    def replacement_candidates(self, benchmark_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        selected = self.selected()
        current = next((row for row in selected if row["benchmark_id"] == benchmark_id), None)
        if not current:
            return []
        cohort = current.get("selection_cohort")
        out = []
        selected_ids = {row["benchmark_id"] for row in selected}
        reserve = self.reserve()
        candidates = [(candidate, "reserve") for candidate in reserve]
        reserve_ids = {row["benchmark_id"] for row in reserve}
        broader = []
        for item in self._inventory.values():
            if item["benchmark_id"] in selected_ids or item["benchmark_id"] in reserve_ids:
                continue
            if cohort not in item.get("selection_bucket_candidates", []):
                continue
            broader.append(({"benchmark_id": item["benchmark_id"], "selection_cohort": cohort,
                             "quality_tier": item.get("complexity_tier"),
                             "vendor_token": item.get("probable_vendor_token"),
                             "template_signature": item.get("template_signature"),
                             "page_count": item.get("page_count"),
                             "selection_status": "replacement_from_inventory"}, "inventory"))
        broader.sort(key=lambda pair: (pair[0].get("quality_tier") not in {"B", "C"}, pair[0]["benchmark_id"]))
        candidates.extend(broader)
        for candidate, source in candidates:
            if candidate.get("selection_cohort") != cohort:
                continue
            if self._would_duplicate(candidate["benchmark_id"], selected, replacing=benchmark_id):
                continue
            if not self._within_limits(candidate, selected, replacing=benchmark_id):
                continue
            out.append({"benchmark_id": candidate["benchmark_id"], "cohort": cohort,
                        "quality_tier": candidate.get("quality_tier"), "page_count": candidate.get("page_count"),
                        "source": source})
            if len(out) >= limit:
                break
        return out

    def record_triage(self, benchmark_id: str, *, reviewer: str, decision: str, reason: str,
                      replacement_benchmark_id: str | None = None) -> dict[str, Any]:
        self._ensure_not_frozen()
        if decision not in TRIAGE_DECISIONS:
            raise WorkspaceError("invalid triage decision")
        if not reviewer.strip() or not reason.strip():
            raise WorkspaceError("reviewer and reason are required")
        selected = self.selected()
        current = next((row for row in selected if row["benchmark_id"] == benchmark_id), None)
        if not current or current.get("quality_tier") != "D":
            raise WorkspaceError("triage is restricted to selected Tier D documents")
        previous = self.latest_triage_decisions().get(benchmark_id, {}).get("new_status", "pending")
        new_status = "kept" if decision in FINAL_KEEP_DECISIONS else "review_flagged"
        replacement = None
        if decision in EXCLUSION_DECISIONS:
            replacement = self._replace(benchmark_id, replacement_benchmark_id)
            new_status = "replaced"
        event = {"benchmark_id": benchmark_id, "reviewer": reviewer.strip(), "timestamp": utc_now(),
                 "decision": decision, "reason": reason.strip(), "previous_status": previous,
                 "new_status": new_status, "replacement_benchmark_id": replacement}
        self._append_jsonl(self.decisions_path, event)
        return event

    def apply_preview_rotation_metadata(self) -> dict[str, Any]:
        """Persist rotation metadata without rewriting private source documents."""
        decisions = self.latest_triage_decisions()
        rotations = []
        unresolved = []
        for benchmark_id, event in decisions.items():
            if event.get("decision") != "needs_manual_rotation":
                continue
            match = re.search(r"(?<!\d)(90|180|270)(?!\d)", str(event.get("reason") or ""))
            if not match:
                unresolved.append(benchmark_id)
                continue
            path = self.private_document_path(benchmark_id)
            before = _sha256_file(path)
            rotation = {"benchmark_id": benchmark_id, "degrees_clockwise": int(match.group(1)),
                        "source_sha256": before, "source_modified": False,
                        "derived_from": "human_triage_reason", "created_at": utc_now()}
            after = _sha256_file(path)
            if before != after:
                raise WorkspaceError("source changed while applying preview metadata")
            rotations.append(rotation)
        payload = {"schema_version": "preview-transformations/1.0", "rotations": rotations,
                   "unresolved_benchmark_ids": unresolved, "updated_at": utc_now()}
        self._atomic_json(self.transforms_path, payload)
        return {"rotations_applied": len(rotations), "unresolved": len(unresolved)}

    def preview_rotation(self, benchmark_id: str) -> int:
        payload = self._read_json(self.transforms_path, {"rotations": []})
        return next((int(row["degrees_clockwise"]) for row in payload.get("rotations", [])
                     if row.get("benchmark_id") == benchmark_id), 0)

    def _replace(self, benchmark_id: str, replacement_id: str | None) -> str:
        selected, reserve = self.selected(), self.reserve()
        current = next(row for row in selected if row["benchmark_id"] == benchmark_id)
        candidates = self.replacement_candidates(benchmark_id, limit=100)
        chosen_id = replacement_id or (candidates[0]["benchmark_id"] if candidates else None)
        if not chosen_id or chosen_id not in {item["benchmark_id"] for item in candidates}:
            raise WorkspaceError("no valid same-cohort reserve replacement")
        replacement = next((row for row in reserve if row["benchmark_id"] == chosen_id), None)
        replacement_source = "reserve"
        if replacement is None:
            item = self._inventory[chosen_id]
            replacement_source = "inventory"
            replacement = {"benchmark_id": chosen_id, "selection_cohort": current.get("selection_cohort"),
                           "quality_tier": item.get("complexity_tier"),
                           "vendor_token": item.get("probable_vendor_token"),
                           "template_signature": item.get("template_signature"), "page_count": item.get("page_count"),
                           "selection_status": "replacement_from_inventory"}
        selected = [replacement if row["benchmark_id"] == benchmark_id else row for row in selected]
        reserve = [row for row in reserve if row["benchmark_id"] != chosen_id]
        if len(selected) != 120 or len({row["benchmark_id"] for row in selected}) != 120:
            raise WorkspaceError("replacement must preserve exactly 120 unique selected documents")
        self._atomic_json(self.selected_path, {"selection": selected, "updated_at": utc_now()})
        self._atomic_json(self.reserve_path, {"selection": reserve, "updated_at": utc_now()})
        history = self._read_json(self.replacements_path, {"replacements": []})
        history["replacements"].append({"removed_benchmark_id": benchmark_id,
                                         "replacement_benchmark_id": chosen_id,
                                         "cohort": current.get("selection_cohort"), "source": replacement_source,
                                         "timestamp": utc_now()})
        self._atomic_json(self.replacements_path, history)
        return chosen_id

    def freeze_dataset(self, version: str = "v1") -> dict[str, str]:
        decisions = self.latest_triage_decisions()
        pending = [row["benchmark_id"] for row in self.selected()
                   if row.get("quality_tier") == "D" and decisions.get(row["benchmark_id"], {}).get("new_status") != "kept"]
        if pending:
            raise WorkspaceError(f"Tier D triage incomplete: {len(pending)} pending")
        rotation_required = {benchmark_id for benchmark_id, event in decisions.items()
                             if event.get("decision") == "needs_manual_rotation"}
        transformations = self._read_json(self.transforms_path, {"rotations": [], "unresolved_benchmark_ids": []})
        rotation_recorded = {row.get("benchmark_id") for row in transformations.get("rotations", [])}
        if transformations.get("unresolved_benchmark_ids") or rotation_required - rotation_recorded:
            raise WorkspaceError("preview rotation metadata is incomplete")
        selected = self.selected()
        if len(selected) != 120:
            raise WorkspaceError("frozen dataset must contain exactly 120 documents")
        snapshot = {"schema_version": "selected-dataset/1.0", "dataset_version": version,
                    "created_at": utc_now(), "selection": selected}
        encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(encoded).hexdigest()
        snapshot_path = self.selection_dir / f"selected_120_{version}.json"
        hash_path = self.selection_dir / f"selected_120_{version}.sha256"
        if snapshot_path.exists() or hash_path.exists():
            raise FrozenDatasetError("dataset version already exists")
        self._atomic_bytes(snapshot_path, encoded + b"\n")
        self._atomic_bytes(hash_path, (digest + "\n").encode())
        return {"dataset_version": version, "sha256": digest}

    def save_label(self, benchmark_id: str, label: Mapping[str, Any], *, reviewer_id: str,
                   dataset_version: str, completion_status: str = "in_progress") -> dict[str, Any]:
        if benchmark_id not in {row["benchmark_id"] for row in self.selected()}:
            raise WorkspaceError("label target is not selected")
        self._assert_blind(label)
        errors = validate_reviewer_1_label(label, self.gl_catalog)
        if completion_status == "complete" and errors:
            raise LabelValidationError(errors)
        path = self.labels_dir / f"{benchmark_id}.json"
        previous = self._read_json(path, {})
        now = utc_now()
        history = list(previous.get("audit_history", []))
        history.append({"timestamp": now, "reviewer_id": reviewer_id, "action": "autosave",
                        "previous_completion_status": previous.get("completion_status")})
        payload = {"schema_version": "reviewer-1-label/1.0", "benchmark_id": benchmark_id,
                   "dataset_version": dataset_version, "reviewer_id": reviewer_id,
                   "created_at": previous.get("created_at") or now, "updated_at": now,
                   "completion_status": completion_status,
                   "validation_status": "valid" if not errors else "invalid",
                   "validation_errors": errors, "unresolved_questions": list(label.get("unresolved_questions", [])),
                   "document": dict(label.get("document", {})), "line_items": list(label.get("line_items", [])),
                   "audit_history": history}
        self._atomic_json(path, payload)
        self._atomic_json(self.labels_dir / ".crash_recovery.json",
                          {"last_saved_benchmark_id": benchmark_id, "updated_at": now})
        return payload

    def latest_triage_decisions(self) -> dict[str, dict[str, Any]]:
        latest = {}
        for row in self._read_jsonl(self.decisions_path):
            latest[row["benchmark_id"]] = row
        return latest

    def status(self) -> dict[str, Any]:
        selected = self.selected(); decisions = self.latest_triage_decisions()
        labels = [self._read_json(path, {}) for path in self.labels_dir.glob("bench-*.json")]
        d_rows = [row for row in selected if row.get("quality_tier") == "D"]
        counts = Counter(event.get("new_status") for event in decisions.values())
        label_counts = Counter(label.get("completion_status", "not_started") for label in labels)
        confidence = Counter(_confidence_bucket(label) for label in labels if label)
        transformations = self._read_json(self.transforms_path, {"rotations": []})
        frozen_hashes = sorted(self.selection_dir.glob("selected_120_v*.sha256"))
        frozen_hash = frozen_hashes[-1].read_text(encoding="ascii").strip() if frozen_hashes else None
        frozen_version = frozen_hashes[-1].stem.replace("selected_120_", "") if frozen_hashes else None
        return {"schema_version": "phase-3.7-status/1.0", "selected_count": len(selected),
                "tier_d_total": len(d_rows), "tier_d_reviewed": sum(row["benchmark_id"] in decisions for row in d_rows),
                "kept": counts["kept"], "replaced": counts["replaced"],
                "excluded": sum(event.get("decision") in EXCLUSION_DECISIONS for event in decisions.values()),
                "labeling": {"not_started": len(selected) - len(labels), "in_progress": label_counts["in_progress"],
                             "complete": label_counts["complete"],
                             "validation_errors": sum(bool(label.get("validation_errors")) for label in labels)},
                "cohort_progress": dict(Counter(row.get("selection_cohort") for row in selected)),
                "reviewer_confidence_distribution": dict(confidence),
                "preview_rotations_applied": len(transformations.get("rotations", [])),
                "dataset_frozen": bool(frozen_hashes), "dataset_version": frozen_version,
                "dataset_sha256": frozen_hash,
                "ai_calls": 0, "strong_reasoner_used": False}

    def safe_status_markdown(self) -> str:
        status = self.status()
        labeling = status["labeling"]
        return f"""# Phase 3.7 Labeling Status

This report contains aggregate status only. It contains no document content,
filenames, private paths, labels, vendor names, addresses, screenshots, or notes.

- Selected documents: {status['selected_count']}
- Tier D total: {status['tier_d_total']}
- Tier D reviewed: {status['tier_d_reviewed']}
- Kept: {status['kept']}
- Replaced: {status['replaced']}
- Excluded: {status['excluded']}
- Labeling not started: {labeling['not_started']}
- Labeling in progress: {labeling['in_progress']}
- Labeling complete: {labeling['complete']}
- Labels with validation errors: {labeling['validation_errors']}
- Dataset frozen: {'yes' if status['dataset_frozen'] else 'no'}
- Preview rotations applied: {status['preview_rotations_applied']}
- Dataset version: {status['dataset_version'] or 'not frozen'}
- Dataset SHA-256: {status['dataset_sha256'] or 'not frozen'}
- AI calls: 0
- Strong reasoner used: no

## Workspace

The loopback-only reviewer workspace is implemented and operational. Start it
with `python scripts/run_private_labeling_workspace.py` after setting
`INNER_VIEW_PRIVATE_BENCHMARK_ROOT`. Reviewer 1 receives only source preview and
inventory metadata; application decisions, AI confidence, historical values,
and reviewer 2 labels are not exposed.

Dataset freeze remains blocked until all selected Tier D documents have a human
triage decision and every exclusion has a valid replacement. No human decisions
are inferred or generated automatically.
"""

    def _would_duplicate(self, candidate_id: str, selected: list[dict[str, Any]], *, replacing: str) -> bool:
        remaining = {row["benchmark_id"] for row in selected if row["benchmark_id"] != replacing}
        duplicate_members = self._duplicate_by_member.get(candidate_id, set())
        return replacing in duplicate_members or bool(duplicate_members & remaining)

    @staticmethod
    def _within_limits(candidate, selected, *, replacing):
        rows = [row for row in selected if row["benchmark_id"] != replacing] + [candidate]
        vendors = Counter(row.get("vendor_token") or f"unknown:{row['benchmark_id']}" for row in rows)
        templates = Counter(row.get("template_signature") or f"unique:{row['benchmark_id']}" for row in rows)
        return max(vendors.values(), default=0) <= 5 and max(templates.values(), default=0) <= 3

    def _ensure_not_frozen(self):
        if any(self.selection_dir.glob("selected_120_v*.sha256")):
            raise FrozenDatasetError("selected dataset is frozen; create a new dataset version")

    @staticmethod
    def _assert_blind(payload: Mapping[str, Any]):
        serialized = json.dumps(payload).lower()
        for key in FORBIDDEN_BLIND_KEYS:
            if f'"{key.lower()}"' in serialized:
                raise WorkspaceError(f"blind workspace payload contains forbidden key: {key}")

    @staticmethod
    def _read_json(path: Path, default):
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default

    @staticmethod
    def _read_jsonl(path: Path):
        if not path.is_file(): return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    @staticmethod
    def _append_jsonl(path: Path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle: handle.write(json.dumps(payload, sort_keys=True) + "\n")

    @staticmethod
    def _atomic_json(path: Path, payload):
        PrivateLabelingWorkspace._atomic_bytes(path, (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode())

    @staticmethod
    def _atomic_bytes(path: Path, payload: bytes):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".autosave-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle: handle.write(payload); handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary): os.unlink(temporary)


def validate_reviewer_1_label(label: Mapping[str, Any], gl_catalog) -> list[str]:
    errors: list[str] = []
    document = label.get("document") if isinstance(label.get("document"), Mapping) else {}
    lines = label.get("line_items") if isinstance(label.get("line_items"), list) else []
    for field in REQUIRED_DOCUMENT_FIELDS:
        if not explicit_value(document.get(field)):
            errors.append(f"document.{field}:value_or_explicit_unknown_required")
    if not lines:
        errors.append("line_items:at_least_one_required")
    line_total = Decimal("0")
    amounts_complete = True
    for index, line in enumerate(lines):
        if not isinstance(line, Mapping):
            errors.append(f"line_items[{index}]:invalid"); continue
        for field in REQUIRED_LINE_FIELDS:
            if field not in line or not explicit_value(line.get(field)):
                errors.append(f"line_items[{index}].{field}:value_or_explicit_unknown_required")
        gl = unwrap_value(line.get("expected_gl"))
        exceptional = bool(line.get("exceptional_gl"))
        if gl and not exceptional and not is_payable_gl_account(str(gl), gl_catalog):
            errors.append(f"line_items[{index}].expected_gl:invalid_or_non_payable")
        evidence = line.get("evidence")
        if not isinstance(evidence, list) or not evidence or not all(isinstance(item, Mapping) and item.get("page") for item in evidence):
            errors.append(f"line_items[{index}].evidence:page_region_required")
        amount = decimal_or_none(unwrap_value(line.get("amount")))
        if amount is None: amounts_complete = False
        else: line_total += amount
    total = decimal_or_none(unwrap_value(document.get("total")))
    mismatch_acknowledged = bool(label.get("reconciliation_discrepancy"))
    if total is None and not is_unknown(document.get("total")):
        errors.append("document.total:value_or_unreadable_required")
    if total is not None and amounts_complete and abs(line_total - total) > Decimal("0.01") and not mismatch_acknowledged:
        errors.append("reconciliation:totals_mismatch_requires_explicit_flag")
    return sorted(set(errors))


def explicit_value(value: Any) -> bool:
    if is_unknown(value): return bool(value.get("reason"))
    if isinstance(value, bool): return True
    if isinstance(value, (int, float, Decimal)): return True
    if isinstance(value, list): return True
    return bool(str(value or "").strip())


def is_unknown(value: Any) -> bool:
    return isinstance(value, Mapping) and value.get("status") in {"unknown", "unreadable", "not_applicable"}


def unwrap_value(value: Any) -> Any:
    return None if is_unknown(value) else value


def decimal_or_none(value: Any) -> Decimal | None:
    try: return Decimal(str(value)) if value not in (None, "") else None
    except (InvalidOperation, ValueError): return None


def _confidence_bucket(label: Mapping[str, Any]) -> str:
    value = unwrap_value((label.get("document") or {}).get("reviewer_confidence"))
    try: number = float(value)
    except (TypeError, ValueError): return "unknown"
    return "high" if number >= .85 else "medium" if number >= .6 else "low"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
