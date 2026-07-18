"""
Reusable Dropbox uploader helper.

Used by every vendor processor that needs to upload a support document
(e.g. the original utility bill or the billing-history file) to Dropbox
and obtain a shareable link to put in the ResMan import template's
last column.

Design goals (per the project brief):
  * Read credentials from environment variables / .env — NEVER hardcode.
  * Never print or log full token values.
  * Return a structured result object so callers can distinguish success,
    failure, and missing-credentials cases.
  * Don't crash the caller if Dropbox is unreachable, the SDK is missing,
    or credentials are absent. Just return a result with success=False.
  * Configurable Dropbox base folder + per-vendor sub-folder pattern.
  * Reuse existing shared links instead of creating duplicates.
  * Rewrite "?dl=0" → "?dl=1" so the link is a direct-download URL.

Environment variables (looked up in this order; the OAuth refresh-token
flow takes precedence over a plain access token):

    DROPBOX_REFRESH_TOKEN  + DROPBOX_APP_KEY + DROPBOX_APP_SECRET   (preferred)
    DROPBOX_ACCESS_TOKEN                                            (legacy)
    DROPBOX_BASE_FOLDER                                             (default: "/Billing_Refactoring_2026")

If `python-dotenv` is installed and a `.env` file exists at the project
root, those values are loaded into `os.environ` automatically. The .env
file itself is gitignored.

Typical caller:

    from utils.dropbox_uploader import DropboxUploader, build_dropbox_path
    uploader = DropboxUploader.from_env()
    if uploader.is_configured:
        dst = build_dropbox_path(
            base_folder=uploader.base_folder,
            vendor_name="Richmond Utilities",
            billing_date=billing_date,
            filename=path.name,
        )
        result = uploader.upload(local_path=path, dropbox_path=dst)
        if result.success:
            row[document_url_col] = result.shared_link
        else:
            # graceful degradation; caller flags manual review
            ...
"""

from __future__ import annotations

import logging
import os
import base64
import http.client
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

# python-dotenv is optional. If it's installed and there's a .env file at the
# project root, we'll load it automatically. If not, environment variables
# still work the normal way.
try:
    from dotenv import load_dotenv
    _HAS_DOTENV = True
except Exception:
    _HAS_DOTENV = False

# The Dropbox SDK is also optional at import time. We tolerate it being
# missing so the rest of the project still runs.
try:
    import dropbox  # type: ignore
    from dropbox.files import WriteMode  # type: ignore
    from dropbox.exceptions import ApiError, AuthError  # type: ignore
    _HAS_DROPBOX = True
except Exception:
    _HAS_DROPBOX = False


# ---------------------------------------------------------------------------
# .env loader (project-root aware)
# ---------------------------------------------------------------------------
def _find_project_root_from_module() -> Optional[Path]:
    """Walk up from this module until we find the project's identifying
    markers. Used only to locate an optional .env file."""
    here = Path(__file__).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "config" / "vendors").is_dir() and (candidate / "Output" / "Template.xlsx").is_file():
            return candidate
    return None


_DOTENV_LOADED = False


def _load_dotenv_once() -> None:
    """Load .env into os.environ exactly once per process. Quietly no-op if
    python-dotenv isn't installed or there's no .env file."""
    global _DOTENV_LOADED
    if _DOTENV_LOADED or not _HAS_DOTENV:
        _DOTENV_LOADED = True
        return
    root = _find_project_root_from_module()
    if root is None:
        _DOTENV_LOADED = True
        return
    env_path = root / ".env"
    if env_path.is_file():
        load_dotenv(env_path, override=False)
    _DOTENV_LOADED = True


# ---------------------------------------------------------------------------
# Path helper
# ---------------------------------------------------------------------------
MONTH_ABBREVS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                 "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def build_dropbox_path(
    *,
    base_folder: str,
    vendor_name: str,
    billing_date: Optional[datetime],
    filename: str,
    folder_pattern: str = "{base_folder}/{vendor_name}/{year}/{month_number} - {month_abbrev}",
) -> str:
    """Build the destination Dropbox path.

    Pattern placeholders:
      {base_folder}    — value of DROPBOX_BASE_FOLDER (or the YAML override)
      {vendor_name}    — display name of the vendor
      {year}           — 4-digit year of billing_date (or current year if None)
      {month_number}   — zero-padded 2-digit month (e.g. "04")
      {month_abbrev}   — title-case 3-letter month (e.g. "Apr")

    The filename is appended to the resulting folder. The path always
    starts with a single "/" and never contains "//".
    """
    if not billing_date:
        billing_date = datetime.now()
    folder = folder_pattern.format(
        base_folder=base_folder.rstrip("/"),
        vendor_name=vendor_name,
        year=billing_date.strftime("%Y"),
        month_number=billing_date.strftime("%m"),
        month_abbrev=MONTH_ABBREVS[billing_date.month - 1],
    )
    if not folder.startswith("/"):
        folder = "/" + folder
    # Collapse any accidental double-slashes
    while "//" in folder:
        folder = folder.replace("//", "/")
    return f"{folder}/{filename}"


# ---------------------------------------------------------------------------
# Token-redaction helper for safe logging
# ---------------------------------------------------------------------------
def _redact(s: Optional[str]) -> str:
    """Render a token for logs without leaking its value. Shows first 3
    characters, length, and 'REDACTED'. Empty/None becomes '<unset>'."""
    if not s:
        return "<unset>"
    return f"{s[:3]}…(len={len(s)},REDACTED)"


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------
@dataclass
class UploadResult:
    success: bool
    shared_link: str = ""
    dropbox_path: str = ""
    error_kind: str = ""           # "credentials_missing" | "sdk_missing" | "auth" | "api" | "io" | ""
    error_message: str = ""        # safe (no token), human-readable

    def __bool__(self) -> bool:
        return self.success


# ---------------------------------------------------------------------------
# DropboxUploader
# ---------------------------------------------------------------------------
class DropboxUploader:
    """Thin wrapper around the Dropbox SDK with safe defaults.

    Use `DropboxUploader.from_env()` to build one from environment
    variables. The constructor itself does not raise — `is_configured` is
    False if credentials are missing, and `upload()` short-circuits to a
    structured failure rather than throwing."""

    def __init__(
        self,
        *,
        access_token: Optional[str] = None,
        refresh_token: Optional[str] = None,
        app_key: Optional[str] = None,
        app_secret: Optional[str] = None,
        base_folder: str = "/Billing_Refactoring_2026",
        logger: Optional[logging.Logger] = None,
    ):
        self.access_token = access_token or None
        self.refresh_token = refresh_token or None
        self.app_key = app_key or None
        self.app_secret = app_secret or None
        self.base_folder = (base_folder or "/Billing_Refactoring_2026").strip() or "/Billing_Refactoring_2026"
        if not self.base_folder.startswith("/"):
            self.base_folder = "/" + self.base_folder
        self.logger = logger or logging.getLogger(__name__)
        self._client: Optional["dropbox.Dropbox"] = None

    # --- factory ---
    @classmethod
    def from_env(cls, logger: Optional[logging.Logger] = None) -> "DropboxUploader":
        _load_dotenv_once()
        return cls(
            access_token=os.environ.get("DROPBOX_ACCESS_TOKEN"),
            refresh_token=os.environ.get("DROPBOX_REFRESH_TOKEN"),
            app_key=os.environ.get("DROPBOX_APP_KEY"),
            app_secret=os.environ.get("DROPBOX_APP_SECRET"),
            base_folder=os.environ.get("DROPBOX_BASE_FOLDER", "/Billing_Refactoring_2026"),
            logger=logger,
        )

    # --- introspection ---
    @property
    def is_configured(self) -> bool:
        # OAuth refresh-token flow needs all three; legacy flow needs an access token.
        if self.refresh_token and self.app_key and self.app_secret:
            return True
        if self.access_token:
            return True
        return False

    @property
    def auth_mode(self) -> str:
        if self.refresh_token and self.app_key and self.app_secret:
            return "refresh_token"
        if self.access_token:
            return "access_token"
        if not _HAS_DROPBOX:
            return "sdk_missing"
        return "credentials_missing"

    def credential_summary(self) -> dict:
        """Safe-to-log summary. No token values."""
        return {
            "sdk_installed": _HAS_DROPBOX,
            "auth_mode": self.auth_mode,
            "is_configured": self.is_configured,
            "base_folder": self.base_folder,
            "access_token": _redact(self.access_token),
            "refresh_token": _redact(self.refresh_token),
            "app_key": _redact(self.app_key),
            "app_secret": _redact(self.app_secret),
        }

    # --- internal client ---
    def _get_client(self):
        if not _HAS_DROPBOX:
            return None
        if self._client is not None:
            return self._client
        if self.refresh_token and self.app_key and self.app_secret:
            self._client = dropbox.Dropbox(
                oauth2_refresh_token=self.refresh_token,
                app_key=self.app_key,
                app_secret=self.app_secret,
            )
        elif self.access_token:
            self._client = dropbox.Dropbox(self.access_token)
        return self._client

    # --- main entry point ---
    def upload(self, *, local_path: Path, dropbox_path: str, overwrite: bool = True) -> UploadResult:
        """Upload `local_path` to `dropbox_path`, then return a shareable URL.
        Reuses an existing shared link if one already exists, otherwise creates
        a new one. Always returns an UploadResult — never raises."""
        if not self.is_configured:
            return UploadResult(
                success=False,
                error_kind="credentials_missing",
                error_message="No Dropbox credentials in environment. Set DROPBOX_REFRESH_TOKEN+APP_KEY+APP_SECRET, or DROPBOX_ACCESS_TOKEN.",
            )
        if not local_path.is_file():
            return UploadResult(
                success=False,
                error_kind="io",
                error_message=f"Local file not found: {local_path}",
            )
        if not dropbox_path.startswith("/"):
            dropbox_path = "/" + dropbox_path

        if not _HAS_DROPBOX:
            return self._upload_with_stdlib(local_path=local_path, dropbox_path=dropbox_path, overwrite=overwrite)

        client = self._get_client()
        try:
            with open(local_path, "rb") as f:
                client.files_upload(
                    f.read(),
                    dropbox_path,
                    mode=WriteMode("overwrite") if overwrite else WriteMode("add"),
                )
        except AuthError as e:
            return UploadResult(success=False, dropbox_path=dropbox_path,
                                error_kind="auth", error_message=f"Auth error: {type(e).__name__}")
        except ApiError as e:
            return UploadResult(success=False, dropbox_path=dropbox_path,
                                error_kind="api", error_message=f"API error during upload: {type(e).__name__}")
        except Exception as e:
            detail = "SSL verification" if _looks_like_ssl_error(e) else type(e).__name__
            self.logger.warning(
                "Dropbox SDK upload failed with %s; retrying with system HTTP fallback.",
                detail,
            )
            fallback = self._upload_with_stdlib(
                local_path=local_path,
                dropbox_path=dropbox_path,
                overwrite=overwrite,
            )
            if fallback.success:
                return fallback
            return UploadResult(success=False, dropbox_path=dropbox_path,
                                error_kind=fallback.error_kind or "io",
                                error_message=fallback.error_message or f"Unexpected upload error: {type(e).__name__}")

        # Get or create a shared link
        try:
            existing = client.sharing_list_shared_links(path=dropbox_path, direct_only=True)
            if existing.links:
                url = existing.links[0].url
            else:
                url = client.sharing_create_shared_link_with_settings(dropbox_path).url
        except ApiError as e:
            return UploadResult(success=False, dropbox_path=dropbox_path,
                                error_kind="api",
                                error_message=f"API error creating share link: {type(e).__name__}")
        except Exception as e:
            detail = type(e).__name__
            self.logger.warning(
                "Dropbox SDK share-link lookup failed with %s; retrying with system HTTP fallback.",
                detail,
            )
            fallback = self._upload_with_stdlib(
                local_path=local_path,
                dropbox_path=dropbox_path,
                overwrite=overwrite,
            )
            if fallback.success:
                return fallback
            return UploadResult(success=False, dropbox_path=dropbox_path,
                                error_kind=fallback.error_kind or "io",
                                error_message=fallback.error_message or f"Unexpected share-link error: {detail}")

        # Legacy share links of the form ".../<file>?dl=0" get rewritten
        # to "?dl=1" (direct download) — the original convention. The
        # newer SCL/RLkey share links keep their trailing "&dl=0" so
        # they open the Dropbox preview page on click; this matches the
        # behaviour the project already uses for HWEA / Richmond / etc.
        # and keeps every vendor's links consistent.
        url = url.replace("?dl=0", "?dl=1")
        return UploadResult(success=True, dropbox_path=dropbox_path, shared_link=url)

    def _upload_with_stdlib(self, *, local_path: Path, dropbox_path: str, overwrite: bool = True) -> UploadResult:
        """Dropbox upload fallback that uses urllib + the OS certificate store.

        On some Windows desktops, ``requests``/``certifi`` cannot validate the
        local TLS chain while the standard library succeeds through the Windows
        trust store. This fallback keeps the upload path working without
        disabling TLS verification or logging credentials.
        """
        try:
            token = self._stdlib_access_token()
            mode = "overwrite" if overwrite else "add"
            arg = {
                "path": dropbox_path,
                "mode": {".tag": mode},
                "autorename": False,
                "mute": False,
                "strict_conflict": False,
            }
            upload_headers = {
                "Authorization": f"Bearer {token}",
                "Dropbox-API-Arg": json.dumps(arg),
                "Content-Type": "application/octet-stream",
                "User-Agent": _DROPBOX_HTTP_USER_AGENT,
            }
            _stdlib_request(
                "https://content.dropboxapi.com/2/files/upload",
                headers=upload_headers,
                data=local_path.read_bytes(),
            )
            list_payload = {"path": dropbox_path, "direct_only": True}
            existing = _stdlib_request_json(
                "https://api.dropboxapi.com/2/sharing/list_shared_links",
                token=token,
                payload=list_payload,
            )
            links = existing.get("links") if isinstance(existing, dict) else []
            if links:
                url = str((links[0] or {}).get("url") or "")
            else:
                created = _stdlib_request_json(
                    "https://api.dropboxapi.com/2/sharing/create_shared_link_with_settings",
                    token=token,
                    payload={"path": dropbox_path},
                )
                url = str((created or {}).get("url") or "")
            if not url:
                return UploadResult(
                    success=False,
                    dropbox_path=dropbox_path,
                    error_kind="api",
                    error_message="Dropbox did not return a shared link.",
                )
            return UploadResult(
                success=True,
                dropbox_path=dropbox_path,
                shared_link=url.replace("?dl=0", "?dl=1"),
            )
        except urllib.error.HTTPError as e:
            return UploadResult(
                success=False,
                dropbox_path=dropbox_path,
                error_kind="api",
                error_message=f"Dropbox HTTP error: {e.code}",
            )
        except urllib.error.URLError as e:
            return UploadResult(
                success=False,
                dropbox_path=dropbox_path,
                error_kind="api",
                error_message=f"Dropbox network error: {type(e.reason).__name__}",
            )
        except Exception as e:
            return UploadResult(
                success=False,
                dropbox_path=dropbox_path,
                error_kind="io",
                error_message=f"Unexpected Dropbox fallback error: {type(e).__name__}",
            )

    def _stdlib_access_token(self) -> str:
        # Keep the same precedence as the SDK path: refresh-token auth is
        # preferred because legacy access tokens can expire or be revoked even
        # when the long-lived refresh token is valid.
        if not (self.refresh_token and self.app_key and self.app_secret):
            if self.access_token:
                return self.access_token
            raise RuntimeError("Dropbox credentials missing")
        credentials = f"{self.app_key}:{self.app_secret}".encode("utf-8")
        headers = {
            "Authorization": "Basic " + base64.b64encode(credentials).decode("ascii"),
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": _DROPBOX_HTTP_USER_AGENT,
        }
        data = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
        }).encode("utf-8")
        payload = _stdlib_request(
            "https://api.dropboxapi.com/oauth2/token",
            headers=headers,
            data=data,
        )
        token = str((json.loads(payload.decode("utf-8")) or {}).get("access_token") or "")
        if not token:
            raise RuntimeError("Dropbox token refresh returned no access token")
        return token


def _looks_like_ssl_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return "ssl" in text or "certificate_verify_failed" in text or "certificateverifyfailed" in text


_RETRYABLE_NETWORK_ERRORS = (
    http.client.RemoteDisconnected,
    ConnectionResetError,
    TimeoutError,
    socket.timeout,
)

_DROPBOX_HTTP_USER_AGENT = "BillingRefactoring2026/1.0"


def _is_retryable_url_error(exc: urllib.error.URLError) -> bool:
    reason = getattr(exc, "reason", None)
    if isinstance(reason, _RETRYABLE_NETWORK_ERRORS):
        return True
    text = f"{type(reason).__name__}: {reason}".lower()
    return (
        "remotedisconnected" in text
        or "connection reset" in text
        or "timed out" in text
        or "temporarily unavailable" in text
    )


def _stdlib_request(
    url: str,
    *,
    headers: dict[str, str],
    data: bytes,
    timeout: int = 100,
    attempts: int = 3,
) -> bytes:
    last_error: Exception | None = None
    request_headers = {
        "User-Agent": _DROPBOX_HTTP_USER_AGENT,
        "Accept": "*/*",
        "Connection": "close",
        **headers,
    }
    for attempt in range(max(1, attempts)):
        request = urllib.request.Request(url, data=data, headers=request_headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError:
            raise
        except urllib.error.URLError as exc:
            if not _is_retryable_url_error(exc):
                raise
            last_error = exc
        except _RETRYABLE_NETWORK_ERRORS as exc:
            last_error = exc

        if attempt < attempts - 1:
            time.sleep(min(0.35 * (2 ** attempt), 2.0))

    if last_error is not None:
        raise last_error
    raise RuntimeError("Dropbox request failed without an error detail.")


def _stdlib_request_json(url: str, *, token: str, payload: dict, timeout: int = 100) -> dict:
    data = json.dumps(payload).encode("utf-8")
    body = _stdlib_request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": _DROPBOX_HTTP_USER_AGENT,
        },
        data=data,
        timeout=timeout,
    )
    return json.loads(body.decode("utf-8") or "{}")
