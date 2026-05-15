from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp.backend.services import canonical_rules


def main() -> int:
    rules = canonical_rules.import_canonical_rules_from_excel()
    print(f"Wrote {canonical_rules.CANONICAL_RULES_YAML}")
    print(f"Imported {len(rules.get('source', {}).get('imported_rows') or [])} template column rule rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
