"""Provider-agnostic AI invoice extraction client.

Phase AI-1 intentionally keeps the provider surface narrow and disabled by
default. The rest of the backend calls this module through
``extract_invoice_structured`` and receives a parsed JSON object; API keys
never leave the process.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .. import settings
from . import canonical_rules


_LOG = logging.getLogger(__name__)


class AIProviderError(RuntimeError):
    """Base exception for configured provider failures."""


class AIProviderNotConfigured(AIProviderError):
    """Raised when AI invoice processing is disabled or incomplete."""


class AIProviderInvalidJSON(AIProviderError):
    """Raised when provider text cannot be parsed as strict JSON."""


class AIProviderInvalidSchema(AIProviderError):
    """Raised when parsed provider JSON does not match the invoice schema."""


class AIProviderUnavailable(AIProviderError):
    """Raised when the configured provider cannot be reached."""


@dataclass(frozen=True)
class AIProviderStatus:
    enabled: bool
    provider: str | None
    model: str | None
    configured: bool
    supports_vision: bool
    vision_enabled: bool
    vision_provider: str | None
    vision_model: str | None
    vision_mode: str
    message: str


def provider_status() -> AIProviderStatus:
    provider = (settings.AI_PROVIDER or "").strip().lower()
    model = (settings.AI_MODEL or "").strip()
    key = (settings.AI_API_KEY or "").strip()
    base_url = (settings.AI_BASE_URL or "").strip()
    vision_requested = bool(getattr(settings, "AI_VISION_ENABLED", False))
    configured_vision_model = (getattr(settings, "AI_VISION_MODEL", "") or "").strip()
    vision_model = configured_vision_model
    vision_provider = (getattr(settings, "AI_VISION_PROVIDER", "") or provider).strip().lower()
    vision_key = (getattr(settings, "AI_VISION_API_KEY", "") or key).strip()
    vision_base_url = (getattr(settings, "AI_VISION_BASE_URL", "") or base_url).strip()
    vision_mode = (getattr(settings, "AI_VISION_MODE", "fallback_only") or "fallback_only").strip()
    enabled = bool(settings.AI_ASSIST_ENABLED)

    if not enabled:
        return AIProviderStatus(
            enabled=False,
            provider=None,
            model=None,
            configured=False,
            supports_vision=False,
            vision_enabled=False,
            vision_provider=None,
            vision_model=None,
            vision_mode=vision_mode,
            message="AI is not configured.",
        )
    if provider == "mock":
        vision_enabled = bool(getattr(settings, "AI_VISION_ENABLED", False))
        return AIProviderStatus(
            enabled=True,
            provider="mock",
            model=model or "mock-invoice-v1",
            configured=True,
            supports_vision=vision_enabled,
            vision_enabled=vision_enabled,
            vision_provider="mock" if vision_enabled else None,
            vision_model=vision_model or ("mock-vision-v1" if vision_enabled else None),
            vision_mode=vision_mode,
            message=(
                "AI invoice processing is configured with the mock provider."
                if not vision_enabled
                else "AI invoice processing and mock vision assist are configured."
            ),
        )
    missing: list[str] = []
    if not provider:
        missing.append("AI_PROVIDER")
    if not model:
        missing.append("AI_MODEL")
    if not key:
        missing.append("AI_API_KEY")
    if provider == "openai_compatible" and not base_url:
        missing.append("AI_BASE_URL")
    elif provider not in {"openai", "openai_compatible"} and not base_url:
        missing.append("AI_BASE_URL")
    if missing:
        return AIProviderStatus(
            enabled=True,
            provider=provider or None,
            model=model or None,
            configured=False,
            supports_vision=False,
            vision_enabled=False,
            vision_provider=vision_provider or None,
            vision_model=vision_model or None,
            vision_mode=vision_mode,
            message="AI is enabled but missing: " + ", ".join(missing),
        )
    if provider not in {"openai", "openai_compatible"}:
        return AIProviderStatus(
            enabled=True,
            provider=provider or None,
            model=model or None,
            configured=False,
            supports_vision=False,
            vision_enabled=False,
            vision_provider=vision_provider or None,
            vision_model=vision_model or None,
            vision_mode=vision_mode,
            message=(
                "Unsupported AI_PROVIDER. Use mock or openai_compatible "
                "for invoice extraction."
            ),
        )
    vision_missing: list[str] = []
    if vision_requested:
        if not vision_model:
            vision_missing.append("AI_VISION_MODEL")
        if not vision_key:
            vision_missing.append("AI_VISION_API_KEY or AI_API_KEY")
        if vision_provider == "openai_compatible" and not vision_base_url:
            vision_missing.append("AI_VISION_BASE_URL or AI_BASE_URL")
        elif vision_provider not in {"openai", "openai_compatible", "mock"} and not vision_base_url:
            vision_missing.append("AI_VISION_BASE_URL or AI_BASE_URL")
    supports_vision = bool(vision_model and vision_key and (vision_base_url or vision_provider == "openai"))
    vision_enabled = bool(vision_requested and supports_vision)
    return AIProviderStatus(
        enabled=True,
        provider=provider,
        model=model,
        configured=True,
        supports_vision=supports_vision,
        vision_enabled=vision_enabled,
        vision_provider=vision_provider or None,
        vision_model=vision_model or None,
        vision_mode=vision_mode,
        message=(
            "AI invoice processing is configured."
            if not vision_requested
            else (
                "AI invoice processing and vision assist are configured."
                if vision_enabled
                else "AI vision is enabled but missing: " + ", ".join(vision_missing)
            )
        ),
    )


def status_payload() -> dict[str, Any]:
    status = provider_status()
    return {
        "enabled": status.enabled,
        "provider": status.provider,
        "model": status.model,
        "configured": status.configured,
        "supports_vision": status.supports_vision,
        "vision_enabled": status.vision_enabled,
        "vision_provider": status.vision_provider,
        "vision_model": status.vision_model,
        "vision_mode": status.vision_mode,
        "message": status.message,
    }


def _require_configured() -> AIProviderStatus:
    status = provider_status()
    if not status.enabled or not status.configured:
        raise AIProviderNotConfigured(status.message)
    return status


def _require_vision_configured() -> AIProviderStatus:
    status = _require_configured()
    if not getattr(settings, "AI_VISION_ENABLED", False):
        raise AIProviderNotConfigured(
            "Vision assist is not enabled. Configure AI_VISION_ENABLED and a vision-capable model."
        )
    if not status.supports_vision or not status.vision_enabled:
        raise AIProviderNotConfigured(
            "Vision assist is not available for this provider. Configure AI_VISION_MODEL with a vision-capable model."
        )
    return status


def _chat_completions_url(provider: str, base_url: str) -> str:
    base = (base_url or "").rstrip("/")
    if base.startswith("//"):
        base = "https:" + base
    elif base and "://" not in base:
        base = "https://" + base
    if provider == "openai" and not base:
        base = "https://api.openai.com/v1"
    if provider == "openai_compatible" and not base:
        raise AIProviderNotConfigured("AI_BASE_URL is required for openai_compatible.")
    if not base:
        raise AIProviderNotConfigured("AI_BASE_URL is required for this provider.")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def _send_chat_completion(
    *,
    provider: str,
    payload: dict[str, Any],
    vision: bool = False,
    api_key_override: str | None = None,
    base_url_override: str | None = None,
    timeout_seconds_override: int | None = None,
    max_attempts_override: int | None = None,
) -> str:
    raw = json.dumps(payload).encode("utf-8")
    request_provider = (
        provider
        if api_key_override is not None or base_url_override is not None
        else (
            (getattr(settings, "AI_VISION_PROVIDER", "") or provider).strip().lower()
            if vision
            else provider
        )
    )
    request_base_url = base_url_override or (
        (getattr(settings, "AI_VISION_BASE_URL", "") or settings.AI_BASE_URL).strip()
        if vision
        else settings.AI_BASE_URL
    )
    request_key = api_key_override or (
        (getattr(settings, "AI_VISION_API_KEY", "") or settings.AI_API_KEY).strip()
        if vision
        else settings.AI_API_KEY
    )
    if not request_key:
        label = "AI vision provider" if vision else "AI provider"
        raise AIProviderNotConfigured(f"{label} API key is not configured.")
    url = _chat_completions_url(request_provider, request_base_url)
    label = "AI vision provider" if vision else "AI provider"
    retryable_statuses = {429, 500, 502, 503, 504}
    max_attempts = max_attempts_override or (3 if vision else 2)
    last_http_error: tuple[int, str] | None = None
    for attempt in range(max_attempts):
        req = urllib.request.Request(
            url,
            data=raw,
            headers={
                "Authorization": f"Bearer {request_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                req,
                timeout=timeout_seconds_override or settings.AI_TIMEOUT_SECONDS,
            ) as resp:
                body = resp.read(settings.AI_MAX_OUTPUT_CHARS * 2).decode("utf-8", "replace")
            break
        except urllib.error.HTTPError as exc:
            safe_body = exc.read(1000).decode("utf-8", "replace")
            last_http_error = (exc.code, safe_body)
            # Provider error bodies can echo masked or partial credentials.
            # Preserve the body only in-process for capability/error handling;
            # never emit it to normal logs.
            _LOG.warning("%s HTTP error %s", label, exc.code)
            if vision and ("image_url" in safe_body or "expected `text`" in safe_body or "expected text" in safe_body):
                raise AIProviderNotConfigured(
                    "The configured AI vision provider/model does not accept image input. "
                    "Set AI_VISION_MODEL plus AI_VISION_BASE_URL/AI_VISION_API_KEY for a vision-capable OpenAI-compatible provider."
                ) from exc
            if exc.code not in retryable_statuses or attempt >= max_attempts - 1:
                raise AIProviderUnavailable(
                    f"{label} returned HTTP {exc.code}."
                ) from exc
            time.sleep(1.5 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt >= max_attempts - 1:
                raise AIProviderUnavailable(f"{label} request failed or timed out.") from exc
            time.sleep(1.5 * (attempt + 1))
    else:
        code, _ = last_http_error or (0, "")
        raise AIProviderUnavailable(f"{label} returned HTTP {code}.")

    try:
        envelope = json.loads(body)
        content = envelope["choices"][0]["message"]["content"]
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    parts.append(str(item.get("text") or item.get("content") or ""))
                else:
                    parts.append(str(item))
            content = "\n".join(parts)
    except Exception as exc:
        label = "AI vision provider" if vision else "AI provider"
        raise AIProviderInvalidJSON(
            f"{label} returned an unexpected response shape."
        ) from exc
    if not isinstance(content, str) or not content.strip():
        label = "AI vision provider" if vision else "AI provider"
        raise AIProviderInvalidJSON(f"{label} response content was empty.")
    if len(content) > settings.AI_MAX_OUTPUT_CHARS:
        label = "AI vision provider" if vision else "AI provider"
        raise AIProviderInvalidJSON(f"{label} response exceeded the configured output limit.")
    return content


def _extract_json_object(text: str) -> dict[str, Any]:
    trimmed = text.strip()
    if trimmed.startswith("```"):
        lines = trimmed.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        trimmed = "\n".join(lines).strip()
    try:
        parsed = json.loads(trimmed)
    except json.JSONDecodeError:
        start = trimmed.find("{")
        end = trimmed.rfind("}")
        if start < 0 or end <= start:
            raise AIProviderInvalidJSON("AI response was not valid JSON.")
        try:
            parsed = json.loads(trimmed[start:end + 1])
        except json.JSONDecodeError as exc:
            decoder = json.JSONDecoder()
            best: dict[str, Any] | None = None
            best_score = -1
            for idx, char in enumerate(trimmed):
                if char != "{":
                    continue
                try:
                    candidate, _ = decoder.raw_decode(trimmed[idx:])
                except json.JSONDecodeError:
                    continue
                if isinstance(candidate, dict):
                    score = sum(1 for key in _REQUIRED_SCHEMA_KEYS if key in candidate)
                    if score > best_score:
                        best = candidate
                        best_score = score
            if best is None:
                raise AIProviderInvalidJSON("AI response was not valid JSON.") from exc
            parsed = best
    if not isinstance(parsed, dict):
        raise AIProviderInvalidJSON("AI response JSON must be an object.")
    return parsed


_REQUIRED_SCHEMA_KEYS = {
    "vendor_name",
    "invoice_number",
    "invoice_date",
    "due_date",
    "bill_or_credit",
    "account_number",
    "service_address",
    "service_period_start",
    "service_period_end",
    "service_period",
    "property_candidate",
    "property_abbreviation",
    "invoice_description",
    "line_items",
    "subtotal",
    "tax_amount",
    "shipping_amount",
    "fees_amount",
    "total_amount",
}


_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "vendor_name": ("vendor", "vendorName", "supplier", "supplier_name", "supplierName"),
    "invoice_number": ("invoiceNo", "invoice_no", "invoiceNumber", "number", "invoice_id"),
    "invoice_date": ("invoiceDate", "date", "bill_date", "billDate"),
    "due_date": ("dueDate", "payment_due_date", "paymentDueDate"),
    "bill_or_credit": ("billOrCredit", "type", "document_type"),
    "account_number": ("accountNumber", "customer_number", "customerNumber", "customer_id"),
    "service_address": ("serviceAddress", "ship_to", "shipTo", "shipping_address", "billing_address"),
    "service_period_start": ("servicePeriodStart", "service_start_date", "serviceStartDate", "billing_period_start", "billingPeriodStart", "period_start"),
    "service_period_end": ("servicePeriodEnd", "service_end_date", "serviceEndDate", "billing_period_end", "billingPeriodEnd", "period_end"),
    "service_period": ("servicePeriod", "billing_period", "billingPeriod", "period", "service_dates", "date_range"),
    "property_candidate": ("property", "propertyCandidate", "property_name", "propertyName"),
    "property_abbreviation": ("propertyAbbreviation", "property_abbrev", "property_code"),
    "invoice_description": ("description", "summary", "invoiceDescription"),
    "line_items": ("items", "invoice_items", "invoiceItems", "lineItems", "products"),
    "subtotal": ("sub_total", "merchandise_subtotal", "merchandiseSubtotal"),
    "tax_amount": ("tax", "sales_tax", "salesTax", "taxAmount"),
    "shipping_amount": ("shipping", "shippingAmount", "freight"),
    "fees_amount": ("fees", "feesAmount", "other_fees"),
    "total_amount": ("total", "amount_due", "amountDue", "invoice_total", "invoiceTotal"),
}


_LINE_ITEM_ALIASES: dict[str, tuple[str, ...]] = {
    "description": ("item_description", "itemDescription", "name", "product", "details"),
    "quantity": ("qty", "ordered", "shipped"),
    "unit_price": ("unitPrice", "price", "unit_cost", "unitCost"),
    "amount": ("total", "line_total", "lineTotal", "extension", "extended_amount"),
    "gl_account_candidate": ("gl_account", "glAccount", "gl_code", "glCode", "category"),
    "expense_type": ("expenseType", "expense_category", "category_name"),
    "is_replacement_reserve": ("replacementReserve", "isReplacementReserve"),
}


def _first_present(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in payload and payload.get(key) not in (None, ""):
            return payload.get(key)
    return None


def _coerce_invoice_schema(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize recoverable provider variations before validation.

    Real OpenAI-compatible providers occasionally return good invoice data
    with slightly different field names. Missing canonical fields are still
    surfaced later by backend validation, but they should not discard the
    whole invoice before rows can be reviewed.
    """
    coerced = dict(payload)
    for canonical, aliases in _FIELD_ALIASES.items():
        if canonical not in coerced or coerced.get(canonical) in (None, ""):
            value = _first_present(coerced, aliases)
            if value is not None:
                coerced[canonical] = value

    defaults: dict[str, Any] = {
        "line_items": [],
        "subtotal": 0.0,
        "tax_amount": 0.0,
        "shipping_amount": 0.0,
        "fees_amount": 0.0,
        "total_amount": 0.0,
        "confidence": None,
        "warnings": [],
        "needs_manual_review": False,
    }
    for key in _REQUIRED_SCHEMA_KEYS:
        coerced.setdefault(key, defaults.get(key, ""))
    coerced.setdefault("confidence", None)
    coerced.setdefault("warnings", [])
    coerced.setdefault("needs_manual_review", False)

    if isinstance(coerced.get("warnings"), str):
        coerced["warnings"] = [coerced["warnings"]]
    elif coerced.get("warnings") is None:
        coerced["warnings"] = []

    raw_items = coerced.get("line_items")
    if isinstance(raw_items, dict):
        raw_items = [raw_items]
    if not isinstance(raw_items, list):
        raw_items = []
    normalized_items: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        normalized = dict(item)
        for canonical, aliases in _LINE_ITEM_ALIASES.items():
            if canonical not in normalized or normalized.get(canonical) in (None, ""):
                value = _first_present(normalized, aliases)
                if value is not None:
                    normalized[canonical] = value
        normalized.setdefault("description", "")
        normalized.setdefault("quantity", None)
        normalized.setdefault("unit_price", None)
        normalized.setdefault("amount", 0.0)
        normalized.setdefault("gl_account_candidate", "")
        normalized.setdefault("expense_type", "General")
        normalized.setdefault("is_replacement_reserve", False)
        normalized.setdefault("confidence", None)
        normalized.setdefault("reason", "")
        normalized_items.append(normalized)
    coerced["line_items"] = normalized_items
    return coerced


def _validate_invoice_schema(payload: dict[str, Any]) -> dict[str, Any]:
    payload = _coerce_invoice_schema(payload)
    missing = sorted(k for k in _REQUIRED_SCHEMA_KEYS if k not in payload)
    if missing:
        raise AIProviderInvalidSchema(
            "AI response is missing required field(s): " + ", ".join(missing[:5])
        )
    line_items = payload.get("line_items")
    if not isinstance(line_items, list):
        raise AIProviderInvalidSchema("AI response line_items must be a list.")
    for idx, item in enumerate(line_items, start=1):
        if not isinstance(item, dict):
            raise AIProviderInvalidSchema(f"AI response line item {idx} must be an object.")
    warnings = payload.get("warnings")
    if warnings is None:
        payload["warnings"] = []
    elif not isinstance(warnings, list):
        raise AIProviderInvalidSchema("AI response warnings must be a list.")
    payload.setdefault("confidence", None)
    payload.setdefault("needs_manual_review", False)
    for item in line_items:
        item.setdefault("confidence", None)
        item.setdefault("reason", "")
    return payload


def _parse_invoice_content(content: str) -> dict[str, Any]:
    return _validate_invoice_schema(_extract_json_object(content))


def _repair_prompt(original_prompt: str, error: str) -> str:
    return "\n".join(
        [
            "Your previous response could not be accepted by the invoice parser.",
            f"Parser error: {error}",
            "Return one valid JSON object only. No markdown, no comments, no prose.",
            "Use the exact schema from the original request.",
            "Include every top-level schema key even if the value is empty, null, 0, or an empty list.",
            "line_items must be an array of objects.",
            "",
            "Original request:",
            original_prompt,
        ]
    )


def _safe_document_text(document_text: str) -> tuple[str, bool]:
    limit = max(1000, int(settings.AI_MAX_TEXT_CHARS or 45000))
    if len(document_text or "") <= limit:
        return document_text or "", False
    return (document_text or "")[:limit], True


def extract_invoice_structured(
    *,
    vendor_hint: str,
    document_text: str,
    page_images_or_refs: list[str] | None,
    template_schema: dict[str, Any],
    property_reference: list[dict[str, Any]] | None,
    gl_reference: list[dict[str, Any]] | None,
    vendor_reference: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Call an OpenAI-compatible provider and return strict JSON.

    Vision payloads are intentionally not sent in Phase AI-1. The parameter is
    accepted so later providers can add image support without changing callers.
    """
    status = _require_configured()
    provider = status.provider or ""
    model = status.model or ""
    if provider == "mock":
        return _mock_extract_invoice_structured(document_text=document_text)
    safe_text, input_truncated = _safe_document_text(document_text)
    prompt = _build_prompt(
        vendor_hint=vendor_hint,
        document_text=safe_text,
        template_schema=template_schema,
        property_reference=property_reference or [],
        gl_reference=gl_reference or [],
        vendor_reference=vendor_reference or [],
        has_page_refs=bool(page_images_or_refs),
    )
    payload = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "You extract invoice data into strict JSON only. "
                    "Never include prose, markdown, code fences, or comments."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "max_tokens": min(
            max(512, int(getattr(settings, "AI_MAX_RESPONSE_TOKENS", 4096) or 4096)),
            8192,
        ),
    }
    last_parse_error: AIProviderError | None = None
    parsed: dict[str, Any] | None = None
    for attempt in range(2):
        content = _send_chat_completion(provider=provider, payload=payload)
        try:
            parsed = _parse_invoice_content(content)
            break
        except (AIProviderInvalidJSON, AIProviderInvalidSchema) as exc:
            last_parse_error = exc
            if attempt:
                raise
            _LOG.info("Retrying AI invoice extraction after invalid JSON/schema response.")
            payload["messages"] = [
                {
                    "role": "system",
                    "content": (
                        "You repair invoice extraction output into strict JSON only. "
                        "Never include prose, markdown, code fences, or comments."
                    ),
                },
                {"role": "user", "content": _repair_prompt(prompt, str(exc))},
            ]
    if parsed is None:
        raise last_parse_error or AIProviderInvalidJSON("AI response was not valid JSON.")
    if input_truncated:
        warnings = parsed.get("warnings") if isinstance(parsed.get("warnings"), list) else []
        parsed["warnings"] = [*warnings, "ai_input_truncated"]
    return parsed


def extract_invoice_vision_structured(
    *,
    vendor_hint: str,
    document_text: str,
    page_images_or_refs: list[str],
    template_schema: dict[str, Any],
    property_reference: list[dict[str, Any]] | None,
    gl_reference: list[dict[str, Any]] | None,
    vendor_reference: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Run a vision-capable extraction call and return strict JSON.

    The caller is responsible for rendering/capping images. This function only
    accepts image refs after the status layer has confirmed that vision is
    explicitly enabled and supported.
    """
    status = _require_vision_configured()
    provider = status.provider or ""
    model = status.vision_model or status.model or ""
    if provider == "mock":
        return _mock_extract_invoice_vision_structured(document_text=document_text)
    if not page_images_or_refs:
        raise AIProviderNotConfigured("No page images were supplied for vision assist.")
    safe_text, input_truncated = _safe_document_text(document_text)
    prompt = _build_vision_prompt(
        vendor_hint=vendor_hint,
        document_text=safe_text,
        template_schema=template_schema,
        property_reference=property_reference or [],
        gl_reference=gl_reference or [],
        vendor_reference=vendor_reference or [],
    )
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for ref in page_images_or_refs[: max(1, int(getattr(settings, "AI_VISION_MAX_PAGES", 2) or 2))]:
        content.append({"type": "image_url", "image_url": {"url": ref}})
    payload = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "You visually inspect invoices and return strict JSON only. "
                    "Never include prose, markdown, code fences, or comments."
                ),
            },
            {"role": "user", "content": content},
        ],
        "max_tokens": min(
            max(512, int(getattr(settings, "AI_MAX_RESPONSE_TOKENS", 4096) or 4096)),
            8192,
        ),
    }
    last_parse_error: AIProviderError | None = None
    parsed: dict[str, Any] | None = None
    original_content = list(content)
    for attempt in range(2):
        content_text = _send_chat_completion(provider=provider, payload=payload, vision=True)
        try:
            parsed = _parse_invoice_content(content_text)
            break
        except (AIProviderInvalidJSON, AIProviderInvalidSchema) as exc:
            last_parse_error = exc
            if attempt:
                raise
            _LOG.info("Retrying AI vision extraction after invalid JSON/schema response.")
            repaired_content = list(original_content)
            repaired_content[0] = {"type": "text", "text": _repair_prompt(prompt, str(exc))}
            payload["messages"] = [
                {
                    "role": "system",
                    "content": (
                        "You repair visual invoice extraction output into strict JSON only. "
                        "Never include prose, markdown, code fences, or comments."
                    ),
                },
                {"role": "user", "content": repaired_content},
            ]
    if parsed is None:
        raise last_parse_error or AIProviderInvalidJSON("AI vision response was not valid JSON.")
    parsed["vision_candidates"] = _normalize_vision_candidates(parsed.get("vision_candidates"))
    if input_truncated:
        warnings = parsed.get("warnings") if isinstance(parsed.get("warnings"), list) else []
        parsed["warnings"] = [*warnings, "ai_input_truncated"]
    return parsed


def _mock_extract_invoice_structured(*, document_text: str) -> dict[str, Any]:
    """Deterministic fixture provider used by Phase AI-1.1 tests.

    No network calls, no API keys, and mode can be forced with
    ``AI_MOCK_MODE`` or fixture text markers.
    """
    delay = max(0, int(getattr(settings, "AI_MOCK_DELAY_SECONDS", 0) or 0))
    if delay:
        time.sleep(delay)
    mode = (getattr(settings, "AI_MOCK_MODE", "") or "").strip().lower()
    text_upper = (document_text or "").upper()
    if "MOCK_MALFORMED_JSON" in text_upper:
        mode = "malformed_json"
    elif "MOCK_TOTAL_MISMATCH" in text_upper:
        mode = "total_mismatch"
    elif "MOCK_LOW_CONFIDENCE" in text_upper:
        mode = "low_confidence"

    if mode == "malformed_json":
        return _extract_json_object("this is not valid json")

    low_confidence = mode == "low_confidence"
    total_amount = 206.65
    if mode == "total_mismatch":
        total_amount = 211.65

    return _validate_invoice_schema({
        "vendor_name": "HD Supply Facilities Maintenance, Ltd",
        "invoice_number": "HDS-104857",
        "invoice_date": "05/06/2026",
        "due_date": "06/05/2026",
        "bill_or_credit": "Bill",
        "account_number": "40293817",
        "service_address": "1726 Stone Street, Union City, TN 38261",
        "property_candidate": "1732-Hillwood Manor",
        "property_abbreviation": "1732-HMA",
        "invoice_description": "Maintenance supplies for 1732-Hillwood Manor",
        "line_items": [
            {
                "description": "Angle stop valve, chrome, 1/2 inch",
                "quantity": 6,
                "unit_price": 8.12,
                "amount": 48.72,
                "gl_account_candidate": "6615 Building Maintenance & Repairs - Minor",
                "expense_type": "Repairs and maintenance",
                "is_replacement_reserve": False,
                "confidence": 0.93 if not low_confidence else 0.58,
                "reason": "Matched plumbing repair supply line on invoice detail.",
            },
            {
                "description": "LED exterior fixture, bronze",
                "quantity": 1,
                "unit_price": 139.99,
                "amount": 139.99,
                "gl_account_candidate": "6627 Electrical Parts & Supplies",
                "expense_type": "Electrical supplies",
                "is_replacement_reserve": False,
                "confidence": 0.88 if not low_confidence else 0.52,
                "reason": "Item description references electrical fixture replacement.",
            },
        ],
        "subtotal": 188.71,
        "tax_amount": 17.94,
        "shipping_amount": 0.00,
        "fees_amount": 0.00,
        "total_amount": total_amount,
        "confidence": 0.89 if not low_confidence else 0.55,
        "warnings": (
            ["Mock low confidence: supplier invoice line descriptions are abbreviated."]
            if low_confidence
            else []
        ),
        "needs_manual_review": low_confidence,
    })


def _mock_extract_invoice_vision_structured(*, document_text: str) -> dict[str, Any]:
    payload = _mock_extract_invoice_structured(document_text=document_text)
    payload["confidence"] = max(float(payload.get("confidence") or 0), 0.92)
    payload["warnings"] = list(payload.get("warnings") or [])
    payload["vision_candidates"] = [
        {
            "field_key": "vendor_name",
            "field_label": "Vendor",
            "value": payload.get("vendor_name"),
            "page": 1,
            "bbox": {"x": 0.09, "y": 0.08, "w": 0.28, "h": 0.07},
            "confidence": 0.93,
            "validation_status": "candidate",
        },
        {
            "field_key": "invoice_number",
            "field_label": "Invoice number",
            "value": payload.get("invoice_number"),
            "page": 1,
            "bbox": {"x": 0.63, "y": 0.11, "w": 0.22, "h": 0.05},
            "confidence": 0.91,
            "validation_status": "candidate",
        },
        {
            "field_key": "total_amount",
            "field_label": "Invoice total",
            "value": payload.get("total_amount"),
            "page": 1,
            "bbox": {"x": 0.70, "y": 0.78, "w": 0.18, "h": 0.05},
            "confidence": 0.94,
            "validation_status": "candidate",
        },
        {
            "field_key": "line_items_table",
            "field_label": "Line items",
            "value": "Detected line item table",
            "page": 1,
            "bbox": {"x": 0.08, "y": 0.34, "w": 0.82, "h": 0.24},
            "confidence": 0.89,
            "validation_status": "candidate",
        },
    ]
    return _validate_invoice_schema(payload)


def _build_prompt(
    *,
    vendor_hint: str,
    document_text: str,
    template_schema: dict[str, Any],
    property_reference: list[dict[str, Any]],
    gl_reference: list[dict[str, Any]],
    vendor_reference: list[dict[str, Any]],
    has_page_refs: bool,
) -> str:
    schema = {
        "vendor_name": "",
        "invoice_number": "",
        "invoice_date": "",
        "purchase_date": "",
        "ship_date": "",
        "received_date": "",
        "due_date": "",
        "bill_or_credit": "Bill",
        "account_number": "",
        "service_address": "",
        "service_period_start": "",
        "service_period_end": "",
        "service_period": "",
        "property_candidate": "",
        "property_abbreviation": "",
        "invoice_description": "",
        "line_items": [
            {
                "description": "",
                "quantity": None,
                "unit_price": None,
                "amount": 0.00,
                "gl_account_candidate": "",
                "expense_type": "General",
                "is_replacement_reserve": False,
                "confidence": 0.0,
                "reason": "",
            }
        ],
        "subtotal": 0.00,
        "tax_amount": 0.00,
        "shipping_amount": 0.00,
        "fees_amount": 0.00,
        "total_amount": 0.00,
        "confidence": 0.0,
        "warnings": [],
        "needs_manual_review": True,
    }
    return "\n".join(
        [
            "Extract this invoice into the exact JSON schema below.",
            "Use null for unknown numeric values and empty strings for unknown text.",
            "Line item amounts must be signed numbers. Credits should be negative when applicable.",
            "If the source lists products/services but does not show per-line dollar amounts, return one payable line item for the explicit invoice total and include a warning that line amounts were not visible.",
            "If the invoice total is explicit but the line table is incomplete, never return an empty payable invoice; use the invoice total fallback line for operator review.",
            "Every top-level and line-item confidence must be a number from 0.0 to 1.0.",
            "Do not omit confidence. Use 0.90+ only when the source text is explicit and totals reconcile.",
            "Use 0.70-0.89 when fields are mostly clear but mapping is uncertain.",
            "Use below 0.70 when key fields are inferred, missing, or ambiguous.",
            "Every line item must include a short reason explaining the extraction and GL suggestion.",
            "Set needs_manual_review=true only when a specific missing/ambiguous/invalid field requires operator review.",
            "When needs_manual_review=true, include a clear human-readable warning explaining why.",
            "Do not invent missing property, GL, service address, or date values.",
            "For recurring bills/utilities, extract the visible service/billing period as service_period_start and service_period_end. Examples include '03/26/26 to 04/27/26' or 'service from ... to ...'.",
            "Search for invoice number, bill number, statement number, account number, and billing ID. If no invoice number exists on a bill, leave invoice_number empty; the backend will generate a required stable bill number from account/date/source context.",
            "If the invoice has no explicit invoice date, leave invoice_date empty and use purchase_date, ship_date, or received_date only when that source is explicit.",
            "Do not put vendor-side labels such as GL CODE:MISCELLANEOUS into gl_account_candidate unless it is a real numeric ResMan/Chart of Accounts code from the reference.",
            "Use the exact source line-item descriptions where possible; avoid vague summaries like 'hardware and miscellaneous items'.",
            "Include zero-dollar source lines only when they carry accounting meaning; otherwise include a warning that zero-dollar lines were omitted.",
            "Do not decide the ResMan property or location unless it is explicit in the source text or a reference match is highly clear.",
            "Return JSON only.",
            "",
            canonical_rules.prompt_rules_summary(),
            "",
            f"Vendor hint: {vendor_hint or 'unknown'}",
            f"Vision references supplied: {'yes' if has_page_refs else 'no'}",
            "",
            "Required JSON schema:",
            json.dumps(schema, indent=2),
            "",
            "ResMan template columns:",
            json.dumps(template_schema, indent=2)[:5000],
            "",
            "Known vendors (sample/reference):",
            json.dumps(vendor_reference[:80], indent=2)[:6000],
            "",
            "Property reference (sample/reference):",
            json.dumps(property_reference[:120], indent=2)[:6000],
            "",
            "General ledger reference (sample/reference):",
            json.dumps(gl_reference[:120], indent=2)[:6000],
            "",
            "Document text:",
            document_text,
        ]
    )


def _build_vision_prompt(
    *,
    vendor_hint: str,
    document_text: str,
    template_schema: dict[str, Any],
    property_reference: list[dict[str, Any]],
    gl_reference: list[dict[str, Any]],
    vendor_reference: list[dict[str, Any]],
) -> str:
    schema = {
        "vendor_name": "",
        "invoice_number": "",
        "invoice_date": "",
        "purchase_date": "",
        "ship_date": "",
        "received_date": "",
        "due_date": "",
        "bill_or_credit": "Bill",
        "account_number": "",
        "service_address": "",
        "service_period_start": "",
        "service_period_end": "",
        "service_period": "",
        "property_candidate": "",
        "property_abbreviation": "",
        "invoice_description": "",
        "line_items": [
            {
                "description": "",
                "quantity": None,
                "unit_price": None,
                "amount": 0.00,
                "gl_account_candidate": "",
                "expense_type": "General",
                "is_replacement_reserve": False,
                "confidence": 0.0,
                "reason": "",
            }
        ],
        "subtotal": 0.00,
        "tax_amount": 0.00,
        "shipping_amount": 0.00,
        "fees_amount": 0.00,
        "total_amount": 0.00,
        "confidence": 0.0,
        "warnings": [],
        "needs_manual_review": True,
        "vision_candidates": [
            {
                "field_key": "invoice_number",
                "field_label": "Invoice number",
                "value": "",
                "page": 1,
                "bbox": {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0},
                "confidence": 0.0,
                "validation_status": "candidate",
            }
        ],
    }
    return "\n".join(
        [
            "Visually inspect the attached invoice page image(s) and return the exact JSON schema below.",
            "Return JSON only. Do not include prose, markdown, code fences, or comments.",
            "Use null when unknown. Do not invent values that are not visible.",
            "Preserve visible text exactly where possible, especially invoice number, vendor, dates, totals, and line descriptions.",
            "For recurring bills/utilities, extract the visible service/billing period as service_period_start and service_period_end. Examples include '03/26/26 to 04/27/26' or 'service from ... to ...'.",
            "Search for invoice number, bill number, statement number, account number, and billing ID. If no invoice number exists on a bill, leave invoice_number empty; the backend will generate a required stable bill number from account/date/source context.",
            "If line-level dollar amounts are not visible but the invoice total is visible, return one payable line item using the explicit invoice total and warn that line amounts were not visible.",
            "If the invoice total is explicit but the line table is incomplete, never return an empty payable invoice; use the invoice total fallback line for operator review.",
            "Include confidence per field/line item from 0.0 to 1.0.",
            "Include candidate bounding boxes only when visually confident.",
            "Bounding boxes must be normalized page coordinates: x, y, w, h from 0.0 to 1.0.",
            "If OCR text is provided, use it as helper context but trust the image when OCR is weak or missing.",
            "Treat vendor-side category text as source text only; do not invent ResMan GL accounts.",
            "Flag ambiguity in warnings and needs_manual_review.",
            "",
            canonical_rules.prompt_rules_summary(),
            "",
            f"Vendor hint: {vendor_hint or 'unknown'}",
            "",
            "Required JSON schema:",
            json.dumps(schema, indent=2),
            "",
            "ResMan template columns:",
            json.dumps(template_schema, indent=2)[:5000],
            "",
            "Known vendors (sample/reference):",
            json.dumps(vendor_reference[:80], indent=2)[:6000],
            "",
            "Property reference (sample/reference):",
            json.dumps(property_reference[:120], indent=2)[:6000],
            "",
            "General ledger reference (sample/reference):",
            json.dumps(gl_reference[:120], indent=2)[:6000],
            "",
            "OCR/text helper context:",
            document_text or "(none)",
        ]
    )


def _normalize_vision_candidates(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        bbox = item.get("bbox")
        if not isinstance(bbox, dict):
            continue
        try:
            x = float(bbox.get("x"))
            y = float(bbox.get("y"))
            w = float(bbox.get("w"))
            h = float(bbox.get("h"))
        except (TypeError, ValueError):
            continue
        if w <= 0 or h <= 0:
            continue
        out.append({
            "field_key": str(item.get("field_key") or item.get("field") or "vision_candidate"),
            "field_label": str(item.get("field_label") or item.get("field_key") or "AI vision candidate"),
            "value": item.get("value"),
            "page": max(1, int(item.get("page") or 1)),
            "bbox": {
                "x": max(0.0, min(1.0, x)),
                "y": max(0.0, min(1.0, y)),
                "w": max(0.001, min(1.0, w)),
                "h": max(0.001, min(1.0, h)),
            },
            "confidence": max(0.0, min(1.0, float(item.get("confidence") or 0))),
            "validation_status": str(item.get("validation_status") or "candidate"),
        })
    return out


__all__ = [
    "AIProviderError",
    "AIProviderInvalidJSON",
    "AIProviderInvalidSchema",
    "AIProviderNotConfigured",
    "AIProviderStatus",
    "AIProviderUnavailable",
    "extract_invoice_vision_structured",
    "extract_invoice_structured",
    "provider_status",
    "status_payload",
]
