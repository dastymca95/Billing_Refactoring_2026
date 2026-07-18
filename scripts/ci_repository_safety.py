"""Fail CI on tracked private artifacts, secrets, or unsafe source changes.

The scanner intentionally reports only a path, line number, and finding
category.  It never prints the matched value.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
TEXT_SCAN_LIMIT = 5 * 1024 * 1024
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"}
RUNTIME_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".log"}
FORBIDDEN_DIRS = {"webapp_data", "tmp", "test-results", "playwright-report"}
PRIVATE_IMAGE_TERMS = re.compile(r"(?:invoice|receipt|evidence|crop)", re.IGNORECASE)
WINDOWS_ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/]")
MERGE_MARKER = re.compile(r"^(?:<{7}|>{7})(?:\s.*)?$")

SECRET_PATTERNS = (
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("anthropic-api-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("openai-api-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
)
GENERIC_QUOTED_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password)\b"
    r"\s*[:=]\s*([\"'])([^\"']+)\1"
)
GENERIC_CONFIG_SECRET_ASSIGNMENT = re.compile(
    r"(?i)^\s*(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password)"
    r"\s*[:=]\s*([^\s,#}]+)"
)
PLACEHOLDER_VALUE = re.compile(
    r"(?i)^(?:disabled|false|none|null|mock|dummy|test|redacted|example|"
    r"your[_-].*|<.*>|\.\.\.|\$\{.*\})$"
)


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    line: int
    category: str


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _tracked_paths() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return sorted(
        item.decode("utf-8", errors="replace")
        for item in result.stdout.split(b"\0")
        if item
    )


def _read_text(relative_path: str) -> str | None:
    path = ROOT / relative_path
    try:
        if not path.is_file() or path.stat().st_size > TEXT_SCAN_LIMIT:
            return None
        payload = path.read_bytes()
    except OSError:
        return None
    if b"\0" in payload:
        return None
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return payload.decode("utf-8", errors="replace")


def _forbidden_tracked_path(relative_path: str) -> str | None:
    pure = PurePosixPath(relative_path)
    lower_parts = {part.casefold() for part in pure.parts}
    name = pure.name.casefold()
    suffix = pure.suffix.casefold()
    if lower_parts & FORBIDDEN_DIRS:
        return "tracked-runtime-artifact"
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return "tracked-private-env"
    if suffix == ".pdf":
        return "tracked-private-document"
    if suffix in RUNTIME_SUFFIXES:
        return "tracked-runtime-database-or-log"
    if suffix in IMAGE_SUFFIXES and PRIVATE_IMAGE_TERMS.search(relative_path):
        return "tracked-invoice-image-or-evidence-crop"
    return None


def _is_test_path(relative_path: str) -> bool:
    parts = {part.casefold() for part in PurePosixPath(relative_path).parts}
    return "tests" in parts or "e2e" in parts or "fixtures" in parts


def _scan_text(relative_path: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    suffix = PurePosixPath(relative_path).suffix.casefold()
    generic_candidate = suffix in {".env", ".yaml", ".yml", ".json", ".toml", ".ini", ".cfg"}
    for line_number, line in enumerate(text.splitlines(), start=1):
        if MERGE_MARKER.match(line):
            findings.append(Finding(relative_path, line_number, "merge-conflict-marker"))
        for category, pattern in SECRET_PATTERNS:
            if pattern.search(line):
                findings.append(Finding(relative_path, line_number, category))
        if not _is_test_path(relative_path):
            quoted_match = GENERIC_QUOTED_SECRET_ASSIGNMENT.search(line)
            config_match = GENERIC_CONFIG_SECRET_ASSIGNMENT.search(line) if generic_candidate else None
            value = ""
            if quoted_match:
                value = quoted_match.group(2).strip()
            elif config_match:
                value = config_match.group(1).strip().strip("\"'")
            if value and len(value) >= 12 and not PLACEHOLDER_VALUE.match(value):
                findings.append(Finding(relative_path, line_number, "literal-secret-assignment"))
    return findings


def _scan_env_example() -> list[Finding]:
    relative_path = ".env.example"
    text = _read_text(relative_path)
    if text is None:
        return [Finding(relative_path, 0, "missing-sanitized-env-example")]
    findings: list[Finding] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if re.search(r"(?i)(?:key|token|secret|password|credential)", key):
            if value and not PLACEHOLDER_VALUE.match(value):
                findings.append(Finding(relative_path, line_number, "unsanitized-env-example"))
    return findings


def _valid_commit(value: str) -> bool:
    if not value or set(value) == {"0"}:
        return False
    return _git("cat-file", "-e", f"{value}^{{commit}}", check=False).returncode == 0


def _scan_added_production_lines(base: str) -> list[Finding]:
    if not _valid_commit(base):
        return []
    production_paths = ("webapp/backend", "webapp/frontend/src", "utils", "config")
    result = _git(
        "diff", "--unified=0", "--no-color", f"{base}...HEAD", "--", *production_paths
    )
    findings: list[Finding] = []
    current_path = ""
    new_line = 0
    for raw_line in result.stdout.splitlines():
        if raw_line.startswith("+++ b/"):
            current_path = raw_line[6:]
            continue
        header = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw_line)
        if header:
            new_line = int(header.group(1))
            continue
        if raw_line.startswith("+") and not raw_line.startswith("+++"):
            if current_path and WINDOWS_ABSOLUTE_PATH.search(raw_line[1:]):
                findings.append(Finding(current_path, new_line, "new-absolute-windows-path"))
            new_line += 1
        elif not raw_line.startswith("-"):
            new_line += 1
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="", help="Base commit for new source-line checks")
    args = parser.parse_args()

    findings: list[Finding] = []
    tracked = _tracked_paths()
    tracked_set = set(tracked)
    for relative_path in tracked:
        category = _forbidden_tracked_path(relative_path)
        if category:
            findings.append(Finding(relative_path, 0, category))
        text = _read_text(relative_path)
        if text is not None:
            findings.extend(_scan_text(relative_path, text))

    if ".env.example" not in tracked_set:
        findings.append(Finding(".env.example", 0, "env-example-not-tracked"))
    findings.extend(_scan_env_example())
    findings.extend(_scan_added_production_lines(args.base))

    workflow = _read_text(".github/workflows/ci.yml")
    if workflow and "secrets." in workflow:
        for line_number, line in enumerate(workflow.splitlines(), start=1):
            if "secrets." in line:
                findings.append(Finding(".github/workflows/ci.yml", line_number, "provider-secret-reference"))

    unique = sorted(set(findings))
    if unique:
        print(f"repository safety: FAIL ({len(unique)} finding(s); values redacted)")
        for finding in unique:
            print(f"{finding.path}:{finding.line}: {finding.category} [REDACTED]")
        return 1
    print(f"repository safety: PASS ({len(tracked)} repository files scanned; values never printed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
