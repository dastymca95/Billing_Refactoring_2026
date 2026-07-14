"""FastAPI entrypoint.

Run from the project root:

    "./.venv/Scripts/python.exe" -m uvicorn webapp.backend.main:app --reload --port 8000

Then point the React dev server at http://localhost:8000 (Vite handles the
proxy in webapp/frontend/vite.config.ts).
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .settings import BATCHES_ROOT, InvalidBatchIdError
from .api import (
    ai_status,
    ai_invoice,
    ai_mappings,
    billing_v2,
    batches,
    canonical_rules,
    cells,
    export,
    invoice_format_rules,
    preview,
    processing,
    regions,
    uploads,
    vendor_rules,
)


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

    @app.exception_handler(InvalidBatchIdError)
    async def invalid_batch_id_handler(
        request: Request,
        exc: InvalidBatchIdError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"detail": "Invalid batch id"},
        )

    app.include_router(batches.router)
    app.include_router(uploads.router)
    app.include_router(preview.router)
    app.include_router(processing.router)
    # Phase 2D — cross-batch processing queue endpoint.
    app.include_router(processing.queue_router)
    app.include_router(export.router)
    # Phase 1H — region hints + AI status.
    app.include_router(regions.router)
    app.include_router(ai_status.router)
    app.include_router(ai_invoice.router)
    app.include_router(ai_invoice.test_router)
    app.include_router(ai_invoice.batch_router)
    app.include_router(ai_mappings.router)
    app.include_router(ai_mappings.batch_router)
    # Phase 1Z — Vendor Rules Studio.
    app.include_router(vendor_rules.router)
    app.include_router(invoice_format_rules.router)
    app.include_router(canonical_rules.router)
    # Phase 2K — Cell explain / correct / learn.
    app.include_router(cells.router)
    app.include_router(cells.learned_router)
    app.include_router(billing_v2.router)

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
