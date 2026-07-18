from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from webapp.backend.api import processing_routes as route_api
from webapp.backend.main import create_app


@pytest.fixture()
def route_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, str, Path]:
    batch_id = "batch_20260717_160000_001"
    batch_dir = tmp_path / batch_id
    input_dir = batch_dir / "input"
    input_dir.mkdir(parents=True)
    (input_dir / "Known Utility.pdf").write_bytes(b"not parsed by this API test")
    (input_dir / "unknown.pdf").write_bytes(b"not parsed by this API test")

    real_get_batch_dir = route_api.batch_store.get_batch_dir

    def get_batch_dir(value: str) -> Path:
        if value == batch_id:
            return batch_dir
        return real_get_batch_dir(value)

    monkeypatch.setattr(route_api.batch_store, "get_batch_dir", get_batch_dir)

    def detect(path: Path) -> dict:
        if path.name == "Known Utility.pdf":
            return {
                "vendor_key": "known_utility",
                "confidence": 0.97,
                "reason": "test detector matched public-safe content",
            }
        return {
            "vendor_key": "unknown",
            "confidence": 0.0,
            "reason": "no_detector_claimed_this_file",
        }

    # The endpoint is a read-only discovery surface.  No OCR or AI provider is
    # needed (or permitted) in these tests.
    monkeypatch.setattr(route_api, "detect_vendor_for_file", detect)
    monkeypatch.setattr(
        route_api.batch_processor,
        "_PROCESSOR_LOADERS",
        {"known_utility": (lambda: None, "process_known_utility_batch")},
    )
    return TestClient(create_app()), batch_id, batch_dir


def test_get_returns_stable_contract_and_backend_decisions_without_paths(
    route_client: tuple[TestClient, str, Path],
) -> None:
    client, batch_id, batch_dir = route_client

    first = client.get(f"/api/batches/{batch_id}/processing-routes")
    second = client.get(f"/api/batches/{batch_id}/processing-routes")

    assert first.status_code == 200
    payload = first.json()
    assert payload["contract_version"] == "processing-route-api/1.0"
    assert payload["policy_version"] == second.json()["policy_version"]
    assert payload["batch"]["resolution"]["requested_mode"] == "auto_cost_safe"
    assert payload["batch"]["resolution"]["inherited_from"] == "default"
    assert payload["pages"] == []
    assert payload["audit"] == []

    documents = {item["filename"]: item for item in payload["documents"]}
    known = documents["Known Utility.pdf"]
    assert known["detection"] == {
        "vendor_key": "known_utility",
        "confidence": 0.97,
        "reason": "test detector matched public-safe content",
    }
    assert known["decision"]["effective_route"] == "deterministic"
    assert known["decision"]["ai_fallback_authorized"] is False
    assert known["decision"]["processor_id"] == (
        "known_utility.process_known_utility_batch"
    )
    assert documents["unknown.pdf"]["decision"]["effective_route"] == "ai"

    serialized = json.dumps(payload)
    assert str(batch_dir) not in serialized
    assert str(batch_dir.parent) not in serialized
    assert "request headers" not in serialized.lower()


def test_patch_supports_scope_precedence_bulk_reset_and_page_decisions(
    route_client: tuple[TestClient, str, Path],
) -> None:
    client, batch_id, _ = route_client
    endpoint = f"/api/batches/{batch_id}/processing-routes"
    version = client.get(endpoint).json()["policy_version"]

    document = client.patch(endpoint, json={
        "scope": "document",
        "mode": "deterministic_only",
        "filename": "unknown.pdf",
        "expected_policy_version": version,
    })
    assert document.status_code == 200
    assert document.json()["policy_version"] != version
    assert next(
        item for item in document.json()["documents"]
        if item["filename"] == "unknown.pdf"
    )["decision"]["effective_route"] == "blocked"
    assert document.json()["audit"][-1]["actor"] == "local_operator"

    page = client.patch(endpoint, json={
        "scope": "page",
        "mode": "ai_fallback_allowed",
        "filename": "Known Utility.pdf",
        "page": 2,
        "expected_policy_version": document.json()["policy_version"],
        "actor": "page_reviewer",
    })
    assert page.status_code == 200
    assert page.json()["pages"][0]["page"] == 2
    assert page.json()["pages"][0]["decision"]["inherited_from"] == "page"
    assert page.json()["pages"][0]["decision"]["ai_fallback_authorized"] is True

    bulk = client.patch(endpoint, json={
        "scope": "batch",
        "mode": "deterministic_only",
        "reset_exceptions": True,
        "expected_policy_version": page.json()["policy_version"],
        "actor": "batch_operator",
    })
    assert bulk.status_code == 200
    bulk_payload = bulk.json()
    assert bulk_payload["pages"] == []
    assert all(
        item["decision"]["inherited_from"] == "batch"
        for item in bulk_payload["documents"]
    )
    event = bulk_payload["audit"][-1]
    assert event["action"] == "apply_bulk"
    assert event["cleared_document_overrides"] == 1
    assert event["cleared_page_overrides"] == 1


def test_patch_clear_and_optimistic_conflict_are_auditable(
    route_client: tuple[TestClient, str, Path],
) -> None:
    client, batch_id, _ = route_client
    endpoint = f"/api/batches/{batch_id}/processing-routes"
    initial_version = client.get(endpoint).json()["policy_version"]
    set_response = client.patch(endpoint, json={
        "scope": "document",
        "mode": "ai_fallback_allowed",
        "filename": "Known Utility.pdf",
        "expected_policy_version": initial_version,
    })
    assert set_response.status_code == 200

    stale = client.patch(endpoint, json={
        "scope": "batch",
        "mode": "deterministic_only",
        "expected_policy_version": initial_version,
    })
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "processing_route_policy_version_conflict"

    latest_version = set_response.json()["policy_version"]
    cleared = client.patch(endpoint, json={
        "scope": "document",
        "mode": None,
        "filename": "known utility.PDF",
        "expected_policy_version": latest_version,
    })
    assert cleared.status_code == 200
    payload = cleared.json()
    known = next(
        item for item in payload["documents"]
        if item["filename"] == "Known Utility.pdf"
    )
    assert known["decision"]["inherited_from"] == "default"
    assert payload["audit"][-1]["action"] == "clear"
    assert payload["audit"][-1]["filename"] == "Known Utility.pdf"


@pytest.mark.parametrize(
    "patch",
    [
        {"scope": "document", "mode": "deterministic_only", "filename": "../secret.pdf"},
        {"scope": "document", "mode": "deterministic_only", "filename": r"C:\\private\\secret.pdf"},
        {"scope": "document", "mode": "deterministic_only", "filename": "absent.pdf"},
        {"scope": "batch", "mode": None, "reset_exceptions": True},
        {"scope": "page", "mode": "deterministic_only", "filename": "unknown.pdf"},
    ],
)
def test_patch_rejects_paths_missing_documents_and_ambiguous_scope(
    route_client: tuple[TestClient, str, Path],
    patch: dict,
) -> None:
    client, batch_id, _ = route_client

    response = client.patch(
        f"/api/batches/{batch_id}/processing-routes",
        json=patch,
    )

    assert response.status_code == 422


def test_missing_batch_is_404_and_does_not_create_policy(
    route_client: tuple[TestClient, str, Path],
) -> None:
    client, _, _ = route_client

    response = client.get(
        "/api/batches/batch_20260717_160000_999/processing-routes"
    )

    assert response.status_code == 404
