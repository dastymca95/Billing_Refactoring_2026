"""The sole authority that converts GL candidates into selected GL decisions."""

from __future__ import annotations

import hashlib
import json
from typing import Iterable

from .accounting_contracts import AccountingDecision, DocumentFacts, GLCandidate, SemanticClassification, model_dict
from .gl_catalog import load_decision_config, load_gl_catalog


class AccountingDecisionEngine:
    def decide(self, facts: DocumentFacts, semantics: SemanticClassification,
               gl_catalog_metadata: dict, candidates: Iterable[GLCandidate],
               client_accounting_policy: dict | None = None) -> AccountingDecision:
        del client_accounting_policy
        config = load_decision_config()
        weights = config.get("ranking_weights") or {}
        thresholds = config.get("thresholds") or {}
        catalog_version, canonical_catalog = load_gl_catalog()
        catalog = gl_catalog_metadata or canonical_catalog
        line_facts = next(item for item in facts.line_items if item.line_item_id == semantics.line_item_id)
        explicit_material = bool(semantics.work_mode == "material_purchase" or "mixed_material_and_service_indicators" in semantics.contradictions)
        explicit_service = bool(semantics.work_mode in {"labor_service", "recurring_service"} or "mixed_material_and_service_indicators" in semantics.contradictions)
        ranked: list[GLCandidate] = []
        rejected: list[dict] = []
        contradictions = list(semantics.contradictions)

        by_code: dict[str, GLCandidate] = {}
        for candidate in candidates:
            existing = by_code.get(candidate.gl_code)
            if existing is None or candidate.base_score > existing.base_score:
                by_code[candidate.gl_code] = candidate.model_copy(deep=True) if hasattr(candidate, "model_copy") else candidate.copy(deep=True)
            elif candidate.source not in existing.source:
                existing.source += f"+{candidate.source}"
                existing.base_score = max(existing.base_score, candidate.base_score)

        for code, candidate in by_code.items():
            meta = catalog.get(code)
            if not meta or not meta.payable:
                rejected.append({"gl_code": code, "reason": "GL is absent from the payable chart.", "blocking": True})
                continue
            trade_match = semantics.trade_family in meta.trade_families or meta.gl_family == semantics.trade_family
            broad_fallback = meta.gl_family in {"contract_services", "general_maintenance"} and semantics.line_family == "labor_service"
            work_match = not meta.compatible_work_modes or semantics.work_mode in meta.compatible_work_modes
            approved_current_rule = any(key in candidate.source.lower() for key in ("deterministic", "canonical", "utility_rule", "manual_approved"))
            incompatible = semantics.work_mode in meta.incompatible_work_modes
            if semantics.line_family == "labor_service" and "material_purchase" in meta.compatible_work_modes and not explicit_material:
                incompatible = True
            if semantics.line_family == "materials" and any(mode in meta.compatible_work_modes for mode in ("labor_service", "recurring_service")) and not explicit_service:
                incompatible = True
            if meta.capital_context == "capital" and not (semantics.capital_context == "capital" or "approved_deterministic_rule" in candidate.source):
                incompatible = True
            relevant = trade_match or broad_fallback or meta.gl_family == semantics.line_family or (
                meta.gl_family == "capital" and semantics.capital_context == "capital"
            ) or (
                approved_current_rule and work_match
            ) or (
                semantics.trade_family == "unknown" and meta.gl_family == "general_maintenance"
            )
            if not relevant:
                rejected.append({"gl_code": code, "gl_name": meta.gl_name, "reason": "Alternative is not semantically relevant to this line."})
                continue
            source_support = _source_support(candidate.source, weights, candidate.base_score)
            components = {
                "semantic_compatibility": float(weights.get("semantic_compatibility", 0)) if relevant and not incompatible else 0.0,
                "trade_match": float(weights.get("trade_match", 0)) if trade_match else 0.0,
                "work_mode_match": float(weights.get("work_mode_match", 0)) if work_match and not incompatible else 0.0,
                "specificity": float(weights.get("specificity", 0)) if meta.specificity == "specific" else 0.0,
                **source_support,
                "contradiction_penalty": float(weights.get("contradiction_penalty", -0.25)) if incompatible else 0.0,
            }
            candidate.score_components = {key: round(value, 4) for key, value in components.items()}
            candidate.compatibility_results = [
                {"check": "payable_chart", "passed": True},
                {"check": "semantic_relevance", "passed": relevant},
                {"check": "work_mode", "passed": work_match and not incompatible},
            ]
            if incompatible:
                candidate.negative_evidence.append({"reason": "Candidate contradicts current line-level work mode."})
                contradictions.append(f"{code}:incompatible_work_mode")
            ranked.append(candidate)

        ranked.sort(key=lambda item: (sum(item.score_components.values()), item.gl_code), reverse=True)
        safe = [item for item in ranked if not any(not result["passed"] for result in item.compatibility_results)]
        top = safe[0] if safe else None
        top_score = sum(top.score_components.values()) if top else 0.0
        second_score = sum(safe[1].score_components.values()) if len(safe) > 1 else 0.0
        margin = top_score - second_score
        minimum = float(thresholds.get("minimum_selectable_score", 0.45))
        minimum_margin = float(thresholds.get("minimum_top_1_margin", 0.08))
        selected = top if top and top_score >= minimum else None
        ambiguous = bool(selected and len(safe) > 1 and margin < minimum_margin)
        blocking = selected is None or margin < float(thresholds.get("blocking_ambiguity", 0.04))
        mixed_evidence = "mixed_material_and_service_indicators" in semantics.contradictions
        review_required = selected is None or ambiguous or blocking or mixed_evidence
        confidence = round(max(0.0, min(1.0, top_score)), 3) if selected else 0.0
        evidence = [model_dict(ref) for ref in semantics.positive_evidence]
        why = (
            f"{selected.gl_name} is the highest-scoring payable GL compatible with "
            f"{semantics.trade_family} {semantics.work_mode} evidence."
            if selected else "No payable, semantically compatible GL candidate met the configured safety threshold."
        )
        material = json.dumps({"facts": facts.document_id, "line": semantics.line_item_id,
            "semantic": model_dict(semantics), "candidates": [model_dict(item) for item in ranked]}, sort_keys=True, default=str)
        return AccountingDecision(
            decision_id=hashlib.sha256(material.encode("utf-8")).hexdigest(),
            decision_version=str(config.get("decision_version") or "accounting-decision/1.0"),
            line_item_id=semantics.line_item_id,
            selected_gl_code=selected.gl_code if selected else None,
            selected_gl_name=selected.gl_name if selected else None,
            decision_source="AccountingDecisionEngine",
            confidence=confidence, why_selected=why, evidence=evidence,
            candidates_ranked=ranked,
            rejected_alternatives=rejected,
            contradictions=sorted(set(contradictions)),
            review_required=review_required, review_blocking=blocking,
            review_reason=("Mixed material and service evidence requires review." if mixed_evidence else
                "Ambiguous GL candidates require review." if ambiguous else (None if selected else "No safe GL candidate.")),
            catalog_version=catalog_version, semantic_version=semantics.semantic_version,
        )


def _source_support(source: str, weights: dict, base_score: float) -> dict[str, float]:
    source_key = source.lower()
    return {
        "deterministic_rule_support": float(weights.get("deterministic_rule_support", 0)) * base_score if any(key in source_key for key in ("deterministic", "canonical", "utility", "manual")) else 0.0,
        "historical_support": float(weights.get("historical_support", 0)) * base_score if "historical" in source_key or "learned" in source_key else 0.0,
        "vendor_support": float(weights.get("vendor_support", 0)) * base_score if "vendor" in source_key else 0.0,
        "property_policy": float(weights.get("property_policy", 0)) * base_score if "property" in source_key else 0.0,
    }


__all__ = ["AccountingDecisionEngine"]
