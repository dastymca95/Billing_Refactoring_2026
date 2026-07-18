"""Human-governed deterministic processor improvement workspace.

Private samples and conversations produce a typed declarative patch. The AI
cannot write Python, activate a patch, select final GL, decide readiness, or
authorize export. Activation requires a successful dry-run preview and an
explicit revision-bound approval.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .. import settings
from . import ai_provider, batch_processor, batch_store, deterministic_coverage, document_ingestion
from . import rules_impact, vendor_rules
from .accounting_assistant import _estimate_cost
from .semantic_reasoning_gateway import _select_accounting_profile


CONTRACT_VERSION = "deterministic-builder/1.0"
ROOT = settings.WEBAPP_DATA_ROOT / "deterministic_builder"
ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".csv", ".xlsx"}
MAX_SAMPLE_BYTES = 30 * 1024 * 1024
MAX_SAMPLES = 25
_LOCK = threading.RLock()


class BuilderSample(BaseModel):
    sample_id: str
    original_filename: str
    source_type: str
    size_bytes: int
    page_count: int = 0
    sha256: str
    text_available: bool = False
    warnings: list[str] = Field(default_factory=list)
    uploaded_at: datetime


class BuilderMessage(BaseModel):
    message_id: str
    role: Literal["user", "assistant", "system"]
    content: str
    created_at: datetime
    provider_profile_id: str | None = None
    estimated_cost_usd: float = 0
    proposed_paths: list[str] = Field(default_factory=list)


class BuilderPreview(BaseModel):
    status: Literal["not_run", "passed", "failed"] = "not_run"
    revision: int = 0
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0
    warnings: list[str] = Field(default_factory=list)
    generated_at: datetime | None = None


class DeterministicBuilderSession(BaseModel):
    contract_version: str = CONTRACT_VERSION
    session_id: str
    vendor_key: str
    vendor_name: str
    status: Literal["draft", "previewed", "approved", "rejected"] = "draft"
    revision: int = 0
    selected_column: str | None = None
    samples: list[BuilderSample] = Field(default_factory=list)
    messages: list[BuilderMessage] = Field(default_factory=list)
    draft_patch: dict[str, Any] = Field(default_factory=dict)
    draft_rationales: dict[str, str] = Field(default_factory=dict)
    validation_issues: list[dict[str, Any]] = Field(default_factory=list)
    preview: BuilderPreview = Field(default_factory=BuilderPreview)
    created_at: datetime
    updated_at: datetime
    audit: list[dict[str, Any]] = Field(default_factory=list)


class ProposedConfigChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=300)
    value: Any
    rationale: str = Field(min_length=3, max_length=1000)


class BuilderModelResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assistant_message: str = Field(min_length=1, max_length=4000)
    proposed_changes: list[ProposedConfigChange] = Field(default_factory=list, max_length=100)


def create_session(vendor_key: str, *, actor: str = "local_operator") -> DeterministicBuilderSession:
    coverage = deterministic_coverage.coverage_for_key(vendor_key)
    if coverage is None:
        raise ValueError("Vendor has no registered deterministic processor.")
    if not coverage.editable:
        raise ValueError("This processor is code-managed and has no verified editable contract.")
    now = _now()
    session = DeterministicBuilderSession(
        session_id="dbs_" + uuid.uuid4().hex[:16],
        vendor_key=vendor_key,
        vendor_name=coverage.display_name,
        created_at=now,
        updated_at=now,
        messages=[BuilderMessage(
            message_id="dbm_" + uuid.uuid4().hex[:12], role="system",
            content=("Upload representative bills, then describe the deterministic behavior you want. "
                     "Nothing is activated until preview and explicit approval."),
            created_at=now,
        )],
        audit=[{"event": "session_created", "actor": actor, "at": now.isoformat()}],
    )
    _write(session)
    return session


def get_session(session_id: str) -> DeterministicBuilderSession:
    path = _session_path(session_id)
    if not path.is_file():
        raise KeyError(session_id)
    return DeterministicBuilderSession.model_validate_json(path.read_text(encoding="utf-8"))


def add_sample(
    session_id: str, *, original_filename: str, content: bytes, actor: str = "local_operator",
) -> DeterministicBuilderSession:
    session = get_session(session_id)
    if len(session.samples) >= MAX_SAMPLES:
        raise ValueError(f"A session supports at most {MAX_SAMPLES} samples.")
    safe_name = Path(original_filename or "sample").name
    suffix = Path(safe_name).suffix.casefold()
    if suffix not in ALLOWED_EXTENSIONS:
        raise ValueError(f"Unsupported sample extension: {suffix or '(none)'}.")
    if not content:
        raise ValueError("Uploaded sample is empty.")
    if len(content) > MAX_SAMPLE_BYTES:
        raise ValueError("Uploaded sample exceeds the 30 MB limit.")
    sample_id = "dbf_" + uuid.uuid4().hex[:14]
    sample_dir = _session_dir(session_id) / "samples" / sample_id
    sample_dir.mkdir(parents=True, exist_ok=False)
    source = sample_dir / ("source" + suffix)
    source.write_bytes(content)
    candidate = document_ingestion.ingest_document(
        source, vendor_hint=session.vendor_name, allow_ocr=True, allow_vision_hint=True,
    )
    evidence = candidate.to_dict()
    evidence["source_path"] = ""  # never serialize the private absolute path
    (sample_dir / "evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8",
    )
    sample = BuilderSample(
        sample_id=sample_id, original_filename=safe_name,
        source_type=candidate.source_type, size_bytes=len(content), page_count=candidate.page_count,
        sha256=hashlib.sha256(content).hexdigest(),
        text_available=bool(candidate.document_text.strip()), warnings=list(candidate.warnings),
        uploaded_at=_now(),
    )
    session.samples.append(sample)
    session.revision += 1
    session.status = "draft"
    session.preview = BuilderPreview()
    session.updated_at = _now()
    session.audit.append({"event": "sample_added", "actor": actor, "at": session.updated_at.isoformat(),
                          "sample_id": sample_id, "sha256": sample.sha256})
    _write(session)
    return session


def chat(
    session_id: str, *, message: str, selected_column: str | None = None,
    actor: str = "local_operator",
) -> DeterministicBuilderSession:
    session = get_session(session_id)
    message = str(message or "").strip()
    if not message:
        raise ValueError("A message is required.")
    if len(message) > 4000:
        raise ValueError("Message exceeds the 4000-character limit.")
    if selected_column and session.preview.columns and selected_column not in session.preview.columns:
        raise ValueError("Selected column is not part of the current preview.")
    session.selected_column = selected_column or session.selected_column
    session.messages.append(BuilderMessage(
        message_id="dbm_" + uuid.uuid4().hex[:12], role="user", content=message, created_at=_now(),
    ))
    profile = _select_accounting_profile()
    if profile is None:
        raise ai_provider.AIProviderNotConfigured(
            "No probe-verified accounting reasoning profile is available.",
            failure_code="deterministic_builder_profile_unavailable",
        )
    prompt = _builder_prompt(session, message)
    estimated_cost = _estimate_cost(profile, {"messages": prompt}, 1800)
    limit = float(os.environ.get("AI_MAX_DETERMINISTIC_BUILDER_COST_USD", "0.03") or 0.03)
    if estimated_cost > limit:
        raise ai_provider.AIProviderUnavailable(
            "Deterministic builder request exceeds its configured cost budget.",
            failure_code="deterministic_builder_cost_budget_exceeded",
        )
    response = _request_builder_model(profile, prompt)
    accepted: dict[str, Any] = {}
    rationales: dict[str, str] = {}
    rejected: list[str] = []
    for change in response.proposed_changes:
        issues = vendor_rules.validate_patch(session.vendor_key, {change.path: change.value})
        if issues:
            rejected.append(change.path)
            continue
        accepted[change.path] = change.value
        rationales[change.path] = change.rationale
    if accepted:
        session.draft_patch.update(accepted)
        session.draft_rationales.update(rationales)
        session.revision += 1
        session.status = "draft"
        session.preview = BuilderPreview()
    session.validation_issues = vendor_rules.validate_patch(
        session.vendor_key, session.draft_patch,
    ) if session.draft_patch else []
    answer = response.assistant_message
    if rejected:
        answer += " Some proposed fields were rejected by the deterministic contract: " + ", ".join(rejected) + "."
    session.messages.append(BuilderMessage(
        message_id="dbm_" + uuid.uuid4().hex[:12], role="assistant", content=answer,
        created_at=_now(), provider_profile_id=profile.profile_id,
        estimated_cost_usd=estimated_cost, proposed_paths=sorted(accepted),
    ))
    session.updated_at = _now()
    session.audit.append({"event": "ai_draft_proposed", "actor": actor, "at": session.updated_at.isoformat(),
                          "revision": session.revision, "paths": sorted(accepted),
                          "profile_id": profile.profile_id})
    _write(session)
    return session


def preview(session_id: str, *, actor: str = "local_operator") -> DeterministicBuilderSession:
    session = get_session(session_id)
    if not session.samples:
        raise ValueError("Upload at least one sample before previewing.")
    if not session.draft_patch:
        raise ValueError("The draft has no proposed changes to preview.")
    issues = vendor_rules.validate_patch(session.vendor_key, session.draft_patch)
    if issues:
        raise ValueError("Draft validation failed: " + json.dumps(issues, ensure_ascii=False))
    batch_id = batch_store.create_batch()
    temp_config: Path | None = None
    try:
        input_dir = batch_store.get_input_dir(batch_id)
        for sample in session.samples:
            sample_dir = _session_dir(session_id) / "samples" / sample.sample_id
            source = next((item for item in sample_dir.glob("source.*") if item.is_file()), None)
            if source:
                destination = input_dir / sample.original_filename
                if destination.exists():
                    destination = input_dir / f"{destination.stem}_{sample.sample_id}{destination.suffix}"
                shutil.copy2(source, destination)
        rules = vendor_rules.load_vendor_rules(session.vendor_key)
        for dotted, value in session.draft_patch.items():
            vendor_rules._set_dotted(rules, dotted, value)
        fd, tmp = tempfile.mkstemp(prefix="builder_draft_", suffix=".yaml", dir=str(_session_dir(session_id)))
        os.close(fd)
        temp_config = Path(tmp)
        temp_config.write_text(yaml.safe_dump(rules, sort_keys=False, allow_unicode=True), encoding="utf-8")
        result = batch_processor.process_batch(
            batch_id, dry_run=True, rules_override_paths={session.vendor_key: temp_config},
            forced_vendor_key=session.vendor_key,
        )
        rows = rules_impact._flatten_rows(result, session.vendor_key)
        public_rows = [{key: value for key, value in row.items() if not key.startswith("__")} for row in rows[:200]]
        columns = sorted({key for row in public_rows for key in row})
        warnings = [] if rows else ["No rows were produced for the uploaded samples."]
        session.preview = BuilderPreview(
            status="passed" if rows else "failed", revision=session.revision,
            columns=columns, rows=public_rows, row_count=len(rows), warnings=warnings,
            generated_at=_now(),
        )
        session.status = "previewed" if rows else "draft"
    except Exception as exc:
        session.preview = BuilderPreview(
            status="failed", revision=session.revision,
            warnings=[f"preview_failed:{type(exc).__name__}"], generated_at=_now(),
        )
        session.status = "draft"
        session.updated_at = _now()
        session.audit.append({"event": "preview_failed", "actor": actor, "at": session.updated_at.isoformat(),
                              "failure_code": type(exc).__name__})
        _write(session)
        raise ValueError("Sample preview failed safely. No rule was activated.") from exc
    finally:
        if temp_config and temp_config.exists():
            temp_config.unlink(missing_ok=True)
        try:
            batch_store.delete_batch(batch_id)
        except FileNotFoundError:
            pass
    session.updated_at = _now()
    session.audit.append({"event": "preview_completed", "actor": actor, "at": session.updated_at.isoformat(),
                          "revision": session.revision, "row_count": session.preview.row_count})
    _write(session)
    return session


def approve(
    session_id: str, *, expected_revision: int, actor: str = "local_operator",
) -> DeterministicBuilderSession:
    session = get_session(session_id)
    if expected_revision != session.revision:
        raise ValueError("Draft revision changed; run preview again before approval.")
    if session.preview.status != "passed" or session.preview.revision != session.revision:
        raise ValueError("A passing preview for the current revision is required before approval.")
    if session.validation_issues:
        raise ValueError("Draft has unresolved validation issues.")
    result = vendor_rules.apply_patch(session.vendor_key, session.draft_patch)
    session.status = "approved"
    session.updated_at = _now()
    session.audit.append({"event": "draft_approved", "actor": actor, "at": session.updated_at.isoformat(),
                          "revision": session.revision, "backup_filename": result["backup_filename"],
                          "written_paths": result["written_paths"]})
    _write(session)
    return session


def _builder_prompt(session: DeterministicBuilderSession, message: str) -> list[dict[str, str]]:
    groups = vendor_rules.editable_groups(session.vendor_key)
    allowed = [{"path": field["path"], "type": field["type"], "value": field.get("value")}
               for group in groups for field in group.get("fields", []) if field.get("editable")]
    evidence: list[dict[str, Any]] = []
    for sample in session.samples[:8]:
        path = _session_dir(session.session_id) / "samples" / sample.sample_id / "evidence.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        evidence.append({"filename": sample.original_filename, "source_type": sample.source_type,
                         "document_text": str(data.get("document_text") or "")[:12000],
                         "warnings": data.get("warnings") or []})
    history = [{"role": item.role, "content": item.content} for item in session.messages[-12:] if item.role != "system"]
    system = (
        "You are InnerView's deterministic processor builder. Converse naturally, but only propose changes to "
        "the allowed declarative fields supplied below. Never propose Python, vendor-specific hidden code, final GL "
        "selection, readiness, export authorization, or automatic activation. Use sample document evidence. If the "
        "operator selected a preview column, treat it as the explicit scope of the instruction. Return one JSON object "
        "with assistant_message and proposed_changes [{path,value,rationale}]. An empty proposed_changes list is valid."
    )
    context = {"vendor_key": session.vendor_key, "selected_column": session.selected_column,
               "current_draft": session.draft_patch, "allowed_fields": allowed,
               "samples": evidence, "preview_columns": session.preview.columns,
               "operator_message": message}
    return [{"role": "system", "content": system}, *history,
            {"role": "user", "content": json.dumps(context, ensure_ascii=False, default=str)}]


def _request_builder_model(profile: Any, messages: list[dict[str, str]]) -> BuilderModelResponse:
    payload: dict[str, Any] = {
        "model": profile.model_id, "response_format": {"type": "json_object"}, "messages": messages,
    }
    if profile.provider == "deepseek":
        payload["thinking"] = {"type": "disabled"}
    payload.update(ai_provider._completion_controls(profile.provider, 1800))
    raw = ai_provider._send_chat_completion(
        provider=profile.provider, payload=payload,
        api_key_override=profile.api_key.get_secret_value() if profile.api_key else None,
        base_url_override=profile.base_url, timeout_seconds_override=profile.timeout_seconds,
        max_attempts_override=profile.max_retries + 1,
    )
    try:
        return BuilderModelResponse.model_validate(ai_provider._extract_json_object(raw))
    except ValidationError as exc:
        raise ai_provider.AIProviderInvalidSchema(
            "AI response did not match the deterministic builder contract.",
        ) from exc


def _session_dir(session_id: str) -> Path:
    if not session_id.startswith("dbs_") or not session_id[4:].isalnum():
        raise ValueError("Invalid deterministic builder session id.")
    path = (ROOT / session_id).resolve()
    path.relative_to(ROOT.resolve())
    return path


def _session_path(session_id: str) -> Path:
    return _session_dir(session_id) / "session.json"


def _write(session: DeterministicBuilderSession) -> None:
    with _LOCK:
        directory = _session_dir(session.session_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "session.json"
        tmp = directory / ".session.json.tmp"
        tmp.write_text(session.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(path)


def _now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "BuilderMessage", "BuilderModelResponse", "BuilderPreview", "BuilderSample",
    "CONTRACT_VERSION", "DeterministicBuilderSession", "ProposedConfigChange",
    "add_sample", "approve", "chat", "create_session", "get_session", "preview",
]
