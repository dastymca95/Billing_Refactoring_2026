from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from webapp.backend.services.accounting_contracts import CropCoordinates
from webapp.backend.services.evidence_benchmark import (
    AdjudicationState,
    EvidenceAsset,
    EvidenceBackedField,
    HumanAdjudication,
)


def _asset() -> EvidenceAsset:
    return EvidenceAsset(
        source_document_sha256="a" * 64,
        source_page=1,
        crop_coordinates=CropCoordinates(
            page=1, x=10, y=20, width=30, height=40, render_dpi=600,
        ),
        crop_sha256="b" * 64,
        crop_ref="crops/invoice/row.png",
    )


def test_verifier_output_cannot_become_gold_without_human_adjudication():
    with pytest.raises(ValidationError, match="human adjudication"):
        EvidenceBackedField(
            field_name="row_identity",
            observed_raw_text="57B",
            accepted_normalized_value="57B",
            evidence=[_asset()],
            state=AdjudicationState.ADJUDICATED,
        )


def test_human_adjudication_must_match_accepted_value():
    with pytest.raises(ValidationError, match="human-adjudicated"):
        EvidenceBackedField(
            field_name="row_identity",
            observed_raw_text="57B",
            accepted_normalized_value="53B",
            evidence=[_asset()],
            state=AdjudicationState.ADJUDICATED,
            human_adjudication=HumanAdjudication(
                reviewer_id="reviewer-1",
                adjudicated_at=datetime.now(timezone.utc),
                accepted_value="57B",
                rationale="Confirmed against the source crop.",
            ),
        )


def test_crop_reference_cannot_expose_an_absolute_private_path():
    with pytest.raises(ValidationError, match="safe relative"):
        EvidenceAsset(
            source_document_sha256="a" * 64,
            source_page=1,
            crop_coordinates=CropCoordinates(
                page=1, x=10, y=20, width=30, height=40, render_dpi=600,
            ),
            crop_sha256="b" * 64,
            crop_ref="C:/private/source.png",
        )
