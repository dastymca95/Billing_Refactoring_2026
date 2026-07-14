import importlib.util
import sys
from collections import Counter
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "inventory_private_benchmark.py"
spec = importlib.util.spec_from_file_location("private_inventory", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def _item(index: int, bucket: str) -> dict:
    return {
        "benchmark_id": f"bench-{index:04d}", "processing_suitability": "suitable",
        "probable_vendor_token": f"vendor-{index // 4}", "template_signature": f"template-{index // 2}",
        "complexity_tier": ("A", "B", "C", "D")[index % 4], "page_count": 1 + index % 3,
        "selection_bucket_candidates": [bucket], "source_folder": "synthetic-source",
        "preliminary_cohort": "unknown_or_unusual", "private_relative_path": f"private/{index}",
    }


def test_stratified_selection_exact_counts_and_limits():
    items = []
    index = 0
    for bucket, target in module.SELECTION_TARGETS.items():
        for _ in range(target + 8):
            items.append(_item(index, bucket)); index += 1
    for _ in range(30):
        items.append(_item(index, "unknown_unusual")); index += 1
    selected, reserve, warnings = module.select_stratified(items, set())
    assert len(selected) == 120 and len(reserve) == 20
    assert Counter(row["selection_cohort"] for row in selected) == module.SELECTION_TARGETS
    assert max(Counter(row["vendor_token"] for row in selected if row["vendor_token"]).values()) <= 5
    assert max(Counter(row["template_signature"] for row in selected).values()) <= 3
    assert not warnings


def test_safe_summary_and_markdown_do_not_include_private_paths():
    item = _item(1, "unknown_unusual") | {"extension": ".pdf", "duplicate_hash": "hash",
                                                  "preliminary_cohort": "unknown_or_unusual"}
    safe = module.safe_summary([item], [], [], [], [], 1.2, True)
    rendered = module.render_safe_markdown(safe)
    assert "private/1" not in rendered
    assert "benchmark_id" not in rendered
    assert safe["ai_calls"] == 0 and safe["strong_reasoner_used"] is False


def test_exact_duplicate_detection_excludes_secondary_member():
    first = _item(1, "unknown_unusual") | {"duplicate_hash": "same", "content_signature": None,
                                                   "perceptual_hash": None}
    second = _item(2, "unknown_unusual") | {"duplicate_hash": "same", "content_signature": None,
                                                    "perceptual_hash": None}
    groups = module.detect_duplicates([first, second])
    assert groups[0]["kind"] == "exact"
    assert groups[0]["member_count"] == 2
