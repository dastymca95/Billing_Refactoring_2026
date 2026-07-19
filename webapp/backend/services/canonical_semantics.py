"""Stable, vendor-neutral canonical concepts for accounting semantics.

This module normalizes meaning only.  It never selects or writes a GL.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable

from .accounting_artifact_cache import dependency_versions


CANONICAL_SEMANTICS_VERSION = "canonical-line-concepts/1.0"


@dataclass(frozen=True)
class CanonicalConcept:
    concept_id: str
    line_family: str
    trade_family: str
    work_mode: str
    phrases: tuple[str, ...]


@dataclass(frozen=True)
class CanonicalConceptResolution:
    concept_id: str | None
    line_family: str
    trade_family: str
    work_mode: str
    matched_phrase: str | None
    version: str = CANONICAL_SEMANTICS_VERSION

    @property
    def cacheable(self) -> bool:
        return bool(self.concept_id and self.line_family != "unknown" and self.work_mode != "unknown")


_CONCEPTS = (
    CanonicalConcept("surface.countertop_refinishing", "labor_service", "countertop",
                     "labor_service", ("kitchen counter", "bath counter", "countertop", "counter top")),
    CanonicalConcept("surface.bathtub_refinishing", "labor_service", "tub_refinishing",
                     "labor_service", ("bath tub", "bathtub", "tub refinishing")),
    CanonicalConcept("surface.wall_tile_refinishing", "labor_service", "tub_refinishing",
                     "labor_service", ("wall tile", "tile refinishing")),
    CanonicalConcept("surface.window_sill_refinishing", "labor_service", "tub_refinishing",
                     "labor_service", ("window sill", "window sills", "window seal")),
    CanonicalConcept("surface.tub_mat_work", "labor_service", "tub_refinishing",
                     "labor_service", ("tub mat", "bath mat", "tubmat")),
    CanonicalConcept("lock.key_by_code", "labor_service", "locksmith",
                     "labor_service", ("key by code", "key cutting", "duplicate key")),
)


def normalize_literal(value: str | None) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold())
    return " ".join(text.split())


def resolve_canonical_concept(
    raw_text: str | None,
    *,
    line_family: str = "unknown",
    trade_family: str = "unknown",
    work_mode: str = "unknown",
) -> CanonicalConceptResolution:
    normalized = normalize_literal(raw_text)
    for concept in _CONCEPTS:
        for phrase in sorted(concept.phrases, key=len, reverse=True):
            if normalize_literal(phrase) in normalized:
                return CanonicalConceptResolution(
                    concept.concept_id, concept.line_family, concept.trade_family,
                    concept.work_mode, phrase,
                )
    if line_family != "unknown" and work_mode != "unknown":
        stable = ".".join(normalize_literal(part).replace(" ", "_")
                          for part in (line_family, trade_family, work_mode))
        return CanonicalConceptResolution(
            f"classified.{stable}", line_family, trade_family, work_mode, None,
        )
    return CanonicalConceptResolution(None, line_family, trade_family, work_mode, None)


def tenant_accounting_context_fingerprint(
    dependencies: dict[str, str] | None = None, *, tenant_id: str | None = None,
) -> str:
    if dependencies is None and tenant_id is None:
        from .tenant_accounting_policies import default_tenant_id
        tenant_id = default_tenant_id()
    payload = dependencies or dependency_versions(tenant_id)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def semantic_candidate_cache_key(
    concepts: Iterable[CanonicalConceptResolution],
    *,
    candidate_gl_codes: Iterable[Iterable[str]],
    provider: str,
    profile_id: str,
    model_id: str,
    tenant_context_fingerprint: str,
    version: str,
) -> str | None:
    resolved = list(concepts)
    if not resolved or any(not item.cacheable for item in resolved):
        return None
    payload: dict[str, Any] = {
        "cache_contract": "canonical-semantic-candidates/1.0",
        "semantic_version": CANONICAL_SEMANTICS_VERSION,
        "reasoning_version": version,
        "provider": provider,
        "profile_id": profile_id,
        "model_id": model_id,
        "tenant_context": tenant_context_fingerprint,
        "lines": [
            {
                "ordinal": index,
                "canonical_concept": item.concept_id,
                "line_family": item.line_family,
                "trade_family": item.trade_family,
                "work_mode": item.work_mode,
                "allowed_candidate_gl_codes": sorted({str(code) for code in codes}),
            }
            for index, (item, codes) in enumerate(zip(resolved, candidate_gl_codes, strict=True))
        ],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


__all__ = [
    "CANONICAL_SEMANTICS_VERSION", "CanonicalConceptResolution", "normalize_literal",
    "resolve_canonical_concept", "semantic_candidate_cache_key",
    "tenant_accounting_context_fingerprint",
]
