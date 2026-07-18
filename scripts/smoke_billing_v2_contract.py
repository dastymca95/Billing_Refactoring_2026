"""Smoke-test the additive Billing V2 backend contract.

Runs in-process with FastAPI TestClient. It does not process real vendor files;
it verifies the V2 audit endpoint and the explicit pre-export document-link
preparation step against a synthetic cached preview.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from webapp.backend.main import app  # noqa: E402
from webapp.backend.services import batch_store  # noqa: E402


client = TestClient(app)


def main() -> int:
    audit = client.get("/api/billing-v2/audit")
    assert audit.status_code == 200, audit.text
    audit_json = audit.json()
    assert audit_json["count"] >= 1, audit_json
    assert audit_json["available_count"] >= 1, audit_json
    assert any(
        item["vendor_key"] == "richmond_utilities"
        for item in audit_json["processors"]
    ), audit_json["processors"]

    batch_id = batch_store.create_batch()
    try:
        source = batch_store.get_input_dir(batch_id) / "source_invoice.pdf"
        source.write_bytes(b"%PDF-1.4\n% billing-v2 smoke fixture\n")
        cache = batch_store.get_processed_dir(batch_id) / "_webapp_result.json"
        cache.write_text(
            json.dumps(
                {
                    "batch_id": batch_id,
                    "summary": {
                        "files_total": 1,
                        "files_supported": 1,
                        "files_unsupported": 0,
                        "invoices_total": 1,
                        "manual_review_total": 0,
                    },
                    "by_vendor": {
                        "smoke_vendor": {
                            "invoices": [
                                {
                                    "invoice_number": "V2-100",
                                    "source_file": source.name,
                                    "rows": [
                                        {
                                            "Invoice Number": "V2-100",
                                            "Vendor": "Smoke Vendor",
                                            "Document Url": "",
                                        }
                                    ],
                                }
                            ],
                        }
                    },
                    "all_invoices": [
                        {
                            "invoice_number": "V2-100",
                            "source_file": source.name,
                            "rows": [
                                {
                                    "Invoice Number": "V2-100",
                                    "Vendor": "Smoke Vendor",
                                    "Document Url": "",
                                }
                            ],
                        }
                    ],
                    "all_manual_review": [],
                }
            ),
            encoding="utf-8",
        )

        prepared = client.post(f"/api/billing-v2/batches/{batch_id}/prepare-links")
        assert prepared.status_code == 200, prepared.text
        body = prepared.json()
        assert body["prepared"] is True, body
        assert body["rows_total"] == 1, body
        assert body["rows_with_links"] == 1, body
        assert body["links"]["local_webapp"] == 1, body

        updated = json.loads(cache.read_text(encoding="utf-8"))
        row = updated["all_invoices"][0]["rows"][0]
        assert row["Document Url"], row
        assert f"/api/batches/{batch_id}/files/{source.name}/content" in row["Document Url"], row
    finally:
        batch_store.delete_batch(batch_id)

    print("Billing V2 backend contract smoke passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
