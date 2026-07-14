"""Budgets, isolated caches, and privacy-safe traces for Phase 3."""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping


class BudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeBudget:
    max_cost_usd: float
    max_latency_ms: int


@dataclass
class BudgetLedger:
    budget: RuntimeBudget
    cost_usd: float = 0.0
    latency_ms: int = 0

    def authorize(self, *, estimated_cost_usd: float, estimated_latency_ms: int) -> None:
        if estimated_cost_usd < 0 or estimated_latency_ms < 0:
            raise ValueError("budget estimates cannot be negative")
        if self.cost_usd + estimated_cost_usd > self.budget.max_cost_usd:
            raise BudgetExceeded("cost_budget_exceeded")
        if self.latency_ms + estimated_latency_ms > self.budget.max_latency_ms:
            raise BudgetExceeded("latency_budget_exceeded")

    def record(self, *, cost_usd: float, latency_ms: int) -> None:
        self.authorize(estimated_cost_usd=cost_usd, estimated_latency_ms=latency_ms)
        self.cost_usd += cost_usd
        self.latency_ms += latency_ms


class VersionedJsonCache:
    """A cache namespace bound to one artifact type and contract version."""
    def __init__(self, root: Path, namespace: str, version: str) -> None:
        if namespace not in {"facts", "accounting_reasoning"}:
            raise ValueError("cache namespace must separate facts from accounting reasoning")
        self.root = root / namespace / version
        self.namespace = namespace
        self.version = version

    def key(self, payload: Mapping[str, Any]) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(f"{self.namespace}:{self.version}:{canonical}".encode()).hexdigest()

    def get(self, key: str) -> dict[str, Any] | None:
        path = self.root / f"{key}.json"
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def put(self, key: str, value: Mapping[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / f"{key}.json").write_text(json.dumps(value, sort_keys=True, default=str), encoding="utf-8")


@dataclass(frozen=True)
class DecisionTrace:
    trace_version: str
    document_id_hash: str
    route: str
    route_reason: str
    model_used: str | None
    shadow_only: bool
    processing_time_ms: int
    estimated_cost_usd: float
    cache_namespace: str | None
    outcome: str


def hashed_document_id(document_id: str) -> str:
    return hashlib.sha256(document_id.encode()).hexdigest()[:20]


class TraceSink:
    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, trace: DecisionTrace) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(trace), sort_keys=True) + "\n")


class Timer:
    def __enter__(self):
        self.started = time.perf_counter()
        return self

    def __exit__(self, *_args):
        self.elapsed_ms = int((time.perf_counter() - self.started) * 1000)
