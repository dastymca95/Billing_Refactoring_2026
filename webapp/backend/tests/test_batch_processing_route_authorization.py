from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from webapp.backend.services import (
    ai_invoice_processor,
    batch_processor,
    batch_store,
    processing_route_policy,
)


@dataclass
class FakeResult:
    summary: dict[str, Any] = field(default_factory=lambda: {"files_processed": 1})
    invoices: list[dict[str, Any]] = field(default_factory=list)
    manual_review_rows: list[dict[str, Any]] = field(default_factory=list)
    unsupported_files: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)


@pytest.fixture()
def isolated_batch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    batch_id = "batch_20260717_130000_001"
    root = tmp_path / batch_id
    for name in ("input", "processed", "export", "logs", "manual_review"):
        (root / name).mkdir(parents=True, exist_ok=True)
    source = root / "input" / "numeric-invoice.pdf"
    source.write_bytes(b"test")

    monkeypatch.setattr(batch_store, "get_batch_dir", lambda value: root)
    monkeypatch.setattr(batch_store, "get_input_dir", lambda value: root / "input")
    monkeypatch.setattr(batch_store, "get_processed_dir", lambda value: root / "processed")
    monkeypatch.setattr(
        batch_store,
        "list_files_in_batch",
        lambda value: [source],
    )
    return batch_id, source


def known_detection() -> dict[str, Any]:
    return {
        "vendor_key": "registered_vendor",
        "confidence": 0.99,
        "reason": "test registered identity",
        "supported_in_phase_1": True,
        "processing_mode": "deterministic",
    }


def install_processor(monkeypatch: pytest.MonkeyPatch, process):
    monkeypatch.setitem(
        batch_processor._PROCESSOR_LOADERS,
        "registered_vendor",
        (lambda: SimpleNamespace(process=process), "process"),
    )


def forbid_ai(monkeypatch: pytest.MonkeyPatch):
    def fail(**_kwargs):
        raise AssertionError("AI provider path must not be reached")

    monkeypatch.setattr(ai_invoice_processor, "process_ai_vendor_files", fail)


def test_cost_safe_registered_processor_makes_zero_ai_calls_on_success(
    isolated_batch, monkeypatch: pytest.MonkeyPatch,
):
    batch_id, _source = isolated_batch
    contexts: list[dict[str, Any]] = []

    def process(**kwargs):
        contexts.append(kwargs["run_context"])
        return FakeResult(invoices=[{"source_file": "numeric-invoice.pdf", "rows": []}])

    install_processor(monkeypatch, process)
    monkeypatch.setattr(batch_processor, "detect_vendor_for_file", lambda _path: known_detection())
    forbid_ai(monkeypatch)

    result = batch_processor.process_batch(batch_id)

    assert len(result["all_invoices"]) == 1
    assert contexts[0]["ai_fallback_enabled"] is False
    route = result["processing_routes"]["numeric-invoice.pdf"]
    assert route["effective_route"] == "deterministic"
    assert route["ai_fallback_authorized"] is False
    assert route["reason_code"] == "cost_safe_deterministic_default"


def test_cost_safe_zero_output_is_reviewable_but_never_silently_calls_ai(
    isolated_batch, monkeypatch: pytest.MonkeyPatch,
):
    batch_id, _source = isolated_batch
    install_processor(monkeypatch, lambda **_kwargs: FakeResult())
    monkeypatch.setattr(batch_processor, "detect_vendor_for_file", lambda _path: known_detection())
    forbid_ai(monkeypatch)

    result = batch_processor.process_batch(batch_id)

    assert result["all_invoices"] == []
    assert result["unsupported_files"][0]["reason"] == (
        "deterministic_zero_output_ai_not_authorized"
    )
    assert result["detection"]["numeric-invoice.pdf"]["fallback_processing_mode"] == (
        "blocked_by_route_policy"
    )


def test_explicit_ai_fallback_authorization_is_scoped_and_audited(
    isolated_batch, monkeypatch: pytest.MonkeyPatch,
):
    batch_id, _source = isolated_batch
    install_processor(monkeypatch, lambda **_kwargs: FakeResult())
    monkeypatch.setattr(batch_processor, "detect_vendor_for_file", lambda _path: known_detection())
    processing_route_policy.set_document_mode(
        batch_id,
        "numeric-invoice.pdf",
        "ai_fallback_allowed",
        actor="test_operator",
    )
    calls: list[list[str]] = []

    def ai_process(**kwargs):
        calls.append([item.name for item in kwargs["files"]])
        return {
            "vendor_key": ai_invoice_processor.AI_VENDOR_KEY,
            "summary": {"processing_mode": "ai_assisted", "files_processed": 1},
            "invoices": [{"source_file": "numeric-invoice.pdf", "rows": []}],
            "manual_review_rows": [],
            "unsupported_files": [],
        }

    monkeypatch.setattr(ai_invoice_processor, "process_ai_vendor_files", ai_process)

    result = batch_processor.process_batch(batch_id)

    assert calls == [["numeric-invoice.pdf"]]
    route = result["processing_routes"]["numeric-invoice.pdf"]
    assert route["effective_route"] == "deterministic"
    assert route["ai_fallback_authorized"] is True
    assert route["inherited_from"] == "document"


def test_deterministic_only_unknown_document_blocks_without_ai(
    isolated_batch, monkeypatch: pytest.MonkeyPatch,
):
    batch_id, _source = isolated_batch
    processing_route_policy.set_document_mode(
        batch_id,
        "numeric-invoice.pdf",
        "deterministic_only",
        actor="test_operator",
    )
    monkeypatch.setattr(
        batch_processor,
        "detect_vendor_for_file",
        lambda _path: {
            "vendor_key": "unknown",
            "confidence": 0.0,
            "reason": "no_detector_claimed_this_file",
            "supported_in_phase_1": False,
            "processing_mode": "ai_assisted",
        },
    )
    forbid_ai(monkeypatch)

    result = batch_processor.process_batch(batch_id)

    assert result["all_invoices"] == []
    assert result["unsupported_files"][0]["reason"] == (
        "deterministic_processor_unavailable"
    )
    assert result["processing_routes"]["numeric-invoice.pdf"]["effective_route"] == "blocked"
