"""Content-addressed observed-facts cache for non-deterministic documents.

Only exact canonical raster equality may reuse facts.  The cache contains no
normalized accounting result, selected GL, readiness status, or export state.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .. import settings
from . import batch_store
from .local_processing_guard import serialized_local_document_operation
from .accounting_contracts import (
    DateFieldProvenance,
    DocumentFacts,
    EvidenceReference,
    ExcludedPaidRowFacts,
    HandwrittenRowIdentityEvidence,
    LineItemFacts,
)


PAGE_FACTS_CACHE_SCHEMA_VERSION = "page-facts-cache/1.0"
VISUAL_HASH_VERSION = "exact-rgb-144dpi/1.0"
EXTRACTOR_VERSION = "unknown-invoice-extractor/3.0"
PROMPT_VERSION = "observed-invoice-facts/3.0"
PREPROCESSING_VERSION = "document-ingestion-and-visual-crops/3.0"
NORMALIZED_FACTS_SCHEMA_VERSION = "normalized-document-facts/1.0"
_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT: dict[str, threading.Event] = {}
_MANIFEST_LOCK = threading.Lock()


class VisualPageIdentity(BaseModel):
    visual_sha256: str
    width_points: float
    height_points: float
    rotation: int
    raster_width: int
    raster_height: int
    colorspace_components: int
    visual_hash_version: str = VISUAL_HASH_VERSION


class PageFactsCacheContext(BaseModel):
    extractor_version: str = EXTRACTOR_VERSION
    schema_version: str = "document-facts/1.0"
    prompt_version: str = PROMPT_VERSION
    provider: str
    profile_id: str
    model: str
    preprocessing_version: str = PREPROCESSING_VERSION
    reference_fingerprint: str = ""


class CachedPageFactsArtifact(BaseModel):
    cache_schema_version: str = PAGE_FACTS_CACHE_SCHEMA_VERSION
    cache_key: str
    page_identities: list[VisualPageIdentity]
    context: PageFactsCacheContext
    observed_payload: dict[str, Any]
    document_facts: DocumentFacts
    handwritten_identity_evidence: list[HandwrittenRowIdentityEvidence] = Field(default_factory=list)
    excluded_paid_rows: list[ExcludedPaidRowFacts] = Field(default_factory=list)
    date_provenance: list[DateFieldProvenance] = Field(default_factory=list)
    created_at: datetime


class DocumentFactsManifestEntry(BaseModel):
    group_index: int
    page_numbers: list[int]
    artifact_cache_key: str


class DocumentFactsManifest(BaseModel):
    schema_version: str = "document-facts-manifest/1.0"
    source_sha256: str
    source_size: int
    expected_group_count: int | None = None
    complete: bool = False
    entries: list[DocumentFactsManifestEntry] = Field(default_factory=list)
    updated_at: datetime


def _dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json") if hasattr(model, "model_dump") else model.dict()


@serialized_local_document_operation
def exact_visual_page_identity(
    *, batch_id: str, filename: str, page_number: int
) -> VisualPageIdentity:
    """Render a bounded canonical raster solely to compute exact identity.

    This happens before any high-resolution/detail rendering or base64 work.
    Approximate/perceptual hashes are intentionally not produced.
    """

    input_dir = batch_store.get_input_dir(batch_id).resolve()
    safe_name = Path(filename or "").name
    path = (input_dir / safe_name).resolve()
    if input_dir not in path.parents or not path.is_file():
        raise FileNotFoundError("Source page is unavailable.")
    if path.suffix.lower() != ".pdf":
        if int(page_number) != 1:
            raise ValueError("Image documents contain one visual page.")
        from PIL import Image  # type: ignore

        with Image.open(path) as image:
            rgb = image.convert("RGB")
            digest = hashlib.sha256()
            digest.update(VISUAL_HASH_VERSION.encode("ascii"))
            digest.update(f"{rgb.width}:{rgb.height}:3".encode("ascii"))
            digest.update(rgb.tobytes())
            dpi = image.info.get("dpi") or (72, 72)
            dpi_x = float(dpi[0] or 72)
            dpi_y = float(dpi[1] or 72)
            return VisualPageIdentity(
                visual_sha256=digest.hexdigest(),
                width_points=round(float(rgb.width) * 72.0 / dpi_x, 4),
                height_points=round(float(rgb.height) * 72.0 / dpi_y, 4),
                rotation=0,
                raster_width=int(rgb.width),
                raster_height=int(rgb.height),
                colorspace_components=3,
            )

    try:
        import fitz  # type: ignore

        with fitz.open(str(path)) as document:
            if not 1 <= int(page_number) <= len(document):
                raise ValueError("Source page is unavailable.")
            page = document[int(page_number) - 1]
            matrix = fitz.Matrix(2.0, 2.0)  # 144 DPI, exact RGB identity only.
            pixmap = page.get_pixmap(matrix=matrix, alpha=False, colorspace=fitz.csRGB)
            samples = pixmap.samples
            width = int(pixmap.width)
            height = int(pixmap.height)
            components = int(pixmap.n)
            width_points = float(page.rect.width)
            height_points = float(page.rect.height)
            rotation = int(page.rotation or 0)
    except ImportError:
        import pypdfium2 as pdfium  # type: ignore

        document = pdfium.PdfDocument(str(path))
        try:
            if not 1 <= int(page_number) <= len(document):
                raise ValueError("Source page is unavailable.")
            page = document[int(page_number) - 1]
            width_points, height_points = page.get_size()
            rotation = int(page.get_rotation() or 0)
            image = page.render(scale=2.0, rotation=0).to_pil().convert("RGB")
            samples = image.tobytes()
            width = int(image.width)
            height = int(image.height)
            components = 3
        finally:
            document.close()

    digest = hashlib.sha256()
    digest.update(VISUAL_HASH_VERSION.encode("ascii"))
    digest.update(f"{width}:{height}:{components}".encode("ascii"))
    digest.update(samples)
    return VisualPageIdentity(
        visual_sha256=digest.hexdigest(),
        width_points=round(float(width_points), 4),
        height_points=round(float(height_points), 4),
        rotation=rotation,
        raster_width=width,
        raster_height=height,
        colorspace_components=components,
    )


def reference_fingerprint(*values: Any) -> str:
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def cache_key(
    identities: list[VisualPageIdentity], context: PageFactsCacheContext
) -> str:
    payload = {
        "cache_schema_version": PAGE_FACTS_CACHE_SCHEMA_VERSION,
        "pages": [_dump(item) for item in identities],
        "context": _dump(context),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _path(key: str) -> Path:
    return settings.WEBAPP_DATA_ROOT / "cache" / "document_facts" / f"{key}.json"


def _source_identity(batch_id: str, filename: str) -> tuple[str, int]:
    input_dir = batch_store.get_input_dir(batch_id).resolve()
    path = (input_dir / Path(filename or "").name).resolve()
    if input_dir not in path.parents or not path.is_file():
        raise FileNotFoundError("Source document is unavailable.")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _manifest_path(source_sha256: str) -> Path:
    return (
        settings.WEBAPP_DATA_ROOT
        / "cache"
        / "document_facts_manifest"
        / f"{source_sha256}.json"
    )


def normalization_dependency_fingerprint() -> str:
    paths = [
        settings.PROJECT_ROOT / "Vendors" / "Vendor List.csv",
        settings.PROJECT_ROOT / "Properties" / "Properties.csv",
        settings.PROJECT_ROOT / "Properties" / "Unit Info Clean.csv",
        settings.GENERAL_LEDGER_REFERENCE,
        settings.PROJECT_ROOT / "config" / "canonical_rules.yaml",
        settings.PROJECT_ROOT / "config" / "ai_learned_mappings.yaml",
        settings.PROJECT_ROOT / "config" / "tenant_document_policies.yaml",
        settings.PROJECT_ROOT / "config" / "invoice_format_rules.yaml",
        settings.PROJECT_ROOT / "config" / "vendor_rules_index.yaml",
    ]
    vendor_dir = settings.PROJECT_ROOT / "config" / "vendors"
    if vendor_dir.is_dir():
        paths.extend(sorted(vendor_dir.glob("*.yaml")))
    signature = [
        (str(path.relative_to(settings.PROJECT_ROOT)), path.stat().st_mtime_ns, path.stat().st_size)
        if path.is_file() else (str(path), 0, 0)
        for path in paths
    ]
    # Normalization resolves vendor, property/unit and accounting identities
    # from the tenant's active ResMan snapshots. A cache produced in an empty
    # isolated runtime must not survive after those snapshots are provisioned.
    signature.append(("resman_context", _active_resman_context_fingerprints()))
    encoded = json.dumps(signature, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _active_resman_context_fingerprints() -> dict[str, str]:
    """Return immutable active-snapshot hashes, never raw tenant records."""
    try:
        from . import resman_context_data
        from .tenant_accounting_policies import default_tenant_id

        tenant_id = default_tenant_id()
        return {
            dataset.value: (
                resman_context_data.current_snapshot_fingerprint(tenant_id, dataset)
                or "missing"
            )
            for dataset in resman_context_data.DatasetKind
        }
    except Exception:
        # Fail closed: an unavailable context has a distinct identity and can
        # never masquerade as a populated runtime snapshot.
        return {"status": "unavailable"}


def normalized_facts_cache_key(
    artifact_cache_key: str,
    dependency_fingerprint: str | None = None,
) -> str:
    encoded = json.dumps({
        "schema_version": NORMALIZED_FACTS_SCHEMA_VERSION,
        "observed_artifact": artifact_cache_key,
        "dependencies": dependency_fingerprint or normalization_dependency_fingerprint(),
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalized_path(key: str) -> Path:
    return (
        settings.WEBAPP_DATA_ROOT
        / "cache"
        / "normalized_document_facts"
        / f"{key}.json"
    )


def load_normalized_facts(
    artifact_cache_key: str,
) -> dict[str, Any] | None:
    key = normalized_facts_cache_key(artifact_cache_key)
    try:
        envelope = json.loads(_normalized_path(key).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if (
        envelope.get("schema_version") != NORMALIZED_FACTS_SCHEMA_VERSION
        or envelope.get("cache_key") != key
        or envelope.get("observed_artifact") != artifact_cache_key
        or not isinstance(envelope.get("normalized"), dict)
    ):
        return None
    return envelope["normalized"]


def save_normalized_facts(
    artifact_cache_key: str,
    normalized: dict[str, Any],
) -> None:
    key = normalized_facts_cache_key(artifact_cache_key)
    sanitized = json.loads(json.dumps(normalized, default=str))
    for forbidden in (
        "accounting_decision", "accounting_readiness", "selected_gl", "export_allowed"
    ):
        sanitized.pop(forbidden, None)
    for item in sanitized.get("line_items") or []:
        if isinstance(item, dict):
            item.pop("accounting_decision", None)
            item.pop("selected_gl", None)
    envelope = {
        "schema_version": NORMALIZED_FACTS_SCHEMA_VERSION,
        "cache_key": key,
        "observed_artifact": artifact_cache_key,
        "dependency_fingerprint": normalization_dependency_fingerprint(),
        "normalized": sanitized,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    path = _normalized_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
    temporary.replace(path)


def register_document_artifact(
    *,
    batch_id: str,
    filename: str,
    group_index: int,
    page_numbers: list[int],
    artifact: CachedPageFactsArtifact,
) -> None:
    source_sha256, source_size = _source_identity(batch_id, filename)
    path = _manifest_path(source_sha256)
    with _MANIFEST_LOCK:
        try:
            manifest = DocumentFactsManifest.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError, TypeError):
            manifest = DocumentFactsManifest(
                source_sha256=source_sha256,
                source_size=source_size,
                updated_at=datetime.now(timezone.utc),
            )
        entries = {
            int(entry.group_index): entry for entry in manifest.entries
        }
        entries[max(1, int(group_index))] = DocumentFactsManifestEntry(
            group_index=max(1, int(group_index)),
            page_numbers=[max(1, int(value)) for value in page_numbers],
            artifact_cache_key=artifact.cache_key,
        )
        manifest.entries = [entries[key] for key in sorted(entries)]
        manifest.source_size = source_size
        manifest.updated_at = datetime.now(timezone.utc)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(path)


def finalize_document_manifest(
    *, batch_id: str, filename: str, expected_group_count: int
) -> None:
    source_sha256, source_size = _source_identity(batch_id, filename)
    path = _manifest_path(source_sha256)
    with _MANIFEST_LOCK:
        try:
            manifest = DocumentFactsManifest.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError, TypeError):
            return
        expected = max(1, int(expected_group_count))
        actual = {int(entry.group_index) for entry in manifest.entries}
        manifest.expected_group_count = expected
        manifest.complete = actual == set(range(1, expected + 1))
        manifest.source_size = source_size
        manifest.updated_at = datetime.now(timezone.utc)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(path)


def load_document_manifest(
    *,
    batch_id: str,
    filename: str,
    allowed_provider_models: set[tuple[str, str]],
) -> list[tuple[DocumentFactsManifestEntry, CachedPageFactsArtifact]]:
    source_sha256, source_size = _source_identity(batch_id, filename)
    try:
        manifest = DocumentFactsManifest.model_validate_json(
            _manifest_path(source_sha256).read_text(encoding="utf-8")
        )
    except (OSError, ValueError, TypeError):
        return []
    expected = int(manifest.expected_group_count or 0)
    if (
        not manifest.complete
        or manifest.source_sha256 != source_sha256
        or int(manifest.source_size) != source_size
        or expected < 1
        or {int(entry.group_index) for entry in manifest.entries}
        != set(range(1, expected + 1))
    ):
        return []
    loaded: list[tuple[DocumentFactsManifestEntry, CachedPageFactsArtifact]] = []
    for entry in sorted(manifest.entries, key=lambda item: item.group_index):
        try:
            artifact = CachedPageFactsArtifact.model_validate_json(
                _path(entry.artifact_cache_key).read_text(encoding="utf-8")
            )
        except (OSError, ValueError, TypeError):
            return []
        context = artifact.context
        provider_model = (context.provider.strip().lower(), context.model.strip())
        if (
            context.extractor_version != EXTRACTOR_VERSION
            or context.schema_version != "document-facts/1.0"
            or context.prompt_version != PROMPT_VERSION
            or context.preprocessing_version != PREPROCESSING_VERSION
            or artifact.observed_payload.get("_migrated_from_validated_result")
            or provider_model not in allowed_provider_models
        ):
            return []
        loaded.append((entry, artifact))
    return loaded


def load(
    identities: list[VisualPageIdentity], context: PageFactsCacheContext
) -> CachedPageFactsArtifact | None:
    key = cache_key(identities, context)
    try:
        payload = json.loads(_path(key).read_text(encoding="utf-8"))
        artifact = CachedPageFactsArtifact(**payload)
    except (OSError, ValueError, TypeError):
        return None
    if artifact.cache_key != key:
        return None
    # Defense in depth: every exact identity and every versioned dependency
    # must still match the lookup request.
    if [_dump(item) for item in artifact.page_identities] != [_dump(item) for item in identities]:
        return None
    if _dump(artifact.context) != _dump(context):
        return None
    return artifact


def load_compatible_exact_observed(
    identities: list[VisualPageIdentity],
    context: PageFactsCacheContext,
) -> CachedPageFactsArtifact | None:
    """Migrate only provider-observed facts from an older reference key.

    Reference catalogs never change page facts. All visual identities and
    extraction contract versions still have to match exactly. Artifacts
    reconstructed from normalized results are deliberately ineligible.
    """
    root = _path("probe").parent
    if not root.is_dir():
        return None
    expected_identities = [_dump(item) for item in identities]
    required_context = _dump(context)
    compatible: list[CachedPageFactsArtifact] = []
    for path in sorted(root.glob("*.json")):
        try:
            artifact = CachedPageFactsArtifact.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError, TypeError):
            continue
        if artifact.observed_payload.get("_migrated_from_validated_result"):
            continue
        if [_dump(item) for item in artifact.page_identities] != expected_identities:
            continue
        candidate_context = _dump(artifact.context)
        if any(
            candidate_context.get(field) != required_context.get(field)
            for field in (
                "extractor_version",
                "schema_version",
                "prompt_version",
                "provider",
                "profile_id",
                "model",
                "preprocessing_version",
            )
        ):
            continue
        compatible.append(artifact)
    if not compatible:
        return None
    selected = max(compatible, key=_observed_artifact_quality)
    return save(
        identities=identities,
        context=context,
        observed_payload=selected.observed_payload,
    )


def _observed_artifact_quality(
    artifact: CachedPageFactsArtifact,
) -> tuple[int, int, int, int, float, int, str]:
    payload = artifact.observed_payload
    items = [item for item in payload.get("line_items") or [] if isinstance(item, dict)]
    required = sum(bool(str(payload.get(field) or "").strip()) for field in (
        "vendor_name", "invoice_number", "service_date", "total_amount"
    ))
    parseable_dates = 0
    for field in ("invoice_date", "service_date", "due_date"):
        value = str(payload.get(field) or "").strip()
        if not value:
            continue
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
            try:
                datetime.strptime(value, fmt)
                parseable_dates += 1
                break
            except ValueError:
                continue
    total = _decimal(payload.get("total_amount")) or Decimal("0")
    line_total = sum(
        ((_decimal(item.get("amount")) or Decimal("0")) for item in items),
        Decimal("0"),
    )
    components = sum(
        ((_decimal(payload.get(field)) or Decimal("0")) for field in (
            "tax_amount", "shipping_amount", "fees_amount"
        )),
        Decimal("0"),
    )
    reconciles = int(bool(total) and abs(total - line_total - components) <= Decimal("0.01"))
    visual_complete = int(
        str(payload.get("visual_extraction_status") or "").lower() == "complete"
        and not payload.get("unresolved_visual_regions")
    )
    try:
        confidence = float(payload.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    warnings = len(payload.get("warnings") or [])
    return (
        reconciles,
        required,
        parseable_dates,
        visual_complete,
        confidence,
        -warnings,
        artifact.created_at.isoformat(),
    )


def load_or_reserve(
    identities: list[VisualPageIdentity], context: PageFactsCacheContext
) -> tuple[CachedPageFactsArtifact | None, bool]:
    """Single-flight lookup so exact duplicate pages share one cold request.

    Returns ``(artifact, owns_reservation)``. A reservation owner must call
    ``release_reservation`` on failure; successful ``save`` releases it.
    """
    key = cache_key(identities, context)
    while True:
        artifact = load(identities, context)
        if artifact is not None:
            return artifact, False
        with _INFLIGHT_LOCK:
            event = _INFLIGHT.get(key)
            if event is None:
                _INFLIGHT[key] = threading.Event()
                return None, True
        # A real visual provider call may take several minutes. Waiting does
        # not consume a worker's provider semaphore and never authorizes an
        # approximate match.
        if event.wait(timeout=300):
            continue
        # Recover from an interrupted owner without leaving the batch stuck.
        with _INFLIGHT_LOCK:
            if _INFLIGHT.get(key) is event:
                _INFLIGHT.pop(key, None)


def seed_from_persisted_result(
    *,
    batch_id: str,
    source_file: str,
    source_page: int,
    identities: list[VisualPageIdentity],
    context: PageFactsCacheContext,
) -> CachedPageFactsArtifact | None:
    """Migrate a validated batch-local result into the exact facts cache.

    The adapter is allowed only when the persisted result is newer than the
    immutable source and names the exact source file/page. It copies source
    evidence and typed provenance, never AccountingDecision or readiness.
    """
    try:
        input_path = batch_store.get_input_dir(batch_id) / Path(source_file).name
        result_path = (
            settings.BATCHES_ROOT / batch_id / "processed" / "_webapp_result.json"
        )
        if not input_path.is_file() or not result_path.is_file():
            return None
        if result_path.stat().st_mtime_ns < input_path.stat().st_mtime_ns:
            return None
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    invoice = next((
        item for item in result.get("all_invoices") or []
        if isinstance(item, dict)
        and Path(str(item.get("source_file") or "")).name == Path(source_file).name
        and int(item.get("source_page") or 1) == int(source_page)
    ), None)
    if invoice is None:
        # A batch may retain one canonical invoice while listing exact visual
        # duplicates under other filenames. Reuse is still authorized only by
        # full exact raster identity and matching page geometry/version.
        for candidate in result.get("all_invoices") or []:
            if not isinstance(candidate, dict):
                continue
            candidate_name = Path(str(candidate.get("source_file") or "")).name
            candidate_page = int(candidate.get("source_page") or 1)
            candidate_path = batch_store.get_input_dir(batch_id) / candidate_name
            if not candidate_path.is_file() or result_path.stat().st_mtime_ns < candidate_path.stat().st_mtime_ns:
                continue
            try:
                candidate_identities = [
                    exact_visual_page_identity(
                        batch_id=batch_id,
                        filename=candidate_name,
                        page_number=candidate_page + offset,
                    )
                    for offset in range(len(identities))
                ]
            except (OSError, ValueError, IndexError):
                continue
            if [_dump(item) for item in candidate_identities] == [_dump(item) for item in identities]:
                invoice = candidate
                break
    if not isinstance(invoice, dict) or not list(invoice.get("rows") or []):
        return None
    summary = invoice.get("validation_summary") or {}
    if summary.get("total_reconciliation_passed") is False:
        return None
    rows = [row for row in invoice.get("rows") or [] if isinstance(row, dict)]
    first_meta = rows[0].get("_meta") if isinstance(rows[0].get("_meta"), dict) else {}
    persisted_date_provenance = list(first_meta.get("ai_date_provenance") or [])
    provenance_by_field = {
        str(item.get("field") or ""): item
        for item in persisted_date_provenance
        if isinstance(item, dict)
    }
    invoice_date_provenance = provenance_by_field.get("invoice_date") or {}
    due_date_provenance = provenance_by_field.get("due_date") or {}
    # A normalized date is not necessarily a visually observed fact. Rebuild
    # only the observed side of the contract so tenant policy can deterministically
    # derive the same normalized value and preserve its inference blocker.
    observed_invoice_date = (
        invoice_date_provenance.get("raw_value")
        if invoice_date_provenance.get("provenance") == "document_observed"
        else ""
    )
    observed_due_date = (
        due_date_provenance.get("raw_value")
        if due_date_provenance.get("provenance") == "document_observed"
        else ""
    )
    line_items: list[dict[str, Any]] = []
    for row in rows:
        meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
        line_items.append({
            "source_page": int(meta.get("source_page") or source_page),
            "section_header": meta.get("ai_line_section_header") or "",
            "row_label": meta.get("ai_line_row_label") or "",
            "location_candidate": meta.get("ai_line_location_candidate") or row.get("Location") or "",
            "activity": meta.get("ai_line_activity") or "",
            "description": meta.get("ai_source_line_description") or row.get("Line Item Description") or "",
            "raw_description": meta.get("ai_source_line_description") or "",
            "normalized_description": meta.get("normalized_source_description") or "",
            "generated_description": meta.get("ai_generated_description") or row.get("Line Item Description") or "",
            "quantity": row.get("Quantity"),
            "unit_price": row.get("Unit Price"),
            "amount": row.get("Amount"),
            "gl_account_candidate": "",
            "expense_type": "General",
            "is_replacement_reserve": False,
            "confidence": meta.get("ai_confidence"),
            "reason": "migrated_exact_source_evidence",
        })
    payload = {
        "vendor_name": rows[0].get("Vendor"),
        "invoice_number": invoice.get("invoice_number") or rows[0].get("Invoice Number"),
        "invoice_date": observed_invoice_date,
        "service_date": first_meta.get("ai_service_date"),
        "due_date": observed_due_date,
        "due_date_text": first_meta.get("ai_due_date_text"),
        "payment_terms": first_meta.get("ai_payment_terms"),
        "bill_or_credit": rows[0].get("Bill or Credit") or "Bill",
        "account_number": invoice.get("account_number") or "",
        "service_address": first_meta.get("ai_service_address") or "",
        "address_role": first_meta.get("ai_address_role") or "unknown",
        "location_candidate": first_meta.get("ai_line_location_candidate") or "",
        "property_candidate": first_meta.get("ai_raw_property_candidate") or first_meta.get("ai_property_candidate") or "",
        "property_abbreviation": rows[0].get("Property Abbreviation") or "",
        "invoice_description": rows[0].get("Invoice Description") or "",
        "line_items": line_items,
        "excluded_paid_rows": list(first_meta.get("ai_excluded_paid_rows") or []),
        "subtotal": (invoice.get("validation_summary") or {}).get("reconciled_total") or invoice.get("total_amount"),
        "tax_amount": 0,
        "shipping_amount": 0,
        "fees_amount": 0,
        "total_amount": invoice.get("total_amount"),
        "confidence": invoice.get("confidence"),
        "warnings": list(first_meta.get("ai_warnings") or []),
        "needs_manual_review": bool(invoice.get("manual_review_codes")),
        "visual_extraction_status": (
            "needs_confirmation"
            if first_meta.get("ai_unresolved_visual_field_candidates")
            or first_meta.get("ai_row_identity_verification", {}).get(
                "payable_needs_confirmation"
            )
            else "complete"
        ),
        "unresolved_visual_regions": list(
            first_meta.get("ai_unresolved_visual_field_candidates") or []
        ),
        "page_reconciliations": [],
        "vision_candidates": [],
        "date_provenance": persisted_date_provenance,
        "_handwritten_row_identities": list(first_meta.get("ai_handwritten_row_identities") or []),
        "_row_identity_verification": dict(
            first_meta.get("ai_row_identity_verification") or {}
        ),
        "_critical_header_verification": dict(
            first_meta.get("ai_critical_header_verification") or {}
        ),
        "_provider_profile_id": context.profile_id,
        "_provider_name": context.provider,
        "_provider_model_id": context.model,
        "_migrated_from_validated_result": True,
    }
    return save(
        identities=identities,
        context=context,
        observed_payload=payload,
    )


def release_reservation(
    identities: list[VisualPageIdentity], context: PageFactsCacheContext
) -> None:
    key = cache_key(identities, context)
    with _INFLIGHT_LOCK:
        event = _INFLIGHT.pop(key, None)
    if event is not None:
        event.set()


def save(
    *,
    identities: list[VisualPageIdentity],
    context: PageFactsCacheContext,
    observed_payload: dict[str, Any],
) -> CachedPageFactsArtifact:
    key = cache_key(identities, context)
    sanitized = json.loads(json.dumps(observed_payload, default=str))
    for source_specific in (
        "_source_file", "_source_page", "_document_text", "_document_candidate"
    ):
        sanitized.pop(source_specific, None)
    for accounting_field in (
        "accounting_decision",
        "accounting_readiness",
        "selected_gl",
        "export_allowed",
    ):
        sanitized.pop(accounting_field, None)
    # Resolved tenant/accounting identities are recomputed from raw candidates
    # and current references; they are not visual source facts.
    sanitized.pop("property_abbreviation", None)
    for item in sanitized.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        # The visual provider is not an accounting authority. Any legacy GL
        # suggestion is discarded before persistence and must be regenerated
        # as a candidate by the current accounting pipeline.
        for accounting_field in (
            "gl_account",
            "gl_account_candidate",
            "selected_gl",
            "gl_candidates",
            "accounting_decision",
        ):
            item.pop(accounting_field, None)
    artifact = CachedPageFactsArtifact(
        cache_key=key,
        page_identities=identities,
        context=context,
        observed_payload=sanitized,
        document_facts=_document_facts(sanitized, identities, context),
        handwritten_identity_evidence=_validated_list(
            HandwrittenRowIdentityEvidence,
            sanitized.get("handwritten_row_identities")
            or sanitized.get("_handwritten_row_identities")
            or [],
        ),
        excluded_paid_rows=_validated_list(
            ExcludedPaidRowFacts, sanitized.get("excluded_paid_rows") or []
        ),
        date_provenance=_validated_list(
            DateFieldProvenance, sanitized.get("date_provenance") or []
        ),
        created_at=datetime.now(timezone.utc),
    )
    path = _path(key)
    tmp = path.with_suffix(".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(_dump(artifact), sort_keys=True, ensure_ascii=True), encoding="utf-8")
    tmp.replace(path)
    release_reservation(identities, context)
    return artifact


def _validated_list(model: type[BaseModel], values: list[Any]) -> list[Any]:
    result = []
    for value in values:
        try:
            result.append(model(**value) if isinstance(value, dict) else model.model_validate(value))
        except (TypeError, ValueError):
            continue
    return result


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(",", "").replace("$", "").strip())
    except (InvalidOperation, ValueError):
        return None


def _date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    for pattern in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def _document_facts(
    payload: dict[str, Any],
    identities: list[VisualPageIdentity],
    context: PageFactsCacheContext,
) -> DocumentFacts:
    document_id = "visual:" + hashlib.sha256(
        "|".join(item.visual_sha256 for item in identities).encode("ascii")
    ).hexdigest()
    lines: list[LineItemFacts] = []
    for index, item in enumerate(payload.get("line_items") or [], start=1):
        if not isinstance(item, dict):
            continue
        raw_description = item.get("source_line_description") or item.get("raw_description") or item.get("description")
        lines.append(LineItemFacts(
            line_item_id=str(item.get("line_item_id") or index),
            raw_activity=item.get("activity"),
            raw_description=raw_description,
            normalized_activity=item.get("normalized_activity"),
            normalized_description=item.get("normalized_source_description"),
            generated_description=item.get("generated_item_description"),
            quantity=_decimal(item.get("quantity")),
            unit_price=_decimal(item.get("unit_price")),
            amount=_decimal(item.get("amount")),
            tax=_decimal(item.get("tax")),
            detected_location=item.get("location") or item.get("location_candidate"),
            evidence=[EvidenceReference(
                document_id=document_id,
                page=int(item.get("source_page") or 1),
                text=str(raw_description) if raw_description not in (None, "") else None,
                source_type="visual_page",
                extraction_method=context.extractor_version,
                confidence=float(item.get("confidence")) if item.get("confidence") is not None else None,
            )],
        ))
    return DocumentFacts(
        document_id=document_id,
        invoice_id=str(payload.get("invoice_number") or document_id),
        vendor_candidate=payload.get("vendor_name") or payload.get("vendor_candidate"),
        invoice_number=payload.get("invoice_number"),
        invoice_date=_date(payload.get("invoice_date")),
        due_date=_date(payload.get("due_date")),
        service_address=payload.get("service_address"),
        property_candidate=payload.get("property_candidate") or payload.get("property_abbreviation"),
        total_amount=_decimal(payload.get("total_amount")),
        document_family_candidate=payload.get("document_family_candidate") or payload.get("category"),
        line_items=lines,
        extraction_route=context.profile_id,
        extraction_model=context.model,
        evidence=[EvidenceReference(
            document_id=document_id,
            page=index + 1,
            source_type="visual_page",
            extraction_method=context.extractor_version,
        ) for index, _ in enumerate(identities)],
    )


__all__ = [
    "CachedPageFactsArtifact",
    "EXTRACTOR_VERSION",
    "PAGE_FACTS_CACHE_SCHEMA_VERSION",
    "PREPROCESSING_VERSION",
    "PROMPT_VERSION",
    "PageFactsCacheContext",
    "VisualPageIdentity",
    "cache_key",
    "exact_visual_page_identity",
    "load",
    "load_compatible_exact_observed",
    "load_document_manifest",
    "register_document_artifact",
    "finalize_document_manifest",
    "normalization_dependency_fingerprint",
    "normalized_facts_cache_key",
    "load_normalized_facts",
    "save_normalized_facts",
    "load_or_reserve",
    "reference_fingerprint",
    "release_reservation",
    "seed_from_persisted_result",
    "save",
]
