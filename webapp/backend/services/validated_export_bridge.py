"""Single readiness-authorized workbook export boundary."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import accounting_readiness


@dataclass
class ExportAuthorizationError(ValueError):
    code: str
    blockers: list[dict[str, Any]]

    def __str__(self) -> str:
        return self.code


class ReadinessValidatedExporter:
    def authorize(self, batch_id: str, normalized_rows: list[dict[str, Any]],
                  expected_snapshot_id: str | None = None) -> dict[str, Any]:
        decision = accounting_readiness.evaluate_rows(normalized_rows)
        payload = accounting_readiness.as_dict(decision)
        if expected_snapshot_id and decision.snapshot_id != expected_snapshot_id:
            raise ExportAuthorizationError("stale_readiness_snapshot", payload["blockers"])
        if not decision.export_allowed:
            raise ExportAuthorizationError("accounting_readiness_blocked", payload["blockers"])
        return payload

    def export(self, batch_id: str, normalized_rows: list[dict[str, Any]], snapshot_context: dict[str, Any],
               output_path: Path, writer: Callable[[Path, list[dict[str, Any]]], Any]) -> tuple[Any, dict[str, Any]]:
        readiness = self.authorize(batch_id, normalized_rows, snapshot_context.get("snapshot_id"))
        result = writer(output_path, normalized_rows)
        return result, readiness


__all__ = ["ExportAuthorizationError", "ReadinessValidatedExporter"]
