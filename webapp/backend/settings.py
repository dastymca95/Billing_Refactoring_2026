"""Project paths used by every backend service."""

from __future__ import annotations

import sys
import re
from datetime import datetime
from pathlib import Path


def _find_project_root() -> Path:
    here = Path(__file__).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "config" / "vendors").is_dir() and (candidate / "Output" / "Template.xlsx").is_file():
            return candidate
    raise RuntimeError("Could not locate project root from " + str(here))


PROJECT_ROOT = _find_project_root()

# Make the project root importable so backend can import existing modules
# (`utils.dropbox_uploader`, the Richmond processor, etc.).
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Webapp runtime data
WEBAPP_DATA_ROOT = PROJECT_ROOT / "webapp_data"
BATCHES_ROOT = WEBAPP_DATA_ROOT / "batches"

BATCH_ID_PATTERN = re.compile(r"^batch_\d{8}_\d{6}_\d{3}$")


class InvalidBatchIdError(ValueError):
    """Raised when a caller supplies a batch id outside the generated format."""

# Existing project assets (read-only)
RESMAN_TEMPLATE = PROJECT_ROOT / "Output" / "Template.xlsx"
VENDORS_INDEX_YAML = PROJECT_ROOT / "config" / "vendor_rules_index.yaml"
VENDORS_DIR = PROJECT_ROOT / "config" / "vendors"

# Allowed file extensions for upload
ALLOWED_UPLOAD_EXTENSIONS = {
    ".csv", ".xlsx", ".xls", ".pdf",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp",
    ".docx", ".doc",
    ".txt",
}


def new_batch_id() -> str:
    return "batch_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]


def validate_batch_id(batch_id: str) -> str:
    if not BATCH_ID_PATTERN.fullmatch(batch_id or ""):
        raise InvalidBatchIdError("Invalid batch id")
    return batch_id


def is_valid_batch_id(batch_id: str) -> bool:
    return BATCH_ID_PATTERN.fullmatch(batch_id or "") is not None


def batch_dir(batch_id: str) -> Path:
    safe_id = validate_batch_id(batch_id)
    root = BATCHES_ROOT.resolve()
    candidate = (root / safe_id).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise InvalidBatchIdError("Invalid batch id")
    return candidate
