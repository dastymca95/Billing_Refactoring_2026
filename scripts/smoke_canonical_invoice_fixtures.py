from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp.backend.services import canonical_invoice_fixtures  # noqa: E402


OUTPUT_TEMPLATE = ROOT / "Output" / "Template.xlsx"


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    before_output_mtime = OUTPUT_TEMPLATE.stat().st_mtime_ns if OUTPUT_TEMPLATE.is_file() else None
    result = canonical_invoice_fixtures.run_all_complete()

    for summary in result["summary"]:
        key = summary["fixture_key"]
        status = summary["status"]
        failed = summary.get("failed_checks") or []
        reason = summary.get("skip_reason") or ""
        suffix = f" ({', '.join(failed)})" if failed else ""
        if status == "SKIPPED" and reason:
            suffix = f" - {reason}"
        print(f"{key}: {status}{suffix}")

    complete_results = [item for item in result["results"] if not item.get("skipped")]
    _assert(complete_results, "No complete canonical invoice fixtures were executed.")
    _assert(result["ok"], "One or more complete canonical invoice fixtures failed.")

    by_key = {item["fixture_key"]: item for item in result["results"]}
    _assert(by_key["capital_waste"]["ok"], "Capital Waste fixture must pass.")
    _assert(by_key["spectrum"]["ok"], "Spectrum fixture must pass.")
    _assert(by_key["lowes_pro_supply"]["ok"], "Lowe's Pro Supply fixture must pass.")
    for optional_key in ("servall_pest", "tk_elevator", "epb"):
        item = by_key[optional_key]
        if item.get("skipped"):
            _assert(item.get("skip_reason"), f"{optional_key} skipped without an explicit reason.")
        else:
            _assert(item.get("ok"), f"{optional_key} fixture must pass when complete.")

    if before_output_mtime is not None:
        _assert(OUTPUT_TEMPLATE.stat().st_mtime_ns == before_output_mtime, "Output/Template.xlsx changed.")

    print("Canonical invoice fixture smoke passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
