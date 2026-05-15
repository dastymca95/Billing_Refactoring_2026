from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp.backend.services import canonical_rules
from webapp.backend.services.template_rules import get_template_rules, reset_cache


def main() -> int:
    if not canonical_rules.CANONICAL_RULES_YAML.is_file():
        canonical_rules.import_canonical_rules_from_excel()
    rules = canonical_rules.load_rules()
    required = canonical_rules.required_columns()
    expected = {
        "Invoice Number",
        "Bill or Credit",
        "Invoice Date",
        "Accounting Date",
        "Vendor",
        "Invoice Description",
        "Line Item Number",
        "Property Abbreviation",
        "GL Account",
        "Line Item Description",
        "Amount",
        "Expense Type",
        "Is Replacement Reserve",
        "Due Date",
        "Document Url",
    }
    missing = sorted(expected - set(required))
    if missing:
        print(f"Missing canonical required columns: {missing}")
        return 1
    categories = set((rules.get("categories") or {}).keys())
    for category in {"utilities", "trash_collection_services", "other_infrequent"}:
        if category not in categories:
            print(f"Missing canonical category: {category}")
            return 1
    reset_cache()
    template_rules = get_template_rules()
    template_required = set(template_rules.get("required_columns") or [])
    missing_template = sorted(expected - template_required)
    if missing_template:
        print(f"Template rules did not adopt canonical required columns: {missing_template}")
        return 1
    print("Canonical rules engine smoke passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
