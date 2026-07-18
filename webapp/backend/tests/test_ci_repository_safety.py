from __future__ import annotations

import importlib
import os
from types import SimpleNamespace

import pytest
import yaml

from scripts import ci_repository_safety as safety
from scripts import ci_verify_discovery as discovery


@pytest.mark.parametrize(
    ("path", "category"),
    [
        (".env", "tracked-private-env"),
        ("webapp_data/batches/runtime.json", "tracked-runtime-artifact"),
        ("tmp/result.json", "tracked-runtime-artifact"),
        ("webapp/frontend/test-results/result.json", "tracked-runtime-artifact"),
        ("playwright-report/index.html", "tracked-runtime-artifact"),
        ("private/invoice.pdf", "tracked-private-document"),
        ("runtime/state.sqlite3", "tracked-runtime-database-or-log"),
        ("evidence/row-crop.png", "tracked-invoice-image-or-evidence-crop"),
    ],
)
def test_forbidden_private_or_runtime_paths(path: str, category: str) -> None:
    assert safety._forbidden_tracked_path(path) == category


@pytest.mark.parametrize(
    "path",
    [
        ".env.example",
        "webapp/frontend/src/assets/logo.png",
        "webapp/backend/tests/fixtures/public-safe.json",
    ],
)
def test_safe_tracked_paths_remain_allowed(path: str) -> None:
    assert safety._forbidden_tracked_path(path) is None


def test_secret_scan_reports_category_without_secret_value() -> None:
    synthetic = "OPENAI_API_KEY=" + "sk-" + ("A" * 32)
    findings = safety._scan_text("config/runtime.yml", synthetic)

    assert any(item.category == "openai-api-key" for item in findings)
    assert all("sk-" not in repr(item) for item in findings)


def test_environment_lookup_is_not_mistaken_for_literal_secret() -> None:
    findings = safety._scan_text(
        "webapp/backend/services/example.py",
        'api_key = os.environ.get("AI_API_KEY", "")',
    )
    assert not findings


def test_merge_markers_are_typed_findings() -> None:
    findings = safety._scan_text("webapp/backend/example.py", "<<<<<<< HEAD\nvalue = 1\n")
    assert [item.category for item in findings] == ["merge-conflict-marker"]


def test_new_windows_paths_are_reported_only_in_production_source(monkeypatch) -> None:
    diff = """\
+++ b/webapp/backend/tests/test_paths.py
@@ -0,0 +1 @@
+sample = r\"C:\\private\\fixture.pdf\"
+++ b/webapp/backend/services/unsafe.py
@@ -0,0 +1 @@
+runtime_path = r\"C:\\private\\runtime.pdf\"
"""
    monkeypatch.setattr(safety, "_valid_commit", lambda _base: True)
    monkeypatch.setattr(safety, "_git", lambda *_args: SimpleNamespace(stdout=diff))

    findings = safety._scan_added_production_lines("base")

    assert [(item.path, item.category) for item in findings] == [
        ("webapp/backend/services/unsafe.py", "new-absolute-windows-path")
    ]


def test_sanitized_u4_fixture_has_exact_discovery_contract() -> None:
    discovery._validate_u4_fixture(10)


def test_ci_vendor_configs_contain_only_minimal_public_safe_metadata() -> None:
    root = (
        safety.ROOT
        / "webapp/backend/tests/fixtures/runtime_assets/config/vendors"
    )
    files = sorted(root.glob("*.yaml"))
    assert len(files) == 4
    for path in files:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert set(payload) <= {"vendor_identity", "document_detection"}
        serialized = path.read_text(encoding="utf-8").casefold()
        for forbidden in ("account_number", "invoice_number", "amount", "property", "gl_code"):
            assert forbidden not in serialized


def test_ci_processor_stubs_are_import_only() -> None:
    if os.environ.get("INNER_VIEW_CI") != "1":
        pytest.skip("CI-only import stubs are intentionally inactive locally")
    module = importlib.import_module("process_richmond_utilities")
    with pytest.raises(RuntimeError, match="CI import-only processor stub cannot execute"):
        module.process_richmond_utilities_batch("not-a-real-batch")
