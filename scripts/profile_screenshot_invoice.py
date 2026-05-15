"""Phase PERF-1 — profile screenshot/image invoice processing.

Measures the cold + warm path for image-only invoices (PNG/JPG) so we
can see how much time the OCR cache shaves off on re-runs. Each
fixture is read straight through ``utils.pdf_text_extractor`` even
when the file is an image — for that path the cache key still works
because the file's bytes are hashed.

This script ONLY measures the extraction step. It does not invoke
vendor processors or AI providers (per Phase PERF-1's no-real-AI
constraint).

Output:
  docs/reports/phases/screenshots/phase_perf1/profile_screenshot_invoice.json

Usage:
  python scripts/profile_screenshot_invoice.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DROPBOX_DISABLE_FOR_TESTS", "1")
os.environ.setdefault("AI_FALLBACK_DISABLED", "1")

from utils import ocr_cache  # noqa: E402
from webapp.backend.services import perf_timer  # noqa: E402

# Try imports needed for raw image OCR. If pytesseract isn't installed
# we report it and bail gracefully.
try:
    import pytesseract  # type: ignore
    from PIL import Image  # type: ignore
    _OCR_OK = True
except Exception as e:
    pytesseract = None  # type: ignore
    Image = None  # type: ignore
    _OCR_OK = False
    _OCR_IMPORT_ERR = repr(e)


_FIXTURES = [
    "Training Bills_Invoices/Electricity - Power/Weakley County Municipal Electric "
    "System/Bills_Training/8c40c2c8-2521-4377-8868-217cfa77dbfc.png",
    "Training Bills_Invoices/Electricity - Power/Weakley County Municipal Electric "
    "System/Bills_Training/24b4b317-787e-4f1d-bbf1-cbc2012371a4.jpg",
]


def _ocr_image(path: Path) -> tuple[str, float, float]:
    """Run a single Tesseract pass on the image. Returns
    (text, mean_confidence_0_1, ocr_ms)."""
    if not _OCR_OK:
        return ("", 0.0, 0.0)
    img = Image.open(path)
    if img.mode != "RGB":
        img = img.convert("RGB")
    t0 = time.perf_counter()
    text = pytesseract.image_to_string(img) or ""
    ocr_ms = (time.perf_counter() - t0) * 1000.0
    # Mean confidence — best effort; some Tesseract versions return -1.
    try:
        data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
        confs = [float(c) for c in (data.get("conf") or []) if str(c).strip() not in ("", "-1")]
        mean_conf = (sum(confs) / len(confs) / 100.0) if confs else 0.0
    except Exception:
        mean_conf = 0.0
    return text, mean_conf, ocr_ms


def _profile_one(path: Path) -> dict:
    if not path.is_file():
        return {"path": str(path), "skipped": "missing"}
    out: dict = {"path": str(path.relative_to(ROOT))}

    # Cold: clear cache for this file.
    key = ocr_cache.cache_key(path, 200)
    cache_file = ROOT / "webapp_data" / "cache" / "ocr" / f"{key}.json"
    if cache_file.is_file():
        try:
            cache_file.unlink()
        except OSError:
            pass

    if not _OCR_OK:
        out["skipped"] = "ocr_dependencies_missing"
        out["error"] = _OCR_IMPORT_ERR
        return out

    # First pass — actual Tesseract.
    text_cold, conf_cold, ocr_ms_cold = _ocr_image(path)
    out["cold"] = {
        "ocr_ms": round(ocr_ms_cold, 2),
        "mean_confidence": round(conf_cold, 3),
        "text_chars": len(text_cold or ""),
    }

    # Persist to cache so the warm pass can use it. We hand-roll the
    # payload because the image path doesn't go through extract_pdf_text.
    ocr_cache.store(path, 200, {
        "pages": [{
            "page_number": 1,
            "text": text_cold,
            "width": 0,
            "height": 0,
            "words": [],
        }],
        "extraction_method": "ocr",
        "confidence": conf_cold,
        "warnings": [],
    })

    # Warm — cache lookup only.
    t0 = time.perf_counter()
    cached = ocr_cache.lookup(path, 200)
    warm_ms = (time.perf_counter() - t0) * 1000.0
    out["warm"] = {
        "lookup_ms": round(warm_ms, 2),
        "hit": cached is not None,
        "text_chars": len((cached or {}).get("pages", [{}])[0].get("text") or "") if cached else 0,
    }
    out["speedup_x"] = round(ocr_ms_cold / max(warm_ms, 0.001), 2)
    return out


def main() -> int:
    results: list[dict] = []
    for rel in _FIXTURES:
        p = ROOT / rel
        print(f"--- {p.name} ---", flush=True)
        r = _profile_one(p)
        results.append(r)
        print(json.dumps(r, indent=2), flush=True)

    out_dir = ROOT / "docs" / "reports" / "phases" / "screenshots" / "phase_perf1"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "profile_screenshot_invoice.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "schema": "phase_perf1/profile_screenshot/v1",
            "run_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "ocr_dependencies_available": _OCR_OK,
            "fixtures": results,
            "ocr_cache_stats": ocr_cache.cache_stats(),
        }, f, indent=2)
    print(f"\nWrote summary -> {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
