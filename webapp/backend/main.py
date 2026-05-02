"""FastAPI entrypoint.

Run from the project root:

    "./.venv/Scripts/python.exe" -m uvicorn webapp.backend.main:app --reload --port 8000

Then point the React dev server at http://localhost:8000 (Vite handles the
proxy in webapp/frontend/vite.config.ts).
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .settings import BATCHES_ROOT
from .api import batches, uploads, preview, processing, export, regions, ai_status


def create_app() -> FastAPI:
    BATCHES_ROOT.mkdir(parents=True, exist_ok=True)

    app = FastAPI(
        title="Billing Refactoring 2026 — Webapp Backend",
        version="0.1.0",
        description="Phase 1: drag/drop → process → preview → export. Richmond Utilities only.",
    )

    # During local dev the Vite frontend runs on http://localhost:5173 and
    # proxies /api/* through to here, but we still allow CORS so the
    # frontend can fetch raw files cross-origin in case the proxy is bypassed.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(batches.router)
    app.include_router(uploads.router)
    app.include_router(preview.router)
    app.include_router(processing.router)
    app.include_router(export.router)
    # Phase 1H — region hints + AI status.
    app.include_router(regions.router)
    app.include_router(ai_status.router)

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "service": "billing_refactoring_2026_webapp"}

    @app.get("/")
    def root() -> dict:
        return {
            "name": "Billing Refactoring 2026 — Webapp Backend",
            "frontend_dev_url": "http://localhost:5173",
            "docs": "/docs",
        }

    return app


app = create_app()
