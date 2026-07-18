"""Light heuristic vendor router for Phase 1.

Only Richmond Utilities is wired into a backend processor in Phase 1, but
the detection logic returns a `vendor_key` plus a confidence so the UI can
present a manual-pick dropdown when confidence is low. Future phases can
extend `_detectors` without changing the API contract.
"""

from __future__ import annotations

import csv
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable

_TEXT_SAMPLE_CACHE: dict[tuple[str, int, int, int], str] = {}
_TEXT_SAMPLE_CACHE_MAX = 512


def _path_cache_key(path: Path, limit: int) -> tuple[str, int, int, int] | None:
    try:
        st = path.stat()
        return (str(path.resolve()), int(st.st_mtime_ns), int(st.st_size), int(limit))
    except OSError:
        return None


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


def _looks_like_apartments_com(path: Path) -> tuple[bool, float, str]:
    """Detect Apartments.com / CoStar invoices.

    Current PDFs expose "CoStar Federal Tax ID", "Account #/Location ID",
    and the Apartments LLC remittance block on page 1.
    """
    if path.suffix.lower() != ".pdf":
        return False, 0.0, ""
    text_sample = _document_text_sample(path, 5000)
    hay = f"{path.name}\n{text_sample}".lower()
    compact = re.sub(r"\s+", "", hay)
    if (
        "costarfederaltaxid" in compact
        and "account#/locationid" in compact
        and ("apartmentsllc" in compact or "costar.billtrust.com" in hay)
    ):
        return True, 0.96, "PDF text matches Apartments.com / CoStar invoice"
    if "apartments.com" in hay and "invoice number" in hay and "service period" in hay:
        return True, 0.9, "PDF text matches Apartments.com invoice"
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
    if (
        "service period:" in hay
        and "date due:" in hay
        and "account #:" in hay
        and ("<<electric sub-total>>" in hay or "<<telecom sub-total>>" in hay)
        and ("clarksvillede.com" in hay or "cde is not responsible" in hay)
    ):
        return True, 0.9, "PDF text matches CDE balance-only utility billing format"
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
    if any(_vendor_signal_present(hay, compact, label) for label in labels):
        if not secondary or any(_vendor_signal_present(hay, compact, signal) for signal in secondary):
            return True, 0.92, reason
    return False, 0.0, ""


def _vendor_signal_present(haystack: str, compact_haystack: str, signal: str) -> bool:
    """Match vendor evidence without treating acronyms as substrings.

    The old ``"nes" in haystack`` test classified Cook's Pest Control as
    Nashville Electric Service because the invoice footer contained the word
    ``business``. Short alphabetic signals must be standalone tokens.
    """
    normalized = str(signal or "").strip().lower()
    if not normalized:
        return False
    if normalized.isalpha() and len(normalized) <= 4:
        return re.search(
            rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])",
            haystack,
        ) is not None
    compact_signal = re.sub(r"\s+", "", normalized)
    return normalized in haystack or compact_signal in compact_haystack


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


def _looks_like_nashville_electric_service(path: Path) -> tuple[bool, float, str]:
    return _looks_like_keyword_vendor(
        path,
        labels=("Nashville Electric Service", "nespower.com", "NES"),
        secondary=("Current balance due", "Billing period", "Service Address", "Account #:"),
        reason="PDF text matches Nashville Electric Service bill",
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
    hay = f"{path.name}\n{_document_text_sample(path, 3500)}".lower()
    if "hop-electric.utilitynexus.com" in hay and "electric service charges" in hay:
        return True, 0.94, "PDF text matches Hopkinsville Electric portal statement"
    return _looks_like_keyword_vendor(
        path,
        labels=("Hopkinsville Electric System", "hop-electric.com", "hop-electric.utilitynexus.com"),
        secondary=("Electric Service", "ACCOUNTNUMBER", "TOTALCURRENTCHARGES", "Statement Amount"),
        reason="PDF text matches Hopkinsville Electric System bill",
    )


def _looks_like_cumberland_emc(path: Path) -> tuple[bool, float, str]:
    return _looks_like_keyword_vendor(
        path,
        labels=(
            "Cumberland EMC",
            "Cumberland Electric Membership Corporation",
            "cemc.org",
        ),
        secondary=("Current Charges Due", "AUTOPAY AMOUNT", "Account Information"),
        reason="PDF text matches Cumberland EMC bill",
    )


def _looks_like_pleasant_view_utility_district(path: Path) -> tuple[bool, float, str]:
    return _looks_like_keyword_vendor(
        path,
        labels=(
            "PLEASANT VIEW UTILITY DISTRICT",
            "pvudwater.com",
        ),
        secondary=("WATER SERVICE", "SEWER SERVICE", "WTR LEAK RELIEF"),
        reason="PDF text matches Pleasant View Utility District bill",
    )


def _pdf_text_sample(path: Path, limit: int = 2500) -> str:
    if path.suffix.lower() != ".pdf":
        return ""
    cache_key = _path_cache_key(path, limit)
    if cache_key is not None and cache_key in _TEXT_SAMPLE_CACHE:
        return _TEXT_SAMPLE_CACHE[cache_key]
    try:
        import pdfplumber  # type: ignore
        with pdfplumber.open(path) as pdf:
            if pdf.pages:
                text = (pdf.pages[0].extract_text() or "")[:limit]
                if cache_key is not None:
                    _TEXT_SAMPLE_CACHE[cache_key] = text
                    if len(_TEXT_SAMPLE_CACHE) > _TEXT_SAMPLE_CACHE_MAX:
                        _TEXT_SAMPLE_CACHE.pop(next(iter(_TEXT_SAMPLE_CACHE)))
                return text
    except Exception:
        pass
    # Empty text is also deterministic for unchanged scanned PDFs. Caching it
    # prevents every vendor detector from reopening the same image-only file.
    if cache_key is not None:
        _TEXT_SAMPLE_CACHE[cache_key] = ""
        if len(_TEXT_SAMPLE_CACHE) > _TEXT_SAMPLE_CACHE_MAX:
            _TEXT_SAMPLE_CACHE.pop(next(iter(_TEXT_SAMPLE_CACHE)))
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
    if _looks_like_lowes_pro_supply_layout(hay):
        return True, 0.88, "variable supplier invoice: Lowe's Pro Supply layout"
    return False, 0.0, ""


def _looks_like_lowes_pro_supply_layout(hay: str) -> bool:
    """Detect Lowe's Pro Supply invoices where the logo/vendor is image-only.

    Some LPS PDFs expose the remit/header/table text but not the vendor name.
    The combination below is specific enough to avoid treating generic supplier
    invoices as Lowe's while still routing these files out of the unknown path.
    """
    compact = re.sub(r"\s+", "", hay or "")
    return (
        "bill to #" in hay
        and "order #" in hay
        and ("ship point lps-" in hay or "shippointlps-" in compact)
        and "gl code:" in hay
        and ("p.o. box 301451" in hay or "pobox301451" in compact)
    )


def _looks_like_home_depot(path: Path) -> tuple[bool, float, str]:
    hay = f"{path.name}\n{_pdf_text_sample(path)}".lower()
    if "home depot" in hay or "the home depot" in hay:
        return True, 0.85, "variable supplier invoice: Home Depot"
    return False, 0.0, ""


def _looks_like_resman_llc(path: Path) -> tuple[bool, float, str]:
    hay = f"{path.name}\n{_document_text_sample(path, 5000)}".lower()
    compact = re.sub(r"\s+", "", hay)
    strong = (
        "myresman.com" in hay
        or "resman llc" in hay
        or "attn: resman" in hay
        or "invoice#:rsm" in compact
        or "customerid:rsm-" in compact
    )
    if strong and ("rsm-" in hay or re.search(r"\bRSM\d{5,}\b", hay, re.I)):
        return True, 0.97, "strong ResMan invoice keyword"
    if re.match(r"^rsm\d{5,}\.pdf$", path.name.lower()):
        return True, 0.78, "ResMan invoice filename pattern"
    return False, 0.0, ""


def _looks_like_granite_telecommunications(path: Path) -> tuple[bool, float, str]:
    """Detect Granite's digital multi-page telecom statements."""
    hay = f"{path.name}\n{_document_text_sample(path, 5000)}".lower()
    compact = re.sub(r"\s+", "", hay)
    if (
        "granitenet.com" in hay
        or "granitetelecommunications,llc" in compact
        or "rockreports.granitenet.com" in hay
    ) and "accountnumber:" in compact:
        return True, 0.99, "strong Granite Telecommunications statement keywords"
    return False, 0.0, ""


_DETECTORS: list[tuple[str, Callable[[Path], tuple[bool, float, str]]]] = [
    ("richmond_utilities", _looks_like_richmond_utilities),
    ("hopkinsville_water_environment_authority", _looks_like_hopkinsville_water),
    ("columbia_power_and_water_system", _looks_like_columbia_power_and_water),
    ("atmos_energy_auto_pay", _looks_like_atmos_energy_auto_pay),
    ("hardin_county_water_district_no_2", _looks_like_hardin_county_water),
    ("shelbyville_power_system", _looks_like_shelbyville_power),
    ("zillow_rentals", _looks_like_zillow_rentals),
    ("apartments_com", _looks_like_apartments_com),
    ("resman_llc", _looks_like_resman_llc),
    ("granite_telecommunications_llc", _looks_like_granite_telecommunications),
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
    ("nashville_electric_service", _looks_like_nashville_electric_service),
    ("weakley_county_municipal_electric_system", _looks_like_weakley_county_electric),
    ("birmingham_water_works", _looks_like_birmingham_water_works),
    ("city_of_mcminnville_water_sewer_dept", _looks_like_city_of_mcminnville_water),
    ("city_of_chattanooga_wastewater_department", _looks_like_chattanooga_wastewater),
    ("city_of_martin", _looks_like_city_of_martin),
    ("city_of_union_city", _looks_like_city_of_union_city),
    ("guardian_water_power", _looks_like_guardian_water_power),
    ("hopkinsville_electric_system", _looks_like_hopkinsville_electric),
    ("cumberland_emc", _looks_like_cumberland_emc),
    ("pleasant_view_utility_district", _looks_like_pleasant_view_utility_district),
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
    "apartments_com",
    "resman_llc",
    "granite_telecommunications_llc",
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
    "nashville_electric_service",
    "weakley_county_municipal_electric_system",
    "birmingham_water_works",
    "city_of_mcminnville_water_sewer_dept",
    "city_of_chattanooga_wastewater_department",
    "city_of_martin",
    "city_of_union_city",
    "guardian_water_power",
    "hopkinsville_electric_system",
    "cumberland_emc",
    "pleasant_view_utility_district",
    "lowes",
}

AI_ASSIST_VENDOR_KEYS = {"hd_supply", "home_depot"}


def _detection_payload(vendor_key: str, confidence: float, reason: str) -> dict:
    return {
        "vendor_key": vendor_key,
        "confidence": confidence,
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


def _fast_keyword_detection(path: Path) -> dict | None:
    """One-pass strong-keyword router.

    Many legacy detector functions open the first PDF page individually.
    On large batches that makes vendor routing slower than the processor
    itself. This fast path samples the document once and only returns a
    result for high-specificity vendor strings; ambiguous cases still
    fall through to the existing detector chain.
    """
    text = _document_text_sample(path, 5000)
    return detect_vendor_from_text(path, text)


def detect_vendor_from_text(path: Path, text: str) -> dict | None:
    """Route from one already-extracted text sample without another OCR pass."""
    if not text.strip():
        return None
    hay = f"{path.name}\n{text}".lower()
    compact = re.sub(r"\s+", "", hay)
    if (
        "costarfederaltaxid" in compact
        and "account#/locationid" in compact
        and ("apartmentsllc" in compact or "costar.billtrust.com" in hay)
    ):
        return _detection_payload("apartments_com", 0.97, "strong text keyword: apartments_com")
    if (
        "cityofchattanoogawastewaterdepartment" in compact
        or "sewerpayments.com/chattanooga" in hay
        or ("cityofchattanooga" in compact and "sewerusagecharges" in compact)
    ):
        return _detection_payload(
            "city_of_chattanooga_wastewater_department",
            0.98,
            "strong text keyword: city_of_chattanooga_wastewater_department",
    )
    # The processor registry and each processor's vendor_identity contract are
    # the canonical deterministic inventory.  Consult them before the legacy
    # hand-maintained keyword list so adding a registered processor cannot
    # silently leave vendor detection (and therefore cost routing) behind.
    # Short aliases are deliberately ignored: they are useful for operator
    # search, but are not strong enough to authorize deterministic routing.
    registered = _detect_registered_processor_identity(hay)
    if registered is not None:
        return registered
    checks: list[tuple[str, tuple[str, ...], str]] = [
        ("zillow_rentals", ("zillow rentals", "zillow.com/rental-manager", "rentalpartners@zillowgroup.com", "zillowgroup.com"), "strong text keyword"),
        ("apartments_com", ("apartments.com", "costarfederaltaxid", "costar.billtrust.com"), "strong text keyword"),
        ("resman_llc", ("myresman.com", "resmanllc", "invoice#:rsm", "customerid:rsm-"), "strong text keyword"),
        ("granite_telecommunications_llc", ("granitenet.com", "granitetelecommunications,llc", "rockreports.granitenet.com"), "strong text keyword"),
        ("pennyrile_electric", ("pennyrileruralelectriccoopcorp", "precc.com"), "strong text keyword"),
        ("mcminnville_electric_system", ("mcminnvilleelectricsystem", "morfordstreet"), "strong text keyword"),
        ("alabama_power", ("alabamapower", "alabamapower.com"), "strong text keyword"),
        ("epb_fiber_optics", ("epbfiberoptics", "epb.com"), "strong text keyword"),
        ("the_city_of_henderson", ("cityofhenderson", "hendersonky.gov"), "strong text keyword"),
        ("cde_lightband", ("cdelightband", "clarksvillestelectric"), "strong text keyword"),
        ("nolin_recc_smarthub", ("nolinrecc", "nolinruralelectric"), "strong text keyword"),
        ("clarksville_gas_and_water", ("clarksvillegasandwater", "cityofclarksville"), "strong text keyword"),
        ("knoxville_utilities_board", ("knoxvilleutilitiesboard", "kub.org"), "strong text keyword"),
        ("kentucky_utilities", ("kentuckyutilities", "lge-ku.com"), "strong text keyword"),
        ("tennessee_american_water", ("tennesseeamericanwater", "amwater.com"), "strong text keyword"),
        ("union_city_energy_authority", ("unioncityenergyauthority", "unioncityenergy"), "strong text keyword"),
        ("nashville_electric_service", ("nashvilleelectricservice", "nespower.com"), "strong text keyword"),
        ("weakley_county_municipal_electric_system", ("weakleycountymunicipalelectricsystem", "wcmes.com"), "strong text keyword"),
        ("birmingham_water_works", ("birminghamwaterworks", "bwwb.org"), "strong text keyword"),
        ("city_of_mcminnville_water_sewer_dept", ("cityofmcminnville", "cityofmcminnvilletn.gov"), "strong text keyword"),
        ("city_of_chattanooga_wastewater_department", ("chattanoogawastewater", "chattanooga.gov"), "strong text keyword"),
        ("city_of_martin", ("cityofmartin", "martintn.gov"), "strong text keyword"),
        ("city_of_union_city", ("cityofunioncity", "unioncitytn.gov/water"), "strong text keyword"),
        ("guardian_water_power", ("guardianwater&power", "myguardianwp.com"), "strong text keyword"),
        (
            "hopkinsville_electric_system",
            ("hopkinsvilleelectricsystem", "hop-electric.com", "hop-electric.utilitynexus.com"),
            "strong text keyword",
        ),
        ("hopkinsville_water_environment_authority", ("hopkinsvillewaterenvironmentauthority", "hwea-ky.com"), "strong text keyword"),
        ("cumberland_emc", ("cumberlandemc", "cumberlandelectricmembershipcorporation", "cemc.org"), "strong text keyword"),
        ("pleasant_view_utility_district", ("pleasantviewutilitydistrict", "pvudwater.com"), "strong text keyword"),
    ]
    for vendor_key, needles, reason in checks:
        if any(needle in compact or needle in hay for needle in needles):
            return _detection_payload(vendor_key, 0.96, f"{reason}: {vendor_key}")
    if _looks_like_lowes_pro_supply_layout(hay):
        return _detection_payload("lowes", 0.88, "strong layout keyword: lowes_pro_supply")
    return None


def _detect_registered_processor_identity(text: str) -> dict | None:
    """Match strong declarative identities for registered processors only.

    A YAML file alone never activates a route.  ``_registered_identity_specs``
    first intersects the config directory with ``batch_processor``'s runtime
    registry, then this function requires a unique, sufficiently long literal
    identity in the observed source text.  The longest matching identity wins
    so e.g. a specific utility name is not shadowed by a broader city alias.
    """

    compact = _identity_token(text)
    if not compact:
        return None
    matches: list[tuple[int, str, str]] = []
    for vendor_key, identities in _registered_identity_specs():
        for identity in identities:
            token = _identity_token(identity)
            if len(token) < 8 or token not in compact:
                continue
            matches.append((len(token), vendor_key, identity))
    if not matches:
        return None
    matches.sort(key=lambda item: (-item[0], item[1], item[2].casefold()))
    best_length = matches[0][0]
    best_vendor_keys = {item[1] for item in matches if item[0] == best_length}
    if len(best_vendor_keys) != 1:
        return None
    vendor_key = matches[0][1]
    return _detection_payload(
        vendor_key,
        0.97,
        f"registered deterministic identity: {vendor_key}",
    )


def _identity_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _registered_identity_specs() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Return active identities with an mtime-aware cache.

    Imports are intentionally lazy to avoid a module cycle: ``batch_processor``
    imports this detector while defining the authoritative processor registry.
    At request time that registry is complete.  Config mtimes form the cache
    key, so an approved deterministic-builder edit is visible without a server
    restart.
    """

    try:
        from ..settings import VENDORS_DIR
        from . import batch_processor

        keys = tuple(sorted(batch_processor._PROCESSOR_LOADERS))  # type: ignore[attr-defined]
    except Exception:
        return ()
    signature: list[tuple[str, int, int]] = []
    for vendor_key in keys:
        path = VENDORS_DIR / f"{vendor_key}.yaml"
        try:
            stat = path.stat()
            signature.append((vendor_key, int(stat.st_mtime_ns), int(stat.st_size)))
        except OSError:
            signature.append((vendor_key, 0, 0))
    return _load_registered_identity_specs(tuple(signature))


@lru_cache(maxsize=8)
def _load_registered_identity_specs(
    signature: tuple[tuple[str, int, int], ...],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    try:
        import yaml
        from ..settings import VENDORS_DIR
    except Exception:
        return ()

    specs: list[tuple[str, tuple[str, ...]]] = []
    for vendor_key, _mtime_ns, _size in signature:
        path = VENDORS_DIR / f"{vendor_key}.yaml"
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        identity = payload.get("vendor_identity")
        if not isinstance(identity, dict) or not bool(identity.get("active", True)):
            continue
        values = [
            identity.get("vendor_name"),
            *(identity.get("detection_keywords") or []),
            *(identity.get("aliases") or []),
        ]
        identities = tuple(
            sorted(
                {str(value).strip() for value in values if str(value or "").strip()},
                key=str.casefold,
            )
        )
        if identities:
            specs.append((vendor_key, identities))
    return tuple(specs)


def detect_vendor_for_file(path: Path) -> dict:
    """Return a dict with detected vendor_key, confidence, reason. If no
    detector claims the file, vendor_key is 'unknown' and the UI should let
    the operator pick from a manual list."""
    fast = _fast_keyword_detection(path)
    if fast is not None:
        return fast
    for vendor_key, fn in _DETECTORS:
        try:
            ok, conf, reason = fn(path)
        except Exception as e:
            ok, conf, reason = False, 0.0, f"detector_error:{type(e).__name__}"
        if ok:
            return _detection_payload(vendor_key, conf, reason)
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
