"""
Reusable service-period resolver for utility vendors.

Designed to work for water, sewer, gas, electric, trash, and similar
recurring vendors where the billing cycle has a *service period* (or
*reading period* / *billing period*) that should appear on the AP row.

Priority (configurable per vendor in YAML):

    1. document_explicit_dates       — dates pulled directly off the bill/PDF
    2. reading_rows_if_available     — derived from meter-reading row dates
    3. batch_override                — temporary YAML file in the vendor folder
    4. vendor_default_fallback       — permanent rule in the vendor YAML
    5. manual_review                 — give up; flag for human review

Each level is enabled/disabled independently. Higher-priority levels that
are enabled but yield no usable dates fall through to the next.

The resolver is deliberately stateless and vendor-agnostic. Each vendor
processor passes in:

    * the invoice/billing date          (datetime)
    * a list of reading-row dates       (list[datetime] | None)
    * optional explicit dates extracted from the document
                                         (tuple[datetime, datetime] | None)
    * the YAML `service_period_rules` block
    * the path to the vendor folder where a batch override may live
    * a logger

It returns a `ServicePeriodResult` with:
    * start_date / end_date
    * formatted_range
    * source            (which priority level fired)
    * rule_used         (which specific rule inside that level)
    * inferred          (True if the period was derived rather than read directly)
    * manual_review_reasons
    * explicit_dates_found / batch_override_used / vendor_default_fallback_used
"""

from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

try:
    import yaml  # type: ignore
except ImportError:
    yaml = None  # type: ignore


SOURCE_DOCUMENT_EXPLICIT = "document_explicit_dates"
SOURCE_READING_ROWS = "reading_rows_if_available"
SOURCE_BATCH_OVERRIDE = "batch_override"
SOURCE_VENDOR_DEFAULT_FALLBACK = "vendor_default_fallback"
SOURCE_MANUAL_REVIEW = "manual_review"

DEFAULT_OUTPUT_FORMAT = "MM/DD/YY-MM/DD/YY"


@dataclass
class ServicePeriodResult:
    start_date: Optional[datetime]
    end_date: Optional[datetime]
    formatted_range: str
    source: str
    rule_used: str = ""
    inferred: bool = False
    manual_review_reasons: list[str] = field(default_factory=list)
    explicit_dates_found: bool = False
    batch_override_used: bool = False
    vendor_default_fallback_used: bool = False
    user_warning: str = ""

    @property
    def has_dates(self) -> bool:
        return self.start_date is not None and self.end_date is not None


# ---------------------------------------------------------------------------
# Output format
# ---------------------------------------------------------------------------
def _format_range(start: datetime, end: datetime, output_format: str) -> str:
    """Translate a YAML-friendly format like 'MM/DD/YY-MM/DD/YY' into actual
    strftime calls. Currently supports the user's documented format only."""
    fmt = (output_format or DEFAULT_OUTPUT_FORMAT).strip()
    if fmt == "MM/DD/YY-MM/DD/YY":
        return f"{start.strftime('%m/%d/%y')}-{end.strftime('%m/%d/%y')}"
    if fmt == "MM/DD/YYYY-MM/DD/YYYY":
        return f"{start.strftime('%m/%d/%Y')}-{end.strftime('%m/%d/%Y')}"
    if fmt == "MM-DD-YY-MM-DD-YY":
        return f"{start.strftime('%m-%d-%y')}-{end.strftime('%m-%d-%y')}"
    # Default: ISO with hyphen separator
    return f"{start.strftime('%Y-%m-%d')}/{end.strftime('%Y-%m-%d')}"


# ---------------------------------------------------------------------------
# Date arithmetic for offset-based fallback
# ---------------------------------------------------------------------------
def _shift_month(d: datetime, months: int) -> tuple[int, int]:
    """Return (year, month) after shifting by `months`."""
    y = d.year
    m = d.month + months
    while m <= 0:
        m += 12
        y -= 1
    while m > 12:
        m -= 12
        y += 1
    return y, m


def _safe_day(year: int, month: int, day: int, invalid_strategy: str) -> int:
    last = calendar.monthrange(year, month)[1]
    if 1 <= day <= last:
        return day
    if invalid_strategy == "last_day_of_month":
        return last
    if invalid_strategy == "first_day_of_month":
        return 1
    if invalid_strategy == "clamp":
        return max(1, min(last, day))
    return last


def _resolve_offset_dates(base_date: datetime, rule: dict) -> tuple[datetime, datetime]:
    """Apply an `inferred_from_invoice_date_offsets` block to a base date.
    Returns (start, end). Raises ValueError if the rule is malformed."""
    invalid = (rule.get("invalid_day_strategy") or "last_day_of_month").strip()
    start_cfg = rule.get("start") or {}
    end_cfg = rule.get("end") or {}
    sy, sm = _shift_month(base_date, int(start_cfg.get("month_offset_from_base_date", 0)))
    ey, em = _shift_month(base_date, int(end_cfg.get("month_offset_from_base_date", 0)))
    sd = _safe_day(sy, sm, int(start_cfg.get("day_of_month", 1)), invalid)
    ed = _safe_day(ey, em, int(end_cfg.get("day_of_month", 28)), invalid)
    return datetime(sy, sm, sd), datetime(ey, em, ed)


# ---------------------------------------------------------------------------
# Batch-override loader
# ---------------------------------------------------------------------------
def _load_batch_override(folder: Path, filename: str, logger: logging.Logger) -> Optional[dict]:
    """Look for `<folder>/<filename>`. Return its parsed YAML if present and
    enabled, else None. Never raises."""
    if yaml is None:
        return None
    path = folder / filename
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning("Could not parse %s: %s — ignoring batch override.", path, e)
        return None
    if not data.get("enabled"):
        logger.info("Batch override file %s exists but enabled=false — skipping.", path.name)
        return None
    logger.info("Batch override active: %s (reason: %s)", path, data.get("reason") or "(no reason)")
    return data


# ---------------------------------------------------------------------------
# Main resolver
# ---------------------------------------------------------------------------
def resolve_service_period(
    *,
    invoice_date: datetime,
    reading_dates_on_or_before: Optional[Sequence[datetime]] = None,
    explicit_period_from_document: Optional[tuple[datetime, datetime]] = None,
    vendor_rules: Optional[dict] = None,
    vendor_folder: Optional[Path] = None,
    logger: Optional[logging.Logger] = None,
) -> ServicePeriodResult:
    """Resolve the service period for a single bill.

    Args:
      invoice_date: the bill date / latest BILLING transaction date.
      reading_dates_on_or_before: meter-reading row dates ≤ invoice_date,
          oldest first. If at least 2 are present, the resolver uses the
          last two as start/end.
      explicit_period_from_document: an explicit (start, end) tuple if the
          vendor processor extracted dates directly from a PDF/bill.
      vendor_rules: the YAML `service_period_rules` block.
      vendor_folder: where to look for the batch override file.
      logger: optional logger.
    """
    log = logger or logging.getLogger(__name__)
    rules = vendor_rules or {}
    output_format = rules.get("output_format") or DEFAULT_OUTPUT_FORMAT

    if not rules.get("enabled", True):
        # Resolver is disabled — pass back manual-review.
        return ServicePeriodResult(
            start_date=None, end_date=None, formatted_range="",
            source=SOURCE_MANUAL_REVIEW, rule_used="resolver_disabled",
            inferred=False,
            manual_review_reasons=["service_period_rules_disabled"],
        )

    priority = rules.get("source_priority") or [
        SOURCE_DOCUMENT_EXPLICIT, SOURCE_READING_ROWS,
        SOURCE_BATCH_OVERRIDE, SOURCE_VENDOR_DEFAULT_FALLBACK, SOURCE_MANUAL_REVIEW,
    ]

    explicit_dates_found = bool(explicit_period_from_document and
                                explicit_period_from_document[0] and
                                explicit_period_from_document[1])

    # ---------- LEVEL 1: document_explicit_dates ----------
    if SOURCE_DOCUMENT_EXPLICIT in priority:
        cfg = rules.get(SOURCE_DOCUMENT_EXPLICIT) or {}
        if cfg.get("enabled", True) and explicit_dates_found:
            s, e = explicit_period_from_document  # type: ignore[misc]
            return ServicePeriodResult(
                start_date=s, end_date=e,
                formatted_range=_format_range(s, e, output_format),
                source=SOURCE_DOCUMENT_EXPLICIT,
                rule_used="document_dates_used_directly",
                inferred=False,
                explicit_dates_found=True,
            )

    # ---------- LEVEL 2: reading_rows_if_available ----------
    if SOURCE_READING_ROWS in priority:
        cfg = rules.get(SOURCE_READING_ROWS) or {}
        if cfg.get("enabled", True) and reading_dates_on_or_before:
            sorted_reads = sorted(d for d in reading_dates_on_or_before if d)
            if len(sorted_reads) >= 1:
                # Period = most recent reading on/before invoice_date as START,
                # invoice_date itself as END. (Common pattern: meter is read
                # near the start of the cycle, bill is dated end-of-cycle.)
                start = sorted_reads[-1]
                end = invoice_date
                return ServicePeriodResult(
                    start_date=start, end_date=end,
                    formatted_range=_format_range(start, end, output_format),
                    source=SOURCE_READING_ROWS,
                    rule_used="most_recent_reading_to_billing_date",
                    inferred=False,
                )

    # We've now exhausted both "explicit"-type sources. Whatever fires below
    # is by definition inferred.

    # ---------- LEVEL 3: batch_override ----------
    if SOURCE_BATCH_OVERRIDE in priority:
        cfg = rules.get(SOURCE_BATCH_OVERRIDE) or {}
        if cfg.get("enabled", True) and vendor_folder is not None:
            override_filename = cfg.get("config_file") or "batch_service_period_override.yaml"
            override = _load_batch_override(vendor_folder, override_filename, log)
            if override:
                method = (override.get("method")
                          or "inferred_from_invoice_date_offsets")
                inner = override.get(method) or {}
                try:
                    s, e = _resolve_offset_dates(invoice_date, inner)
                except Exception as ex:
                    log.warning("Batch override '%s' could not be applied: %s", method, ex)
                else:
                    review_reasons: list[str] = ["service_period_inferred_from_batch_override"]
                    if override.get("manual_review_required_when_used", True):
                        review_reasons.append("manual_review_required_when_batch_override_used")
                    return ServicePeriodResult(
                        start_date=s, end_date=e,
                        formatted_range=_format_range(s, e, output_format),
                        source=SOURCE_BATCH_OVERRIDE,
                        rule_used=method,
                        inferred=True,
                        batch_override_used=True,
                        manual_review_reasons=review_reasons,
                        user_warning=(
                            "Explicit service/reading dates were not found in the input "
                            "document. The service period was inferred using the BATCH "
                            "OVERRIDE rule defined in 'batch_service_period_override.yaml'. "
                            "This rule applies only to this run; delete or disable the "
                            "file when the batch is done."
                        ),
                    )

    # ---------- LEVEL 4: vendor_default_fallback ----------
    if SOURCE_VENDOR_DEFAULT_FALLBACK in priority:
        cfg = rules.get(SOURCE_VENDOR_DEFAULT_FALLBACK) or {}
        if cfg.get("enabled", False):
            method = cfg.get("method") or "inferred_from_invoice_date_offsets"
            inner = cfg.get(method) or {}
            try:
                s, e = _resolve_offset_dates(invoice_date, inner)
            except Exception as ex:
                log.warning(
                    "Vendor default fallback '%s' could not be applied: %s", method, ex,
                )
            else:
                review_reasons = []
                if cfg.get("manual_review_required_when_used", True):
                    review_reasons.append("service_period_inferred_from_vendor_default_fallback")
                return ServicePeriodResult(
                    start_date=s, end_date=e,
                    formatted_range=_format_range(s, e, output_format),
                    source=SOURCE_VENDOR_DEFAULT_FALLBACK,
                    rule_used=method,
                    inferred=True,
                    vendor_default_fallback_used=True,
                    manual_review_reasons=review_reasons,
                    user_warning=(
                        "Explicit service/reading dates were not found in the input "
                        "document. The service period was inferred using the configured "
                        "YAML fallback rule. Please verify whether this rule should "
                        "apply only to this batch (move it to "
                        "batch_service_period_override.yaml) or should remain as the "
                        "vendor default."
                    ),
                )

    # ---------- LEVEL 5: manual_review ----------
    missing_cfg = rules.get("missing_explicit_dates_behavior") or {}
    reason = missing_cfg.get("manual_review_reason",
                             "missing_explicit_service_or_reading_dates")
    return ServicePeriodResult(
        start_date=None, end_date=None,
        formatted_range="",
        source=SOURCE_MANUAL_REVIEW,
        rule_used="no_rule_applicable",
        inferred=False,
        manual_review_reasons=[reason],
        user_warning=(
            "Explicit service/reading dates were not found and no fallback rule "
            "is enabled. Please create a batch_service_period_override.yaml for "
            "this batch (see *.example.yaml in the vendor folder), or configure "
            "service_period_rules.vendor_default_fallback in the vendor YAML."
        ),
    )
