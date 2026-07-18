"""Private operator chat that proposes corrections and governed rules.

The model is advisory.  It cannot mutate invoices, select a final GL, activate a
rule, decide readiness, or authorize export.  Every returned correction and
rule is schema/catalog validated before it reaches the UI.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
import uuid
from difflib import SequenceMatcher
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .. import settings
from . import ai_provider
from .gl_catalog import load_gl_catalog
from .economic_responsibility import FilenameFolderContextParser
from .invoice_identity import build_invoice_identities
from .operator_accounting_rules import (
    AccountingRuleDraft,
    OperatorAccountingRule,
    create_draft,
)
from .tenant_accounting_policies import (
    TenantAccountingPolicy,
    TenantPolicyDraft,
    create_policy_draft,
    default_tenant_id,
    list_vendor_entities,
    resolve_tenant_context,
    validate_tenant_id,
)
from .semantic_reasoning_gateway import _select_accounting_profile


ASSISTANT_CONTRACT_VERSION = "accounting-assistant/1.0"
EDITABLE_FIELDS = {
    "GL Account", "Property Abbreviation", "Location",
    "Invoice Description", "Line Item Description",
}


class ProposedInvoiceCorrection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    row_index: int = Field(ge=0)
    field: Literal[
        "GL Account", "Property Abbreviation", "Location",
        "Invoice Description", "Line Item Description",
    ]
    new_value: str
    rationale: str = Field(min_length=3, max_length=1000)
    evidence: list[str] = Field(default_factory=list, max_length=8)


class AssistantModelResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assistant_message: str = Field(min_length=1, max_length=4000)
    corrections: list[ProposedInvoiceCorrection] = Field(default_factory=list, max_length=100)
    proposed_rule: AccountingRuleDraft | None = None
    proposed_tenant_policy: TenantPolicyDraft | None = None


class ConversationTurnResolution(BaseModel):
    """Deterministic context for short answers to the preceding assistant turn."""

    model_config = ConfigDict(extra="forbid")
    answer_to_previous_question: Literal["affirmative", "negative", "none"] = "none"
    previous_question: str | None = None
    resolved_previous_question: bool = False
    unsupported_rule_scope: Literal[
        "vendor_identity", "property_identity", "invoice_identity",
    ] | None = None


class AssistantChatResult(BaseModel):
    contract_version: str = ASSISTANT_CONTRACT_VERSION
    interaction_id: str
    batch_id: str
    invoice_group_id: str
    tenant_id: str = "local-default"
    conversation_mode: Literal["lightweight", "advisory", "action"] = "advisory"
    action_extraction_status: Literal[
        "not_requested", "succeeded", "failed_safe",
    ] = "not_requested"
    assistant_message: str
    corrections: list[ProposedInvoiceCorrection]
    proposed_rule: OperatorAccountingRule | None = None
    proposed_tenant_policy: TenantAccountingPolicy | None = None
    requires_correction_confirmation: bool
    requires_rule_confirmation: bool
    requires_tenant_policy_simulation: bool = False
    accounting_readiness_changed: bool = False
    export_authorized: bool = False
    provider_profile_id: str
    estimated_cost_usd: float
    created_at: datetime
    correction_status: Literal["not_applicable", "pending", "applied", "rejected"] = "not_applicable"
    corrections_decided_at: datetime | None = None
    corrections_decided_by: str | None = None


def chat(
    *, batch_id: str, invoice_group_id: str, message: str,
    tenant_id: str | None = None,
) -> AssistantChatResult:
    message = str(message or "").strip()
    if not message:
        raise ValueError("A message is required.")
    if len(message) > 4000:
        raise ValueError("Message exceeds the 4000-character limit.")
    tenant_id = resolve_tenant_context(tenant_id)
    rows = _load_invoice_rows(batch_id, invoice_group_id)
    conversation_history = _conversation_context(
        batch_id=batch_id, invoice_group_id=invoice_group_id,
    )
    turn_resolution = _resolve_conversation_turn(message, conversation_history)
    conversation_mode = _conversation_mode(message, turn_resolution)
    accounting_profile = _select_accounting_profile()
    conversation_profile = _select_conversation_profile()
    profile = (
        conversation_profile or accounting_profile
        if conversation_mode == "lightweight"
        else accounting_profile or conversation_profile
    )
    if profile is None:
        raise ai_provider.AIProviderNotConfigured(
            "No probe-verified accounting assistant profile is available.",
            failure_code="accounting_assistant_profile_unavailable",
        )
    interaction_id = "aai_" + uuid.uuid4().hex[:14]
    natural_response = conversation_mode in {"lightweight", "advisory"}
    if conversation_mode == "lightweight":
        messages = _lightweight_conversation_messages(message, conversation_history)
        max_tokens = 500
    elif conversation_mode == "advisory":
        messages = _natural_accounting_messages(
            message, conversation_history, rows, tenant_id=tenant_id,
        )
        max_tokens = 1200
    else:
        prompt = _prompt(
            message,
            rows,
            conversation_history=conversation_history,
            turn_resolution=turn_resolution,
            tenant_id=tenant_id,
        )
        messages = _conversation_messages(prompt)
        max_tokens = 2200
    estimated_cost = _estimate_cost(profile, {"messages": messages}, max_tokens)
    limit = _float_env("AI_MAX_ACCOUNTING_ASSISTANT_COST_USD", 0.02)
    if estimated_cost > limit:
        raise ai_provider.AIProviderUnavailable(
            "Accounting assistant request exceeds its configured cost budget.",
            failure_code="accounting_assistant_cost_budget_exceeded",
        )
    provider_failures: list[str] = []
    action_extraction_status: Literal["not_requested", "succeeded", "failed_safe"] = "not_requested"
    if natural_response:
        try:
            natural_text = _request_natural_model(profile, messages, max_tokens=max_tokens)
        except ai_provider.AIProviderError as exc:
            provider_failures.append(exc.failure_code)
            fallback = (
                accounting_profile if conversation_mode == "lightweight"
                else conversation_profile
            )
            if fallback is None or fallback.profile_id == profile.profile_id:
                raise
            fallback_cost = _estimate_cost(fallback, {"messages": messages}, max_tokens)
            if estimated_cost + fallback_cost > limit:
                raise
            profile = fallback
            natural_text = _request_natural_model(profile, messages, max_tokens=max_tokens)
            estimated_cost = round(estimated_cost + fallback_cost, 6)
        parsed = AssistantModelResponse(
            assistant_message=natural_text,
            corrections=[], proposed_rule=None, proposed_tenant_policy=None,
        )
    else:
        payload = {
            "model": profile.model_id,
            "response_format": {"type": "json_object"},
            "messages": messages,
        }
        if profile.provider == "deepseek":
            payload["thinking"] = {"type": "disabled"}
        payload.update(ai_provider._completion_controls(profile.provider, max_tokens))
        try:
            parsed = _request_model(profile, payload)
            action_extraction_status = "succeeded"
        except ai_provider.AIProviderError as exc:
            # The governed proposal failed, but conversation must remain
            # available. Retry without the structured-output constraint and
            # return an advisory response with no mutation proposal.
            provider_failures.append(exc.failure_code)
            natural_messages = _natural_accounting_messages(
                message, conversation_history, rows, tenant_id=tenant_id,
                structured_failure_code=exc.failure_code,
            )
            natural_profile = profile
            fallback_cost = _estimate_cost(
                natural_profile, {"messages": natural_messages}, 1200,
            )
            if estimated_cost + fallback_cost > limit and conversation_profile is not None:
                natural_profile = conversation_profile
                fallback_cost = _estimate_cost(
                    natural_profile, {"messages": natural_messages}, 1200,
                )
            if estimated_cost + fallback_cost > limit:
                raise
            try:
                natural_text = _request_natural_model(
                    natural_profile, natural_messages, max_tokens=1200,
                )
            except ai_provider.AIProviderError as fallback_exc:
                provider_failures.append(fallback_exc.failure_code)
                if (conversation_profile is None
                        or conversation_profile.profile_id == natural_profile.profile_id):
                    raise
                natural_profile = conversation_profile
                second_cost = _estimate_cost(
                    natural_profile, {"messages": natural_messages}, 1200,
                )
                if estimated_cost + fallback_cost + second_cost > limit:
                    raise
                natural_text = _request_natural_model(
                    natural_profile, natural_messages, max_tokens=1200,
                )
                fallback_cost += second_cost
            profile = natural_profile
            estimated_cost = round(estimated_cost + fallback_cost, 6)
            parsed = AssistantModelResponse(
                assistant_message=natural_text,
                corrections=[], proposed_rule=None, proposed_tenant_policy=None,
            )
            action_extraction_status = "failed_safe"
    repeat_repair_attempted = False
    previous_answer = next((
        item["content"] for item in reversed(conversation_history)
        if item.get("role") == "assistant"
    ), "")
    if (conversation_mode == "action" and action_extraction_status == "succeeded"
            and _is_stalled_response(parsed, previous_answer, turn_resolution)):
        repeat_repair_attempted = True
        if estimated_cost * 2 <= limit:
            repair_payload = dict(payload)
            repair_payload["messages"] = _conversation_messages(prompt, repair_repetition=True)
            parsed = _request_model(profile, repair_payload)
            estimated_cost = round(estimated_cost * 2, 6)
        if _is_stalled_response(parsed, previous_answer, turn_resolution):
            parsed = parsed.model_copy(update={
                "assistant_message": _safe_stalled_response(turn_resolution),
                "corrections": [],
                "proposed_rule": None,
            })
    corrections = _validate_corrections(parsed.corrections, rows)
    proposed_rule = None
    proposed_tenant_policy = None
    rule_warning = ""
    if parsed.proposed_rule is not None:
        try:
            proposed_rule = create_draft(
                parsed.proposed_rule,
                source_interaction_id=interaction_id,
            )
        except ValueError as exc:
            rule_warning = f" The reusable rule was not saved because it failed validation: {exc}"
    if parsed.proposed_tenant_policy is not None:
        try:
            proposed_tenant_policy = create_policy_draft(
                tenant_id,
                parsed.proposed_tenant_policy,
                source_interaction_id=interaction_id,
            )
        except (KeyError, ValueError) as exc:
            rule_warning += (
                " The tenant policy was not saved because its tenant identity or accounting "
                f"contract failed validation: {exc}"
            )
    result = AssistantChatResult(
        interaction_id=interaction_id,
        batch_id=batch_id,
        invoice_group_id=invoice_group_id,
        tenant_id=tenant_id,
        conversation_mode=conversation_mode,
        action_extraction_status=action_extraction_status,
        assistant_message=parsed.assistant_message + rule_warning,
        corrections=corrections,
        proposed_rule=proposed_rule,
        proposed_tenant_policy=proposed_tenant_policy,
        requires_correction_confirmation=bool(corrections),
        requires_rule_confirmation=proposed_rule is not None,
        requires_tenant_policy_simulation=proposed_tenant_policy is not None,
        provider_profile_id=profile.profile_id,
        estimated_cost_usd=estimated_cost,
        created_at=datetime.now(timezone.utc),
        correction_status="pending" if corrections else "not_applicable",
    )
    _write_interaction(result, user_message=message)
    from . import operator_activity_log
    operator_activity_log.record(
        batch_id=batch_id,
        invoice_group_id=invoice_group_id,
        event_type="ai_assistant_response_created",
        source="ai",
        actor=profile.profile_id,
        summary=(
            f"AI assistant responded with {len(corrections)} proposed correction"
            f"{'s' if len(corrections) != 1 else ''}"
            f" and {'a reusable rule proposal' if proposed_rule else 'no reusable rule proposal'}"
            f" and {'a tenant policy draft' if proposed_tenant_policy else 'no tenant policy draft'}."
        ),
        details={
            "interaction_id": interaction_id,
            "correction_count": len(corrections),
            "rule_id": proposed_rule.rule_id if proposed_rule else None,
            "tenant_policy_id": (
                proposed_tenant_policy.policy_id if proposed_tenant_policy else None
            ),
            "provider_profile_id": profile.profile_id,
            "repeat_repair_attempted": repeat_repair_attempted,
            "resolved_previous_question": turn_resolution.resolved_previous_question,
            "answer_to_previous_question": turn_resolution.answer_to_previous_question,
            "unsupported_rule_scope": turn_resolution.unsupported_rule_scope,
            "conversation_mode": conversation_mode,
            "provider_fallback_failure_codes": provider_failures,
        },
    )
    return result


def _load_invoice_rows(batch_id: str, invoice_group_id: str) -> list[dict[str, Any]]:
    from . import batch_store
    path = batch_store.get_processed_dir(batch_id) / "_webapp_result.json"
    if not path.is_file():
        raise FileNotFoundError("No processed preview is available for this batch.")
    payload = json.loads(path.read_text(encoding="utf-8"))
    invoices = list(payload.get("all_invoices", []) or [])
    identities = build_invoice_identities(invoices)
    matches: list[dict[str, Any]] = []
    flat_index = 0
    for identity, invoice in zip(identities, invoices):
        rows = invoice.get("rows") or []
        for row in rows:
            meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
            group = str(meta.get("invoice_group_id") or identity.group_id)
            if group == invoice_group_id:
                matches.append({"row_index": flat_index, "row": row})
            flat_index += 1
    if not matches:
        raise KeyError(invoice_group_id)
    return matches


def _prompt(
    message: str, rows: list[dict[str, Any]], *,
    conversation_history: list[dict[str, str]] | None = None,
    turn_resolution: ConversationTurnResolution | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    tenant_id = validate_tenant_id(tenant_id) if tenant_id else default_tenant_id()
    _, catalog = load_gl_catalog()
    safe_rows = []
    for item in rows:
        row = item["row"]
        meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
        source = meta.get("source_text") if isinstance(meta.get("source_text"), dict) else {}
        safe_rows.append({
            "row_index": item["row_index"],
            "invoice_number": row.get("Invoice Number"),
            "vendor_candidate": row.get("Vendor"),
            "property": row.get("Property Abbreviation"),
            "location": row.get("Location"),
            "current_gl": row.get("GL Account"),
            "amount": row.get("Amount"),
            "raw_activity": source.get("raw_activity"),
            "raw_description": source.get("raw_description"),
            "normalized_description": meta.get("normalized_source_description"),
            "generated_description": row.get("Line Item Description"),
            "semantic_classification": meta.get("semantic_classification"),
            "accounting_decision": meta.get("accounting_decision"),
            "source_metadata_evidence": _safe_source_metadata_evidence(
                meta, document_id=str(meta.get("source_file") or item["row_index"]),
            ),
        })
    chart = [{
        "gl_code": code,
        "gl_name": account.gl_name,
        "gl_family": account.gl_family,
        "trade_families": account.trade_families,
        "compatible_work_modes": account.compatible_work_modes,
    } for code, account in catalog.items() if account.payable]
    return {
        "operator_message": message,
        "conversation_history": list(conversation_history or []),
        "turn_resolution": (turn_resolution or ConversationTurnResolution()).model_dump(),
        "tenant_context": {
            "tenant_id": tenant_id,
            "vendor_entities": [{
                "vendor_entity_id": entity.vendor_entity_id,
                "canonical_name": entity.canonical_name,
                "erp_vendor_id": entity.erp_vendor_id,
                "aliases": entity.aliases,
            } for entity in list_vendor_entities(tenant_id)],
        },
        "selected_invoice_rows": safe_rows,
        "payable_chart": chart,
        "editable_fields": sorted(EDITABLE_FIELDS),
        "rule_scope_fields": [
            "document_family", "line_family", "trade_family", "work_mode",
            "description_terms", "term_match",
        ],
        "rule_constraint_fields": [
            "allowed_gl_codes", "minimum_gl_code", "maximum_gl_code",
        ],
        "automation_contract": {
            "legacy_supported": "vendor-neutral semantic GL candidate constraints",
            "tenant_policy_supported": (
                "tenant-isolated vendor/service and semantic GL candidate constraints"
            ),
            "tenant_policy_requires": [
                "existing vendor_entity_id when vendor-scoped",
                "simulation before approval",
                "explicit human approval before activation",
            ],
            "unsupported_identity_scopes": ["single invoice identity"],
            "final_gl_authority": "AccountingDecisionEngine",
            "activation_authority": "explicit human approval",
            "instruction": (
                "Use proposed_tenant_policy for vendor-scoped policies. Never place vendor identity "
                "inside proposed_rule. Never invent a vendor_entity_id."
            ),
        },
        "response_schema": {
            "assistant_message": "clear response in the operator's language",
            "corrections": [{
                "row_index": "exact supplied row index",
                "field": "one editable field",
                "new_value": "proposed value",
                "rationale": "why this change fits source evidence and chart",
                "evidence": ["verbatim source evidence"],
            }],
            "proposed_rule": {
                "title": "reusable semantic policy",
                "description": "plain-language meaning and risk",
                "scope": {
                    "document_family": None,
                    "line_family": None,
                    "trade_family": None,
                    "work_mode": None,
                    "description_terms": [],
                    "term_match": "any",
                },
                "constraint": {
                    "allowed_gl_codes": [],
                    "minimum_gl_code": None,
                    "maximum_gl_code": None,
                },
            },
            "proposed_tenant_policy": {
                "title": "tenant-specific governed policy",
                "description": "plain-language meaning, scope and risk",
                "policy_type": "semantic_gl or vendor_service_gl",
                "scope": {
                    "vendor_entity_id": "existing tenant vendor entity id or null",
                    "property_ids": [],
                    "document_family": None,
                    "line_family": None,
                    "trade_family": None,
                    "work_mode": None,
                    "description_terms": [],
                    "term_match": "any",
                },
                "action": {
                    "allowed_gl_codes": [],
                    "expected_amount": None,
                    "amount_tolerance": "0.01",
                    "amount_mismatch_behavior": "review",
                },
            },
        },
    }


def _system_prompt() -> str:
    return (
        "You are the operator's private accounting copilot. Converse naturally, directly, and "
        "helpfully in the operator's language, while staying focused on invoices, finance, "
        "accounting operations, supporting evidence, and the supplied chart of accounts. Use the "
        "bounded conversation history to follow context and answer follow-up questions instead of "
        "restarting the conversation. A short reply can answer your immediately preceding question; "
        "resolve it from the preceding turns instead of asking the same question again. When the "
        "operator has clearly confirmed a generally applicable GL policy, proposed_rule must contain "
        "the inert reusable draft in that response; do not repeatedly ask for scope or GL that the "
        "operator already supplied. Do not force a checklist, table, or proposal when the user is "
        "only asking a question. Return strict JSON using assistant_message for the natural reply. "
        "Analyze only supplied source facts, semantics, current decisions, and payable chart metadata. You may "
        "propose invoice edits, but never claim they were applied. You may propose a reusable "
        "rule only when the operator expresses a general policy; otherwise proposed_rule must be "
        "null. Reusable rules must be vendor-neutral and may never contain vendor, person, "
        "property, invoice, account-number, filename, or fixture identity. Vendor-specific behavior "
        "must use proposed_tenant_policy, never proposed_rule. A vendor_service_gl tenant policy must "
        "reference an existing vendor_entity_id from tenant_context and should also include compatible "
        "line-level semantic or description evidence; vendor identity alone is insufficient. Never "
        "invent a vendor entity ID. Tenant policy drafts remain inert, require simulation, and require "
        "explicit human approval before activation. If the requested vendor entity does not exist, say "
        "that it must be created or connected from the tenant ERP before the policy can be drafted. "
        "Correct any prior assistant turn that incorrectly implied vendor-scoped policies were wholly "
        "unsupported. A resolved yes/no question is closed: acknowledge the answer and advance "
        "or explain the applicable boundary; never ask the same decision again in different words. "
        "Rules only constrain "
        "GL candidates; they never select final GL, calculate readiness, or authorize export. "
        "Do not alter raw source text, amounts, tax, dates, status, readiness, or provenance. "
        "Generated descriptions are non-source explanatory text: never quote or treat them as "
        "observed document evidence. GL minimum and maximum constraints are inclusive; translate "
        "'above N' to minimum N+1 and 'at least N' to minimum N. Numeric GL order is not semantic "
        "evidence, so warn when a requested range includes unrelated accounts or excludes "
        "compatible accounts. "
        "Every GL correction must exist in the supplied payable chart. Explain tradeoffs and "
        "compare materially relevant alternatives. If evidence is insufficient, propose no edit "
        "and say what evidence is missing."
    )


def _conversation_messages(
    prompt: dict[str, Any], *, repair_repetition: bool = False,
) -> list[dict[str, str]]:
    """Build real provider chat turns while keeping accounting context typed."""
    context = dict(prompt)
    operator_message = str(context.pop("operator_message", "")).strip()
    history = context.pop("conversation_history", [])
    system = _system_prompt()
    resolution = context.get("turn_resolution")
    if isinstance(resolution, dict) and resolution.get("resolved_previous_question"):
        system += (
            " The runtime has deterministically resolved the newest message as an answer to your "
            "immediately preceding question. Treat CONVERSATION TURN RESOLUTION as authoritative "
            "dialogue state. The quoted previous question is closed and must not be asked again."
        )
    if repair_repetition:
        system += (
            " The prior attempt failed to advance the conversation, including possible paraphrasing "
            "of the same question. Re-evaluate only the newest operator message as a contextual "
            "follow-up. Directly acknowledge what it resolves. Emit a validated proposed_rule only "
            "for vendor-neutral semantics, or proposed_tenant_policy for an identity-resolved tenant "
            "scope. If a required vendor entity is missing, explain that prerequisite naturally and "
            "stop asking for the same confirmation."
        )
    system += "\n\nPRIVATE ACCOUNTING CONTEXT JSON:\n" + json.dumps(
        context, ensure_ascii=False, default=str,
    )
    messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    for item in history if isinstance(history, list) else []:
        role = str(item.get("role") or "") if isinstance(item, dict) else ""
        content = str(item.get("content") or "").strip() if isinstance(item, dict) else ""
        if role in {"user", "assistant"} and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": operator_message})
    return messages


def _conversation_mode(
    message: str, turn_resolution: ConversationTurnResolution,
) -> Literal["lightweight", "advisory", "action"]:
    """Route social turns deterministically; never use AI for accounting gates."""
    normalized = _normalize_dialogue_text(message)
    exact = {
        "hola", "hello", "hi", "hey", "buenas", "buenos dias", "buenas tardes",
        "buenas noches", "como estas", "como estas hoy", "gracias", "muchas gracias",
        "thank you", "thanks", "que puedes hacer", "quien eres",
    }
    if normalized in exact:
        return "lightweight"
    tokens = normalized.split()
    greeting = tokens[:1] and tokens[0] in {"hola", "hello", "hi", "hey", "buenas"}
    accounting_terms = {
        "gl", "invoice", "factura", "vendor", "proveedor", "property", "propiedad",
        "regla", "rule", "cuenta", "codigo", "code", "cambia", "change", "corrige",
        "correct", "aprobar", "approve", "export", "ready", "monto", "amount",
    }
    if greeting and len(tokens) <= 8 and not accounting_terms.intersection(tokens):
        return "lightweight"
    if _action_requested(normalized, turn_resolution):
        return "action"
    return "advisory"


def _action_requested(
    normalized_message: str, turn_resolution: ConversationTurnResolution,
) -> bool:
    if turn_resolution.resolved_previous_question:
        return True
    action_phrases = (
        "cambia ", "cambiar ", "cambiame ", "corrige ", "corregir ",
        "asigna ", "asignar ", "pon ", "coloca ", "aplica ", "aplicar ",
        "crea una regla", "crear una regla", "regla deterministica",
        "quiero que ", "debe usar ", "deben usar ", "debe ir ", "deben ir ",
        "utiliza ", "utilices ", "usa solo ", "use only ", "should use ",
        "change ", "correct ", "assign ", "apply ", "create a rule",
        "approve ", "aprobar ", "reject ", "rechaza ", "propon ", "propose ",
    )
    return any(phrase in f"{normalized_message} " for phrase in action_phrases)


def _select_conversation_profile():
    """Use the cheapest probe-eligible text profile for non-accounting dialogue."""
    return ai_provider._select_cost_routing_profile("text_extraction")


def _lightweight_conversation_messages(
    message: str, history: list[dict[str, str]],
) -> list[dict[str, str]]:
    system = (
        "You are InnerView's friendly private accounting assistant. Reply naturally in the "
        "user's language, as a capable human finance copilot would. This is a social or basic "
        "orientation turn, so do not analyze the invoice, propose edits, create rules, select a "
        "GL, decide readiness, or authorize export. Keep the response warm and concise, and invite "
        "the user to ask about the selected invoice if useful. Return strict JSON with exactly: "
        "assistant_message (string), corrections (empty array), proposed_rule (null), and "
        "proposed_tenant_policy (null)."
    )
    messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    bounded: list[dict[str, str]] = []
    for item in history[-4:]:
        role = item.get("role")
        content = str(item.get("content") or "").strip()[:600]
        if role in {"user", "assistant"} and content:
            bounded.append({"role": role, "content": content})
    messages.extend(bounded)
    messages.append({"role": "user", "content": message})
    return messages


def _natural_accounting_messages(
    message: str,
    history: list[dict[str, str]],
    rows: list[dict[str, Any]],
    *,
    tenant_id: str,
    structured_failure_code: str | None = None,
) -> list[dict[str, str]]:
    system = (
        "You are InnerView's private accounting copilot speaking directly with a finance operator. "
        "Respond as a fluent, thoughtful human expert would: understand the newest message in the "
        "conversation, reason from the selected invoice context, explain implications, identify "
        "uncertainty, and ask a useful follow-up only when it advances the work. Do not sound like a "
        "form, workflow bot, or scripted support agent. Do not repeat a question the operator already "
        "answered. Reply in the operator's language using normal prose, not JSON. "
        "The operator may be supplying a fact rather than ordering a change; acknowledge and analyze "
        "that fact instead of inventing an instruction. Distinguish payment instrument, purchaser, "
        "economic responsibility, reimbursement, property allocation, expense nature, and GL coding: "
        "one does not automatically prove another. Use raw source text as evidence; normalized text is "
        "an interpretation and generated descriptions are never source evidence. "
        "The original filename and explicitly supplied folder display names are human-provided source "
        "metadata evidence. Use them when relevant, but treat parsed filename candidates as "
        "non-authoritative: compare them with the document and operator statements, surface conflicts, "
        "and never let a filename silently overwrite raw document facts. "
        "Never claim that a field, GL, invoice, rule, readiness decision, or export was changed. Only "
        "AccountingDecisionEngine may select final GL and only AccountingReadiness may authorize "
        "export. If the operator appears to want a change, explain what you understand and what safe "
        "proposal would be appropriate; the application will handle confirmation separately."
    )
    if structured_failure_code:
        system += (
            " A governed structured proposal attempt failed validation. Continue the conversation "
            "naturally and explain any recommendation, but do not claim a proposal or change was saved."
        )
    context = {
        "tenant_id": tenant_id,
        "selected_invoice": _compact_invoice_context(rows),
        "structured_proposal_failure_code": structured_failure_code,
    }
    messages: list[dict[str, str]] = [{
        "role": "system",
        "content": system + "\n\nPRIVATE SELECTED-INVOICE CONTEXT:\n"
        + json.dumps(context, ensure_ascii=False, default=str),
    }]
    for item in history[-6:]:
        role = item.get("role")
        content = str(item.get("content") or "").strip()[:1000]
        if role in {"user", "assistant"} and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": message})
    return messages


def _compact_invoice_context(rows: list[dict[str, Any]]) -> dict[str, Any]:
    _, catalog = load_gl_catalog()
    output: list[dict[str, Any]] = []
    omitted = 0
    used_chars = 0
    for item in rows:
        row = item.get("row") if isinstance(item.get("row"), dict) else {}
        meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
        source = meta.get("source_text") if isinstance(meta.get("source_text"), dict) else {}
        semantics = (
            meta.get("semantic_classification")
            if isinstance(meta.get("semantic_classification"), dict) else {}
        )
        decision = (
            meta.get("accounting_decision")
            if isinstance(meta.get("accounting_decision"), dict) else {}
        )
        code = str(row.get("GL Account") or "").strip()
        account = catalog.get(code)
        ranked = decision.get("candidates_ranked") if isinstance(decision, dict) else []
        candidates = []
        for candidate in ranked[:4] if isinstance(ranked, list) else []:
            if not isinstance(candidate, dict):
                continue
            candidates.append({
                "gl_code": candidate.get("gl_code"),
                "gl_name": candidate.get("gl_name"),
                "score": candidate.get("total_score"),
            })
        compact = {
            "row_index": item.get("row_index"),
            "invoice_number": row.get("Invoice Number"),
            "vendor": row.get("Vendor"),
            "property": row.get("Property Abbreviation"),
            "location": row.get("Location"),
            "amount": row.get("Amount"),
            "current_gl": code or None,
            "current_gl_name": account.gl_name if account else None,
            "raw_source_activity": source.get("raw_activity"),
            "raw_source_description": source.get("raw_description"),
            "normalized_source_description": meta.get("normalized_source_description"),
            "generated_description_non_source": row.get("Line Item Description"),
            "source_metadata_evidence": _safe_source_metadata_evidence(
                meta, document_id=str(meta.get("source_file") or item.get("row_index")),
            ),
            "semantic_classification": {
                key: semantics.get(key) for key in (
                    "document_family", "line_family", "trade_family", "work_mode",
                ) if semantics.get(key) is not None
            },
            "decision_candidates": candidates,
            "economic_responsibility": meta.get("economic_responsibility"),
            "reimbursement": meta.get("reimbursement_classification"),
        }
        size = len(json.dumps(compact, ensure_ascii=False, default=str))
        if output and used_chars + size > 12000:
            omitted += 1
            continue
        output.append(compact)
        used_chars += size
    return {
        "rows": output,
        "total_rows": len(rows),
        "omitted_rows_due_to_context_budget": omitted,
        "authority_note": "Context is evidence for conversation, not authorization to mutate fields.",
    }


def _safe_source_metadata_evidence(
    meta: dict[str, Any], *, document_id: str,
) -> dict[str, Any] | None:
    """Expose filename evidence to the private model without filesystem paths."""
    raw_value = str(meta.get("source_file") or "").strip()
    if not raw_value:
        return None
    original_filename = Path(raw_value.replace("\\", "/")).name
    if not original_filename:
        return None
    folders_value = meta.get("source_parent_folders")
    if not isinstance(folders_value, list):
        folders_value = meta.get("relevant_parent_folders")
    folder_names: list[str] = []
    for value in folders_value if isinstance(folders_value, list) else []:
        display = Path(str(value or "").replace("\\", "/")).name.strip()
        if display and display not in folder_names:
            folder_names.append(display[:160])
    existing = (
        meta.get("source_metadata_candidates")
        if isinstance(meta.get("source_metadata_candidates"), dict) else None
    )
    if existing is not None:
        candidates = [
            {
                "candidate_type": item.get("candidate_type"),
                "normalized_value": item.get("normalized_value"),
                "source_kind": item.get("source_kind"),
                "source_part_index": item.get("source_part_index"),
                "confidence": item.get("confidence"),
                "authoritative": False,
            }
            for item in existing.get("candidates", [])
            if isinstance(item, dict)
        ]
        warnings = [str(item) for item in existing.get("warnings", [])]
        schema_version = str(existing.get("schema_version") or "filename-folder-facts/1.0")
    else:
        parsed = FilenameFolderContextParser().parse(
            document_id, original_filename, folder_names,
        )
        candidates = [item.model_dump(mode="json") for item in parsed.candidates]
        warnings = list(parsed.warnings)
        schema_version = parsed.schema_version
    if "filename_and_folder_context_is_non_authoritative" not in warnings:
        warnings.append("filename_and_folder_context_is_non_authoritative")
    return {
        "schema_version": schema_version,
        "original_filename": original_filename,
        "filename_stem": Path(original_filename).stem,
        "relevant_parent_folder_display_names": folder_names,
        "parsed_candidates": candidates,
        "parser_warnings": warnings,
        "authoritative": False,
        "usage_instruction": (
            "Human-provided evidence; verify against document and operator context before interpretation."
        ),
    }


def _request_model(profile: Any, payload: dict[str, Any]) -> AssistantModelResponse:
    request = dict(payload)
    if profile.provider == "deepseek":
        request["thinking"] = {"type": "disabled"}
    raw = ai_provider._send_chat_completion(
        provider=profile.provider,
        payload=request,
        api_key_override=profile.api_key.get_secret_value() if profile.api_key else None,
        base_url_override=profile.base_url,
        timeout_seconds_override=profile.timeout_seconds,
        max_attempts_override=profile.max_retries + 1,
    )
    try:
        return AssistantModelResponse(**ai_provider._extract_json_object(raw))
    except ValidationError as exc:
        raise ai_provider.AIProviderInvalidSchema(
            "AI provider response did not match the accounting assistant contract."
        ) from exc


def _request_natural_model(
    profile: Any, messages: list[dict[str, str]], *, max_tokens: int,
) -> str:
    payload: dict[str, Any] = {"model": profile.model_id, "messages": messages}
    if profile.provider == "deepseek":
        thinking_enabled = os.environ.get(
            "DEEPSEEK_ACCOUNTING_THINKING_ENABLED", "true",
        ).strip().lower() in {"1", "true", "yes", "on"}
        payload["thinking"] = {"type": "enabled" if thinking_enabled else "disabled"}
        if thinking_enabled:
            payload["reasoning_effort"] = os.environ.get(
                "DEEPSEEK_ACCOUNTING_REASONING_EFFORT", "medium",
            ).strip() or "medium"
    payload.update(ai_provider._completion_controls(profile.provider, max_tokens))
    return ai_provider._send_chat_completion(
        provider=profile.provider,
        payload=payload,
        api_key_override=profile.api_key.get_secret_value() if profile.api_key else None,
        base_url_override=profile.base_url,
        timeout_seconds_override=profile.timeout_seconds,
        max_attempts_override=profile.max_retries + 1,
    ).strip()


def _is_repeated_answer(current: str, previous: str) -> bool:
    def normalized(value: str) -> str:
        return " ".join(str(value or "").casefold().split())

    candidate = normalized(current)
    prior = normalized(previous)
    if len(candidate) < 80 or not prior:
        return False
    return candidate == prior or SequenceMatcher(None, candidate, prior).ratio() >= 0.97


def _resolve_conversation_turn(
    message: str, history: list[dict[str, str]],
) -> ConversationTurnResolution:
    previous_assistant = next((
        str(item.get("content") or "").strip()
        for item in reversed(history)
        if item.get("role") == "assistant" and str(item.get("content") or "").strip()
    ), "")
    previous_question = _last_question(previous_assistant)
    answer = _short_answer_kind(message) if previous_question else "none"
    resolved = answer != "none" and bool(previous_question)
    recent_user = next((
        str(item.get("content") or "").strip()
        for item in reversed(history)
        if item.get("role") == "user" and str(item.get("content") or "").strip()
    ), "")
    scope_text = " ".join(filter(None, [recent_user, previous_question or "", message]))
    unsupported = _unsupported_identity_scope(scope_text) if resolved else None
    return ConversationTurnResolution(
        answer_to_previous_question=answer,
        previous_question=previous_question,
        resolved_previous_question=resolved,
        unsupported_rule_scope=unsupported,
    )


def _short_answer_kind(message: str) -> Literal["affirmative", "negative", "none"]:
    normalized = _normalize_dialogue_text(message)
    tokens = normalized.split()
    if not tokens or len(tokens) > 8:
        return "none"
    affirmative = {
        "si", "yes", "correcto", "correct", "confirmo", "confirmed", "claro",
        "ok", "okay", "dale", "exacto", "exactamente", "de acuerdo", "asi es",
    }
    negative = {
        "no", "incorrecto", "incorrect", "rechazo", "reject", "cancelar", "cancel",
    }
    if normalized in affirmative or tokens[0] in affirmative:
        return "affirmative"
    if normalized in negative or tokens[0] in negative:
        return "negative"
    return "none"


def _last_question(text: str) -> str | None:
    cleaned = " ".join(str(text or "").split())
    if "?" not in cleaned:
        return None
    prefix = cleaned.rsplit("?", 1)[0]
    starts = [prefix.rfind(mark) for mark in ("¿", ".", "!", "?")]
    start = max(starts)
    question = prefix[start + 1:].strip() + "?"
    return question if len(question) >= 8 else None


def _unsupported_identity_scope(
    text: str,
) -> Literal["vendor_identity", "property_identity", "invoice_identity"] | None:
    normalized = _normalize_dialogue_text(text)
    if not any(word in normalized for word in ("regla", "determin", "automat", "rule")):
        return None
    specific_words = (
        "especific", "solo para", "solamente", "este vendor", "this vendor",
        "por vendor", "by vendor", "para este proveedor", "for this provider",
    )
    if not any(word in normalized for word in specific_words):
        return None
    if any(word in normalized for word in ("vendor", "proveedor")):
        return "vendor_identity"
    if any(word in normalized for word in ("property", "propiedad")):
        return "property_identity"
    if any(word in normalized for word in ("invoice", "factura")):
        return "invoice_identity"
    return None


def _is_stalled_response(
    parsed: AssistantModelResponse,
    previous_answer: str,
    resolution: ConversationTurnResolution,
) -> bool:
    if _is_repeated_answer(parsed.assistant_message, previous_answer):
        return True
    if not resolution.resolved_previous_question:
        return False
    if (parsed.corrections or parsed.proposed_rule is not None
            or parsed.proposed_tenant_policy is not None):
        return False
    current_question = _last_question(parsed.assistant_message)
    if not current_question:
        return _misrepresents_unsupported_scope(parsed.assistant_message, resolution)
    if _misrepresents_unsupported_scope(parsed.assistant_message, resolution):
        return True
    if (resolution.unsupported_rule_scope
            and _unsupported_identity_scope(current_question) == resolution.unsupported_rule_scope):
        return True
    return _question_similarity(current_question, resolution.previous_question or "") >= 0.45


def _question_similarity(first: str, second: str) -> float:
    stop = {
        "a", "al", "all", "and", "con", "de", "del", "el", "en", "for", "la", "las",
        "los", "of", "o", "para", "que", "the", "to", "todos", "un", "una", "y", "you",
    }
    left = {word for word in _normalize_dialogue_text(first).split() if word not in stop}
    right = {word for word in _normalize_dialogue_text(second).split() if word not in stop}
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _misrepresents_unsupported_scope(
    message: str, resolution: ConversationTurnResolution,
) -> bool:
    if not resolution.unsupported_rule_scope:
        return False
    normalized = _normalize_dialogue_text(message)
    promises = (
        "la regla sera especific", "creare una regla", "crear una regla deterministica especific",
        "puedo crear una regla", "i will create", "i can create a vendor specific rule",
    )
    return any(phrase in normalized for phrase in promises)


def _safe_stalled_response(resolution: ConversationTurnResolution) -> str:
    if resolution.unsupported_rule_scope == "vendor_identity":
        return (
            "Entendí tu confirmación: quieres una automatización específica para ese vendor. "
            "No volveré a pedirte que confirmes lo mismo. Esa automatización debe representarse como "
            "una política aislada del tenant, vinculada a una VendorEntity existente, simulada contra "
            "datos históricos y aprobada por un administrador. No creé ni activé una política porque "
            "el proveedor no produjo un borrador válido con esa identidad; el borrador general anterior "
            "continúa sin aprobar y no se aplicará."
        )
    return (
        "Entendí tu respuesta a la pregunta anterior y no voy a volver a preguntarte lo mismo. "
        "El proveedor no produjo un siguiente paso válido después del reintento, por lo que no se "
        "creó ni activó ninguna corrección o regla."
    )


def _normalize_dialogue_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", str(value or "").casefold())
    ascii_text = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(re.findall(r"[a-z0-9]+", ascii_text))


def _conversation_context(*, batch_id: str, invoice_group_id: str) -> list[dict[str, str]]:
    history = list_interactions(batch_id=batch_id, invoice_group_id=invoice_group_id)[-6:]
    output: list[dict[str, str]] = []
    for item in history:
        user_text = str(item.get("user_message") or "").strip()
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        assistant_text = str(result.get("assistant_message") or "").strip()
        if user_text:
            output.append({"role": "user", "content": user_text[:1500]})
        if assistant_text:
            output.append({"role": "assistant", "content": assistant_text[:2500]})
    # Keep prompt growth bounded even when prior answers were verbose.
    while sum(len(item["content"]) for item in output) > 8000 and output:
        output.pop(0)
    return output


def _validate_corrections(
    corrections: list[ProposedInvoiceCorrection],
    rows: list[dict[str, Any]],
) -> list[ProposedInvoiceCorrection]:
    valid_indexes = {item["row_index"] for item in rows}
    _, catalog = load_gl_catalog()
    output = []
    for correction in corrections:
        if correction.row_index not in valid_indexes or correction.field not in EDITABLE_FIELDS:
            continue
        correction.new_value = str(correction.new_value or "").strip()
        if not correction.new_value:
            continue
        if correction.field == "GL Account":
            account = catalog.get(correction.new_value)
            if account is None or not account.payable:
                continue
        output.append(correction)
    return output


def _write_interaction(result: AssistantChatResult, *, user_message: str) -> None:
    root = settings.WEBAPP_DATA_ROOT / "accounting_assistant" / "interactions"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{result.interaction_id}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "contract_version": ASSISTANT_CONTRACT_VERSION,
        "user_message": user_message,
        "result": result.model_dump(mode="json"),
        "privacy": "local_runtime_only",
    }, indent=2), encoding="utf-8")
    tmp.replace(path)


def list_interactions(*, batch_id: str, invoice_group_id: str) -> list[dict[str, Any]]:
    """Return persisted private chat history for one selected invoice."""
    root = settings.WEBAPP_DATA_ROOT / "accounting_assistant" / "interactions"
    if not root.is_dir():
        return []
    output: list[dict[str, Any]] = []
    for path in root.glob("aai_*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            result = AssistantChatResult(**payload.get("result", {}))
        except (OSError, ValueError, TypeError):
            continue
        if result.batch_id != batch_id or result.invoice_group_id != invoice_group_id:
            continue
        output.append({
            "user_message": str(payload.get("user_message") or ""),
            "result": result.model_dump(mode="json"),
        })
    output.sort(key=lambda item: item["result"]["created_at"])
    return output


def decide_corrections(
    interaction_id: str, *, approve: bool, actor: str = "local_operator",
) -> dict[str, Any]:
    """Apply/reject an assistant proposal with a durable audit decision."""
    path, payload, result = _read_interaction(interaction_id)
    if not result.corrections:
        raise ValueError("This interaction contains no proposed corrections.")
    if result.correction_status in {"applied", "rejected"}:
        return {
            "result": result.model_dump(mode="json"),
            "applied": len(result.corrections) if result.correction_status == "applied" else 0,
            "replayed": result.correction_status == "applied",
        }

    now = datetime.now(timezone.utc)
    replayed = False
    applied = 0
    if approve:
        from . import approved_invoice_corrections as approved
        from . import batch_store, revisions as revisions_service

        cache_path = batch_store.get_processed_dir(result.batch_id) / "_webapp_result.json"
        if not cache_path.is_file():
            raise FileNotFoundError("No processed preview is available for this batch.")
        current = json.loads(cache_path.read_text(encoding="utf-8"))
        approved.approve(
            batch_id=result.batch_id,
            invoice_group_id=result.invoice_group_id,
            interaction_id=result.interaction_id,
            corrections=result.corrections,
            result=current,
            actor=actor,
        )
        report = approved.apply_to_result(current, batch_id=result.batch_id)
        temp = cache_path.with_suffix(".tmp")
        temp.write_text(json.dumps(current, default=str, indent=2), encoding="utf-8")
        temp.replace(cache_path)
        current_revision = revisions_service.current_revision_id(result.batch_id)
        if current_revision:
            revisions_service.overwrite_snapshot(
                result.batch_id, current_revision, result=current,
            )
        applied = report.matched
        replayed = True
        result.correction_status = "applied"
    else:
        result.correction_status = "rejected"
    result.requires_correction_confirmation = False
    result.corrections_decided_at = now
    result.corrections_decided_by = actor
    payload["result"] = result.model_dump(mode="json")
    audit = list(payload.get("correction_audit") or [])
    audit.append({
        "event": "corrections_approved_and_applied" if approve else "corrections_rejected",
        "actor": actor,
        "at": now.isoformat(),
        "count": len(result.corrections),
    })
    payload["correction_audit"] = audit
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temp.replace(path)
    from . import operator_activity_log
    operator_activity_log.record(
        batch_id=result.batch_id,
        invoice_group_id=result.invoice_group_id,
        event_type="ai_corrections_applied" if approve else "ai_corrections_rejected",
        source="ai",
        actor=actor,
        summary=(
            f"Approved and applied {len(result.corrections)} AI-proposed correction"
            f"{'s' if len(result.corrections) != 1 else ''}."
            if approve else
            f"Rejected {len(result.corrections)} AI-proposed correction"
            f"{'s' if len(result.corrections) != 1 else ''}."
        ),
        details={
            "interaction_id": result.interaction_id,
            "correction_count": len(result.corrections),
            "replayed": replayed,
        },
    )
    return {
        "result": result.model_dump(mode="json"),
        "applied": applied,
        "replayed": replayed,
    }


def _read_interaction(
    interaction_id: str,
) -> tuple[Path, dict[str, Any], AssistantChatResult]:
    safe_id = str(interaction_id or "").strip()
    if not safe_id.startswith("aai_") or not safe_id.replace("_", "").isalnum():
        raise KeyError(interaction_id)
    path = settings.WEBAPP_DATA_ROOT / "accounting_assistant" / "interactions" / f"{safe_id}.json"
    if not path.is_file():
        raise KeyError(interaction_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    return path, payload, AssistantChatResult(**payload.get("result", {}))


def interaction_context(interaction_id: str) -> dict[str, str] | None:
    try:
        _path, _payload, result = _read_interaction(interaction_id)
    except (KeyError, OSError, ValueError):
        return None
    return {
        "batch_id": result.batch_id,
        "invoice_group_id": result.invoice_group_id,
    }


def _estimate_cost(profile: Any, prompt: dict[str, Any], max_tokens: int) -> float:
    input_rate = profile.input_cost_usd_per_million
    output_rate = profile.output_cost_usd_per_million
    if input_rate is None or output_rate is None:
        return 1.0
    input_tokens = len(json.dumps(prompt, default=str)) / 4
    return round(input_tokens * input_rate / 1_000_000
                 + max_tokens * output_rate / 1_000_000, 6)


def _float_env(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


__all__ = [
    "AssistantChatResult", "AssistantModelResponse", "ProposedInvoiceCorrection",
    "chat", "decide_corrections", "interaction_context", "list_interactions",
]
