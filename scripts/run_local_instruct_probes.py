"""Run public-safe loopback probes for the Phase A instruct candidate."""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _synthetic_invoice_image() -> str:
    image = Image.new("RGB", (1600, 900), "white")
    draw = ImageDraw.Draw(image)
    font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "arial.ttf"
    title = ImageFont.truetype(str(font_path), 108)
    body = ImageFont.truetype(str(font_path), 82)
    draw.text((100, 70), "SYNTHETIC INVOICE", fill="black", font=title)
    draw.text((100, 240), "Vendor: SYNTHETIC SUPPLIES", fill="black", font=body)
    draw.text((100, 360), "Invoice: VISION42", fill="black", font=body)
    draw.text((100, 480), "SYNTHETIC WIDGET     $18.75", fill="black", font=body)
    draw.text((100, 640), "TOTAL                 $18.75", fill="black", font=body)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="qwen3-vl:2b-instruct")
    parser.add_argument("--profile-id", default="local-qwen3-vl-2b-instruct")
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    os.environ["INNER_VIEW_LOCAL_INFERENCE_ONLY"] = "1"
    os.environ["OLLAMA_NO_CLOUD"] = "1"
    os.environ["LOCAL_MULTIMODAL_MODEL"] = args.model
    os.environ["LOCAL_MULTIMODAL_PROFILE_ID"] = args.profile_id
    os.environ["LOCAL_MULTIMODAL_BASE_URL"] = args.base_url
    os.environ["LOCAL_MULTIMODAL_CONTEXT_TOKENS"] = "8192"

    from webapp.backend.services.local_inference_guard import (
        LocalInferenceNetworkBlocked,
        assert_dispatch_allowed,
        local_network_isolation,
    )
    from webapp.backend.services.local_multimodal_provider import (
        LocalMultimodalProvider,
        LocalMultimodalProviderError,
    )
    from webapp.backend.services.semantic_reasoning_gateway import (
        InvoiceSemanticProposalEnvelope,
    )

    provider = LocalMultimodalProvider(
        model=args.model,
        base_url=args.base_url,
        profile_id=args.profile_id,
        timeout_seconds=180,
    )
    probes: list[dict[str, object]] = []

    def execute(name: str, payload: dict) -> tuple[object | None, dict[str, object]]:
        started = time.perf_counter()
        try:
            result = provider.chat_completion(payload)
        except LocalMultimodalProviderError as exc:
            row = {
                "name": name,
                "passed": False,
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                "failure_code": exc.failure_code,
            }
            probes.append(row)
            return None, row
        row = {
            "name": name,
            "passed": False,
            "response_channel": result.response_channel,
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            "provider_latency_ms": result.latency_ms,
            "ram_used_mb_before": result.resources.system_ram_used_mb_before,
            "ram_used_mb_after": result.resources.system_ram_used_mb_after,
            "gpu_memory_mb_before": result.resources.gpu_memory_used_mb_before,
            "gpu_memory_mb_after": result.resources.gpu_memory_used_mb_after,
            "gpu_utilization_percent_after": result.resources.gpu_utilization_percent_after,
            "warnings": list(result.warnings),
        }
        probes.append(row)
        return result, row

    with local_network_isolation():
        text_result, text_probe = execute("text_json", {
            "model": args.model,
            "messages": [{
                "role": "user",
                "content": (
                    'Return strict JSON only: {"probe":"INSTRUCT_TEXT_JSON","ok":true}. '
                    "Do not add keys or prose."
                ),
            }],
            "max_output_tokens": 512,
        })
        text_probe["passed"] = bool(text_result is not None
            and
            text_result.response_channel == "content"
            and text_result.structured_output.get("probe") == "INSTRUCT_TEXT_JSON"
            and text_result.structured_output.get("ok") is True
        )

        image_ref = _synthetic_invoice_image()
        vision_result, vision_probe = execute("high_contrast_visual", {
            "model": args.model,
            "messages": [{
                "role": "system",
                "content": "Observe the synthetic image and return strict JSON facts only.",
            }, {
                "role": "user",
                "content": [{
                    "type": "text",
                    "text": (
                        "Extract the visible invoice facts. Use vendor_name, invoice_number, "
                        "total_amount, and line_items with source_page, raw_description, "
                        "generated_description, amount. gl_account_candidate must be empty."
                    ),
                }, {
                    "type": "image_url", "image_url": {"url": image_ref},
                }],
            }],
            "max_output_tokens": 1024,
        })
        lines = list(vision_result.structured_output.get("line_items") or []) if vision_result else []
        vision_probe["passed"] = bool(vision_result is not None
            and
            vision_result.response_channel == "content"
            and str(vision_result.structured_output.get("invoice_number") or "").upper() == "VISION42"
            and abs(float(vision_result.structured_output.get("total_amount") or 0) - 18.75) <= 0.01
            and lines
            and all(not str(line.get("gl_account_candidate") or "") for line in lines)
        )

        semantic_result, semantic_probe = execute("semantic_envelope", {
            "model": args.model,
            "messages": [{
                "role": "user",
                "content": json.dumps({
                    "instruction": "Return only the exact candidate-only proposal envelope.",
                    "observed_text": "SYNTHETIC FILTER",
                    "response_schema": {
                        "proposals": [{
                            "line_item_id": "synthetic-line-1",
                            "line_family": "materials",
                            "trade_family": "general",
                            "work_mode": "materials",
                            "confidence": 0.9,
                            "evidence_quotes": ["SYNTHETIC FILTER"],
                            "candidate_gl_codes": [],
                            "reasoning_summary": "Physical filter is a material item.",
                        }],
                    },
                }, separators=(",", ":")),
            }],
            "max_output_tokens": 768,
        })
        envelope = (
            InvoiceSemanticProposalEnvelope.model_validate({
                "proposals": semantic_result.structured_output.get("proposals") or [],
            })
            if semantic_result is not None else None
        )
        semantic_probe["passed"] = bool(semantic_result is not None
            and
            semantic_result.response_channel == "content"
            and envelope is not None
            and len(envelope.proposals) == 1
            and envelope.proposals[0].line_item_id == "synthetic-line-1"
            and envelope.proposals[0].candidate_gl_codes == []
        )

        remote_blocked = False
        try:
            assert_dispatch_allowed(
                provider="openai",
                url="https://api.openai.com/v1/responses",
                stage="local_instruct_remote_fallback_probe",
            )
        except LocalInferenceNetworkBlocked:
            remote_blocked = True
        probes.append({
            "name": "remote_fallback_rejection",
            "passed": remote_blocked,
            "remote_calls": 0,
        })

    summary = {
        "schema_version": "phase-a-local-instruct-probes/1.0",
        "model": args.model,
        "profile_id": args.profile_id,
        "all_passed": all(bool(row["passed"]) for row in probes),
        "remote_provider_calls": 0,
        "probes": probes,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0 if summary["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
