import copy
from datetime import datetime, timezone
from pathlib import Path

from webapp.backend.services.accounting_contracts import CropCoordinates
from webapp.backend.services.evidence_benchmark import (
    EvidenceAsset, EvidenceBackedField, EvidenceBackedGoldenContract,
    GoldenInvoiceContract, GoldenRowContract,
)
from webapp.backend.services.evidence_benchmark_gates import evaluate


def test_pending_human_contract_passes_replay_safety_but_not_independent_gold(tmp_path: Path):
    crop = tmp_path / "crop.png"
    crop.write_bytes(b"evidence")
    from webapp.backend.services.evidence_benchmark import file_sha256
    asset = EvidenceAsset(
        source_document_sha256="a" * 64, source_page=1,
        crop_coordinates=CropCoordinates(page=1, x=0, y=0, width=1, height=1, render_dpi=600),
        crop_sha256=file_sha256(crop), crop_ref="crop.png",
    )
    field = lambda name: EvidenceBackedField(field_name=name, evidence=[asset])
    row = GoldenRowContract(
        row_id="inv:line:001", source_page=1, row_identity=field("row_identity"),
        paid_crossed_out_status=field("paid"), line_item_concept=field("concept"),
        amount=field("amount"), canonical_semantic_concept=None,
    )
    contract = EvidenceBackedGoldenContract(
        batch_id="batch", created_at=datetime.now(timezone.utc),
        source_manifest_sha256="b" * 64,
        invoices=[GoldenInvoiceContract(
            invoice_id="inv", source_file_name="source.pdf",
            source_document_sha256="a" * 64, source_page=1,
            header_fields={}, rows=[row],
        )],
    )
    replay = {"all_invoices": [{"invoice_number": "inv", "rows": [{"GL Account": ""}]}]}
    result = evaluate(
        contract=contract, replay=replay, replay_repeat=copy.deepcopy(replay),
        replay_metrics={"readiness": {"inv": {"status": "blocked", "export_allowed": False}},
                        "false_safe_export_count": 0, "provider_calls_executed": 0,
                        "external_provider_network_attempts": 0},
        benchmark_root=tmp_path,
        verifier_results={"successful_crop_count": 1},
    )
    assert result["deterministic_replay_gate"]["status"] == "pass"
    assert result["independent_cold_extraction_gate"]["status"] == "blocked_pending_human_adjudication"
    assert result["safety"]["source_evidence_loss_count"] == 0
    assert result["safety"]["false_safe_export_count"] == 0
