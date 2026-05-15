"""Phase AI-1.1 smoke tests for the mock AI invoice provider.

Runs entirely in-process with FastAPI TestClient. It creates and deletes
QA-only webapp batches and never calls external AI providers, Dropbox, or
vendor CLI processors.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Set before importing the app/settings. The settings module also allows the
# script to patch these values between test cases below.
os.environ["AI_ASSIST_ENABLED"] = "true"
os.environ["AI_PROVIDER"] = "mock"
os.environ.setdefault("AI_MODEL", "mock-invoice-v1")
os.environ.pop("AI_API_KEY", None)
os.environ.pop("AI_BASE_URL", None)

from fastapi.testclient import TestClient  # noqa: E402

from webapp.backend import settings  # noqa: E402
from webapp.backend.main import app  # noqa: E402
from webapp.backend.services import ai_provider, batch_processor  # noqa: E402
from webapp.backend.services.vendor_detection import detect_vendor_for_file  # noqa: E402


FIXTURE = (
    ROOT
    / "webapp"
    / "backend"
    / "tests"
    / "fixtures"
    / "variable_supplier_mock_invoice.txt"
)


client = TestClient(app)


def configure_ai(*, enabled: bool = True, mode: str = "") -> None:
    settings.AI_ASSIST_ENABLED = enabled
    settings.AI_PROVIDER = "mock" if enabled else ""
    settings.AI_MODEL = "mock-invoice-v1" if enabled else ""
    settings.AI_API_KEY = ""
    settings.AI_BASE_URL = ""
    settings.AI_MOCK_MODE = mode


def create_batch(name: str) -> str:
    response = client.post(
        "/api/batches",
        json={"batch_name": name, "document_mode": "auto_detect"},
    )
    response.raise_for_status()
    return response.json()["batch_id"]


def upload_fixture(batch_id: str, *, marker: str = "") -> None:
    data = FIXTURE.read_bytes()
    if marker:
        data += f"\n\n{marker}\n".encode("utf-8")
    response = client.post(
        f"/api/batches/{batch_id}/upload",
        files={
            "file": (
                "HD Supply mock invoice.txt",
                data,
                "text/plain",
            )
        },
    )
    response.raise_for_status()


def delete_batch(batch_id: str) -> None:
    client.delete(f"/api/batches/{batch_id}")


def process_fixture_case(name: str, *, marker: str = "", mode: str = "") -> dict:
    configure_ai(enabled=True, mode=mode)
    batch_id = create_batch(name)
    try:
        upload_fixture(batch_id, marker=marker)
        response = client.post(f"/api/batches/{batch_id}/process?sync=1")
        response.raise_for_status()
        result = response.json()
        preview = client.get(f"/api/batches/{batch_id}/preview")
        if preview.status_code == 200:
            result["_preview"] = preview.json()
        review = client.get(f"/api/batches/{batch_id}/manual-review")
        if review.status_code == 200:
            result["_manual_review"] = review.json()
        return result
    finally:
        delete_batch(batch_id)


def assert_ai_disabled_path() -> None:
    configure_ai(enabled=False)
    batch_id = create_batch("QA AI disabled smoke")
    try:
        upload_fixture(batch_id)
        response = client.post(f"/api/batches/{batch_id}/process?sync=1")
        response.raise_for_status()
        result = response.json()
        reasons = [item.get("reason") for item in result.get("unsupported_files", [])]
        assert "ai_invoice_processing_not_configured" in reasons, reasons
        assert result.get("all_manual_review"), "Disabled AI path should create review item"
        print("AI disabled path: OK")
    finally:
        delete_batch(batch_id)


def assert_mock_status() -> None:
    configure_ai(enabled=True)
    status = client.get("/api/ai/status").json()
    assert status["enabled"] is True
    assert status["provider"] == "mock"
    assert status["configured"] is True
    assert "AI_API_KEY" not in str(status)
    print("Mock status endpoint: OK")


def assert_mock_success_path() -> None:
    result = process_fixture_case("QA AI mock success")
    summary = result.get("summary") or {}
    assert summary.get("files_supported") == 1, summary
    assert not result.get("unsupported_files"), result.get("unsupported_files")
    rows = ((result.get("_preview") or {}).get("rows") or [])
    assert len(rows) >= 2, rows
    assert all((row.get("_meta") or {}).get("ai_generated") for row in rows)
    vendors = {row.get("Vendor") for row in rows}
    assert any(str(v or "").rstrip(".") == "HD Supply Facilities Maintenance, Ltd" for v in vendors), vendors
    print("Mock AI success path: OK")


def assert_malformed_mock_rejected() -> None:
    configure_ai(enabled=True, mode="malformed_json")
    try:
        ai_provider.extract_invoice_structured(
            vendor_hint="HD Supply",
            document_text=FIXTURE.read_text(encoding="utf-8"),
            page_images_or_refs=[],
            template_schema={"columns": []},
            property_reference=[],
            gl_reference=[],
            vendor_reference=[],
        )
        raise AssertionError("Malformed mock JSON was accepted")
    except ai_provider.AIProviderError:
        pass
    result = process_fixture_case(
        "QA AI malformed mock", marker="MOCK_MALFORMED_JSON", mode=""
    )
    reasons = [item.get("reason") for item in result.get("unsupported_files", [])]
    assert (
        "ai_response_invalid_json" in reasons or "ai_processing_failed" in reasons
    ), reasons
    print("Malformed mock JSON rejected: OK")


def assert_total_mismatch_flagged() -> None:
    result = process_fixture_case(
        "QA AI total mismatch mock", marker="MOCK_TOTAL_MISMATCH"
    )
    reasons = {
        reason
        for item in result.get("all_manual_review", [])
        for reason in item.get("reasons", [])
    }
    codes = {
        code
        for item in result.get("all_manual_review", [])
        for code in item.get("reason_codes", [])
    }
    assert "total_reconciliation_failed" in codes, (codes, reasons)
    print("Total mismatch validation: OK")


def assert_low_confidence_flagged() -> None:
    result = process_fixture_case(
        "QA AI low confidence mock", marker="MOCK_LOW_CONFIDENCE"
    )
    rows = ((result.get("_preview") or {}).get("rows") or [])
    assert rows, "low confidence mock should still generate rows"
    assert any((row.get("_meta") or {}).get("ai_confidence_low") for row in rows)
    reasons = {
        reason
        for item in result.get("all_manual_review", [])
        for reason in item.get("reasons", [])
    }
    codes = {
        code
        for item in result.get("all_manual_review", [])
        for code in item.get("reason_codes", [])
    }
    assert "ai_confidence_low" in codes, (codes, reasons)
    print("Low confidence validation: OK")


def assert_deterministic_vendors_bypass_ai() -> None:
    assert "richmond_utilities" in batch_processor._PROCESSOR_LOADERS
    assert "hopkinsville_water_environment_authority" in batch_processor._PROCESSOR_LOADERS

    richmond = next(
        (
            ROOT
            / "Training Bills_Invoices"
            / "Water - Sewer"
            / "Richmond Utilities"
            / "Bills_Training"
        ).glob("*.csv"),
        None,
    )
    if richmond:
        det = detect_vendor_for_file(richmond)
        assert det["vendor_key"] == "richmond_utilities", det
        assert det.get("processing_mode") == "deterministic", det

    hwea = next(
        (
            ROOT
            / "Training Bills_Invoices"
            / "Water - Sewer"
            / "Hopkinsville Water Environment Authority"
            / "Bills_Training"
        ).glob("*.pdf"),
        None,
    )
    if hwea:
        det = detect_vendor_for_file(hwea)
        assert det["vendor_key"] == "hopkinsville_water_environment_authority", det
        assert det.get("processing_mode") == "deterministic", det

    print("Deterministic vendor routing: OK")


def main() -> int:
    assert FIXTURE.is_file(), FIXTURE
    assert_ai_disabled_path()
    assert_mock_status()
    assert_mock_success_path()
    assert_malformed_mock_rejected()
    assert_total_mismatch_flagged()
    assert_low_confidence_flagged()
    assert_deterministic_vendors_bypass_ai()
    print("Phase AI-1.1 mock provider smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
