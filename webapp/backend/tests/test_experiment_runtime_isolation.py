from __future__ import annotations

import pytest

from webapp.backend import settings


def _experiment_env(root: str, tenant: str = "exp-document-learning") -> dict[str, str]:
    return {
        "INNER_VIEW_EXPERIMENT_MODE": "1",
        "INNER_VIEW_WEBAPP_DATA_ROOT": root,
        "INNER_VIEW_TENANT_ID": tenant,
        "INNER_VIEW_EXPERIMENT_AUTHORIZED_TENANT_ID": tenant,
        "INNER_VIEW_DEPLOYMENT_MODE": "production",
    }


def test_experiment_runtime_accepts_only_ignored_private_project_roots(tmp_path):
    root = settings.PROJECT_ROOT / "tmp" / "document-learning" / "runtime"
    assert settings._resolve_webapp_data_root(_experiment_env(str(root))) == root.resolve()


@pytest.mark.parametrize("tenant", ["", "default-runtime", "company-runtime", "tenant-a"])
def test_experiment_runtime_rejects_non_experiment_tenants(tenant):
    root = settings.PROJECT_ROOT / "tmp" / "document-learning" / "runtime"
    with pytest.raises(RuntimeError, match=r"exp-\*"):
        settings._resolve_webapp_data_root(_experiment_env(str(root), tenant))


def test_experiment_runtime_rejects_cross_tenant_authorization():
    root = settings.PROJECT_ROOT / "tmp" / "document-learning" / "runtime"
    env = _experiment_env(str(root), "exp-tenant-a")
    env["INNER_VIEW_EXPERIMENT_AUTHORIZED_TENANT_ID"] = "exp-tenant-b"
    with pytest.raises(RuntimeError, match="does not match"):
        settings._resolve_webapp_data_root(env)


def test_experiment_runtime_requires_explicit_authorized_tenant():
    root = settings.PROJECT_ROOT / "tmp" / "document-learning" / "runtime"
    env = _experiment_env(str(root))
    env.pop("INNER_VIEW_EXPERIMENT_AUTHORIZED_TENANT_ID")
    with pytest.raises(RuntimeError, match="AUTHORIZED_TENANT_ID"):
        settings._resolve_webapp_data_root(env)


def test_experiment_tenant_comparison_is_normalized():
    root = settings.PROJECT_ROOT / "tmp" / "document-learning" / "runtime"
    env = _experiment_env(str(root), " EXP-Tenant-A ")
    env["INNER_VIEW_EXPERIMENT_AUTHORIZED_TENANT_ID"] = "exp-tenant-a"
    assert settings._resolve_webapp_data_root(env) == root.resolve()


def test_experiment_runtime_rejects_official_or_external_root(tmp_path):
    with pytest.raises(RuntimeError, match="ignored tmp"):
        settings._resolve_webapp_data_root(_experiment_env(str(tmp_path / "runtime")))


def test_experiment_runtime_requires_server_side_identity_enforcement():
    root = settings.PROJECT_ROOT / "tmp" / "document-learning" / "runtime"
    env = _experiment_env(str(root))
    env["INNER_VIEW_DEPLOYMENT_MODE"] = "local"
    with pytest.raises(RuntimeError, match="production identity"):
        settings._resolve_webapp_data_root(env)


def test_normal_runtime_keeps_default_when_override_is_absent():
    assert settings._resolve_webapp_data_root({}) == (settings.PROJECT_ROOT / "webapp_data").resolve()


def test_normal_runtime_rejects_ambiguous_experiment_override(tmp_path):
    with pytest.raises(RuntimeError, match="only when.*EXPERIMENT_MODE"):
        settings._resolve_webapp_data_root({
            "INNER_VIEW_WEBAPP_DATA_ROOT": str(tmp_path / "unexpected-runtime"),
        })
