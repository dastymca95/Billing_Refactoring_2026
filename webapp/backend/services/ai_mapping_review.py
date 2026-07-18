"""AI vendor / GL mapping review and learned mapping storage.

This service is intentionally separate from deterministic vendor processors.
It helps the webapp review AI-assisted supplier invoices, persist operator
confirmations, and apply those confirmations to future AI-assisted results.
"""

from __future__ import annotations

import csv
import datetime as _dt
import difflib
import re
import shutil
import tempfile
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any

from .. import settings


_LOCK = threading.RLock()
LEARNED_MAPPINGS_PATH = settings.PROJECT_ROOT / "config" / "ai_learned_mappings.yaml"


def normalize_key(value: str) -> str:
    s = str(value or "").lower().strip()
    s = s.replace("&", " and ")
    s = re.sub(r"['’]s\b", "s", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    words = [
        w
        for w in s.split()
        if w not in {"the", "inc", "llc", "ltd", "co", "company", "corp", "corporation"}
    ]
    return " ".join(words).strip()


def mapping_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", normalize_key(value)).strip("_") or "unknown"


def vendor_candidates(detected_vendor: str, *, limit: int = 6) -> dict[str, Any]:
    detected = str(detected_vendor or "").strip()
    learned = learned_vendor_mapping(detected)
    vendors = _load_vendor_list()
    scored: list[dict[str, Any]] = []
    if learned:
        scored.append({
            "vendor_name": learned["resman_vendor_name"],
            "vendor_id": learned.get("vendor_id") or "",
            "score": 1.0,
            "reason": "Previously confirmed by user",
            "learned": True,
        })

    needle = normalize_key(detected)
    for vendor in vendors:
        name = str(vendor.get("vendor_name") or "")
        vendor_id = str(vendor.get("vendor_id") or "")
        score, reason = _vendor_score(needle, name, vendor)
        if score <= 0:
            continue
        scored.append({
            "vendor_name": name,
            "vendor_id": vendor_id,
            "score": round(min(score, 0.99), 3),
            "reason": reason,
            "learned": False,
        })

    dedup: dict[str, dict[str, Any]] = {}
    for item in sorted(scored, key=lambda x: x["score"], reverse=True):
        key = normalize_key(item["vendor_name"])
        if key not in dedup or item["score"] > dedup[key]["score"]:
            dedup[key] = item
    candidates = list(dedup.values())[: max(1, limit)]
    top_score = candidates[0]["score"] if candidates else 0
    ambiguous = len(candidates) > 1 and abs(top_score - candidates[1]["score"]) < 0.08
    return {
        "detected_vendor": detected,
        "normalized_detected_vendor": mapping_key(detected),
        "candidates": candidates,
        "needs_confirmation": not learned or ambiguous,
    }


def gl_candidates(
    *,
    line_item_description: str,
    vendor_name: str = "",
    ai_suggested_gl: str = "",
    limit: int = 6,
) -> dict[str, Any]:
    desc = str(line_item_description or "").strip()
    vendor = str(vendor_name or "").strip()
    ai_gl = str(ai_suggested_gl or "").strip()
    accounts = _load_gl_accounts()
    scored: list[dict[str, Any]] = []

    learned = learned_gl_mapping(vendor, desc)
    if learned:
        scored.append({
            "gl_account": learned["gl_account"],
            "gl_code": learned.get("gl_code") or learned["gl_account"],
            "gl_name": learned.get("gl_name") or "",
            "score": 1.0,
            "reason": f"Previously confirmed pattern: {learned.get('pattern')}",
            "learned": True,
        })

    vendor_default = _vendor_default_gl(vendor)
    if vendor_default:
        account = validate_gl_account(vendor_default)
        if account:
            scored.append(_candidate_from_account(account, 0.87, "Vendor default GL"))

    if ai_gl:
        account = validate_gl_account(ai_gl)
        if account:
            scored.append(_candidate_from_account(account, 0.92, "AI suggested a valid GL account"))
        else:
            # Keep an invalid AI suggestion visible, but never treated as accepted.
            scored.append({
                "gl_account": ai_gl,
                "gl_code": "",
                "gl_name": "",
                "score": 0.35,
                "reason": "AI suggested this, but it is not validated against GL reference",
                "learned": False,
                "valid": False,
            })

    haystack = normalize_key(desc)
    for account in accounts:
        score, reason = _gl_score(haystack, account)
        if score > 0:
            scored.append(_candidate_from_account(account, score, reason))

    dedup: dict[str, dict[str, Any]] = {}
    for item in sorted(scored, key=lambda x: x["score"], reverse=True):
        key = item.get("gl_code") or item.get("gl_account")
        if not key:
            key = normalize_key(str(item.get("gl_account") or ""))
        if key not in dedup or item["score"] > dedup[key]["score"]:
            dedup[key] = item
    candidates = list(dedup.values())[: max(1, limit)]
    return {
        "line_item_description": desc,
        "amount": None,
        "vendor_name": vendor,
        "ai_suggested_gl": ai_gl,
        "candidates": candidates,
        "needs_confirmation": True,
    }


def property_candidates(
    *,
    query: str = "",
    service_address: str = "",
    limit: int = 8,
) -> dict[str, Any]:
    needle = normalize_key(query)
    address_needle = normalize_key(service_address)
    scored: list[dict[str, Any]] = []
    for prop in _load_property_rows():
        score, reason = _property_score(needle, address_needle, prop)
        if score <= 0:
            continue
        scored.append({
            "property_abbreviation": prop["property_abbreviation"],
            "property_name": prop["property_name"],
            "location": prop["location"],
            "address": prop["address"],
            "score": round(min(score, 0.99), 3),
            "reason": reason,
        })
    dedup: dict[tuple[str, str], dict[str, Any]] = {}
    for item in sorted(scored, key=lambda x: x["score"], reverse=True):
        key = (item["property_abbreviation"], item["location"])
        if key not in dedup:
            dedup[key] = item
    return {
        "query": query,
        "service_address": service_address,
        "candidates": list(dedup.values())[: max(1, limit)],
        "needs_confirmation": True,
    }


def location_candidates(
    *,
    property_abbreviation: str,
    query: str = "",
    limit: int = 20,
) -> dict[str, Any]:
    prop_key = normalize_key(property_abbreviation)
    needle = normalize_key(query)
    rows: list[dict[str, Any]] = []
    for prop in _load_property_rows():
        if normalize_key(prop["property_abbreviation"]) != prop_key:
            continue
        if needle and not any(
            needle in normalize_key(str(prop.get(k) or ""))
            for k in ("location", "address", "property_name")
        ):
            continue
        rows.append({
            "property_abbreviation": prop["property_abbreviation"],
            "property_name": prop["property_name"],
            "location": prop["location"],
            "address": prop["address"],
        })
    return {
        "property_abbreviation": property_abbreviation,
        "query": query,
        "locations": rows[: max(1, limit)],
    }


def validate_property_location(
    *,
    property_abbreviation: str,
    location: str = "",
) -> dict[str, str] | None:
    prop_key = normalize_key(property_abbreviation)
    location_key = normalize_key(location)
    first_prop: dict[str, str] | None = None
    for prop in _load_property_rows():
        if normalize_key(prop["property_abbreviation"]) != prop_key:
            continue
        first_prop = first_prop or prop
        if not location_key or normalize_key(prop["location"]) == location_key:
            return prop
    return first_prop if first_prop and not location_key else None


def save_property_mapping(
    *,
    service_address: str,
    property_abbreviation: str,
    location: str = "",
    confirmed_by: str = "user",
) -> dict[str, Any]:
    prop = validate_property_location(
        property_abbreviation=property_abbreviation,
        location=location,
    )
    if not prop:
        raise ValueError("Selected property/location is not valid.")
    key = mapping_key(service_address or f"{property_abbreviation} {location}")
    entry = {
        "service_address": str(service_address or "").strip(),
        "property_abbreviation": prop["property_abbreviation"],
        "location": prop["location"] if location else "",
        "confirmed_by": confirmed_by,
        "created_at": _now(),
        "updated_at": _now(),
    }
    with _LOCK:
        data = load_learned_mappings()
        data.setdefault("property_mappings", {})[key] = entry
        _save_learned_mappings(data)
    return {"normalized_service_address": key, **entry}


def save_vendor_mapping(
    *,
    detected_vendor: str,
    resman_vendor_name: str,
    vendor_id: str = "",
    confirmed_by: str = "user",
) -> dict[str, Any]:
    selected = _vendor_by_name(resman_vendor_name)
    if not selected:
        raise ValueError(f"Unknown vendor: {resman_vendor_name}")
    key = mapping_key(detected_vendor)
    entry = {
        "detected_vendor": str(detected_vendor or "").strip(),
        "resman_vendor_name": selected["vendor_name"],
        "vendor_id": vendor_id or selected.get("vendor_id") or "",
        "confirmed_by": confirmed_by,
        "created_at": _now(),
        "updated_at": _now(),
    }
    with _LOCK:
        data = load_learned_mappings()
        data.setdefault("vendor_mappings", {})[key] = entry
        _save_learned_mappings(data)
    return {"normalized_detected_vendor": key, **entry}


def save_gl_mapping(
    *,
    vendor_name: str,
    pattern: str,
    gl_account: str,
    confirmed_by: str = "user",
) -> dict[str, Any]:
    account = validate_gl_account(gl_account)
    if not account:
        raise ValueError(f"Invalid GL account: {gl_account}")
    vendor_key = mapping_key(vendor_name)
    pattern_norm = normalize_key(pattern)
    if not pattern_norm:
        raise ValueError("Pattern is required")
    entry = {
        "pattern": pattern_norm,
        "display_pattern": str(pattern or "").strip(),
        "gl_account": account["gl_code"],
        "gl_code": account["gl_code"],
        "gl_name": account["gl_name"],
        "confirmed_by": confirmed_by,
        "created_at": _now(),
        "updated_at": _now(),
    }
    with _LOCK:
        data = load_learned_mappings()
        vendor_bucket = data.setdefault("gl_mappings", {}).setdefault(
            vendor_key,
            {
                "vendor_name": str(vendor_name or "").strip(),
                "item_patterns": [],
            },
        )
        items = list(vendor_bucket.get("item_patterns") or [])
        replaced = False
        for idx, existing in enumerate(items):
            if existing.get("pattern") == pattern_norm:
                items[idx] = entry
                replaced = True
                break
        if not replaced:
            items.append(entry)
        vendor_bucket["item_patterns"] = items
        data["gl_mappings"][vendor_key] = vendor_bucket
        _save_learned_mappings(data)
    return {"vendor_key": vendor_key, **entry}


def learned_vendor_mapping(detected_vendor: str) -> dict[str, Any] | None:
    data = load_learned_mappings()
    item = (data.get("vendor_mappings") or {}).get(mapping_key(detected_vendor))
    return dict(item) if isinstance(item, dict) else None


def learned_gl_mapping(vendor_name: str, line_item_description: str) -> dict[str, Any] | None:
    data = load_learned_mappings()
    desc = normalize_key(line_item_description)
    if not desc:
        return None
    buckets = data.get("gl_mappings") or {}
    keys = [mapping_key(vendor_name), "default"]
    for key in keys:
        bucket = buckets.get(key) if isinstance(buckets, dict) else None
        if not isinstance(bucket, dict):
            continue
        for item in bucket.get("item_patterns") or []:
            if not isinstance(item, dict):
                continue
            pat = normalize_key(item.get("pattern") or item.get("display_pattern") or "")
            if pat and (pat in desc or desc in pat):
                return dict(item)
    return None


def apply_learned_mappings_to_normalized(normalized: dict[str, Any]) -> dict[str, Any]:
    """Apply user-confirmed vendor/GL mappings to a normalized AI invoice."""
    applied: list[dict[str, Any]] = []
    detected_vendor = (
        str(normalized.get("raw_vendor_name") or "")
        or str(normalized.get("vendor_name") or "")
    )
    vendor_mapping = learned_vendor_mapping(detected_vendor)
    if vendor_mapping:
        normalized["vendor_name"] = vendor_mapping["resman_vendor_name"]
        applied.append({
            "kind": "vendor_mapping",
            "detected_vendor": detected_vendor,
            "resman_vendor_name": vendor_mapping["resman_vendor_name"],
        })
        _remove_review_code(normalized, "vendor_mapping_required")
        _remove_review_code(normalized, "vendor_mapping_not_found")

    vendor_for_gl = str(normalized.get("vendor_name") or detected_vendor)
    gl_applied = False
    for item in normalized.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        desc = str(item.get("description") or "")
        mapping = learned_gl_mapping(vendor_for_gl, desc)
        if mapping:
            account = validate_gl_account(mapping.get("gl_account") or mapping.get("gl_code") or "")
            if not account or not is_payable_gl_account(account):
                continue
            item["gl_account_candidate"] = account["gl_code"]
            item["gl_mapping_confirmed"] = True
            item["gl_mapping_reason"] = f"User-confirmed mapping: {mapping.get('display_pattern') or mapping.get('pattern')}"
            applied.append({
                "kind": "gl_mapping",
                "pattern": mapping.get("display_pattern") or mapping.get("pattern"),
                "gl_account": account["gl_code"],
                "line_item_description": desc,
            })
            gl_applied = True
    if gl_applied and all(
        bool((item or {}).get("gl_account_candidate"))
        for item in normalized.get("line_items") or []
        if isinstance(item, dict)
    ):
        _remove_review_code(normalized, "gl_mapping_required")
        _remove_review_code(normalized, "ambiguous_gl_mapping")
    if applied:
        normalized.setdefault("mapping_provenance", []).extend(applied)
    return normalized


def validate_gl_account(value: str) -> dict[str, str] | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    code_match = re.search(r"\b\d{3,6}\b", raw)
    norm = normalize_key(raw)
    for account in _load_gl_accounts():
        code = account["gl_code"]
        name = account["gl_name"]
        if code_match and code == code_match.group(0):
            return account
        if norm and normalize_key(name) == norm:
            return account
        if norm and normalize_key(f"{code} {name}") == norm:
            return account
    return None


def resolve_vendor_name(value: str) -> dict[str, str] | None:
    """Return a Vendor List row for an exact normalized vendor name."""
    return _vendor_by_name(value)


def load_learned_mappings() -> dict[str, Any]:
    path = LEARNED_MAPPINGS_PATH
    if not path.is_file():
        return {"vendor_mappings": {}, "gl_mappings": {}, "property_mappings": {}}
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            return {"vendor_mappings": {}, "gl_mappings": {}, "property_mappings": {}}
        data.setdefault("vendor_mappings", {})
        data.setdefault("gl_mappings", {})
        data.setdefault("property_mappings", {})
        return data
    except Exception:
        return {"vendor_mappings": {}, "gl_mappings": {}, "property_mappings": {}}


def _save_learned_mappings(data: dict[str, Any]) -> None:
    import yaml  # type: ignore

    path = LEARNED_MAPPINGS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        try:
            shutil.copy2(path, backup)
        except OSError:
            pass
    payload = {
        "vendor_mappings": dict(data.get("vendor_mappings") or {}),
        "gl_mappings": dict(data.get("gl_mappings") or {}),
        "property_mappings": dict(data.get("property_mappings") or {}),
    }
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        delete=False,
        dir=str(path.parent),
        prefix=".ai_learned_mappings_",
        suffix=".tmp",
    ) as fh:
        tmp = Path(fh.name)
        yaml.safe_dump(payload, fh, sort_keys=True, allow_unicode=False)
    tmp.replace(path)


def _vendor_score(needle: str, vendor_name: str, vendor: dict[str, str]) -> tuple[float, str]:
    candidate = normalize_key(vendor_name)
    if not needle or not candidate:
        return 0, ""
    if needle == candidate:
        return 0.98, "Exact normalized match"
    abbreviation = normalize_key(vendor.get("vendor_id") or "")
    if abbreviation and needle == abbreviation:
        return 0.96, "Company abbreviation match"
    if needle in candidate or candidate in needle:
        return 0.88, "Contained normalized name"
    ratio = difflib.SequenceMatcher(None, needle, candidate).ratio()
    token_score = _token_overlap(needle, candidate)
    score = max(ratio * 0.86, token_score * 0.90)
    if vendor.get("active", "").lower() == "yes":
        score += 0.02
    if score >= 0.55:
        reason = "Fuzzy name match" if ratio >= token_score else "Token overlap match"
        return score, reason
    return 0, ""


def _gl_score(desc_norm: str, account: dict[str, str]) -> tuple[float, str]:
    if not desc_norm:
        return 0, ""
    gl_norm = account.get("_gl_norm") or normalize_key(f"{account['gl_code']} {account['gl_name']}")
    name_norm = account.get("_name_norm") or normalize_key(account["gl_name"])
    if name_norm and name_norm in desc_norm:
        return 0.88, "GL name appears in line item"

    internet_terms = (
        "internet",
        "fiber",
        "broadband",
        "fi speed",
        "wifi",
        "wi fi",
        "smart network",
    )
    phone_terms = ("telephone", "phone", "voice", "telecom")
    cable_terms = ("cable", "television", "tv")
    if any(_contains_normalized_term(desc_norm, term) for term in internet_terms):
        if "internet" in name_norm:
            return 0.94, "Service keyword match: internet/fiber"
        if "telephone" in name_norm and any(_contains_normalized_term(desc_norm, term) for term in phone_terms):
            return 0.86, "Service keyword match: telecom/phone"
        if "cable" in name_norm and any(_contains_normalized_term(desc_norm, term) for term in cable_terms):
            return 0.84, "Service keyword match: cable"
    if any(_contains_normalized_term(desc_norm, term) for term in phone_terms) and "telephone" in name_norm:
        return 0.93, "Service keyword match: telephone"
    if any(_contains_normalized_term(desc_norm, term) for term in cable_terms) and "cable" in name_norm:
        return 0.92, "Service keyword match: cable"

    keyword_map = {
        "landscap": ("landscape",),
        "lawn": ("landscape", "lawn", "trees", "shrubs"),
        "limb": ("landscape", "lawn", "trees", "shrubs"),
        "tree": ("landscape", "lawn", "trees", "shrubs"),
        "shrub": ("landscape", "lawn", "trees", "shrubs"),
        "mow": ("landscape", "lawn"),
        "paint": ("paint", "painting"),
        "hardware": ("hardware", "building", "maintenance", "repair"),
        "bar": ("hardware", "building", "maintenance", "repair"),
        "pull": ("hardware", "building", "maintenance", "repair"),
        "mailbox": ("hardware", "building", "maintenance", "repair"),
        "lock": ("hardware", "building", "maintenance", "repair"),
        "bulb": ("light", "bulb", "fixture"),
        "light": ("light", "bulb", "fixture"),
        "lighting": ("light", "bulb", "fixture"),
        "cfl": ("light", "bulb", "fixture"),
        "fixture": ("light", "bulb", "fixture"),
        "water heater": ("water heater", "plumbing"),
        "wtr htr": ("water heater", "plumbing"),
        "htr": ("water heater", "plumbing"),
        "flex": ("plumbing", "maintenance", "repair"),
        "repair": ("maintenance", "repair"),
        "appliance": ("appliance",),
        "floor": ("floor", "carpet", "vinyl"),
        "plumb": ("plumbing",),
        "hvac": ("heating", "air conditioning", "hvac"),
        "door": ("hardware", "building", "maintenance", "repair"),
    }
    for item_key, gl_keys in keyword_map.items():
        if item_key in desc_norm and any(k in name_norm for k in gl_keys):
            if item_key in {"landscap", "lawn", "limb", "tree", "shrub", "mow"} and account.get("gl_code") == "6810":
                return 0.94, f"Keyword match: {item_key} + landscape contract"
            if item_key in {"hardware", "bar", "pull", "door", "mailbox", "lock"} and "hardware" in name_norm:
                return 0.90, f"Keyword match: {item_key} + hardware"
            if item_key in {"bulb", "light", "lighting", "cfl", "fixture"} and any(
                k in name_norm for k in ("light", "bulb", "fixture")
            ):
                return 0.91, f"Keyword match: {item_key} + lighting"
            if item_key == "paint" and account.get("gl_code") == "6770":
                return 0.91, "Keyword match: paint supplies"
            if item_key in {"water heater", "wtr htr", "htr"} and "water heater" in name_norm:
                return 0.90, f"Keyword match: {item_key} + water heater"
            return 0.82, f"Keyword match: {item_key}"

    token = _token_overlap(desc_norm, name_norm)
    if token <= 0 and gl_norm and not any(word in gl_norm for word in desc_norm.split()):
        return 0, ""
    ratio = difflib.SequenceMatcher(None, desc_norm, name_norm).ratio()
    score = max(token * 0.78, ratio * 0.55)
    if score >= 0.38:
        return score, "Description similarity"
    return 0, ""


def _contains_normalized_term(haystack: str, term: str) -> bool:
    needle = normalize_key(term)
    if not needle:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack) is not None


def _token_overlap(left: str, right: str) -> float:
    a = set(left.split())
    b = set(right.split())
    if not a or not b:
        return 0
    return len(a & b) / len(a | b)


def _candidate_from_account(account: dict[str, str], score: float, reason: str) -> dict[str, Any]:
    if account.get("gl_account_type") == "Expense":
        score += 0.03
    return {
        "gl_account": account["gl_code"],
        "gl_code": account["gl_code"],
        "gl_name": account["gl_name"],
        "gl_account_type": account.get("gl_account_type", ""),
        "score": round(min(score, 0.99), 3),
        "reason": reason,
        "learned": False,
        "valid": True,
    }


def is_payable_gl_account(account: dict[str, str]) -> bool:
    account_type = normalize_key(account.get("gl_account_type", ""))
    if not account_type:
        return True
    return "expense" in account_type and "asset" not in account_type


@lru_cache(maxsize=1)
def _load_vendor_list() -> list[dict[str, str]]:
    path = settings.PROJECT_ROOT / "Vendors" / "Vendor List.csv"
    rows: list[dict[str, str]] = []
    if not path.is_file():
        return rows
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            with open(path, "r", encoding=encoding, newline="") as fh:
                parsed = []
                for row in csv.DictReader(fh):
                    name = str(row.get("Vendor") or "").strip()
                    if not name:
                        continue
                    parsed.append({
                        "vendor_name": name,
                        "vendor_id": str(row.get("Company Abbreviation") or "").strip(),
                        "default_gl": str(row.get("Default GL") or "").strip(),
                        "status": str(row.get("Status") or "").strip(),
                        "active": str(row.get("Active") or "").strip(),
                    })
                return parsed
        except (OSError, UnicodeDecodeError):
            continue
    return rows


def _vendor_by_name(name: str) -> dict[str, str] | None:
    target = normalize_key(name)
    for vendor in _load_vendor_list():
        if normalize_key(vendor["vendor_name"]) == target:
            return vendor
    return None


@lru_cache(maxsize=1)
def _load_property_rows() -> list[dict[str, str]]:
    raw_rows: list[dict[str, str]] = []
    for path in (
        settings.PROJECT_ROOT / "Properties" / "Unit Info Clean.csv",
        settings.PROJECT_ROOT / "Properties" / "Properties.csv",
    ):
        if not path.is_file():
            continue
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as fh:
                for row in csv.DictReader(fh):
                    abbr = str(row.get("Property Abbreviation") or "").strip()
                    name = str(row.get("Property Name") or "").strip()
                    location = str(
                        row.get("Unit Number")
                        or row.get("Unit")
                        or row.get("Location")
                        or ""
                    ).strip()
                    address = str(row.get("Address") or row.get("Service Address") or "").strip()
                    if not abbr and not name:
                        continue
                    raw_rows.append({
                        "property_abbreviation": abbr,
                        "property_name": name,
                        "location": location,
                        "address": address,
                    })
        except (OSError, UnicodeDecodeError):
            continue
    abbreviation_by_name = {
        normalize_key(row["property_name"]): row["property_abbreviation"]
        for row in raw_rows
        if row["property_name"] and row["property_abbreviation"]
    }
    rows: list[dict[str, str]] = []
    for row in raw_rows:
        enriched = dict(row)
        if not enriched["property_abbreviation"] and enriched["property_name"]:
            enriched["property_abbreviation"] = abbreviation_by_name.get(
                normalize_key(enriched["property_name"]),
                "",
            )
        rows.append(enriched)
    return rows


def _property_score(
    needle: str,
    address_needle: str,
    prop: dict[str, str],
) -> tuple[float, str]:
    abbr = normalize_key(prop.get("property_abbreviation", ""))
    name = normalize_key(prop.get("property_name", ""))
    location = normalize_key(prop.get("location", ""))
    address = normalize_key(prop.get("address", ""))
    score = 0.0
    reason = ""
    if needle:
        if needle == abbr:
            return 0.96, "Exact property abbreviation"
        if needle == name:
            return 0.92, "Exact property name"
        if name and (needle in name or name in needle):
            score = max(score, 0.74)
            reason = "Property name contains search text"
        if location and needle == location:
            score = max(score, 0.70)
            reason = "Exact location match"
    if address_needle and address:
        if address_needle == address:
            return 0.94, "Exact service address"
        if address in address_needle or address_needle in address:
            score = max(score, 0.78)
            reason = "Service address match"
    return score, reason


def _vendor_default_gl(vendor_name: str) -> str:
    vendor = _vendor_by_name(vendor_name)
    return (vendor or {}).get("default_gl", "")


@lru_cache(maxsize=1)
def _load_gl_accounts() -> list[dict[str, str]]:
    path = settings.GENERAL_LEDGER_REFERENCE
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    out: list[dict[str, str]] = []
    for item in data.get("detected_gl_accounts") or []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("gl_code") or item.get("code") or "").strip()
        name = str(
            item.get("chart_of_accounts_description")
            or item.get("gl_description")
            or item.get("description")
            or ""
        ).strip()
        account_type = str(item.get("gl_account_type") or "").strip()
        if not code or not name:
            continue
        if account_type and account_type not in {
            "Expense",
            "Non-Operating Expense",
            "Fixed Asset",
            "Other Current Asset",
        }:
            continue
        out.append({
            "gl_code": code,
            "gl_name": name,
            "gl_account_type": account_type,
            "_name_norm": normalize_key(name),
            "_gl_norm": normalize_key(f"{code} {name}"),
        })
    return out


def _remove_review_code(normalized: dict[str, Any], code: str) -> None:
    issues = [
        issue
        for issue in normalized.get("manual_review_issues") or []
        if not (isinstance(issue, dict) and issue.get("code") == code)
    ]
    normalized["manual_review_issues"] = issues
    normalized["manual_review_codes"] = [
        c for c in normalized.get("manual_review_codes") or [] if c != code
    ]
    normalized["manual_review_reasons"] = [
        issue.get("message")
        for issue in issues
        if isinstance(issue, dict) and issue.get("message")
    ]


def _now() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).isoformat(timespec="seconds")


__all__ = [
    "LEARNED_MAPPINGS_PATH",
    "apply_learned_mappings_to_normalized",
    "gl_candidates",
    "learned_gl_mapping",
    "learned_vendor_mapping",
    "location_candidates",
    "load_learned_mappings",
    "mapping_key",
    "normalize_key",
    "property_candidates",
    "resolve_vendor_name",
    "save_gl_mapping",
    "save_property_mapping",
    "save_vendor_mapping",
    "validate_gl_account",
    "validate_property_location",
    "vendor_candidates",
]
