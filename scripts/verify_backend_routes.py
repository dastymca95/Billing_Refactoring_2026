"""Verify the webapp backend exposes the critical Phase 1Q route contract.

Run from the project root:

    python scripts/verify_backend_routes.py
"""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp.backend.main import app  # noqa: E402


REQUIRED_ROUTES: set[tuple[str, str]] = {
    ("POST", "/api/batches"),
    ("GET", "/api/batches"),
    ("GET", "/api/batches/{batch_id}"),
    ("PATCH", "/api/batches/{batch_id}"),
    ("DELETE", "/api/batches/{batch_id}"),
    ("POST", "/api/batches/{batch_id}/upload"),
    ("GET", "/api/batches/{batch_id}/files"),
    ("DELETE", "/api/batches/{batch_id}/files/{filename}"),
    ("POST", "/api/batches/{batch_id}/process"),
    ("POST", "/api/batches/{batch_id}/cancel"),
    ("POST", "/api/batches/{batch_id}/export"),
    ("GET", "/api/batches/{batch_id}/regions"),
    ("PUT", "/api/batches/{batch_id}/regions"),
    ("GET", "/api/ai/status"),
}


def main() -> int:
    actual: set[tuple[str, str]] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not path or not methods:
            continue
        for method in methods:
            if method in {"HEAD", "OPTIONS"}:
                continue
            actual.add((method, path))

    missing = sorted(REQUIRED_ROUTES - actual)
    if missing:
        print("Missing backend routes:")
        for method, path in missing:
            print(f"  {method:6} {path}")
        return 1

    print("Backend route contract OK.")
    for method, path in sorted(REQUIRED_ROUTES):
        print(f"  {method:6} {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
