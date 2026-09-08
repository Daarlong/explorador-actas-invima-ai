from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from check_index_pending import build_pending_status
from services.automation import pending_index_items


class AutomationTests(unittest.TestCase):
    def test_missing_report_requests_index_update(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            status = pending_index_items(Path(directory) / "missing.json")

        self.assertTrue(status["needs_update"])
        self.assertEqual(status["reason"], "missing_integrity_report")

    def test_pending_documents_request_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "integrity.json"
            path.write_text(
                json.dumps(
                    {
                        "missing_documents": ["Acta 01"],
                        "documents_without_pages": [],
                        "documents_without_chunks": [],
                    }
                ),
                encoding="utf-8",
            )
            status = pending_index_items(path)

        self.assertTrue(status["needs_update"])
        self.assertEqual(status["pending_count"], 1)

    def test_complete_report_does_not_request_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "integrity.json"
            path.write_text(
                json.dumps({key: [] for key in (
                    "missing_documents",
                    "documents_without_pages",
                    "documents_without_chunks",
                )}),
                encoding="utf-8",
            )
            status = pending_index_items(path)

        self.assertFalse(status["needs_update"])
        self.assertEqual(status["reason"], "complete")

    def test_combined_status_detects_extractor_semantic_and_package_pending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            integrity = root / "integrity.json"
            indexing = root / "indexing.json"
            semantic = root / "semantic.json"
            integrity.write_text(
                json.dumps(
                    {
                        "missing_documents": [],
                        "documents_without_pages": [],
                        "documents_without_chunks": [],
                        "regulatory_extraction_pending": ["Acta 01"],
                        "regulatory_extraction_errors": [],
                        "page_inventory_pending": [],
                        "schema_version": 7,
                        "expected_schema_version": 7,
                    }
                ),
                encoding="utf-8",
            )
            indexing.write_text(json.dumps({"documents_failed": 0}), encoding="utf-8")
            semantic.write_text(json.dumps({"status": "error"}), encoding="utf-8")

            status = build_pending_status(
                integrity,
                indexing,
                semantic,
                root / "actas.db",
                root / "semantic.db",
                semantic_enabled=True,
            )

        self.assertTrue(status["needs_update"])
        self.assertIn("regulatory_extraction_pending", status["reasons"])
        self.assertIn("semantic_index_stale", status["reasons"])
        self.assertIn("database_package_missing_or_incomplete", status["reasons"])

    def test_combined_status_accepts_complete_raw_databases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            integrity = root / "integrity.json"
            indexing = root / "indexing.json"
            semantic = root / "semantic.json"
            integrity.write_text(
                json.dumps(
                    {
                        "missing_documents": [],
                        "documents_without_pages": [],
                        "documents_without_chunks": [],
                        "regulatory_extraction_pending": [],
                        "regulatory_extraction_errors": [],
                        "page_inventory_pending": [],
                        "schema_version": 7,
                        "expected_schema_version": 7,
                    }
                ),
                encoding="utf-8",
            )
            indexing.write_text(json.dumps({"documents_failed": 0}), encoding="utf-8")
            semantic.write_text(json.dumps({"status": "built"}), encoding="utf-8")
            (root / "actas.db").write_bytes(b"database")
            (root / "semantic.db").write_bytes(b"semantic")

            status = build_pending_status(
                integrity,
                indexing,
                semantic,
                root / "actas.db",
                root / "semantic.db",
                semantic_enabled=True,
            )

        self.assertFalse(status["needs_update"])
        self.assertEqual(status["reason"], "complete")

    def test_combined_status_detects_an_outdated_neural_build(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            integrity = root / "integrity.json"
            indexing = root / "indexing.json"
            semantic = root / "semantic.json"
            integrity.write_text(
                json.dumps(
                    {
                        "missing_documents": [],
                        "documents_without_pages": [],
                        "documents_without_chunks": [],
                        "regulatory_extraction_pending": [],
                        "regulatory_extraction_errors": [],
                        "page_inventory_pending": [],
                        "schema_version": 7,
                        "expected_schema_version": 7,
                    }
                ),
                encoding="utf-8",
            )
            indexing.write_text(json.dumps({"documents_failed": 0}), encoding="utf-8")
            semantic.write_text(
                json.dumps(
                    {
                        "status": "built",
                        "method": "old",
                        "build_signature": "old",
                        "neural_status": "fallback",
                    }
                ),
                encoding="utf-8",
            )
            (root / "actas.db").write_bytes(b"database")
            (root / "semantic.db").write_bytes(b"semantic")

            status = build_pending_status(
                integrity,
                indexing,
                semantic,
                root / "actas.db",
                root / "semantic.db",
                semantic_enabled=True,
                expected_semantic_method="new",
                expected_semantic_signature="new-signature",
                require_neural=True,
            )

        self.assertTrue(status["needs_update"])
        self.assertIn("semantic_build_outdated", status["reasons"])
        self.assertIn("semantic_neural_pending", status["reasons"])


if __name__ == "__main__":
    unittest.main()
