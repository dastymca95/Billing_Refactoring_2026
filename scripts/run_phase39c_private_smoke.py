"""Run the authorized, exactly-three-document Phase 3.9C private smoke gate.

Private source identifiers and document-level results never leave the configured
private benchmark root. Console output is aggregate and Git-safe.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault(
    "INNER_VIEW_TEST_ASSET_ROOT",
    str(ROOT / "webapp" / "backend" / "tests" / "fixtures" / "runtime_assets"),
)

from webapp.backend.services import ai_provider
from webapp.backend.services.accounting_pipeline_v2 import capture_source_fields, decide_row
from webapp.backend.services.accounting_readiness import as_dict as readiness_dict, evaluate_rows
from webapp.backend.services.autonomous_adjudication import (
    AutonomousAdjudicator, ExtractedField, ExtractionPass, FieldEvidence, VerificationFinding,
)
from webapp.backend.services.gl_catalog import load_gl_catalog
from webapp.backend.services.provider_capabilities import ProfileLoader

EXPECTED_HASH = "8b5c065d8898a7aa32e56a150bc1cdf2f2a10599005f901000385313090ffcbf"
REQUIRED_PROFILES = {"runtime-text", "runtime-vision", "runtime-verification", "runtime-accounting"}
MAX_COST_USD = Decimal("1.00")


class SourceEvidence(BaseModel):
    page: int | None = None
    supporting_text: Any = None


class ExtractedValue(BaseModel):
    field_path: str
    value: Any = None
    confidence: float = Field(default=0, ge=0, le=1)
    evidence: list[SourceEvidence] = Field(default_factory=list)


class ExtractedLine(BaseModel):
    line_id: str | int
    raw_description: str | None = None
    amount: Any = None
    quantity: Any = None
    unit_price: Any = None
    tax: Any = None
    evidence: list[SourceEvidence] = Field(default_factory=list)


class DocumentExtraction(BaseModel):
    fields: list[ExtractedValue] = Field(default_factory=list)
    lines: list[ExtractedLine] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class VerificationOutput(BaseModel):
    fields: list[ExtractedValue] = Field(default_factory=list)
    lines: list[ExtractedLine] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class AccountingCandidate(BaseModel):
    line_id: str
    gl_code: str | None = None
    confidence: float = Field(ge=0, le=1)
    evidence_summary: str


class AccountingCandidates(BaseModel):
    candidates: list[AccountingCandidate]


def load_private_env(path: Path) -> None:
    if not path.exists():
        return
    parsed: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name, value = name.strip(), value.strip().strip('"').strip("'")
        if name:
            parsed[name] = value
    for name, value in parsed.items():
        if name not in os.environ:
            os.environ[name] = value


def canonical_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().rstrip(b"\n")).hexdigest()


def select_documents(snapshot: dict[str, Any], private_root: Path) -> list[tuple[str, dict[str, Any], Path]]:
    rows = list(snapshot["selection"])
    specs = [
        ("digital_invoice", lambda r: r.get("selection_cohort") == "digital_vendor_invoices"
         and int(r.get("page_count") or 0) == 1
         and str(r.get("private_relative_path", "")).lower().endswith(".pdf")),
        ("scanned_photo_receipt", lambda r: r.get("selection_cohort") == "clean_photos_receipts" and Path(str(r.get("private_relative_path", ""))).suffix.lower() in {".jpg", ".jpeg", ".png"}),
        ("handwriting_heavy", lambda r: r.get("selection_cohort") == "handwritten" and Path(str(r.get("private_relative_path", ""))).suffix.lower() in {".jpg", ".jpeg", ".png"}),
    ]
    chosen = []
    for category, predicate in specs:
        candidates = sorted((r for r in rows if predicate(r)), key=lambda r: str(r.get("benchmark_id")))
        if not candidates:
            raise RuntimeError(f"private_source_missing:{category}")
        row = candidates[0]
        source = (private_root / str(row["private_relative_path"])).resolve()
        if private_root.resolve() not in source.parents or not source.is_file():
            raise RuntimeError(f"private_source_invalid:{category}")
        chosen.append((category, row, source))
    return chosen


def pdf_text(path: Path) -> str:
    from pypdf import PdfReader
    return "\n\n".join(f"[PAGE {i}]\n{page.extract_text() or ''}" for i, page in enumerate(PdfReader(str(path)).pages, 1))


def image_url(path: Path) -> str:
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def source_context(path: Path, private_root: Path) -> str:
    relative = path.relative_to(private_root)
    parents = [part for part in relative.parts[:-1][-3:] if part not in {".", ".."}]
    return json.dumps({"original_filename": path.name, "relevant_parent_folders": parents}, ensure_ascii=False)


def schema_prompt() -> str:
    fields = ["document.document_family", "document.vendor", "document.invoice_number", "document.invoice_date",
              "document.due_date", "document.total", "document.property", "document.payment_source",
              "document.economic_bearer", "document.settlement_treatment", "document.reimbursement_required",
              "document.allocation_scope"]
    return ("Return one JSON object with fields, lines, warnings. fields entries require field_path, value, confidence, "
            "evidence[{page,supporting_text}]. Include every requested field, using null when unobserved: " + ", ".join(fields) +
            ". Lines require line_id, raw_description, amount, quantity, unit_price, tax, evidence. "
            "Extract observable facts only. Filename/folders are non-authoritative evidence and conflicts must be warnings. "
            "Do not choose a GL and do not decide readiness.")


def call_json(profile, system: str, content: Any, output_model):
    payload = {"model": profile.model_id, "response_format": {"type": "json_object"},
               "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}]}
    if profile.provider == "openai":
        payload.update({"max_completion_tokens": 4096, "reasoning_effort": "low"})
    else:
        payload.update({"max_tokens": 4096, "temperature": 0})
    started = time.perf_counter()
    raw = ai_provider._send_chat_completion(provider=profile.provider, payload=payload, vision=profile.vision,
        api_key_override=profile.api_key.get_secret_value() if profile.api_key else None,
        base_url_override=profile.base_url, timeout_seconds_override=max(90, profile.timeout_seconds),
        max_attempts_override=min(2, profile.max_retries + 1))
    latency = (time.perf_counter() - started) * 1000
    decoded = json.loads(raw)
    for wrapper in ("extraction", "verification", "result"):
        if isinstance(decoded, dict) and isinstance(decoded.get(wrapper), dict):
            decoded = decoded[wrapper]
            break
    if output_model in {DocumentExtraction, VerificationOutput} and isinstance(decoded, dict):
        for index, item in enumerate(decoded.get("fields") or []):
            if isinstance(item, dict):
                item.setdefault("field_path", item.get("field") or item.get("path") or f"unmapped.{index}")
                item["field_path"] = str(item["field_path"])
                try:
                    confidence = float(str(item.get("confidence") or 0).rstrip("%"))
                    item["confidence"] = min(1.0, confidence / 100 if confidence > 1 else confidence)
                except ValueError: item["confidence"] = 0
                if isinstance(item.get("evidence"), dict): item["evidence"] = [item["evidence"]]
                item["evidence"] = [{"page": 1, "supporting_text": e} if not isinstance(e, dict) else e
                                    for e in (item.get("evidence") or [])]
                for e in item["evidence"]:
                    try: e["page"] = int(e.get("page")) if e.get("page") is not None else None
                    except (TypeError, ValueError): e["page"] = None
        for index, item in enumerate(decoded.get("lines") or []):
            if isinstance(item, dict):
                item.setdefault("line_id", item.get("id") or str(index + 1))
                if item.get("raw_description") is not None:
                    item["raw_description"] = (str(item["raw_description"]) if not isinstance(item["raw_description"], (dict, list))
                                               else json.dumps(item["raw_description"], ensure_ascii=False))
                if isinstance(item.get("evidence"), dict): item["evidence"] = [item["evidence"]]
                item["evidence"] = [{"page": 1, "supporting_text": e} if not isinstance(e, dict) else e
                                    for e in (item.get("evidence") or [])]
    parsed = output_model.model_validate(decoded)
    input_chars = len(system) + len(json.dumps(content, ensure_ascii=False))
    estimated_cost = Decimal(str((input_chars / 4 * 5 + len(raw) / 4 * 30) / 1_000_000))
    return parsed, latency, estimated_cost


def evidence(field: ExtractedValue, profile: str) -> list[FieldEvidence]:
    return [FieldEvidence(page=e.page, source_type="private_source", extraction_profile=profile,
                          raw_supporting_text=str(e.supporting_text) if e.supporting_text is not None else None)
            for e in field.evidence]


def extraction_pass(result: DocumentExtraction, profile) -> ExtractionPass:
    return ExtractionPass(pass_id="primary", profile_version="phase39c-smoke/1.0", model_id=profile.model_id,
        model_family=profile.model_family, route=profile.profile_id,
        fields=[ExtractedField(field_path=f.field_path, value=f.value, confidence=f.confidence,
                               evidence=evidence(f, profile.profile_id), source_pass="primary") for f in result.fields],
        lines=[line.model_dump(mode="json") for line in result.lines], warnings=result.warnings)


def verification_findings(result: VerificationOutput, profile) -> list[VerificationFinding]:
    return [VerificationFinding(field_path=f.field_path, disposition="confirmed" if f.value not in (None, "") else "missing",
        value=f.value, confidence=f.confidence, evidence=evidence(f, profile.profile_id)) for f in result.fields]


def value_map(result: DocumentExtraction) -> dict[str, Any]:
    return {f.field_path: f.value for f in result.fields}


def dec(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value).replace("$", "").replace(",", "")) if value not in (None, "") else None
    except (InvalidOperation, ValueError):
        return None


def private_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def main() -> int:
    load_private_env(ROOT / ".env")
    private_root = Path(os.environ.get(
        "INNER_VIEW_PRIVATE_BENCHMARK_ROOT",
        r"C:\Users\Dasty\PycharmProjects\Innerview_Private_Benchmark",
    )).resolve()
    snapshot_path = private_root / "selection" / "selected_120_v1.json"
    before_hash = canonical_hash(snapshot_path)
    if before_hash != EXPECTED_HASH:
        raise RuntimeError("dataset_hash_mismatch")
    profiles = {p.profile_id: p for p in ProfileLoader().load()}
    if not REQUIRED_PROFILES.issubset(profiles) or any(not profiles[p].credentials_present for p in REQUIRED_PROFILES):
        raise RuntimeError("required_verified_profile_missing")
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    selected = select_documents(snapshot, private_root)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    run_root = private_root / ".private" / "phase_3_9c" / "smoke_runs" / run_id
    run_root.mkdir(parents=True, exist_ok=False)
    totals = {"provider_calls": 0, "estimated_cost_usd": Decimal("0"), "latencies_ms": [], "exceptions": 0,
              "ready": 0, "documents": 3, "critical_failures": []}
    trace_index = []
    _, catalog = load_gl_catalog()
    catalog_prompt = [{"gl_code": code, "name": account.gl_name} for code, account in catalog.items() if account.payable]
    for index, (category, _, source) in enumerate(selected, 1):
        anonymous_id = f"smoke-{index}"
        metadata = source_context(source, private_root)
        if category == "digital_invoice":
            primary_profile = profiles["runtime-text"]
            content = schema_prompt() + "\nSource metadata: " + metadata + "\nDocument text:\n" + pdf_text(source)
        else:
            primary_profile = profiles["runtime-vision"]
            content = [{"type": "text", "text": schema_prompt() + "\nSource metadata: " + metadata},
                       {"type": "image_url", "image_url": {"url": image_url(source)}}]
        primary, latency, cost = call_json(primary_profile, "Independent source-facts extraction. Strict JSON only.", content, DocumentExtraction)
        totals["provider_calls"] += 1; totals["estimated_cost_usd"] += cost; totals["latencies_ms"].append(latency)
        private_write(run_root / "extraction" / f"{anonymous_id}.json", primary.model_dump(mode="json"))
        verifier = profiles["runtime-verification"]
        if source.suffix.lower() == ".pdf":
            verify_content = schema_prompt() + "\nIndependently verify; no primary output is provided.\nSource metadata: " + metadata + "\nDocument text:\n" + pdf_text(source)
        else:
            verify_content = [{"type": "text", "text": schema_prompt() + "\nIndependently verify; no primary output is provided.\nSource metadata: " + metadata},
                              {"type": "image_url", "image_url": {"url": image_url(source)}}]
        verified, latency, cost = call_json(verifier, "Isolated same-family verification. Strict JSON only.", verify_content, VerificationOutput)
        totals["provider_calls"] += 1; totals["estimated_cost_usd"] += cost; totals["latencies_ms"].append(latency)
        private_write(run_root / "verification" / f"{anonymous_id}.json", verified.model_dump(mode="json"))
        adjudicated = AutonomousAdjudicator().adjudicate(anonymous_id,
            deterministic_primary=extraction_pass(primary, primary_profile),
            deterministic_verification=verification_findings(verified, verifier),
            visual_required=category != "digital_invoice")
        private_write(run_root / "consensus" / f"{anonymous_id}.json", [x.model_dump(mode="json") for x in adjudicated.consensus])
        reason_content = json.dumps({"observable_facts": primary.model_dump(mode="json"), "payable_chart": catalog_prompt}, ensure_ascii=False)
        candidates, latency, cost = call_json(profiles["runtime-accounting"],
            "Return JSON {candidates:[{line_id,gl_code,confidence,evidence_summary}]}. Propose candidates only from the supplied chart. Do not select final GL or decide readiness.",
            reason_content, AccountingCandidates)
        totals["provider_calls"] += 1; totals["estimated_cost_usd"] += cost; totals["latencies_ms"].append(latency)
        candidate_by_line = {str(c.line_id): c for c in candidates.candidates}
        values = value_map(primary); rows = []
        for line_no, line in enumerate(primary.lines, 1):
            candidate = candidate_by_line.get(str(line.line_id))
            row = {"Invoice Number": values.get("document.invoice_number"), "Bill or Credit": "Bill",
                   "Invoice Date": values.get("document.invoice_date"), "Accounting Date": values.get("document.invoice_date"),
                   "Vendor": values.get("document.vendor"), "Invoice Description": values.get("document.document_family") or "Invoice",
                   "Line Item Number": line_no, "Property Abbreviation": values.get("document.property"), "GL Account": "",
                   "Amount": dec(line.amount), "Line Item Description": line.raw_description, "Expense Type": "Expense",
                   "Is Replacement Reserve": False, "Due Date": values.get("document.due_date"), "Document Url": f"private://{anonymous_id}",
                   "_meta": {"invoice_group_id": anonymous_id, "source_page": line.evidence[0].page if line.evidence else None,
                             "source_line_description": line.raw_description, "ai_source_gl_candidate": candidate.gl_code if candidate else None,
                             "extraction_model": primary_profile.model_id}}
            capture_source_fields(row, document_id=anonymous_id, line_item_id=str(line.line_id))
            decide_row(row, document_id=anonymous_id, line_item_id=str(line.line_id), extraction_route=primary_profile.profile_id)
            rows.append(row)
        expected_total = dec(values.get("document.total")); actual_total = sum((dec(r.get("Amount")) or Decimal("0") for r in rows), Decimal("0"))
        reconciled = expected_total is not None and abs(expected_total - actual_total) <= Decimal("0.01")
        for row in rows: row["_meta"]["total_reconciliation_passed"] = reconciled
        readiness = evaluate_rows(rows)
        result = {"category": category, "status": adjudicated.status.value, "arithmetic": [x.model_dump(mode="json") for x in adjudicated.arithmetic_validation],
                  "property": adjudicated.property_resolution.model_dump(mode="json"),
                  "economic_responsibility": adjudicated.economic_responsibility.model_dump(mode="json"),
                  "reimbursement": adjudicated.reimbursement_resolution.model_dump(mode="json"), "rows": rows,
                  "readiness": readiness_dict(readiness), "exception_codes": adjudicated.exception_codes,
                  "verification_independence": "isolated_same_family"}
        private_write(run_root / "adjudication" / f"{anonymous_id}.json", result)
        private_write(run_root / "validation" / f"{anonymous_id}.json", {"schema_valid": True, "arithmetic_reconciled": reconciled,
            "primary_evidence_complete": all((f.value in (None, "") or f.evidence) for f in primary.fields),
            "verification_evidence_complete": all((f.value in (None, "") or f.evidence) for f in verified.fields)})
        is_exception = bool(adjudicated.exception_codes or not readiness.export_allowed)
        totals["exceptions"] += int(is_exception); totals["ready"] += int(readiness.export_allowed)
        trace_index.extend([{"document": anonymous_id, "profile": p, "trace_id": f"{p}:{run_id}:{anonymous_id}",
                             "cache_key": hashlib.sha256(f"{p}:{run_id}:{anonymous_id}".encode()).hexdigest()}
                            for p in (primary_profile.profile_id, verifier.profile_id, "runtime-accounting")])
    if totals["estimated_cost_usd"] > MAX_COST_USD:
        totals["critical_failures"].append("cost_budget_exceeded")
    after_hash = canonical_hash(snapshot_path)
    if after_hash != before_hash: totals["critical_failures"].append("dataset_hash_changed")
    latencies = sorted(totals["latencies_ms"])
    safe = {"schema_version": "phase-3.9c-private-smoke/1.0", "run_id": run_id, "documents": 3,
            "categories": ["digital_invoice", "scanned_photo_receipt", "handwriting_heavy"],
            "provider_calls": totals["provider_calls"], "ready": totals["ready"], "exception_required": totals["exceptions"],
            "estimated_cost_usd": round(float(totals["estimated_cost_usd"]), 6),
            "latency_p50_ms": round(latencies[len(latencies)//2], 1), "latency_p95_ms": round(latencies[-1], 1),
            "dataset_hash_verified": after_hash == EXPECTED_HASH, "verification_independence": "isolated_same_family",
            "critical_failures": totals["critical_failures"], "contains_private_identifiers": False}
    private_write(run_root / "manifest.json", {"run_id": run_id, "dataset_hash": before_hash,
        "documents": [{"anonymous_id": f"smoke-{i}", "category": item[0], "private_relative_path": str(item[2].relative_to(private_root))}
                      for i, item in enumerate(selected, 1)]})
    private_write(run_root / "metrics.json", safe); private_write(run_root / "trace_index.json", trace_index)
    print(json.dumps(safe, indent=2, sort_keys=True))
    return 1 if totals["critical_failures"] else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        safe = {"status": "blocked_smoke_test_failed", "failure_code": type(exc).__name__}
        if isinstance(exc, ValidationError):
            safe["schema_errors"] = [{"location": [str(x) for x in e["loc"]], "type": e["type"]} for e in exc.errors()]
        print(json.dumps(safe, sort_keys=True))
        raise SystemExit(1)
