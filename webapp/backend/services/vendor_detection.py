"""Light heuristic vendor router for Phase 1.

Only Richmond Utilities is wired into a backend processor in Phase 1, but
the detection logic returns a `vendor_key` plus a confidence so the UI can
present a manual-pick dropdown when confidence is low. Future phases can
extend `_detectors` without changing the API contract.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Callable, Iterable


# ---------------------------------------------------------------------------
# Per-vendor heuristic functions
# ---------------------------------------------------------------------------
def _looks_like_richmond_utilities(path: Path) -> tuple[bool, float, str]:
    """Returns (matches, confidence_0_to_1, reason)."""
    name = path.name
    # Strong signal: filename of the form "<digits>_<digits>_BillingHistory_Recent..."
    if re.match(r"^\d{4,}_\d+_BillingHistory.*", name, re.IGNORECASE):
        return True, 0.95, "filename matches Richmond Utilities billing-history pattern"
    if "BillingHistory_Recent" in name:
        return True, 0.85, "filename contains 'BillingHistory_Recent'"
    # Light signal: column header in a CSV.
    if path.suffix.lower() == ".csv":
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.reader(f)
                header = next(reader, [])
                norm = [h.strip().lower() for h in header]
                if "transaction" in norm and "service" in norm and "meter number" in norm:
                    return True, 0.85, "CSV header has Transaction/Service/Meter Number (Richmond pattern)"
        except Exception:
            pass
    # PDF path: filename hint OR embedded text scan.
    if path.suffix.lower() == ".pdf":
        lname = name.lower()
        if "richmond" in lname:
            return True, 0.9, "PDF filename contains 'richmond'"
        # Light text-layer scan (digital PDFs only — keep this cheap; we don't
        # OCR during detection. Scanned PDFs will fall through to "unknown"
        # at detect time but the Richmond processor will still accept them
        # if the operator routes them there.)
        try:
            import pdfplumber  # type: ignore
            with pdfplumber.open(path) as pdf:
                if pdf.pages:
                    sample = (pdf.pages[0].extract_text() or "")[:2000]
                    if "Richmond Utilities" in sample or "richmondutilities.com" in sample.lower():
                        return True, 0.9, "PDF text layer mentions Richmond Utilities"
        except Exception:
            pass
    return False, 0.0, ""


def _looks_like_hopkinsville_water(path: Path) -> tuple[bool, float, str]:
    """Detect Hopkinsville Water Environment Authority PDFs.

    Two cheap signals (no OCR — that's a UI hot path):
      * Filename hint: `HWEA` or `Hopkinsville` in the name (lowest signal —
        many `UtilityBill (NN).pdf` files in the HWEA training folder are
        actually misfiled City of Henderson Electric, so filename alone
        is weak).
      * PDF text-layer scan: first page contains "Hopkinsville Water
        Environment Authority", "hwea-ky.com", or "(270) 887-4246". This
        is the strong signal. Misfiled Henderson PDFs DO contain "City of
        Henderson" but lack the HWEA keywords, so the order of checks
        matters: positive HWEA wins; otherwise a Henderson hit returns
        unknown so the file isn't routed to HWEA.
    """
    if path.suffix.lower() != ".pdf":
        return False, 0.0, ""
    name_lower = path.name.lower()
    fname_hint = ("hwea" in name_lower) or ("hopkinsville" in name_lower)
    # Try a cheap text-layer scan. Scanned PDFs return empty text — those
    # match only on filename hint and get a slightly lower confidence.
    text_sample = ""
    try:
        import pdfplumber  # type: ignore
        with pdfplumber.open(path) as pdf:
            if pdf.pages:
                text_sample = (pdf.pages[0].extract_text() or "")[:3000]
    except Exception:
        text_sample = ""
    text_lower = text_sample.lower()
    has_hwea_kw = (
        "hopkinsville water environment" in text_lower
        or "hwea-ky" in text_lower
        or "(270) 887-4246" in text_sample
    )
    has_henderson_kw = "city of henderson" in text_lower
    if has_hwea_kw:
        return True, 0.95, "PDF text mentions Hopkinsville Water Environment Authority"
    if fname_hint and has_henderson_kw:
        return False, 0.0, "PDF filename suggests HWEA but content is City of Henderson"
    if fname_hint and not text_sample:
        # Scanned PDF in the HWEA folder — accept on filename but lower confidence.
        return True, 0.7, "PDF filename starts with HWEA / Hopkinsville (scanned, OCR will confirm)"
    return False, 0.0, ""


# Order matters: first detector to claim ownership wins.
_DETECTORS: list[tuple[str, Callable[[Path], tuple[bool, float, str]]]] = [
    ("richmond_utilities", _looks_like_richmond_utilities),
    ("hopkinsville_water_environment_authority", _looks_like_hopkinsville_water),
]


SUPPORTED_VENDOR_KEYS = {
    "richmond_utilities",
    "hopkinsville_water_environment_authority",
}


def detect_vendor_for_file(path: Path) -> dict:
    """Return a dict with detected vendor_key, confidence, reason. If no
    detector claims the file, vendor_key is 'unknown' and the UI should let
    the operator pick from a manual list."""
    for vendor_key, fn in _DETECTORS:
        try:
            ok, conf, reason = fn(path)
        except Exception as e:
            ok, conf, reason = False, 0.0, f"detector_error:{type(e).__name__}"
        if ok:
            return {
                "vendor_key": vendor_key,
                "confidence": conf,
                "reason": reason,
                "supported_in_phase_1": vendor_key in SUPPORTED_VENDOR_KEYS,
            }
    return {
        "vendor_key": "unknown",
        "confidence": 0.0,
        "reason": "no_detector_claimed_this_file",
        "supported_in_phase_1": False,
    }


def detect_vendors_for_files(paths: Iterable[Path]) -> dict[str, dict]:
    """Convenience: return a {filename: detection_dict} map."""
    return {p.name: detect_vendor_for_file(p) for p in paths}
