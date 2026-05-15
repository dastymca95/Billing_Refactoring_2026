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


def _looks_like_columbia_power_and_water(path: Path) -> tuple[bool, float, str]:
    """Detect Columbia Power and Water System PDFs.

    Strong signals (no OCR — we keep detection cheap):
      * filename hint: ``columbia`` substring (low signal, since other
        vendors share the city name).
      * PDF text-layer scan: page 1 contains "COLUMBIA POWER", "cpws.com",
        the CPWS phone number, or the literal "(931) 388-4833". Any of
        these is decisive.
    """
    if path.suffix.lower() != ".pdf":
        return False, 0.0, ""
    name_lower = path.name.lower()
    fname_hint = "columbia" in name_lower or "cpws" in name_lower
    text_sample = ""
    try:
        import pdfplumber  # type: ignore
        with pdfplumber.open(path) as pdf:
            if pdf.pages:
                text_sample = (pdf.pages[0].extract_text() or "")[:3000]
    except Exception:
        text_sample = ""
    text_lower = text_sample.lower()
    has_cpws_kw = (
        "columbia power" in text_lower
        or "cpws.com" in text_lower
        or "(931) 388-4833" in text_sample
    )
    if has_cpws_kw:
        return True, 0.95, "PDF text mentions Columbia Power & Water Systems / cpws.com"
    if fname_hint and not text_sample:
        return True, 0.6, "PDF filename hints Columbia (scanned, OCR will confirm)"
    return False, 0.0, ""


def _looks_like_atmos_energy_auto_pay(path: Path) -> tuple[bool, float, str]:
    """Detect Atmos Energy Auto Pay PDFs.

    Strong signals (cheap, no OCR):
      * filename hint: ``atmos`` substring (low signal — generic).
      * PDF text-layer: page 1 mentions "ATMOS ENERGY",
        "atmosenergy.com", or the customer-service phone number
        ``1-888-286-6700``. Any one of those is decisive.
    """
    if path.suffix.lower() != ".pdf":
        return False, 0.0, ""
    name_lower = path.name.lower()
    fname_hint = "atmos" in name_lower
    text_sample = ""
    try:
        import pdfplumber  # type: ignore
        with pdfplumber.open(path) as pdf:
            if pdf.pages:
                text_sample = (pdf.pages[0].extract_text() or "")[:3000]
    except Exception:
        text_sample = ""
    text_lower = text_sample.lower()
    has_atmos_kw = (
        "atmos energy" in text_lower
        or "atmosenergy.com" in text_lower
        or "1-888-286-6700" in text_sample
        or "1-866-322-8667" in text_sample
    )
    if has_atmos_kw:
        return True, 0.95, "PDF text mentions Atmos Energy / atmosenergy.com"
    if fname_hint and not text_sample:
        return True, 0.6, "PDF filename hints Atmos (scanned, OCR will confirm)"
    return False, 0.0, ""


def _looks_like_hardin_county_water(path: Path) -> tuple[bool, float, str]:
    """Detect Hardin County Water District No. 2 PDFs (digital + scanned).

    For digital PDFs the text layer carries "Hardin County Water
    District No. 2" / "hcwd2.org" / the office phone "270.737.1056".
    Scanned PDFs return empty text via the cheap pdfplumber probe;
    those are accepted only on a vendor-specific filename hint at lower
    confidence — OCR will confirm during processing.
    """
    if path.suffix.lower() != ".pdf":
        return False, 0.0, ""
    name_lower = path.name.lower()
    # Do not treat generic UUID download names as vendor evidence. Many
    # scanned bills arrive as UUID PDFs; routing those to Hardin County
    # caused unrelated vendors to bypass AI/vision extraction.
    fname_hint = (
        "hardin" in name_lower
        or "hcwd2" in name_lower
        or "billprint" in name_lower
    )
    text_sample = ""
    try:
        import pdfplumber  # type: ignore
        with pdfplumber.open(path) as pdf:
            if pdf.pages:
                text_sample = (pdf.pages[0].extract_text() or "")[:3000]
    except Exception:
        text_sample = ""
    text_lower = text_sample.lower()
    has_hcwd2_kw = (
        "hardin county water" in text_lower
        or "hcwd2.org" in text_lower
        or "270.737.1056" in text_sample
        or "270.737.2301" in text_sample
    )
    if has_hcwd2_kw:
        return True, 0.95, "PDF text mentions Hardin County Water District / hcwd2.org"
    if fname_hint and not text_sample:
        return True, 0.7, "PDF filename hints HCWD2 (scanned, OCR will confirm)"
    return False, 0.0, ""


def _looks_like_shelbyville_power(path: Path) -> tuple[bool, float, str]:
    """Detect Shelbyville Power System PDFs.

    Strong signals (cheap, no OCR):
      * filename hint: bills are named with the bill sequence number
        (e.g. ``2903088.pdf``); on its own that's too generic, but
        when the sibling text-layer probe also returns "Shelbyville
        Power" we lock it in.
      * PDF text-layer: page 1 mentions "SHELBYVILLE POWER SYSTEM",
        "shelbyvillepower.com", or the customer-service phone numbers.

    Note: many Shelbyville PDFs ship with malformed MediaBox metadata
    that pdfplumber can't open. The shared text extractor falls back
    to PyPDF2 transparently, but the cheap probe used here also has
    to be tolerant — we wrap it in try/except and fall through to
    the filename hint when extraction throws."""
    if path.suffix.lower() != ".pdf":
        return False, 0.0, ""
    name_lower = path.name.lower()
    fname_hint = "shelbyville" in name_lower
    text_sample = ""
    try:
        import pdfplumber  # type: ignore
        with pdfplumber.open(path) as pdf:
            if pdf.pages:
                text_sample = (pdf.pages[0].extract_text() or "")[:3000]
    except Exception:
        # Malformed MediaBox; try PyPDF2 directly for the probe.
        try:
            import PyPDF2  # type: ignore
            with open(path, "rb") as fh:
                reader = PyPDF2.PdfReader(fh)
                if reader.pages:
                    text_sample = (reader.pages[0].extract_text() or "")[:3000]
        except Exception:
            text_sample = ""
    text_lower = text_sample.lower()
    has_kw = (
        "shelbyville power system" in text_lower
        or "shelbyvillepower.com" in text_lower
        or "931-684-7171" in text_sample
        or "(866)-784-0063" in text_sample
    )
    if has_kw:
        return True, 0.95, "PDF text mentions Shelbyville Power System / shelbyvillepower.com"
    if fname_hint and not text_sample:
        return True, 0.6, "PDF filename hints Shelbyville (probe failed, processor will retry)"
    return False, 0.0, ""


# Order matters: first detector to claim ownership wins.
def _looks_like_zillow_rentals(path: Path) -> tuple[bool, float, str]:
    """Detect Zillow Rentals invoices.

    Strong signals:
      * filename hint: Zillow's e-mailed invoices are named
        ``INV<digits>.pdf``.
      * PDF text-layer: page 1 mentions "Zillow Rentals" / "Zillow,
        Inc." / "zillow.com/rental-manager".
    """
    if path.suffix.lower() != ".pdf":
        return False, 0.0, ""
    name_lower = path.name.lower()
    fname_hint = bool(re.match(r"^inv\d{6,12}\.pdf$", name_lower)) or "zillow" in name_lower
    text_sample = ""
    try:
        import pdfplumber  # type: ignore
        with pdfplumber.open(path) as pdf:
            if pdf.pages:
                text_sample = (pdf.pages[0].extract_text() or "")[:3000]
    except Exception:
        text_sample = ""
    text_lower = text_sample.lower()
    has_kw = (
        "zillow rentals" in text_lower
        or "zillow, inc" in text_lower
        or "zillow.com/rental-manager" in text_lower
        or "rentalpartners@zillowgroup.com" in text_lower
    )
    if has_kw:
        return True, 0.95, "PDF text mentions Zillow Rentals / Zillow, Inc."
    if fname_hint and not text_sample:
        return True, 0.6, "PDF filename hints Zillow (probe failed)"
    return False, 0.0, ""


def _looks_like_pennyrile_electric(path: Path) -> tuple[bool, float, str]:
    """Detect Pennyrile Rural Electric Coop Corp (PRECC) PDFs.

    Strong signals from the PDF text layer:
      * "PENNYRILE RURAL ELECTRIC COOP CORP" anywhere in the body or
        the receipt strip.
      * "precc.com" / "outage.precc.com" links.
      * Hopkinsville KY 42241 PO Box 2900 office address.
    """
    if path.suffix.lower() != ".pdf":
        return False, 0.0, ""
    text_sample = ""
    try:
        import pdfplumber  # type: ignore
        with pdfplumber.open(path) as pdf:
            if pdf.pages:
                text_sample = (pdf.pages[0].extract_text() or "")[:3000]
    except Exception:
        text_sample = ""
    if not text_sample:
        return False, 0.0, ""
    compact = re.sub(r"\s+", "", text_sample).upper()
    if "PENNYRILERURALELECTRICCOOPCORP" in compact:
        return True, 0.95, "PDF text mentions Pennyrile Rural Electric Coop Corp"
    if "PRECC.COM" in compact and "POBOX2900" in compact and "HOPKINSVILLE" in compact:
        return True, 0.9, "PDF text matches PRECC office signature"
    return False, 0.0, ""


def _looks_like_mcminnville_electric(path: Path) -> tuple[bool, float, str]:
    """Detect McMinnville Electric System (MES) PDFs.

    Strong signals from the PDF text layer:
      * "MCMINNVILLE ELECTRIC SYSTEM" header (case-insensitive after
        whitespace stripping — pdfplumber sometimes joins letters).
      * Address line "200 W. Morford Street" / "MCMINNVILLE TN".
      * Phone (931) 473-3144.
    """
    if path.suffix.lower() != ".pdf":
        return False, 0.0, ""
    text_sample = ""
    try:
        import pdfplumber  # type: ignore
        with pdfplumber.open(path) as pdf:
            if pdf.pages:
                text_sample = (pdf.pages[0].extract_text() or "")[:3000]
    except Exception:
        text_sample = ""
    if not text_sample:
        return False, 0.0, ""
    compact = re.sub(r"\s+", "", text_sample).upper()
    if "MCMINNVILLEELECTRICSYSTEM" in compact:
        return True, 0.95, "PDF text mentions McMinnville Electric System"
    if "(931)473-3144" in compact and "MORFORDSTREET" in compact:
        return True, 0.9, "PDF text matches MES office address + phone"
    return False, 0.0, ""


def _looks_like_alabama_power(path: Path) -> tuple[bool, float, str]:
    if path.suffix.lower() != ".pdf":
        return False, 0.0, ""
    hay = f"{path.name}\n{_pdf_text_sample(path, 3500)}".lower()
    compact = re.sub(r"\s+", "", hay)
    if "alabamapower" in compact or "alabamapower.com" in hay:
        return True, 0.95, "PDF text mentions Alabama Power / AlabamaPower.com"
    if "alabama power" in hay:
        return True, 0.9, "PDF text mentions Alabama Power"
    return False, 0.0, ""


def _looks_like_epb_fiber_optics(path: Path) -> tuple[bool, float, str]:
    if path.suffix.lower() != ".pdf":
        return False, 0.0, ""
    hay = f"{path.name}\n{_pdf_text_sample(path, 3500)}".lower()
    compact = re.sub(r"\s+", "", hay)
    if "epbfiberoptics" in compact or "epb.com" in hay:
        return True, 0.95, "PDF text mentions EPB Fiber Optics / epb.com"
    if "fi-speed internet" in hay and "accountnumber" in compact:
        return True, 0.9, "PDF text matches EPB fiber billing format"
    return False, 0.0, ""


def _looks_like_city_of_henderson(path: Path) -> tuple[bool, float, str]:
    if path.suffix.lower() != ".pdf":
        return False, 0.0, ""
    hay = f"{path.name}\n{_pdf_text_sample(path, 3500)}".lower()
    if "city of henderson" in hay and ("current billing" in hay or "hendersonky.gov" in hay):
        return True, 0.95, "PDF text mentions City of Henderson utility billing"
    return False, 0.0, ""


def _looks_like_cde_lightband(path: Path) -> tuple[bool, float, str]:
    if path.suffix.lower() != ".pdf":
        return False, 0.0, ""
    hay = f"{path.name}\n{_pdf_text_sample(path, 3500)}".lower()
    if "cde lightband" in hay or "cdelightband" in hay:
        return True, 0.95, "PDF text mentions CDE Lightband"
    if "service period:" in hay and "electric energy charge" in hay and "date due:" in hay:
        return True, 0.85, "PDF text matches CDE utility billing format"
    return False, 0.0, ""


def _looks_like_nolin_recc_smarthub(path: Path) -> tuple[bool, float, str]:
    if path.suffix.lower() != ".pdf":
        return False, 0.0, ""
    hay = f"{path.name}\n{_pdf_text_sample(path, 3500)}".lower()
    compact = re.sub(r"\s+", "", hay)
    if "nolin" in hay and ("recc" in hay or "rural electric" in hay or "smarthub" in hay):
        return True, 0.95, "PDF text mentions Nolin RECC / SmartHub"
    if "payment will draft" in hay and "masteraccount" in compact:
        return True, 0.9, "PDF text matches Nolin master-billing statement"
    return False, 0.0, ""


def _looks_like_keyword_vendor(
    path: Path,
    *,
    labels: tuple[str, ...],
    reason: str,
    secondary: tuple[str, ...] = (),
) -> tuple[bool, float, str]:
    hay = f"{path.name}\n{_document_text_sample(path, 3500)}".lower()
    compact = re.sub(r"\s+", "", hay)
    if any(label.lower() in hay or re.sub(r"\s+", "", label.lower()) in compact for label in labels):
        if not secondary or any(s.lower() in hay or re.sub(r"\s+", "", s.lower()) in compact for s in secondary):
            return True, 0.92, reason
    return False, 0.0, ""


def _looks_like_clarksville_gas_and_water(path: Path) -> tuple[bool, float, str]:
    return _looks_like_keyword_vendor(
        path,
        labels=("Clarksville Gas and Water", "Clarksville Gas & Water", "clarksvillegw.com"),
        secondary=("Total Current", "Current Billing", "Account No"),
        reason="PDF/OCR text matches Clarksville Gas and Water utility bill",
    )


def _looks_like_knoxville_utilities_board(path: Path) -> tuple[bool, float, str]:
    return _looks_like_keyword_vendor(
        path,
        labels=("Knoxville Utilities Board", "kub.org", "KUB Payment"),
        secondary=("Billing Summary", "Summary of Charges by Address", "Account Number"),
        reason="PDF text matches Knoxville Utilities Board bill",
    )


def _looks_like_kentucky_utilities(path: Path) -> tuple[bool, float, str]:
    return _looks_like_keyword_vendor(
        path,
        labels=("Kentucky Utilities", "lge-ku.com", "LG&E KU"),
        secondary=("Current Electric Charges", "Total Current Charges", "Account #"),
        reason="PDF text matches Kentucky Utilities bill",
    )


def _looks_like_tennessee_american_water(path: Path) -> tuple[bool, float, str]:
    return _looks_like_keyword_vendor(
        path,
        labels=("Tennessee American Water", "tennesseeamwater.com", "amwater.com"),
        secondary=("ServiceRelatedCharges", "Account No", "Payment Due By"),
        reason="PDF text matches Tennessee American Water bill",
    )


def _looks_like_union_city_energy_authority(path: Path) -> tuple[bool, float, str]:
    return _looks_like_keyword_vendor(
        path,
        labels=("Union City Energy Authority", "unioncityenergy.com", "Union City Energy"),
        secondary=("METERED ELECTRIC", "NET AMOUNT DUE", "BANK DRAFT"),
        reason="PDF/OCR text matches Union City Energy Authority bill",
    )


def _looks_like_weakley_county_electric(path: Path) -> tuple[bool, float, str]:
    matched, score, reason = _looks_like_keyword_vendor(
        path,
        labels=("Weakley County Municipal Electric System", "wcmes.com"),
        secondary=("Metered Electric", "ACCOUNTNUMBER", "TOTALCURRENTCHARGES"),
        reason="PDF/OCR text matches Weakley County Municipal Electric System bill",
    )
    if matched:
        return matched, score, reason
    # OCR on photographed Weakley bills often breaks the heading across
    # unrelated text, so the full normalized vendor label is not contiguous.
    hay = f"{path.name}\n{_document_text_sample(path, 3500)}".lower()
    compact = re.sub(r"\s+", "", hay)
    if (
        "weakley county" in hay
        and "municipal electric system" in hay
        and (
            "totalcurrentcharges" in compact
            or "netamountdue" in compact
            or "pay by phone" in hay
        )
    ):
        return True, 0.9, "OCR text matches Weakley County Municipal Electric System image bill"
    return False, 0.0, ""


def _looks_like_birmingham_water_works(path: Path) -> tuple[bool, float, str]:
    return _looks_like_keyword_vendor(
        path,
        labels=("Birmingham Water Works", "Central Alabama Water", "caw-al.gov"),
        secondary=("CAW WATER SERVICE", "JEFFERSON COUNTY SEWER SERVICE", "ACCOUNT NUMBER"),
        reason="PDF text matches Birmingham Water Works / Central Alabama Water bill",
    )


def _looks_like_city_of_mcminnville_water(path: Path) -> tuple[bool, float, str]:
    return _looks_like_keyword_vendor(
        path,
        labels=("City of McMinnville", "cityofmcminnvilletn.gov"),
        secondary=("Account Number Service Period", "AMOUNT DUE NOW", "WA"),
        reason="PDF text matches City of McMinnville Water/Sewer bill",
    )


def _looks_like_chattanooga_wastewater(path: Path) -> tuple[bool, float, str]:
    return _looks_like_keyword_vendor(
        path,
        labels=("City of Chattanooga Wastewater Department", "sewerpayments.com/chattanooga"),
        secondary=("ACCOUNT=", "Sewer Usage Charges", "BILLDATE="),
        reason="PDF text matches City of Chattanooga Wastewater bill",
    )


def _looks_like_city_of_martin(path: Path) -> tuple[bool, float, str]:
    return _looks_like_keyword_vendor(
        path,
        labels=("City of Martin", "cityofmartin.net"),
        secondary=("Account Number Service Period", "AMOUNT DUE NOW", "WA"),
        reason="PDF text matches City of Martin utility bill",
    )


def _looks_like_city_of_union_city(path: Path) -> tuple[bool, float, str]:
    return _looks_like_keyword_vendor(
        path,
        labels=("CITY OF UNION CITY - WATER", "CITY OF UNION CITY", "unioncitytn.gov/water"),
        secondary=("CURRENT BILL", "SANITATION", "STORMWATER"),
        reason="PDF text matches City of Union City water/sewer bill",
    )


def _looks_like_guardian_water_power(path: Path) -> tuple[bool, float, str]:
    return _looks_like_keyword_vendor(
        path,
        labels=("Guardian Water & Power", "Guardian Water and Power", "myguardianwp.com"),
        secondary=("Invoice Number", "Customer Number", "BILLING FEE"),
        reason="PDF text matches Guardian Water & Power invoice",
    )


def _looks_like_hopkinsville_electric(path: Path) -> tuple[bool, float, str]:
    return _looks_like_keyword_vendor(
        path,
        labels=("Hopkinsville Electric System", "hop-electric.com"),
        secondary=("Electric Service", "ACCOUNTNUMBER", "TOTALCURRENTCHARGES"),
        reason="PDF text matches Hopkinsville Electric System bill",
    )


def _pdf_text_sample(path: Path, limit: int = 2500) -> str:
    if path.suffix.lower() != ".pdf":
        return ""
    try:
        import pdfplumber  # type: ignore
        with pdfplumber.open(path) as pdf:
            if pdf.pages:
                return (pdf.pages[0].extract_text() or "")[:limit]
    except Exception:
        return ""
    return ""


def _document_text_sample(path: Path, limit: int = 2500) -> str:
    text = _pdf_text_sample(path, limit)
    if text.strip():
        return text
    if path.suffix.lower() not in {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}:
        return ""
    # Phase PERF-1 hotfix — fast-mode short-circuit.
    #
    # When the caller is `listFiles` (vendor detection at upload time),
    # running 5 Tesseract variants per image is a 10-50 s blocker on
    # screenshot uploads. The OCR_FAST_DETECTION_ONLY env flag (set by
    # the listFiles endpoint via thread-local context) tells us to
    # only consult the OCR cache, not run a fresh OCR. If the cache
    # is empty, we return "" and the file is treated as unknown until
    # the user actually processes it. The cache fills on the first
    # processing pass so subsequent listFiles calls match instantly.
    try:
        import threading
        flag = getattr(_DETECT_CTX, "fast_only", False)
    except Exception:
        flag = False
    if flag:
        try:
            from utils import ocr_cache  # type: ignore
            cached = ocr_cache.lookup(path, 0)
            if cached and cached.get("pages"):
                return (cached["pages"][0].get("text") or "")[:limit]
        except Exception:
            pass
        return ""
    try:
        from .document_ingestion import ingest_document

        return (ingest_document(path, max_pages=2).document_text or "")[:limit]
    except Exception:
        return ""


# Thread-local context — set by callers (e.g. listFiles endpoint) to
# request fast-mode vendor detection that doesn't run image OCR.
import threading as _threading
_DETECT_CTX = _threading.local()


class fast_detection_context:
    """Context manager: temporarily mark this thread as "fast detection
    only" so image-OCR-dependent detectors fall back to cache lookups
    only. Usage:

        with fast_detection_context():
            entries = _detect_files_cached(batch_id, files)
    """
    def __enter__(self):
        _DETECT_CTX.fast_only = True
        return self
    def __exit__(self, *exc):
        _DETECT_CTX.fast_only = False


def _looks_like_hd_supply(path: Path) -> tuple[bool, float, str]:
    hay = f"{path.name}\n{_pdf_text_sample(path)}".lower()
    if "hd supply" in hay or "hdsupply" in hay:
        return True, 0.85, "variable supplier invoice: HD Supply"
    return False, 0.0, ""


def _looks_like_lowes(path: Path) -> tuple[bool, float, str]:
    hay = f"{path.name}\n{_pdf_text_sample(path)}".lower()
    if "lowe's" in hay or "lowes" in hay or "lowe s" in hay:
        return True, 0.85, "variable supplier invoice: Lowe's"
    return False, 0.0, ""


def _looks_like_home_depot(path: Path) -> tuple[bool, float, str]:
    hay = f"{path.name}\n{_pdf_text_sample(path)}".lower()
    if "home depot" in hay or "the home depot" in hay:
        return True, 0.85, "variable supplier invoice: Home Depot"
    return False, 0.0, ""


_DETECTORS: list[tuple[str, Callable[[Path], tuple[bool, float, str]]]] = [
    ("richmond_utilities", _looks_like_richmond_utilities),
    ("hopkinsville_water_environment_authority", _looks_like_hopkinsville_water),
    ("columbia_power_and_water_system", _looks_like_columbia_power_and_water),
    ("atmos_energy_auto_pay", _looks_like_atmos_energy_auto_pay),
    ("hardin_county_water_district_no_2", _looks_like_hardin_county_water),
    ("shelbyville_power_system", _looks_like_shelbyville_power),
    ("zillow_rentals", _looks_like_zillow_rentals),
    ("mcminnville_electric_system", _looks_like_mcminnville_electric),
    ("pennyrile_electric", _looks_like_pennyrile_electric),
    ("alabama_power", _looks_like_alabama_power),
    ("epb_fiber_optics", _looks_like_epb_fiber_optics),
    ("the_city_of_henderson", _looks_like_city_of_henderson),
    ("cde_lightband", _looks_like_cde_lightband),
    ("nolin_recc_smarthub", _looks_like_nolin_recc_smarthub),
    ("clarksville_gas_and_water", _looks_like_clarksville_gas_and_water),
    ("knoxville_utilities_board", _looks_like_knoxville_utilities_board),
    ("kentucky_utilities", _looks_like_kentucky_utilities),
    ("tennessee_american_water", _looks_like_tennessee_american_water),
    ("union_city_energy_authority", _looks_like_union_city_energy_authority),
    ("weakley_county_municipal_electric_system", _looks_like_weakley_county_electric),
    ("birmingham_water_works", _looks_like_birmingham_water_works),
    ("city_of_mcminnville_water_sewer_dept", _looks_like_city_of_mcminnville_water),
    ("city_of_chattanooga_wastewater_department", _looks_like_chattanooga_wastewater),
    ("city_of_martin", _looks_like_city_of_martin),
    ("city_of_union_city", _looks_like_city_of_union_city),
    ("guardian_water_power", _looks_like_guardian_water_power),
    ("hopkinsville_electric_system", _looks_like_hopkinsville_electric),
    ("hd_supply", _looks_like_hd_supply),
    ("lowes", _looks_like_lowes),
    ("home_depot", _looks_like_home_depot),
]


SUPPORTED_VENDOR_KEYS = {
    "richmond_utilities",
    "hopkinsville_water_environment_authority",
    "columbia_power_and_water_system",
    "atmos_energy_auto_pay",
    "hardin_county_water_district_no_2",
    "shelbyville_power_system",
    "zillow_rentals",
    "mcminnville_electric_system",
    "pennyrile_electric",
    "alabama_power",
    "epb_fiber_optics",
    "the_city_of_henderson",
    "cde_lightband",
    "nolin_recc_smarthub",
    "clarksville_gas_and_water",
    "knoxville_utilities_board",
    "kentucky_utilities",
    "tennessee_american_water",
    "union_city_energy_authority",
    "weakley_county_municipal_electric_system",
    "birmingham_water_works",
    "city_of_mcminnville_water_sewer_dept",
    "city_of_chattanooga_wastewater_department",
    "city_of_martin",
    "city_of_union_city",
    "guardian_water_power",
    "hopkinsville_electric_system",
}

AI_ASSIST_VENDOR_KEYS = {"hd_supply", "lowes", "home_depot"}


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
                "processing_mode": (
                    "ai_assisted"
                    if vendor_key in AI_ASSIST_VENDOR_KEYS
                    else "deterministic"
                    if vendor_key in SUPPORTED_VENDOR_KEYS
                    else "manual"
                ),
            }
    return {
        "vendor_key": "unknown",
        "confidence": 0.0,
        "reason": "no_detector_claimed_this_file",
        "supported_in_phase_1": False,
        "processing_mode": "ai_assisted",
    }


def detect_vendors_for_files(paths: Iterable[Path]) -> dict[str, dict]:
    """Convenience: return a {filename: detection_dict} map."""
    return {p.name: detect_vendor_for_file(p) for p in paths}
