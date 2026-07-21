from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from webapp.backend.services.document_learning_experiment import (
    ExperimentPathError,
    assert_git_safe_summary,
    build_exact_leakage_groups,
    build_git_safe_summary,
    classify_ground_truth_eligibility,
    create_phase_a_calibration_sample,
    create_phase_a_split,
    deterministic_group_split,
    inspect_local_document,
    inventory_local_corpus,
    render_git_safe_summary,
    validate_private_paths,
)
from webapp.backend.services import document_learning_experiment as experiment_module


def _roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    project = tmp_path / "project"
    source = tmp_path / "private-source"
    runtime = project / "tmp" / "document-learning"
    project.mkdir()
    source.mkdir()
    (project / ".gitignore").write_text("/tmp/\n/webapp_data/\n", encoding="utf-8")
    return project, source, runtime


def _record(document_id: str, file_hash: str, *page_hashes: str) -> dict:
    return {
        "document_id": document_id,
        "content_sha256": file_hash,
        "exact_visual_page_hashes": list(page_hashes),
        "visual_hash_status": "verified",
        "size_bytes": 10,
        "page_count": len(page_hashes),
        "format_family": "image",
        "extension": ".png",
    }


def test_path_contract_requires_external_source_and_ignored_private_runtime(tmp_path: Path):
    project, source, runtime = _roots(tmp_path)
    contract = validate_private_paths(
        project_root=project, source_root=source, runtime_root=runtime
    )
    assert contract.runtime_root == runtime.resolve()

    inside_source = project / "source"
    inside_source.mkdir()
    with pytest.raises(ExperimentPathError, match="outside"):
        validate_private_paths(
            project_root=project, source_root=inside_source, runtime_root=runtime
        )
    with pytest.raises(ExperimentPathError, match="tmp or webapp_data"):
        validate_private_paths(
            project_root=project,
            source_root=source,
            runtime_root=project / "reports" / "private",
        )


def test_local_inventory_is_anonymous_exact_and_private(tmp_path: Path):
    project, source, runtime = _roots(tmp_path)
    image = Image.new("RGB", (16, 12), color=(12, 34, 56))
    private_name = "private-client-invoice-123.png"
    image.save(source / private_name)

    result = inventory_local_corpus(
        project_root=project, source_root=source, runtime_root=runtime
    )
    inventory_path = result.snapshot_root / "inventory.jsonl"
    record = json.loads(inventory_path.read_text(encoding="utf-8").strip())
    assert private_name not in inventory_path.read_text(encoding="utf-8")
    assert record["document_id"].startswith("doc-")
    assert len(record["content_sha256"]) == 64
    assert len(record["exact_visual_page_hashes"]) == 1
    assert record["visual_hash_status"] == "verified"

    locator_text = (result.snapshot_root / "private_locators.jsonl").read_text(
        encoding="utf-8"
    )
    assert private_name in locator_text
    assert result.snapshot_root.is_relative_to(runtime.resolve())
    assert result.git_safe_summary["network_calls"] == 0
    assert result.git_safe_summary["provider_calls"] == 0


def test_exact_page_identity_groups_documents_before_split():
    records = [
        _record("doc-a", "file-a", "shared-page"),
        _record("doc-b", "file-b", "shared-page"),
        _record("doc-c", "file-c", "unique-page"),
    ]
    groups = build_exact_leakage_groups(records)
    duplicate_group = next(group for group in groups if group["member_count"] == 2)
    assert duplicate_group["members"] == ["doc-a", "doc-b"]
    assert duplicate_group["reason_codes"] == ["exact_visual_page_sha256"]

    split = deterministic_group_split(
        records=records,
        groups=groups,
        dataset_version="corpus-test",
        seed="stable-seed",
    )
    assignment = next(item for item in split["assignments"] if item["document_count"] == 2)
    assert assignment["document_ids"] == ["doc-a", "doc-b"]
    assert split == deterministic_group_split(
        records=records,
        groups=groups,
        dataset_version="corpus-test",
        seed="stable-seed",
    )


def test_decoded_visual_identity_groups_different_encodings_only_when_pixels_match(
    tmp_path: Path,
):
    first_path = tmp_path / "first.png"
    second_path = tmp_path / "second.png"
    changed_path = tmp_path / "changed.png"
    base = Image.new("RGB", (12, 9), color=(90, 40, 12))
    base.save(first_path, compress_level=0)
    base.save(second_path, compress_level=9)
    changed = base.copy()
    changed.putpixel((0, 0), (91, 40, 12))
    changed.save(changed_path, compress_level=9)
    salt = b"x" * 32

    first = inspect_local_document(path=first_path, relative_path="first.png", salt=salt)
    second = inspect_local_document(path=second_path, relative_path="second.png", salt=salt)
    changed_record = inspect_local_document(
        path=changed_path, relative_path="changed.png", salt=salt
    )
    assert first["content_sha256"] != second["content_sha256"]
    assert first["exact_visual_page_hashes"] == second["exact_visual_page_hashes"]
    assert first["exact_visual_page_hashes"] != changed_record["exact_visual_page_hashes"]

    groups = build_exact_leakage_groups([first, second, changed_record])
    matching = next(group for group in groups if group["member_count"] == 2)
    assert set(matching["members"]) == {first["document_id"], second["document_id"]}
    assert changed_record["document_id"] not in matching["members"]


def test_git_safe_summary_rejects_private_fields_and_contains_only_aggregates():
    records = [_record("doc-a", "file-a", "page-a")]
    groups = build_exact_leakage_groups(records)
    split = deterministic_group_split(
        records=records, groups=groups, dataset_version="corpus-test", seed="seed"
    )
    summary = build_git_safe_summary(records, groups, split)
    rendered = render_git_safe_summary(summary)
    assert "doc-a" not in rendered
    assert "file-a" not in rendered
    assert "page-a" not in rendered
    assert summary["approximate_similarity_used_for_split"] is False
    assert summary["unique_exact_files"] == 1
    assert summary["unique_exact_visual_pages"] == 1

    with pytest.raises(ValueError, match="forbidden field"):
        assert_git_safe_summary({"private_filename": "redacted.pdf"})
    with pytest.raises(ValueError, match="path-like"):
        assert_git_safe_summary({"note": "C:/private/file.pdf"})


def test_inventory_snapshot_is_deterministic_and_immutable(tmp_path: Path):
    project, source, runtime = _roots(tmp_path)
    Image.new("RGB", (8, 8), color="white").save(source / "sample.png")
    first = inventory_local_corpus(
        project_root=project,
        source_root=source,
        runtime_root=runtime,
        split_seed="fixed",
    )
    second = inventory_local_corpus(
        project_root=project,
        source_root=source,
        runtime_root=runtime,
        split_seed="fixed",
    )
    assert first.dataset_version == second.dataset_version
    assert first.git_safe_summary == second.git_safe_summary


def test_posted_history_is_ground_truth_only_with_exact_source_corroboration(
    tmp_path: Path, monkeypatch,
):
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, StreamObject
    from webapp.backend.services import gl_catalog, resman_context_data

    project, source, runtime = _roots(tmp_path)
    pdf_path = source / "private.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    resources = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})
    })
    stream = StreamObject()
    stream._data = (
        b"BT /F1 12 Tf 72 720 Td "
        b"(Example Supplier Invoice #ABC-123 Invoice Date 01/02/2026 Total $125.00) Tj ET"
    )
    page[NameObject("/Resources")] = resources
    page[NameObject("/Contents")] = writer._add_object(stream)
    with pdf_path.open("wb") as handle:
        writer.write(handle)
    inventory_result = inventory_local_corpus(
        project_root=project, source_root=source, runtime_root=runtime,
    )
    _version, catalog = gl_catalog.load_gl_catalog()
    payable_code = next(code for code, item in catalog.items() if item.payable)
    history = [{
        "invoice_occurrence_id": "occ-private",
        "allocation_index": 1,
        "vendor_name": "Example Supplier",
        "invoice_number": "ABC-123",
        "invoice_date": "01/02/2026",
        "accounting_date": "01/02/2026",
        "due_date": None,
        "invoice_description": "private",
        "invoice_total": "125.00",
        "po_number": None,
        "batch": None,
        "property_code": "PRIVATE-PROPERTY",
        "gl_code": payable_code,
        "allocation_description": "private",
        "allocation_amount": "125.00",
        "allocation_count": 1,
        "invoice_reconciliation_status": "reconciled",
        "notes": None,
    }]
    monkeypatch.setattr(
        resman_context_data, "list_all_effective_records", lambda *_args: history,
    )
    monkeypatch.setattr(
        resman_context_data, "dataset_status",
        lambda *_args: SimpleNamespace(
            current_snapshot=SimpleNamespace(snapshot_id="posted-snapshot")
        ),
    )
    result = classify_ground_truth_eligibility(
        inventory_snapshot_root=inventory_result.snapshot_root,
        source_root=source,
        experiment_runtime_root=runtime,
        historical_tenant_id="private-test",
    )
    assert result.git_safe_summary["defensible_posted_ground_truth_documents"] == 1
    private_row = json.loads(result.private_report_path.read_text(encoding="utf-8"))
    assert private_row["eligibility_class"] == "accepted_posted_resman_ground_truth"
    assert private_row["ground_truth"]["expected_gl"] == payable_code
    assert private_row["ground_truth"]["match_evidence"]["invoice_total_visible"] is True
    assert result.git_safe_summary["prior_ai_outputs_used_as_truth"] == 0


def test_eligibility_rejects_source_mutated_after_inventory(tmp_path: Path, monkeypatch):
    from webapp.backend.services import resman_context_data

    project, source, runtime = _roots(tmp_path)
    target = source / "observed.png"
    Image.new("RGB", (12, 12), color="white").save(target)
    inventory_result = inventory_local_corpus(
        project_root=project, source_root=source, runtime_root=runtime,
    )
    Image.new("RGB", (12, 12), color="black").save(target)
    monkeypatch.setattr(resman_context_data, "list_all_effective_records", lambda *_: [])
    monkeypatch.setattr(
        resman_context_data, "dataset_status",
        lambda *_: SimpleNamespace(current_snapshot=None),
    )
    with pytest.raises(experiment_module.InventorySourceChangedError, match="changed"):
        classify_ground_truth_eligibility(
            inventory_snapshot_root=inventory_result.snapshot_root,
            source_root=source,
            experiment_runtime_root=runtime,
            historical_tenant_id="private-test",
        )


def test_runtime_ignore_check_obeys_git_negation_rules(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    (project / ".gitignore").write_text(
        "/tmp/\n!/tmp/\n!/tmp/document-learning/\n", encoding="utf-8",
    )
    runtime = project / "tmp" / "document-learning"
    runtime.mkdir(parents=True)
    with pytest.raises(ExperimentPathError, match="authoritatively ignored"):
        experiment_module._assert_runtime_git_ignored(project, runtime)


def test_phase_a_split_is_five_way_deterministic_and_hides_holdout_answers(tmp_path: Path):
    project, _source, runtime = _roots(tmp_path)
    snapshot = runtime / "snapshots" / "corpus-test"
    snapshot.mkdir(parents=True)
    records = []
    labels = []
    for family in range(5):
        for index in range(4):
            document_id = f"doc-{family}-{index}"
            records.append({
                "document_id": document_id,
                "content_sha256": f"file-{family}-{index}",
                "exact_visual_page_hashes": [f"page-{family}-{index}"],
                "invoice_identity_fingerprints": [f"invoice-{family}-{index}"],
                "probable_media_kind": "digital" if index % 2 else "scanned",
                "deterministic_parser_status": "active" if family == 0 else "not_detected",
                "page_count": 1,
                "appears_multi_invoice": False,
                "layout_family_fingerprints": [f"layout-{family}"],
                "metadata_family_fingerprint": f"metadata-{family}",
            })
            labels.append({
                "document_id": document_id,
                "eligibility_class": "accepted_posted_resman_ground_truth",
                "ground_truth": {
                    "history_occurrence_id": f"occ-{family}-{index}",
                    "expected_gl": f"GL-{family}",
                    "expected_property": "PROPERTY",
                    "canonical_accounting_family": f"accounting-family-{family}",
                    "vendor_family_fingerprint": f"vendor-{family}-{index % 2}",
                    "property_family_fingerprint": "property-one",
                    "invoice_identity_fingerprint": f"invoice-{family}-{index}",
                },
            })
    (snapshot / "inventory.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in records), encoding="utf-8",
    )
    eligibility = runtime / "eligibility" / "eligibility.jsonl"
    eligibility.parent.mkdir(parents=True)
    eligibility.write_text(
        "".join(json.dumps(row) + "\n" for row in labels), encoding="utf-8",
    )
    first = create_phase_a_split(
        inventory_snapshot_root=snapshot,
        eligibility_path=eligibility,
        experiment_runtime_root=runtime,
        seed="fixed-seed",
        maximum_unique_invoices=20,
    )
    second = create_phase_a_split(
        inventory_snapshot_root=snapshot,
        eligibility_path=eligibility,
        experiment_runtime_root=runtime,
        seed="fixed-seed",
        maximum_unique_invoices=20,
    )
    assert first.split_sha256 == second.split_sha256
    assert first.git_safe_summary["cohort_counts"] == {
        "benchmark_only": 2,
        "rule_simulation": 2,
        "similar_holdout": 4,
        "training": 8,
        "unrelated_holdout": 4,
    }
    manifest_text = (first.split_root / "split_manifest.json").read_text(encoding="utf-8")
    assert "expected_gl" not in manifest_text
    assert "ground_truth" not in manifest_text
    assert len((first.split_root / "scopes" / "training_labels.jsonl").read_text().splitlines()) == 8
    assert len((first.split_root / "hidden" / "holdout_labels.jsonl").read_text().splitlines()) == 8

    sample = create_phase_a_calibration_sample(
        inventory_snapshot_root=snapshot,
        split_root=first.split_root,
        experiment_runtime_root=runtime,
        seed="fixed-seed",
        maximum_documents=20,
    )
    sample_manifest = json.loads(sample.manifest_path.read_text(encoding="utf-8"))
    assert len(sample_manifest["assignments"]) == 20
    assert sample_manifest["answers_embedded"] is False
    assert "expected_gl" not in sample.manifest_path.read_text(encoding="utf-8")
