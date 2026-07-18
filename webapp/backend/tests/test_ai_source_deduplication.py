from pathlib import Path

from webapp.backend.services.ai_invoice_processor import (
    _attach_duplicate_source_provenance,
    _deduplicate_source_files,
)
from webapp.backend.services.batch_processor import _supported_file_count


def test_exact_duplicate_uploads_are_planned_once_and_remain_auditable(tmp_path: Path):
    first = tmp_path / "first-name.pdf"
    second = tmp_path / "second-name.pdf"
    distinct = tmp_path / "distinct.pdf"
    first.write_bytes(b"same-private-document")
    second.write_bytes(b"same-private-document")
    distinct.write_bytes(b"different-private-document")

    unique, aliases = _deduplicate_source_files([first, second, distinct])

    assert unique == [first, distinct]
    assert aliases == {"first-name.pdf": ["second-name.pdf"]}

    invoices = [{
        "source_file": "first-name.pdf",
        "debug_info": {},
        "rows": [{"_meta": {}}],
    }]
    _attach_duplicate_source_provenance(invoices, aliases)

    assert invoices[0]["debug_info"]["exact_duplicate_sources"] == ["second-name.pdf"]
    assert invoices[0]["rows"][0]["_meta"]["exact_duplicate_sources"] == ["second-name.pdf"]


def test_supported_file_metric_excludes_unique_failed_sources(tmp_path: Path):
    files = [tmp_path / name for name in ("one.pdf", "two.pdf", "three.pdf")]
    unsupported = [
        {"filename": "two.pdf", "failure_code": "provider_invalid_schema"},
        {"filename": "two.pdf", "failure_code": "provider_invalid_schema"},
    ]

    assert _supported_file_count(files, unsupported) == 2
