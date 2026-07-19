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
            manual_approved = "manual_approved" in candidate.source.lower()
            semantic_family_match = trade_match or meta.gl_family == semantics.line_family
            manual_resolution_bridge = (
                manual_approved
                and semantics.line_family in {"fee", "admin", "unknown"}
                and semantics.work_mode in {"one_time_fee", "unknown"}
                and meta.gl_family not in {"materials", "supplies", "capital"}
            )
            # A human-approved correction is valid resolution evidence for a
            # narrow work-mode taxonomy mismatch (for example a one-time legal
            # fee in a legal-services account), but never for a different
            # semantic family and never for an explicitly incompatible mode.
            work_match = (
                not meta.compatible_work_modes
                or semantics.work_mode in meta.compatible_work_modes
                or (manual_approved and semantic_family_match)
                or manual_resolution_bridge
            )
            approved_current_rule = any(key in candidate.source.lower() for key in ("deterministic", "canonical", "utility_rule", "manual_approved"))
            specific_scope_supported = _specific_scope_supported(meta, semantics, line_facts, candidate.source)
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
                manual_resolution_bridge
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
                "specificity": float(weights.get("specificity", 0)) if meta.specificity == "specific" and specific_scope_supported else 0.0,
                **source_support,
                "contradiction_penalty": float(weights.get("contradiction_penalty", -0.25)) if incompatible else 0.0,
            }
            candidate.score_components = {key: round(value, 4) for key, value in components.items()}
            candidate.compatibility_results = [
                {"check": "payable_chart", "passed": True},
                {"check": "semantic_relevance", "passed": relevant},
                {"check": "work_mode", "passed": work_match and not incompatible},
                {"check": "specific_scope_evidence", "passed": specific_scope_supported},
            ]
            if incompatible:
                candidate.negative_evidence.append({"reason": "Candidate contradicts current line-level work mode."})
                contradictions.append(f"{code}:incompatible_work_mode")
            if not specific_scope_supported:
                missing_qualifiers = _missing_scope_qualifiers(meta, line_facts)
                scope_reason = (
                    "Specific chart scope qualifier(s) "
                    f"{', '.join(item.upper() for item in missing_qualifiers)} are not present in line-bound source evidence."
                    if missing_qualifiers else
                    "Specific scope of this account is not supported by current line evidence."
                )
                candidate.negative_evidence.append({"reason": scope_reason})
                rejected.append({"gl_code": code, "gl_name": meta.gl_name,
                                 "reason": f"Not selected because {scope_reason[0].lower()}{scope_reason[1:]}"})
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
        why = _selection_explanation(selected, safe, semantics, line_facts, catalog)
        if selected:
            rejected.extend(_ranked_alternative_explanations(selected, safe, catalog))
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
        "historical_support": float(weights.get("historical_support", 0)) * base_score
        if any(key in source_key for key in ("historical", "learned", "learning")) else 0.0,
        "vendor_support": float(weights.get("vendor_support", 0)) * base_score if "vendor" in source_key else 0.0,
        "property_policy": float(weights.get("property_policy", 0)) * base_score if "property" in source_key else 0.0,
    }


def _specific_scope_supported(meta, semantics: SemanticClassification, line_facts, source: str) -> bool:
    if meta.specificity != "specific":
        return True
    source_key = source.lower()
    if "manual_approved" in source_key or "approved_deterministic_rule" in source_key:
        return True
    if ("approved_config" in meta.metadata_source
            and semantics.trade_family not in {"unknown", "admin", "general_maintenance"}
            and semantics.trade_family in meta.trade_families):
        return True
    text = _source_evidence_text(line_facts).lower().replace("_", " ")
    qualifiers = {str(item).lower() for item in getattr(meta, "scope_qualifiers", []) if str(item).strip()}
    if qualifiers and not qualifiers.issubset(set(text.replace("&", " ").split())):
        return False
    tokens = {str(token).lower() for token in meta.description_tokens if len(str(token)) >= 3}
    stemmed_tokens = {token[:-1] if token.endswith("s") and len(token) > 4 else token for token in tokens}
    if any(token in text for token in tokens | stemmed_tokens):
        # A generic trade word alone (for example paint) does not prove a
        # narrower qualifier such as exterior or carpentry.
        generic_trade = {semantics.trade_family, semantics.trade_family.rstrip("ing"), "paint" if semantics.trade_family == "painting" else ""}
        distinguishing = tokens - {item for item in generic_trade if item}
        distinguishing_stems = {token[:-1] if token.endswith("s") and len(token) > 4 else token
                                for token in distinguishing}
        if not distinguishing or any(token in text for token in distinguishing | distinguishing_stems):
            return True
    mode_tokens = {
        "material_purchase": {"supply", "supplies", "material", "materials", "parts", "tools", "merchandise"},
        "labor_service": {"labor", "service", "contract", "repair", "maintenance"},
        "recurring_service": {"service", "utility", "monthly", "recurring"},
        "one_time_fee": {"fee", "charge"},
        "renewal": {"renewal", "subscription", "membership"},
    }
    expected = mode_tokens.get(semantics.work_mode, set())
    return bool(tokens & expected or stemmed_tokens & expected)


def _missing_scope_qualifiers(meta, line_facts) -> list[str]:
    qualifiers = {str(item).lower() for item in getattr(meta, "scope_qualifiers", []) if str(item).strip()}
    if not qualifiers:
        return []
    evidence_tokens = set(_source_evidence_text(line_facts).lower().replace("&", " ").split())
    return sorted(qualifiers - evidence_tokens)


def _candidate_score(candidate: GLCandidate) -> float:
    return round(sum(candidate.score_components.values()), 4)


def _human_label(value: str) -> str:
    return value.replace("_", " ").strip()


def _source_text(line_facts) -> str:
    # Generated descriptions are intentionally excluded from decision evidence.
    return str(line_facts.raw_description or line_facts.raw_activity
               or line_facts.normalized_description or line_facts.normalized_activity or "").strip()


def _source_evidence_text(line_facts) -> str:
    scoped = " ".join(
        str(ref.text or ref.normalized_text or "")
        for ref in line_facts.evidence
        if ref.source_type in {"line_item", "line_section_header", "line_product_context"}
    )
    return " ".join(part for part in (_source_text(line_facts), scoped) if part).strip()


def _selection_explanation(selected: GLCandidate | None, safe: list[GLCandidate],
                           semantics: SemanticClassification, line_facts, catalog: dict) -> str:
    if selected is None:
        return ("No GL was selected because no payable candidate was both semantically compatible "
                "with the observed line and strong enough to meet the configured safety threshold.")
    meta = catalog[selected.gl_code]
    observed = _source_text(line_facts)
    classification = (f"{_human_label(semantics.line_family)} / {_human_label(semantics.trade_family)} "
                      f"with {_human_label(semantics.work_mode)} work mode")
    factors = [_human_label(name) for name, value in selected.score_components.items()
               if value > 0 and name not in {"historical_support", "vendor_support", "property_policy"}]
    text = (f"Selected {selected.gl_code} — {selected.gl_name} because the observed line"
            f"{f' ‘{observed}’' if observed else ''} was classified as {classification}. "
            f"This account is payable, its {_human_label(meta.gl_family)} purpose is compatible with that classification, "
            f"and the decision received current-line support from {', '.join(factors) or 'semantic compatibility'}.")
    alternatives = [candidate for candidate in safe if candidate.gl_code != selected.gl_code]
    if alternatives:
        runner_up = alternatives[0]
        advantages = [_human_label(name) for name, value in selected.score_components.items()
                      if value > runner_up.score_components.get(name, 0) and value > 0]
        text += (f" It ranked above {runner_up.gl_code} — {runner_up.gl_name} because it had stronger "
                 f"evidence from {', '.join(advantages) or 'the overall current-line comparison'} "
                 f"({_candidate_score(selected):.2f} vs {_candidate_score(runner_up):.2f}).")
    else:
        text += " No other candidate passed all payable, semantic-relevance, and work-mode checks."
    return text


def _ranked_alternative_explanations(selected: GLCandidate, safe: list[GLCandidate], catalog: dict) -> list[dict]:
    output: list[dict] = []
    for candidate in safe:
        if candidate.gl_code == selected.gl_code:
            continue
        weaker = [_human_label(name) for name, value in selected.score_components.items()
                  if value > candidate.score_components.get(name, 0) and value > 0]
        output.append({
            "gl_code": candidate.gl_code,
            "gl_name": catalog[candidate.gl_code].gl_name,
            "reason": (f"Not selected: although payable and semantically relevant, it had weaker "
                       f"evidence from {', '.join(weaker) or 'the overall current-line comparison'} than {selected.gl_code} "
                       f"({_candidate_score(candidate):.2f} vs {_candidate_score(selected):.2f})."),
        })
    return output


__all__ = ["AccountingDecisionEngine"]
