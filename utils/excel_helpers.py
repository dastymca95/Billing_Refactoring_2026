"""Phase 2L — Shared Excel cell-write helpers.

Centralises the conversion of preview-row values into properly-typed
Excel cells so every vendor's workbook (HWEA, Richmond, Columbia, …)
writes consistent output. The big one is **date columns**: ResMan
expects real Excel date cells, not text. Without this the import
either rejects the file or coerces dates incorrectly.

Usage
-----
::

    from utils.excel_helpers import set_cell

    for col_name, value in row.items():
        if col_name.startswith("_"):
            continue
        col_idx = header_index.get(col_name)
        if not col_idx:
            continue
        set_cell(sheet.cell(row=r_idx, column=col_idx), value, col_name)
"""

from __future__ import annotations

import warnings
from datetime import date, datetime
from typing import Any


# The ResMan template ships with Data Validation rules openpyxl drops
# at load time, emitting a UserWarning each time. The warning is purely
# cosmetic — the dropped rules don't affect AP imports — so we silence
# it here once. Importing this module is enough; every workbook writer
# that uses ``set_cell`` already imports it.
warnings.filterwarnings(
    "ignore",
    message=r"Data Validation extension is not supported and will be removed",
    category=UserWarning,
    module=r"openpyxl(\..*)?",
)


_DATE_COLUMN_NAMES = {"Invoice Date", "Accounting Date", "Due Date"}
_DATE_NUMBER_FORMAT = "MM/DD/YYYY"

_DATE_INPUT_FORMATS = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%m-%d-%Y",
    "%m-%d-%y",
    "%Y/%m/%d",
)


def _to_date(value: Any) -> Any:
    """Best-effort coerce ``value`` to a :class:`datetime.date`.

    Returns the original value when it can't be parsed — caller
    decides whether to surface that as a manual-review flag or just
    write the raw string."""
    if value in (None, ""):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s:
        return value
    for fmt in _DATE_INPUT_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return value


def set_cell(cell, value: Any, column_name: str) -> None:
    """Write ``value`` to ``cell`` with the right Excel type.

    * Date columns (Invoice Date / Accounting Date / Due Date) are
      coerced to :class:`datetime.date` and given a ``MM/DD/YYYY``
      number format so Excel renders + sorts them as real dates.
    * Anything else is written as-is (numbers stay numeric, booleans
      stay boolean).
    """
    if column_name in _DATE_COLUMN_NAMES:
        coerced = _to_date(value)
        cell.value = coerced
        if isinstance(coerced, date):
            cell.number_format = _DATE_NUMBER_FORMAT
        return
    cell.value = value
