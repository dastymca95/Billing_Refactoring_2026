"""Verify deterministic backend and Playwright discovery counts for CI."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "webapp" / "frontend"
U4_FIXTURE = FRONTEND / "e2e" / "fixtures" / "legacy-u4" / "fixture_manifest.json"


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    if result.returncode:
        print(output)
        raise SystemExit(result.returncode)
    return output


def _backend(minimum: int) -> None:
    output = _run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "webapp/backend/tests"],
        cwd=ROOT,
    )
    matches = re.findall(r"(\d+) tests collected", output)
    if not matches:
        raise SystemExit("backend discovery count was not reported")
    count = int(matches[-1])
    if count < minimum:
        raise SystemExit(f"backend discovery regression: {count} collected; minimum is {minimum}")
    print(f"backend discovery: PASS ({count} tests; minimum {minimum})")


def _validate_u4_fixture(expected: int) -> None:
    if not U4_FIXTURE.is_file():
        raise SystemExit("tracked sanitized legacy U4 fixture is missing")
    try:
        payload = json.loads(U4_FIXTURE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit("tracked sanitized legacy U4 fixture is invalid") from exc
    cases = payload.get("cases") if isinstance(payload, dict) else None
    if payload.get("schema_version") != "legacy-u4-discovery/1.0" or not isinstance(cases, list):
        raise SystemExit("tracked sanitized legacy U4 fixture has an invalid schema")
    if len(cases) != expected:
        raise SystemExit(f"legacy U4 fixture regression: {len(cases)} cases; expected {expected}")
    for item in cases:
        if not isinstance(item, dict) or set(item) != {"key", "label"}:
            raise SystemExit("legacy U4 fixture contains unsupported metadata")


def _playwright(suite: str, expected: int, expected_u4: int) -> None:
    executable = shutil.which("npx") or shutil.which("npx.cmd")
    if not executable:
        raise SystemExit("npx is unavailable; run npm ci first")
    command = [executable, "playwright", "test"]
    env = dict(os.environ)
    env["CI"] = "true"
    env["NO_COLOR"] = "1"
    if suite == "legacy":
        _validate_u4_fixture(expected_u4)
        command.extend(["--config", "playwright.legacy.config.ts"])
        env["INNER_VIEW_U4_RUNTIME_MANIFEST"] = (
            "e2e/fixtures/legacy-u4/ci-runtime-manifest-must-not-exist.json"
        )
    command.append("--list")
    output = _run(command, cwd=FRONTEND, env=env)
    matches = re.findall(r"Total:\s+(\d+) tests? in \d+ files?", output)
    if not matches:
        raise SystemExit(f"{suite} Playwright discovery count was not reported")
    count = int(matches[-1])
    if count != expected:
        raise SystemExit(f"{suite} discovery regression: {count} tests; expected {expected}")
    if suite == "legacy":
        u4_count = sum("utility-u4.spec.ts:" in line for line in output.splitlines())
        if u4_count != expected_u4:
            raise SystemExit(
                f"legacy U4 discovery regression: {u4_count} tests; expected {expected_u4}"
            )
        print(f"legacy discovery: PASS ({count} tests; {u4_count} U4 cases)")
    else:
        print(f"active E2E discovery: PASS ({count} tests)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("suite", choices=("backend", "active", "legacy"))
    parser.add_argument("--minimum", type=int, default=432)
    parser.add_argument("--expected", type=int)
    parser.add_argument("--expected-u4", type=int, default=10)
    args = parser.parse_args()
    if args.suite == "backend":
        _backend(args.minimum)
    else:
        if args.expected is None:
            parser.error("--expected is required for Playwright discovery")
        _playwright(args.suite, args.expected, args.expected_u4)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
