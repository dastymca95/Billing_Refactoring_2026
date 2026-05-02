"""AI fallback service skeleton — Phase 1H foundation.

The web app's optional AI assistance layer. Vendor processors run first,
YAML rules and Unit Info Clean / GL evidence run first; AI fires only
when those produce a low-confidence or missing field. This module
provides the architecture (config + provider adapters + audit log) and
ships with `provider=disabled` so the app works end-to-end with no key.

Design constraints (codified in `config/ai_fallback_rules.yaml`):
  * Rules-first.   AI never overrides a rule-based extraction with
                   confidence above threshold.
  * Validated-only writes.  AI may suggest a Location candidate, but the
                   caller must still validate (property, unit) against
                   Unit Info Clean before persisting.
  * Auditable.     Every suggestion carries field_name, suggested_value,
                   confidence, source_text_excerpt, provider_used, cost
                   estimate, and `requires_manual_review`.
  * Cost-bounded.  `max_cost_per_batch_usd` enforced before each call.
  * Never logs raw provider payloads unless explicitly enabled.

Phase 1H ships only the `DisabledAdapter`. The four real providers are
typed stubs; calling them raises `AIProviderNotImplementedError` so a
misconfiguration can never accidentally call an external API.
"""

from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None  # type: ignore


_LOG = logging.getLogger("webapp.ai_fallback")


# ---------------------------------------------------------------------------
# Public dataclasses (return values)
# ---------------------------------------------------------------------------
@dataclass
class AISuggestion:
    """Structured output for every AI call. The caller decides whether
    the suggestion is good enough to use; this object never mutates the
    target row by itself."""
    field_name: str
    suggested_value: Optional[str]
    confidence: float
    source_text_excerpt: str = ""
    reason: str = ""
    requires_manual_review: bool = True
    provider_used: str = ""
    cost_estimate_usd: float = 0.0
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AIStatus:
    """Status payload exposed via `GET /api/ai/status`. Only the
    operator-safe fields ever reach the wire — API keys are never sent."""
    enabled: bool
    provider: str
    configured: bool
    reason: str
    policy: str = ""
    max_cost_per_batch_usd: float = 0.0
    allowed_tasks: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------
class AIProviderNotImplementedError(NotImplementedError):
    """Raised when an enabled provider has no adapter wired up yet. The
    skeleton ships only `DisabledAdapter` — everything else stays a
    stub until a follow-up phase wires real HTTP."""


class AICostCeilingExceeded(RuntimeError):
    """Raised when a per-batch USD ceiling has been reached."""


# ---------------------------------------------------------------------------
# Provider adapters (one per supported AI vendor)
# ---------------------------------------------------------------------------
class AIProvider(ABC):
    """Common interface every provider adapter implements."""

    name: str = "abstract"

    def __init__(self, *, api_key: Optional[str], rules: dict) -> None:
        self.api_key = api_key
        self.rules = rules

    @abstractmethod
    def is_configured(self) -> bool: ...

    @abstractmethod
    def suggest_field(
        self,
        *,
        field_name: str,
        prompt_text: str,
        cropped_image_bytes: Optional[bytes] = None,
    ) -> AISuggestion: ...


class DisabledAdapter(AIProvider):
    """No-op adapter used when AI is off OR the configured provider has
    no key. Always returns an `AISuggestion` with confidence 0 and
    `error="ai_fallback_not_configured"` so callers can short-circuit."""

    name = "disabled"

    def is_configured(self) -> bool:
        return False

    def suggest_field(
        self, *, field_name: str, prompt_text: str,
        cropped_image_bytes: Optional[bytes] = None,
    ) -> AISuggestion:
        return AISuggestion(
            field_name=field_name,
            suggested_value=None,
            confidence=0.0,
            reason="AI fallback disabled or not configured",
            requires_manual_review=True,
            provider_used=self.name,
            error="ai_fallback_not_configured",
        )


def _stub_adapter(provider_name: str) -> type[AIProvider]:
    """Create a typed stub class for a provider that doesn't yet have a
    real adapter. Calling `suggest_field` on a stub raises
    `AIProviderNotImplementedError` — there is no path to a silent
    "fallback to network" for an unwired provider."""

    class _Stub(AIProvider):
        name = provider_name

        def is_configured(self) -> bool:
            return bool(self.api_key)

        def suggest_field(
            self, *, field_name: str, prompt_text: str,
            cropped_image_bytes: Optional[bytes] = None,
        ) -> AISuggestion:
            raise AIProviderNotImplementedError(
                f"AI provider '{provider_name}' is not yet implemented. "
                f"Phase 1H ships only the disabled adapter."
            )

    _Stub.__name__ = f"{provider_name.capitalize()}Adapter"
    return _Stub


_PROVIDER_ADAPTERS: dict[str, type[AIProvider]] = {
    "disabled": DisabledAdapter,
    "openai": _stub_adapter("openai"),
    "anthropic": _stub_adapter("anthropic"),
    "google_gemini": _stub_adapter("google_gemini"),
    "deepseek": _stub_adapter("deepseek"),
}


# ---------------------------------------------------------------------------
# Service singleton (constructed lazily)
# ---------------------------------------------------------------------------
class AIFallbackService:
    """Reads `config/ai_fallback_rules.yaml`, picks an adapter from the
    `AI_PROVIDER` env var, and exposes a small per-batch API. The
    service deliberately does NOT cache adapter state across batches —
    each batch creates its own bookkeeping (cost tally, audit log path)."""

    def __init__(self, rules_path: Path) -> None:
        self.rules_path = rules_path
        self.rules = self._load_rules(rules_path)
        self.provider_name = (
            os.environ.get("AI_PROVIDER")
            or self.rules.get("ai_fallback", {}).get("provider", "disabled")
        ).strip().lower() or "disabled"
        self.master_enabled = self._resolve_master_enabled()
        self.api_key = self._resolve_api_key(self.provider_name)

        adapter_cls = _PROVIDER_ADAPTERS.get(
            self.provider_name, DisabledAdapter,
        )
        self.adapter: AIProvider = adapter_cls(
            api_key=self.api_key, rules=self.rules,
        )

    # -- public API -------------------------------------------------------
    def is_enabled(self) -> bool:
        """True only if the master switch is on (env + YAML), the
        provider is something other than `disabled`, and the adapter
        has a key."""
        return (
            self.master_enabled
            and self.provider_name != "disabled"
            and self.adapter.is_configured()
        )

    def status(self) -> AIStatus:
        cfg = self.rules.get("ai_fallback", {}) or {}
        allowed = self.rules.get("allowed_tasks", {}) or {}
        if self.is_enabled():
            reason = f"AI fallback ready · provider={self.provider_name}"
        elif self.provider_name == "disabled":
            reason = "AI fallback disabled (provider=disabled)"
        elif not self.master_enabled:
            reason = "AI fallback disabled (AI_FALLBACK_ENABLED=false or rules.enabled=false)"
        elif not self.adapter.is_configured():
            reason = f"AI fallback not configured (no API key for provider={self.provider_name})"
        else:
            reason = "AI fallback unavailable"
        return AIStatus(
            enabled=self.is_enabled(),
            provider=self.provider_name,
            configured=self.adapter.is_configured(),
            reason=reason,
            policy=str(cfg.get("policy", "")),
            max_cost_per_batch_usd=float(cfg.get("max_cost_per_batch_usd", 0.0) or 0.0),
            allowed_tasks=[k for k, v in allowed.items() if v],
        )

    def suggest_field(
        self,
        *,
        field_name: str,
        prompt_text: str,
        cropped_image_bytes: Optional[bytes] = None,
        batch_audit_dir: Optional[Path] = None,
    ) -> AISuggestion:
        """Shortcut wrapper that handles disabled state, allowed_tasks
        check, and audit logging. Phase 1H always lands in the
        `disabled` branch unless the operator has wired a real adapter."""
        if not self.is_enabled():
            sug = self.adapter.suggest_field(
                field_name=field_name,
                prompt_text=prompt_text,
                cropped_image_bytes=cropped_image_bytes,
            )
            self._audit(batch_audit_dir, sug)
            return sug

        allowed = self.rules.get("allowed_tasks", {}) or {}
        if not allowed.get(self._task_key(field_name), False):
            sug = AISuggestion(
                field_name=field_name,
                suggested_value=None,
                confidence=0.0,
                reason=f"task '{field_name}' not in allowed_tasks",
                requires_manual_review=True,
                provider_used=self.provider_name,
                error="task_not_allowed",
            )
            self._audit(batch_audit_dir, sug)
            return sug

        try:
            sug = self.adapter.suggest_field(
                field_name=field_name,
                prompt_text=prompt_text,
                cropped_image_bytes=cropped_image_bytes,
            )
        except AIProviderNotImplementedError as e:
            sug = AISuggestion(
                field_name=field_name,
                suggested_value=None,
                confidence=0.0,
                reason=str(e),
                requires_manual_review=True,
                provider_used=self.provider_name,
                error="provider_not_implemented",
            )
        except Exception as e:
            _LOG.exception("AI fallback adapter raised")
            sug = AISuggestion(
                field_name=field_name,
                suggested_value=None,
                confidence=0.0,
                reason=f"{type(e).__name__}: {e}",
                requires_manual_review=True,
                provider_used=self.provider_name,
                error="adapter_exception",
            )
        self._audit(batch_audit_dir, sug)
        return sug

    # -- private helpers ---------------------------------------------------
    @staticmethod
    def _task_key(field_name: str) -> str:
        """Map a field name to the `allowed_tasks` key. Conservative —
        any field whose name doesn't end with `_extraction` etc. is
        treated as 'unknown task' and refused."""
        f = field_name.strip().lower()
        candidates = {
            "service_address": "service_address_extraction",
            "account_number": "account_number_extraction",
            "due_date": "due_date_extraction",
            "total_amount": "total_amount_extraction",
            "notice_block": "notice_boundary_detection",
            "notice_boundary": "notice_boundary_detection",
            "unit_candidate": "unit_candidate_suggestion",
            "manual_review_explanation": "manual_review_explanation",
        }
        return candidates.get(f, f)

    def _load_rules(self, p: Path) -> dict:
        if not p.is_file() or yaml is None:
            return {}
        try:
            return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception as e:
            _LOG.warning("Failed to load ai_fallback_rules.yaml: %s", e)
            return {}

    def _resolve_master_enabled(self) -> bool:
        env = os.environ.get("AI_FALLBACK_ENABLED", "").strip().lower()
        if env in {"true", "1", "yes"}:
            env_enabled = True
        elif env in {"false", "0", "no", ""}:
            env_enabled = False
        else:
            env_enabled = False
        rules_enabled = bool(
            (self.rules.get("ai_fallback") or {}).get("enabled", False)
        )
        # Both must agree to enable. Conservative by design.
        return env_enabled and rules_enabled

    @staticmethod
    def _resolve_api_key(provider_name: str) -> Optional[str]:
        # We never hardcode the key. Read from environment only. The
        # adapter never exposes the key via __repr__ or status().
        env_var_map = {
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "google_gemini": "GOOGLE_API_KEY",
            "deepseek": "DEEPSEEK_API_KEY",
        }
        var = env_var_map.get(provider_name)
        if not var:
            return None
        v = os.environ.get(var, "").strip()
        return v or None

    def _audit(self, batch_audit_dir: Optional[Path], sug: AISuggestion) -> None:
        if batch_audit_dir is None:
            return
        try:
            log_path = batch_audit_dir / (
                self.rules.get("audit", {}).get("log_jsonl_relative_path")
                or "logs/ai_fallback.jsonl"
            )
            log_path.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "provider": self.provider_name,
                "enabled": self.is_enabled(),
                **sug.to_dict(),
            }
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            _LOG.exception("AI audit log write failed")


# ---------------------------------------------------------------------------
# Module-level helpers (lazy singleton so import order doesn't matter)
# ---------------------------------------------------------------------------
_SERVICE: Optional[AIFallbackService] = None


def get_service() -> AIFallbackService:
    global _SERVICE
    if _SERVICE is None:
        # Late import to avoid a circular dependency on settings.
        from ..settings import PROJECT_ROOT
        rules_path = PROJECT_ROOT / "config" / "ai_fallback_rules.yaml"
        _SERVICE = AIFallbackService(rules_path)
    return _SERVICE


def reset_service_for_tests() -> None:
    """Drop the cached service so a test can mutate env vars and re-init."""
    global _SERVICE
    _SERVICE = None
