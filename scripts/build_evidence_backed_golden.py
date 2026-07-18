"""Build a private, pending-human evidence contract for the seven-invoice batch.

This script never calls a provider and never promotes an extractor result to
ground truth.  It renders exact source pages, creates evidence crops, and
records both historical candidates for later human adjudication.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from webapp.backend.services.accounting_contracts import CropCoordinates
from webapp.backend.services.canonical_semantics import resolve_canonical_concept
from webapp.backend.services.evidence_benchmark import (
    AdjudicationState,
    EvidenceAsset,
    EvidenceBackedField,
    EvidenceBackedGoldenContract,
    ExportSafetyExpectation,
    GoldenInvoiceContract,
    GoldenRowContract,
    ObservedCandidate,
    canonical_sha256,
    file_sha256,
)


BATCH_ID = "batch_20260717_155909_724"
SOURCE_PAGE = {
    "22-3127": 1, "22-3195": 2, "22-3194": 1, "22-3197": 3,
    "22-3198": 4, "22-3172": 1, "180547": 1,
}
PAGE_OVERRIDES = {("22-3127", "21F"): 2}
FALLBACK_Y = {
    "22-3195": {"41F": 3150},
    "22-3197": {value: 3000 + index * 145 for index, value in enumerate(
        ("22F", "21A", "17C", "37D", "21F", "33I", "61H", "53D"))},
    "22-3198": {"2302-A": 3000, "2302A": 3000, "2516 B": 3145, "2316 B": 3145},
    "180547": {"unassigned": 2300},
    "22-3127": {"21F": 3000},
}
VISUAL_FIELDS = {
    "Amount", "Quantity", "Unit Price", "Line Item Description", "Location",
    "Property Abbreviation", "Invoice Date", "Due Date", "ai_service_date",
    "ai_service_date_raw", "ai_handwritten_row_identities", "ai_row_identity_evidence",
    "ai_row_identity_verification", "ai_excluded_paid_rows",
}


def _dump(model) -> dict[str, Any]:
    return model.model_dump(mode="json")


def _load_json(path: Path, *, encoding: str = "utf-8") -> dict[str, Any]:
    return json.loads(path.read_text(encoding=encoding))


def _render_page(pdf: Path, page: int, output: Path, pdftoppm: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.is_file():
        image = None
        try:
            image = Image.open(output)
            image.verify()
            return
        except OSError:
            if image is not None:
                image.close()
            output.unlink()
        finally:
            if image is not None:
                image.close()
    prefix = output.with_suffix("")
    subprocess.run([
        pdftoppm, "-f", str(page), "-l", str(page), "-r", "600", "-png",
        "-singlefile", str(pdf), str(prefix),
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def _candidate(value: Any, source: str) -> ObservedCandidate:
    rendered = None if value is None else str(value)
    return ObservedCandidate(value=rendered, normalized_value=rendered, source=source)


def _identity_by_ordinal(invoice: dict[str, Any]) -> dict[str, dict[str, Any]]:
    first_meta = invoice["rows"][0].get("_meta") or {}
    evidence = list(first_meta.get("ai_handwritten_row_identities") or [])
    if any(str(item.get("selection_marker") or "") == "circled" for item in evidence):
        evidence = [item for item in evidence if str(item.get("selection_marker") or "") == "circled"]
    groups = []
    for row in invoice["rows"]:
        meta = row.get("_meta") or {}
        value = str(meta.get("ai_line_location_candidate") or meta.get("ai_line_location") or "unassigned")
        if value not in groups:
            groups.append(value)
    result: dict[str, dict[str, Any]] = {}
    for index, group in enumerate(groups):
        if index < len(evidence):
            result[group] = evidence[index]
    return result


def _row_box(invoice_id: str, group: str, identity: dict[str, Any] | None,
             width: int, height: int, page: int) -> CropCoordinates:
    raw = (identity or {}).get("crop_coordinates") or {}
    y = int(raw.get("y") or FALLBACK_Y.get(invoice_id, {}).get(group, 2900))
    x = max(0, min(250, width - 1))
    top = max(0, y - 10)
    return CropCoordinates(
        page=page, x=x, y=top, width=min(width - x, 4600),
        height=min(height - top, 130 if invoice_id != "180547" else 2600),
        render_dpi=600, source_page_width=width, source_page_height=height,
    )


def _identity_box(row_box: CropCoordinates, identity: dict[str, Any] | None) -> CropCoordinates:
    raw = (identity or {}).get("crop_coordinates") or {}
    if raw:
        x = max(0, int(raw["x"]) - 50)
        y = max(0, int(raw["y"]) - 15)
        width = min((row_box.source_page_width or 5100) - x, int(raw["width"]) + 100)
        height = min((row_box.source_page_height or 6600) - y, int(raw["height"]) + 30)
    else:
        x, y = row_box.x, row_box.y
        width, height = min(row_box.width, 800), row_box.height
    return CropCoordinates(
        page=row_box.page, x=x, y=y, width=width, height=height,
        render_dpi=row_box.render_dpi, source_page_width=row_box.source_page_width,
        source_page_height=row_box.source_page_height,
    )


def _column_box(row_box: CropCoordinates, *, x: int, width: int) -> CropCoordinates:
    page_width = row_box.source_page_width or 5100
    x = max(0, min(x, page_width - 1))
    return CropCoordinates(
        page=row_box.page, x=x, y=row_box.y, width=min(width, page_width - x),
        height=row_box.height, render_dpi=row_box.render_dpi,
        source_page_width=row_box.source_page_width,
        source_page_height=row_box.source_page_height,
    )


def _crop_asset(source_image: Image.Image, output_root: Path, relative: Path,
                box: CropCoordinates, source_sha: str) -> EvidenceAsset:
    output = output_root / relative
    output.parent.mkdir(parents=True, exist_ok=True)
    crop = source_image.crop((box.x, box.y, box.x + box.width, box.y + box.height))
    crop.save(output)
    return EvidenceAsset(
        source_document_sha256=source_sha.lower(), source_page=box.page,
        crop_coordinates=box, crop_sha256=file_sha256(output),
        crop_ref=relative.as_posix(),
    )


def _field(name: str, asset: EvidenceAsset, cold: Any = None, prior: Any = None) -> EvidenceBackedField:
    candidates = []
    if cold not in (None, "", []):
        candidates.append(_candidate(cold, "cold_extractor"))
    if prior not in (None, "", []) and str(prior) != str(cold):
        candidates.append(_candidate(prior, "prior_accepted_run"))
    return EvidenceBackedField(
        field_name=name, candidates=candidates, evidence=[asset],
        state=AdjudicationState.PENDING_HUMAN_REVIEW,
    )


def build(base: Path, output: Path, pdftoppm: str) -> dict[str, Any]:
    cold = _load_json(base / "cold_result.json")
    prior = _load_json(base / "official_golden.json")
    comparison = _load_json(base / "cold_comparison.json", encoding="utf-16")
    input_root = base / "isolated_webapp_data" / "batches" / BATCH_ID / "input"
    prior_by_invoice = {str(item["invoice_number"]): item for item in prior["all_invoices"]}
    output.mkdir(parents=True, exist_ok=True)
    rendered_root = output / "rendered_sources"
    invoices: list[GoldenInvoiceContract] = []
    row_assets: dict[tuple[str, int], EvidenceAsset] = {}
    row_field_assets: dict[tuple[str, int, str], EvidenceAsset] = {}
    header_assets: dict[str, EvidenceAsset] = {}
    source_manifest = []

    for invoice in cold["all_invoices"]:
        invoice_id = str(invoice["invoice_number"])
        prior_invoice = prior_by_invoice[invoice_id]
        pdf = input_root / invoice["source_file"]
        source_sha = file_sha256(pdf)
        source_manifest.append({"invoice_id": invoice_id, "file_name": pdf.name,
                                "sha256": source_sha, "default_page": SOURCE_PAGE[invoice_id]})
        pages = {SOURCE_PAGE[invoice_id]}
        if invoice_id == "22-3127":
            pages.add(2)
        page_png: dict[int, Path] = {}
        for page in sorted(pages):
            png = rendered_root / invoice_id / f"page-{page}.png"
            _render_page(pdf, page, png, pdftoppm)
            page_png[page] = png
        page_images = {page: Image.open(path) for page, path in page_png.items()}
        width, height = page_images[SOURCE_PAGE[invoice_id]].size
        header_box = CropCoordinates(
            page=SOURCE_PAGE[invoice_id], x=200, y=150,
            width=min(width - 200, 4700), height=min(height - 150, 2600),
            render_dpi=600, source_page_width=width, source_page_height=height,
        )
        header_asset = _crop_asset(
            page_images[SOURCE_PAGE[invoice_id]], output,
            Path("crops") / invoice_id / "header.png", header_box, source_sha,
        )
        header_assets[invoice_id] = header_asset
        cold_meta = invoice["rows"][0].get("_meta") or {}
        prior_meta = prior_invoice["rows"][0].get("_meta") or {}
        header_fields = {
            "vendor": _field("vendor", header_asset, invoice.get("vendor_key"), prior_invoice.get("vendor_key")),
            "invoice_number": _field("invoice_number", header_asset, invoice_id, prior_invoice.get("invoice_number")),
            "invoice_date": _field("invoice_date", header_asset, invoice.get("invoice_date"), prior_invoice.get("invoice_date")),
            "service_date": _field("service_date", header_asset, cold_meta.get("ai_service_date_raw"), prior_meta.get("ai_service_date_raw")),
            "due_date_text": _field("due_date_text", header_asset, cold_meta.get("ai_due_date_text"), prior_meta.get("ai_due_date_text")),
            "property": _field("property", header_asset, cold_meta.get("ai_property_candidate"), prior_meta.get("ai_property_candidate")),
            "sold_to_raw_text": _field("sold_to_raw_text", header_asset, cold_meta.get("ai_sold_to_raw_text"), prior_meta.get("ai_sold_to_raw_text")),
            "invoice_total": _field("invoice_total", header_asset, invoice.get("total_amount"), prior_invoice.get("total_amount")),
        }

        identity_lookup = _identity_by_ordinal(invoice)
        rows = []
        for index, row in enumerate(invoice["rows"]):
            meta = row.get("_meta") or {}
            prior_row = prior_invoice["rows"][index] if index < len(prior_invoice["rows"]) else {}
            prior_row_meta = prior_row.get("_meta") or {}
            group = str(meta.get("ai_line_location_candidate") or meta.get("ai_line_location") or "unassigned")
            page = PAGE_OVERRIDES.get((invoice_id, group), SOURCE_PAGE[invoice_id])
            page_width, page_height = page_images[page].size
            identity = identity_lookup.get(group)
            box = _row_box(invoice_id, group, identity, page_width, page_height, page)
            relative = Path("crops") / invoice_id / f"page-{page}-row-{index + 1:03d}.png"
            asset = _crop_asset(page_images[page], output, relative, box, source_sha)
            row_assets[(invoice_id, index + 1)] = asset
            identity_asset = _crop_asset(
                page_images[page], output,
                Path("crops") / invoice_id / f"page-{page}-row-{index + 1:03d}-identity.png",
                _identity_box(box, identity), source_sha,
            )
            concept_asset = _crop_asset(
                page_images[page], output,
                Path("crops") / invoice_id / f"page-{page}-row-{index + 1:03d}-components.png",
                _column_box(box, x=650, width=3900), source_sha,
            )
            paid_asset = _crop_asset(
                page_images[page], output,
                Path("crops") / invoice_id / f"page-{page}-row-{index + 1:03d}-paid.png",
                _column_box(box, x=4200, width=800), source_sha,
            )
            row_field_assets[(invoice_id, index + 1, "row_identity")] = identity_asset
            row_field_assets[(invoice_id, index + 1, "line_item_concept")] = concept_asset
            row_field_assets[(invoice_id, index + 1, "amount")] = concept_asset
            row_field_assets[(invoice_id, index + 1, "quantity")] = concept_asset
            row_field_assets[(invoice_id, index + 1, "unit_price")] = concept_asset
            row_field_assets[(invoice_id, index + 1, "paid_crossed_out_status")] = paid_asset
            raw_description = meta.get("ai_source_line_description") or row.get("Line Item Description")
            concept = resolve_canonical_concept(str(raw_description or ""))
            rows.append(GoldenRowContract(
                row_id=f"{invoice_id}:line:{index + 1:03d}", source_page=page,
                row_identity=_field("row_identity", identity_asset, group,
                                    (prior_row_meta.get("ai_line_location_candidate")
                                     or prior_row_meta.get("ai_line_location"))),
                paid_crossed_out_status=_field("paid_crossed_out_status", paid_asset, "payable", "payable"),
                line_item_concept=_field("line_item_concept", concept_asset, raw_description,
                                         prior_row_meta.get("ai_source_line_description")
                                         or prior_row.get("Line Item Description")),
                amount=_field("amount", concept_asset, row.get("Amount"), prior_row.get("Amount")),
                canonical_semantic_concept=concept.concept_id,
                acceptable_gl_set=[], expected_gl=None,
                required_review_categories=["human_evidence_adjudication_pending"],
                export_safety_expectation=ExportSafetyExpectation.BLOCKED,
            ))

        excluded_rows = []
        for excluded_index, excluded in enumerate(cold_meta.get("ai_excluded_paid_rows") or []):
            raw_identity = str(excluded.get("raw_apartment_number") or f"excluded-{excluded_index + 1}")
            identity = excluded.get("apartment_identity") or {}
            page_width, page_height = page_images[SOURCE_PAGE[invoice_id]].size
            box = _row_box(invoice_id, raw_identity, identity, page_width, page_height,
                           SOURCE_PAGE[invoice_id])
            asset = _crop_asset(
                page_images[SOURCE_PAGE[invoice_id]], output,
                Path("crops") / invoice_id / f"excluded-{excluded_index + 1:02d}.png",
                box, source_sha,
            )
            excluded_rows.append(GoldenRowContract(
                row_id=f"{invoice_id}:excluded:{excluded_index + 1:03d}",
                source_page=SOURCE_PAGE[invoice_id],
                row_identity=_field("row_identity", asset, raw_identity),
                paid_crossed_out_status=_field("paid_crossed_out_status", asset, "paid_excluded"),
                line_item_concept=_field("line_item_concept", asset,
                                         json.dumps(excluded.get("component_amounts") or {}, sort_keys=True)),
                amount=_field("amount", asset, excluded.get("row_total")),
                canonical_semantic_concept=None, acceptable_gl_set=[], expected_gl=None,
                required_review_categories=["paid_marker_ambiguous", "human_evidence_adjudication_pending"],
                export_safety_expectation=ExportSafetyExpectation.BLOCKED,
            ))

        invoices.append(GoldenInvoiceContract(
            invoice_id=invoice_id, source_file_name=pdf.name,
            source_document_sha256=source_sha.lower(), source_page=SOURCE_PAGE[invoice_id],
            header_fields=header_fields, rows=rows, excluded_rows=excluded_rows,
            required_review_categories=["human_evidence_adjudication_pending"],
            export_safety_expectation=ExportSafetyExpectation.BLOCKED,
        ))
        for image in page_images.values():
            image.close()

    source_manifest_hash = canonical_sha256(sorted(source_manifest, key=lambda item: item["invoice_id"]))
    contract = EvidenceBackedGoldenContract(
        batch_id=BATCH_ID, created_at=datetime.now(timezone.utc),
        source_manifest_sha256=source_manifest_hash,
        state=AdjudicationState.PENDING_HUMAN_REVIEW, invoices=invoices,
    )
    (output / "golden_contract.pending.json").write_text(
        json.dumps(_dump(contract), indent=2, sort_keys=True), encoding="utf-8"
    )
    (output / "source_manifest.json").write_text(
        json.dumps({"batch_id": BATCH_ID, "sha256": source_manifest_hash,
                    "sources": source_manifest}, indent=2, sort_keys=True), encoding="utf-8"
    )

    queue = []
    for sequence, difference in enumerate(comparison["critical_differences"], start=1):
        invoice_id = str(difference["invoice"])
        line = difference.get("line")
        canonical_field = {
            "ai_handwritten_row_identities": "row_identity",
            "ai_row_identity_evidence": "row_identity",
            "ai_row_identity_verification": "row_identity",
            "Location": "row_identity",
            "ai_excluded_paid_rows": "paid_crossed_out_status",
            "Line Item Description": "line_item_concept",
            "Amount": "amount", "Quantity": "quantity", "Unit Price": "unit_price",
        }.get(difference["field"])
        asset = (row_field_assets.get((invoice_id, int(line), canonical_field))
                 if line and canonical_field else None)
        asset = asset or (row_assets.get((invoice_id, int(line))) if line else header_assets[invoice_id])
        queue.append({
            "task_id": f"visual-disagreement-{sequence:04d}",
            "invoice_id": invoice_id, "line": line, "field": difference["field"],
            "is_visual_field": difference["field"] in VISUAL_FIELDS,
            "priority": "critical_financial_or_identity" if difference["field"] in {
                "Amount", "Quantity", "Unit Price", "Location", "Property Abbreviation",
                "ai_excluded_paid_rows", "ai_row_identity_evidence",
            } else "provenance_or_explanation",
            "cold_candidate": difference.get("after"),
            "prior_candidate": difference.get("before"),
            "candidate_list": difference.get("values"),
            "evidence": _dump(asset),
            "verifier_status": "pending",
            "adjudication_status": "pending_human_review",
        })
    queue.sort(key=lambda item: (item["priority"] != "critical_financial_or_identity",
                                 not item["is_visual_field"], item["invoice_id"], item["line"] or 0,
                                 item["field"]))
    (output / "visual_disagreement_queue.json").write_text(
        json.dumps({"schema_version": "targeted-visual-adjudication-queue/1.0",
                    "batch_id": BATCH_ID, "tasks": queue}, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    result = {
        "batch_id": BATCH_ID, "invoices": len(invoices),
        "payable_rows": sum(len(invoice.rows) for invoice in invoices),
        "excluded_paid_rows": sum(len(invoice.excluded_rows) for invoice in invoices),
        "disagreement_tasks": len(queue),
        "visual_disagreement_tasks": sum(item["is_visual_field"] for item in queue),
        "state": contract.state.value, "source_manifest_sha256": source_manifest_hash,
        "external_provider_calls": 0,
    }
    (output / "build_summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    bundled_poppler = Path(r"C:\poppler\bin\pdftoppm.exe")
    parser.add_argument(
        "--pdftoppm",
        default=str(bundled_poppler) if bundled_poppler.is_file() else shutil.which("pdftoppm"),
    )
    args = parser.parse_args()
    print(json.dumps(build(args.base.resolve(), args.output.resolve(), args.pdftoppm), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
