from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from webapp.backend.services import deterministic_builder as builder


@pytest.fixture()
def private_builder_root(tmp_path, monkeypatch):
    root = tmp_path / "private_builder"
    monkeypatch.setattr(builder, "ROOT", root)
    return root


@pytest.mark.parametrize(
    ("original_filename", "expected_filename"),
    [
        (r"C:\private\manager\invoice.csv", "invoice.csv"),
        ("C:/private/manager/invoice.csv", "invoice.csv"),
        ("/private/manager/invoice.csv", "invoice.csv"),
        ("relative/folder/invoice.csv", "invoice.csv"),
        ("invoice.csv", "invoice.csv"),
    ],
)
def test_private_sample_preserves_safe_filename_and_never_serializes_absolute_path(
    private_builder_root, original_filename, expected_filename,
):
    session = builder.create_session("alabama_power")
    session = builder.add_sample(
        session.session_id,
        original_filename=original_filename,
        content=b"invoice,total\nABC,10.00\n",
    )
    assert session.samples[0].original_filename == expected_filename
    evidence_path = private_builder_root / session.session_id / "samples" / session.samples[0].sample_id / "evidence.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["source_path"] == ""
    assert original_filename not in evidence_path.read_text(encoding="utf-8")


def test_ai_chat_only_updates_validated_draft_and_never_activates(monkeypatch, private_builder_root):
    session = builder.create_session("alabama_power")
    pattern = next(
        item for item in builder.deterministic_coverage.coverage_for_key("alabama_power").patterns
        if item.path not in {"vendor_identity.aliases", "vendor_identity.detection_keywords"}
    )
    monkeypatch.setattr(builder, "_select_accounting_profile", lambda: SimpleNamespace(profile_id="test-accounting"))
    monkeypatch.setattr(builder, "_estimate_cost", lambda *_args, **_kwargs: 0.001)
    monkeypatch.setattr(builder, "_request_builder_model", lambda *_args, **_kwargs: builder.BuilderModelResponse(
        assistant_message="I prepared a draft for review.",
        proposed_changes=[
            builder.ProposedConfigChange(path=pattern.path, value=[*pattern.values, "SAFE TEST"], rationale="Sample evidence."),
            builder.ProposedConfigChange(path="python.execute", value="unsafe", rationale="Must be rejected."),
        ],
    ))
    applied = []
    monkeypatch.setattr(builder.vendor_rules, "apply_patch", lambda *_args, **_kwargs: applied.append(True))

    updated = builder.chat(session.session_id, message="Improve the vendor pattern.")

    assert pattern.path in updated.draft_patch
    assert "python.execute" not in updated.draft_patch
    assert updated.status == "draft"
    assert updated.preview.status == "not_run"
    assert applied == []
    assert updated.messages[-1].proposed_paths == [pattern.path]


def test_selected_column_must_exist_in_current_preview(private_builder_root):
    session = builder.create_session("alabama_power")
    with pytest.raises(ValueError, match="Selected column"):
        session.preview.columns = ["Invoice Number"]
        builder._write(session)
        builder.chat(session.session_id, message="Set it", selected_column="GL Account")


def test_approval_requires_matching_revision_and_passing_preview(monkeypatch, private_builder_root):
    session = builder.create_session("alabama_power")
    session.draft_patch = {"vendor_identity.detection_keywords": ["ALABAMA POWER"]}
    session.revision = 2
    builder._write(session)
    with pytest.raises(ValueError, match="passing preview"):
        builder.approve(session.session_id, expected_revision=2)

    session.preview = builder.BuilderPreview(status="passed", revision=2, columns=["Invoice Number"], rows=[], row_count=1)
    builder._write(session)
    with pytest.raises(ValueError, match="revision changed"):
        builder.approve(session.session_id, expected_revision=1)

    calls = []
    monkeypatch.setattr(builder.vendor_rules, "apply_patch", lambda key, patch: calls.append((key, patch)) or {
        "backup_filename": "safe.yaml", "written_paths": sorted(patch),
    })
    approved = builder.approve(session.session_id, expected_revision=2)
    assert approved.status == "approved"
    assert calls == [("alabama_power", session.draft_patch)]
    assert approved.audit[-1]["event"] == "draft_approved"


def test_preview_is_dry_run_uses_revision_and_never_applies(monkeypatch, private_builder_root, tmp_path):
    session = builder.create_session("alabama_power")
    session = builder.add_sample(
        session.session_id, original_filename="sample.csv", content=b"invoice,total\nABC,10.00\n",
    )
    session.draft_patch = {"vendor_identity.detection_keywords": ["ALABAMA POWER"]}
    session.revision = 3
    builder._write(session)
    batch_input = tmp_path / "batch_input"
    batch_input.mkdir()
    deleted = []
    applied = []
    monkeypatch.setattr(builder.batch_store, "create_batch", lambda: "batch_20260717_120000_001")
    monkeypatch.setattr(builder.batch_store, "get_input_dir", lambda _batch: batch_input)
    monkeypatch.setattr(builder.batch_store, "delete_batch", lambda batch: deleted.append(batch))
    process_calls = []
    monkeypatch.setattr(builder.batch_processor, "process_batch", lambda batch, **kwargs: process_calls.append((batch, kwargs)) or {
        "dry_run": kwargs.get("dry_run"), "batch": batch,
    })
    monkeypatch.setattr(builder.rules_impact, "_flatten_rows", lambda result, vendor: [{
        "__key": "1", "Invoice Number": "ABC", "Amount": "10.00",
    }])
    monkeypatch.setattr(builder.vendor_rules, "apply_patch", lambda *_args, **_kwargs: applied.append(True))

    previewed = builder.preview(session.session_id)

    assert previewed.status == "previewed"
    assert previewed.preview.status == "passed"
    assert previewed.preview.revision == 3
    assert previewed.preview.rows == [{"Invoice Number": "ABC", "Amount": "10.00"}]
    assert deleted == ["batch_20260717_120000_001"]
    assert applied == []
    assert process_calls[0][1]["dry_run"] is True
    assert process_calls[0][1]["forced_vendor_key"] == "alabama_power"


def test_forced_vendor_routing_cannot_be_used_outside_registered_dry_run():
    with pytest.raises(ValueError, match="only for dry-run"):
        builder.batch_processor.process_batch(
            "not_a_batch", dry_run=False, forced_vendor_key="alabama_power",
        )
    with pytest.raises(ValueError, match="not a registered"):
        builder.batch_processor.process_batch(
            "not_a_batch", dry_run=True, forced_vendor_key="invented_vendor",
        )
