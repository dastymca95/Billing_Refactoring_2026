from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp.backend.services.document_benchmark import load_manifest  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and enumerate the sanitized document benchmark.")
    parser.add_argument("--manifest", type=Path, default=ROOT / "webapp/backend/tests/fixtures/document_benchmark/manifest.json")
    parser.add_argument("--sample", type=int)
    parser.add_argument("--model", default="current")
    parser.add_argument("--dry-run", action="store_true", default=True)
    args = parser.parse_args()
    cases = load_manifest(args.manifest)
    if args.sample is not None:
        cases = cases[:max(args.sample, 0)]
    # Provider execution is intentionally not implicit. A future approved
    # runner must inject facts extraction and accounting reasoning separately.
    print(json.dumps({"schema_version": "benchmark-plan/1.0", "model": args.model,
                      "dry_run": True, "cases": len(cases),
                      "case_ids": [case.case_id for case in cases]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
