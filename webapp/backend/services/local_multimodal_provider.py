"""Typed loopback-only Ollama adapter for private invoice fact extraction."""

from __future__ import annotations

import base64
import contextvars
import ctypes
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .local_inference_guard import assert_dispatch_allowed, validate_loopback_endpoint


LOCAL_PROVIDER_CONTRACT_VERSION = "local-multimodal-provider/1.0"


class LocalEvidenceReference(BaseModel):
    page: int | None = None
    bbox: list[float] | dict[str, float] | None = None
    raw_text: str | None = None
    confidence: Any = None
    status: Literal["observed", "inferred", "unknown"] = "observed"


class LocalExtractedField(BaseModel):
    value: Any = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    status: Literal["observed", "inferred", "unknown"] = "unknown"
    evidence: list[LocalEvidenceReference] = Field(default_factory=list)


class LocalLineItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    source_page: int | None = None
    row_label: str | None = None
    raw_description: str | None = None
    description: str | None = None
    quantity: Any = None
    unit_price: Any = None
    amount: Any = None
    confidence: Any = None
    evidence: list[LocalEvidenceReference] = Field(default_factory=list)


class LocalInvoiceExtraction(BaseModel):
    """Permissive typed view over the existing normalized extraction contract."""

    model_config = ConfigDict(extra="allow")

    vendor_name: str | None = None
    invoice_number: str | None = None
    invoice_date: str | None = None
    service_date: str | None = None
    due_date: str | None = None
    due_date_text: str | None = None
    payment_terms: str | None = None
    property_candidate: str | None = None
    location_candidate: str | None = None
    service_address: str | None = None
    subtotal: Any = None
    tax_amount: Any = None
    total_amount: Any = None
    line_items: list[LocalLineItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    needs_manual_review: bool = False
    visual_extraction_status: str | None = None


class LocalResourceMetrics(BaseModel):
    system_ram_used_mb_before: float | None = None
    system_ram_used_mb_after: float | None = None
    gpu_memory_used_mb_before: int | None = None
    gpu_memory_used_mb_after: int | None = None
    gpu_utilization_percent_after: int | None = None


class LocalMultimodalResult(BaseModel):
    contract_version: str = LOCAL_PROVIDER_CONTRACT_VERSION
    request_id: str
    provider: str = "local_ollama"
    model: str
    model_version: str | None = None
    execution_profile: str
    response_channel: Literal["content", "validated_thinking"] = "content"
    document_id: str | None = None
    page_identifiers: list[str] = Field(default_factory=list)
    extracted_fields: dict[str, LocalExtractedField] = Field(default_factory=dict)
    line_items: list[LocalLineItem] = Field(default_factory=list)
    structured_output: dict[str, Any]
    confidence: float | None = None
    warnings: list[str] = Field(default_factory=list)
    failure_reason: str | None = None
    latency_ms: float
    resources: LocalResourceMetrics
    completed_at: datetime


_LAST_RESULT: contextvars.ContextVar[LocalMultimodalResult | None] = contextvars.ContextVar(
    "innerview_local_multimodal_last_result", default=None,
)


class LocalMultimodalProviderError(RuntimeError):
    def __init__(self, failure_code: str) -> None:
        super().__init__(failure_code)
        self.failure_code = failure_code


def last_result() -> LocalMultimodalResult | None:
    return _LAST_RESULT.get()


class LocalMultimodalProvider:
    """Call one Ollama instance over loopback and validate structured output."""

    def __init__(
        self,
        *,
        model: str,
        base_url: str = "http://127.0.0.1:11434",
        profile_id: str = "phase-a-local-vision",
        timeout_seconds: int = 180,
    ) -> None:
        endpoint = validate_loopback_endpoint(provider="local_ollama", base_url=base_url)
        self.model = str(model or "").strip()
        if not self.model:
            raise ValueError("local_model_required")
        self.base_url = endpoint.base_url.rstrip("/")
        self.profile_id = str(profile_id or "phase-a-local-vision")
        self.timeout_seconds = max(10, int(timeout_seconds))

    def chat_completion(self, payload: dict[str, Any]) -> LocalMultimodalResult:
        started = time.perf_counter()
        request_id = uuid.uuid4().hex
        before = _resource_snapshot()
        messages, page_ids = _ollama_messages(payload.get("messages") or [])
        prompt_text = "\n".join(str(row.get("content") or "") for row in messages).lower()
        invoice_contract = bool(page_ids) or (
            "invoice" in prompt_text and ("extract" in prompt_text or "observable" in prompt_text)
        )
        request_payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "format": _ollama_invoice_schema() if invoice_contract else "json",
            "think": False,
            "options": {
                "temperature": 0,
                "seed": 42,
                "num_ctx": min(16_384, max(4_096, int(
                    os.environ.get("LOCAL_MULTIMODAL_CONTEXT_TOKENS", "8192")
                ))),
                "num_predict": min(4_096, max(512, int(
                    payload.get("max_output_tokens")
                    or payload.get("max_completion_tokens")
                    or payload.get("max_tokens")
                    or 4096
                ))),
            },
            "keep_alive": "10m",
        }
        url = f"{self.base_url}/api/chat"
        assert_dispatch_allowed(provider="local_ollama", url=url, stage="local_multimodal")
        raw = json.dumps(request_payload, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=raw,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                envelope = json.loads(response.read(4_000_000).decode("utf-8", "replace"))
            content = str((envelope.get("message") or {}).get("content") or "")
            response_channel: Literal["content", "validated_thinking"] = "content"
            if content.strip():
                parsed = _enforce_candidate_only(_parse_json_object(content))
            else:
                parsed = _validated_structured_thinking(envelope)
                if parsed is None:
                    raise LocalMultimodalProviderError(_empty_content_failure(envelope))
                response_channel = "validated_thinking"
                warnings = list(parsed.get("warnings") or [])
                anomaly = (
                    "instruct_profile_structured_thinking_anomaly"
                    if "instruct" in self.model.casefold()
                    else "structured_thinking_recovery"
                )
                if anomaly not in warnings:
                    parsed["warnings"] = [*warnings, anomaly]
            extraction = LocalInvoiceExtraction.model_validate(parsed)
        except urllib.error.HTTPError as exc:
            body = exc.read(2000).decode("utf-8", "replace")
            raise LocalMultimodalProviderError(
                _safe_ollama_http_failure(int(exc.code), body)
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise LocalMultimodalProviderError("local_ollama_transport_unavailable") from exc
        except json.JSONDecodeError as exc:
            raise LocalMultimodalProviderError(_local_json_failure_code(content)) from exc
        except ValidationError as exc:
            raise LocalMultimodalProviderError("local_ollama_schema_validation_failed") from exc
        except (TypeError, ValueError) as exc:
            raise LocalMultimodalProviderError("local_ollama_invalid_structured_output") from exc
        after = _resource_snapshot()
        result = LocalMultimodalResult(
            request_id=request_id,
            model=self.model,
            model_version=str(envelope.get("model") or self.model),
            execution_profile=self.profile_id,
            response_channel=response_channel,
            page_identifiers=page_ids,
            extracted_fields=_field_contract(extraction),
            line_items=extraction.line_items,
            structured_output=extraction.model_dump(mode="json"),
            confidence=_confidence(parsed.get("confidence")),
            warnings=list(extraction.warnings),
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
            resources=LocalResourceMetrics(
                system_ram_used_mb_before=before.get("system_ram_used_mb"),
                system_ram_used_mb_after=after.get("system_ram_used_mb"),
                gpu_memory_used_mb_before=before.get("gpu_memory_used_mb"),
                gpu_memory_used_mb_after=after.get("gpu_memory_used_mb"),
                gpu_utilization_percent_after=after.get("gpu_utilization_percent"),
            ),
            completed_at=datetime.now(timezone.utc),
        )
        _LAST_RESULT.set(result)
        return result


def _enforce_candidate_only(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("local_provider_output_must_be_object")
    result = dict(payload)
    for key in (
        "selected_gl", "final_gl", "export_allowed", "readiness",
        "accounting_readiness", "ready_status",
    ):
        result.pop(key, None)
    clean_lines: list[dict[str, Any]] = []
    for item in result.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        line = dict(item)
        for key in ("selected_gl", "final_gl", "export_allowed", "readiness"):
            line.pop(key, None)
        line["gl_account_candidate"] = ""
        clean_lines.append(line)
    result["line_items"] = clean_lines
    return result


def _parse_json_object(content: str) -> dict[str, Any]:
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(content[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("local_provider_output_must_be_object")
    return value


def _safe_ollama_http_failure(status: int, body: str) -> str:
    text = str(body or "").casefold()
    categories = (
        (("context length", "context window", "too large"), "context_limit"),
        (("image", "vision"), "image_input_rejected"),
        (("memory", "system resources", "vram"), "resource_exhausted"),
        (("format", "schema"), "structured_format_rejected"),
        (("model", "not found"), "model_unavailable"),
    )
    for terms, code in categories:
        if any(term in text for term in terms):
            return f"local_ollama_{code}"
    return f"local_ollama_http_{status}"


def _local_json_failure_code(content: str) -> str:
    stripped = str(content or "").strip()
    if not stripped:
        return "local_ollama_empty_content"
    if "{" not in stripped or "}" not in stripped:
        return "local_ollama_non_json_content"
    return "local_ollama_malformed_json"


def _empty_content_failure(envelope: dict[str, Any]) -> str:
    reason = re_safe_token(envelope.get("done_reason") or "unknown")
    message = envelope.get("message") if isinstance(envelope.get("message"), dict) else {}
    thinking_text = str(message.get("thinking") or "").strip()
    if not thinking_text:
        return f"local_ollama_empty_{reason}_no_thinking"
    length_bucket = min(999, max(1, len(thinking_text) // 1000 + 1))
    shape = "thinking_non_json"
    try:
        candidate = _enforce_candidate_only(_parse_json_object(thinking_text))
        LocalInvoiceExtraction.model_validate(candidate)
        shape = "thinking_valid_schema"
    except Exception:
        # Never serialize or log the private reasoning body.  This diagnostic
        # records only its safe structural classification and coarse size.
        pass
    return f"local_ollama_empty_{reason}_{shape}_{length_bucket}k"


def _validated_structured_thinking(envelope: dict[str, Any]) -> dict[str, Any] | None:
    """Recover only a complete typed payload misplaced in Ollama's think field.

    Some thinking-tag models return a schema-conformant JSON object under
    ``message.thinking`` while leaving ``message.content`` empty even when
    ``think=false`` was requested.  We never expose or persist that private
    field.  It is accepted only when the entire value parses and validates as
    the extraction contract; free-form reasoning remains fail-closed.
    """

    message = envelope.get("message") if isinstance(envelope.get("message"), dict) else {}
    thinking_text = str(message.get("thinking") or "").strip()
    if not thinking_text:
        return None
    try:
        candidate = _enforce_candidate_only(_parse_json_object(thinking_text))
        LocalInvoiceExtraction.model_validate(candidate)
    except Exception:
        return None
    return candidate


def re_safe_token(value: Any) -> str:
    return "".join(
        character if character.isalnum() else "_"
        for character in str(value or "").casefold()
    ).strip("_")[:48] or "unknown"


def _ollama_invoice_schema() -> dict[str, Any]:
    """Ollama-compatible subset; the existing backend performs full validation."""

    nullable_string = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    nullable_number = {
        "anyOf": [{"type": "number"}, {"type": "string"}, {"type": "null"}],
    }
    line = {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "source_page": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
            "row_label": nullable_string,
            "location_candidate": nullable_string,
            "activity": nullable_string,
            "description": nullable_string,
            "raw_description": nullable_string,
            "normalized_description": nullable_string,
            "generated_description": nullable_string,
            "quantity": nullable_number,
            "unit_price": nullable_number,
            "amount": nullable_number,
            "confidence": nullable_number,
        },
    }
    properties = {
        name: nullable_string
        for name in (
            "vendor_name", "invoice_number", "invoice_date", "service_date",
            "due_date", "due_date_text", "payment_terms", "property_candidate",
            "location_candidate", "service_address", "invoice_description",
            "visual_extraction_status",
        )
    }
    properties.update({
        "subtotal": nullable_number,
        "tax_amount": nullable_number,
        "total_amount": nullable_number,
        "confidence": nullable_number,
        "line_items": {"type": "array", "items": line},
        "warnings": {"type": "array", "items": {"type": "string"}},
        "needs_manual_review": {"type": "boolean"},
    })
    return {"type": "object", "additionalProperties": True, "properties": properties}


def _ollama_messages(messages: list[Any]) -> tuple[list[dict[str, Any]], list[str]]:
    converted: list[dict[str, Any]] = []
    page_ids: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user")
        content = message.get("content")
        images: list[str] = []
        text_parts: list[str] = []
        if isinstance(content, list):
            for index, item in enumerate(content):
                if not isinstance(item, dict):
                    text_parts.append(str(item))
                elif item.get("type") == "text":
                    text_parts.append(str(item.get("text") or ""))
                elif item.get("type") == "image_url":
                    ref = str((item.get("image_url") or {}).get("url") or "")
                    images.append(_decode_data_image(ref))
                    page_ids.append(f"visual-ref-{index}")
        else:
            text_parts.append(str(content or ""))
        row: dict[str, Any] = {"role": role, "content": "\n".join(text_parts)}
        if images:
            row["images"] = images
        converted.append(row)
    return converted, page_ids


def _decode_data_image(ref: str) -> str:
    if not ref.startswith("data:") or ";base64," not in ref:
        raise ValueError("local_provider_requires_embedded_image_evidence")
    encoded = ref.split(",", 1)[1]
    base64.b64decode(encoded, validate=True)
    return encoded


def _field_contract(extraction: LocalInvoiceExtraction) -> dict[str, LocalExtractedField]:
    fields: dict[str, LocalExtractedField] = {}
    for name in (
        "vendor_name", "invoice_number", "invoice_date", "service_date", "due_date",
        "due_date_text", "payment_terms", "property_candidate", "location_candidate",
        "service_address", "subtotal", "tax_amount", "total_amount",
    ):
        value = getattr(extraction, name)
        fields[name] = LocalExtractedField(
            value=value,
            status="unknown" if value in (None, "") else "observed",
        )
    return fields


def _confidence(value: Any) -> float | None:
    try:
        return min(1.0, max(0.0, float(value))) if value is not None else None
    except (TypeError, ValueError):
        return None


def _resource_snapshot() -> dict[str, float | int | None]:
    values: dict[str, float | int | None] = {
        "system_ram_used_mb": None,
        "gpu_memory_used_mb": None,
        "gpu_utilization_percent": None,
    }
    try:
        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        state = MemoryStatus()
        state.dwLength = ctypes.sizeof(MemoryStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(state)):
            values["system_ram_used_mb"] = round(
                (state.ullTotalPhys - state.ullAvailPhys) / (1024 * 1024), 2,
            )
    except Exception:
        pass
    try:
        completed = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        first = completed.stdout.strip().splitlines()[0].split(",")
        values["gpu_memory_used_mb"] = int(first[0].strip())
        values["gpu_utilization_percent"] = int(first[1].strip())
    except Exception:
        pass
    return values


__all__ = [
    "LOCAL_PROVIDER_CONTRACT_VERSION",
    "LocalInvoiceExtraction",
    "LocalMultimodalProvider",
    "LocalMultimodalProviderError",
    "LocalMultimodalResult",
    "last_result",
]
