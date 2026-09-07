from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.corpus_status import corpus_status_from_reports


class CorpusStatusTests(unittest.TestCase):
    def test_application_and_workflows_do_not_connect_evaluation_module(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.assertNotIn(
            "pages/6_Evaluacion.py",
            (root / "home.py").read_text(encoding="utf-8"),
        )
        for workflow_name in ("build-index.yml", "reprocess-corpus.yml"):
            workflow = (
                root / ".github/workflows" / workflow_name
            ).read_text(encoding="utf-8")
            self.assertNotIn("evaluate_search.py", workflow)
            self.assertNotIn("evaluation-report.json", workflow)

    def test_missing_integrity_report_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = corpus_status_from_reports(
                root / "integrity.json",
                root / "indexing.json",
                root / "semantic.json",
            )

        self.assertEqual(status["status"], "unavailable")
        self.assertFalse(status["integrity_available"])

    def test_status_reports_technical_coverage_without_search_evaluation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime(2026, 9, 4, 12, tzinfo=timezone.utc)
            integrity = {
                "generated_at": now.isoformat(),
                "manifest_documents": 4,
                "indexed_documents": 3,
                "missing_documents": ["Acta faltante"],
                "unexpected_documents": [],
                "documents_without_pages": [],
                "documents_without_chunks": [],
                "ocr_candidates": [{"title": "Acta", "pages": [2, 3]}],
                "page_inventory_pending": [],
                "sqlite_integrity": "ok",
                "pages_indexed": 99,
                "pdf_pages": 100,
                "text_page_coverage_percent": 99.0,
                "coverage_by_year": [
                    {
                        "year": 2026,
                        "manifest_documents": 4,
                        "indexed_documents": 3,
                        "missing_documents": 1,
                        "coverage_percent": 75.0,
                    }
                ],
                "regulatory_extraction_errors": [],
            }
            (root / "integrity.json").write_text(
                json.dumps(integrity), encoding="utf-8"
            )
            (root / "indexing.json").write_text(
                json.dumps({"documents_failed": 0}), encoding="utf-8"
            )
            (root / "semantic.json").write_text(
                json.dumps({"status": "built"}), encoding="utf-8"
            )

            status = corpus_status_from_reports(
                root / "integrity.json",
                root / "indexing.json",
                root / "semantic.json",
                now=now,
            )

        self.assertEqual(status["status"], "error")
        self.assertEqual(status["document_coverage_percent"], 75.0)
        self.assertEqual(status["ocr_candidate_pages"], 2)
        self.assertNotIn("quality_gate", status)

    def test_valid_source_snapshot_builds_layered_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime(2026, 9, 4, 12, tzinfo=timezone.utc)
            payloads = {
                "integrity.json": {
                    "generated_at": (now - timedelta(hours=1)).isoformat(),
                    "manifest_documents": 90,
                    "indexed_documents": 90,
                    "missing_documents": [],
                    "unexpected_documents": [],
                    "documents_without_pages": [],
                    "documents_without_chunks": [],
                    "ocr_candidates": [],
                    "page_inventory_pending": [],
                    "sqlite_integrity": "ok",
                    "pages_indexed": 1000,
                    "pdf_pages": 1000,
                    "text_page_coverage_percent": 100.0,
                    "coverage_by_year": [],
                    "regulatory_extraction_errors": [],
                },
                "indexing.json": {"documents_failed": 0},
                "semantic.json": {"status": "built"},
                "catalog.json": {
                    "source_checked": True,
                    "discovered_records": 100,
                    "catalog_records": 100,
                    "currently_listed": 100,
                    "manifest_documents": 90,
                    "records_without_url": 10,
                    "shared_url_conflicts": [],
                },
                "snapshot.json": {
                    "status": "valid",
                    "generated_at": now.isoformat(),
                    "parser_version": "catalog-parser-v2",
                    "html_bytes": 12345,
                    "html_sha256": "a" * 64,
                    "discovered_records": 100,
                },
            }
            for name, payload in payloads.items():
                (root / name).write_text(json.dumps(payload), encoding="utf-8")

            status = corpus_status_from_reports(
                root / "integrity.json",
                root / "indexing.json",
                root / "semantic.json",
                catalog_report_path=root / "catalog.json",
                source_snapshot_path=root / "snapshot.json",
                now=now,
            )

        layers = {item["key"]: item for item in status["coverage_layers"]}
        self.assertTrue(status["source_snapshot_valid"])
        self.assertEqual(layers["source_catalog"]["coverage_percent"], 100.0)
        self.assertEqual(layers["catalog_manifest"]["coverage_percent"], 90.0)
        self.assertEqual(layers["manifest_index"]["coverage_percent"], 100.0)


if __name__ == "__main__":
    unittest.main()
