"""Phase 2L — Shared property + unit lookup over ``Unit Info Clean.csv``.

A small, self-contained matcher that vendor processors can call without
pulling in the larger HWEA / Richmond unit-directory machinery. Builds
two indices the first time it's used and caches them at module level:

* ``by_address``  — normalized street → list of unit rows
* ``by_property`` — property abbreviation → set of valid unit numbers

Match strategies
----------------
1. **(street_number, unit_hint, street_name)** parsed from the bill's
   "Service Location" → exact match against (Address, Unit Number).
2. **(street_number, street_name)** alone → exact match against the
   building (Address) and pick the first unit. Useful for whole-
   building meters that don't carry a unit suffix.
3. **(street_number_alpha)** like "405-B" → match Unit Number directly
   when the bill prints the unit suffix as part of the street_number
   slot.

Returns ``UnitMatch(property_abbreviation, property_name,
unit_number, address, strategy)`` or None.
"""

from __future__ import annotations

import csv
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


_LOG = logging.getLogger(__name__)
_LOCK = threading.RLock()


@dataclass
class UnitMatch:
    property_abbreviation: str
    property_name: str
    unit_number: str
    address: str
    strategy: str
    confidence: float = 1.0


@dataclass
class _UnitRow:
    property_name: str
    property_abbreviation: str
    unit_number: str
    address: str
    city: str = ""
    state: str = ""
    zip_code: str = ""


_INDEX_BY_ADDR: Optional[dict[str, list[_UnitRow]]] = None
_INDEX_BY_PROP_UNIT: Optional[dict[tuple[str, str], _UnitRow]] = None
# Phase 2L — index keyed by *street name only* (no number). Used to
# resolve bills whose Service Address prints a per-building street
# number (e.g. "2116 OAK TREE VILLA DR APT C") that doesn't appear in
# Unit Info Clean's Address column ("Oak Tree Villa Drive"). Match
# strategy then composes a unit hint like "2116C" and looks for that.
_INDEX_BY_STREETNAME: Optional[dict[str, list[_UnitRow]]] = None
_INDEX_PATH: Optional[Path] = None
_PROPERTY_BY_NAME: Optional[dict[str, tuple[str, str]]] = None
_PROPERTY_NAME_PATH: Optional[Path] = None


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _csv_path() -> Path:
    return _project_root() / "Properties" / "Unit Info Clean.csv"


def _property_abbreviations_by_name() -> dict[str, str]:
    """Load property abbreviations when Unit Info Clean omits that column."""

    path = _project_root() / "Properties" / "Properties.csv"
    if not path.is_file():
        return {}
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            with open(path, "r", encoding=enc, newline="") as f:
                result: dict[str, str] = {}
                for row in csv.DictReader(f):
                    name = (row.get("Property Name") or "").strip()
                    abbreviation = (row.get("Property Abbreviation") or "").strip()
                    if name and abbreviation:
                        result.setdefault(name.casefold(), abbreviation)
                return result
        except UnicodeDecodeError:
            continue
        except Exception as exc:  # pragma: no cover
            _LOG.warning("Failed to read property abbreviations from %s: %s", path, exc)
            return {}
    return {}


def _normalize_property_name(value: str) -> str:
    """Normalize display-name variations without weakening address matching."""

    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(r"\s*\([^)]*\)\s*$", "", text).strip()
    trailing_article = re.match(r"^(.*),\s*(the|a|an)$", text, re.I)
    if trailing_article:
        text = f"{trailing_article.group(2)} {trailing_article.group(1)}"
    text = re.sub(r"[^a-z0-9]+", " ", text.casefold())
    return re.sub(r"\s+", " ", text).strip()


def _property_records_by_name() -> dict[str, tuple[str, str]]:
    path = _project_root() / "Properties" / "Properties.csv"
    if not path.is_file():
        return {}
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            with open(path, "r", encoding=enc, newline="") as f:
                result: dict[str, tuple[str, str]] = {}
                for row in csv.DictReader(f):
                    name = (row.get("Property Name") or "").strip()
                    abbreviation = (row.get("Property Abbreviation") or "").strip()
                    key = _normalize_property_name(name)
                    if key and abbreviation:
                        result.setdefault(key, (abbreviation, name))
                return result
        except UnicodeDecodeError:
            continue
        except Exception as exc:  # pragma: no cover
            _LOG.warning("Failed to read property names from %s: %s", path, exc)
            return {}
    return {}


def _ensure_property_names_loaded() -> None:
    global _PROPERTY_BY_NAME, _PROPERTY_NAME_PATH
    with _LOCK:
        path = _project_root() / "Properties" / "Properties.csv"
        if _PROPERTY_BY_NAME is not None and _PROPERTY_NAME_PATH == path:
            return
        _PROPERTY_BY_NAME = _property_records_by_name()
        _PROPERTY_NAME_PATH = path


# ---------------------------------------------------------------------------
# Address normalization
# ---------------------------------------------------------------------------

_SUFFIX_REPL = {
    "drive": "Dr", "dr.": "Dr", "dr": "Dr",
    "street": "St", "st.": "St", "st": "St",
    "road": "Rd", "rd.": "Rd", "rd": "Rd",
    "avenue": "Ave", "ave.": "Ave", "ave": "Ave",
    "court": "Ct", "ct.": "Ct", "ct": "Ct",
    "lane": "Ln", "ln.": "Ln", "ln": "Ln",
    "boulevard": "Blvd", "blvd.": "Blvd", "blvd": "Blvd",
    "place": "Pl", "pl.": "Pl", "pl": "Pl",
    "circle": "Cir", "cir.": "Cir", "cir": "Cir",
    "highway": "Hwy", "hwy.": "Hwy", "hwy": "Hwy",
    "parkway": "Pkwy", "pkwy.": "Pkwy", "pkwy": "Pkwy",
    "way": "Way",
    "terrace": "Ter", "ter.": "Ter", "ter": "Ter",
    "trail": "Trl", "trl.": "Trl", "trl": "Trl",
    "north": "N", "n.": "N",
    "south": "S", "s.": "S",
    "east": "E", "e.": "E",
    "west": "W", "w.": "W",
    "northwest": "NW", "nw.": "NW",
    "northeast": "NE", "ne.": "NE",
    "southwest": "SW", "sw.": "SW",
    "southeast": "SE", "se.": "SE",
}


def _normalize_token(t: str) -> str:
    raw = t.strip(" .,").lower()
    return _SUFFIX_REPL.get(raw, t.strip(" .,"))


def normalize_street(street: str) -> str:
    """Lower-case, strip punctuation, expand common suffixes to their
    canonical short form. ``101 McMeen Drive`` and ``101 mcmeen dr``
    both normalize to ``101 mcmeen dr``."""
    if not street:
        return ""
    parts = [_normalize_token(p) for p in re.split(r"\s+", street.strip())]
    return " ".join(p.lower() for p in parts if p)


# ---------------------------------------------------------------------------
# Index build
# ---------------------------------------------------------------------------


def _build_index() -> tuple[
    dict[str, list[_UnitRow]],
    dict[tuple[str, str], _UnitRow],
    dict[str, list[_UnitRow]],
]:
    by_addr: dict[str, list[_UnitRow]] = {}
    by_pu: dict[tuple[str, str], _UnitRow] = {}
    by_street_only: dict[str, list[_UnitRow]] = {}
    property_abbreviations = _property_abbreviations_by_name()
    path = _csv_path()
    if not path.is_file():
        _LOG.warning("Unit Info Clean not found at %s", path)
        return by_addr, by_pu, by_street_only
    try:
        for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
            try:
                with open(path, "r", encoding=enc, newline="") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        property_name = (row.get("Property Name") or "").strip()
                        ur = _UnitRow(
                            property_name=property_name,
                            property_abbreviation=(
                                (row.get("Property Abbreviation") or "").strip()
                                or property_abbreviations.get(property_name.casefold(), "")
                            ),
                            unit_number=(row.get("Unit Number") or "").strip(),
                            address=(row.get("Address") or "").strip(),
                            city=(row.get("City") or "").strip(),
                            state=(row.get("State") or "").strip(),
                            zip_code=(row.get("Zip") or "").strip(),
                        )
                        if not ur.address or not ur.property_abbreviation:
                            continue
                        full_key = normalize_street(ur.address)
                        by_addr.setdefault(full_key, []).append(ur)
                        by_pu[(ur.property_abbreviation.upper(), ur.unit_number.upper())] = ur
                        # Street-name-only index: drop any leading digits
                        # of the address (e.g. "513 Pinecrest Street" →
                        # "pinecrest st") so a bill that prints its own
                        # building number can still resolve.
                        sn_only_key = _strip_leading_number(full_key)
                        if sn_only_key and sn_only_key != full_key:
                            by_street_only.setdefault(sn_only_key, []).append(ur)
                        elif sn_only_key:
                            by_street_only.setdefault(sn_only_key, []).append(ur)
                if by_addr:
                    break
                by_addr, by_pu, by_street_only = {}, {}, {}
            except UnicodeDecodeError:
                continue
    except Exception as e:  # pragma: no cover
        _LOG.warning("Failed to read Unit Info Clean: %s", e)
    return by_addr, by_pu, by_street_only


def _strip_leading_number(key: str) -> str:
    if not key:
        return ""
    parts = key.split(" ", 1)
    if len(parts) == 2 and parts[0].isdigit():
        return parts[1].strip()
    return key.strip()


def _ensure_loaded() -> None:
    global _INDEX_BY_ADDR, _INDEX_BY_PROP_UNIT, _INDEX_BY_STREETNAME, _INDEX_PATH
    with _LOCK:
        path = _csv_path()
        if _INDEX_BY_ADDR is not None and _INDEX_PATH == path:
            return
        (
            _INDEX_BY_ADDR,
            _INDEX_BY_PROP_UNIT,
            _INDEX_BY_STREETNAME,
        ) = _build_index()
        _INDEX_PATH = path


# ---------------------------------------------------------------------------
# Bill-address parsing
# ---------------------------------------------------------------------------


def parse_bill_address(raw: str) -> tuple[str, str, str]:
    """Pull (street_number, unit_hint, street_name) out of a typical
    utility-bill service-location string.

    Handles these layouts:

      * ``"405-B CALDWELL DR"``        → ("405", "405-B",   "CALDWELL DR")
      * ``"413-17 CALDWELL DR"``       → ("413", "17",      "CALDWELL DR")
      * ``"101-2 MCMEEN DR"``          → ("101", "2",       "MCMEEN DR")
      * ``"1200 DENZIL DR 6"``         → ("1200", "6",      "DENZIL DR")
      * ``"2629 Kenwood Dr Apt 1"``    → ("2629", "1",      "Kenwood Dr")
      * ``"304 Griffin Gate Dr B-09"`` → ("304", "B-09",    "Griffin Gate Dr")
      * ``"100 Main St"``              → ("100", "",        "Main St")

    Empty inputs return three empty strings.
    """
    if not raw:
        return "", "", ""
    s = re.sub(r"\s+", " ", raw.strip())

    # Split off explicit "Apt N" / "Unit N" / "# N" suffixes first.
    # Note: the `\b` word boundary intentionally only applies to "apt"
    # and "unit" (both word-char tokens). The `#` symbol is non-word
    # so a `\b` before it would never match — we use an explicit
    # whitespace/start anchor instead.
    explicit_unit = ""
    m = re.search(
        r"(?:\b(?:apt|unit)\s*|(?:^|\s)#\s*)([A-Za-z0-9-]+)\s*$",
        s,
        re.IGNORECASE,
    )
    if m:
        explicit_unit = m.group(1).strip()
        s = s[: m.start()].strip()

    tokens = s.split()
    if not tokens:
        return "", "", ""

    # First token = street number (may include a "-suffix" like "405-B" or "413-17").
    head = tokens[0]
    rest = " ".join(tokens[1:])

    street_number = head
    unit_from_head = ""
    m2 = re.match(r"^(\d+)-([A-Za-z0-9]+)$", head)
    if m2:
        street_number = m2.group(1)
        unit_from_head = head if not m2.group(2).isdigit() else m2.group(2)

    # Trailing numeric-only unit ("DENZIL DR 6").
    trailing_unit = ""
    rest_tokens = rest.split()
    if (
        rest_tokens
        and re.match(r"^[A-Za-z]+-?\d+[A-Za-z]?$", rest_tokens[-1])
    ):
        trailing_unit = rest_tokens[-1]
        rest_tokens = rest_tokens[:-1]
    elif rest_tokens and rest_tokens[-1].isdigit() and len(rest_tokens) >= 2:
        # only treat trailing pure-digit as unit when there's more street
        # before it (e.g. "DENZIL DR 6" but not "DR 6")
        trailing_unit = rest_tokens[-1]
        rest_tokens = rest_tokens[:-1]

    street_name = " ".join(rest_tokens).strip()

    unit_hint = explicit_unit or unit_from_head or trailing_unit
    return street_number, unit_hint, street_name


# ---------------------------------------------------------------------------
# Public lookup
# ---------------------------------------------------------------------------


def match_by_property_name(property_name: str) -> Optional[UnitMatch]:
    """Resolve a property display name against the shared property directory.

    Exact normalized names are preferred. A unique containment match handles
    harmless directory qualifiers such as ``"(Jack Miller)"`` while avoiding
    ambiguous guesses.
    """

    _ensure_property_names_loaded()
    key = _normalize_property_name(property_name)
    if not key:
        return None

    resolved = (_PROPERTY_BY_NAME or {}).get(key)
    strategy = "property_name_exact"
    confidence = 1.0
    if resolved is None:
        candidates = {
            value
            for candidate_key, value in (_PROPERTY_BY_NAME or {}).items()
            if key in candidate_key or candidate_key in key
        }
        if len(candidates) == 1:
            resolved = candidates.pop()
            strategy = "property_name_unique_containment"
            confidence = 0.92
        else:
            try:
                from webapp.backend.services import resman_context_data as context_data
                from webapp.backend.services.tenant_accounting_policies import default_tenant_id

                imported = context_data.find_property_by_name(default_tenant_id(), property_name)
            except Exception:
                imported = None
            abbreviation = str((imported or {}).get("property_code") or "").strip()
            canonical_name = str((imported or {}).get("property_name") or "").strip()
            if not abbreviation or not canonical_name:
                return None
            resolved = (abbreviation, canonical_name)
            strategy = "resman_context_property_name"
            confidence = 1.0

    abbreviation, canonical_name = resolved
    return UnitMatch(
        property_abbreviation=abbreviation,
        property_name=canonical_name,
        unit_number="",
        address="",
        strategy=strategy,
        confidence=confidence,
    )


def match_by_address(
    bill_address: str,
    *,
    expected_property_abbrev: str = "",
) -> Optional[UnitMatch]:
    """Look up a property + unit using the bill's service-location text.

    Returns the best match or None. Strategy:

      1. Parse the bill into (street_number, unit_hint, street_name).
      2. Normalize ``"<street_number> <street_name>"`` and look up
         every unit at that street in the index.
      3. If the parser produced a ``unit_hint``, prefer the exact
         (street, unit) match. Otherwise return the first unit at
         that street (whole-building meter case).
      4. When ``expected_property_abbrev`` is provided, only accept
         units whose property matches.
    """
    _ensure_loaded()
    if _INDEX_BY_ADDR is None:
        return None

    sn, uh, st = parse_bill_address(bill_address)
    if not (sn and st):
        return None
    full = f"{sn} {st}".strip()
    key = normalize_street(full)

    candidates = list(_INDEX_BY_ADDR.get(key) or [])
    if not candidates:
        # Phase 2L — prefix-match fallback. The bill often prints the
        # canonical street followed by a non-unit suffix (e.g.
        # "301 LIGON DR BLDG D HP" or "301 LIGON DR STORAGE BLDG"
        # for common-area meters; "1300 PINE VALLEY DR APT 144" with
        # extra trailing fields after OCR). When the full normalized
        # key isn't in the index, look for any index key whose
        # tokenised form is a PREFIX of ours — that gives us the
        # property even when the bill's suffix isn't a unit. Yields
        # `address_only_property_known` because the unit can't be
        # pinned reliably without an APT/UNIT/# marker.
        target_tokens = key.split()
        for k, rows in _INDEX_BY_ADDR.items():
            k_tokens = k.split()
            # A real prefix of length >= 2 (street_number + at least
            # one street-name token) is required to avoid spurious
            # number-only collisions.
            if (
                len(k_tokens) >= 2
                and len(target_tokens) >= len(k_tokens)
                and target_tokens[: len(k_tokens)] == k_tokens
            ):
                # All rows at this address share the same property —
                # we're confident about the property even if not the
                # specific unit.
                first = rows[0]
                return UnitMatch(
                    property_abbreviation=first.property_abbreviation,
                    property_name=first.property_name,
                    unit_number="",
                    address=first.address,
                    strategy="address_prefix_property_known",
                    confidence=0.7,
                )
    if not candidates:
        # Phase 2L — street-name-only lookup. Some properties (notably
        # Oak Tree Farms) record only the street ("Oak Tree Villa
        # Drive") in Unit Info Clean while the bill prints the
        # building's specific street number ("2116 Oak Tree Villa
        # Dr"). Try matching by street name alone and look for a unit
        # number composed of <building_number><apt_letter>
        # (e.g. "2116" + "C" → "2116C").
        sn_only = _strip_leading_number(normalize_street(st)) or normalize_street(st)
        sn_candidates = list((_INDEX_BY_STREETNAME or {}).get(sn_only) or [])
        if sn_candidates and sn:
            composite_targets = []
            if uh:
                composite_targets.append(f"{sn}{uh}".upper())
                composite_targets.append(f"{sn}-{uh}".upper())
            else:
                composite_targets.append(sn.upper())
            for r in sn_candidates:
                if r.unit_number.upper() in composite_targets:
                    return UnitMatch(
                        property_abbreviation=r.property_abbreviation,
                        property_name=r.property_name,
                        unit_number=r.unit_number,
                        address=r.address,
                        strategy="streetname_composite_unit",
                        confidence=0.92,
                    )
            # No exact composite match; fall through to substring
            # search below so the operator still gets *something*.
            candidates = sn_candidates
        if not candidates:
            # Loose: try matching just the street_name (without number) so
            # a bill that misspells the number can still resolve.
            sub = normalize_street(st)
            for k, rows in _INDEX_BY_ADDR.items():
                if sub and sub in k:
                    candidates.extend(rows)
        if not candidates:
            return None

    if expected_property_abbrev:
        ep = expected_property_abbrev.upper().strip()
        narrowed = [r for r in candidates
                    if r.property_abbreviation.upper() == ep]
        if narrowed:
            candidates = narrowed

    if uh:
        # Try exact unit number match (case-insensitive).
        uhU = uh.upper()
        for r in candidates:
            if r.unit_number.upper() == uhU:
                return UnitMatch(
                    property_abbreviation=r.property_abbreviation,
                    property_name=r.property_name,
                    unit_number=r.unit_number,
                    address=r.address,
                    strategy="address_exact_unit",
                    confidence=0.95,
                )
        # Strip leading zeros and re-try.
        uh_norm = re.sub(r"^0+", "", uh) or "0"
        for r in candidates:
            if r.unit_number.upper().lstrip("0") == uh_norm.upper():
                return UnitMatch(
                    property_abbreviation=r.property_abbreviation,
                    property_name=r.property_name,
                    unit_number=r.unit_number,
                    address=r.address,
                    strategy="address_unit_lz_normalized",
                    confidence=0.85,
                )
        # Some properties encode the building and unit together in Unit
        # Info Clean (for example "115 WALNUT ST 13" -> unit "115-13").
        # The bill parser correctly extracts street_number=115 and
        # unit_hint=13; try those composed forms before falling back to a
        # property-only match.
        composite_targets = {
            f"{sn}-{uh}".upper(),
            f"{sn}{uh}".upper(),
        }
        for r in candidates:
            if r.unit_number.upper() in composite_targets:
                return UnitMatch(
                    property_abbreviation=r.property_abbreviation,
                    property_name=r.property_name,
                    unit_number=r.unit_number,
                    address=r.address,
                    strategy="address_composite_unit",
                    confidence=0.92,
                )

    # Whole-building / no-unit fallback: a unique match, OR the first
    # unit at this street if the property abbrev is unambiguous.
    if len(candidates) == 1:
        r = candidates[0]
        return UnitMatch(
            property_abbreviation=r.property_abbreviation,
            property_name=r.property_name,
            unit_number=r.unit_number,
            address=r.address,
            strategy="address_only_single_unit",
            confidence=0.7,
        )
    abs_set = {r.property_abbreviation for r in candidates}
    if len(abs_set) == 1:
        # Same property; we know the property even if not the unit.
        r = candidates[0]
        return UnitMatch(
            property_abbreviation=r.property_abbreviation,
            property_name=r.property_name,
            unit_number="",  # unit unknown
            address=r.address,
            strategy="address_only_property_known",
            confidence=0.55,
        )
    return None


def lookup_unit(
    property_abbreviation: str,
    unit_candidate: str,
) -> Optional[UnitMatch]:
    """Strict (property_abbreviation, unit_number) lookup against Unit
    Info Clean. Returns the canonical row when found, else None.

    Use this when the caller already knows the property (e.g. routed
    by account-prefix) and just needs to verify the unit string the
    bill prints maps to a real unit in the property directory."""
    _ensure_loaded()
    if _INDEX_BY_PROP_UNIT is None:
        return None
    key = (property_abbreviation.upper().strip(),
           (unit_candidate or "").upper().strip())
    r = _INDEX_BY_PROP_UNIT.get(key)
    if r is None:
        return None
    return UnitMatch(
        property_abbreviation=r.property_abbreviation,
        property_name=r.property_name,
        unit_number=r.unit_number,
        address=r.address,
        strategy="prop_unit_exact",
        confidence=1.0,
    )


__all__ = [
    "UnitMatch",
    "lookup_unit",
    "match_by_address",
    "match_by_property_name",
    "normalize_street",
    "parse_bill_address",
]
