from decimal import Decimal

import pytest

from webapp.backend.services.accounting_contracts import (
    DocumentFacts,
    GLCandidate,
    LineItemFacts,
    SemanticClassification,
)
from webapp.backend.services.accounting_decision_engine import AccountingDecisionEngine
from webapp.backend.services.gl_catalog import load_gl_catalog
from webapp.backend.services import tenant_accounting_policies as policies
from webapp.backend.services import operator_accounting_rules as legacy_rules


@pytest.fixture
def isolated_tenant_store(monkeypatch, tmp_path):
    monkeypatch.setattr(policies.settings, "WEBAPP_DATA_ROOT", tmp_path)
    return tmp_path


def semantics(line_id: str = "line-1") -> SemanticClassification:
    return SemanticClassification(
        semantic_version="test/1.0",
        line_item_id=line_id,
        document_family="utility_bill",
        line_family="utility",
        trade_family="internet",
        work_mode="recurring_service",
        recurrence="recurring",
        capital_context="operating",
        confidence=1.0,
    )


def internet_policy(vendor_entity_id: str) -> policies.TenantPolicyDraft:
    return policies.TenantPolicyDraft(
        title="Internet service for approved tenant vendor",
        description="Use the tenant-approved Internet expense GL for matching service lines.",
        policy_type=policies.TenantPolicyType.VENDOR_SERVICE_GL,
        scope=policies.TenantPolicyScope(
            vendor_entity_id=vendor_entity_id,
            trade_family="internet",
            description_terms=["energynet", "internet"],
            term_match="any",
        ),
        action=policies.TenantPolicyAction(
            allowed_gl_codes=["6139"],
            expected_amount=Decimal("99.95"),
            amount_tolerance=Decimal("0.01"),
        ),
    )


def test_vendor_identity_and_policies_are_tenant_isolated(isolated_tenant_store):
    entity = policies.create_vendor_entity(
        "tenant-a",
        policies.VendorEntityDraft(
            canonical_name="Example Utility",
            erp_vendor_id="erp-utility-a",
            aliases=["Example Network"],
        ),
    )

    assert policies.resolve_vendor_entity("tenant-a", "example network").vendor_entity_id == entity.vendor_entity_id
    assert policies.resolve_vendor_entity("tenant-b", "example network").resolved is False
    assert policies.list_vendor_entities("tenant-b") == []


def test_policy_cannot_activate_without_current_simulation(isolated_tenant_store):
    entity = policies.create_vendor_entity(
        "tenant-a",
        policies.VendorEntityDraft(canonical_name="Example Utility"),
    )
    policy = policies.create_policy_draft("tenant-a", internet_policy(entity.vendor_entity_id))

    with pytest.raises(ValueError, match="simulation"):
        policies.decide_policy("tenant-a", policy.policy_id, approve=True)

    simulated = policies.simulate_policy(
        "tenant-a",
        policy.policy_id,
        [policies.PolicySimulationLine(
            line_id="internet-1",
            observed_vendor="Example Utility",
            raw_description="Energynet Business Internet",
            document_family="utility_bill",
            line_family="utility",
            trade_family="internet",
            work_mode="recurring_service",
            amount=Decimal("99.95"),
            current_gl="6139",
            candidate_gl_codes=["6139", "6669"],
        )],
    )
    assert simulated.status is policies.TenantPolicyStatus.SIMULATED
    assert simulated.latest_simulation is not None
    assert simulated.latest_simulation.matched_lines == 1
    assert simulated.latest_simulation.blocking_conflicts == 0

    active = policies.decide_policy("tenant-a", policy.policy_id, approve=True, actor="tenant-admin")
    assert active.status is policies.TenantPolicyStatus.ACTIVE
    assert active.approved_by == "tenant-admin"


def test_policy_cannot_activate_after_empty_or_nonmatching_simulation(isolated_tenant_store):
    entity = policies.create_vendor_entity(
        "tenant-a", policies.VendorEntityDraft(canonical_name="Example Utility"),
    )
    policy = policies.create_policy_draft("tenant-a", internet_policy(entity.vendor_entity_id))
    policies.simulate_policy(
        "tenant-a", policy.policy_id,
        [policies.PolicySimulationLine(
            line_id="other-1", observed_vendor="Different Vendor",
            raw_description="Unrelated service", candidate_gl_codes=["6669"],
        )],
    )

    with pytest.raises(ValueError, match="matching historical line"):
        policies.decide_policy("tenant-a", policy.policy_id, approve=True)


def test_conflicting_simulation_fails_closed_and_cannot_activate(isolated_tenant_store):
    entity = policies.create_vendor_entity(
        "tenant-a",
        policies.VendorEntityDraft(canonical_name="Example Utility"),
    )
    draft = internet_policy(entity.vendor_entity_id)
    draft.scope.trade_family = "materials"
    draft.scope.work_mode = "material_purchase"
    draft.action.allowed_gl_codes = ["6205"]
    policy = policies.create_policy_draft("tenant-a", draft)
    simulated = policies.simulate_policy(
        "tenant-a",
        policy.policy_id,
        [policies.PolicySimulationLine(
            line_id="internet-1",
            observed_vendor="Example Utility",
            raw_description="Internet service materials",
            trade_family="materials",
            work_mode="material_purchase",
            candidate_gl_codes=["6669"],
        )],
    )

    assert simulated.latest_simulation.blocking_conflicts == 1
    with pytest.raises(ValueError, match="blocking conflicts"):
        policies.decide_policy("tenant-a", policy.policy_id, approve=True)


def test_active_vendor_service_policy_is_candidate_only_and_engine_selects_gl(
    isolated_tenant_store,
):
    entity = policies.create_vendor_entity(
        "tenant-a",
        policies.VendorEntityDraft(
            canonical_name="Example Utility",
            erp_vendor_id="erp-1",
            aliases=["Example Network"],
        ),
    )
    policy = policies.create_policy_draft("tenant-a", internet_policy(entity.vendor_entity_id))
    policies.simulate_policy(
        "tenant-a",
        policy.policy_id,
        [policies.PolicySimulationLine(
            line_id="internet-1",
            observed_vendor="Example Network",
            raw_description="Energynet Internet",
            trade_family="internet",
            candidate_gl_codes=["6139", "6669"],
        )],
    )
    policies.decide_policy("tenant-a", policy.policy_id, approve=True)
    _, catalog = load_gl_catalog()
    candidates = [
        GLCandidate(gl_code="6139", gl_name=catalog["6139"].gl_name,
                    source="vendor_default", base_score=.3),
        GLCandidate(gl_code="6669", gl_name=catalog["6669"].gl_name,
                    source="catalog", base_score=.7),
    ]
    row = {
        "Vendor": "Example Network",
        "Amount": Decimal("99.95"),
        "_meta": {"source_text": {"raw_description": "Energynet Internet service"}},
    }
    applied = policies.apply_active_policies(
        tenant_id="tenant-a",
        row=row,
        semantics=semantics(),
        catalog=catalog,
        candidates=candidates,
    )

    assert [item.gl_code for item in applied.candidates] == ["6139"]
    assert "tenant_policy" in applied.candidates[0].source
    assert applied.trace["selected_gl"] is None
    facts = DocumentFacts(
        document_id="doc-1",
        invoice_id="invoice-1",
        line_items=[LineItemFacts(
            line_item_id="line-1",
            raw_description="Energynet Internet service",
            amount=Decimal("99.95"),
        )],
        extraction_route="test",
    )
    decision = AccountingDecisionEngine().decide(
        facts, semantics(), catalog, applied.candidates, {},
    )
    assert decision.decision_source == "AccountingDecisionEngine"
    assert decision.selected_gl_code == "6139"


def test_active_policy_from_other_tenant_never_applies(isolated_tenant_store):
    entity = policies.create_vendor_entity(
        "tenant-a", policies.VendorEntityDraft(canonical_name="Example Utility"),
    )
    policy = policies.create_policy_draft("tenant-a", internet_policy(entity.vendor_entity_id))
    policies.simulate_policy(
        "tenant-a", policy.policy_id,
        [policies.PolicySimulationLine(
            line_id="line-1", observed_vendor="Example Utility",
            raw_description="Internet", trade_family="internet",
            candidate_gl_codes=["6139"],
        )],
    )
    policies.decide_policy("tenant-a", policy.policy_id, approve=True)
    _, catalog = load_gl_catalog()
    candidate = GLCandidate(
        gl_code="6669", gl_name=catalog["6669"].gl_name,
        source="catalog", base_score=.5,
    )

    result = policies.apply_active_policies(
        tenant_id="tenant-b",
        row={"Vendor": "Example Utility", "_meta": {"source_text": {"raw_description": "Internet"}}},
        semantics=semantics(),
        catalog=catalog,
        candidates=[candidate],
    )

    assert result.candidates == [candidate]
    assert result.trace["matched_policy_ids"] == []


def test_global_legacy_rule_adapter_is_skipped_for_other_tenants(
    isolated_tenant_store, monkeypatch, tmp_path,
):
    monkeypatch.setenv("INNER_VIEW_TENANT_ID", "local-default")
    monkeypatch.setattr(legacy_rules, "_store_path", lambda: tmp_path / "legacy-rules.json")
    draft = legacy_rules.AccountingRuleDraft(
        title="Legacy internet constraint",
        description="Historical local-only rule.",
        scope={"trade_family": "internet"},
        constraint={"allowed_gl_codes": ["6139"]},
    )
    legacy_rules.decide_draft(legacy_rules.create_draft(draft).rule_id, approve=True)
    _, catalog = load_gl_catalog()
    candidate = GLCandidate(
        gl_code="6669", gl_name=catalog["6669"].gl_name,
        source="catalog", base_score=.5,
    )

    result = legacy_rules.apply_active_rules(
        tenant_id="tenant-b",
        row={"_meta": {"source_text": {"raw_description": "internet"}}},
        semantics=semantics(),
        catalog=catalog,
        candidates=[candidate],
    )

    assert result.candidates == [candidate]
    assert result.trace["legacy_adapter"] == "skipped_for_non_default_tenant"
