from __future__ import annotations
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from webapp.backend.services import processing_route_policy as routes


@pytest.fixture()
def isolated_batch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path]:
    batch_id = "batch_20260717_120000_001"
    batch_dir = tmp_path / batch_id
    batch_dir.mkdir()
    monkeypatch.setattr(routes.batch_store, "get_batch_dir", lambda value: batch_dir if value == batch_id else (_ for _ in ()).throw(FileNotFoundError(value)))
    return batch_id, batch_dir


def test_absent_policy_resolves_to_cost_safe_default_without_writing(
    isolated_batch: tuple[str, Path],
) -> None:
    batch_id, batch_dir = isolated_batch

    resolution = routes.resolve_requested_mode(batch_id, filename="invoice.pdf", page=1)

    assert resolution.requested_mode == routes.ProcessingRouteMode.AUTO_COST_SAFE
    assert resolution.inherited_from == "default"
    assert resolution.configured_by is None
    assert resolution.contract_version == "processing-route-policy/1.0"
    assert not (batch_dir / routes.STORE_DIRECTORY / routes.STORE_FILENAME).exists()


def test_scope_precedence_is_page_then_document_then_batch_then_default(
    isolated_batch: tuple[str, Path],
) -> None:
    batch_id, _ = isolated_batch
    routes.set_batch_mode(batch_id, "ai_fallback_allowed", actor="owner")
    routes.set_document_mode(
        batch_id, "Invoice.PDF", "deterministic_only", actor="document-reviewer",
    )
    routes.set_page_mode(
        batch_id, "invoice.pdf", 2, "auto_cost_safe", actor="page-reviewer",
    )

    page = routes.resolve_requested_mode(batch_id, filename="INVOICE.pdf", page=2)
    document = routes.resolve_requested_mode(batch_id, filename="invoice.pdf", page=3)
    batch = routes.resolve_requested_mode(batch_id, filename="other.pdf", page=1)

    assert (page.requested_mode, page.inherited_from, page.configured_by) == (
        routes.ProcessingRouteMode.AUTO_COST_SAFE, "page", "page-reviewer",
    )
    assert (document.requested_mode, document.inherited_from) == (
        routes.ProcessingRouteMode.DETERMINISTIC_ONLY, "document",
    )
    assert (batch.requested_mode, batch.inherited_from) == (
        routes.ProcessingRouteMode.AI_FALLBACK_ALLOWED, "batch",
    )


def test_document_resolution_does_not_consider_page_overrides(
    isolated_batch: tuple[str, Path],
) -> None:
    batch_id, _ = isolated_batch
    routes.set_page_mode(batch_id, "invoice.pdf", 1, "deterministic_only", actor="owner")

    resolution = routes.resolve_requested_mode(batch_id, filename="invoice.pdf")

    assert resolution.requested_mode == routes.ProcessingRouteMode.AUTO_COST_SAFE
    assert resolution.inherited_from == "default"


def test_all_three_modes_persist_with_strict_versioned_contract(
    isolated_batch: tuple[str, Path],
) -> None:
    batch_id, batch_dir = isolated_batch
    routes.set_batch_mode(batch_id, routes.ProcessingRouteMode.AUTO_COST_SAFE, actor="owner")
    routes.set_document_mode(batch_id, "a.pdf", "deterministic_only", actor="owner")
    routes.set_page_mode(batch_id, "a.pdf", 1, "ai_fallback_allowed", actor="owner")

    path = batch_dir / routes.STORE_DIRECTORY / routes.STORE_FILENAME
    raw = json.loads(path.read_text(encoding="utf-8"))
    reloaded = routes.get_policy(batch_id)

    assert raw["contract_version"] == routes.CONTRACT_VERSION
    assert raw["batch_id"] == batch_id
    assert reloaded.batch_override is not None
    assert reloaded.batch_override.requested_mode == routes.ProcessingRouteMode.AUTO_COST_SAFE
    assert reloaded.document_overrides[0].requested_mode == routes.ProcessingRouteMode.DETERMINISTIC_ONLY
    assert reloaded.page_overrides[0].requested_mode == routes.ProcessingRouteMode.AI_FALLBACK_ALLOWED
    assert not list(path.parent.glob("*.tmp"))


def test_replacing_same_override_is_case_insensitive_and_audited(
    isolated_batch: tuple[str, Path],
) -> None:
    batch_id, _ = isolated_batch
    routes.set_document_mode(batch_id, "A.PDF", "auto_cost_safe", actor="first")
    policy = routes.set_document_mode(
        batch_id, "a.pdf", "deterministic_only", actor="second",
    )

    assert len(policy.document_overrides) == 1
    assert policy.document_overrides[0].filename == "a.pdf"
    assert policy.document_overrides[0].actor == "second"
    event = policy.audit[-1]
    assert event.action == "set"
    assert event.actor == "second"
    assert event.previous_mode == routes.ProcessingRouteMode.AUTO_COST_SAFE
    assert event.requested_mode == routes.ProcessingRouteMode.DETERMINISTIC_ONLY
    assert event.occurred_at.tzinfo is not None


def test_apply_bulk_mode_resets_every_exception_atomically(
    isolated_batch: tuple[str, Path],
) -> None:
    batch_id, _ = isolated_batch
    routes.set_batch_mode(batch_id, "auto_cost_safe", actor="owner")
    routes.set_document_mode(batch_id, "a.pdf", "deterministic_only", actor="owner")
    routes.set_document_mode(batch_id, "b.pdf", "ai_fallback_allowed", actor="owner")
    routes.set_page_mode(batch_id, "a.pdf", 1, "ai_fallback_allowed", actor="owner")
    routes.set_page_mode(batch_id, "b.pdf", 3, "deterministic_only", actor="owner")

    policy = routes.apply_bulk_mode(batch_id, "deterministic_only", actor="bulk-owner")

    assert policy.document_overrides == []
    assert policy.page_overrides == []
    assert policy.batch_override is not None
    assert policy.batch_override.requested_mode == routes.ProcessingRouteMode.DETERMINISTIC_ONLY
    assert routes.resolve_requested_mode(
        batch_id, filename="a.pdf", page=1,
    ).inherited_from == "batch"
    event = policy.audit[-1]
    assert event.action == "apply_bulk"
    assert event.cleared_document_overrides == 2
    assert event.cleared_page_overrides == 2


def test_batch_set_without_reset_preserves_exceptions(
    isolated_batch: tuple[str, Path],
) -> None:
    batch_id, _ = isolated_batch
    routes.set_document_mode(batch_id, "a.pdf", "deterministic_only", actor="owner")

    policy = routes.set_batch_mode(
        batch_id, "ai_fallback_allowed", actor="owner", reset_exceptions=False,
    )

    assert len(policy.document_overrides) == 1
    assert routes.resolve_requested_mode(
        batch_id, filename="a.pdf",
    ).requested_mode == routes.ProcessingRouteMode.DETERMINISTIC_ONLY


def test_reset_exceptions_preserves_batch_override_and_records_counts(
    isolated_batch: tuple[str, Path],
) -> None:
    batch_id, _ = isolated_batch
    routes.set_batch_mode(batch_id, "ai_fallback_allowed", actor="owner")
    routes.set_document_mode(batch_id, "a.pdf", "deterministic_only", actor="owner")
    routes.set_page_mode(batch_id, "a.pdf", 1, "auto_cost_safe", actor="owner")

    policy = routes.reset_exceptions(batch_id, actor="owner")

    assert policy.batch_override is not None
    assert policy.batch_override.requested_mode == routes.ProcessingRouteMode.AI_FALLBACK_ALLOWED
    assert not policy.document_overrides
    assert not policy.page_overrides
    assert policy.audit[-1].action == "reset_exceptions"
    assert policy.audit[-1].cleared_document_overrides == 1
    assert policy.audit[-1].cleared_page_overrides == 1


def test_clearing_overrides_reveals_next_precedence_level(
    isolated_batch: tuple[str, Path],
) -> None:
    batch_id, _ = isolated_batch
    routes.set_batch_mode(batch_id, "auto_cost_safe", actor="owner")
    routes.set_document_mode(batch_id, "a.pdf", "deterministic_only", actor="owner")
    routes.set_page_mode(batch_id, "a.pdf", 1, "ai_fallback_allowed", actor="owner")

    routes.clear_route_mode(
        batch_id, scope="page", filename="a.pdf", page=1, actor="owner",
    )
    after_page = routes.resolve_requested_mode(batch_id, filename="a.pdf", page=1)
    routes.clear_route_mode(
        batch_id, scope="document", filename="a.pdf", actor="owner",
    )
    after_document = routes.resolve_requested_mode(batch_id, filename="a.pdf", page=1)
    policy = routes.clear_route_mode(batch_id, scope="batch", actor="owner")
    after_batch = routes.resolve_requested_mode(batch_id, filename="a.pdf", page=1)

    assert (after_page.requested_mode, after_page.inherited_from) == (
        routes.ProcessingRouteMode.DETERMINISTIC_ONLY, "document",
    )
    assert (after_document.requested_mode, after_document.inherited_from) == (
        routes.ProcessingRouteMode.AUTO_COST_SAFE, "batch",
    )
    assert (after_batch.requested_mode, after_batch.inherited_from) == (
        routes.ProcessingRouteMode.AUTO_COST_SAFE, "default",
    )
    assert [event.action for event in policy.audit[-3:]] == ["clear", "clear", "clear"]


@pytest.mark.parametrize(
    ("scope", "filename", "page"),
    [
        ("batch", "a.pdf", None),
        ("batch", None, 1),
        ("document", None, None),
        ("document", "a.pdf", 1),
        ("page", None, 1),
        ("page", "a.pdf", None),
        ("page", "a.pdf", 0),
        ("page", "a.pdf", True),
    ],
)
def test_scope_contract_rejects_ambiguous_identifiers(
    isolated_batch: tuple[str, Path], scope: str, filename: str | None, page: int | None,
) -> None:
    batch_id, _ = isolated_batch
    with pytest.raises(ValueError):
        routes.set_route_mode(
            batch_id, scope=scope, mode="auto_cost_safe", actor="owner",
            filename=filename, page=page,
        )


@pytest.mark.parametrize(
    "filename",
    ["../invoice.pdf", "folder/invoice.pdf", r"C:\\private\\invoice.pdf", "..", ""],
)
def test_private_paths_cannot_be_serialized_as_filenames(
    isolated_batch: tuple[str, Path], filename: str,
) -> None:
    batch_id, _ = isolated_batch
    with pytest.raises(ValueError, match="filename|paths"):
        routes.set_document_mode(batch_id, filename, "auto_cost_safe", actor="owner")


@pytest.mark.parametrize("mode", ["ai", "deterministic", "", "AUTO_COST_SAFE"])
def test_unknown_or_ambiguous_modes_never_fall_back_silently(
    isolated_batch: tuple[str, Path], mode: str,
) -> None:
    batch_id, _ = isolated_batch
    with pytest.raises(ValueError):
        routes.set_batch_mode(batch_id, mode, actor="owner")


def test_non_batch_scope_cannot_reset_other_exceptions(
    isolated_batch: tuple[str, Path],
) -> None:
    batch_id, _ = isolated_batch
    with pytest.raises(ValueError, match="batch scope"):
        routes.set_route_mode(
            batch_id, scope="document", filename="a.pdf", mode="auto_cost_safe",
            actor="owner", reset_exceptions=True,
        )


def test_corrupt_or_wrong_version_store_fails_closed(
    isolated_batch: tuple[str, Path],
) -> None:
    batch_id, batch_dir = isolated_batch
    path = batch_dir / routes.STORE_DIRECTORY / routes.STORE_FILENAME
    path.parent.mkdir()
    path.write_text('{"contract_version":"processing-route-policy/999"}', encoding="utf-8")

    with pytest.raises(routes.ProcessingRoutePolicyError):
        routes.resolve_requested_mode(batch_id, filename="a.pdf")


def test_policy_for_another_batch_is_rejected(
    isolated_batch: tuple[str, Path],
) -> None:
    batch_id, batch_dir = isolated_batch
    now = "2026-07-17T12:00:00Z"
    path = batch_dir / routes.STORE_DIRECTORY / routes.STORE_FILENAME
    path.parent.mkdir()
    path.write_text(json.dumps({
        "contract_version": routes.CONTRACT_VERSION,
        "batch_id": "batch_20260717_120000_999",
        "batch_override": None,
        "document_overrides": [],
        "page_overrides": [],
        "audit": [],
        "created_at": now,
        "updated_at": now,
    }), encoding="utf-8")

    with pytest.raises(routes.ProcessingRoutePolicyError, match="another batch"):
        routes.get_policy(batch_id)


def test_concurrent_document_updates_remain_valid_and_complete(
    isolated_batch: tuple[str, Path],
) -> None:
    batch_id, batch_dir = isolated_batch

    def update(index: int) -> None:
        routes.set_document_mode(
            batch_id, f"invoice-{index}.pdf", "deterministic_only", actor=f"operator-{index}",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(update, range(24)))

    policy = routes.get_policy(batch_id)
    raw = json.loads(
        (batch_dir / routes.STORE_DIRECTORY / routes.STORE_FILENAME).read_text(encoding="utf-8")
    )
    assert len(policy.document_overrides) == 24
    assert len(policy.audit) == 24
    assert len(raw["document_overrides"]) == 24


def test_audit_actor_is_required_and_control_characters_are_rejected(
    isolated_batch: tuple[str, Path],
) -> None:
    batch_id, _ = isolated_batch
    for actor in ("", "  ", "owner\nsecret"):
        with pytest.raises(ValueError, match="actor"):
            routes.set_batch_mode(batch_id, "auto_cost_safe", actor=actor)
