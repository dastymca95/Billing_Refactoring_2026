from __future__ import annotations

import unittest
from unittest.mock import patch

from webapp.backend.services.accounting_readiness import CONTRACT_VERSION, evaluate_and_record, evaluate_rows
from webapp.backend.services import batch_processor


VALID_GLS = {"6100": "Repairs"}
REQUIRED = ["Invoice Number", "Vendor", "Property Abbreviation", "GL Account", "Amount"]


def row(**updates):
    value = {
        "Invoice Number": "INV-1",
        "Vendor": "Example Vendor",
        "Property Abbreviation": "PROP",
        "GL Account": "6100",
        "Amount": 25.0,
        "_meta": {
            "invoice_group_id": "invoice-1",
            "ai_provenance": {"invoice_total": 25.0},
            "total_reconciliation_passed": True,
        },
    }
    value.update(updates)
    return value


class AccountingReadinessTests(unittest.TestCase):
    def evaluate(self, rows):
        with patch("webapp.backend.services.accounting_readiness.get_template_rules", return_value={"required_columns": REQUIRED}), patch(
            "webapp.backend.services.accounting_readiness.load_chart_of_accounts", return_value=VALID_GLS
        ):
            return evaluate_rows(rows)

    def test_ready_contract_is_versioned_and_deterministic(self):
        first = self.evaluate([row()])
        second = self.evaluate([row()])
        self.assertTrue(first.export_allowed)
        self.assertEqual(first.contract_version, CONTRACT_VERSION)
        self.assertEqual(first.snapshot_id, second.snapshot_id)

    def test_each_export_critical_field_blocks(self):
        cases = [
            ("Property Abbreviation", "", "required_field_missing:Property Abbreviation"),
            ("GL Account", "", "required_field_missing:GL Account"),
            ("GL Account", "9999", "gl_invalid"),
            ("Amount", "not-money", "amount_invalid"),
        ]
        for field, value, code in cases:
            with self.subTest(field=field, value=value):
                decision = self.evaluate([row(**{field: value})])
                self.assertFalse(decision.export_allowed)
                self.assertIn(code, {issue.code for issue in decision.blockers})

    def test_total_mismatch_blocks(self):
        bad = row(Amount=24.0)
        bad["_meta"]["total_reconciliation_passed"] = False
        decision = self.evaluate([bad])
        self.assertFalse(decision.export_allowed)
        self.assertEqual(decision.reconciliation_status, "failed")
        self.assertIn("total_mismatch", {issue.code for issue in decision.blockers})

    def test_vision_warning_is_non_blocking_and_confidence_is_ignored(self):
        warned = row()
        warned["_meta"].update({"ai_warnings": ["blurred page"], "ai_confidence": 0.01})
        decision = self.evaluate([warned])
        self.assertTrue(decision.export_allowed)
        self.assertEqual(decision.status.value, "needs_review")
        self.assertEqual(decision.non_blocking_issues[0].source, "vision_or_ocr")

    def test_unresolved_duplicate_blocks(self):
        with patch("webapp.backend.services.accounting_readiness.get_template_rules", return_value={"required_columns": REQUIRED}), patch(
            "webapp.backend.services.accounting_readiness.load_chart_of_accounts", return_value=VALID_GLS
        ):
            decision = evaluate_rows([row()], duplicate_status="unresolved")
        self.assertFalse(decision.export_allowed)
        self.assertIn("duplicate_unresolved", {issue.code for issue in decision.blockers})

    def test_backend_records_evidence_when_a_blocker_is_resolved(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp, patch(
            "webapp.backend.services.accounting_readiness.get_template_rules", return_value={"required_columns": REQUIRED}
        ), patch("webapp.backend.services.accounting_readiness.load_chart_of_accounts", return_value=VALID_GLS), patch(
            "webapp.backend.services.batch_store.get_batch_dir", return_value=Path(tmp)
        ):
            evaluate_and_record("batch", [row(**{"GL Account": ""})])
            resolved = evaluate_and_record("batch", [row()])
        evidence = [issue for issue in resolved.non_blocking_issues if issue.resolved]
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].resolved_by, "backend_validation")
        self.assertEqual(evidence[0].resolution_evidence["validation"], "blocker_condition_no_longer_present")

    def test_opaque_legacy_workbook_is_disabled_instead_of_copied(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "processed" / "legacy_vendor" / "legacy_resman_import_20200101.xlsx"
            legacy.parent.mkdir(parents=True)
            legacy.write_bytes(b"opaque legacy workbook")
            (root / "export").mkdir()
            with patch("webapp.backend.services.batch_store.get_batch_dir", return_value=root), patch(
                "webapp.backend.services.batch_store.get_export_dir", return_value=root / "export"
            ):
                result = batch_processor.export_batch("batch")
            self.assertEqual(result["reason"], "legacy_export_disabled")
            self.assertEqual(list((root / "export").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
