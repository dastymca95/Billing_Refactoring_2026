"""Smoke-test Phase AI-5 vision assist without external provider calls.

This script mutates in-process settings only. It does not read API keys,
does not call external AI services, does not export, and does not touch source
training files.
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp.backend import settings  # noqa: E402
from webapp.backend.main import app  # noqa: E402
from webapp.backend.services import ai_invoice_processor  # noqa: E402


def _set_ai(
    *,
    enabled: bool,
    provider: str = "",
    model: str = "",
    vision_enabled: bool = False,
    vision_model: str = "",
) -> None:
    settings.AI_ASSIST_ENABLED = enabled
    settings.AI_PROVIDER = provider
    settings.AI_MODEL = model
    settings.AI_API_KEY = "test-key"
    settings.AI_BASE_URL = "http://example.invalid/v1"
    settings.AI_VISION_ENABLED = vision_enabled
    settings.AI_VISION_MODEL = vision_model
    settings.AI_VISION_MAX_PAGES = 2
    settings.AI_VISION_MAX_IMAGE_WIDTH = 800
    settings.AI_VISION_MODE = "fallback_only"
    settings.AI_MOCK_MODE = ""


def main() -> int:
    client = TestClient(app)
    created_batches: list[str] = []

    try:
        _set_ai(enabled=False)
        off = client.get("/api/ai/status").json()
        assert off["enabled"] is False
        assert off["vision_enabled"] is False

        _set_ai(enabled=True, provider="openai_compatible", model="text-model", vision_enabled=True)
        no_vision = client.get("/api/ai/status").json()
        assert no_vision["configured"] is True
        assert no_vision["supports_vision"] is False
        assert no_vision["vision_enabled"] is False

        _set_ai(
            enabled=True,
            provider="openai_compatible",
            model="text-model",
            vision_enabled=True,
            vision_model="vision-model",
        )
        configured_vision = client.get("/api/ai/status").json()
        assert configured_vision["configured"] is True
        assert configured_vision["supports_vision"] is True
        assert configured_vision["vision_enabled"] is True
        assert configured_vision["vision_model"] == "vision-model"

        _set_ai(enabled=True, provider="mock", model="mock-invoice-v1", vision_enabled=False)
        disabled_batch = client.post("/api/batches", json={"batch_name": "AI vision disabled smoke"}).json()
        created_batches.append(disabled_batch["batch_id"])
        disabled = client.post(
            f"/api/batches/{disabled_batch['batch_id']}/ai-invoice/vision-assist",
            json={"filename": "mock.pdf", "dry_run": True},
        )
        assert disabled.status_code == 400
        assert "Vision assist is not enabled" in disabled.text

        _set_ai(
            enabled=True,
            provider="mock",
            model="mock-invoice-v1",
            vision_enabled=True,
            vision_model="mock-vision-v1",
        )
        batch = client.post("/api/batches", json={"batch_name": "AI vision smoke"}).json()
        batch_id = batch["batch_id"]
        created_batches.append(batch_id)
        res = client.post(
            f"/api/batches/{batch_id}/ai-invoice/vision-assist",
            json={
                "filename": "mock-variable-invoice.pdf",
                "page_numbers": [1, 2, 3],
                "vendor_hint": "HD Supply",
                "dry_run": True,
            },
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["vision_enabled"] is True
        assert len(body["trace_regions"]) >= 1
        assert body["validation"]["valid"] is True

        settings.AI_MOCK_MODE = "malformed_json"
        malformed = client.post(
            f"/api/batches/{batch_id}/ai-invoice/vision-assist",
            json={"filename": "mock-variable-invoice.pdf", "dry_run": True},
        )
        assert malformed.status_code == 422
        settings.AI_MOCK_MODE = ""

        merged = ai_invoice_processor.merge_text_and_vision_results(
            {
                "vendor_name": "Vendor A",
                "invoice_number": "100",
                "invoice_date": "05/01/2026",
                "due_date": "06/01/2026",
                "total_amount": 10.00,
                "confidence": 0.80,
                "manual_review_reasons": [],
                "manual_review_codes": [],
                "manual_review_issues": [],
                "validation_summary": {},
            },
            {
                "vendor_name": "Vendor A",
                "invoice_number": "100",
                "invoice_date": "05/01/2026",
                "due_date": "06/01/2026",
                "total_amount": 10.00,
                "confidence": 0.82,
                "vision_candidates": [],
                "manual_review_reasons": [],
                "manual_review_codes": [],
                "manual_review_issues": [],
                "validation_summary": {},
            },
        )
        assert merged["confidence"] >= 0.90

        conflict = ai_invoice_processor.merge_text_and_vision_results(
            merged,
            {**merged, "total_amount": 11.00, "vision_candidates": []},
        )
        assert "ai_text_vision_conflict" in conflict["manual_review_codes"]

        print("AI vision assist smoke OK.")
        return 0
    finally:
        for batch_id in created_batches:
            try:
                client.delete(f"/api/batches/{batch_id}")
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
