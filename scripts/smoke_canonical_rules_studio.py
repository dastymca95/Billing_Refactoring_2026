from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp.backend.main import app  # noqa: E402
from webapp.backend.services import canonical_rules  # noqa: E402


OUTPUT_TEMPLATE = ROOT / "Output" / "Template.xlsx"


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    if not canonical_rules.CANONICAL_RULES_YAML.is_file():
        canonical_rules.import_canonical_rules_from_excel()

    before_output_mtime = OUTPUT_TEMPLATE.stat().st_mtime_ns if OUTPUT_TEMPLATE.is_file() else None
    before_yaml = canonical_rules.CANONICAL_RULES_YAML.read_text(encoding="utf-8")
    client = TestClient(app)

    res = client.get("/api/canonical-rules")
    _assert(res.status_code == 200, f"GET canonical rules failed: {res.text}")
    payload = res.json()
    _assert("trash_collection_services" in [c["key"] for c in payload["categories"]], "Trash category missing.")

    res = client.get("/api/canonical-rules/not-a-category")
    _assert(res.status_code == 400, "Invalid category should be rejected.")

    fixtures = client.get("/api/canonical-rules/test-fixtures")
    _assert(fixtures.status_code == 200, f"Fixture list failed: {fixtures.text}")
    fixture_keys = [item["key"] for item in fixtures.json()["fixtures"]]
    _assert("spectrum" in fixture_keys, "Spectrum fixture missing from test bench.")

    res = client.post("/api/canonical-rules/validate", json={"config": {"template_requirements": {"required_columns": []}}})
    _assert(res.status_code == 200, f"Validate request failed: {res.text}")
    validation = res.json()
    _assert(not validation["ok"], "Missing required rule should fail validation.")

    bench = client.post("/api/canonical-rules/test-bench", json={"test_case": "capital_waste"})
    _assert(bench.status_code == 200, f"Capital Waste test bench failed: {bench.text}")
    bench_payload = bench.json()
    _assert(bench_payload["ok"], "Capital Waste expected/actual checks should pass.")

    spectrum = client.post("/api/canonical-rules/test-bench", json={"fixture_key": "spectrum"})
    _assert(spectrum.status_code == 200, f"Spectrum test bench failed: {spectrum.text}")
    spectrum_payload = spectrum.json()
    _assert(spectrum_payload["ok"], "Spectrum expected/actual checks should pass.")
    _assert(spectrum_payload["actual"]["category"] == "subscriptions", "Spectrum category should be stable.")

    suite = client.post("/api/canonical-rules/test-bench", json={"run_all": True})
    _assert(suite.status_code == 200, f"Run-all fixture bench failed: {suite.text}")
    _assert(suite.json()["ok"], "Run-all complete fixture suite should pass.")

    dry_run = client.post(
        "/api/canonical-rules/test-bench",
        json={
            "test_case": "capital_waste",
            "category": "trash_collection_services",
            "draft_patch": {"invoice_description_format": "{vendor_name}"},
        },
    )
    _assert(dry_run.status_code == 200, f"Dry-run test bench failed: {dry_run.text}")
    dry_payload = dry_run.json()
    _assert(dry_payload["dry_run"], "Draft rule test should report dry_run.")
    _assert(
        dry_payload["actual"]["invoice_description"] == "Capital Waste Services",
        "Draft rule did not change the dry-run result.",
    )
    _assert(
        canonical_rules.CANONICAL_RULES_YAML.read_text(encoding="utf-8") == before_yaml,
        "Dry-run test modified canonical_rules.yaml.",
    )

    current = client.get("/api/canonical-rules/trash_collection_services").json()["editable"]
    current_policy = current["location_policy"]
    patch = client.patch(
        "/api/canonical-rules/trash_collection_services",
        json={"patch": {"location_policy": current_policy}},
    )
    _assert(patch.status_code == 200, f"No-op patch failed: {patch.text}")
    _assert("backup_path" in patch.json()["result"], "Patch did not create a backup.")

    restored = client.post("/api/canonical-rules/restore")
    _assert(restored.status_code == 200, f"Restore failed: {restored.text}")
    _assert(
        canonical_rules.CANONICAL_RULES_YAML.read_text(encoding="utf-8") == before_yaml,
        "Restore did not return canonical_rules.yaml to its previous content.",
    )

    if before_output_mtime is not None:
        _assert(OUTPUT_TEMPLATE.stat().st_mtime_ns == before_output_mtime, "Output/Template.xlsx changed.")

    print("Canonical Rules Studio smoke passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
