from __future__ import annotations

import base64
import copy
import io
import inspect
import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from pydantic import ValidationError

from webapp.backend.services import ai_invoice_processor, ai_provider
from webapp.backend.services.gemini_supplementary_verification import (
    GeminiSupplementaryObservation,
    IdentityCandidateType,
    SupplementaryEvidenceReference,
    SupplementaryIdentityCandidate,
    SupplementaryObservedCandidate,
    SupplementaryResolutionKind,
    SupplementaryTarget,
    SupplementaryTargetType,
    merge_supplementary_observations,
    parse_supplementary_response,
    select_supplementary_targets,
    supplementary_response_format,
)
from webapp.backend.services.supplementary_evidence_planner import (
    CropCategory,
    CropRole,
    EvidenceLocalizationError,
    PlannedEvidenceCrop,
    NormalizedCropCoordinates,
    SupplementaryTargetSubtype,
    build_evidence_packet,
    build_supplementary_evidence_plan,
    page_image_mapping,
    second_plan_justification,
)
from webapp.backend.services.experiment_spend_controller import (
    ExperimentSpendController,
    activate_experiment_spend_gate,
)


FIXTURE = Path(__file__).parent / "fixtures" / "supplementary_evidence_plans_synthetic.json"


def _target(value: str) -> SupplementaryTarget:
    return SupplementaryTarget(
        target_type=SupplementaryTargetType(value), page_number=1,
        field_name="invoice_number" if value == "invoice_number_ambiguity" else "reconciliation",
        local_trigger_codes=["synthetic_local_validation"],
    )


def _layout(text: str, page_count: int = 1) -> dict:
    pages = []
    for page in range(1, page_count + 1):
        pages.append({
            "page_number": page,
            "blocks": [{
                "text": text if page == 1 else "Continued Description Amount Total",
                "bbox": {"x": 0.08, "y": 0.08 if page == 1 else 0.04, "w": 0.84, "h": 0.78},
                "source": "synthetic_pdf_text",
            }],
        })
    return {"page_count": page_count, "pages": pages}


def _facts(page_count: int = 1) -> dict:
    rows = [{
        "source_page": page, "raw_description": f"synthetic row {page}",
        "amount": "10.00", "evidence": [],
    } for page in range(1, page_count + 1)]
    return {
        "line_items": rows, "total_amount": "25.00", "tax_amount": None,
        "fees_amount": None, "shipping_amount": None, "evidence": [],
        "warnings": [], "page_reconciliations": [{
            "page": 1, "component_total": "10.00", "printed_total": "25.00",
            "difference": "15.00", "status": "mismatch",
        }],
    }


def _image_ref(width: int = 1600, height: int = 2000) -> str:
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    for y, color in ((80, "navy"), (500, "black"), (1300, "purple"), (1700, "darkgreen")):
        if y >= height - 20:
            continue
        draw.rectangle((50, y, width - 50, min(height - 20, y + 120)), outline=color, width=8)
        draw.text((80, y + 30), f"synthetic region {y}", fill=color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


@pytest.mark.parametrize("case", json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"])
def test_synthetic_target_plans_select_expected_subtype_and_regions(case):
    target = _target(case["target_type"])
    plan = build_supplementary_evidence_plan(
        opaque_document_id="opaque-synthetic", target=target,
        initial_facts=_facts(case["page_count"]),
        document_layout=_layout(case["page_text"], case["page_count"]),
    )
    assert plan.target_subtype.value == case["expected_subtype"]
    if "expected_categories" in case:
        categories = {crop.category.value for crop in plan.crops}
        assert set(case["expected_categories"]).issubset(categories)
    forbidden = {
        "expected_answer", "ground_truth", "gl", "readiness", "export_status",
        "human_corrections", "holdout_labels",
    }
    assert forbidden.isdisjoint(plan.model_dump(mode="json"))


def test_total_components_remain_distinct_and_delta_is_not_requested():
    target = _target("total_mismatch")
    plan = build_supplementary_evidence_plan(
        opaque_document_id="opaque", target=target, initial_facts=_facts(),
        document_layout=_layout("Description Amount Subtotal Tax Fee Credit Payment Previous Balance Amount Due"),
    )
    fields = set(plan.expected_observable_fields)
    assert {"tax", "fees", "credits", "payments", "previous_balance"}.issubset(fields)
    assert not any("delta" in field or "missing_amount" in field for field in fields)


def test_multipage_continuation_has_both_page_regions():
    target = _target("total_mismatch")
    plan = build_supplementary_evidence_plan(
        opaque_document_id="opaque", target=target, initial_facts=_facts(2),
        document_layout=_layout("Description Amount Page 1 of 2 Continued Subtotal", 2),
    )
    assert plan.target_subtype is SupplementaryTargetSubtype.PAGE_CONTINUATION
    categories = {(crop.category, crop.coordinates.page_number) for crop in plan.crops}
    assert (CropCategory.PAGE_BOTTOM, 1) in categories
    assert (CropCategory.PAGE_TOP, 2) in categories


def test_tight_crop_includes_context_and_packet_is_readable_and_bounded():
    target = _target("invoice_number_ambiguity")
    layout = {
        "page_count": 1,
        "pages": [{"page_number": 1, "blocks": [
            {"text": "Invoice Number", "bbox": {"x": .55, "y": .05, "w": .25, "h": .06}, "source": "pdf_text"},
            {"text": "Bill To", "bbox": {"x": .05, "y": .08, "w": .20, "h": .05}, "source": "pdf_text"},
        ]}],
    }
    plan = build_supplementary_evidence_plan(
        opaque_document_id="opaque", target=target, initial_facts=_facts(), document_layout=layout,
    )
    packet = build_evidence_packet(plan, page_images={1: [_image_ref()]})
    assert any(item.role is CropRole.PRIMARY for item in packet.images)
    assert any(item.role is CropRole.CONTEXT for item in packet.images)
    assert all(item.width >= 320 and item.height >= 120 for item in packet.images if item.role is not CropRole.CONTEXT)
    assert packet.combined_pixels <= plan.maximum_combined_pixels


def test_duplicate_near_identical_crops_are_removed():
    target = _target("total_mismatch")
    plan = build_supplementary_evidence_plan(
        opaque_document_id="opaque", target=target, initial_facts=_facts(),
        document_layout=_layout("Description Amount Subtotal Total"),
    )
    primary = next(item for item in plan.crops if item.role is CropRole.PRIMARY)
    duplicate = PlannedEvidenceCrop(
        crop_id="duplicate-related", role=CropRole.RELATED,
        category=CropCategory.TAX_FEE, coordinates=primary.coordinates,
        anchor_ids=primary.anchor_ids,
    )
    duplicated = plan.model_copy(update={"crops": (*plan.crops, duplicate), "maximum_image_count": 6})
    packet = build_evidence_packet(duplicated, page_images={1: [_image_ref()]})
    hashes = [item.image_sha256 for item in packet.images]
    assert len(hashes) == len(set(hashes))
    assert len(packet.images) < len(duplicated.crops)


def test_unreadable_identity_target_prevents_packet():
    plan = build_supplementary_evidence_plan(
        opaque_document_id="opaque", target=_target("invoice_number_ambiguity"),
        initial_facts=_facts(), document_layout=_layout(""),
    )
    with pytest.raises(EvidenceLocalizationError, match="supplementary_crop_unreadable"):
        build_evidence_packet(plan, page_images={1: [_image_ref(width=240, height=240)]})


def test_missing_target_label_rejects_packet_before_dispatch():
    plan = build_supplementary_evidence_plan(
        opaque_document_id="opaque", target=_target("invoice_number_ambiguity"),
        initial_facts=_facts(), document_layout=_layout("Statement details"),
    )
    with pytest.raises(
        EvidenceLocalizationError,
        match="supplementary_target_label_not_localized",
    ):
        build_evidence_packet(plan, page_images={1: [_image_ref()]})


def test_invalid_crop_geometry_is_rejected_by_typed_plan_contract():
    with pytest.raises(ValidationError):
        NormalizedCropCoordinates(
            page_number=1, x=1.1, y=0.1, width=0.2, height=0.2,
        )


def test_context_required_but_missing_prevents_packet():
    plan = build_supplementary_evidence_plan(
        opaque_document_id="opaque", target=_target("invoice_number_ambiguity"),
        initial_facts=_facts(), document_layout=_layout("Invoice Number Account Number"),
    )
    no_context = plan.model_copy(update={
        "crops": tuple(item for item in plan.crops if item.role is not CropRole.CONTEXT),
    })
    with pytest.raises(EvidenceLocalizationError, match="supplementary_context_thumbnail_missing"):
        build_evidence_packet(no_context, page_images={1: [_image_ref()]})


def test_invalid_crop_returns_review_without_dispatch_and_without_spend(
    monkeypatch, tmp_path,
):
    calls = []
    monkeypatch.setattr(
        ai_provider, "extract_gemini_supplementary_facts_structured",
        lambda **kwargs: calls.append(kwargs),
    )
    controller = ExperimentSpendController(tmp_path / "private", "offline-localization")
    before = controller.snapshot("A")
    with activate_experiment_spend_gate(
        controller, phase="A", pricing_version="synthetic-offline",
    ):
        result = ai_invoice_processor._run_controlled_gemini_supplementary(
            initial_facts=_facts(), escalation_reasons=["invoice_reconciliation_failed"],
            page_images_or_refs=[], page_numbers=[1], cost_scope_id="offline",
            document_layout=_layout("Description Amount Total"),
        )
    after = controller.snapshot("A")
    assert calls == []
    assert before.cumulative_charged_usd == after.cumulative_charged_usd == "0.000000"
    assert before.active_reserved_usd == after.active_reserved_usd == "0.000000"
    assert after.outstanding_reservation_ids == []
    assert after.by_provider_profile == {}
    assert result["accepted"] is False
    assert result["export_allowed"] is False
    assert "supplementary_evidence_localization_unavailable" in result["warnings"]


def test_second_slot_requires_distinct_justified_plan():
    target = _target("total_mismatch")
    first = build_supplementary_evidence_plan(
        opaque_document_id="opaque", target=target, initial_facts=_facts(),
        document_layout=_layout("Description Amount Subtotal Total"),
    )
    same = first.model_copy(deep=True)
    other_target = _target("invoice_number_ambiguity")
    second = build_supplementary_evidence_plan(
        opaque_document_id="opaque", target=other_target, initial_facts=_facts(),
        document_layout=_layout("Invoice Number Account Number"),
    )
    assert second_plan_justification(first, same) is None
    assert second_plan_justification(first, second) == "distinct_deterministic_target"


def test_invoice_candidates_preserve_labels_and_alternatives():
    target = _target("invoice_number_ambiguity")
    payload = {
        "target_type": target.target_type.value,
        "observed_candidate_value": None,
        "raw_visible_text": None,
        "page_number": 1,
        "evidence_reference": None,
        "confidence": .6,
        "contradiction_flag": False,
        "unresolved_flag": True,
        "warnings": [],
        "visibility_status": "ambiguous",
        "observed_candidates": [
            {
                "raw_candidate": "SYN-001", "adjacent_visible_label": "Invoice No.",
                "candidate_type": "invoice_number", "evidence_reference": {
                    "page_number": 1, "bbox": [0.5, 0.1, 0.2, 0.05],
                    "crop_id": "crop-a", "crop_role": "primary_target",
                }, "confidence": .7, "unresolved": False,
            },
            {
                "raw_candidate": "SYN-002", "adjacent_visible_label": "Work Order",
                "candidate_type": "work_order", "evidence_reference": {
                    "page_number": 1, "bbox": [0.5, 0.2, 0.2, 0.05],
                    "crop_id": "crop-a", "crop_role": "primary_target",
                }, "confidence": .6, "unresolved": False,
            },
        ],
        "financial_components": None,
    }
    observation = parse_supplementary_response(json.dumps(payload), target=target)
    assert [item.adjacent_visible_label for item in observation.observed_candidates] == [
        "Invoice No.", "Work Order",
    ]
    assert observation.unresolved_flag is True


def test_contradiction_is_separate_revision_and_initial_evidence_is_immutable():
    initial = _facts()
    initial["evidence"] = [{
        "page": 1, "text": "synthetic", "bbox": [0.1, 0.1, 0.2, 0.1],
        "source_type": "source", "extraction_method": "initial", "confidence": .9,
    }]
    before = copy.deepcopy(initial)
    target = _target("total_mismatch")
    observation = GeminiSupplementaryObservation(
        target_type=target.target_type,
        observed_candidate_value=SupplementaryObservedCandidate(
            resolution_kind=SupplementaryResolutionKind.TOTAL_AMOUNT,
            field_name="total_amount", raw_value="99.00", line_item=None,
        ),
        raw_visible_text="synthetic conflict", page_number=1,
        evidence_reference=SupplementaryEvidenceReference(
            page_number=1, bbox=[0.5, 0.7, 0.3, 0.1],
            crop_id="crop-a", crop_role="primary_target",
        ),
        confidence=.8, contradiction_flag=True, unresolved_flag=False,
        warnings=[],
    )
    merged = merge_supplementary_observations(initial, [(target, observation)])
    assert initial == before
    assert merged["supplementary_evidence_revisions"][0]["source_role"] == "supplementary_visual_observation"
    assert merged["needs_manual_review"] is True
    assert merged["visual_extraction_status"] == "partial"


def test_response_schema_is_facts_only_and_requires_multi_region_fields():
    schema = supplementary_response_format(_target("total_mismatch"))["json_schema"]["schema"]
    properties = schema["properties"]
    assert {"observed_candidates", "financial_components", "visibility_status"}.issubset(properties)
    serialized = json.dumps(schema).casefold()
    assert '"gl"' not in serialized
    assert '"readiness"' not in serialized
    assert '"export_allowed"' not in serialized


def test_page_mapping_is_stable_and_does_not_duplicate_pages():
    refs = ["one-full", "one-detail", "two-full", "two-detail"]
    assert page_image_mapping(refs, page_numbers=[1, 2]) == {
        1: ["one-full", "one-detail"], 2: ["two-full", "two-detail"],
    }


def test_visual_only_target_does_not_create_arithmetic_target():
    facts = _facts()
    facts["total_amount"] = "10.00"
    facts["page_reconciliations"][0].update({
        "component_total": "10.00", "printed_total": "10.00",
        "difference": "0.00", "status": "reconciled",
    })
    targets = select_supplementary_targets(facts, ["paid_marker_ambiguous"])
    assert [item.target_type for item in targets] == [
        SupplementaryTargetType.PAID_CROSSED_OUT_ROW_STATUS,
    ]


def test_planner_has_no_accounting_or_readiness_authority():
    plan = build_supplementary_evidence_plan(
        opaque_document_id="opaque", target=_target("total_mismatch"),
        initial_facts=_facts(), document_layout=_layout("Description Amount Subtotal Total"),
    )
    summary = plan.provider_summary()
    assert "gl" not in json.dumps(summary).casefold()
    assert "readiness" not in json.dumps(summary).casefold()
    assert "export" not in json.dumps(summary).casefold()
    assert "deposits" in summary["expected_observable_fields"]


def test_provider_surface_requires_validated_plan_and_packet_not_raw_page_refs():
    parameters = inspect.signature(
        ai_provider.extract_gemini_supplementary_facts_structured
    ).parameters
    assert "evidence_plan" in parameters
    assert "evidence_packet" in parameters
    assert "page_images_or_refs" not in parameters
