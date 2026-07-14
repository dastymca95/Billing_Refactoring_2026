"""Private, local-only inventory and stratified sample selection.

Detailed outputs never leave ``INNER_VIEW_PRIVATE_BENCHMARK_ROOT``. Console
output and the optional safe summary contain aggregates only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import secrets
import statistics
import time
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageStat
from pypdf import PdfReader


SOURCE_NAMES = ("Invoices CC Pictures", "Bills for training AP", "Bills for training TIA")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp", ".heic"}
DOCUMENT_EXTENSIONS = IMAGE_EXTENSIONS | {".pdf", ".docx", ".doc", ".xls", ".xlsx", ".csv", ".eml"}
SELECTION_TARGETS = {
    "clean_photos_receipts": 15,
    "difficult_blurry_photos": 15,
    "handwritten": 10,
    "scanned_invoices": 15,
    "scanned_bills": 15,
    "digital_vendor_invoices": 20,
    "multi_line_contractor": 10,
    "fees_renewals_subscriptions": 5,
    "mixed_materials_labor": 5,
    "unknown_unusual": 10,
}
SENSITIVE_PATTERNS = {
    "possible_bank_data": re.compile(r"\b(?:routing|bank account|aba|swift)\b", re.I),
    "possible_account_number": re.compile(r"\b(?:account|acct)\s*(?:number|no\.?|#)", re.I),
    "possible_email": re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b"),
    "possible_phone": re.compile(r"\b(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}\b"),
}
MATERIAL_TERMS = ("material", "parts", "supplies", "hardware", "paint", "wire", "fitting", "lumber")
LABOR_TERMS = ("labor", "service", "install", "repair", "maintenance", "contract")
FEE_TERMS = ("fee", "renewal", "subscription", "membership", "annual", "late charge")


@dataclass
class OriginalSnapshot:
    count: int
    bytes: int
    signature: str


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("INNER_VIEW_PRIVATE_BENCHMARK_ROOT", "")))
    parser.add_argument("--safe-summary", type=Path)
    parser.add_argument("--reuse-inventory", action="store_true")
    parser.add_argument("--full-scan-seconds", type=float)
    args = parser.parse_args()
    if not str(args.root) or not args.root.is_dir():
        raise SystemExit("INNER_VIEW_PRIVATE_BENCHMARK_ROOT is missing or invalid")
    root = args.root.resolve()
    source_roots = {name: (root / name).resolve() for name in SOURCE_NAMES}
    for path in source_roots.values():
        if not path.is_dir():
            raise SystemExit("One or more configured source folders are missing")

    started = time.perf_counter()
    before = snapshot_originals(source_roots.values())
    salt_path = root / "inventory" / ".inventory_salt"
    salt_path.parent.mkdir(parents=True, exist_ok=True)
    if not salt_path.exists():
        salt_path.write_text(secrets.token_hex(32), encoding="ascii")
    salt = salt_path.read_text(encoding="ascii").strip()

    if args.reuse_inventory:
        inventory = [json.loads(line) for line in (root / "inventory/private_inventory.jsonl").read_text(encoding="utf-8").splitlines()]
        errors = json.loads((root / "inventory/inventory_errors.json").read_text(encoding="utf-8"))["errors"]
        errors_by_id = {item["benchmark_id"]: item for item in errors}
        known = {item["benchmark_id"] for item in inventory} | {item["benchmark_id"] for item in errors}
        for source, source_root in source_roots.items():
            for path in iter_files(source_root):
                relative = path.relative_to(root).as_posix()
                anonymous_id = anonymous(salt, relative)
                if anonymous_id in known:
                    if anonymous_id in errors_by_id:
                        errors_by_id[anonymous_id].setdefault("extension", path.suffix.lower() or "[none]")
                    continue
                try:
                    inventory.append(inspect_file(root, source, path, relative, anonymous_id, salt))
                except Exception as exc:
                    errors.append({"benchmark_id": anonymous_id, "source_folder": source,
                                   "extension": path.suffix.lower() or "[none]",
                                   "error_type": type(exc).__name__, "stage": "inventory_reconciliation"})
                known.add(anonymous_id)
        for item in inventory:
            if not item.get("content_signature"):
                item["template_signature"] = ("visual-" + item["perceptual_hash"] if item.get("perceptual_hash")
                                               else "opaque-" + item["duplicate_hash"][:24])
    else:
        inventory = []
        errors = []
        files: list[tuple[str, Path]] = []
        for source, source_root in source_roots.items():
            files.extend((source, path) for path in iter_files(source_root))
        files.sort(key=lambda item: str(item[1]).lower())
        for source, path in files:
            relative = path.relative_to(root).as_posix()
            anonymous_id = anonymous(salt, relative)
            try:
                inventory.append(inspect_file(root, source, path, relative, anonymous_id, salt))
            except Exception as exc:
                errors.append({"benchmark_id": anonymous_id, "source_folder": source,
                               "extension": path.suffix.lower() or "[none]",
                               "error_type": type(exc).__name__, "stage": "inventory"})

    duplicate_groups = detect_duplicates(inventory)
    duplicate_members = {member for group in duplicate_groups for member in group["members"][1:]}
    for item in inventory:
        item["duplicate_group_id"] = None
        item["near_duplicate_group"] = None
    for group in duplicate_groups:
        for member in group["members"]:
            target = next(item for item in inventory if item["benchmark_id"] == member)
            field = "duplicate_group_id" if group["kind"] == "exact" else "near_duplicate_group"
            target[field] = group["group_id"]

    selected, reserve, selection_warnings = select_stratified(inventory, duplicate_members)
    write_private_outputs(root, inventory, errors, duplicate_groups, selected, reserve, selection_warnings,
                          elapsed=time.perf_counter() - started, full_scan_seconds=args.full_scan_seconds)
    after = snapshot_originals(source_roots.values())
    if before != after:
        raise RuntimeError("original source snapshot changed during inventory")
    safe = safe_summary(inventory, errors, duplicate_groups, selected, reserve,
                        elapsed=time.perf_counter() - started, originals_unchanged=True,
                        full_scan_seconds=args.full_scan_seconds)
    if args.safe_summary:
        args.safe_summary.parent.mkdir(parents=True, exist_ok=True)
        args.safe_summary.write_text(render_safe_markdown(safe), encoding="utf-8")
    print(json.dumps(safe, indent=2, sort_keys=True))
    return 0 if len(selected) == 120 and len(reserve) == 20 else 2


def inspect_file(root: Path, source: str, path: Path, relative: str, benchmark_id: str, salt: str) -> dict[str, Any]:
    stat = path.stat()
    extension = path.suffix.lower()
    content_hash = sha256_file(path)
    base: dict[str, Any] = {
        "benchmark_id": benchmark_id, "private_relative_path": relative, "source_folder": source,
        "extension": extension or "[none]", "file_size": stat.st_size, "page_count": None,
        "image_dimensions": None, "pdf_text_layer": None, "estimated_media_class": "unsupported",
        "estimated_ocr_quality": None, "orientation": "unknown", "blur_score": None,
        "contrast_score": None, "handwriting_likelihood": 0.0, "probable_document_family": "unknown",
        "probable_document_type": "unknown", "probable_vendor_token": None,
        "duplicate_hash": content_hash, "content_signature": None, "perceptual_hash": None,
        "template_signature": None, "complexity_tier": "D", "sensitive_data_flags": [],
        "processing_suitability": "unsupported", "inventory_warnings": [], "text_line_count": 0,
        "selection_bucket_candidates": [],
    }
    text = ""
    if extension == ".pdf":
        text, pdf = inspect_pdf(path)
        base.update(pdf)
    elif extension in IMAGE_EXTENSIONS:
        image = inspect_image(path)
        base.update(image)
    elif extension == ".docx":
        text = extract_docx_text(path)
        base.update({"page_count": None, "estimated_media_class": "digital_document",
                     "estimated_ocr_quality": quality_from_text(text), "processing_suitability": "reviewable"})
    elif extension in DOCUMENT_EXTENSIONS:
        base.update({"estimated_media_class": "digital_document", "processing_suitability": "reviewable",
                     "inventory_warnings": ["metadata_only_format"]})
    else:
        base["inventory_warnings"].append("unsupported_extension")

    normalized = normalize_text(text)
    base["text_line_count"] = sum(bool(line.strip()) for line in text.splitlines())
    base["content_signature"] = hashlib.sha256(normalized[:12000].encode()).hexdigest() if normalized else None
    base["template_signature"] = template_signature(base, normalized)
    base["sensitive_data_flags"] = sorted(key for key, pattern in SENSITIVE_PATTERNS.items() if pattern.search(text))
    vendor_hint = probable_vendor(text)
    base["probable_vendor_token"] = anonymous(salt, "vendor:" + vendor_hint) if vendor_hint else None
    base.update(classify_preliminary(base, normalized, source))
    return base


def inspect_pdf(path: Path) -> tuple[str, dict[str, Any]]:
    reader = PdfReader(str(path), strict=False)
    texts: list[str] = []
    for page in reader.pages[:3]:
        try:
            texts.append(page.extract_text() or "")
        except Exception:
            texts.append("")
    text = "\n".join(texts)
    chars = len(normalize_text(text))
    layer = chars >= 40
    return text, {"page_count": len(reader.pages), "pdf_text_layer": layer,
                  "estimated_media_class": "digital" if layer else "scan",
                  "estimated_ocr_quality": quality_from_text(text) if layer else 0.2,
                  "processing_suitability": "suitable" if layer else "needs_ocr",
                  "inventory_warnings": [] if layer else ["pdf_without_text_layer"]}


def inspect_image(path: Path) -> dict[str, Any]:
    with Image.open(path) as source:
        image = source.convert("L")
        image.thumbnail((512, 512))
        width, height = source.size
        stats = ImageStat.Stat(image)
        contrast = round(float(stats.stddev[0]) / 64.0, 4)
        blur = adjacent_difference(image)
        phash = difference_hash(image)
        orientation = "portrait" if height > width else "landscape" if width > height else "square"
        quality = max(0.0, min(1.0, .45 * min(1, contrast) + .55 * min(1, blur / 18)))
        tier = quality_tier(quality)
        return {"image_dimensions": [width, height], "page_count": 1, "estimated_media_class": "photo",
                "estimated_ocr_quality": round(quality, 4), "orientation": orientation,
                "blur_score": round(blur, 4), "contrast_score": contrast, "perceptual_hash": phash,
                "processing_suitability": "suitable" if tier in {"A", "B"} else "reviewable",
                "inventory_warnings": ["low_visual_quality"] if tier in {"C", "D"} else []}


def classify_preliminary(item: dict[str, Any], text: str, source: str) -> dict[str, Any]:
    ext = item["extension"]
    media = item["estimated_media_class"]
    pages = item.get("page_count") or 1
    quality = item.get("estimated_ocr_quality")
    has_material = any(term in text for term in MATERIAL_TERMS)
    has_labor = any(term in text for term in LABOR_TERMS)
    has_fee = any(term in text for term in FEE_TERMS)
    handwriting = 0.0
    if ext in IMAGE_EXTENSIONS:
        handwriting = .45 + (.25 if quality is not None and quality < .45 else 0) + (.15 if item["orientation"] == "landscape" else 0)
    document_type = "receipt" if "receipt" in text or ext in IMAGE_EXTENSIONS else "statement" if "statement" in text else "invoice"
    if has_fee:
        cohort = "fee_renewal_subscription"
    elif has_material and has_labor:
        cohort = "mixed_materials_and_labor"
    elif has_material:
        cohort = "materials_invoice"
    elif has_labor:
        cohort = "labor_service_invoice"
    elif source == "Bills for training TIA":
        cohort = "scanned_bill"
    elif source == "Bills for training AP" and media == "digital":
        cohort = "digital_vendor_invoice"
    elif ext in IMAGE_EXTENSIONS:
        cohort = "handwritten_invoice" if handwriting >= .65 else "photo_receipt"
    elif media == "scan":
        cohort = "scanned_vendor_invoice"
    else:
        cohort = "unknown_or_unusual"
    if pages > 1 and cohort not in {"scanned_bill", "fee_renewal_subscription"}:
        secondary = "multi_page_invoice"
    else:
        secondary = None
    tier = quality_tier(quality)
    candidates = selection_candidates(item, source, text, handwriting, has_material, has_labor, has_fee, tier)
    warnings = list(item.get("inventory_warnings") or [])
    if pages > 50:
        warnings.append("unusually_high_page_count")
    return {"handwriting_likelihood": round(min(1, handwriting), 3),
            "probable_document_family": "invoice" if document_type in {"invoice", "receipt"} else document_type,
            "probable_document_type": document_type, "preliminary_cohort": cohort,
            "secondary_cohort": secondary, "complexity_tier": tier,
            "selection_bucket_candidates": candidates, "inventory_warnings": sorted(set(warnings))}


def selection_candidates(item: dict[str, Any], source: str, text: str, handwriting: float,
                         has_material: bool, has_labor: bool, has_fee: bool, tier: str) -> list[str]:
    out: list[str] = []
    ext = item["extension"]
    media = item["estimated_media_class"]
    if ext in IMAGE_EXTENSIONS and tier in {"A", "B"}: out.append("clean_photos_receipts")
    if ext in IMAGE_EXTENSIONS and tier in {"C", "D"}: out.append("difficult_blurry_photos")
    if ext in IMAGE_EXTENSIONS and handwriting >= .6: out.append("handwritten")
    if media == "scan" and source == "Invoices CC Pictures": out.append("scanned_invoices")
    if source == "Bills for training TIA": out.append("scanned_bills")
    if source == "Bills for training AP" and media == "digital": out.append("digital_vendor_invoices")
    if item.get("text_line_count", 0) >= 8 and (has_labor or (item.get("page_count") or 1) > 1): out.append("multi_line_contractor")
    if has_fee: out.append("fees_renewals_subscriptions")
    if has_material and has_labor: out.append("mixed_materials_labor")
    if item["processing_suitability"] == "unsupported" or not out: out.append("unknown_unusual")
    return out


def detect_duplicates(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    exact: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        exact[item["duplicate_hash"]].append(item)
    grouped: set[str] = set()
    for digest, members in exact.items():
        if len(members) > 1:
            ids = sorted(item["benchmark_id"] for item in members)
            groups.append(group("exact", digest, ids, 1.0, "identical_sha256"))
            grouped.update(ids[1:])
    structural: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        if item["benchmark_id"] in grouped:
            continue
        signature = item.get("content_signature")
        if signature:
            structural[signature].append(item)
    for signature, members in structural.items():
        if len(members) > 1:
            ids = sorted(item["benchmark_id"] for item in members)
            groups.append(group("near", signature, ids, .88, "matching_normalized_text_or_visual_signature"))
    visual = [item for item in items if item.get("perceptual_hash") and item["benchmark_id"] not in grouped]
    parent = {item["benchmark_id"]: item["benchmark_id"] for item in visual}
    by_id = {item["benchmark_id"]: item for item in visual}
    def find(value):
        while parent[value] != value:
            parent[value] = parent[parent[value]]; value = parent[value]
        return value
    def union(left, right):
        a, b = find(left), find(right)
        if a != b: parent[b] = a
    for index, left in enumerate(visual):
        for right in visual[index + 1:]:
            ratio = max(left["file_size"], right["file_size"]) / max(1, min(left["file_size"], right["file_size"]))
            if ratio <= 1.5 and hamming_hex(left["perceptual_hash"], right["perceptual_hash"]) <= 5:
                union(left["benchmark_id"], right["benchmark_id"])
    visual_groups: dict[str, list[str]] = defaultdict(list)
    for item in visual: visual_groups[find(item["benchmark_id"])].append(item["benchmark_id"])
    for ids in visual_groups.values():
        if len(ids) > 1:
            ids.sort()
            groups.append(group("near", "visual:" + ids[0], ids, .82, "perceptual_image_similarity"))
    return groups


def group(kind: str, signature: str, ids: list[str], confidence: float, reason: str) -> dict[str, Any]:
    return {"group_id": "dup-" + hashlib.sha256((kind + signature).encode()).hexdigest()[:16],
            "kind": kind, "member_count": len(ids), "members": ids, "canonical_document": ids[0],
            "confidence": confidence, "reason": reason}


def select_stratified(items: list[dict[str, Any]], duplicate_members: set[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    eligible = [item for item in items if item["benchmark_id"] not in duplicate_members
                and item["processing_suitability"] != "unsupported"]
    selected: list[dict[str, Any]] = []
    used: set[str] = set()
    vendor_counts: Counter[str] = Counter()
    template_counts: Counter[str] = Counter()
    warnings: list[str] = []
    for bucket, target in SELECTION_TARGETS.items():
        candidates = [item for item in eligible if bucket in item["selection_bucket_candidates"]]
        candidates.sort(key=lambda item: selection_rank(item, bucket))
        picked = pick(candidates, target, bucket, used, vendor_counts, template_counts)
        selected.extend(picked)
        if len(picked) < target:
            fallback = [item for item in eligible if item["benchmark_id"] not in used]
            fallback.sort(key=lambda item: selection_rank(item, bucket))
            extra = pick(fallback, target - len(picked), bucket, used, vendor_counts, template_counts)
            selected.extend(extra)
            if len(picked) + len(extra) < target:
                warnings.append(f"selection_shortfall:{bucket}:{target-len(picked)-len(extra)}")
    reserve_used = set(used)
    reserve_vendor: Counter[str] = Counter()
    reserve_template: Counter[str] = Counter()
    reserve = []
    for bucket in SELECTION_TARGETS:
        reserve_candidates = [item for item in eligible if item["benchmark_id"] not in reserve_used
                              and bucket in item["selection_bucket_candidates"]]
        reserve_candidates.sort(key=lambda item: selection_rank(item, bucket))
        reserve.extend(pick(reserve_candidates, 2, bucket, reserve_used, reserve_vendor, reserve_template))
    if len(reserve) < 20:
        reserve_candidates = [item for item in eligible if item["benchmark_id"] not in reserve_used]
        reserve_candidates.sort(key=lambda item: (item["complexity_tier"], item["benchmark_id"]))
        reserve.extend(pick(reserve_candidates, 20 - len(reserve), "unknown_unusual",
                            reserve_used, reserve_vendor, reserve_template))
    return selected, reserve, warnings


def pick(candidates: list[dict[str, Any]], count: int, bucket: str, used: set[str],
         vendor_counts: Counter[str], template_counts: Counter[str]) -> list[dict[str, Any]]:
    out = []
    for item in candidates:
        if len(out) >= count or item["benchmark_id"] in used:
            continue
        vendor = item.get("probable_vendor_token") or "unknown:" + item["benchmark_id"]
        template = item.get("template_signature") or "unique:" + item["benchmark_id"]
        if vendor_counts[vendor] >= 5 or template_counts[template] >= 3:
            continue
        used.add(item["benchmark_id"]); vendor_counts[vendor] += 1; template_counts[template] += 1
        out.append({"benchmark_id": item["benchmark_id"], "selection_cohort": bucket,
                    "source_folder": item["source_folder"], "document_class": item["preliminary_cohort"],
                    "quality_tier": item["complexity_tier"], "page_count": item.get("page_count"),
                    "vendor_token": item.get("probable_vendor_token"), "template_signature": template,
                    "private_relative_path": item["private_relative_path"], "selection_status": "preliminary_not_gold"})
    return out


def selection_rank(item: dict[str, Any], bucket: str) -> tuple[Any, ...]:
    preferred_tier = {"clean_photos_receipts": "A", "difficult_blurry_photos": "C", "handwritten": "C"}.get(bucket, "B")
    return (item["complexity_tier"] != preferred_tier, -(item.get("page_count") or 1), item["benchmark_id"])


def write_private_outputs(root: Path, inventory: list[dict[str, Any]], errors: list[dict[str, Any]],
                          duplicates: list[dict[str, Any]], selected: list[dict[str, Any]], reserve: list[dict[str, Any]],
                          warnings: list[str], elapsed: float, full_scan_seconds: float | None = None) -> None:
    for relative in ("inventory", "selection", "labels/reviewer_1", "labels/reviewer_2", "labels/adjudicated", "reports"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    write_jsonl(root / "inventory/private_inventory.jsonl", inventory)
    write_json(root / "inventory/duplicate_groups.json", {"groups": duplicates})
    write_json(root / "inventory/inventory_errors.json", {"errors": errors})
    write_json(root / "inventory/classification_summary.json", aggregate_inventory(inventory))
    write_json(root / "selection/selected_120.json", {"selection": selected, "warnings": warnings})
    write_json(root / "selection/reserve_20.json", {"selection": reserve})
    write_json(root / "selection/cohort_summary.json", {"selected": dict(Counter(x["selection_cohort"] for x in selected)),
                                                         "quality_tiers": dict(Counter(x["quality_tier"] for x in selected))})
    summary = safe_summary(inventory, errors, duplicates, selected, reserve, elapsed, True, full_scan_seconds)
    (root / "reports/inventory_summary.md").write_text(render_safe_markdown(summary), encoding="utf-8")
    (root / "reports/privacy_audit.md").write_text(
        "# Privacy audit\n\nDetailed outputs remain inside the private root. No AI calls were made. "
        "The Git-safe summary contains aggregates only.\n", encoding="utf-8")
    (root / "reports/selection_report.md").write_text(
        "# Selection report\n\nSelected: %d\n\nReserve: %d\n\nWarnings: %s\n" %
        (len(selected), len(reserve), ", ".join(warnings) or "none"), encoding="utf-8")


def safe_summary(items, errors, duplicates, selected, reserve, elapsed, originals_unchanged,
                 full_scan_seconds: float | None = None) -> dict[str, Any]:
    exact = [group for group in duplicates if group["kind"] == "exact"]
    near = [group for group in duplicates if group["kind"] == "near"]
    return {"schema_version": "private-corpus-safe-summary/1.0", "total_files": len(items) + len(errors),
            "readable_files": len(items), "errors": len(errors),
            "counts_by_folder": dict(Counter(item["source_folder"] for item in [*items, *errors])),
            "counts_by_format": dict(Counter(item.get("extension", "[unknown]") for item in [*items, *errors])),
            "quality_tiers": dict(Counter(item["complexity_tier"] for item in items)),
            "probable_cohorts": dict(Counter(item["preliminary_cohort"] for item in items)),
            "exact_duplicate_groups": len(exact), "exact_duplicate_members": sum(g["member_count"] - 1 for g in exact),
            "near_duplicate_groups": len(near), "near_duplicate_members": sum(g["member_count"] - 1 for g in near),
            "selected_count": len(selected), "reserve_count": len(reserve),
            "selected_cohorts": dict(Counter(item["selection_cohort"] for item in selected)),
            "selected_quality_tiers": dict(Counter(item["quality_tier"] for item in selected)),
            "inventory_seconds": round(full_scan_seconds if full_scan_seconds is not None else elapsed, 2),
            "selection_recalculation_seconds": round(elapsed, 2), "ai_calls": 0,
            "originals_unchanged": originals_unchanged, "strong_reasoner_used": False}


def render_safe_markdown(summary: dict[str, Any]) -> str:
    def table(values):
        return "\n".join(f"| {key} | {value} |" for key, value in sorted(values.items())) or "| none | 0 |"
    return f"""# Private Corpus Inventory Summary

This file contains aggregate, redacted metadata only. It contains no filenames,
private paths, raw text, account data, tenant names, or document contents.

## Totals

- Files found: {summary['total_files']}
- Readable: {summary['readable_files']}
- Errors: {summary['errors']}
- Exact duplicate groups/members excluded: {summary['exact_duplicate_groups']} / {summary['exact_duplicate_members']}
- Near-duplicate groups/members: {summary['near_duplicate_groups']} / {summary['near_duplicate_members']}
- Selected: {summary['selected_count']}
- Reserve: {summary['reserve_count']}
- Inventory time: {summary['inventory_seconds']} seconds
- AI calls: 0
- Strong reasoner used: no
- Originals unchanged: {'yes' if summary['originals_unchanged'] else 'no'}

## Counts by authorized source folder

| Source | Count |
|---|---:|
{table(summary['counts_by_folder'])}

## Formats

| Extension | Count |
|---|---:|
{table(summary['counts_by_format'])}

## Quality tiers

| Tier | Count |
|---|---:|
{table(summary['quality_tiers'])}

## Preliminary cohorts

| Cohort | Count |
|---|---:|
{table(summary['probable_cohorts'])}

## Selected cohorts

| Cohort | Count |
|---|---:|
{table(summary['selected_cohorts'])}

## Selected quality mix

| Tier | Count |
|---|---:|
{table(summary['selected_quality_tiers'])}

## Deterministic control cohort

Twenty known recurring digital bills already loaded in the application should
be added later as a separate control cohort. They must measure deterministic hit
rate, unnecessary AI calls, latency, GL and reconciliation regressions, and
unexpected review creation. They were not copied or inventoried in this stage.
"""


def aggregate_inventory(items):
    return {"total": len(items), "cohorts": dict(Counter(x["preliminary_cohort"] for x in items)),
            "quality_tiers": dict(Counter(x["complexity_tier"] for x in items)),
            "formats": dict(Counter(x["extension"] for x in items))}


def snapshot_originals(roots: Iterable[Path]) -> OriginalSnapshot:
    records = []
    total_bytes = 0
    for root in roots:
        for path in iter_files(root):
            try:
                stat = path.stat(); total_bytes += stat.st_size
                records.append(f"{path.relative_to(root).as_posix()}|{stat.st_size}|{stat.st_mtime_ns}")
            except OSError:
                records.append(f"{path.relative_to(root).as_posix()}|STAT_ERROR")
    records.sort()
    return OriginalSnapshot(len(records), total_bytes, hashlib.sha256("\n".join(records).encode()).hexdigest())


def iter_files(root: Path) -> Iterable[Path]:
    for directory, _subdirs, filenames in os.walk(root):
        for filename in filenames:
            yield Path(directory) / filename


def anonymous(salt: str, value: str) -> str:
    return "bench-" + hashlib.sha256((salt + "\0" + value).encode()).hexdigest()[:20]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def probable_vendor(text: str) -> str | None:
    for line in text.splitlines()[:12]:
        cleaned = re.sub(r"[^a-zA-Z ]", " ", line)
        cleaned = re.sub(r"\s+", " ", cleaned).strip().lower()
        if 3 <= len(cleaned) <= 80 and not any(word in cleaned for word in ("invoice", "bill to", "total", "page")):
            return cleaned
    return None


def quality_from_text(text: str) -> float:
    normalized = normalize_text(text)
    if not normalized: return 0.0
    printable = sum(char.isprintable() for char in text) / max(len(text), 1)
    alpha = sum(char.isalpha() for char in normalized) / max(len(normalized), 1)
    return round(min(1.0, .5 * printable + .5 * min(1, alpha * 2)), 4)


def quality_tier(value: float | None) -> str:
    if value is None: return "D"
    if value >= .78: return "A"
    if value >= .52: return "B"
    if value >= .28: return "C"
    return "D"


def adjacent_difference(image: Image.Image) -> float:
    pixels = list(image.resize((64, 64)).getdata())
    diffs = [abs(pixels[i] - pixels[i - 1]) for i in range(1, len(pixels)) if i % 64]
    return sum(diffs) / max(len(diffs), 1)


def difference_hash(image: Image.Image) -> str:
    resized = image.resize((9, 8))
    pixels = list(resized.getdata())
    bits = []
    for y in range(8):
        for x in range(8): bits.append(pixels[y * 9 + x] > pixels[y * 9 + x + 1])
    return f"{sum((1 << index) for index, bit in enumerate(bits) if bit):016x}"


def template_signature(item: dict[str, Any], normalized_text: str) -> str:
    if not normalized_text:
        if item.get("perceptual_hash"):
            return "visual-" + item["perceptual_hash"]
        return "opaque-" + item["duplicate_hash"][:24]
    scrubbed = re.sub(r"\b\d+(?:\.\d+)?\b", "#", normalized_text[:4000])
    basis = f"{item['extension']}|{item.get('page_count')}|{scrubbed}"
    return hashlib.sha256(basis.encode()).hexdigest()[:24]


def hamming_hex(left: str, right: str) -> int:
    return (int(left, 16) ^ int(right, 16)).bit_count()


def extract_docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml").decode("utf-8", errors="ignore")
    return re.sub(r"<[^>]+>", " ", xml)


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows: handle.write(json.dumps(row, sort_keys=True) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
