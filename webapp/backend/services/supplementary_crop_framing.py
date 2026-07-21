"""Deterministic crop framing for supplementary Gemini requests.

This module is deliberately provider-neutral and offline.  It labels every
authorized image immediately before the image part, binds the response schema
to the exact ordered packet, and rejects framing/schema drift before dispatch.
It never reads source documents and has no accounting or readiness authority.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


SUPPLEMENTARY_CROP_FRAMING_VERSION = "supplementary-crop-framing/2.0"
_LABEL_PATTERN = re.compile(
    r"^CROP_ID: (?P<crop_id>[^\r\n]+)\n"
    r"CROP_ROLE: (?P<crop_role>[^\r\n]+)\n"
    r"CROP_ORDINAL: (?P<ordinal>\d+)\n"
    r"TARGET_RELEVANCE: (?P<relevance>[^\r\n]+)$"
)


class SupplementaryCropFramingError(ValueError):
    """Safe fail-closed framing error."""

    def __init__(self, failure_code: str) -> None:
        super().__init__(failure_code)
        self.failure_code = failure_code


@dataclass(frozen=True)
class AuthorizedCropDescriptor:
    crop_id: str
    crop_role: str
    ordinal: int
    target_relevance: str
    mime_type: str
    page_number: int | None = None
    source_kind: str = "supplementary_planned_crop"

    def safe_metadata(self) -> dict[str, Any]:
        return {
            "crop_id": self.crop_id,
            "crop_role": self.crop_role,
            "ordinal": self.ordinal,
            "target_relevance": self.target_relevance,
            "mime_type": self.mime_type,
            "page_number": self.page_number,
            "source_kind": self.source_kind,
        }


@dataclass(frozen=True)
class SupplementaryCropFraming:
    parts: tuple[Mapping[str, Any], ...]
    descriptors: tuple[AuthorizedCropDescriptor, ...]
    framing_sha256: str
    schema_binding_sha256: str


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def crop_label(descriptor: AuthorizedCropDescriptor) -> str:
    return "\n".join((
        f"CROP_ID: {descriptor.crop_id}",
        f"CROP_ROLE: {descriptor.crop_role}",
        f"CROP_ORDINAL: {descriptor.ordinal}",
        f"TARGET_RELEVANCE: {descriptor.target_relevance}",
    ))


def evidence_linkage_instruction(
    descriptors: Sequence[AuthorizedCropDescriptor],
) -> str:
    ordered = validate_authorized_crop_descriptors(descriptors)
    allowed = ", ".join(item.crop_id for item in ordered)
    return "\n".join((
        "EVIDENCE_LINKAGE_CONTRACT:",
        f"AUTHORIZED_CROP_IDS: {allowed}",
        "Use only AUTHORIZED_CROP_IDS; never invent or alter a crop ID.",
        "Every visible or ambiguous observation must carry its own evidence_refs.",
        "Every contradiction candidate must carry its own evidence_refs.",
        "Use an empty evidence_refs list for a not_visible observation unless the crop itself visibly demonstrates absence.",
        "Do not apply one global evidence reference silently to multiple observations.",
    ))


def validate_authorized_crop_descriptors(
    descriptors: Sequence[AuthorizedCropDescriptor],
) -> tuple[AuthorizedCropDescriptor, ...]:
    result = tuple(descriptors)
    if not result:
        raise SupplementaryCropFramingError("supplementary_evidence_reference_missing")
    if [item.ordinal for item in result] != list(range(len(result))):
        raise SupplementaryCropFramingError("supplementary_crop_label_order_mismatch")
    crop_ids = [item.crop_id for item in result]
    if any(not value or "\n" in value or "\r" in value for value in crop_ids):
        raise SupplementaryCropFramingError("supplementary_evidence_reference_invalid")
    if len(set(crop_ids)) != len(crop_ids):
        raise SupplementaryCropFramingError("supplementary_evidence_reference_invalid")
    if any(
        not item.crop_role or not item.target_relevance or not item.mime_type
        for item in result
    ):
        raise SupplementaryCropFramingError("supplementary_evidence_reference_invalid")
    return result


def _schema_crop_enums(schema: Mapping[str, Any]) -> list[tuple[str, ...]]:
    result: list[tuple[str, ...]] = []

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            properties = value.get("properties")
            if isinstance(properties, Mapping):
                crop = properties.get("crop_id")
                if isinstance(crop, Mapping) and isinstance(crop.get("enum"), list):
                    result.append(tuple(str(item) for item in crop["enum"]))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(schema)
    return result


def packet_specific_schema_binding_sha256(
    *, schema: Mapping[str, Any], packet_sha256: str,
    descriptors: Sequence[AuthorizedCropDescriptor], transport_version: str,
) -> str:
    ordered = validate_authorized_crop_descriptors(descriptors)
    return _sha256(canonical_json_bytes({
        "framing_version": SUPPLEMENTARY_CROP_FRAMING_VERSION,
        "transport_version": transport_version,
        "packet_sha256": packet_sha256,
        "ordered_crops": [item.safe_metadata() for item in ordered],
        "provider_schema": schema,
    }))


def validate_packet_specific_schema(
    *, schema: Mapping[str, Any], packet_sha256: str,
    descriptors: Sequence[AuthorizedCropDescriptor], transport_version: str,
    expected_binding_sha256: str | None = None,
) -> str:
    ordered = validate_authorized_crop_descriptors(descriptors)
    expected_ids = tuple(item.crop_id for item in ordered)
    enums = _schema_crop_enums(schema)
    if not enums or any(values != expected_ids for values in enums):
        raise SupplementaryCropFramingError("supplementary_crop_enum_packet_mismatch")
    fingerprint = packet_specific_schema_binding_sha256(
        schema=schema,
        packet_sha256=packet_sha256,
        descriptors=ordered,
        transport_version=transport_version,
    )
    if expected_binding_sha256 and fingerprint != expected_binding_sha256:
        raise SupplementaryCropFramingError("supplementary_crop_enum_packet_mismatch")
    return fingerprint


def validate_ordered_crop_parts(
    parts: Sequence[Mapping[str, Any]],
    descriptors: Sequence[AuthorizedCropDescriptor],
) -> None:
    ordered = validate_authorized_crop_descriptors(descriptors)
    if len(parts) != len(ordered) * 2:
        raise SupplementaryCropFramingError("supplementary_crop_label_order_mismatch")
    for index, descriptor in enumerate(ordered):
        label_part = parts[index * 2]
        image_part = parts[index * 2 + 1]
        label = label_part.get("text") if isinstance(label_part, Mapping) else None
        match = _LABEL_PATTERN.fullmatch(str(label or ""))
        if not match:
            raise SupplementaryCropFramingError("supplementary_crop_label_order_mismatch")
        if (
            match.group("crop_id") != descriptor.crop_id
            or int(match.group("ordinal")) != descriptor.ordinal
            or match.group("relevance") != descriptor.target_relevance
        ):
            raise SupplementaryCropFramingError("supplementary_crop_label_order_mismatch")
        if match.group("crop_role") != descriptor.crop_role:
            raise SupplementaryCropFramingError("supplementary_crop_role_mismatch")
        inline = image_part.get("inlineData") if isinstance(image_part, Mapping) else None
        if not isinstance(inline, Mapping):
            raise SupplementaryCropFramingError("supplementary_crop_label_order_mismatch")
        if inline.get("mimeType") != descriptor.mime_type or not inline.get("data"):
            raise SupplementaryCropFramingError("supplementary_crop_label_order_mismatch")


def build_supplementary_crop_framing(
    *, descriptors: Sequence[AuthorizedCropDescriptor], images: Sequence[bytes],
    schema: Mapping[str, Any], packet_sha256: str, transport_version: str,
) -> SupplementaryCropFraming:
    ordered = validate_authorized_crop_descriptors(descriptors)
    if len(images) != len(ordered):
        raise SupplementaryCropFramingError("supplementary_crop_label_order_mismatch")
    parts: list[Mapping[str, Any]] = []
    safe_frame: list[Mapping[str, Any]] = []
    for descriptor, image in zip(ordered, images):
        if not isinstance(image, bytes) or not image:
            raise SupplementaryCropFramingError("supplementary_crop_label_order_mismatch")
        label = crop_label(descriptor)
        parts.extend((
            {"text": label},
            {"inlineData": {
                "mimeType": descriptor.mime_type,
                "data": base64.b64encode(image).decode("ascii"),
            }},
        ))
        safe_frame.append({
            **descriptor.safe_metadata(),
            "image_sha256": _sha256(image),
            "image_byte_length": len(image),
        })
    validate_ordered_crop_parts(parts, ordered)
    binding = validate_packet_specific_schema(
        schema=schema,
        packet_sha256=packet_sha256,
        descriptors=ordered,
        transport_version=transport_version,
    )
    return SupplementaryCropFraming(
        parts=tuple(parts),
        descriptors=ordered,
        framing_sha256=_sha256(canonical_json_bytes({
            "framing_version": SUPPLEMENTARY_CROP_FRAMING_VERSION,
            "packet_sha256": packet_sha256,
            "ordered_crop_label_image_pairs": safe_frame,
        })),
        schema_binding_sha256=binding,
    )


__all__ = [
    "AuthorizedCropDescriptor",
    "SUPPLEMENTARY_CROP_FRAMING_VERSION",
    "SupplementaryCropFraming",
    "SupplementaryCropFramingError",
    "build_supplementary_crop_framing",
    "canonical_json_bytes",
    "crop_label",
    "evidence_linkage_instruction",
    "packet_specific_schema_binding_sha256",
    "validate_authorized_crop_descriptors",
    "validate_ordered_crop_parts",
    "validate_packet_specific_schema",
]
