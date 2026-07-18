"""Run bounded, harmless capability probes for the configured AI routes.

The report contains route identity and safe failure codes only.  Credentials,
headers, request bodies, response bodies, and client data are never persisted.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp.backend.services import ai_provider
from webapp.backend.services.provider_capabilities import (
    ModelCapability,
    OpenAICompatibleProbeTransport,
    ProfileLoader,
    _probe_passed,
    _probe_request,
)


PROFILE_ENV = {
    "runtime-vision": "AI_VISION_API_KEY",
    "gemini-vision": "GEMINI_API_KEY",
    "deepseek-accounting": "DEEPSEEK_API_KEY",
    "anthropic-verification": "ANTHROPIC_API_KEY",
}


def _harmless_image_data_url() -> tuple[str, str]:
    marker = "IV39C_VISION"
    image = Image.new("RGB", (360, 140), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((4, 4, 355, 135), outline="black", width=3)
    draw.text((72, 58), marker, fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii"), marker


def _harmless_pdf_data_url() -> str:
    content = b"BT /F1 16 Tf 40 90 Td (IV39C_NATIVE_PDF) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 180] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(content)).encode("ascii") + b" >>\nstream\n"
        + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    data = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(data))
        data.extend(f"{index} 0 obj\n".encode("ascii"))
        data.extend(obj)
        data.extend(b"\nendobj\n")
    xref = len(data)
    data.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    data.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    data.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode("ascii")
    )
    return "data:application/pdf;base64," + base64.b64encode(data).decode("ascii")


def _failure_category(status: int | None, code: str) -> str:
    if status in {401, 403}:
        return "authentication"
    if status == 404:
        return "capability_unavailable"
    if "schema" in code or "json" in code:
        return "invalid_structured_output"
    if "timeout" in code or "transport" in code:
        return "transport"
    return "provider" if code else "none"


def _estimate_profile_cost(profile, prompt: str, *, vision: bool) -> float | None:
    if profile.input_cost_usd_per_million is None or profile.output_cost_usd_per_million is None:
        return None
    input_tokens = max(1, len(prompt) // 4)
    output_tokens = 300
    estimate = (
        input_tokens * profile.input_cost_usd_per_million / 1_000_000
        + output_tokens * profile.output_cost_usd_per_million / 1_000_000
    )
    if vision:
        estimate += float(os.environ.get("AI_ESTIMATED_VISION_IMAGE_COST_USD", "0.002") or 0.002)
    return round(estimate, 6)


def _profile_probe(profile, capability: ModelCapability, *, endpoint_family: str) -> dict[str, Any]:
    request = _probe_request(profile, capability)
    attempts = 0
    original = ai_provider.urllib.request.urlopen

    def counted(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        return original(*args, **kwargs)

    ai_provider.urllib.request.urlopen = counted
    started = time.perf_counter()
    status: int | None = None
    schema_valid = False
    failure_code = ""
    try:
        payload = OpenAICompatibleProbeTransport()(profile, capability, request)
        schema_valid = _probe_passed(capability, payload)
        status = 200
        if not schema_valid:
            failure_code = "probe_assertion_failed"
    except ai_provider.AIProviderError as exc:
        diagnostic = exc.safe_diagnostic()
        status = diagnostic.get("http_status")
        failure_code = str(diagnostic.get("failure_code") or type(exc).__name__)
    except Exception as exc:  # safe class name only; never serialize exception bodies
        failure_code = type(exc).__name__
    finally:
        ai_provider.urllib.request.urlopen = original
    latency_ms = round((time.perf_counter() - started) * 1000, 3)
    return {
        "logical_profile": profile.profile_id,
        "provider": profile.provider,
        "model": profile.model_id,
        "endpoint_family": endpoint_family,
        "credential_environment_variable": PROFILE_ENV[profile.profile_id],
        "authentication_success": status == 200,
        "http_status": status,
        "schema_valid": schema_valid,
        "latency_ms": latency_ms,
        "retry_count": max(0, attempts - 1),
        "capability": capability.value,
        "capability_supported": bool(status == 200 and schema_valid),
        "failure_category": _failure_category(status, failure_code),
        "failure_code": failure_code or None,
        "estimated_cost_usd": _estimate_profile_cost(
            profile, request["prompt"], vision=("vision" in capability.value)
        ),
    }


def _native_pdf_probe(profile) -> dict[str, Any]:
    marker = "IV39C_NATIVE_PDF"
    payload = {
        "model": profile.model_id,
        "input": [{
            "role": "user",
            "content": [
                {
                    "type": "input_file",
                    "filename": "harmless-capability-probe.pdf",
                    "file_data": _harmless_pdf_data_url(),
                },
                {
                    "type": "input_text",
                    "text": f'Return exactly JSON with probe="{marker}" and ok=true.',
                },
            ],
        }],
        "text": {"format": {
            "type": "json_schema",
            "name": "capability_probe",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "probe": {"type": "string"},
                    "ok": {"type": "boolean"},
                },
                "required": ["probe", "ok"],
                "additionalProperties": False,
            },
        }},
        "reasoning": {"effort": "low"},
        "max_output_tokens": 512,
    }
    attempts = 0
    original = ai_provider.urllib.request.urlopen

    def counted(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        return original(*args, **kwargs)

    ai_provider.urllib.request.urlopen = counted
    started = time.perf_counter()
    status: int | None = None
    schema_valid = False
    failure_code = ""
    try:
        content, _usage = ai_provider._send_openai_response(
            payload=payload,
            api_key=profile.api_key.get_secret_value() if profile.api_key else "",
            base_url=profile.base_url or "",
            timeout_seconds=min(90, profile.timeout_seconds),
            max_attempts=2,
        )
        parsed = ai_provider._extract_json_object(content)
        schema_valid = parsed.get("probe") == marker and parsed.get("ok") is True
        status = 200
        if not schema_valid:
            failure_code = "probe_assertion_failed"
    except ai_provider.AIProviderError as exc:
        diagnostic = exc.safe_diagnostic()
        status = diagnostic.get("http_status")
        failure_code = str(diagnostic.get("failure_code") or type(exc).__name__)
    except Exception as exc:
        failure_code = type(exc).__name__
    finally:
        ai_provider.urllib.request.urlopen = original
    return {
        "logical_profile": "runtime-vision-native-pdf",
        "provider": profile.provider,
        "model": profile.model_id,
        "endpoint_family": "responses_native_pdf",
        "credential_environment_variable": "AI_VISION_API_KEY",
        "authentication_success": status == 200,
        "http_status": status,
        "schema_valid": schema_valid,
        "latency_ms": round((time.perf_counter() - started) * 1000, 3),
        "retry_count": max(0, attempts - 1),
        "capability": "native_pdf",
        "capability_supported": bool(status == 200 and schema_valid),
        "failure_category": _failure_category(status, failure_code),
        "failure_code": failure_code or None,
        "estimated_cost_usd": float(
            os.environ.get("AI_ESTIMATED_NATIVE_PDF_COST_USD", "0.05") or 0.05
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-output", type=Path, required=True)
    args = parser.parse_args()

    image, marker = _harmless_image_data_url()
    os.environ["AI_CAPABILITY_VISION_PROBE_IMAGE"] = image
    os.environ["AI_CAPABILITY_VISION_PROBE_EXPECTED"] = marker
    ai_provider._reset_provider_circuits_for_tests()
    profiles = {item.profile_id: item for item in ProfileLoader().load()}
    required = {
        "runtime-vision", "gemini-vision", "deepseek-accounting",
        "anthropic-verification",
    }
    missing = sorted(required - set(profiles))
    if missing:
        print(json.dumps({"status": "blocked_profiles_missing", "profiles": missing}))
        return 3

    results = [
        _profile_probe(
            profiles["runtime-vision"],
            ModelCapability.VISUAL_DOCUMENT_UNDERSTANDING,
            endpoint_family="chat_completions_raster",
        ),
        _native_pdf_probe(profiles["runtime-vision"]),
        _profile_probe(
            profiles["gemini-vision"],
            ModelCapability.VISUAL_DOCUMENT_UNDERSTANDING,
            endpoint_family="openai_compatible_chat_completions",
        ),
        _profile_probe(
            profiles["deepseek-accounting"],
            ModelCapability.ACCOUNTING_REASONING,
            endpoint_family="openai_compatible_chat_completions",
        ),
        _profile_probe(
            profiles["anthropic-verification"],
            ModelCapability.INDEPENDENT_VERIFICATION,
            endpoint_family="anthropic_messages",
        ),
    ]
    report = {
        "schema_version": "provider-route-probes/1.0",
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "private_client_data_used": False,
        "secrets_persisted": False,
        "routes": results,
        "circuit_breakers": ai_provider.provider_circuit_report(),
    }
    args.private_output.parent.mkdir(parents=True, exist_ok=True)
    args.private_output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    safe_summary = {
        "report": str(args.private_output),
        "routes": results,
        "circuit_breakers": report["circuit_breakers"],
        "secrets_exposed": False,
    }
    print(json.dumps(safe_summary, indent=2))
    return 0 if all(item["capability_supported"] for item in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
