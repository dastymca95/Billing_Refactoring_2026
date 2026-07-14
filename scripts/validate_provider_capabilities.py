"""Probe configured provider profiles without printing credentials."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))

from webapp.backend.services.provider_capabilities import (
    ProfileLoader,
    ProviderCapabilityValidator,
    VerifiedCapabilityRegistry,
)


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--private-output", type=Path)
    args = parser.parse_args(); profiles = ProfileLoader().load(); report = ProviderCapabilityValidator().audit(profiles)
    activation = VerifiedCapabilityRegistry(report.profiles).activation_report()
    payload = report.model_dump(mode="json")
    payload["activation"] = activation.model_dump(mode="json")
    if args.private_output:
        args.private_output.parent.mkdir(parents=True, exist_ok=True)
        args.private_output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    safe = {"schema_version": payload["schema_version"], "configured_provider_count": payload["configured_provider_count"],
        "verified_profile_count": payload["verified_profile_count"],
        "credentials_present_count": payload["credentials_present_count"], "secrets_exposed": False,
        "health_status_counts": {status: sum(p["health_status"] == status for p in payload["profiles"])
                                 for status in ("healthy", "degraded", "unavailable", "disabled")},
        "autonomous_gateway_enabled": activation.autonomous_gateway_enabled,
        "strong_reasoning_mode": activation.strong_reasoning_mode,
        "blocking_reasons": activation.blocking_reasons,
    }
    print(json.dumps(safe, indent=2, sort_keys=True))
    return 0 if activation.autonomous_gateway_enabled else 2


if __name__ == "__main__": raise SystemExit(main())
