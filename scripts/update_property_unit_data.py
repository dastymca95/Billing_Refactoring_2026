"""Update app property/unit reference CSVs from ResMan exports.

Inputs:
  * Property List export (.xlsx)
  * All Units export (.xlsx)

Outputs:
  * Properties/Properties.csv
  * Properties/Unit Info Clean.csv

The app uses ``Properties.csv`` to resolve property abbreviations and
``Unit Info Clean.csv`` for unit/address validation. The All Units export does
not carry per-unit street addresses, so this script preserves existing
Unit Info Clean address fields when a (property, unit) row already exists and
falls back to the property-level address for newly introduced rows.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd


PROPERTY_HEADERS = [
    "Property Name",
    "Property Abbreviation",
    "Unit",
    "Unit Type",
    "Unit Status",
    "Sq Ft",
    "Lease Status",
    "Residents",
    "Lease Start",
    "Lease End",
    "Market Rent",
    "Market Rent / Sq Ft",
    "Rent",
    "Rent / Sq Ft",
    "Deposits",
]

UNIT_INFO_HEADERS = [
    "Property Name",
    "Building",
    "Unit Number",
    "Address",
    "City",
    "State",
    "Zip",
    "Status",
    "Occupied",
    "Unit Type",
    "Market Rent",
    "Required Deposit",
    "Sq. Feet",
    "Max Occupancy",
    "Beds",
    "Baths",
    "Floor",
    "Amenities",
    "Pets Permitted",
    "Holding Unit",
    "Online Marketing",
    "Hearing Accessible",
    "Mobility Accessible",
    "Visual Accessible",
    "Excluded from Occ.",
    "Excluded from GPR",
]


def _clean(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except TypeError:
        pass
    text = str(value).replace("\u00a0", " ").strip()
    return " ".join(text.split())


def _date_text(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, datetime):
        return f"{value.month}/{value.day}/{value.year}"
    if isinstance(value, date):
        return f"{value.month}/{value.day}/{value.year}"
    text = _clean(value)
    if not text:
        return ""
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return text
    return f"{parsed.month}/{parsed.day}/{parsed.year}"


def _number_text(value: Any, *, decimals: int | None = None, comma: bool = False) -> str:
    text = _clean(value)
    if not text:
        return ""
    try:
        number = float(text.replace(",", ""))
    except ValueError:
        return text
    if decimals is None:
        if abs(number - round(number)) < 0.0000001:
            return f"{int(round(number)):,}" if comma else str(int(round(number)))
        rendered = f"{number:.2f}".rstrip("0").rstrip(".")
        return rendered
    fmt = f"{{:,.{decimals}f}}" if comma else f"{{:.{decimals}f}}"
    return fmt.format(number)


def _property_list_records(path: Path) -> dict[str, dict[str, str]]:
    raw = pd.read_excel(path, sheet_name=0, dtype=object, header=None)
    header_row = None
    for idx, row in raw.iterrows():
        values = [_clean(value).casefold() for value in row.tolist()]
        if "name" in values and "abbreviation" in values and "total units" in values:
            header_row = idx
            break
    if header_row is None:
        raise ValueError(f"Could not find Property List header row in {path}")

    df = pd.read_excel(path, sheet_name=0, dtype=object, header=header_row)
    records: dict[str, dict[str, str]] = {}
    for _, row in df.iterrows():
        name = _clean(row.get("Name"))
        abbreviation = _clean(row.get("Abbreviation"))
        if not name or not abbreviation:
            continue
        if name.casefold() in {"total", "© resman, llc"}:
            continue
        records[name] = {
            "Property Name": name,
            "Property Abbreviation": abbreviation,
            "Address": _clean(row.get("Address")),
            "City": _clean(row.get("City")),
            "State": _clean(row.get("State")),
            "Zip": _number_text(row.get("Zip")),
            "Total Units": _number_text(row.get("Total Units")),
        }
    return records


def _all_unit_rows(path: Path, property_records: dict[str, dict[str, str]]) -> list[dict[str, str]]:
    raw = pd.read_excel(path, sheet_name=0, dtype=object, header=None)
    sections: list[tuple[int, str]] = []
    for idx in range(len(raw) - 1):
        current = _clean(raw.iat[idx, 0])
        next_first = _clean(raw.iat[idx + 1, 0]).casefold()
        if current and next_first == "unit":
            sections.append((idx, current))

    rows: list[dict[str, str]] = []
    for pos, (section_row, property_name) in enumerate(sections):
        end = sections[pos + 1][0] if pos + 1 < len(sections) else len(raw)
        prop = property_records.get(property_name, {})
        abbreviation = prop.get("Property Abbreviation", "")
        for row_idx in range(section_row + 2, end):
            values = raw.iloc[row_idx].tolist()
            unit = _clean(values[0] if len(values) > 0 else "")
            if not unit:
                continue
            # ResMan report subtotal rows have blank unit type/status and
            # aggregate numeric values in the money columns.
            unit_type = _clean(values[1] if len(values) > 1 else "")
            unit_status = _clean(values[2] if len(values) > 2 else "")
            if not unit_type and not unit_status:
                continue
            rows.append(
                {
                    "Property Name": property_name,
                    "Property Abbreviation": abbreviation,
                    "Unit": unit,
                    "Unit Type": unit_type,
                    "Unit Status": unit_status,
                    "Sq Ft": _number_text(values[3] if len(values) > 3 else ""),
                    "Lease Status": _clean(values[5] if len(values) > 5 else ""),
                    "Residents": _clean(values[6] if len(values) > 6 else ""),
                    "Lease Start": _date_text(values[7] if len(values) > 7 else ""),
                    "Lease End": _date_text(values[8] if len(values) > 8 else ""),
                    "Market Rent": _number_text(values[9] if len(values) > 9 else "", decimals=2, comma=True),
                    "Market Rent / Sq Ft": _number_text(values[10] if len(values) > 10 else "", decimals=2),
                    "Rent": _number_text(values[11] if len(values) > 11 else "", decimals=2, comma=True),
                    "Rent / Sq Ft": _number_text(values[12] if len(values) > 12 else "", decimals=2),
                    "Deposits": _number_text(values[13] if len(values) > 13 else "", decimals=2, comma=True),
                }
            )
    return rows


def _read_existing_unit_info(path: Path) -> dict[tuple[str, str], dict[str, str]]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        result: dict[tuple[str, str], dict[str, str]] = {}
        for row in csv.DictReader(handle):
            key = (_clean(row.get("Property Name")).casefold(), _clean(row.get("Unit Number")).casefold())
            if key[0] and key[1]:
                result.setdefault(key, {header: _clean(row.get(header)) for header in UNIT_INFO_HEADERS})
        return result


def _occupied_marker(unit_row: dict[str, str]) -> str:
    lease = unit_row.get("Lease Status", "").casefold()
    if unit_row.get("Residents") or lease in {
        "current",
        "pending",
        "notice to vacate",
        "under eviction",
        "eviction",
        "cancelled",
    }:
        return "[+]"
    return "[-]"


def _unit_info_rows(
    property_rows: list[dict[str, str]],
    property_records: dict[str, dict[str, str]],
    existing: dict[tuple[str, str], dict[str, str]],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for src in property_rows:
        key = (src["Property Name"].casefold(), src["Unit"].casefold())
        old = existing.get(key, {})
        prop = property_records.get(src["Property Name"], {})
        rows.append(
            {
                "Property Name": src["Property Name"],
                "Building": old.get("Building", ""),
                "Unit Number": src["Unit"],
                "Address": old.get("Address") or prop.get("Address", ""),
                "City": old.get("City") or prop.get("City", ""),
                "State": old.get("State") or prop.get("State", ""),
                "Zip": old.get("Zip") or prop.get("Zip", ""),
                "Status": src["Unit Status"],
                "Occupied": old.get("Occupied") or _occupied_marker(src),
                "Unit Type": src["Unit Type"],
                "Market Rent": src["Market Rent"],
                "Required Deposit": old.get("Required Deposit") or src["Deposits"] or "0.00",
                "Sq. Feet": src["Sq Ft"],
                "Max Occupancy": old.get("Max Occupancy", ""),
                "Beds": old.get("Beds", ""),
                "Baths": old.get("Baths", ""),
                "Floor": old.get("Floor", ""),
                "Amenities": old.get("Amenities", ""),
                "Pets Permitted": old.get("Pets Permitted") or "[+]",
                "Holding Unit": old.get("Holding Unit") or ("[+]" if "holding" in src["Unit"].casefold() else "[-]"),
                "Online Marketing": old.get("Online Marketing") or "[+]",
                "Hearing Accessible": old.get("Hearing Accessible") or "[-]",
                "Mobility Accessible": old.get("Mobility Accessible") or "[-]",
                "Visual Accessible": old.get("Visual Accessible") or "[-]",
                "Excluded from Occ.": old.get("Excluded from Occ.") or "[-]",
                "Excluded from GPR": old.get("Excluded from GPR") or "[-]",
            }
        )
    return rows


def _write_csv(path: Path, headers: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({header: row.get(header, "") for header in headers})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all-units", type=Path, required=True)
    parser.add_argument("--property-list", type=Path, required=True)
    parser.add_argument("--properties-csv", type=Path, default=Path("Properties/Properties.csv"))
    parser.add_argument("--unit-info-clean-csv", type=Path, default=Path("Properties/Unit Info Clean.csv"))
    args = parser.parse_args()

    property_records = _property_list_records(args.property_list)
    property_rows = _all_unit_rows(args.all_units, property_records)
    existing_unit_info = _read_existing_unit_info(args.unit_info_clean_csv)
    unit_info_rows = _unit_info_rows(property_rows, property_records, existing_unit_info)

    missing_abbrev = sorted({row["Property Name"] for row in property_rows if not row["Property Abbreviation"]})
    if missing_abbrev:
        raise ValueError(f"Missing property abbreviations for: {missing_abbrev}")

    _write_csv(args.properties_csv, PROPERTY_HEADERS, property_rows)
    _write_csv(args.unit_info_clean_csv, UNIT_INFO_HEADERS, unit_info_rows)

    counts = Counter(row["Property Name"] for row in property_rows)
    print(f"Updated {args.properties_csv} with {len(property_rows)} rows across {len(counts)} properties")
    print(f"Updated {args.unit_info_clean_csv} with {len(unit_info_rows)} rows")
    for name in sorted(counts):
        print(f"{name}: {counts[name]}")


if __name__ == "__main__":
    main()
