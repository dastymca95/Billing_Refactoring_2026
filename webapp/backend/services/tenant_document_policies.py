"""Explicit tenant policies for source-date to accounting-date derivations."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import yaml
from pydantic import BaseModel

from .. import settings
from .tenant_accounting_policies import default_tenant_id, validate_tenant_id


class TenantDatePolicy(BaseModel):
    policy_id: str
    invoice_date_from_service_date: bool = False
    due_date_from_upon_receipt: bool = False


class TenantDocumentPolicy(BaseModel):
    contract_version: str
    tenant_id: str
    date_policy: TenantDatePolicy


@lru_cache(maxsize=32)
def get_policy(tenant_id: str | None = None) -> TenantDocumentPolicy:
    resolved_tenant = validate_tenant_id(tenant_id or default_tenant_id())
    # Test/deployment asset roots must be self-contained. In production the
    # runtime root defaults to PROJECT_ROOT, preserving the existing policy.
    path = settings.RUNTIME_ASSET_ROOT / "config" / "tenant_document_policies.yaml"
    payload: dict[str, Any] = {}
    if path.is_file():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if isinstance(loaded, dict):
            payload = loaded
    tenant_payload = (payload.get("tenants") or {}).get(resolved_tenant) or {}
    date_payload = tenant_payload.get("date_policy") or {}
    return TenantDocumentPolicy(
        contract_version=str(payload.get("contract_version") or "tenant-document-policy/1.0"),
        tenant_id=resolved_tenant,
        date_policy=TenantDatePolicy(
            policy_id=str(date_payload.get("policy_id") or f"{resolved_tenant}-date-policy/disabled"),
            invoice_date_from_service_date=bool(date_payload.get("invoice_date_from_service_date", False)),
            due_date_from_upon_receipt=bool(date_payload.get("due_date_from_upon_receipt", False)),
        ),
    )


__all__ = ["TenantDatePolicy", "TenantDocumentPolicy", "get_policy"]
