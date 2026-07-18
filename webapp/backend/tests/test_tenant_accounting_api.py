from fastapi.testclient import TestClient

from webapp.backend.main import create_app
from webapp.backend.services import tenant_accounting_policies as policies


def test_tenant_policy_api_requires_simulation_before_activation(monkeypatch, tmp_path):
    monkeypatch.setattr(policies.settings, "WEBAPP_DATA_ROOT", tmp_path)
    monkeypatch.setenv("INNER_VIEW_TENANT_ID", "tenant-api")
    client = TestClient(create_app())

    context = client.get("/api/tenant-accounting/context")
    assert context.status_code == 200
    assert context.json()["tenant_id"] == "tenant-api"

    vendor_response = client.post("/api/tenant-accounting/vendors", json={
        "draft": {
            "canonical_name": "Example Utility",
            "erp_vendor_id": "erp-example",
            "aliases": ["Example Network"],
        },
    })
    assert vendor_response.status_code == 200
    vendor_id = vendor_response.json()["vendor_entity_id"]

    policy_response = client.post("/api/tenant-accounting/policies", json={
        "draft": {
            "title": "Example internet policy",
            "description": "Constrain matching internet lines to the approved tenant GL.",
            "policy_type": "vendor_service_gl",
            "scope": {
                "vendor_entity_id": vendor_id,
                "trade_family": "internet",
                "description_terms": ["internet"],
            },
            "action": {"allowed_gl_codes": ["6139"]},
        },
    })
    assert policy_response.status_code == 200
    policy_id = policy_response.json()["policy_id"]
    assert policy_response.json()["status"] == "draft"

    premature = client.post(
        f"/api/tenant-accounting/policies/{policy_id}/decision",
        json={"approve": True},
    )
    assert premature.status_code == 400
    assert "simulation" in premature.json()["detail"]

    simulation = client.post(
        f"/api/tenant-accounting/policies/{policy_id}/simulate",
        json={"lines": [{
            "line_id": "line-1",
            "observed_vendor": "Example Network",
            "raw_description": "Business internet service",
            "trade_family": "internet",
            "amount": "99.95",
            "current_gl": "6139",
            "candidate_gl_codes": ["6139", "6669"],
        }]},
    )
    assert simulation.status_code == 200
    assert simulation.json()["status"] == "simulated"
    assert simulation.json()["latest_simulation"]["blocking_conflicts"] == 0

    approved = client.post(
        f"/api/tenant-accounting/policies/{policy_id}/decision",
        json={"approve": True, "actor": "tenant-admin"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "active"
    assert approved.json()["approved_by"] == "tenant-admin"


def test_production_context_never_silently_defaults_tenant(monkeypatch):
    monkeypatch.delenv("INNER_VIEW_TENANT_ID", raising=False)
    monkeypatch.setenv("INNER_VIEW_DEPLOYMENT_MODE", "production")
    client = TestClient(create_app())

    response = client.get("/api/tenant-accounting/context")

    assert response.status_code == 503
    assert "INNER_VIEW_TENANT_ID is required" in response.json()["detail"]


def test_production_api_rejects_cross_tenant_override(monkeypatch, tmp_path):
    monkeypatch.setattr(policies.settings, "WEBAPP_DATA_ROOT", tmp_path)
    monkeypatch.setenv("INNER_VIEW_DEPLOYMENT_MODE", "production")
    monkeypatch.setenv("INNER_VIEW_TENANT_ID", "tenant-a")
    client = TestClient(create_app())

    response = client.get("/api/tenant-accounting/vendors?tenant_id=tenant-b")

    assert response.status_code == 403

    chat_response = client.post("/api/accounting-assistant/chat", json={
        "batch_id": "irrelevant",
        "invoice_group_id": "irrelevant",
        "message": "hello",
        "tenant_id": "tenant-b",
    })
    assert chat_response.status_code == 403
