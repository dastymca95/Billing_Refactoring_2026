"""Project paths used by every backend service."""

from __future__ import annotations

import os
import sys
import re
from datetime import datetime
from pathlib import Path


def _find_project_root() -> Path:
    here = Path(__file__).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "webapp" / "backend").is_dir() and (candidate / "config" / "canonical_rules.yaml").is_file():
            return candidate
    raise RuntimeError("Could not locate project root from " + str(here))


PROJECT_ROOT = _find_project_root()
RUNTIME_ASSET_ROOT = Path(os.environ.get("INNER_VIEW_TEST_ASSET_ROOT") or PROJECT_ROOT).resolve()


def _load_project_env() -> None:
    """Small .env loader for local webapp settings.

    Keeps the project dependency-free and uses ``setdefault`` so process env
    variables still win. Values are never logged.
    """
    env_path = PROJECT_ROOT / ".env"
    if not env_path.is_file():
        return
    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, value)
    except OSError:
        return


_load_project_env()

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
RESMAN_TEMPLATE = RUNTIME_ASSET_ROOT / "Output" / "Template.xlsx"
VENDORS_INDEX_YAML = PROJECT_ROOT / "config" / "vendor_rules_index.yaml"
VENDORS_DIR = RUNTIME_ASSET_ROOT / "config" / "vendors"
GENERAL_LEDGER_REFERENCE = PROJECT_ROOT / "config" / "general_ledger_reference.yaml"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# Phase AI-1: provider-agnostic invoice extraction. Disabled by default.
# These values are read only by backend services; API responses never expose
# API keys.
AI_ASSIST_ENABLED = _env_bool("AI_ASSIST_ENABLED", False)
AI_PROVIDER = os.environ.get("AI_PROVIDER", "").strip()
AI_MODEL = os.environ.get("AI_MODEL", "").strip()
AI_API_KEY = os.environ.get("AI_API_KEY", "").strip()
AI_BASE_URL = os.environ.get("AI_BASE_URL", "").strip()
AI_TIMEOUT_SECONDS = _env_int("AI_TIMEOUT_SECONDS", 45)
AI_MAX_TEXT_CHARS = _env_int("AI_MAX_TEXT_CHARS", 45000)
AI_MAX_OUTPUT_CHARS = _env_int("AI_MAX_OUTPUT_CHARS", 20000)
AI_MAX_RESPONSE_TOKENS = _env_int("AI_MAX_RESPONSE_TOKENS", 4096)
AI_MAX_PAGES = _env_int("AI_MAX_PAGES", 5)
AI_MOCK_MODE = os.environ.get("AI_MOCK_MODE", "").strip()
AI_MOCK_DELAY_SECONDS = _env_int("AI_MOCK_DELAY_SECONDS", 0)
AI_TAX_HANDLING = os.environ.get("AI_TAX_HANDLING", "distribute_proportionally").strip().lower()
AI_INCLUDE_ZERO_AMOUNT_LINES = _env_bool("AI_INCLUDE_ZERO_AMOUNT_LINES", False)
AI_VISION_ENABLED = _env_bool("AI_VISION_ENABLED", False)
AI_VISION_PROVIDER = os.environ.get("AI_VISION_PROVIDER", "").strip()
AI_VISION_MODEL = os.environ.get("AI_VISION_MODEL", "").strip()
AI_VISION_API_KEY = os.environ.get("AI_VISION_API_KEY", "").strip()
AI_VISION_BASE_URL = os.environ.get("AI_VISION_BASE_URL", "").strip()
AI_VISION_MAX_PAGES = _env_int("AI_VISION_MAX_PAGES", 2)
AI_VISION_MAX_IMAGE_WIDTH = _env_int("AI_VISION_MAX_IMAGE_WIDTH", 1600)
AI_VISION_MODE = os.environ.get("AI_VISION_MODE", "fallback_only").strip().lower()

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
