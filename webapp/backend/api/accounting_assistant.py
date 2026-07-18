"""Governed accounting chat and operator-rule endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..services import accounting_assistant as assistant
from ..services import approved_invoice_corrections as approved_corrections
from ..services import ai_provider
from ..services import operator_accounting_rules as rules


router = APIRouter(prefix="/api/accounting-assistant", tags=["accounting-assistant"])


class ChatRequest(BaseModel):
    batch_id: str
    invoice_group_id: str
    message: str = Field(min_length=1, max_length=4000)
    tenant_id: str | None = None


class RuleDecisionRequest(BaseModel):
    approve: bool
    actor: str = "local_operator"


class CorrectionDecisionRequest(BaseModel):
    approve: bool
    actor: str = "local_operator"


class RuleUpdateRequest(BaseModel):
    draft: rules.AccountingRuleDraft
    actor: str = "local_operator"


class RuleStatusRequest(BaseModel):
    enabled: bool
    actor: str = "local_operator"


@router.post("/chat")
def chat(body: ChatRequest) -> dict:
    try:
        return assistant.chat(
            batch_id=body.batch_id,
            invoice_group_id=body.invoice_group_id,
            message=body.message,
            tenant_id=body.tenant_id,
        ).model_dump(mode="json")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except KeyError:
        raise HTTPException(status_code=404, detail="Selected invoice was not found in the batch.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ai_provider.AIProviderNotConfigured as exc:
        raise HTTPException(status_code=503, detail=exc.safe_diagnostic())
    except ai_provider.AIProviderError as exc:
        diagnostic = exc.safe_diagnostic()
        diagnostic["message"] = _provider_failure_message(exc.failure_code)
        raise HTTPException(status_code=502, detail=diagnostic)


def _provider_failure_message(failure_code: str) -> str:
    messages = {
        "provider_invalid_json": (
            "The AI provider returned an empty or invalid structured response. "
            "No invoice change was applied; please try again."
        ),
        "provider_invalid_schema": (
            "The AI provider response did not satisfy the accounting contract. "
            "No invoice change was applied; please try again."
        ),
        "text_transport_error": "The AI provider could not be reached. Please try again shortly.",
        "text_http_error": "The AI provider rejected the request. No invoice change was applied.",
    }
    return messages.get(
        failure_code,
        "The accounting assistant provider failed safely. No invoice change was applied.",
    )


@router.get("/interactions")
def list_interactions(batch_id: str, invoice_group_id: str) -> dict:
    return {
        "contract_version": assistant.ASSISTANT_CONTRACT_VERSION,
        "items": assistant.list_interactions(
            batch_id=batch_id, invoice_group_id=invoice_group_id,
        ),
    }


@router.get("/corrections")
def list_approved_corrections(batch_id: str | None = None) -> dict:
    items = approved_corrections.list_corrections(batch_id=batch_id)
    return {
        "contract_version": approved_corrections.CORRECTION_CONTRACT_VERSION,
        "items": [item.model_dump(mode="json") for item in items],
        "active_count": sum(item.status == "active" for item in items),
    }


@router.post("/interactions/{interaction_id}/corrections/decision")
def decide_corrections(interaction_id: str, body: CorrectionDecisionRequest) -> dict:
    try:
        return assistant.decide_corrections(
            interaction_id, approve=body.approve, actor=body.actor,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Assistant interaction not found.")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/rules")
def list_rules() -> dict:
    items = rules.list_rules()
    return {
        "contract_version": rules.RULE_CONTRACT_VERSION,
        "items": [item.model_dump(mode="json") for item in items],
        "active_count": sum(item.status is rules.RuleStatus.ACTIVE for item in items),
    }


@router.post("/rules/{rule_id}/decision")
def decide_rule(rule_id: str, body: RuleDecisionRequest) -> dict:
    try:
        updated = rules.decide_draft(
            rule_id, approve=body.approve, actor=body.actor,
        )
        context = assistant.interaction_context(updated.source_interaction_id or "")
        if context:
            from ..services import operator_activity_log
            operator_activity_log.record(
                batch_id=context["batch_id"],
                invoice_group_id=context["invoice_group_id"],
                event_type="accounting_rule_approved" if body.approve else "accounting_rule_rejected",
                source="rule",
                actor=body.actor,
                summary=(
                    f"Approved reusable accounting rule: {updated.title}."
                    if body.approve else f"Rejected reusable accounting rule: {updated.title}."
                ),
                details={"rule_id": updated.rule_id, "status": updated.status.value},
            )
        return updated.model_dump(mode="json")
    except KeyError:
        raise HTTPException(status_code=404, detail="Rule proposal not found.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.put("/rules/{rule_id}")
def update_rule(rule_id: str, body: RuleUpdateRequest) -> dict:
    try:
        return rules.update_rule(
            rule_id, body.draft, actor=body.actor,
        ).model_dump(mode="json")
    except KeyError:
        raise HTTPException(status_code=404, detail="Rule not found.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/rules/{rule_id}/status")
def set_rule_status(rule_id: str, body: RuleStatusRequest) -> dict:
    try:
        return rules.set_rule_enabled(
            rule_id, enabled=body.enabled, actor=body.actor,
        ).model_dump(mode="json")
    except KeyError:
        raise HTTPException(status_code=404, detail="Rule not found.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
