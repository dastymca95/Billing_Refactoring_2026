"""Safe request tracing and bounded provider concurrency for AI fallback work.

The trace deliberately stores no prompts, response bodies, headers, endpoints,
credentials, filenames, or source text.  It is batch-local runtime evidence.
"""

from __future__ import annotations

import base64
import contextlib
import contextvars
import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .. import settings


TRACE_SCHEMA_VERSION = "ai-request-trace/1.0"


@dataclass(frozen=True)
class RequestContext:
    batch_id: str = ""
    stage: str = "unknown"
    provider: str = ""
    model: str = ""
    profile_id: str = ""
    cache_key: str = ""
    cache_status: str = "not_checked"
    media_bytes: int = 0
    media_pixels: int = 0
    estimated_cost_usd: float = 0.0


_CONTEXT: contextvars.ContextVar[RequestContext] = contextvars.ContextVar(
    "innerview_ai_request_context", default=RequestContext()
)
_LAST_REQUEST_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "innerview_ai_last_request_id", default=""
)
_TRACE_LOCK = threading.Lock()
_SEMAPHORE_LOCK = threading.Lock()
_SEMAPHORES: dict[tuple[str, int], threading.BoundedSemaphore] = {}
_ACTIVE: dict[str, int] = {}
_PEAK: dict[str, int] = {}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_batch_id(value: str) -> str:
    value = str(value or "").strip()
    return value if value.startswith("batch_") and value.replace("_", "").isalnum() else ""


def _trace_path(batch_id: str) -> Path | None:
    safe = _safe_batch_id(batch_id)
    if not safe:
        return None
    return settings.BATCHES_ROOT / safe / "audit" / "ai_request_trace.jsonl"


def _write_event(event: dict[str, Any]) -> None:
    path = _trace_path(str(event.get("batch_id") or ""))
    if path is None:
        return
    payload = {"schema": TRACE_SCHEMA_VERSION, **event}
    line = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    with _TRACE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def current_context() -> RequestContext:
    return _CONTEXT.get()


@contextlib.contextmanager
def operation(
    *,
    batch_id: str,
    stage: str,
    provider: str = "",
    model: str = "",
    profile_id: str = "",
    media_bytes: int = 0,
    media_pixels: int = 0,
) -> Iterator[None]:
    token = _CONTEXT.set(RequestContext(
        batch_id=_safe_batch_id(batch_id),
        stage=str(stage or "unknown")[:120],
        provider=str(provider or "")[:80],
        model=str(model or "")[:160],
        profile_id=str(profile_id or "")[:160],
        media_bytes=max(0, int(media_bytes or 0)),
        media_pixels=max(0, int(media_pixels or 0)),
    ))
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def update_context(**changes: Any) -> None:
    allowed = {key: value for key, value in changes.items() if hasattr(current_context(), key)}
    if allowed:
        _CONTEXT.set(replace(current_context(), **allowed))


def record_cache(cache_key: str, *, hit: bool, layer: str) -> None:
    context = current_context()
    update_context(cache_key=cache_key, cache_status="hit" if hit else "miss")
    _write_event({
        "event": "cache",
        "batch_id": context.batch_id,
        "request_id": "",
        "stage": context.stage,
        "provider": context.provider,
        "model": context.model,
        "profile_id": context.profile_id,
        "cache_layer": str(layer or "")[:80],
        "cache_key": str(cache_key or "")[:128],
        "cache_status": "hit" if hit else "miss",
        "at": _utc_now(),
    })


def _provider_limit(provider: str) -> int:
    normalized = "".join(ch if ch.isalnum() else "_" for ch in provider.upper())
    raw = os.environ.get(f"AI_{normalized}_MAX_CONCURRENCY") or os.environ.get(
        "AI_PROVIDER_MAX_CONCURRENCY", "4"
    )
    try:
        return min(32, max(1, int(raw)))
    except (TypeError, ValueError):
        return 4


def _semaphore(provider: str) -> tuple[threading.BoundedSemaphore, int]:
    key_provider = str(provider or "unknown").strip().lower() or "unknown"
    limit = _provider_limit(key_provider)
    key = (key_provider, limit)
    with _SEMAPHORE_LOCK:
        return _SEMAPHORES.setdefault(key, threading.BoundedSemaphore(limit)), limit


@contextlib.contextmanager
def provider_attempt(provider: str, attempt: int) -> Iterator[str]:
    """Acquire a provider slot and persist one safe transport-attempt event."""

    context = current_context()
    semaphore, limit = _semaphore(provider)
    wait_started = time.perf_counter()
    semaphore.acquire()
    wait_ms = round((time.perf_counter() - wait_started) * 1000, 3)
    normalized = str(provider or "unknown").strip().lower() or "unknown"
    with _SEMAPHORE_LOCK:
        active = _ACTIVE.get(normalized, 0) + 1
        _ACTIVE[normalized] = active
        _PEAK[normalized] = max(_PEAK.get(normalized, 0), active)
        peak = _PEAK[normalized]
    request_id = uuid.uuid4().hex
    _LAST_REQUEST_ID.set(request_id)
    started_at = _utc_now()
    started = time.perf_counter()
    outcome = "response_received"
    failure_code = ""
    try:
        yield request_id
    except Exception as exc:
        outcome = "transport_error"
        failure_code = type(exc).__name__
        raise
    finally:
        ended_at = _utc_now()
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        with _SEMAPHORE_LOCK:
            _ACTIVE[normalized] = max(0, _ACTIVE.get(normalized, 1) - 1)
        semaphore.release()
        latest = current_context()
        _write_event({
            "event": "provider_attempt",
            "batch_id": latest.batch_id or context.batch_id,
            "request_id": request_id,
            "stage": latest.stage or context.stage,
            "provider": normalized,
            "model": latest.model or context.model,
            "profile_id": latest.profile_id or context.profile_id,
            "cache_key": latest.cache_key or context.cache_key,
            "cache_status": latest.cache_status or context.cache_status,
            "attempt": max(1, int(attempt)),
            "started_at": started_at,
            "ended_at": ended_at,
            "elapsed_ms": elapsed_ms,
            "provider_semaphore_wait_ms": wait_ms,
            "provider_concurrency_limit": limit,
            "provider_active_at_start": active,
            "provider_peak_concurrency": peak,
            "media_bytes": latest.media_bytes or context.media_bytes,
            "media_pixels": latest.media_pixels or context.media_pixels,
            "estimated_cost_usd": round(
                float(latest.estimated_cost_usd or context.estimated_cost_usd or 0.0), 6
            ),
            "outcome": outcome,
            "failure_code": failure_code,
        })


def record_schema_result(result: str, *, retry_reason: str = "") -> None:
    context = current_context()
    _write_event({
        "event": "schema_validation",
        "batch_id": context.batch_id,
        "request_id": _LAST_REQUEST_ID.get(),
        "stage": context.stage,
        "provider": context.provider,
        "model": context.model,
        "profile_id": context.profile_id,
        "cache_key": context.cache_key,
        "cache_status": context.cache_status,
        "schema_result": str(result or "unknown")[:120],
        "retry_reason": str(retry_reason or "")[:160],
        "at": _utc_now(),
    })


def record_circuit_breaker(
    *,
    provider: str,
    model: str,
    endpoint_surface: str,
    capability: str,
    action: str,
    http_status: int | None = None,
    failure_code: str = "",
) -> None:
    """Persist a secret-safe permanent-provider-failure transition."""

    context = current_context()
    _write_event({
        "event": "provider_circuit_breaker",
        "batch_id": context.batch_id,
        "request_id": "",
        "stage": context.stage,
        "provider": str(provider or "")[:80],
        "model": str(model or "")[:160],
        "profile_id": context.profile_id,
        "endpoint_surface": str(endpoint_surface or "")[:80],
        "capability": str(capability or "")[:80],
        "action": str(action or "")[:40],
        "http_status": http_status,
        "failure_code": str(failure_code or "")[:120],
        "at": _utc_now(),
    })


def record_stage_timing(stage: str, elapsed_ms: float) -> None:
    context = current_context()
    _write_event({
        "event": "stage_timing",
        "batch_id": context.batch_id,
        "request_id": "",
        "stage": str(stage or context.stage)[:120],
        "provider": context.provider,
        "model": context.model,
        "profile_id": context.profile_id,
        "elapsed_ms": round(max(0.0, float(elapsed_ms or 0.0)), 3),
        "at": _utc_now(),
    })


def media_stats(data_urls: list[str] | tuple[str, ...]) -> tuple[int, int]:
    """Return decoded media bytes and pixels without retaining any media."""

    total_bytes = 0
    total_pixels = 0
    for value in data_urls:
        if not isinstance(value, str) or ";base64," not in value:
            continue
        try:
            raw = base64.b64decode(value.split(",", 1)[1], validate=False)
        except (ValueError, TypeError):
            continue
        total_bytes += len(raw)
        try:
            import io
            from PIL import Image  # type: ignore

            with Image.open(io.BytesIO(raw)) as image:
                total_pixels += int(image.width) * int(image.height)
        except Exception:
            pass
    return total_bytes, total_pixels


def reset_for_tests() -> None:
    with _SEMAPHORE_LOCK:
        _SEMAPHORES.clear()
        _ACTIVE.clear()
        _PEAK.clear()


__all__ = [
    "RequestContext",
    "current_context",
    "media_stats",
    "operation",
    "provider_attempt",
    "record_cache",
    "record_schema_result",
    "record_stage_timing",
    "reset_for_tests",
    "update_context",
]
