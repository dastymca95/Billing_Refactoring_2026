"""Generate an offline private crop review for the latest five-document Gate 5.

The output is deliberately written only below the ignored experiment runtime.
No provider module is imported and no network operation is available here.
Console output contains aggregate counts only.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageSequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from webapp.backend.services import document_ingestion  # noqa: E402
from webapp.backend.services.gemini_supplementary_verification import (  # noqa: E402
    SupplementaryTarget,
    SupplementaryTargetType,
)
from webapp.backend.services.supplementary_evidence_planner import (  # noqa: E402
    CropRole,
    EvidenceLocalizationError,
    build_evidence_packet,
    build_supplementary_evidence_plan,
)


REVIEW_VERSION = "phase-a-supplementary-review/1.3"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build private offline supplementary crop review")
    parser.add_argument("--experiment-root", type=Path)
    parser.add_argument("--source-root", type=Path)
    args = parser.parse_args()
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    experiment_root = _experiment_root(args.experiment_root)
    source_root = _source_root(args.source_root)
    _assert_private_output_root(experiment_root)
    latest_run = _latest_five_document_run(experiment_root)
    run_manifest = _run_manifest(latest_run)
    source_map = dict(run_manifest.get("source_map") or {})
    if len(source_map) != 5:
        raise SystemExit("latest_gate5_source_count_invalid")
    inventory_root = _single_directory(experiment_root / "snapshots", "corpus-*")
    inventory = {
        str(row["document_id"]): row
        for row in _read_jsonl(inventory_root / "inventory.jsonl")
    }
    locators = {
        str(row["document_id"]): str(row["relative_path"])
        for row in _read_jsonl(inventory_root / "private_locators.jsonl")
    }
    output_root = experiment_root / "phase_a" / "offline_supplementary_review" / "review-v4"
    if output_root.exists():
        if (output_root / "COMPLETE").is_file():
            raise SystemExit("private_review_version_already_exists")
        # Only a failed artifact created by this version is replaceable.  Gate
        # runs, spend records, manifests, and completed reviews are untouched.
        output_root.relative_to(experiment_root / "phase_a" / "offline_supplementary_review")
        shutil.rmtree(output_root)
    output_root.mkdir(parents=True, exist_ok=False)

    target_counts: Counter[str] = Counter()
    crops_per_plan: list[int] = []
    invalid_crops = 0
    invalid_reasons: Counter[str] = Counter()
    missing_context = 0
    continuation_pages = 0
    manual_review_plans = 0
    plan_count = 0
    html_cards: list[str] = []

    for document_index, (_private_name, source_record) in enumerate(source_map.items(), start=1):
        document_id = str(source_record["document_id"])
        expected_sha = str(source_record["source_content_sha256"])
        inventory_record = inventory[document_id]
        if str(inventory_record.get("content_sha256") or "") != expected_sha:
            raise SystemExit("private_review_inventory_hash_mismatch")
        source = (source_root / locators[document_id]).resolve(strict=True)
        source.relative_to(source_root.resolve(strict=True))
        if _sha256(source) != expected_sha:
            raise SystemExit("private_review_source_hash_mismatch")
        candidate = document_ingestion.ingest_document(source, allow_ocr=True, allow_vision_hint=False)
        layout = candidate.to_dict()
        page_refs = _render_pages(source)
        batch_id = _batch_id_for_source(latest_run, expected_sha, position=document_index)
        target_types = _target_types_for_batch(latest_run, batch_id)
        if not target_types:
            raise SystemExit("private_review_target_trace_missing")
        initial_facts = _find_saved_observed_facts(latest_run, expected_sha, batch_id=batch_id) or {
            "line_items": [], "evidence": [], "page_reconciliations": [], "warnings": [],
        }
        doc_dir = output_root / f"document_{document_index:02d}"
        doc_dir.mkdir()
        for target_index, target_value in enumerate(target_types, start=1):
            target = SupplementaryTarget(
                target_type=SupplementaryTargetType(target_value),
                page_number=1,
                field_name=_field_for_target(target_value),
                local_trigger_codes=["persisted_gate5_local_validation"],
            )
            opaque_id = "doc_" + hashlib.sha256(expected_sha.encode("ascii")).hexdigest()[:24]
            plan = build_supplementary_evidence_plan(
                opaque_document_id=opaque_id, target=target,
                initial_facts=initial_facts, document_layout=layout,
            )
            plan_count += 1
            target_counts[plan.target_subtype.value] += 1
            crops_per_plan.append(len(plan.crops))
            continuation_pages += int(any(crop.role is CropRole.CONTINUATION for crop in plan.crops))
            plan_path = doc_dir / f"plan_{target_index:02d}.json"
            plan_path.write_text(
                json.dumps(plan.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            try:
                packet = build_evidence_packet(plan, page_images=page_refs)
            except EvidenceLocalizationError as exc:
                invalid_crops += 1
                invalid_reasons[exc.failure_code] += 1
                manual_review_plans += 1
                missing_context += int(exc.failure_code == "supplementary_context_thumbnail_missing")
                html_cards.append(
                    f"<section><h2>Document {document_index}, plan {target_index}</h2>"
                    f"<p>Manual review required: {exc.failure_code}</p></section>"
                )
                continue
            images_html: list[str] = []
            for image_index, packet_image in enumerate(packet.images, start=1):
                image_name = f"plan_{target_index:02d}_crop_{image_index:02d}.jpg"
                _write_data_url(doc_dir / image_name, packet_image.data_url)
                images_html.append(
                    f"<figure><img src='document_{document_index:02d}/{image_name}' />"
                    f"<figcaption>{packet_image.role.value} / {packet_image.category.value}</figcaption></figure>"
                )
            overlay_names = _write_overlays(doc_dir, plan, page_refs, target_index)
            images_html.extend(
                f"<figure><img src='document_{document_index:02d}/{name}' /><figcaption>region overlay</figcaption></figure>"
                for name in overlay_names
            )
            html_cards.append(
                f"<section><h2>Document {document_index}, plan {target_index}</h2>"
                f"<p>Subtype: {plan.target_subtype.value}</p>{''.join(images_html)}</section>"
            )

    aggregate = {
        "review_version": REVIEW_VERSION,
        "documents": 5,
        "plans_generated": plan_count,
        "targets_by_subtype": dict(sorted(target_counts.items())),
        "average_crops_per_plan": round(sum(crops_per_plan) / len(crops_per_plan), 3) if crops_per_plan else 0.0,
        "invalid_crop_count": invalid_crops,
        "invalid_crop_reasons": dict(sorted(invalid_reasons.items())),
        "missing_context_count": missing_context,
        "continuation_page_count": continuation_pages,
        "plans_requiring_manual_review": manual_review_plans,
        "provider_calls": 0,
    }
    (output_root / "aggregate_metrics.json").write_text(
        json.dumps(aggregate, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    (output_root / "crop_preview.html").write_text(
        "<!doctype html><meta charset='utf-8'><title>Private supplementary crop review</title>"
        "<style>body{font-family:sans-serif}section{border:1px solid #ccc;margin:1rem;padding:1rem}"
        "img{max-width:720px;max-height:540px;border:1px solid #888}figure{display:inline-block;vertical-align:top}</style>"
        + "".join(html_cards),
        encoding="utf-8",
    )
    (output_root / "COMPLETE").write_text(REVIEW_VERSION + "\n", encoding="utf-8")
    print(json.dumps(aggregate, sort_keys=True))
    return 0


def _experiment_root(explicit: Path | None) -> Path:
    candidates: list[Path] = []
    configured = explicit or _env_path("INNER_VIEW_DOCUMENT_LEARNING_EXPERIMENT_ROOT")
    if configured and (configured / "calibration" / "active_phase_a_calibration.json").is_file():
        candidates.append(configured.resolve())
    if not candidates:
        search_root = PROJECT_ROOT.parent
        candidates = [
            phase_a.parent.resolve()
            for phase_a in search_root.rglob("phase_a")
            if (phase_a / "runs").is_dir()
            and (phase_a.parent / "calibration" / "active_phase_a_calibration.json").is_file()
        ]
    unique = list(dict.fromkeys(candidates))
    if len(unique) != 1:
        raise SystemExit("private_experiment_root_is_ambiguous")
    return unique[0]


def _source_root(explicit: Path | None) -> Path:
    root = explicit or _env_path("INNER_VIEW_REASONING_TRAINING_ROOT")
    if root is None or not root.is_dir():
        raise SystemExit("private_source_root_unavailable")
    return root.resolve(strict=True)


def _assert_private_output_root(root: Path) -> None:
    try:
        relative = root.resolve().relative_to(PROJECT_ROOT.resolve())
    except ValueError:
        return
    check = subprocess.run(
        ["git", "check-ignore", "--quiet", "--", str(relative)],
        cwd=PROJECT_ROOT, check=False, capture_output=True,
    )
    if check.returncode != 0:
        raise SystemExit("private_experiment_root_is_not_git_ignored")


def _latest_five_document_run(root: Path) -> Path:
    candidates = []
    for run in (root / "phase_a" / "runs").iterdir():
        if not run.is_dir():
            continue
        try:
            manifest = _run_manifest(run)
        except Exception:
            continue
        if len(manifest.get("source_map") or {}) == 5 and len(manifest.get("shard_results") or []) == 5:
            candidates.append(run)
    if not candidates:
        raise SystemExit("five_document_gate5_run_unavailable")
    return max(candidates, key=lambda item: item.stat().st_mtime)


def _run_manifest(run: Path) -> dict[str, Any]:
    for path in run.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "source_map" in data and "shard_results" in data:
            return data
    raise ValueError("run manifest missing")


def _batch_id_for_source(run: Path, digest: str, *, position: int) -> str:
    for inputs in run.rglob("inputs"):
        if not inputs.is_dir():
            continue
        files = [path for path in inputs.iterdir() if path.is_file()]
        if any(_sha256(path) == digest for path in files):
            return inputs.parent.name
    # Controlled runs may clean their staged private input after producing the
    # immutable result.  Runner order is stable and each Gate-5 shard contains
    # exactly one assignment, so the persisted batch metadata is the safe
    # position-preserving fallback.
    metadata_paths = sorted(
        run.rglob("batch_metadata.json"), key=lambda item: (item.stat().st_mtime_ns, str(item.parent))
    )
    if len(metadata_paths) != 5 or not 1 <= position <= len(metadata_paths):
        raise SystemExit("private_review_staged_source_missing")
    metadata = json.loads(metadata_paths[position - 1].read_text(encoding="utf-8"))
    batch_id = str(metadata.get("batch_id") or metadata_paths[position - 1].parent.name)
    if not batch_id:
        raise SystemExit("private_review_batch_identity_missing")
    return batch_id


def _target_types_for_batch(run: Path, batch_id: str) -> list[str]:
    result: list[str] = []
    for path in run.rglob("*.jsonl"):
        for row in _read_jsonl(path):
            if str(row.get("batch_id") or "") != batch_id:
                continue
            if row.get("event") != "supplementary_verification":
                continue
            value = str(row.get("target_category") or "")
            if value and value not in result:
                result.append(value)
    return result


def _find_saved_observed_facts(
    run: Path, digest: str, *, batch_id: str,
) -> dict[str, Any] | None:
    for path in run.rglob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        explicit_digest = str(payload.get("source_sha256") or "") if isinstance(payload, dict) else ""
        belongs_to_batch = batch_id in str(path)
        if explicit_digest and explicit_digest != digest:
            continue
        if not explicit_digest and not belongs_to_batch:
            continue
        found = _find_fact_shape(payload)
        if found is not None:
            return found
    return None


def _find_fact_shape(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        if isinstance(value.get("observed_payload"), dict):
            candidate = value["observed_payload"]
            if isinstance(candidate.get("line_items"), list):
                return candidate
        if isinstance(value.get("line_items"), list) and (
            "total_amount" in value or "page_reconciliations" in value
        ):
            return value
        for child in value.values():
            found = _find_fact_shape(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_fact_shape(child)
            if found is not None:
                return found
    return None


def _render_pages(path: Path) -> dict[int, list[str]]:
    suffix = path.suffix.casefold()
    result: dict[int, list[str]] = {}
    if suffix == ".pdf":
        try:
            import fitz  # type: ignore
            with fitz.open(path) as document:
                for index, page in enumerate(document, start=1):
                    pixmap = page.get_pixmap(matrix=fitz.Matrix(2.5, 2.5), alpha=False)
                    image = Image.open(io.BytesIO(pixmap.tobytes("png"))).convert("RGB")
                    result[index] = [_image_data_url(image)]
        except ImportError:
            import pypdfium2 as pdfium  # type: ignore
            document = pdfium.PdfDocument(str(path))
            try:
                for index in range(len(document)):
                    image = document[index].render(scale=2.5).to_pil().convert("RGB")
                    result[index + 1] = [_image_data_url(image)]
            finally:
                document.close()
        return result
    with Image.open(path) as image:
        for index, frame in enumerate(ImageSequence.Iterator(image), start=1):
            result[index] = [_image_data_url(frame.convert("RGB"))]
    return result


def _image_data_url(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _write_data_url(path: Path, data_url: str) -> None:
    path.write_bytes(base64.b64decode(data_url.split(",", 1)[1]))


def _write_overlays(
    directory: Path, plan: Any, page_refs: Mapping[int, list[str]], target_index: int,
) -> list[str]:
    output: list[str] = []
    by_page: dict[int, list[Any]] = {}
    for crop in plan.crops:
        by_page.setdefault(crop.coordinates.page_number, []).append(crop)
    for page, crops in by_page.items():
        raw = base64.b64decode(page_refs[page][0].split(",", 1)[1])
        image = Image.open(io.BytesIO(raw)).convert("RGB")
        draw = ImageDraw.Draw(image)
        for crop in crops:
            c = crop.coordinates
            box = (
                int(c.x * image.width), int(c.y * image.height),
                int(c.right * image.width), int(c.bottom * image.height),
            )
            color = "red" if crop.role is CropRole.PRIMARY else "blue"
            draw.rectangle(box, outline=color, width=max(3, image.width // 500))
        name = f"plan_{target_index:02d}_page_{page:02d}_overlay.jpg"
        image.thumbnail((1400, 1400))
        image.save(directory / name, format="JPEG", quality=85)
        output.append(name)
    return output


def _field_for_target(target: str) -> str:
    return {
        "invoice_number_ambiguity": "invoice_number",
        "date_ambiguity": "invoice_date",
        "vendor_name_ambiguity": "vendor_name",
        "page_continuation": "page_continuation_status",
        "paid_crossed_out_row_status": "paid_status",
    }.get(target, "reconciliation")


def _single_directory(root: Path, pattern: str) -> Path:
    matches = [item for item in root.glob(pattern) if item.is_dir()]
    if len(matches) != 1:
        raise SystemExit("private_snapshot_selection_ambiguous")
    return matches[0]


def _read_jsonl(path: Path):
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            yield json.loads(line)


def _env_path(name: str) -> Path | None:
    value = str(os.environ.get(name) or "").strip()
    return Path(value).expanduser() if value else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
