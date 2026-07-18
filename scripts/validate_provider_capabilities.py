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
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-output", type=Path)
    parser.add_argument(
        "--profile-id", action="append", default=[],
        help="Probe only this logical profile ID; repeat for multiple profiles.",
    )
    parser.add_argument(
        "--list-only", action="store_true",
        help="Show secret-safe configuration status without making provider calls.",
    )
    args = parser.parse_args()
    profiles = ProfileLoader().load()
    if args.profile_id:
        requested = set(args.profile_id)
        profiles = [profile for profile in profiles if profile.profile_id in requested]
        missing = sorted(requested - {profile.profile_id for profile in profiles})
        if missing:
            print(json.dumps({"error": "profile_not_configured", "profile_ids": missing}))
            return 3
    if args.list_only:
        print(json.dumps({
            "profiles": [{
                "profile_id": profile.profile_id,
                "provider": profile.provider,
                "model_id": profile.model_id,
                "role": profile.role.value,
                "enabled": profile.enabled,
                "credentials_present": profile.credentials_present,
                "endpoint_configured": profile.base_url_configured,
                "declared_capabilities": [item.value for item in profile.declared_capabilities],
            } for profile in profiles],
            "provider_calls_made": 0,
            "secrets_exposed": False,
        }, indent=2, sort_keys=True))
        return 0
    report = ProviderCapabilityValidator().audit(profiles)
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
