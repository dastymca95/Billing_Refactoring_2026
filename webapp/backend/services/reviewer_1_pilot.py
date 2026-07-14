"""Private, human-only controller for the Phase 3.9 Reviewer 1 pilot."""
from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .private_labeling_workspace import PrivateLabelingWorkspace, WorkspaceError, utc_now


DIFFICULTY = {"A": "easy", "B": "easy", "C": "moderate", "D": "difficult"}
TARGETS = {"easy": 10, "moderate": 5, "difficult": 5}


class Reviewer1Pilot:
    def __init__(self, workspace: PrivateLabelingWorkspace) -> None:
        self.workspace = workspace
        self.root = workspace.root
        self.manifest_path = workspace.labels_dir.parent / "pilot_20_v1.json"
        self.events_path = workspace.labels_dir / "pilot_20_v1_events.jsonl"
        self.reports_dir = self.root / "reports"

    def prepare_manifest(self) -> dict[str, Any]:
        existing = self._read_json(self.manifest_path)
        if existing:
            self._validate_manifest(existing)
            return existing
        selected = self._selected_frozen()
        selected_by_id = {row["benchmark_id"]: row for row in selected}
        draft_ids = [path.stem for path in self.workspace.labels_dir.glob("*.json")
                     if path.stem in selected_by_id]
        chosen: list[dict[str, str]] = []
        used: set[str] = set()
        buckets: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in selected:
            buckets[DIFFICULTY.get(row.get("quality_tier"), "difficult")].append(row)
        for rows in buckets.values():
            rows.sort(key=lambda row: (row.get("selection_cohort", ""), row["benchmark_id"]))
        for benchmark_id in draft_ids:
            row = selected_by_id[benchmark_id]
            difficulty = DIFFICULTY.get(row.get("quality_tier"), "difficult")
            if sum(item["difficulty"] == difficulty for item in chosen) < TARGETS[difficulty]:
                chosen.append(self._entry(row, difficulty)); used.add(benchmark_id)
        for difficulty, target in TARGETS.items():
            rows = [row for row in buckets[difficulty] if row["benchmark_id"] not in used]
            by_cohort: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
            for row in rows: by_cohort[row.get("selection_cohort", "unknown")].append(row)
            cohorts = sorted(by_cohort)
            while sum(item["difficulty"] == difficulty for item in chosen) < target:
                progressed = False
                for cohort in cohorts:
                    if by_cohort[cohort] and sum(item["difficulty"] == difficulty for item in chosen) < target:
                        row = by_cohort[cohort].pop(0); chosen.append(self._entry(row, difficulty))
                        used.add(row["benchmark_id"]); progressed = True
                if not progressed: raise WorkspaceError(f"insufficient {difficulty} documents for pilot")
        manifest = {"schema_version": "reviewer-1-pilot-manifest/1.0", "pilot_version": "pilot_20_v1",
                    "dataset_version": "selected_120_v1", "documents": chosen}
        self.workspace._atomic_json(self.manifest_path, manifest)
        self._validate_manifest(manifest)
        return manifest

    def queue(self) -> list[dict[str, Any]]:
        manifest = self.prepare_manifest()
        completed = self.completed_ids()
        queue = []
        for row in manifest["documents"]:
            if row["benchmark_id"] in completed: continue
            saved = self._read_json(self.workspace.labels_dir / f"{row['benchmark_id']}.json") or {}
            draft = {key: saved[key] for key in ("document", "line_items", "unresolved_questions") if key in saved}
            queue.append(self.workspace.blind_document_payload(row["benchmark_id"]) | {
                "pilot_difficulty": row["difficulty"], "pilot_cohort": row["cohort"],
                "pilot_completion_status": self._label_status(row["benchmark_id"]),
                "draft": draft or None, "draft_validation_status": saved.get("validation_status"),
                "draft_validation_errors": saved.get("validation_errors", []),
            })
        return queue

    def record_activity(self, benchmark_id: str, *, reviewer_id: str, action: str,
                        details: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if benchmark_id not in self.pilot_ids(): raise WorkspaceError("document is outside pilot_20_v1")
        allowed = {"start", "resume", "pause", "autosave", "recovery", "validation", "complete",
                   "abandon", "usability_note", "filename_conflict", "allocation_conflict"}
        if action not in allowed: raise WorkspaceError("invalid pilot activity")
        event = {"schema_version": "reviewer-1-pilot-event/1.0", "benchmark_id": benchmark_id,
                 "reviewer_id": reviewer_id, "action": action, "timestamp": utc_now(),
                 "details": dict(details or {})}
        self.workspace._append_jsonl(self.events_path, event)
        return event

    def active_seconds(self, benchmark_id: str) -> float:
        active_since: datetime | None = None; total = 0.0
        for event in self._events(benchmark_id):
            stamp = datetime.fromisoformat(event["timestamp"])
            if event["action"] in {"start", "resume"} and active_since is None: active_since = stamp
            elif event["action"] in {"pause", "complete", "abandon"} and active_since is not None:
                total += max(0.0, (stamp - active_since).total_seconds()); active_since = None
        return total

    def abandon_draft(self, benchmark_id: str, *, reviewer_id: str, reason: str) -> dict[str, Any]:
        if not reason.strip(): raise WorkspaceError("abandon reason is required")
        path = self.workspace.labels_dir / f"{benchmark_id}.json"
        payload = self._read_json(path)
        if not payload: raise WorkspaceError("draft does not exist")
        history = list(payload.get("audit_history", [])); history.append({"timestamp": utc_now(),
            "action": "abandon", "reviewer_id": reviewer_id, "reason": reason.strip(),
            "previous_completion_status": payload.get("completion_status")})
        payload["completion_status"] = "abandoned"; payload["audit_history"] = history
        self.workspace._atomic_json(path, payload)
        self.record_activity(benchmark_id, reviewer_id=reviewer_id, action="abandon")
        return payload

    def completed_ids(self) -> set[str]:
        return {benchmark_id for benchmark_id in self.pilot_ids() if self._label_status(benchmark_id) == "complete"}

    def pilot_ids(self) -> set[str]:
        return {row["benchmark_id"] for row in self.prepare_manifest()["documents"]}

    def reviewer_2_start(self) -> None:
        raise WorkspaceError("Reviewer 2 is disabled until the Reviewer 1 pilot review gates pass")

    def metrics(self) -> dict[str, Any]:
        manifest = self.prepare_manifest(); ids = self.pilot_ids(); times = [self.active_seconds(i) for i in ids if self.active_seconds(i)]
        labels = [self._read_json(self.workspace.labels_dir / f"{i}.json") for i in ids]
        labels = [label for label in labels if label]
        errors = Counter(error.split(":", 1)[0] for label in labels for error in label.get("validation_errors", []))
        unknown = sum(json.dumps(label).count('"status": "unknown"') for label in labels)
        unreadable = sum(json.dumps(label).count('"status": "unreadable"') for label in labels)
        events = [event for event in self.workspace._read_jsonl(self.events_path) if event.get("benchmark_id") in ids]
        return {"schema_version": "reviewer-1-pilot-metrics/1.0", "pilot_size": 20,
            "composition": dict(Counter(row["difficulty"] for row in manifest["documents"])),
            "completed": len(self.completed_ids()), "remaining": 20-len(self.completed_ids()),
            "average_active_seconds": statistics.mean(times) if times else None,
            "median_active_seconds": statistics.median(times) if times else None,
            "validation_errors_by_category": dict(errors), "unknown_fields": unknown,
            "unreadable_fields": unreadable, "autosave_events": sum(e["action"] == "autosave" for e in events),
            "recovery_events": sum(e["action"] == "recovery" for e in events),
            "filename_conflicts": sum(e["action"] == "filename_conflict" for e in events),
            "allocation_conflicts": sum(e["action"] == "allocation_conflict" for e in events),
            "usability_issue_count": sum(e["action"] == "usability_note" for e in events)}

    def write_reports(self) -> dict[str, Any]:
        metrics = self.metrics(); self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.workspace._atomic_json(self.reports_dir / "reviewer_1_pilot_metrics.json", metrics)
        for name, actions in (("reviewer_1_validation_errors.json", {"validation"}),
                              ("reviewer_1_usability_notes.json", {"usability_note"}),
                              ("reviewer_1_ambiguities.json", {"filename_conflict", "allocation_conflict"})):
            rows = [event for event in self.workspace._read_jsonl(self.events_path) if event.get("action") in actions]
            self.workspace._atomic_json(self.reports_dir / name, {"events": rows})
        report = "# Reviewer 1 pilot 20 v1\n\n" + "\n".join(f"- {key}: {value}" for key, value in metrics.items()) + "\n"
        (self.reports_dir / "reviewer_1_pilot_20_v1.md").write_text(report, encoding="utf-8")
        return metrics

    def git_safe_markdown(self) -> str:
        m = self.metrics()
        return f"""# Phase 3.9 — Reviewer 1 controlled pilot status

- Composition: 10 easy / 5 moderate / 5 difficult
- Completed: {m['completed']} / 20
- Average active labeling seconds: {m['average_active_seconds'] if m['average_active_seconds'] is not None else 'not available'}
- Median active labeling seconds: {m['median_active_seconds'] if m['median_active_seconds'] is not None else 'not available'}
- Validation error categories: {json.dumps(m['validation_errors_by_category'], sort_keys=True)}
- Unknown fields: {m['unknown_fields']}
- Unreadable fields: {m['unreadable_fields']}
- Filename conflicts: {m['filename_conflicts']}
- Allocation conflicts: {m['allocation_conflicts']}
- Usability issue count: {m['usability_issue_count']}
- Reviewer 2: disabled
- Strong reasoning: disabled
- Remaining dataset: not authorized
"""

    def _validate_manifest(self, manifest: Mapping[str, Any]) -> None:
        rows = manifest.get("documents", [])
        if len(rows) != 20 or Counter(row.get("difficulty") for row in rows) != Counter(TARGETS):
            raise WorkspaceError("pilot manifest must contain 10 easy, 5 moderate, and 5 difficult documents")
        if not {row["benchmark_id"] for row in rows} <= {row["benchmark_id"] for row in self._selected_frozen()}:
            raise WorkspaceError("pilot manifest contains an ID outside selected_120_v1")

    @staticmethod
    def _entry(row: Mapping[str, Any], difficulty: str) -> dict[str, str]:
        return {"benchmark_id": row["benchmark_id"], "difficulty": difficulty,
                "cohort": row.get("selection_cohort", "unknown")}

    def _label_status(self, benchmark_id: str) -> str:
        return (self._read_json(self.workspace.labels_dir / f"{benchmark_id}.json") or {}).get("completion_status", "not_started")

    def _selected_frozen(self) -> list[dict[str, Any]]:
        path = self.workspace.selection_dir / "selected_120_v1.json"
        if not path.is_file():
            return self.workspace.selected()  # test/development adapter before a frozen fixture exists
        return list((self._read_json(path) or {}).get("selection", []))

    def _events(self, benchmark_id: str) -> list[dict[str, Any]]:
        return [event for event in self.workspace._read_jsonl(self.events_path) if event.get("benchmark_id") == benchmark_id]

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
