import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from webapp.backend.services import accounting_assistant, ai_provider
from webapp.backend.services import operator_accounting_rules as rules
from webapp.backend.services import tenant_accounting_policies as tenant_policies
from webapp.backend.services.accounting_contracts import GLCandidate, LineItemFacts
from webapp.backend.services.accounting_pipeline_v2 import capture_source_fields, decide_row
from webapp.backend.services.gl_catalog import load_gl_catalog
from webapp.backend.services.invoice_identity import build_invoice_identities
from webapp.backend.services.provider_capabilities import (
    ModelCapability,
    ModelProfile,
    ModelProfileRole,
)
from webapp.backend.services.semantic_classifier import classify_line


@pytest.fixture
def isolated_rule_store(monkeypatch, tmp_path):
    path = tmp_path / "rules.json"
    monkeypatch.setattr(rules, "_store_path", lambda: path)
    return path


def legal_draft(**overrides):
    payload = {
        "title": "Legal invoice GL constraint",
        "description": "Keep legal-service candidates within approved legal expense accounts.",
        "scope": {"line_family": "legal", "description_terms": ["legal", "attorney"]},
        "constraint": {"allowed_gl_codes": ["6205"]},
    }
    payload.update(overrides)
    return rules.AccountingRuleDraft(**payload)


def test_ai_created_rule_is_inert_until_explicit_human_approval(isolated_rule_store):
    draft = rules.create_draft(legal_draft(), source_interaction_id="chat-1")
    assert draft.status is rules.RuleStatus.DRAFT
    assert rules.list_rules()[0].approved_by is None

    active = rules.decide_draft(draft.rule_id, approve=True, actor="reviewer")
    assert active.status is rules.RuleStatus.ACTIVE
    assert active.approved_by == "reviewer"
    assert [event.event for event in active.audit] == [
        "draft_created", "rule_approved_and_activated",
    ]


def test_reusable_rule_contract_forbids_vendor_invoice_and_property_scope():
    for forbidden in ("vendor", "invoice_id", "property"):
        with pytest.raises(ValidationError):
            rules.AccountingRuleScope(line_family="legal", **{forbidden: "specific"})


def test_invalid_or_nonpayable_gl_rule_is_rejected(isolated_rule_store):
    with pytest.raises(ValueError, match="invalid or non-payable"):
        rules.create_draft(legal_draft(
            constraint={"allowed_gl_codes": ["1100"]},
        ))


def test_active_rule_constrains_candidates_but_does_not_select_gl(isolated_rule_store):
    draft = rules.create_draft(legal_draft())
    rules.decide_draft(draft.rule_id, approve=True)
    facts = LineItemFacts(line_item_id="legal-1", raw_description="Attorney legal service")
    semantics = classify_line(facts, document_id="doc")
    _, catalog = load_gl_catalog()
    result = rules.apply_active_rules(
        row={"_meta": {"source_text": {
            "raw_description": "Attorney legal service",
            "raw_invoice_description": "Legal representation",
        }}},
        semantics=semantics,
        catalog=catalog,
        candidates=[
            GLCandidate(gl_code="6205", gl_name=catalog["6205"].gl_name,
                        source="catalog", base_score=.5),
            GLCandidate(gl_code="6669", gl_name=catalog["6669"].gl_name,
                        source="catalog", base_score=.5),
        ],
    )
    assert [candidate.gl_code for candidate in result.candidates] == ["6205"]
    assert result.trace["selected_gl"] is None
    assert result.trace["matched_rule_ids"] == [draft.rule_id]


def test_rule_terms_never_match_generated_description(isolated_rule_store):
    draft = rules.create_draft(legal_draft(
        scope={"description_terms": ["attorney"]},
    ))
    rules.decide_draft(draft.rule_id, approve=True)
    facts = LineItemFacts(line_item_id="source-1", raw_description="Professional service")
    semantics = classify_line(facts, document_id="doc")
    _, catalog = load_gl_catalog()
    candidates = [GLCandidate(
        gl_code="6205", gl_name=catalog["6205"].gl_name,
        source="catalog", base_score=.5,
    )]
    result = rules.apply_active_rules(
        row={
            "Line Item Description": "Attorney legal service",
            "_meta": {"source_text": {"raw_description": "Professional service"}},
        },
        semantics=semantics,
        catalog=catalog,
        candidates=candidates,
    )
    assert result.trace["matched_rule_ids"] == []
    assert result.candidates == candidates


def test_pipeline_keeps_accounting_engine_as_final_selector(isolated_rule_store):
    draft = rules.create_draft(legal_draft(
        scope={"line_family": "legal"},
    ))
    rules.decide_draft(draft.rule_id, approve=True)
    row = {
        "Invoice Number": "LEGAL-1",
        "Vendor": "Example Professional Services",
        "Bill or Credit": "Bill",
        "Invoice Date": "2026-01-01",
        "Accounting Date": "2026-01-01",
        "Invoice Description": "Legal services",
        "Line Item Number": 1,
        "Property Abbreviation": "TEST",
        "GL Account": "6205",
        "Line Item Description": "Attorney legal service for filing",
        "Amount": 100,
        "Expense Type": "General",
        "Is Replacement Reserve": False,
        "Document Url": "https://example.invalid/source",
        "_meta": {"source_line_description": "Attorney legal service for filing"},
    }
    capture_source_fields(row, document_id="legal-doc", line_item_id="1")
    decision = decide_row(
        row,
        document_id="legal-doc",
        line_item_id="1",
        extraction_route="assistant-rule-test",
    )
    assert decision.decision_source == "AccountingDecisionEngine"
    assert decision.selected_gl_code == "6205"
    assert row["GL Account"] == "6205"
    assert row["_meta"]["operator_accounting_rule_trace"]["matched_rule_ids"] == [draft.rule_id]


def test_chat_proposal_stays_draft_and_drops_invalid_gl_correction(
    monkeypatch, isolated_rule_store, tmp_path,
):
    monkeypatch.setattr(accounting_assistant.settings, "WEBAPP_DATA_ROOT", tmp_path)
    monkeypatch.setattr(accounting_assistant, "_load_invoice_rows", lambda *_args: [{
        "row_index": 3,
        "row": {
            "Invoice Number": "LEGAL-2",
            "Vendor": "Example Firm",
            "GL Account": "6669",
            "Line Item Description": "Legal filing service",
            "Amount": 25,
            "_meta": {
                "source_text": {"raw_description": "Legal filing service"},
                "semantic_classification": {"line_family": "legal"},
            },
        },
    }])
    profile = ModelProfile(
        provider="deepseek",
        profile_id="deepseek-accounting",
        model_id="configured-model",
        role=ModelProfileRole.ACCOUNTING_REASONING,
        declared_capabilities=[
            ModelCapability.ACCOUNTING_REASONING,
            ModelCapability.STRUCTURED_OUTPUT,
        ],
        credentials_present=True,
        api_key="private-test-secret",
        base_url="https://example.invalid",
        input_cost_usd_per_million=.14,
        output_cost_usd_per_million=.28,
    )
    monkeypatch.setattr(accounting_assistant, "_select_accounting_profile", lambda: profile)
    monkeypatch.setattr(ai_provider, "_send_chat_completion", lambda **_kwargs: json.dumps({
        "assistant_message": "I found a legal-accounting correction.",
        "corrections": [
            {"row_index": 3, "field": "GL Account", "new_value": "6205",
             "rationale": "Legal services fit the legal expense account.",
             "evidence": ["Legal filing service"]},
            {"row_index": 3, "field": "GL Account", "new_value": "9999",
             "rationale": "Invalid suggestion must be removed.", "evidence": []},
        ],
        "proposed_rule": legal_draft().model_dump(),
    }))
    result = accounting_assistant.chat(
        batch_id="batch-test",
        invoice_group_id="legal-group",
        message="Use only approved legal accounts for legal invoices.",
    )
    assert [change.new_value for change in result.corrections] == ["6205"]
    assert result.proposed_rule is not None
    assert result.proposed_rule.status is rules.RuleStatus.DRAFT
    assert result.accounting_readiness_changed is False
    assert result.export_authorized is False
    assert Path(tmp_path / "accounting_assistant" / "interactions"
                / f"{result.interaction_id}.json").is_file()


def test_assistant_uses_same_canonical_invoice_identity_as_preview(monkeypatch, tmp_path):
    processed = tmp_path / "processed"
    processed.mkdir()
    invoice = {
        "file_name": "source-document.pdf",
        "invoice_number": "38680",
        "rows": [{"Invoice Number": "38680", "Amount": 10, "_meta": {}}],
    }
    (processed / "_webapp_result.json").write_text(json.dumps({
        "all_invoices": [invoice],
    }), encoding="utf-8")
    from webapp.backend.services import batch_store
    monkeypatch.setattr(batch_store, "get_processed_dir", lambda _batch_id: processed)
    group_id = build_invoice_identities([invoice])[0].group_id

    loaded = accounting_assistant._load_invoice_rows("batch", group_id)

    assert group_id == "source-document.pdf::page-1::38680"
    assert len(loaded) == 1
    assert loaded[0]["row"]["Invoice Number"] == "38680"


def test_assistant_conversation_context_preserves_follow_up_turns(monkeypatch, tmp_path):
    monkeypatch.setattr(accounting_assistant.settings, "WEBAPP_DATA_ROOT", tmp_path)
    previous = accounting_assistant.AssistantChatResult(
        interaction_id="aai_previous_turn",
        batch_id="batch-chat",
        invoice_group_id="invoice-chat",
        assistant_message="The first line is a legal filing fee.",
        corrections=[],
        requires_correction_confirmation=False,
        requires_rule_confirmation=False,
        provider_profile_id="test-profile",
        estimated_cost_usd=0,
        created_at="2026-07-15T12:00:00Z",
    )
    accounting_assistant._write_interaction(previous, user_message="What is the first line?")

    context = accounting_assistant._conversation_context(
        batch_id="batch-chat", invoice_group_id="invoice-chat",
    )

    assert context == [
        {"role": "user", "content": "What is the first line?"},
        {"role": "assistant", "content": "The first line is a legal filing fee."},
    ]
    assert "Converse naturally" in accounting_assistant._system_prompt()


def test_provider_messages_preserve_real_chat_roles():
    messages = accounting_assistant._conversation_messages({
        "operator_message": "6139",
        "conversation_history": [
            {"role": "user", "content": "Which account should internet use?"},
            {"role": "assistant", "content": "Please confirm the GL."},
        ],
        "selected_invoice_rows": [],
        "payable_chart": [],
        "response_schema": {},
    })

    assert [item["role"] for item in messages] == [
        "system", "user", "assistant", "user",
    ]
    assert messages[-1]["content"] == "6139"
    assert "PRIVATE ACCOUNTING CONTEXT JSON" in messages[0]["content"]
    assert "conversation_history" not in messages[0]["content"]


def test_greeting_uses_bounded_text_profile_and_cannot_propose_changes(
    monkeypatch, isolated_rule_store, tmp_path,
):
    monkeypatch.setattr(accounting_assistant.settings, "WEBAPP_DATA_ROOT", tmp_path)
    monkeypatch.setattr(accounting_assistant, "_load_invoice_rows", lambda *_args: [{
        "row_index": 0,
        "row": {
            "Invoice Number": "HELLO-1", "Vendor": "Example", "GL Account": "6669",
            "Amount": 10, "_meta": {"source_text": {"raw_description": "Example item"}},
        },
    }])
    text_profile = ModelProfile(
        provider="gemini", profile_id="gemini-text", model_id="configured-text-model",
        role=ModelProfileRole.TEXT_EXTRACTION,
        declared_capabilities=[ModelCapability.TEXT_EXTRACTION, ModelCapability.STRUCTURED_OUTPUT],
        credentials_present=True, api_key="private-test-secret", base_url="https://example.invalid",
        input_cost_usd_per_million=.25, output_cost_usd_per_million=1.5,
    )
    accounting_profile = ModelProfile(
        provider="deepseek", profile_id="deepseek-accounting", model_id="configured-accounting-model",
        role=ModelProfileRole.ACCOUNTING_REASONING,
        declared_capabilities=[ModelCapability.ACCOUNTING_REASONING, ModelCapability.STRUCTURED_OUTPUT],
        credentials_present=True, api_key="private-test-secret", base_url="https://example.invalid",
        input_cost_usd_per_million=.14, output_cost_usd_per_million=.28,
    )
    monkeypatch.setattr(accounting_assistant, "_select_conversation_profile", lambda: text_profile)
    monkeypatch.setattr(accounting_assistant, "_select_accounting_profile", lambda: accounting_profile)
    captured = {}

    def fake_send(**kwargs):
        captured.update(kwargs["payload"])
        return "¡Hola! ¿En qué te ayudo con este invoice?"

    monkeypatch.setattr(ai_provider, "_send_chat_completion", fake_send)

    result = accounting_assistant.chat(
        batch_id="batch-chat", invoice_group_id="invoice-chat", message="Hola",
    )

    assert result.conversation_mode == "lightweight"
    assert result.provider_profile_id == "gemini-text"
    assert result.assistant_message.startswith("¡Hola!")
    assert result.corrections == []
    assert result.proposed_rule is None
    assert result.proposed_tenant_policy is None
    assert result.action_extraction_status == "not_requested"
    serialized = json.dumps(captured["messages"])
    assert "payable_chart" not in serialized
    assert len(serialized) < 5000


def test_greeting_intent_does_not_hide_accounting_request():
    neutral = accounting_assistant.ConversationTurnResolution()

    assert accounting_assistant._conversation_mode("Hola", neutral) == "lightweight"
    assert accounting_assistant._conversation_mode(
        "Hola, cambia el GL de esta factura", neutral,
    ) == "action"


def test_accounting_observation_gets_natural_contextual_answer_without_json_contract(
    monkeypatch, isolated_rule_store, tmp_path,
):
    monkeypatch.setattr(accounting_assistant.settings, "WEBAPP_DATA_ROOT", tmp_path)
    monkeypatch.setattr(accounting_assistant, "_load_invoice_rows", lambda *_args: [{
        "row_index": 0,
        "row": {
            "Invoice Number": "CARD-1", "Vendor": "Example Merchant",
            "Property Abbreviation": "", "Location": "", "GL Account": "6669",
            "Amount": 25.50, "Line Item Description": "Generated supply description",
            "_meta": {
                "source_text": {"raw_description": "CARD PURCHASE"},
                "semantic_classification": {"line_family": "materials", "work_mode": "material_purchase"},
            },
        },
    }])
    profile = ModelProfile(
        provider="deepseek", profile_id="deepseek-accounting", model_id="configured-model",
        role=ModelProfileRole.ACCOUNTING_REASONING,
        declared_capabilities=[ModelCapability.ACCOUNTING_REASONING],
        credentials_present=True, api_key="private-test-secret", base_url="https://example.invalid",
        input_cost_usd_per_million=.14, output_cost_usd_per_million=.28,
    )
    monkeypatch.setattr(accounting_assistant, "_select_accounting_profile", lambda: profile)
    monkeypatch.setattr(accounting_assistant, "_select_conversation_profile", lambda: None)
    captured = {}

    def fake_send(**kwargs):
        captured.update(kwargs["payload"])
        return (
            "Entiendo. Que se haya pagado con una tarjeta corporativa identifica el medio de pago, "
            "pero no prueba por sí solo una propiedad. Revisaría si corresponde a gasto corporativo "
            "o reembolso antes de proponer la asignación."
        )

    monkeypatch.setattr(ai_provider, "_send_chat_completion", fake_send)

    result = accounting_assistant.chat(
        batch_id="batch-chat", invoice_group_id="invoice-chat",
        message=(
            "Este recibo fue pagado con la tarjeta de crédito de la compañía y es un gasto "
            "que no pertenece a ninguna propiedad."
        ),
    )

    assert result.conversation_mode == "advisory"
    assert result.action_extraction_status == "not_requested"
    assert "medio de pago" in result.assistant_message
    assert result.corrections == []
    assert "response_format" not in captured
    serialized = json.dumps(captured["messages"], ensure_ascii=False)
    assert "CARD PURCHASE" in serialized
    assert "generated_description_non_source" in serialized
    assert len(serialized) < 20000


def test_failed_structured_action_falls_back_to_natural_reply_instead_of_502(
    monkeypatch, isolated_rule_store, tmp_path,
):
    monkeypatch.setattr(accounting_assistant.settings, "WEBAPP_DATA_ROOT", tmp_path)
    monkeypatch.setattr(accounting_assistant, "_load_invoice_rows", lambda *_args: [{
        "row_index": 0,
        "row": {
            "Invoice Number": "ACTION-1", "Vendor": "Example", "GL Account": "6669",
            "Amount": 40, "_meta": {"source_text": {"raw_description": "Fuel purchase"}},
        },
    }])
    profile = ModelProfile(
        provider="deepseek", profile_id="deepseek-accounting", model_id="configured-model",
        role=ModelProfileRole.ACCOUNTING_REASONING,
        declared_capabilities=[ModelCapability.ACCOUNTING_REASONING],
        credentials_present=True, api_key="private-test-secret", base_url="https://example.invalid",
        input_cost_usd_per_million=.14, output_cost_usd_per_million=.28,
    )
    monkeypatch.setattr(accounting_assistant, "_select_accounting_profile", lambda: profile)
    monkeypatch.setattr(accounting_assistant, "_select_conversation_profile", lambda: None)
    payloads = []

    def fake_send(**kwargs):
        payloads.append(kwargs["payload"])
        if len(payloads) == 1:
            raise ai_provider.AIProviderInvalidJSON("AI provider response content was empty.")
        return (
            "Entiendo que quieres cambiar la codificación. No pude preparar una propuesta "
            "estructurada segura todavía, así que no se aplicó ningún cambio."
        )

    monkeypatch.setattr(ai_provider, "_send_chat_completion", fake_send)

    result = accounting_assistant.chat(
        batch_id="batch-chat", invoice_group_id="invoice-chat",
        message="Cambia el GL de esta línea al código correcto para combustible.",
    )

    assert result.conversation_mode == "action"
    assert result.action_extraction_status == "failed_safe"
    assert "no se aplicó ningún cambio" in result.assistant_message
    assert result.corrections == []
    assert len(payloads) == 2
    assert payloads[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in payloads[1]


def test_private_chat_receives_sanitized_non_authoritative_filename_evidence():
    meta = {
        "source_file": r"C:\Users\Private Person\Dropbox\Receipts\$50.56-Company-Fuel-Office.pdf",
        "source_parent_folders": [
            r"C:\Users\Private Person\Dropbox\Receipts",
            r"C:\Users\Private Person\Dropbox\Corporate Card",
        ],
    }

    evidence = accounting_assistant._safe_source_metadata_evidence(
        meta, document_id="doc-1",
    )

    assert evidence["original_filename"] == "$50.56-Company-Fuel-Office.pdf"
    assert evidence["filename_stem"] == "$50.56-Company-Fuel-Office"
    assert evidence["relevant_parent_folder_display_names"] == ["Receipts", "Corporate Card"]
    assert evidence["authoritative"] is False
    assert "filename_and_folder_context_is_non_authoritative" in evidence["parser_warnings"]
    assert any(
        item["candidate_type"] == "amount" and item["normalized_value"] == "50.56"
        for item in evidence["parsed_candidates"]
    )
    serialized = json.dumps(evidence)
    assert "C:\\Users" not in serialized
    assert "Private Person" not in serialized


def test_filename_evidence_is_available_to_advisory_and_structured_prompts():
    rows = [{
        "row_index": 0,
        "row": {
            "Invoice Number": "FILE-1", "Vendor": "Merchant", "GL Account": "6669",
            "Amount": 50.56,
            "_meta": {
                "source_file": "$50.56-Company-Fuel-Office.pdf",
                "source_text": {"raw_description": "Diesel"},
            },
        },
    }]

    natural = accounting_assistant._natural_accounting_messages(
        "What does the filename tell us?", [], rows, tenant_id="local-default",
    )
    structured = accounting_assistant._prompt(
        "Propose the appropriate property", rows, tenant_id="local-default",
    )

    natural_text = json.dumps(natural, ensure_ascii=False)
    structured_text = json.dumps(structured, ensure_ascii=False)
    assert "$50.56-Company-Fuel-Office.pdf" in natural_text
    assert "$50.56-Company-Fuel-Office.pdf" in structured_text
    assert "non_authoritative" in natural_text
    assert "authoritative\": false" in structured_text.lower()


def test_repeated_follow_up_is_retried_and_creates_inert_rule_draft(
    monkeypatch, isolated_rule_store, tmp_path,
):
    monkeypatch.setattr(accounting_assistant.settings, "WEBAPP_DATA_ROOT", tmp_path)
    monkeypatch.setattr(accounting_assistant, "_load_invoice_rows", lambda *_args: [{
        "row_index": 0,
        "row": {
            "Invoice Number": "SERVICE-1",
            "Vendor": "Example Service Provider",
            "GL Account": "6139",
            "Line Item Description": "Business internet service",
            "Amount": 99.95,
            "_meta": {
                "source_text": {"raw_description": "Business internet service"},
                "semantic_classification": {"line_family": "unknown"},
            },
        },
    }])
    profile = ModelProfile(
        provider="deepseek",
        profile_id="deepseek-accounting",
        model_id="configured-model",
        role=ModelProfileRole.ACCOUNTING_REASONING,
        declared_capabilities=[
            ModelCapability.ACCOUNTING_REASONING,
            ModelCapability.STRUCTURED_OUTPUT,
        ],
        credentials_present=True,
        api_key="private-test-secret",
        base_url="https://example.invalid",
        input_cost_usd_per_million=.14,
        output_cost_usd_per_million=.28,
    )
    monkeypatch.setattr(accounting_assistant, "_select_accounting_profile", lambda: profile)
    repeated = (
        "Please confirm whether every internet charge should use GL 6139, regardless of "
        "provider. I need that policy before I can propose a reusable rule."
    )
    previous = accounting_assistant.AssistantChatResult(
        interaction_id="aai_policy_question",
        batch_id="batch-chat",
        invoice_group_id="invoice-chat",
        assistant_message=repeated,
        corrections=[],
        requires_correction_confirmation=False,
        requires_rule_confirmation=False,
        provider_profile_id="deepseek-accounting",
        estimated_cost_usd=0,
        created_at="2026-07-15T12:00:00Z",
    )
    accounting_assistant._write_interaction(
        previous, user_message="Help me define the internet policy.",
    )
    responses = [
        {"assistant_message": repeated, "corrections": [], "proposed_rule": None},
        {
            "assistant_message": (
                "Understood. I prepared an inert reusable draft for internet charges to use "
                "GL 6139. It still requires your explicit approval."
            ),
            "corrections": [],
            "proposed_rule": {
                "title": "Internet expense GL constraint",
                "description": "Keep internet-service candidates within the approved internet expense account.",
                "scope": {
                    "description_terms": ["internet"],
                    "term_match": "any",
                },
                "constraint": {"allowed_gl_codes": ["6139"]},
            },
        },
    ]
    requests = []

    def fake_send(**kwargs):
        requests.append(kwargs["payload"])
        return json.dumps(responses.pop(0))

    monkeypatch.setattr(ai_provider, "_send_chat_completion", fake_send)

    result = accounting_assistant.chat(
        batch_id="batch-chat",
        invoice_group_id="invoice-chat",
        message="Yes, all internet charges should use 6139.",
    )

    assert len(requests) == 2
    assert [item["role"] for item in requests[0]["messages"]] == [
        "system", "user", "assistant", "user",
    ]
    assert "failed to advance" in requests[1]["messages"][0]["content"]
    assert result.proposed_rule is not None
    assert result.proposed_rule.status is rules.RuleStatus.DRAFT
    assert result.requires_rule_confirmation is True
    assert rules.list_rules()[0].status is rules.RuleStatus.DRAFT


def test_vendor_specific_chat_creates_only_inert_tenant_policy_draft(
    monkeypatch, isolated_rule_store, tmp_path,
):
    monkeypatch.setattr(accounting_assistant.settings, "WEBAPP_DATA_ROOT", tmp_path)
    entity = tenant_policies.create_vendor_entity(
        "tenant-a",
        tenant_policies.VendorEntityDraft(
            canonical_name="Example Utility",
            aliases=["Example Network"],
        ),
    )
    monkeypatch.setattr(accounting_assistant, "_load_invoice_rows", lambda *_args: [{
        "row_index": 0,
        "row": {
            "Invoice Number": "SERVICE-3",
            "Vendor": "Example Network",
            "GL Account": "6139",
            "Line Item Description": "Business internet service",
            "Amount": 99.95,
            "_meta": {"source_text": {"raw_description": "Energynet internet service"}},
        },
    }])
    profile = ModelProfile(
        provider="deepseek",
        profile_id="deepseek-accounting",
        model_id="configured-model",
        role=ModelProfileRole.ACCOUNTING_REASONING,
        declared_capabilities=[
            ModelCapability.ACCOUNTING_REASONING,
            ModelCapability.STRUCTURED_OUTPUT,
        ],
        credentials_present=True,
        api_key="private-test-secret",
        base_url="https://example.invalid",
        input_cost_usd_per_million=.14,
        output_cost_usd_per_million=.28,
    )
    monkeypatch.setattr(accounting_assistant, "_select_accounting_profile", lambda: profile)
    response = {
        "assistant_message": "I created a tenant-isolated draft for review and simulation.",
        "corrections": [],
        "proposed_rule": None,
        "proposed_tenant_policy": {
            "title": "Approved vendor internet service",
            "description": "Constrain matching internet lines to the tenant-approved Internet expense GL.",
            "policy_type": "vendor_service_gl",
            "scope": {
                "vendor_entity_id": entity.vendor_entity_id,
                "trade_family": "internet",
                "description_terms": ["internet", "energynet"],
                "term_match": "any",
            },
            "action": {
                "allowed_gl_codes": ["6139"],
                "expected_amount": "99.95",
                "amount_tolerance": "0.01",
                "amount_mismatch_behavior": "review",
            },
        },
    }
    monkeypatch.setattr(
        ai_provider, "_send_chat_completion", lambda **_kwargs: json.dumps(response),
    )

    result = accounting_assistant.chat(
        batch_id="batch-chat",
        invoice_group_id="invoice-chat",
        message="For this approved vendor, internet service should use 6139.",
        tenant_id="tenant-a",
    )

    assert result.proposed_tenant_policy is not None
    assert result.proposed_tenant_policy.status is tenant_policies.TenantPolicyStatus.DRAFT
    assert result.requires_tenant_policy_simulation is True
    assert result.requires_rule_confirmation is False
    assert result.accounting_readiness_changed is False
    assert result.export_authorized is False
    stored = tenant_policies.list_policies("tenant-a")
    assert [item.policy_id for item in stored] == [result.proposed_tenant_policy.policy_id]
    assert stored[0].approved_by is None


def test_short_yes_resolves_vendor_specific_question_without_asking_again():
    history = [
        {
            "role": "user",
            "content": (
                "No independientemente del vendor. Debe ser una regla deterministica "
                "especificamente para este vendor"
            ),
        },
        {
            "role": "assistant",
            "content": (
                "¿Quieres que todos los cargos de este vendor con descripción que contenga "
                "internet vayan siempre a 6139?"
            ),
        },
    ]

    resolution = accounting_assistant._resolve_conversation_turn("Sí", history)

    assert resolution.answer_to_previous_question == "affirmative"
    assert resolution.resolved_previous_question is True
    assert resolution.unsupported_rule_scope == "vendor_identity"
    assert resolution.previous_question.endswith("6139?")


def test_paraphrased_vendor_confirmation_loop_fails_closed_with_truthful_boundary(
    monkeypatch, isolated_rule_store, tmp_path,
):
    monkeypatch.setattr(accounting_assistant.settings, "WEBAPP_DATA_ROOT", tmp_path)
    monkeypatch.setattr(accounting_assistant, "_load_invoice_rows", lambda *_args: [{
        "row_index": 0,
        "row": {
            "Invoice Number": "SERVICE-2",
            "Vendor": "Example Utility",
            "GL Account": "6139",
            "Line Item Description": "Business internet service",
            "Amount": 99.95,
            "_meta": {"source_text": {"raw_description": "Business internet service"}},
        },
    }])
    profile = ModelProfile(
        provider="deepseek",
        profile_id="deepseek-accounting",
        model_id="configured-model",
        role=ModelProfileRole.ACCOUNTING_REASONING,
        declared_capabilities=[
            ModelCapability.ACCOUNTING_REASONING,
            ModelCapability.STRUCTURED_OUTPUT,
        ],
        credentials_present=True,
        api_key="private-test-secret",
        base_url="https://example.invalid",
        input_cost_usd_per_million=.14,
        output_cost_usd_per_million=.28,
    )
    monkeypatch.setattr(accounting_assistant, "_select_accounting_profile", lambda: profile)
    previous = accounting_assistant.AssistantChatResult(
        interaction_id="aai_vendor_question",
        batch_id="batch-chat",
        invoice_group_id="invoice-chat",
        assistant_message=(
            "¿Quieres que todos los cargos de este vendor con descripción que contenga "
            "internet vayan siempre a 6139?"
        ),
        corrections=[],
        requires_correction_confirmation=False,
        requires_rule_confirmation=False,
        provider_profile_id="deepseek-accounting",
        estimated_cost_usd=0,
        created_at="2026-07-15T12:00:00Z",
    )
    accounting_assistant._write_interaction(
        previous,
        user_message=(
            "Debe ser una regla deterministica especificamente para este vendor"
        ),
    )
    responses = [
        {
            "assistant_message": (
                "Para continuar, ¿confirmas que los cargos de este vendor con internet "
                "deben usar siempre 6139?"
            ),
            "corrections": [],
            "proposed_rule": None,
        },
        {
            "assistant_message": (
                "¿Quieres entonces una regla determinística para este vendor y la cuenta 6139?"
            ),
            "corrections": [],
            "proposed_rule": None,
        },
    ]
    requests = []

    def fake_send(**kwargs):
        requests.append(kwargs["payload"])
        return json.dumps(responses.pop(0))

    monkeypatch.setattr(ai_provider, "_send_chat_completion", fake_send)

    result = accounting_assistant.chat(
        batch_id="batch-chat",
        invoice_group_id="invoice-chat",
        message="Sí",
    )

    assert len(requests) == 2
    assert "authoritative dialogue state" in requests[0]["messages"][0]["content"]
    assert "failed to advance" in requests[1]["messages"][0]["content"]
    assert "No volveré a pedirte que confirmes lo mismo" in result.assistant_message
    assert "VendorEntity" in result.assistant_message
    assert "simulad" in result.assistant_message
    assert result.proposed_rule is None
    assert result.requires_rule_confirmation is False
    assert rules.list_rules() == []


def test_rule_edit_and_disable_remain_auditable(isolated_rule_store):
    created = rules.create_draft(legal_draft())
    active = rules.decide_draft(created.rule_id, approve=True)
    edited = rules.update_rule(active.rule_id, legal_draft(
        title="Edited legal constraint",
    ))
    disabled = rules.set_rule_enabled(edited.rule_id, enabled=False)
    assert disabled.status is rules.RuleStatus.DISABLED
    assert [event.event for event in disabled.audit][-2:] == ["rule_edited", "rule_disabled"]


def test_human_rejection_is_auditable_and_never_active(isolated_rule_store):
    created = rules.create_draft(legal_draft())
    rejected = rules.decide_draft(created.rule_id, approve=False, actor="reviewer")
    assert rejected.status is rules.RuleStatus.REJECTED
    assert rejected.approved_by is None
    assert rejected.audit[-1].event == "rule_rejected"
    assert rules.list_rules(include_rejected=False) == []
