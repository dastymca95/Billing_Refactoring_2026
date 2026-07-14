from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp.backend.services.representative_benchmark import (  # noqa: E402
    BenchmarkLabel, RepresentativeManifest, resolve_document,
)

FIXTURE_ROOT = ROOT / "webapp/backend/tests/fixtures/document_benchmark"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=FIXTURE_ROOT / "representative_manifest.json")
    parser.add_argument("--require-minimum", type=int, default=0)
    args = parser.parse_args()
    manifest = RepresentativeManifest.model_validate_json(args.manifest.read_text(encoding="utf-8"))
    statuses: Counter[str] = Counter()
    cohorts: Counter[str] = Counter()
    missing_documents: list[str] = []
    missing_labels: list[str] = []
    for entry in manifest.entries:
        label_path = (FIXTURE_ROOT / entry.label_ref).resolve()
        if not label_path.is_file():
            missing_labels.append(entry.case_id)
            statuses["unlabeled"] += 1
        else:
            label = BenchmarkLabel.model_validate_json(label_path.read_text(encoding="utf-8"))
            statuses[label.status.value] += 1
        cohorts[entry.document_class] += 1
        try:
            if not resolve_document(entry, FIXTURE_ROOT).is_file():
                missing_documents.append(entry.case_id)
        except FileNotFoundError:
            missing_documents.append(entry.case_id)
    result = {
        "schema_version": "representative-dataset-audit/1.0",
        "entries": len(manifest.entries),
        "label_status": {key: statuses.get(key, 0) for key in ("gold", "partial", "unlabeled")},
        "cohorts": dict(sorted(cohorts.items())),
        "missing_documents": missing_documents,
        "missing_labels": missing_labels,
        "minimum_required": args.require_minimum,
        "minimum_met": statuses["gold"] >= args.require_minimum,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["minimum_met"] and not missing_documents and not missing_labels else 1


if __name__ == "__main__":
    raise SystemExit(main())
