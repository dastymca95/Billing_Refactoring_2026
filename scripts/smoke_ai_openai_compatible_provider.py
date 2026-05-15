"""Phase AI-1.2 smoke tests for OpenAI-compatible provider wiring.

No real provider is called. The script monkeypatches urllib inside
``webapp.backend.services.ai_provider`` so config, strict JSON parsing, the
manual test endpoint, and safe error handling can be verified without API keys
or network traffic.
"""

from __future__ import annotations

import json
import sys
import urllib.error
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from webapp.backend import settings  # noqa: E402
from webapp.backend.main import app  # noqa: E402
from webapp.backend.services import ai_provider  # noqa: E402


client = TestClient(app)


def configure(
    *,
    enabled: bool = True,
    provider: str = "openai_compatible",
    base_url: str = "https://provider.example/v1",
    model: str = "test-model",
    api_key: str = "test-secret",
) -> None:
    settings.AI_ASSIST_ENABLED = enabled
    settings.AI_PROVIDER = provider if enabled else ""
    settings.AI_BASE_URL = base_url if enabled else ""
    settings.AI_MODEL = model if enabled else ""
    settings.AI_API_KEY = api_key if enabled else ""
    settings.AI_MAX_RESPONSE_TOKENS = 2048
    settings.AI_MAX_OUTPUT_CHARS = 20000
    settings.AI_MAX_TEXT_CHARS = 4000


def valid_invoice_payload() -> dict:
    return {
        "vendor_name": "HD Supply Facilities Maintenance, Ltd",
        "invoice_number": "HDS-2048",
        "invoice_date": "05/07/2026",
        "due_date": "06/06/2026",
        "bill_or_credit": "Bill",
        "account_number": "40293817",
        "service_address": "1726 Stone Street, Union City, TN 38261",
        "property_candidate": "1732-Hillwood Manor",
        "property_abbreviation": "1732-HMA",
        "invoice_description": "Maintenance supplies",
        "line_items": [
            {
                "description": "Kitchen faucet cartridge",
                "quantity": 2,
                "unit_price": 24.50,
                "amount": 49.00,
                "gl_account_candidate": "6615 Building Maintenance & Repairs - Minor",
                "expense_type": "Repairs and maintenance",
                "is_replacement_reserve": False,
                "confidence": 0.91,
                "reason": "Supplier item description is a maintenance repair part.",
            }
        ],
        "subtotal": 49.00,
        "tax_amount": 4.66,
        "shipping_amount": 0.00,
        "fees_amount": 0.00,
        "total_amount": 53.66,
        "confidence": 0.90,
        "warnings": [],
        "needs_manual_review": False,
    }


class FakeResponse:
    def __init__(self, body: str):
        self._body = body.encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _n: int = -1) -> bytes:
        return self._body


def provider_envelope(content: str) -> str:
    return json.dumps({"choices": [{"message": {"content": content}}]})


def with_fake_urlopen(fake: Callable) -> Callable:
    original = ai_provider.urllib.request.urlopen

    def run(fn: Callable[[], None]) -> None:
        ai_provider.urllib.request.urlopen = fake  # type: ignore[assignment]
        try:
            fn()
        finally:
            ai_provider.urllib.request.urlopen = original  # type: ignore[assignment]

    return run


def assert_disabled_and_missing_config() -> None:
    configure(enabled=False)
    status = client.get("/api/ai/status").json()
    assert status["enabled"] is False, status
    assert "AI_API_KEY" not in str(status)
    response = client.post(
        "/api/ai-invoice/test-extract",
        json={"vendor_hint": "HD Supply", "document_text": "Invoice text", "dry_run": True},
    )
    assert response.status_code == 400, response.text

    configure(api_key="")
    status = client.get("/api/ai/status").json()
    assert status["configured"] is False, status
    assert "AI_API_KEY" in status["message"], status
    assert "test-secret" not in str(status)

    configure(model="")
    status = client.get("/api/ai/status").json()
    assert status["configured"] is False, status
    assert "AI_MODEL" in status["message"], status

    configure(base_url="")
    status = client.get("/api/ai/status").json()
    assert status["configured"] is False, status
    assert "AI_BASE_URL" in status["message"], status
    print("Provider disabled/missing config checks: OK")


def assert_manual_test_endpoint_success() -> None:
    configure()
    payload = valid_invoice_payload()
    content = "```json\n" + json.dumps(payload) + "\n```"

    def fake(req, timeout=0):  # noqa: ANN001
        assert timeout == settings.AI_TIMEOUT_SECONDS
        assert req.full_url == "https://provider.example/v1/chat/completions"
        assert req.get_header("Authorization") == "Bearer test-secret"
        sent = json.loads(req.data.decode("utf-8"))
        assert sent["model"] == "test-model"
        assert sent["response_format"] == {"type": "json_object"}
        return FakeResponse(provider_envelope(content))

    def run() -> None:
        response = client.post(
            "/api/ai-invoice/test-extract",
            json={
                "vendor_hint": "HD Supply",
                "document_text": "HD Supply invoice HDS-2048 total 53.66",
                "dry_run": True,
            },
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["dry_run"] is True
        assert data["provider"] == "openai_compatible"
        assert data["model"] == "test-model"
        assert data["extraction"]["invoice_number"] == "HDS-2048"
        assert "test-secret" not in str(data)

    with_fake_urlopen(fake)(run)
    print("Manual test endpoint with fake provider: OK")


def assert_malformed_response_rejected() -> None:
    configure()

    def fake(_req, timeout=0):  # noqa: ANN001
        return FakeResponse(provider_envelope("```json\nnot json\n```"))

    def run() -> None:
        try:
            ai_provider.extract_invoice_structured(
                vendor_hint="HD Supply",
                document_text="Invoice text",
                page_images_or_refs=[],
                template_schema={"columns": []},
                property_reference=[],
                gl_reference=[],
                vendor_reference=[],
            )
        except ai_provider.AIProviderInvalidJSON:
            return
        raise AssertionError("Malformed provider JSON was accepted")

    with_fake_urlopen(fake)(run)
    print("Malformed provider JSON rejected: OK")


def assert_lowes_missing_confidence_is_derived() -> None:
    configure()
    payload = valid_invoice_payload()
    payload.update(
        {
            "vendor_name": "Lowe's Home Improvement",
            "invoice_number": "LOW-778812",
            "invoice_date": "05/08/2026",
            "due_date": "",
            "service_address": "",
            "property_candidate": "",
            "property_abbreviation": "",
            "invoice_description": "Maintenance materials from Lowe's",
            "subtotal": 126.40,
            "tax_amount": 12.01,
            "shipping_amount": 0.00,
            "fees_amount": 0.00,
            "total_amount": 138.41,
            "needs_manual_review": True,
            "warnings": [],
            "line_items": [
                {
                    "description": "Interior door hardware",
                    "quantity": 4,
                    "unit_price": 18.75,
                    "amount": 75.00,
                    "gl_account_candidate": "",
                    "expense_type": "Maintenance supplies",
                    "is_replacement_reserve": False,
                },
                {
                    "description": "Primer and paint supplies",
                    "quantity": 1,
                    "unit_price": 51.40,
                    "amount": 51.40,
                    "gl_account_candidate": "repairs materials",
                    "expense_type": "Maintenance supplies",
                    "is_replacement_reserve": False,
                    "confidence": 0,
                    "reason": "",
                },
            ],
        }
    )
    payload.pop("confidence", None)

    def fake(_req, timeout=0):  # noqa: ANN001
        return FakeResponse(provider_envelope(json.dumps(payload)))

    def run() -> None:
        response = client.post(
            "/api/ai-invoice/test-extract",
            json={
                "vendor_hint": "Lowe's",
                "document_text": (
                    "LOWE'S invoice LOW-778812 dated 05/08/2026. "
                    "Interior door hardware 75.00. Primer and paint supplies 51.40. "
                    "Subtotal 126.40 tax 12.01 total 138.41."
                ),
                "dry_run": True,
            },
        )
        assert response.status_code == 200, response.text
        data = response.json()
        validation = data["validation"]
        normalized = data["normalized"]
        assert validation["total_reconciliation_passed"] is True, validation
        assert validation["confidence"] > 0.0, validation
        assert validation["confidence_source"] == "backend_derived", validation
        assert normalized["confidence"] > 0.0, normalized
        assert all(item["confidence"] > 0.0 for item in normalized["line_items"])
        assert all(item["reason"] for item in normalized["line_items"])
        assert all(item["gl_account_candidate"] for item in normalized["line_items"])
        reasons = " ".join(validation["manual_review_reasons"]).lower()
        assert "property" in reasons or "service address" in reasons, reasons
        assert "total_reconciliation_failed" not in validation["manual_review_codes"]

    with_fake_urlopen(fake)(run)
    print("Lowe's missing-confidence derivation: OK")


def assert_unavailable_provider_rejected() -> None:
    configure()

    def fake(_req, timeout=0):  # noqa: ANN001
        raise urllib.error.URLError("offline")

    def run() -> None:
        try:
            ai_provider.extract_invoice_structured(
                vendor_hint="HD Supply",
                document_text="Invoice text",
                page_images_or_refs=[],
                template_schema={"columns": []},
                property_reference=[],
                gl_reference=[],
                vendor_reference=[],
            )
        except ai_provider.AIProviderUnavailable:
            return
        raise AssertionError("Unavailable provider was not rejected")

    with_fake_urlopen(fake)(run)
    print("Provider unavailable handling: OK")


def assert_mock_still_configures() -> None:
    configure(provider="mock", base_url="", model="mock-invoice-v1", api_key="")
    status = client.get("/api/ai/status").json()
    assert status["enabled"] is True, status
    assert status["provider"] == "mock", status
    assert status["configured"] is True, status
    print("Mock provider still configures: OK")


def main() -> int:
    assert_disabled_and_missing_config()
    assert_manual_test_endpoint_success()
    assert_malformed_response_rejected()
    assert_lowes_missing_confidence_is_derived()
    assert_unavailable_provider_rejected()
    assert_mock_still_configures()
    print("Phase AI-1.2 OpenAI-compatible provider smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
