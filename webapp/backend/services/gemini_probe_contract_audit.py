"""Offline request-contract audit for the Gemini 3.5 Flash capability probe.

This module cannot dispatch network requests.  It creates synthetic payloads,
reports structural diagnostics, and converts a provider-compatible envelope
back into the unchanged strict supplementary contract.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from .gemini_supplementary_verification import (
    GeminiSupplementaryObservation,
    SupplementaryFailureStage,
    SupplementarySafeDiagnostics,
    SupplementaryTarget,
    SupplementaryVerificationError,
    parse_decoded_supplementary_payload,
    supplementary_response_format,
)


AUDIT_CONTRACT_VERSION = "gemini-3.5-flash-probe-audit/1.0"
MODEL_ID = "gemini-3.5-flash"
OPENAI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
NATIVE_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-3.5-flash:generateContent"
)
_ALLOWED_OPENAI_FIELDS = frozenset({
    "model", "messages", "response_format", "max_completion_tokens",
    "reasoning_effort",
})
_SUPPORTED_REASONING_EFFORT = frozenset({"minimal", "low"})
_KNOWN_SCHEMA_KEYWORDS = frozenset({
    "$defs", "$id", "$ref", "additionalProperties", "allOf", "anyOf",
    "const", "description", "enum", "format", "items", "maximum",
    "maxItems", "minimum", "minItems", "oneOf", "pattern", "properties",
    "required", "title", "type",
})


class ContractSurface(str, Enum):
    OPENAI_COMPATIBLE = "openai_compatible"
    NATIVE_GEMINI = "native_gemini"


class SchemaAudit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    keyword_inventory: tuple[str, ...]
    unsupported_keywords: tuple[str, ...]
    maximum_depth: int = Field(ge=0)
    property_count: int = Field(ge=0)
    required_field_count: int = Field(ge=0)
    nullable_type_array_count: int = Field(ge=0)
    nullable_object_or_array_conflict_count: int = Field(ge=0)
    one_of_count: int = Field(ge=0)
    any_of_count: int = Field(ge=0)
    all_of_count: int = Field(ge=0)
    recursive_reference_count: int = Field(ge=0)
    pattern_count: int = Field(ge=0)
    additional_properties_count: int = Field(ge=0)
    object_schema_count: int = Field(ge=0)
    object_schema_without_additional_properties_count: int = Field(ge=0)
    complexity_risk: str


class SanitizedRequestShape(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: str = AUDIT_CONTRACT_VERSION
    surface: ContractSurface
    top_level_fields: tuple[str, ...]
    model_id: str
    message_roles: tuple[str, ...]
    content_part_types: tuple[tuple[str, ...], ...]
    image_mime_types: tuple[str, ...]
    image_byte_lengths: tuple[int, ...]
    image_sha256s: tuple[str, ...]
    response_format_type: str | None
    schema_audit: SchemaAudit | None
    generation_parameter_types: Mapping[str, str]
    reasoning_thinking_fields: tuple[str, ...]
    output_token_parameter: str | None
    output_token_limit: int | None
    extra_body_paths: tuple[str, ...]
    payload_fingerprint: str


class ContractDiff(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    unsupported_or_undocumented_fields: tuple[str, ...]
    incorrectly_nested_fields: tuple[str, ...]
    conflicting_fields: tuple[str, ...]
    incompatible_parameter_combinations: tuple[str, ...]
    missing_required_fields: tuple[str, ...]
    image_data_url_valid: bool
    response_format_shape_valid: bool
    model_surface_availability: str = "not_provable_offline"
    most_likely_failure_class: str


class ProbeCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    surface: ContractSurface
    model_id: str = MODEL_ID
    endpoint: str
    payload: Mapping[str, Any] = Field(repr=False)
    payload_fingerprint: str
    response_persistence_allowed: bool = False
    maximum_requests: int = 1
    retries: int = 0
    fallback: bool = False


class RequestContractError(ValueError):
    pass


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def current_strict_internal_schema(target: SupplementaryTarget) -> dict[str, Any]:
    """Return the unchanged strict schema used by local validation."""
    return supplementary_response_format(target)


def provider_compatible_transport_schema() -> dict[str, Any]:
    """Return the historical V1 envelope for telemetry/replay readers only.

    New request construction must use
    ``supplementary_transport_v2_response_format``.  Keeping this function is
    a compatibility adapter for immutable V1 records, not an active request
    contract.
    """
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "innerview_supplementary_transport_envelope",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {"payload_json": {"type": "string"}},
                "required": ["payload_json"],
                "additionalProperties": False,
            },
        },
    }


@dataclass(frozen=True)
class DecodedSupplementaryEnvelope:
    """Exactly-once decoded payload; private values never enter diagnostics."""

    payload: Mapping[str, Any] = field(repr=False)
    diagnostics: SupplementarySafeDiagnostics


@dataclass(frozen=True)
class ParsedSupplementaryTransport:
    observation: GeminiSupplementaryObservation = field(repr=False)
    diagnostics: SupplementarySafeDiagnostics


def _payload_parser_category(payload_json: str, exc: json.JSONDecodeError) -> str:
    stripped = payload_json.strip()
    if not stripped:
        return "empty"
    decoder = json.JSONDecoder()
    try:
        _, consumed = decoder.raw_decode(stripped)
    except json.JSONDecodeError:
        return "malformed_json"
    if stripped[consumed:].strip():
        return "multiple_or_trailing_content"
    if exc.pos > 0:
        return "surrounding_or_malformed_content"
    return "malformed_json"


def _envelope_diagnostics(
    *, failure_code: str | None, payload_json: str | None = None,
    parse_result: str, decoding_count: int = 0,
    stage: SupplementaryFailureStage = SupplementaryFailureStage.ENVELOPE,
) -> SupplementarySafeDiagnostics:
    payload_bytes = payload_json.encode("utf-8") if payload_json is not None else None
    return SupplementarySafeDiagnostics(
        stage=stage,
        failure_code=failure_code,
        payload_present=payload_json is not None,
        payload_byte_length=len(payload_bytes) if payload_bytes is not None else None,
        payload_sha256=sha256_bytes(payload_bytes) if payload_bytes is not None else None,
        payload_parse_result=parse_result,
        decoding_count=decoding_count,
    )


def decode_provider_transport_response(raw_response: str) -> DecodedSupplementaryEnvelope:
    """Decode the expected string once and reject all ambiguous alternatives."""

    try:
        envelope = json.loads(raw_response)
    except (TypeError, json.JSONDecodeError) as exc:
        diagnostics = _envelope_diagnostics(
            failure_code="supplementary_envelope_invalid",
            parse_result="outer_envelope_malformed",
        )
        raise SupplementaryVerificationError(
            "supplementary_envelope_invalid", diagnostics=diagnostics,
        ) from exc
    if not isinstance(envelope, dict):
        diagnostics = _envelope_diagnostics(
            failure_code="supplementary_envelope_invalid",
            parse_result="outer_envelope_not_object",
        )
        raise SupplementaryVerificationError(
            "supplementary_envelope_invalid", diagnostics=diagnostics,
        )
    if "payload_json" not in envelope or envelope.get("payload_json") is None:
        diagnostics = _envelope_diagnostics(
            failure_code="supplementary_payload_json_missing",
            parse_result="payload_json_missing",
        )
        raise SupplementaryVerificationError(
            "supplementary_payload_json_missing", diagnostics=diagnostics,
        )
    if set(envelope) != {"payload_json"}:
        diagnostics = _envelope_diagnostics(
            failure_code="supplementary_envelope_invalid",
            parse_result="outer_envelope_extra_fields",
        ).model_copy(update={
            "unexpected_field_name_hashes": tuple(sorted(
                sha256_bytes(str(key).encode("utf-8"))
                for key in envelope if key != "payload_json"
            )),
        })
        raise SupplementaryVerificationError(
            "supplementary_envelope_invalid", diagnostics=diagnostics,
        )
    payload_json = envelope["payload_json"]
    if not isinstance(payload_json, str):
        diagnostics = _envelope_diagnostics(
            failure_code="supplementary_field_type_invalid",
            parse_result=f"payload_json_unexpected_{type(payload_json).__name__}",
        )
        raise SupplementaryVerificationError(
            "supplementary_field_type_invalid", diagnostics=diagnostics,
        )
    if not payload_json.strip():
        diagnostics = _envelope_diagnostics(
            failure_code="supplementary_payload_json_missing",
            payload_json=payload_json,
            parse_result="payload_json_empty",
        )
        raise SupplementaryVerificationError(
            "supplementary_payload_json_missing", diagnostics=diagnostics,
        )
    try:
        decoded = json.loads(payload_json)
    except json.JSONDecodeError as exc:
        diagnostics = _envelope_diagnostics(
            failure_code="supplementary_payload_json_malformed",
            payload_json=payload_json,
            parse_result=_payload_parser_category(payload_json, exc),
            decoding_count=1,
            stage=SupplementaryFailureStage.PAYLOAD_DECODING,
        )
        raise SupplementaryVerificationError(
            "supplementary_payload_json_malformed", diagnostics=diagnostics,
        ) from exc
    if isinstance(decoded, str):
        # Inspect only the type of a possible second decode. Never use that
        # second value as the observation: repeated decoding is ambiguous.
        try:
            second = json.loads(decoded)
        except (TypeError, json.JSONDecodeError):
            second = None
        if isinstance(second, dict):
            diagnostics = _envelope_diagnostics(
                failure_code="supplementary_payload_double_encoded",
                payload_json=payload_json,
                parse_result="double_encoded_object_detected",
                decoding_count=1,
                stage=SupplementaryFailureStage.PAYLOAD_DECODING,
            )
            raise SupplementaryVerificationError(
                "supplementary_payload_double_encoded", diagnostics=diagnostics,
            )
    if not isinstance(decoded, dict):
        diagnostics = _envelope_diagnostics(
            failure_code="supplementary_field_type_invalid",
            payload_json=payload_json,
            parse_result=f"decoded_{type(decoded).__name__}_not_object",
            decoding_count=1,
            stage=SupplementaryFailureStage.PAYLOAD_DECODING,
        )
        raise SupplementaryVerificationError(
            "supplementary_field_type_invalid", diagnostics=diagnostics,
        )
    diagnostics = _envelope_diagnostics(
        failure_code=None,
        payload_json=payload_json,
        parse_result="object_decoded_once",
        decoding_count=1,
        stage=SupplementaryFailureStage.PAYLOAD_DECODING,
    )
    return DecodedSupplementaryEnvelope(payload=decoded, diagnostics=diagnostics)


def parse_provider_transport_response_with_audit(
    raw_response: str, *, target: SupplementaryTarget,
) -> ParsedSupplementaryTransport:
    decoded = decode_provider_transport_response(raw_response)
    try:
        observation, internal = parse_decoded_supplementary_payload(
            decoded.payload, target=target,
        )
    except SupplementaryVerificationError as exc:
        if exc.diagnostics is not None:
            combined = exc.diagnostics.model_copy(update={
                "payload_present": decoded.diagnostics.payload_present,
                "payload_byte_length": decoded.diagnostics.payload_byte_length,
                "payload_sha256": decoded.diagnostics.payload_sha256,
                "payload_parse_result": decoded.diagnostics.payload_parse_result,
                "decoding_count": decoded.diagnostics.decoding_count,
            })
            raise SupplementaryVerificationError(
                exc.failure_code, diagnostics=combined,
            ) from exc
        raise
    combined = internal.model_copy(update={
        "payload_present": decoded.diagnostics.payload_present,
        "payload_byte_length": decoded.diagnostics.payload_byte_length,
        "payload_sha256": decoded.diagnostics.payload_sha256,
        "payload_parse_result": decoded.diagnostics.payload_parse_result,
        "decoding_count": decoded.diagnostics.decoding_count,
    })
    return ParsedSupplementaryTransport(
        observation=observation,
        diagnostics=combined,
    )


def parse_provider_transport_response(
    raw_response: str, *, target: SupplementaryTarget,
) -> GeminiSupplementaryObservation:
    """Compatibility adapter returning only the strict observation."""
    return parse_provider_transport_response_with_audit(
        raw_response, target=target,
    ).observation


def minimal_probe_response_format(surface: ContractSurface) -> dict[str, Any]:
    schema = {
        "type": "object",
        "properties": {
            "visible": {"type": "boolean"},
            "synthetic_label": {"type": "string"},
        },
        "required": ["visible", "synthetic_label"],
        "additionalProperties": False,
    }
    if surface is ContractSurface.OPENAI_COMPATIBLE:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "innerview_synthetic_capability_probe",
                "strict": True,
                "schema": schema,
            },
        }
    return schema


def gemini_openai_generation_controls(
    *, reasoning_effort: str | None = "low", max_output_tokens: int = 256,
) -> dict[str, Any]:
    if reasoning_effort is not None:
        normalized = str(reasoning_effort).strip().casefold()
        if normalized not in _SUPPORTED_REASONING_EFFORT:
            raise RequestContractError("unsupported_gemini_reasoning_effort")
    if not 1 <= int(max_output_tokens) <= 2048:
        raise RequestContractError("gemini_probe_output_limit_invalid")
    controls: dict[str, Any] = {"max_completion_tokens": int(max_output_tokens)}
    if reasoning_effort is not None:
        controls["reasoning_effort"] = normalized
    return controls


def validate_image_data_url(value: str) -> tuple[str, bytes]:
    if not isinstance(value, str) or not value.startswith("data:image/"):
        raise RequestContractError("image_data_url_required")
    try:
        header, encoded = value.split(",", 1)
        if not header.endswith(";base64"):
            raise ValueError
        mime_type = header[5:-7]
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise RequestContractError("image_data_url_invalid") from exc
    if mime_type not in {"image/png", "image/jpeg", "image/webp"} or not content:
        raise RequestContractError("image_data_url_invalid")
    return mime_type, content


def build_corrected_openai_probe(image_bytes: bytes) -> ProbeCandidate:
    data_url = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")
    validate_image_data_url(data_url)
    payload = {
        "model": MODEL_ID,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "Identify the non-private synthetic marker."},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }],
        "response_format": minimal_probe_response_format(ContractSurface.OPENAI_COMPATIBLE),
        # Omit an optional reasoning control in the first isolation probe.  The
        # local reference set proves neither `none` nor a Gemini-specific
        # thinking field on this OpenAI-compatible surface.
        **gemini_openai_generation_controls(reasoning_effort=None, max_output_tokens=256),
    }
    validate_openai_probe_payload(payload)
    return ProbeCandidate(
        surface=ContractSurface.OPENAI_COMPATIBLE, endpoint=OPENAI_ENDPOINT,
        payload=payload, payload_fingerprint=sha256_bytes(canonical_json_bytes(payload)),
    )


def build_native_gemini_probe(image_bytes: bytes) -> ProbeCandidate:
    if not image_bytes:
        raise RequestContractError("synthetic_image_required")
    payload = {
        "contents": [{
            "role": "user",
            "parts": [
                {"text": "Identify the non-private synthetic marker."},
                {"inlineData": {
                    "mimeType": "image/png",
                    "data": base64.b64encode(image_bytes).decode("ascii"),
                }},
            ],
        }],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseJsonSchema": minimal_probe_response_format(
                ContractSurface.NATIVE_GEMINI,
            ),
            "maxOutputTokens": 256,
        },
    }
    return ProbeCandidate(
        surface=ContractSurface.NATIVE_GEMINI, endpoint=NATIVE_ENDPOINT,
        payload=payload, payload_fingerprint=sha256_bytes(canonical_json_bytes(payload)),
    )


def validate_openai_probe_payload(payload: Mapping[str, Any]) -> None:
    fields = set(payload)
    unsupported = sorted(fields - _ALLOWED_OPENAI_FIELDS)
    if unsupported:
        raise RequestContractError("unsupported_openai_probe_parameter")
    if payload.get("model") != MODEL_ID:
        raise RequestContractError("probe_model_mismatch")
    effort = payload.get("reasoning_effort")
    if effort is not None and str(effort).casefold() not in _SUPPORTED_REASONING_EFFORT:
        raise RequestContractError("unsupported_gemini_reasoning_effort")
    if "max_completion_tokens" not in payload:
        raise RequestContractError("max_completion_tokens_required")
    response = payload.get("response_format")
    if response != minimal_probe_response_format(ContractSurface.OPENAI_COMPATIBLE):
        raise RequestContractError("openai_response_format_invalid")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise RequestContractError("probe_messages_required")
    image_count = 0
    for message in messages:
        if not isinstance(message, Mapping) or message.get("role") not in {"system", "user"}:
            raise RequestContractError("probe_message_invalid")
        content = message.get("content")
        parts = content if isinstance(content, list) else []
        for part in parts:
            if isinstance(part, Mapping) and part.get("type") == "image_url":
                url = (part.get("image_url") or {}).get("url")
                validate_image_data_url(url)
                image_count += 1
    if image_count != 1:
        raise RequestContractError("probe_requires_one_image")


def audit_schema(schema: Mapping[str, Any]) -> SchemaAudit:
    keywords: set[str] = set()
    unsupported: set[str] = set()
    maximum_depth = property_count = required_count = 0
    nullable_arrays = nullable_conflicts = 0
    one_of = any_of = all_of = recursive = patterns = additional = 0
    object_schemas = object_schemas_without_additional = 0

    def walk(value: Any, depth: int = 0, *, schema_position: bool = True) -> None:
        nonlocal maximum_depth, property_count, required_count, nullable_arrays
        nonlocal nullable_conflicts, one_of, any_of, all_of, recursive, patterns, additional
        nonlocal object_schemas, object_schemas_without_additional
        maximum_depth = max(maximum_depth, depth)
        if isinstance(value, Mapping):
            schema_type = value.get("type")
            type_values = schema_type if isinstance(schema_type, list) else [schema_type]
            if "object" in type_values:
                object_schemas += 1
                if "additionalProperties" not in value:
                    object_schemas_without_additional += 1
            for key, child in value.items():
                if schema_position and key in _KNOWN_SCHEMA_KEYWORDS:
                    keywords.add(key)
                elif schema_position and key.startswith("$"):
                    unsupported.add(key)
                if key == "properties" and isinstance(child, Mapping):
                    property_count += len(child)
                    for property_schema in child.values():
                        walk(property_schema, depth + 1, schema_position=True)
                    continue
                if key == "required" and isinstance(child, Sequence):
                    required_count += len(child)
                if key == "type" and isinstance(child, list) and "null" in child:
                    nullable_arrays += 1
                    if any(name in value for name in ("properties", "items")):
                        nullable_conflicts += 1
                one_of += int(key == "oneOf"); any_of += int(key == "anyOf")
                all_of += int(key == "allOf"); recursive += int(key == "$ref")
                patterns += int(key == "pattern"); additional += int(key == "additionalProperties")
                walk(child, depth + 1, schema_position=(key not in {"required", "enum"}))
        elif isinstance(value, list):
            for child in value:
                walk(child, depth + 1, schema_position=schema_position)

    walk(schema)
    risk = "high" if maximum_depth >= 8 or property_count >= 50 or nullable_conflicts else (
        "medium" if maximum_depth >= 6 or property_count >= 25 else "low"
    )
    return SchemaAudit(
        keyword_inventory=tuple(sorted(keywords)), unsupported_keywords=tuple(sorted(unsupported)),
        maximum_depth=maximum_depth, property_count=property_count,
        required_field_count=required_count, nullable_type_array_count=nullable_arrays,
        nullable_object_or_array_conflict_count=nullable_conflicts,
        one_of_count=one_of, any_of_count=any_of, all_of_count=all_of,
        recursive_reference_count=recursive, pattern_count=patterns,
        additional_properties_count=additional, object_schema_count=object_schemas,
        object_schema_without_additional_properties_count=(
            object_schemas_without_additional
        ), complexity_risk=risk,
    )


def sanitize_request_shape(
    payload: Mapping[str, Any], *, surface: ContractSurface,
) -> SanitizedRequestShape:
    roles: list[str] = []
    sequences: list[tuple[str, ...]] = []
    mime_types: list[str] = []
    lengths: list[int] = []
    hashes: list[str] = []
    for message in payload.get("messages") or []:
        if not isinstance(message, Mapping):
            continue
        roles.append(str(message.get("role") or ""))
        content = message.get("content")
        parts = content if isinstance(content, list) else []
        sequences.append(tuple(
            str(item.get("type") or "") for item in parts if isinstance(item, Mapping)
        ) if parts else ("string",))
        for part in parts:
            if not isinstance(part, Mapping) or part.get("type") != "image_url":
                continue
            url = (part.get("image_url") or {}).get("url")
            mime, content_bytes = validate_image_data_url(url)
            mime_types.append(mime); lengths.append(len(content_bytes)); hashes.append(sha256_bytes(content_bytes))
    response = payload.get("response_format") if isinstance(payload.get("response_format"), Mapping) else {}
    schema_value = ((response.get("json_schema") or {}).get("schema")
                    if isinstance(response.get("json_schema"), Mapping) else None)
    generation = {
        key: type(value).__name__ for key, value in payload.items()
        if key not in {"model", "messages", "response_format"}
    }
    reasoning = tuple(sorted(
        key for key in payload if "reason" in key.casefold() or "think" in key.casefold()
    ))
    token_name = next((key for key in ("max_completion_tokens", "max_tokens", "max_output_tokens") if key in payload), None)
    token_limit = int(payload[token_name]) if token_name and isinstance(payload[token_name], int) else None
    extra_paths = tuple(sorted(_paths(payload.get("extra_body"), "extra_body")))
    return SanitizedRequestShape(
        surface=surface, top_level_fields=tuple(sorted(payload)),
        model_id=str(payload.get("model") or ""), message_roles=tuple(roles),
        content_part_types=tuple(sequences), image_mime_types=tuple(mime_types),
        image_byte_lengths=tuple(lengths), image_sha256s=tuple(hashes),
        response_format_type=str(response.get("type")) if response else None,
        schema_audit=audit_schema(schema_value) if isinstance(schema_value, Mapping) else None,
        generation_parameter_types=generation, reasoning_thinking_fields=reasoning,
        output_token_parameter=token_name, output_token_limit=token_limit,
        extra_body_paths=extra_paths,
        payload_fingerprint=sha256_bytes(canonical_json_bytes(payload)),
    )


def diff_failed_openai_request(payload: Mapping[str, Any]) -> ContractDiff:
    unsupported = tuple(sorted(set(payload) - _ALLOWED_OPENAI_FIELDS))
    conflicts: list[str] = []
    combinations: list[str] = []
    if "temperature" in payload:
        conflicts.append("temperature_forced_on_reasoning_model")
    if "max_tokens" in payload:
        combinations.append("legacy_max_tokens_instead_of_max_completion_tokens")
    effort = payload.get("reasoning_effort")
    if effort is not None and str(effort).casefold() not in _SUPPORTED_REASONING_EFFORT:
        conflicts.append("unsupported_reasoning_effort")
    missing = tuple(
        name for name in ("model", "messages", "response_format") if name not in payload
    )
    image_valid = True
    try:
        images = []
        for message in payload.get("messages") or []:
            for part in message.get("content") if isinstance(message.get("content"), list) else []:
                if isinstance(part, Mapping) and part.get("type") == "image_url":
                    images.append((part.get("image_url") or {}).get("url"))
        image_valid = bool(images) and all(validate_image_data_url(item) for item in images)
    except RequestContractError:
        image_valid = False
    response = payload.get("response_format")
    response_valid = bool(
        isinstance(response, Mapping) and response.get("type") == "json_schema"
        and isinstance(response.get("json_schema"), Mapping)
        and isinstance(response["json_schema"].get("schema"), Mapping)
    )
    schema = response["json_schema"]["schema"] if response_valid else {}
    audit = audit_schema(schema)
    likely = (
        "openai_compat_generation_controls_or_schema_subset"
        if unsupported or audit.complexity_risk == "high"
        else "model_surface_availability_or_unobserved_provider_constraint"
    )
    return ContractDiff(
        unsupported_or_undocumented_fields=unsupported,
        incorrectly_nested_fields=(), conflicting_fields=tuple(conflicts),
        incompatible_parameter_combinations=tuple(combinations),
        missing_required_fields=missing, image_data_url_valid=image_valid,
        response_format_shape_valid=response_valid,
        most_likely_failure_class=likely,
    )


def official_reference_shapes() -> dict[str, tuple[str, ...]]:
    """Locally encoded structural references; no request content or credentials."""
    return {
        "minimal_text": ("model", "messages", "max_completion_tokens"),
        "minimal_image": ("model", "messages", "max_completion_tokens"),
        "minimal_structured_output": (
            "model", "messages", "response_format", "max_completion_tokens",
        ),
    }


def _paths(value: Any, prefix: str) -> list[str]:
    result: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            path = f"{prefix}.{key}"
            result.append(path); result.extend(_paths(child, path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            result.extend(_paths(child, f"{prefix}[{index}]"))
    return result


__all__ = [
    "AUDIT_CONTRACT_VERSION", "ContractDiff", "ContractSurface", "MODEL_ID",
    "NATIVE_ENDPOINT", "OPENAI_ENDPOINT", "ProbeCandidate", "RequestContractError",
    "SanitizedRequestShape", "SchemaAudit", "audit_schema", "build_corrected_openai_probe",
    "build_native_gemini_probe", "canonical_json_bytes", "current_strict_internal_schema",
    "diff_failed_openai_request", "gemini_openai_generation_controls",
    "minimal_probe_response_format", "official_reference_shapes",
    "DecodedSupplementaryEnvelope", "ParsedSupplementaryTransport",
    "decode_provider_transport_response", "parse_provider_transport_response",
    "parse_provider_transport_response_with_audit",
    "provider_compatible_transport_schema",
    "sanitize_request_shape", "sha256_bytes", "validate_image_data_url",
    "validate_openai_probe_payload",
]
