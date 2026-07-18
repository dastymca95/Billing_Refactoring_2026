"""Private deterministic-builder session endpoints."""
from __future__ import annotations
from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from ..services import ai_provider
from ..services import deterministic_builder as builder


router = APIRouter(prefix="/api/deterministic-builder", tags=["deterministic-builder"])


class CreateSessionRequest(BaseModel):
    vendor_key: str
    actor: str = "local_operator"


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    selected_column: str | None = None
    actor: str = "local_operator"


class ApprovalRequest(BaseModel):
    expected_revision: int = Field(ge=0)
    actor: str = "local_operator"


@router.post("/sessions")
def create_session(body: CreateSessionRequest) -> dict:
    try:
        return builder.create_session(body.vendor_key, actor=body.actor).model_dump(mode="json")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/sessions/{session_id}")
def get_session(session_id: str) -> dict:
    try:
        return builder.get_session(session_id).model_dump(mode="json")
    except KeyError:
        raise HTTPException(status_code=404, detail="Deterministic builder session not found.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/sessions/{session_id}/samples")
async def upload_sample(session_id: str, file: UploadFile = File(...)) -> dict:
    try:
        content = await file.read(builder.MAX_SAMPLE_BYTES + 1)
        return builder.add_sample(
            session_id, original_filename=file.filename or "sample", content=content,
        ).model_dump(mode="json")
    except KeyError:
        raise HTTPException(status_code=404, detail="Deterministic builder session not found.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/sessions/{session_id}/chat")
def chat(session_id: str, body: ChatRequest) -> dict:
    try:
        return builder.chat(
            session_id, message=body.message, selected_column=body.selected_column, actor=body.actor,
        ).model_dump(mode="json")
    except KeyError:
        raise HTTPException(status_code=404, detail="Deterministic builder session not found.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ai_provider.AIProviderNotConfigured as exc:
        raise HTTPException(status_code=503, detail=exc.safe_diagnostic())
    except ai_provider.AIProviderError as exc:
        raise HTTPException(status_code=502, detail=exc.safe_diagnostic())


@router.post("/sessions/{session_id}/preview")
def preview(session_id: str) -> dict:
    try:
        return builder.preview(session_id).model_dump(mode="json")
    except KeyError:
        raise HTTPException(status_code=404, detail="Deterministic builder session not found.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/sessions/{session_id}/approve")
def approve(session_id: str, body: ApprovalRequest) -> dict:
    try:
        return builder.approve(
            session_id, expected_revision=body.expected_revision, actor=body.actor,
        ).model_dump(mode="json")
    except KeyError:
        raise HTTPException(status_code=404, detail="Deterministic builder session not found.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
