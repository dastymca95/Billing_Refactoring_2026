"""Deterministic, facts-only visual evidence planning for supplementary checks.

The planner is deliberately offline.  It localizes the smallest useful regions
from existing page layout and evidence, validates the rendered crops, and has
no provider, accounting, readiness, benchmark, or learning authority.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field


EVIDENCE_PLAN_VERSION = "supplementary-evidence-plan/1.0"
EVIDENCE_LOCALIZER_VERSION = "supplementary-layout-localizer/1.0"
DEFAULT_MAX_IMAGE_COUNT = 6
DEFAULT_MAX_EDGE_PIXELS = 1800
DEFAULT_MAX_COMBINED_PIXELS = 6_000_000
MIN_PRIMARY_WIDTH = 320
MIN_PRIMARY_HEIGHT = 120


class EvidencePlanModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SupplementaryTargetSubtype(str, Enum):
    MISSING_TAX_OR_FEE = "missing_tax_or_fee"
    MISSING_DISCOUNT_OR_CREDIT = "missing_discount_or_credit"
    PREVIOUS_BALANCE = "previous_balance"
    PAYMENT_OR_DEPOSIT = "payment_or_deposit"
    OMITTED_LINE_ITEM = "omitted_line_item"
    PAGE_CONTINUATION = "page_continuation"
    AMBIGUOUS_TOTAL_LABEL = "ambiguous_total_label"
    STATEMENT_VS_INVOICE = "statement_vs_invoice"
    UNKNOWN_TOTAL_COMPOSITION = "unknown_total_composition"
    INVOICE_IDENTITY = "invoice_identity"
    PAID_OR_CROSSED_ROW = "paid_or_crossed_row"
    DATE_IDENTITY = "date_identity"
    VENDOR_IDENTITY = "vendor_identity"
    DUPLICATE_ROW = "duplicate_row"
    QUANTITY_PRICE = "quantity_price"


class CropCategory(str, Enum):
    DOCUMENT_HEADER = "document_header"
    INVOICE_IDENTITY = "invoice_identity"
    VENDOR_HEADER_CONTEXT = "vendor_header_context"
    LINE_ITEM_TABLE = "line_item_table"
    TOTALS_FOOTER = "totals_footer"
    TAX_FEE = "tax_fee"
    CREDITS_PAYMENTS = "credits_payments"
    PRIOR_BALANCE = "prior_balance"
    PAGE_BOTTOM = "page_bottom"
    PAGE_TOP = "page_top"
    PAID_ROW = "paid_row"
    SERVICE_ADDRESS = "service_address"
    PAGE_CONTEXT = "page_context"


class CropRole(str, Enum):
    PRIMARY = "primary_target"
    CONTEXT = "context_thumbnail"
    RELATED = "related_evidence"
    CONTINUATION = "continuation"


class NormalizedCropCoordinates(EvidencePlanModel):
    page_number: int = Field(ge=1)
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    width: float = Field(gt=0.0, le=1.0)
    height: float = Field(gt=0.0, le=1.0)

    @property
    def right(self) -> float:
        return min(1.0, self.x + self.width)

    @property
    def bottom(self) -> float:
        return min(1.0, self.y + self.height)


class EvidenceAnchor(EvidencePlanModel):
    anchor_id: str
    page_number: int = Field(ge=1)
    anchor_category: str
    coordinates: NormalizedCropCoordinates
    source_kind: str
    label_detected: bool = False


class PlannedEvidenceCrop(EvidencePlanModel):
    crop_id: str
    role: CropRole
    category: CropCategory
    coordinates: NormalizedCropCoordinates
    anchor_ids: tuple[str, ...] = ()
    target_label_required: bool = False
    context_required: bool = False


class PrivacyMinimizationResult(EvidencePlanModel):
    passed: bool
    contains_expected_answer: bool = False
    contains_ground_truth: bool = False
    contains_accounting_authority: bool = False
    selected_regions_only: bool = True
    reason_code: str = "target_specific_regions_only"


class SupplementaryEvidencePlan(EvidencePlanModel):
    plan_version: str = EVIDENCE_PLAN_VERSION
    localizer_version: str = EVIDENCE_LOCALIZER_VERSION
    opaque_document_id: str
    target_id: str
    target_category: str
    target_subtype: SupplementaryTargetSubtype
    source_page_numbers: tuple[int, ...]
    crops: tuple[PlannedEvidenceCrop, ...]
    evidence_anchors: tuple[EvidenceAnchor, ...]
    context_thumbnail_required: bool
    related_region_requirements: tuple[CropCategory, ...] = ()
    expected_observable_fields: tuple[str, ...]
    maximum_image_count: int = Field(default=DEFAULT_MAX_IMAGE_COUNT, ge=1, le=8)
    maximum_edge_pixels: int = Field(default=DEFAULT_MAX_EDGE_PIXELS, ge=640, le=3000)
    maximum_combined_pixels: int = Field(
        default=DEFAULT_MAX_COMBINED_PIXELS, ge=500_000, le=12_000_000
    )
    privacy_minimization: PrivacyMinimizationResult
    plan_generation_reason_code: str

    @property
    def plan_id(self) -> str:
        payload = self.model_dump(mode="json", exclude={"opaque_document_id"})
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:24]

    @property
    def crop_fingerprint(self) -> str:
        value = [
            (crop.role.value, crop.category.value, crop.coordinates.model_dump(mode="json"))
            for crop in self.crops
        ]
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def provider_summary(self) -> dict[str, Any]:
        """Return the facts-only request metadata; no answer can enter it."""
        return {
            "plan_version": self.plan_version,
            "plan_id": self.plan_id,
            "target_category": self.target_category,
            "target_subtype": self.target_subtype.value,
            "expected_observable_fields": list(self.expected_observable_fields),
            "images": [
                {
                    "crop_id": item.crop_id,
                    "role": item.role.value,
                    "category": item.category.value,
                    "page_number": item.coordinates.page_number,
                }
                for item in self.crops
            ],
        }


class EvidencePacketImage(EvidencePlanModel):
    crop_id: str
    role: CropRole
    category: CropCategory
    page_number: int
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    pixel_count: int = Field(gt=0)
    image_sha256: str
    data_url: str = Field(repr=False)


class SupplementaryEvidencePacket(EvidencePlanModel):
    plan_id: str
    images: tuple[EvidencePacketImage, ...]
    combined_pixels: int
    validation_codes: tuple[str, ...] = ()


class EvidenceLocalizationError(ValueError):
    def __init__(self, failure_code: str) -> None:
        super().__init__(failure_code)
        self.failure_code = failure_code


@dataclass(frozen=True)
class _LocalBlock:
    page_number: int
    text: str
    bbox: NormalizedCropCoordinates
    source_kind: str


_REGION_PATTERNS: Mapping[CropCategory, tuple[str, ...]] = {
    CropCategory.INVOICE_IDENTITY: (
        r"\binvoice\s*(?:no|number|#)", r"\binv\s*(?:no|#)", r"\bstatement\s*(?:no|#)",
        r"\baccount\s*(?:no|number|#)", r"\bwork\s*order", r"\border\s*(?:no|#)",
    ),
    CropCategory.VENDOR_HEADER_CONTEXT: (r"\binvoice\b", r"\bbill\s+to\b", r"\bremit\s+to\b"),
    CropCategory.LINE_ITEM_TABLE: (
        r"\bdescription\b", r"\bitem\b", r"\bqty\b", r"\bquantity\b", r"\bunit\s*price\b", r"\bamount\b",
    ),
    CropCategory.TOTALS_FOOTER: (
        r"\bsubtotal\b", r"\binvoice\s+total\b", r"\bamount\s+due\b", r"\bbalance\s+due\b", r"\btotal\b",
    ),
    CropCategory.TAX_FEE: (r"\btax\b", r"\bfee\b", r"\bshipping\b", r"\bfreight\b", r"\bsurcharge\b"),
    CropCategory.CREDITS_PAYMENTS: (
        r"\bcredit\b", r"\bdiscount\b", r"\bpayment\b", r"\bdeposit\b", r"\bpaid\b",
    ),
    CropCategory.PRIOR_BALANCE: (
        r"\bprevious\s+balance\b", r"\bprior\s+balance\b", r"\bbalance\s+forward\b", r"\bcarried\s+forward\b",
    ),
    CropCategory.PAID_ROW: (r"\bpaid\b", r"\bpayment\b", r"\bvoid\b", r"\bcancelled\b"),
    CropCategory.SERVICE_ADDRESS: (
        r"\bservice\s+address\b", r"\bjob\s+site\b", r"\bship\s+to\b", r"\bproperty\b",
    ),
}


_TOTAL_COMPONENT_FIELDS = (
    "subtotal", "tax", "fees", "credits", "discounts", "previous_balance",
    "payments", "deposits", "current_charges", "amount_due", "line_item_sum",
    "total_label", "page_continuation_status",
)


def build_supplementary_evidence_plan(
    *, opaque_document_id: str, target: Any, initial_facts: Mapping[str, Any],
    document_layout: Mapping[str, Any] | None,
) -> SupplementaryEvidencePlan:
    """Create one deterministic evidence plan from existing local observations."""

    target_category = _target_value(target)
    target_id = str(getattr(target, "target_id", "") or _stable_target_id(target))
    target_page = _positive_int(getattr(target, "page_number", None)) or 1
    blocks = _layout_blocks(document_layout, initial_facts)
    page_count = max(
        _positive_int((document_layout or {}).get("page_count")) or 0,
        max((block.page_number for block in blocks), default=0),
        target_page,
    )
    pages = tuple(range(1, page_count + 1))
    subtype = classify_target_subtype(
        target_category=target_category, target_page=target_page,
        initial_facts=initial_facts, blocks=blocks, page_count=page_count,
    )
    anchors: list[EvidenceAnchor] = []
    crops: list[PlannedEvidenceCrop] = []

    def add_region(
        category: CropCategory, role: CropRole, *, page: int = target_page,
        context_required: bool = False, label_required: bool = False,
    ) -> None:
        region_blocks = _matching_blocks(blocks, category, page)
        coordinate = _region_coordinates(category, page, region_blocks)
        region_anchor_ids: list[str] = []
        if region_blocks:
            anchor_bbox = _union_coordinates([item.bbox for item in region_blocks], padding=0.01)
            anchor_id = _identifier("anchor", page, category.value, anchor_bbox)
            anchors.append(EvidenceAnchor(
                anchor_id=anchor_id, page_number=page, anchor_category=category.value,
                coordinates=anchor_bbox, source_kind=region_blocks[0].source_kind,
                label_detected=True,
            ))
            region_anchor_ids.append(anchor_id)
        else:
            anchor_id = _identifier("anchor", page, category.value, coordinate)
            page_text_available = any(item.page_number == page for item in blocks)
            anchors.append(EvidenceAnchor(
                anchor_id=anchor_id, page_number=page, anchor_category=category.value,
                coordinates=coordinate,
                source_kind=(
                    "deterministic_layout_geometry_with_detectable_text"
                    if page_text_available else "deterministic_layout_geometry_without_local_text"
                ),
                label_detected=False,
            ))
            region_anchor_ids.append(anchor_id)
        crops.append(PlannedEvidenceCrop(
            crop_id=_identifier("crop", page, role.value, category.value, coordinate),
            role=role, category=category, coordinates=coordinate,
            anchor_ids=tuple(region_anchor_ids), target_label_required=label_required,
            context_required=context_required,
        ))

    expected_fields: tuple[str, ...]
    related: list[CropCategory] = []
    context_required = True
    if target_category in {"total_mismatch", "subtotal_mismatch", "missing_line_item", "missing_tax_or_fee"}:
        add_region(CropCategory.TOTALS_FOOTER, CropRole.PRIMARY, context_required=True)
        add_region(CropCategory.LINE_ITEM_TABLE, CropRole.RELATED)
        related_categories = _related_total_categories(subtype)
        for category in related_categories:
            add_region(category, CropRole.RELATED)
            related.append(category)
        if page_count > 1:
            add_region(CropCategory.PAGE_BOTTOM, CropRole.CONTINUATION, page=target_page)
            next_page = min(page_count, target_page + 1)
            if next_page != target_page:
                add_region(CropCategory.PAGE_TOP, CropRole.CONTINUATION, page=next_page)
            related.extend([CropCategory.PAGE_BOTTOM, CropCategory.PAGE_TOP])
        expected_fields = _TOTAL_COMPONENT_FIELDS
    elif target_category == "invoice_number_ambiguity":
        add_region(CropCategory.INVOICE_IDENTITY, CropRole.PRIMARY, label_required=True, context_required=True)
        add_region(CropCategory.VENDOR_HEADER_CONTEXT, CropRole.RELATED)
        expected_fields = (
            "raw_candidate", "adjacent_visible_label", "candidate_type",
            "evidence_reference", "confidence", "unresolved",
        )
        related.append(CropCategory.VENDOR_HEADER_CONTEXT)
    elif target_category == "paid_crossed_out_row_status":
        add_region(CropCategory.PAID_ROW, CropRole.PRIMARY, label_required=True, context_required=True)
        add_region(CropCategory.LINE_ITEM_TABLE, CropRole.RELATED)
        expected_fields = ("row_status", "marker_type", "raw_marker", "evidence_reference")
        related.append(CropCategory.LINE_ITEM_TABLE)
    elif target_category == "page_continuation":
        add_region(CropCategory.PAGE_BOTTOM, CropRole.PRIMARY, page=target_page, context_required=True)
        next_page = min(page_count, target_page + 1)
        add_region(CropCategory.PAGE_TOP, CropRole.CONTINUATION, page=next_page)
        add_region(CropCategory.TOTALS_FOOTER, CropRole.RELATED, page=target_page)
        expected_fields = (
            "continuation_status", "repeated_header", "carried_forward_marker", "visible_total_label",
        )
        related.extend([CropCategory.PAGE_TOP, CropCategory.TOTALS_FOOTER])
    elif target_category == "date_ambiguity":
        add_region(CropCategory.DOCUMENT_HEADER, CropRole.PRIMARY, context_required=True)
        expected_fields = ("raw_candidate", "adjacent_visible_label", "date_type", "unresolved")
    elif target_category == "vendor_name_ambiguity":
        add_region(CropCategory.VENDOR_HEADER_CONTEXT, CropRole.PRIMARY, context_required=True)
        expected_fields = ("raw_candidate", "adjacent_visible_label", "unresolved")
    else:
        add_region(CropCategory.LINE_ITEM_TABLE, CropRole.PRIMARY, context_required=True)
        expected_fields = ("raw_candidate", "evidence_reference", "unresolved")

    if context_required:
        add_region(CropCategory.PAGE_CONTEXT, CropRole.CONTEXT, page=target_page)
    crops = _bounded_planned_crops(
        _dedupe_planned_crops(crops), maximum=DEFAULT_MAX_IMAGE_COUNT,
    )
    if not crops:
        raise EvidenceLocalizationError("supplementary_evidence_localization_unavailable")
    selected_pages = tuple(dict.fromkeys(item.coordinates.page_number for item in crops))
    reason = f"deterministic_{target_category}_{subtype.value}"
    return SupplementaryEvidencePlan(
        opaque_document_id=opaque_document_id,
        target_id=target_id,
        target_category=target_category,
        target_subtype=subtype,
        source_page_numbers=selected_pages or pages[:1],
        crops=tuple(crops),
        evidence_anchors=tuple(_anchors_for_crops(anchors, crops)),
        context_thumbnail_required=context_required,
        related_region_requirements=tuple(dict.fromkeys(related)),
        expected_observable_fields=expected_fields,
        privacy_minimization=PrivacyMinimizationResult(passed=True),
        plan_generation_reason_code=reason,
    )


def classify_target_subtype(
    *, target_category: str, target_page: int, initial_facts: Mapping[str, Any],
    blocks: Sequence[_LocalBlock] | None = None, page_count: int = 1,
) -> SupplementaryTargetSubtype:
    """Classify visual intent without using the reconciliation delta as an answer."""

    if target_category == "invoice_number_ambiguity":
        return SupplementaryTargetSubtype.INVOICE_IDENTITY
    if target_category == "paid_crossed_out_row_status":
        return SupplementaryTargetSubtype.PAID_OR_CROSSED_ROW
    if target_category == "page_continuation":
        return SupplementaryTargetSubtype.PAGE_CONTINUATION
    if target_category == "date_ambiguity":
        return SupplementaryTargetSubtype.DATE_IDENTITY
    if target_category == "vendor_name_ambiguity":
        return SupplementaryTargetSubtype.VENDOR_IDENTITY
    if target_category == "duplicate_row_suspicion":
        return SupplementaryTargetSubtype.DUPLICATE_ROW
    if target_category == "quantity_unit_price_mismatch":
        return SupplementaryTargetSubtype.QUANTITY_PRICE

    text = " ".join(item.text for item in (blocks or [])).casefold()
    total_label_count = len(re.findall(r"\b(?:invoice\s+total|amount\s+due|balance\s+due|grand\s+total|total)\b", text))
    if re.search(r"\bstatement\b", text) and total_label_count > 1:
        return SupplementaryTargetSubtype.STATEMENT_VS_INVOICE
    if re.search(r"\b(previous|prior)\s+balance\b|\bbalance\s+forward\b", text):
        return SupplementaryTargetSubtype.PREVIOUS_BALANCE
    if re.search(r"\bpayment\b|\bdeposit\b", text):
        return SupplementaryTargetSubtype.PAYMENT_OR_DEPOSIT
    if re.search(r"\bcredit\b|\bdiscount\b", text):
        return SupplementaryTargetSubtype.MISSING_DISCOUNT_OR_CREDIT
    if re.search(r"\btax\b|\bfee\b|\bshipping\b|\bfreight\b|\bsurcharge\b", text):
        return SupplementaryTargetSubtype.MISSING_TAX_OR_FEE
    if page_count > 1 and (
        re.search(r"\bcontinued\b|\bcarried\s+forward\b|\bpage\s+\d+\s+of\s+\d+\b", text)
        or any(_positive_int(item.get("source_page")) not in {None, target_page}
               for item in initial_facts.get("line_items") or [] if isinstance(item, Mapping))
    ):
        return SupplementaryTargetSubtype.PAGE_CONTINUATION
    if total_label_count > 1:
        return SupplementaryTargetSubtype.AMBIGUOUS_TOTAL_LABEL
    if target_category == "missing_line_item" or any(
        str(item or "") in {"financial_content_collapsed", "financial_content_skipped", "payable_rows_missing"}
        for item in _target_trigger_codes(initial_facts)
    ):
        return SupplementaryTargetSubtype.OMITTED_LINE_ITEM
    return SupplementaryTargetSubtype.UNKNOWN_TOTAL_COMPOSITION


def build_evidence_packet(
    plan: SupplementaryEvidencePlan, *, page_images: Mapping[int, Sequence[str] | str],
) -> SupplementaryEvidencePacket:
    """Crop and validate a plan locally; raises before any provider dispatch."""

    try:
        from PIL import Image  # type: ignore
    except Exception as exc:  # pragma: no cover - dependency is part of runtime
        raise EvidenceLocalizationError("supplementary_crop_renderer_unavailable") from exc
    if not plan.privacy_minimization.passed:
        raise EvidenceLocalizationError("supplementary_evidence_privacy_validation_failed")
    anchors = {item.anchor_id: item for item in plan.evidence_anchors}
    images: list[EvidencePacketImage] = []
    perceptual: list[int] = []
    total_pixels = 0
    for crop in plan.crops:
        page_refs = page_images.get(crop.coordinates.page_number)
        refs = [page_refs] if isinstance(page_refs, str) else list(page_refs or [])
        source = _largest_decodable_image(refs)
        if source is None:
            raise EvidenceLocalizationError("supplementary_evidence_source_page_unavailable")
        source_image, _source_ref = source
        if crop.anchor_ids and not any(
            _intersects(crop.coordinates, anchors[item].coordinates)
            for item in crop.anchor_ids if item in anchors
        ):
            raise EvidenceLocalizationError("supplementary_crop_anchor_mismatch")
        if crop.target_label_required:
            crop_anchors = [anchors[item] for item in crop.anchor_ids if item in anchors]
            label_detected = any(item.label_detected for item in crop_anchors)
            locally_detectable = any(
                "with_detectable_text" in item.source_kind for item in crop_anchors
            )
            if locally_detectable and not label_detected:
                raise EvidenceLocalizationError("supplementary_target_label_not_localized")
        rendered = _crop_image(source_image, crop.coordinates, role=crop.role, max_edge=plan.maximum_edge_pixels)
        width, height = rendered.size
        if crop.role is not CropRole.CONTEXT and (
            width < MIN_PRIMARY_WIDTH or height < MIN_PRIMARY_HEIGHT
        ):
            raise EvidenceLocalizationError("supplementary_crop_unreadable")
        encoded, digest = _encode_image(rendered)
        dhash = _difference_hash(rendered)
        if any(_hamming_distance(dhash, seen) <= 2 for seen in perceptual):
            continue
        perceptual.append(dhash)
        pixels = width * height
        if len(images) >= plan.maximum_image_count or total_pixels + pixels > plan.maximum_combined_pixels:
            raise EvidenceLocalizationError("supplementary_crop_pixel_budget_exceeded")
        total_pixels += pixels
        images.append(EvidencePacketImage(
            crop_id=crop.crop_id, role=crop.role, category=crop.category,
            page_number=crop.coordinates.page_number, width=width, height=height,
            pixel_count=pixels, image_sha256=digest, data_url=encoded,
        ))
    if not images or not any(item.role is CropRole.PRIMARY for item in images):
        raise EvidenceLocalizationError("supplementary_evidence_packet_invalid")
    if plan.context_thumbnail_required and not any(item.role is CropRole.CONTEXT for item in images):
        raise EvidenceLocalizationError("supplementary_context_thumbnail_missing")
    return SupplementaryEvidencePacket(
        plan_id=plan.plan_id, images=tuple(images), combined_pixels=total_pixels,
        validation_codes=("nonempty", "anchors_intersect", "readable", "bounded", "deduplicated"),
    )


def validate_evidence_packet(
    plan: SupplementaryEvidencePlan, packet: SupplementaryEvidencePacket,
) -> None:
    if packet.plan_id != plan.plan_id:
        raise EvidenceLocalizationError("supplementary_evidence_plan_packet_mismatch")
    if not packet.images or len(packet.images) > plan.maximum_image_count:
        raise EvidenceLocalizationError("supplementary_evidence_packet_invalid")
    if packet.combined_pixels > plan.maximum_combined_pixels:
        raise EvidenceLocalizationError("supplementary_crop_pixel_budget_exceeded")
    crop_ids = {item.crop_id for item in plan.crops}
    if any(item.crop_id not in crop_ids for item in packet.images):
        raise EvidenceLocalizationError("supplementary_unplanned_crop_forbidden")
    if not any(item.role is CropRole.PRIMARY for item in packet.images):
        raise EvidenceLocalizationError("supplementary_primary_crop_missing")


def second_plan_justification(
    first: SupplementaryEvidencePlan, second: SupplementaryEvidencePlan,
) -> str | None:
    """Permit slot two only for a distinct target or deterministically new region."""

    if first.target_id != second.target_id:
        return "distinct_deterministic_target"
    if first.target_subtype != second.target_subtype and first.crop_fingerprint != second.crop_fingerprint:
        return "distinct_related_region"
    return None


def page_image_mapping(
    refs: Sequence[str], *, page_numbers: Sequence[int],
) -> dict[int, list[str]]:
    if not refs:
        return {}
    pages = list(page_numbers or [1])
    per_page = max(1, len(refs) // len(pages))
    result: dict[int, list[str]] = {}
    for index, page in enumerate(pages):
        start = index * per_page
        end = len(refs) if index == len(pages) - 1 else min(len(refs), start + per_page)
        result[int(page)] = list(refs[start:end])
    return result


def _layout_blocks(
    layout: Mapping[str, Any] | None, initial_facts: Mapping[str, Any],
) -> list[_LocalBlock]:
    result: list[_LocalBlock] = []
    for page in (layout or {}).get("pages") or []:
        if not isinstance(page, Mapping):
            continue
        page_number = _positive_int(page.get("page_number")) or 1
        for raw in page.get("blocks") or []:
            if not isinstance(raw, Mapping):
                continue
            bbox = _coordinates_from_any(raw.get("bbox"), page_number)
            text = str(raw.get("text") or "").strip()
            if bbox and text:
                result.append(_LocalBlock(page_number, text, bbox, str(raw.get("source") or "local_layout")))
    for raw in _all_evidence(initial_facts):
        page_number = _positive_int(raw.get("page") or raw.get("page_number")) or 1
        bbox = _coordinates_from_any(raw.get("bbox"), page_number)
        text = str(raw.get("text") or "").strip()
        if bbox and text:
            result.append(_LocalBlock(page_number, text, bbox, str(raw.get("extraction_method") or "existing_evidence")))
    return result


def _all_evidence(facts: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    for item in facts.get("evidence") or []:
        if isinstance(item, Mapping):
            yield item
    for line in facts.get("line_items") or []:
        if not isinstance(line, Mapping):
            continue
        for item in line.get("evidence") or []:
            if isinstance(item, Mapping):
                yield item


def _coordinates_from_any(value: Any, page: int) -> NormalizedCropCoordinates | None:
    try:
        if isinstance(value, Mapping):
            values = [value.get(key) for key in ("x", "y", "w", "h")]
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) >= 4:
            values = list(value[:4])
        else:
            return None
        nums = [float(item) for item in values]
        scale = 1000.0 if max(abs(item) for item in nums) > 1.0 else 1.0
        x, y, width, height = [item / scale for item in nums]
        x, y = max(0.0, min(1.0, x)), max(0.0, min(1.0, y))
        width, height = max(0.0001, min(1.0 - x, width)), max(0.0001, min(1.0 - y, height))
        return NormalizedCropCoordinates(page_number=page, x=x, y=y, width=width, height=height)
    except Exception:
        return None


def _matching_blocks(
    blocks: Sequence[_LocalBlock], category: CropCategory, page: int,
) -> list[_LocalBlock]:
    on_page = [item for item in blocks if item.page_number == page]
    if category is CropCategory.DOCUMENT_HEADER:
        return [item for item in on_page if item.bbox.y < 0.35]
    if category is CropCategory.PAGE_BOTTOM:
        return [item for item in on_page if item.bbox.y > 0.72]
    if category is CropCategory.PAGE_TOP:
        return [item for item in on_page if item.bbox.y < 0.25]
    if category is CropCategory.PAGE_CONTEXT:
        return []
    patterns = _REGION_PATTERNS.get(category, ())
    return [item for item in on_page if any(re.search(pattern, item.text, re.I) for pattern in patterns)]


def _region_coordinates(
    category: CropCategory, page: int, blocks: Sequence[_LocalBlock],
) -> NormalizedCropCoordinates:
    if blocks:
        padding = 0.08 if category in {CropCategory.INVOICE_IDENTITY, CropCategory.PAID_ROW} else 0.05
        return _ensure_minimum_extent(
            _union_coordinates([item.bbox for item in blocks], padding=padding), category,
        )
    defaults: Mapping[CropCategory, tuple[float, float, float, float]] = {
        CropCategory.DOCUMENT_HEADER: (0.02, 0.01, 0.96, 0.30),
        CropCategory.INVOICE_IDENTITY: (0.45, 0.01, 0.53, 0.30),
        CropCategory.VENDOR_HEADER_CONTEXT: (0.02, 0.01, 0.58, 0.34),
        CropCategory.LINE_ITEM_TABLE: (0.02, 0.22, 0.96, 0.56),
        CropCategory.TOTALS_FOOTER: (0.46, 0.66, 0.52, 0.32),
        CropCategory.TAX_FEE: (0.48, 0.62, 0.50, 0.30),
        CropCategory.CREDITS_PAYMENTS: (0.44, 0.60, 0.54, 0.34),
        CropCategory.PRIOR_BALANCE: (0.40, 0.54, 0.58, 0.38),
        CropCategory.PAGE_BOTTOM: (0.02, 0.72, 0.96, 0.27),
        CropCategory.PAGE_TOP: (0.02, 0.01, 0.96, 0.28),
        CropCategory.PAID_ROW: (0.02, 0.30, 0.96, 0.45),
        CropCategory.SERVICE_ADDRESS: (0.02, 0.10, 0.60, 0.30),
        CropCategory.PAGE_CONTEXT: (0.0, 0.0, 1.0, 1.0),
    }
    x, y, width, height = defaults.get(category, (0.02, 0.02, 0.96, 0.96))
    return NormalizedCropCoordinates(page_number=page, x=x, y=y, width=width, height=height)


def _ensure_minimum_extent(
    coords: NormalizedCropCoordinates, category: CropCategory,
) -> NormalizedCropCoordinates:
    minimums: Mapping[CropCategory, tuple[float, float]] = {
        CropCategory.INVOICE_IDENTITY: (0.38, 0.16),
        CropCategory.VENDOR_HEADER_CONTEXT: (0.48, 0.20),
        CropCategory.LINE_ITEM_TABLE: (0.72, 0.32),
        CropCategory.TOTALS_FOOTER: (0.42, 0.20),
        CropCategory.TAX_FEE: (0.36, 0.16),
        CropCategory.CREDITS_PAYMENTS: (0.42, 0.18),
        CropCategory.PRIOR_BALANCE: (0.42, 0.18),
        CropCategory.PAID_ROW: (0.72, 0.16),
    }
    min_width, min_height = minimums.get(category, (0.30, 0.12))
    width = max(coords.width, min_width)
    height = max(coords.height, min_height)
    center_x = coords.x + coords.width / 2.0
    center_y = coords.y + coords.height / 2.0
    x = max(0.0, min(1.0 - width, center_x - width / 2.0))
    y = max(0.0, min(1.0 - height, center_y - height / 2.0))
    return NormalizedCropCoordinates(
        page_number=coords.page_number, x=x, y=y, width=width, height=height,
    )


def _union_coordinates(
    coords: Sequence[NormalizedCropCoordinates], *, padding: float,
) -> NormalizedCropCoordinates:
    first = coords[0]
    x = max(0.0, min(item.x for item in coords) - padding)
    y = max(0.0, min(item.y for item in coords) - padding)
    right = min(1.0, max(item.right for item in coords) + padding)
    bottom = min(1.0, max(item.bottom for item in coords) + padding)
    return NormalizedCropCoordinates(
        page_number=first.page_number, x=x, y=y,
        width=max(0.0001, right - x), height=max(0.0001, bottom - y),
    )


def _related_total_categories(subtype: SupplementaryTargetSubtype) -> list[CropCategory]:
    if subtype is SupplementaryTargetSubtype.MISSING_TAX_OR_FEE:
        return [CropCategory.TAX_FEE]
    if subtype in {SupplementaryTargetSubtype.MISSING_DISCOUNT_OR_CREDIT, SupplementaryTargetSubtype.PAYMENT_OR_DEPOSIT}:
        return [CropCategory.CREDITS_PAYMENTS]
    if subtype is SupplementaryTargetSubtype.PREVIOUS_BALANCE:
        return [CropCategory.PRIOR_BALANCE]
    return []


def _dedupe_planned_crops(crops: Sequence[PlannedEvidenceCrop]) -> list[PlannedEvidenceCrop]:
    result: list[PlannedEvidenceCrop] = []
    for crop in crops:
        duplicate = any(
            crop.coordinates.page_number == existing.coordinates.page_number
            and crop.role is existing.role
            and crop.category is existing.category
            and _overlap_ratio(crop.coordinates, existing.coordinates) >= 0.96
            for existing in result
        )
        if not duplicate:
            result.append(crop)
    return result


def _bounded_planned_crops(
    crops: Sequence[PlannedEvidenceCrop], *, maximum: int,
) -> list[PlannedEvidenceCrop]:
    priority = {
        CropRole.PRIMARY: 0,
        CropRole.CONTEXT: 1,
        CropRole.RELATED: 2,
        CropRole.CONTINUATION: 3,
    }
    ordered = sorted(enumerate(crops), key=lambda item: (priority[item[1].role], item[0]))
    chosen_indexes = {index for index, _crop in ordered[:maximum]}
    return [crop for index, crop in enumerate(crops) if index in chosen_indexes]


def _anchors_for_crops(
    anchors: Sequence[EvidenceAnchor], crops: Sequence[PlannedEvidenceCrop],
) -> list[EvidenceAnchor]:
    used = {anchor_id for crop in crops for anchor_id in crop.anchor_ids}
    return [anchor for anchor in anchors if anchor.anchor_id in used]


def _largest_decodable_image(refs: Sequence[str]) -> tuple[Any, str] | None:
    try:
        from PIL import Image  # type: ignore
    except Exception:
        return None
    candidates: list[tuple[int, Any, str]] = []
    for ref in refs:
        try:
            if not str(ref).startswith("data:image/"):
                continue
            raw = base64.b64decode(str(ref).split(",", 1)[1], validate=True)
            image = Image.open(io.BytesIO(raw)).convert("RGB")
            candidates.append((image.width * image.height, image, str(ref)))
        except Exception:
            continue
    if not candidates:
        return None
    _, image, ref = max(candidates, key=lambda item: item[0])
    return image, ref


def _crop_image(image: Any, coords: NormalizedCropCoordinates, *, role: CropRole, max_edge: int) -> Any:
    from PIL import Image  # type: ignore
    left = max(0, int(round(coords.x * image.width)))
    top = max(0, int(round(coords.y * image.height)))
    right = min(image.width, max(left + 1, int(round(coords.right * image.width))))
    bottom = min(image.height, max(top + 1, int(round(coords.bottom * image.height))))
    output = image.crop((left, top, right, bottom))
    edge = 1200 if role is CropRole.CONTEXT else max_edge
    if max(output.size) > edge:
        output.thumbnail((edge, edge), Image.Resampling.LANCZOS)
    return output


def _encode_image(image: Any) -> tuple[str, str]:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=88, optimize=True)
    raw = buffer.getvalue()
    return "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii"), hashlib.sha256(raw).hexdigest()


def _difference_hash(image: Any) -> int:
    from PIL import Image  # type: ignore
    small = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    pixels = list(
        small.get_flattened_data() if hasattr(small, "get_flattened_data") else small.getdata()
    )
    value = 0
    for row in range(8):
        for col in range(8):
            value = (value << 1) | int(pixels[row * 9 + col] > pixels[row * 9 + col + 1])
    return value


def _hamming_distance(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def _intersects(left: NormalizedCropCoordinates, right: NormalizedCropCoordinates) -> bool:
    return (
        left.page_number == right.page_number
        and min(left.right, right.right) > max(left.x, right.x)
        and min(left.bottom, right.bottom) > max(left.y, right.y)
    )


def _overlap_ratio(left: NormalizedCropCoordinates, right: NormalizedCropCoordinates) -> float:
    if left.page_number != right.page_number:
        return 0.0
    width = max(0.0, min(left.right, right.right) - max(left.x, right.x))
    height = max(0.0, min(left.bottom, right.bottom) - max(left.y, right.y))
    intersection = width * height
    smaller = min(left.width * left.height, right.width * right.height)
    return intersection / smaller if smaller else 0.0


def _target_value(target: Any) -> str:
    value = getattr(target, "target_type", target)
    return str(getattr(value, "value", value) or "").strip()


def _stable_target_id(target: Any) -> str:
    if hasattr(target, "model_dump"):
        payload = target.model_dump(mode="json")
    elif isinstance(target, Mapping):
        payload = dict(target)
    else:
        payload = {"target": str(target)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:20]


def _identifier(prefix: str, *parts: Any) -> str:
    raw = json.dumps(parts, sort_keys=True, default=str, separators=(",", ":"))
    return f"{prefix}_{hashlib.sha256(raw.encode()).hexdigest()[:16]}"


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
        return parsed if parsed > 0 else None
    except (TypeError, ValueError):
        return None


def _target_trigger_codes(initial_facts: Mapping[str, Any]) -> list[str]:
    return [str(item or "") for item in initial_facts.get("warnings") or []]


__all__ = [
    "CropCategory", "CropRole", "EvidenceLocalizationError", "EvidencePacketImage",
    "NormalizedCropCoordinates", "PlannedEvidenceCrop", "PrivacyMinimizationResult",
    "SupplementaryEvidencePacket", "SupplementaryEvidencePlan",
    "SupplementaryTargetSubtype", "build_evidence_packet",
    "build_supplementary_evidence_plan", "classify_target_subtype",
    "page_image_mapping", "second_plan_justification", "validate_evidence_packet",
]
