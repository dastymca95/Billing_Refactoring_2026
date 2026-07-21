"""Project paths used by every backend service."""

from __future__ import annotations

import os
import sys
import re
from datetime import datetime
from pathlib import Path
from typing import Mapping


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
        private_label_aliases = {
            "deepseek api": "DEEPSEEK_API_KEY",
            "gemini api": "GEMINI_API_KEY",
            "claude api": "ANTHROPIC_API_KEY",
        }
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, value = line.split("=", 1)
            elif ":" in line:
                # Temporary private-workspace adapter for the original labels
                # entered by the owner.  The value remains in-process only and
                # is never logged or serialized. Standard NAME=value remains
                # the documented format.
                label, value = line.split(":", 1)
                key = private_label_aliases.get(label.strip().casefold(), "")
                if not key:
                    continue
            else:
                continue
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
def _resolve_webapp_data_root(environment: Mapping[str, str] | None = None) -> Path:
    """Resolve an isolated experiment runtime before any store is imported."""
    env = environment if environment is not None else os.environ
    configured = str(env.get("INNER_VIEW_WEBAPP_DATA_ROOT") or "").strip()
    experiment_mode = str(env.get("INNER_VIEW_EXPERIMENT_MODE") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }
    canonical = (PROJECT_ROOT / "webapp_data").resolve()
    if not experiment_mode:
        if configured:
            raise RuntimeError(
                "INNER_VIEW_WEBAPP_DATA_ROOT is valid only when "
                "INNER_VIEW_EXPERIMENT_MODE is explicitly enabled."
            )
        return canonical

    if not configured:
        raise RuntimeError("INNER_VIEW_WEBAPP_DATA_ROOT is required in experiment mode.")
    candidate = Path(configured).expanduser().resolve()
    tenant_id = _normalized_tenant_id(env.get("INNER_VIEW_TENANT_ID"))
    authorized_tenant_id = _normalized_tenant_id(
        env.get("INNER_VIEW_EXPERIMENT_AUTHORIZED_TENANT_ID")
    )
    if not tenant_id or not tenant_id.startswith("exp-"):
        raise RuntimeError("Experiment mode requires a dedicated exp-* tenant.")
    if not authorized_tenant_id or not authorized_tenant_id.startswith("exp-"):
        raise RuntimeError(
            "INNER_VIEW_EXPERIMENT_AUTHORIZED_TENANT_ID must explicitly identify "
            "the dedicated experiment tenant."
        )
    if tenant_id != authorized_tenant_id:
        raise RuntimeError("Experiment tenant does not match the authorized tenant.")
    deployment = str(env.get("INNER_VIEW_DEPLOYMENT_MODE") or "").strip().lower()
    if deployment not in {"production", "prod"}:
        raise RuntimeError("Experiment mode requires production identity enforcement.")
    allowed_roots = [
        (PROJECT_ROOT / "tmp").resolve(),
        (PROJECT_ROOT / "webapp_data" / "experiments").resolve(),
    ]
    if not any(_is_within(candidate, root) for root in allowed_roots):
        raise RuntimeError("Experiment runtime must stay under ignored tmp/ or webapp_data/experiments/.")
    return candidate


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _normalized_tenant_id(value: object) -> str:
    return str(value or "").strip().casefold()


WEBAPP_DATA_ROOT = _resolve_webapp_data_root()
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
AI_VISION_TIMEOUT_SECONDS = _env_int("AI_VISION_TIMEOUT_SECONDS", 120)
AI_VISION_MAX_RESPONSE_TOKENS = _env_int("AI_VISION_MAX_RESPONSE_TOKENS", 8192)
AI_VISION_NATIVE_PDF_ENABLED = _env_bool("AI_VISION_NATIVE_PDF_ENABLED", False)
AI_VISION_NATIVE_PDF_DETAIL = os.environ.get(
    "AI_VISION_NATIVE_PDF_DETAIL", "high"
).strip().lower()
AI_VISION_NATIVE_PDF_REASONING_EFFORT = os.environ.get(
    "AI_VISION_NATIVE_PDF_REASONING_EFFORT", "medium"
).strip().lower()
AI_VISION_NATIVE_PDF_MAX_BYTES = _env_int(
    "AI_VISION_NATIVE_PDF_MAX_BYTES", 50 * 1024 * 1024
)
AI_VISION_NATIVE_PDF_MAX_RESPONSE_TOKENS = _env_int(
    "AI_VISION_NATIVE_PDF_MAX_RESPONSE_TOKENS", 32768
)
AI_VISION_NATIVE_PDF_TIMEOUT_SECONDS = _env_int(
    "AI_VISION_NATIVE_PDF_TIMEOUT_SECONDS", 240
)
AI_INVOICE_GROUP_WORKERS = _env_int("AI_INVOICE_GROUP_WORKERS", 4)
AI_PAGE_FACTS_ALLOW_PERSISTED_MIGRATION = _env_bool(
    "AI_PAGE_FACTS_ALLOW_PERSISTED_MIGRATION", False
)
# Fast-first remains shadow/benchmark-only until the exact golden comparison
# has been explicitly approved. A missing or ambiguous value is always off.
AI_FAST_FIRST_FACTS_ONLY_ENABLED = _env_bool("AI_FAST_FIRST_FACTS_ONLY_ENABLED", False)
AI_FAST_FIRST_GOLDEN_PARITY_APPROVED = _env_bool(
    "AI_FAST_FIRST_GOLDEN_PARITY_APPROVED", False
)

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
