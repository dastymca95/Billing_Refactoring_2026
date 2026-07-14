"""Phase 3.9A machine proposals and explicit human verification sidecars."""
from __future__ import annotations

import hashlib
import json
import re
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping

from .private_labeling_workspace import PrivateLabelingWorkspace, WorkspaceError, utc_now, validate_reviewer_1_label
from .reviewer_1_pilot import Reviewer1Pilot


PROFILE_VERSION = "assisted-labeling/deterministic-extraction-accounting-v2-1.2"
FIELD_ACTIONS = {"accept", "correct", "reject", "unknown", "unreadable", "not_applicable"}


class AssistedLabelingService:
    def __init__(self, workspace: PrivateLabelingWorkspace, pilot: Reviewer1Pilot) -> None:
        self.workspace = workspace; self.pilot = pilot
        self.proposals_dir = workspace.labels_dir / "machine_proposals"
        self.decisions_path = workspace.labels_dir / "assisted_field_decisions.jsonl"
        self.validity_path = workspace.labels_dir.parent / "dataset_adjudication_events.jsonl"

    def record_dataset_owner_validity(self) -> dict[str, Any]:
        existing = self.workspace._read_jsonl(self.validity_path)
        match = next((event for event in existing if event.get("decision") == "all_selected_documents_adjudicable"
                      and event.get("scope") == "selected_120_v1"), None)
        if match: return match
        event = {"schema_version": "dataset-adjudication-event/1.0", "decision":
            "all_selected_documents_adjudicable", "authority": "dataset_owner", "scope": "selected_120_v1",
            "timestamp": utc_now(), "effect": "validity_triage_closed_only",
            "does_not_assert": ["extracted_facts", "property", "reimbursement", "gl", "accounting_completion"]}
        self.workspace._append_jsonl(self.validity_path, event)
        return event

    def proposal(self, benchmark_id: str) -> dict[str, Any]:
        if benchmark_id not in self.pilot.pilot_ids(): raise WorkspaceError("document is outside assisted pilot")
        path = self.proposals_dir / f"{benchmark_id}.json"
        if path.is_file():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing.get("proposal_profile_version") == PROFILE_VERSION: return existing
        source_path = self.workspace.private_document_path(benchmark_id)
        metadata = self.workspace.private_source_metadata(benchmark_id)
        text, extraction_method = self._extract_text(source_path)
        fields = self._deterministic_fields(text, metadata)
        lines = self._deterministic_lines(text)
        fields.extend(self._line_fields(lines))
        proposal = {"schema_version": "assisted-labeling-proposal/1.0", "benchmark_id": benchmark_id,
            "labeling_mode": "assisted_human_verification", "proposal_profile_version": PROFILE_VERSION,
            "proposal_generated_at": utc_now(), "status": "unverified", "source": "machine_proposed",
            "extraction_method": extraction_method, "fields": fields, "lines": lines,
            "accounting_candidates": [], "strong_reasoner_used": False, "authoritative": False}
        self.workspace._atomic_json(path, proposal)
        return proposal

    def review_state(self, benchmark_id: str) -> dict[str, Any]:
        proposal = self.proposal(benchmark_id); latest = self._latest_decisions(benchmark_id)
        fields = []
        for item in proposal["fields"]:
            decision = latest.get(item["field_path"])
            fields.append(item | {"review_action": decision.get("action") if decision else None,
                                  "human_value": decision.get("human_value") if decision else None,
                                  "changed_by_human": bool(decision and decision["action"] == "correct")})
        return {"proposal": proposal | {"fields": fields}, "field_decisions": list(latest.values()),
                "benchmark_status": "human_verified_assisted" if self._is_approved(benchmark_id) else "machine_proposed",
                "adjudicated_gold": False}

    def decide_field(self, benchmark_id: str, field_path: str, *, reviewer_id: str, action: str,
                     human_value: Any = None, reason: str | None = None,
                     evidence_inspected: bool = False, reviewer_confidence: float | None = None) -> dict[str, Any]:
        if action not in FIELD_ACTIONS: raise WorkspaceError("invalid assisted field action")
        proposal = self.proposal(benchmark_id)
        proposed = next((item for item in proposal["fields"] if item["field_path"] == field_path), None)
        if proposed is None: raise WorkspaceError("unknown proposed field")
        if action == "accept": human_value = proposed["proposed_value"]
        if action == "correct" and (human_value is None or human_value == ""):
            raise WorkspaceError("correct requires a human value")
        event = {"schema_version": "assisted-field-decision/1.0", "benchmark_id": benchmark_id,
            "field_path": field_path, "action": action, "machine_proposal": proposed,
            "human_value": human_value, "reason": reason, "reviewer_id": reviewer_id,
            "reviewer_confidence": reviewer_confidence, "evidence_inspected": evidence_inspected,
            "timestamp": utc_now(), "authoritative_source": "reviewer_1"}
        self.workspace._append_jsonl(self.decisions_path, event)
        return event

    def accept_non_conflicting(self, benchmark_id: str, *, reviewer_id: str,
                               evidence_inspected: bool) -> dict[str, int]:
        if not evidence_inspected: raise WorkspaceError("document inspection is required")
        state = self.review_state(benchmark_id); accepted = 0
        for field in state["proposal"]["fields"]:
            if field["review_action"] or field["conflicts"] or field["confidence"] < .75: continue
            self.decide_field(benchmark_id, field["field_path"], reviewer_id=reviewer_id, action="accept",
                              evidence_inspected=True); accepted += 1
        return {"accepted": accepted}

    def approve_document(self, benchmark_id: str, *, reviewer_id: str,
                         evidence_inspected: bool) -> dict[str, Any]:
        saved = self.pilot._read_json(self.workspace.labels_dir / f"{benchmark_id}.json") or {}
        label = {key: saved.get(key) for key in ("document", "line_items", "unresolved_questions")}
        validation_errors = validate_reviewer_1_label(label, self.workspace.gl_catalog)
        if validation_errors: raise WorkspaceError("blocking validation errors must be resolved: " + "; ".join(validation_errors))
        if not evidence_inspected: raise WorkspaceError("document inspection is required")
        state = self.review_state(benchmark_id)
        unresolved = [field["field_path"] for field in state["proposal"]["fields"] if not field["review_action"]]
        if unresolved: raise WorkspaceError("all proposed fields require an explicit reviewer decision")
        event = self.pilot.record_activity(benchmark_id, reviewer_id=reviewer_id, action="complete",
            details={"labeling_mode": "assisted_human_verification", "fields_proposed": len(state["proposal"]["fields"]),
                     "fields_accepted": sum(x["action"] == "accept" for x in state["field_decisions"]),
                     "fields_corrected": sum(x["action"] == "correct" for x in state["field_decisions"])})
        return {"status": "human_verified_assisted", "event": event, "adjudicated_gold": False}

    def exception_fields(self, benchmark_id: str) -> list[dict[str, Any]]:
        fields = self.review_state(benchmark_id)["proposal"]["fields"]
        return [field for field in fields if not field["review_action"] and
                (field["confidence"] < .75 or field["conflicts"] or field["proposed_value"] in (None, ""))]

    def metrics(self) -> dict[str, Any]:
        ids = self.pilot.pilot_ids(); proposals = [self.proposal(i) for i in ids]
        events = [e for e in self.workspace._read_jsonl(self.decisions_path) if e.get("benchmark_id") in ids]
        latest = {(e["benchmark_id"], e["field_path"]): e for e in events}
        counts = {action: sum(e["action"] == action for e in latest.values()) for action in FIELD_ACTIONS}
        total = sum(len(p["fields"]) for p in proposals); accepted = counts["accept"]
        times = {i: self.pilot.active_seconds(i) for i in ids}; positive_times = [v for v in times.values() if v > 0]
        manifest = {row["benchmark_id"]: row["difficulty"] for row in self.pilot.prepare_manifest()["documents"]}
        by_difficulty: dict[str, list[float]] = defaultdict(list)
        for benchmark_id, seconds in times.items():
            if seconds > 0: by_difficulty[manifest[benchmark_id]].append(seconds)
        pilot_events = [e for e in self.workspace._read_jsonl(self.pilot.events_path) if e.get("benchmark_id") in ids]
        filename_events = [e for e in latest.values() if e["machine_proposal"].get("source") == "filename_or_folder_parser"]
        result = {"schema_version": "assisted-labeling-metrics/1.0", "fields_proposed": total,
            "accepted_unchanged": accepted, "corrected": counts["correct"],
            "rejected": counts["reject"], "unresolved": total-len(latest),
            "proposal_acceptance_rate": accepted / total if total else 0,
            "average_active_review_seconds": statistics.mean(positive_times) if positive_times else None,
            "median_active_review_seconds": statistics.median(positive_times) if positive_times else None,
            "active_seconds_by_difficulty": {key: sum(values) for key, values in by_difficulty.items()},
            "property_correction_rate": self._correction_rate(latest.values(), "property"),
            "reimbursement_correction_rate": self._correction_rate(latest.values(), "reimbursement"),
            "gl_correction_rate": self._correction_rate(latest.values(), "gl_candidate"),
            "line_item_correction_rate": self._correction_rate(latest.values(), "lines."),
            "filename_evidence_reviewed": len(filename_events),
            "filename_evidence_accepted": sum(e["action"] == "accept" for e in filename_events),
            "validation_failures": sum(bool((e.get("details") or {}).get("validation_errors")) for e in pilot_events)}
        return result

    def write_metrics(self) -> dict[str, Any]:
        metrics = self.metrics(); self.pilot.reports_dir.mkdir(parents=True, exist_ok=True)
        self.workspace._atomic_json(self.pilot.reports_dir / "reviewer_1_assisted_metrics.json", metrics)
        return metrics

    def _latest_decisions(self, benchmark_id: str) -> dict[str, dict[str, Any]]:
        latest = {}
        for event in self.workspace._read_jsonl(self.decisions_path):
            if event.get("benchmark_id") == benchmark_id: latest[event["field_path"]] = event
        return latest

    def _is_approved(self, benchmark_id: str) -> bool:
        return any(event.get("action") == "complete" for event in self.pilot._events(benchmark_id))

    @staticmethod
    def _extract_text(path: Path) -> tuple[str, str]:
        try:
            from .ai_invoice_processor import extract_document_text
            return extract_document_text(path) or "", "deterministic_document_text"
        except Exception:
            return "", "source_metadata_only"

    def _deterministic_fields(self, text: str, metadata: Mapping[str, Any]) -> list[dict[str, Any]]:
        fields: list[dict[str, Any]] = []
        def add(path, value, confidence, source, evidence, conflicts=None):
            if value not in (None, ""):
                fields.append({"proposal_id": hashlib.sha256(f"{path}|{value}|{source}".encode()).hexdigest()[:16],
                    "field_path": path, "proposed_value": value, "source": source, "status": "unverified",
                    "confidence": confidence, "evidence": evidence, "conflicts": conflicts or [],
                    "profile_version": PROFILE_VERSION})
        for candidate in metadata["candidates"]:
            mapping = {"amount": "document.total", "date": "document.invoice_date",
                       "property_or_entity": "document.property", "vendor": "document.vendor_name",
                       "corporate_indicator": "document.economic_responsibility.economic_bearer",
                       "reimbursement_indicator": "document.economic_responsibility.settlement_treatment"}
            if candidate["candidate_type"] in mapping:
                add(mapping[candidate["candidate_type"]], candidate["normalized_value"], candidate["confidence"],
                    "filename_or_folder_parser", {"source_kind": candidate["source_kind"],
                    "source_part_index": candidate["source_part_index"]})
        patterns = (("document.invoice_number", r"(?im)\b(?:invoice|receipt)\s*(?:no\.?|number|#)?\s*[:#-]?\s*([A-Z0-9-]{3,})", .78),
                    ("document.total", r"(?im)\btotal\s*(?:due)?\s*[:$]?\s*([0-9,]+\.\d{2})", .82),
                    ("document.invoice_date", r"(?im)\b(?:invoice\s+)?date\s*[:#-]?\s*([0-9]{1,2}[/-][0-9]{1,2}[/-][0-9]{2,4})", .78))
        for path, pattern, confidence in patterns:
            match = re.search(pattern, text[:30000])
            if match: add(path, match.group(1), confidence, "deterministic_text_parser", {"page": None, "text": match.group(0)[:120]})
        return fields

    def _deterministic_lines(self, text: str) -> list[dict[str, Any]]:
        lines = []
        pattern = re.compile(r"^(.{3,120}?)\s+\$?([0-9,]+\.\d{2})\s*$")
        for source_line_number, raw in enumerate(text.splitlines(), 1):
            match = pattern.match(raw.strip())
            if not match or re.search(r"\b(?:subtotal|total|balance|tax|amount due)\b", match.group(1), re.I):
                continue
            description = re.sub(r"\s+", " ", match.group(1)).strip()
            semantic, gl_candidate = self._accounting_proposal(description, match.group(2), len(lines) + 1)
            lines.append({"line_id": f"proposal-line-{len(lines)+1}", "status": "unverified",
                "source": "deterministic_text_line_parser", "confidence": .68,
                "raw_description": description, "normalized_description": description.lower(),
                "amount": match.group(2).replace(",", ""), "quantity": None, "unit_price": None,
                "tax": None, "location_unit": None, "semantic_classification": semantic,
                "gl_candidate": gl_candidate, "property_candidate": None, "reimbursement_candidate": None,
                "evidence": {"page": None, "source_line_number": source_line_number, "text": raw[:160]},
                "profile_version": PROFILE_VERSION})
            if len(lines) == 25: break
        return lines

    @staticmethod
    def _accounting_proposal(description: str, amount: str, index: int) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        try:
            from .accounting_pipeline_v2 import decide_row
            row = {"Invoice Number": "assisted-proposal", "Line Item Description": description,
                   "Amount": amount.replace(",", ""), "GL Account": "",
                   "_meta": {"source_text": {"raw_description": description}}}
            decide_row(row, document_id="assisted-proposal", line_item_id=str(index),
                       extraction_route="assisted_deterministic_line")
            meta = row.get("_meta", {})
            decision = meta.get("accounting_decision") or {}
            gl = None
            if decision.get("selected_gl_code"):
                gl = {"gl_code": decision["selected_gl_code"], "gl_name": decision.get("selected_gl_name"),
                      "source": "AccountingDecisionEngine", "decision_version": decision.get("decision_version"),
                      "review_required": decision.get("review_required", True), "status": "unverified"}
            return meta.get("semantic_classification"), gl
        except Exception:
            return None, None

    @staticmethod
    def _line_fields(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
        fields = []
        for line in lines:
            values = {"raw_description": line.get("raw_description"), "normalized_description": line.get("normalized_description"),
                      "amount": line.get("amount"), "semantic_classification": line.get("semantic_classification"),
                      "gl_candidate": line.get("gl_candidate")}
            for key, value in values.items():
                if value is None: continue
                fields.append({"proposal_id": hashlib.sha256(f"{line['line_id']}|{key}|{value}".encode()).hexdigest()[:16],
                    "field_path": f"lines.{line['line_id']}.{key}", "proposed_value": value,
                    "source": line["source"], "status": "unverified", "confidence": line["confidence"],
                    "evidence": line["evidence"], "conflicts": [], "profile_version": PROFILE_VERSION})
        return fields

    @staticmethod
    def _correction_rate(events, fragment):
        relevant = [e for e in events if fragment in e["field_path"]]
        return sum(e["action"] == "correct" for e in relevant) / len(relevant) if relevant else 0
