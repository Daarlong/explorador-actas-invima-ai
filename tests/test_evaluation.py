from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.database import initialize_database, insert_document
from services.evaluation import (
    EvaluationCase,
    audit_corpus_reports,
    evaluate_cases,
    evaluation_cases_to_csv,
    evaluation_results_to_csv,
    parse_evaluation_cases_csv,
    parse_expected_reference,
    summarize_evaluation,
)
from services.models import DocumentMetadata
from services.semantic import build_semantic_index


class EvaluationBankTests(unittest.TestCase):
    def test_csv_round_trip_preserves_reproducible_fields(self) -> None:
        original = EvaluationCase(
            case_id="estabilidad-001",
            query="estudios de estabilidad",
            expected_refs=(
                "acta:2026:01:SEMPB",
                "title:Acta No 02 de 2026 SEMPB",
            ),
            filters={"years": [2026], "sections": ["SEMPB"]},
            k=5,
            exact_phrase=True,
            notes="Validado por revisión manual",
        )

        cases, errors = parse_evaluation_cases_csv(
            evaluation_cases_to_csv([original])
        )

        self.assertEqual(errors, [])
        self.assertEqual(cases, [original])

    def test_parser_skips_bad_rows_without_losing_valid_ones(self) -> None:
        content = (
            "case_id,query,expected_refs,filters_json,k,enabled\n"
            'bien,estabilidad,acta:2026:01:SEMPB,"{}",10,true\n'
            'mal,,referencia-invalida,"{}",10,true\n'
        )

        cases, errors = parse_evaluation_cases_csv(content)

        self.assertEqual([case.case_id for case in cases], ["bien"])
        self.assertEqual(len(errors), 1)
        self.assertIn("Fila 3", errors[0])

    def test_missing_file_columns_are_reported_safely(self) -> None:
        cases, errors = parse_evaluation_cases_csv("consulta,esperado\na,b\n")

        self.assertEqual(cases, [])
        self.assertIn("Faltan columnas obligatorias", errors[0])

    def test_reference_parser_normalizes_act_number(self) -> None:
        reference = parse_expected_reference("acta:2024:001:SEMPB")

        self.assertEqual(reference.year, 2024)
        self.assertEqual(reference.acta_number, "1")
        self.assertEqual(reference.section, "sempb")


class RetrievalEvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.database_path = root / "actas.db"
        self.semantic_path = root / "semantic.db"
        initialize_database(self.database_path)
        documents = [
            (
                "Acta No 01 de 2026 SEMPB",
                "01",
                "El concepto requiere presentar estudios de estabilidad acelerada.",
            ),
            (
                "Acta No 02 de 2026 SEMPB",
                "02",
                "La sala recomienda aprobar el medicamento biológico.",
            ),
            (
                "Acta No 03 de 2026 SEMPB",
                "03",
                "Se revisó el material de envase de aluminio.",
            ),
        ]
        for index, (title, acta_number, text) in enumerate(documents, start=1):
            insert_document(
                self.database_path,
                DocumentMetadata(
                    title=title,
                    url=f"https://www.invima.gov.co/biblioteca/download/{index}",
                    year=2026,
                    acta_number=acta_number,
                    section="SEMPB",
                ),
                f"hash-{index}",
                [{"page": 1, "chunks": [text]}],
            )
        build_semantic_index(
            self.database_path,
            self.semantic_path,
            lexical_dimension=64,
            semantic_dimension=32,
            min_df=1,
            max_vocabulary=500,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_all_modes_produce_standard_metrics(self) -> None:
        case = EvaluationCase(
            case_id="estabilidad",
            query="estudios estabilidad",
            expected_refs=("acta:2026:01:SEMPB",),
            filters={"years": [2026]},
            k=3,
        )

        results = evaluate_cases(
            self.database_path,
            self.semantic_path,
            [case],
            ("textual", "hybrid", "semantic"),
        )

        self.assertEqual(len(results), 3)
        self.assertTrue(all(result.hit_at_k == 1.0 for result in results))
        self.assertTrue(all(result.recall_at_k == 1.0 for result in results))
        self.assertTrue(all(result.first_relevant_rank == 1 for result in results))
        self.assertTrue(all(result.reciprocal_rank == 1.0 for result in results))
        self.assertTrue(all(result.precision_at_k == 1 / 3 for result in results))

        summary = summarize_evaluation(results)
        self.assertEqual({row["mode"] for row in summary}, set(("textual", "hybrid", "semantic")))

    def test_missing_semantic_index_records_fallback(self) -> None:
        case = EvaluationCase(
            case_id="envase",
            query="aluminio",
            expected_refs=("title:Acta No 03 de 2026 SEMPB",),
            filters={},
            k=2,
        )

        result = evaluate_cases(
            self.database_path,
            self.semantic_path.with_name("missing.db"),
            [case],
            ("semantic",),
        )[0]

        self.assertEqual(result.requested_mode, "semantic")
        self.assertEqual(result.used_mode, "textual")
        self.assertEqual(result.hit_at_k, 1.0)

    def test_url_reference_accepts_original_manifest_url(self) -> None:
        root = Path(self.temporary.name)
        database = root / "redirected.db"
        initialize_database(database)
        original_url = "https://www.invima.gov.co/biblioteca/detail/acta-01"
        resolved_url = "https://www.invima.gov.co/biblioteca/download/99"
        insert_document(
            database,
            DocumentMetadata(
                title="Acta con redirección",
                url=resolved_url,
                year=2026,
                acta_number="09",
                section="SEMPB",
            ),
            "redirect-hash",
            [{"page": 1, "chunks": ["concepto de estabilidad especial"]}],
            manifest_url=original_url,
        )
        case = EvaluationCase(
            case_id="url-original",
            query="estabilidad especial",
            expected_refs=(f"url:{original_url}",),
            filters={},
            k=1,
        )

        result = evaluate_cases(
            database,
            root / "missing-semantic.db",
            [case],
            ("textual",),
        )[0]

        self.assertEqual(result.hit_at_k, 1.0)

    def test_results_export_quotes_formula_like_content(self) -> None:
        case = EvaluationCase(
            case_id="formula",
            query="=HYPERLINK(\"https://example.test\")",
            expected_refs=("acta:2026:01:SEMPB",),
            filters={},
            k=1,
        )
        result = evaluate_cases(
            self.database_path,
            self.semantic_path,
            [case],
            ("textual",),
        )[0]

        exported = evaluation_results_to_csv([result])
        row = next(csv.DictReader(io.StringIO(exported)))

        self.assertTrue(row["consulta"].startswith("'="))


class CorpusAuditTests(unittest.TestCase):
    def test_missing_reports_return_actionable_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audit = audit_corpus_reports(
                root / "integrity.json",
                root / "indexing.json",
                root / "semantic.json",
            )

        self.assertEqual(audit["status"], "unavailable")
        self.assertFalse(audit["integrity_available"])
        self.assertEqual(audit["age_hours"], None)

    def test_audit_reports_coverage_ocr_missing_and_age(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime(2026, 9, 2, 12, tzinfo=timezone.utc)
            integrity = {
                "status": "warning",
                "generated_at": (now - timedelta(days=10)).isoformat(),
                "manifest_documents": 4,
                "indexed_documents": 3,
                "missing_documents": ["Acta faltante"],
                "unexpected_documents": [],
                "documents_without_pages": [],
                "documents_without_chunks": [],
                "ocr_candidates": [{"title": "Acta escaneada", "pages": [2, 3]}],
                "page_inventory_pending": [],
                "sqlite_integrity": "ok",
                "text_page_coverage_percent": 98.5,
                "coverage_by_year": [
                    {
                        "year": 2026,
                        "manifest_documents": 4,
                        "indexed_documents": 3,
                        "missing_documents": 1,
                        "coverage_percent": 75.0,
                    }
                ],
                "regulatory_extraction_pending": [],
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

            audit = audit_corpus_reports(
                root / "integrity.json",
                root / "indexing.json",
                root / "semantic.json",
                now=now,
            )

        self.assertEqual(audit["status"], "error")
        self.assertEqual(audit["document_coverage_percent"], 75.0)
        self.assertEqual(audit["ocr_candidate_pages"], 2)
        self.assertEqual(audit["freshness"], "aging")
        self.assertEqual(audit["age_hours"], 240.0)
        self.assertEqual(audit["incomplete_years"][0]["year"], 2026)

    def test_layered_coverage_requires_a_valid_source_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime(2026, 9, 2, 12, tzinfo=timezone.utc)
            integrity = {
                "status": "ok",
                "generated_at": now.isoformat(),
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
                "regulatory_extraction_pending": [],
                "regulatory_extraction_errors": [],
            }
            catalog = {
                "source_checked": True,
                "discovered_records": 100,
                "catalog_records": 100,
                "currently_listed": 100,
                "manifest_documents": 90,
                "records_without_url": 10,
                "shared_url_conflicts": [],
            }
            snapshot = {
                "status": "valid",
                "generated_at": now.isoformat(),
                "parser_version": "catalog-parser-v2",
                "html_bytes": 12345,
                "html_sha256": "a" * 64,
                "discovered_records": 100,
                "records_without_url": 10,
                "years": {"2013": 4, "2026": 8},
            }
            for name, payload in (
                ("integrity.json", integrity),
                ("indexing.json", {"documents_failed": 0}),
                ("semantic.json", {"status": "built"}),
                ("catalog.json", catalog),
                ("snapshot.json", snapshot),
            ):
                (root / name).write_text(json.dumps(payload), encoding="utf-8")

            audit = audit_corpus_reports(
                root / "integrity.json",
                root / "indexing.json",
                root / "semantic.json",
                catalog_report_path=root / "catalog.json",
                source_snapshot_path=root / "snapshot.json",
                now=now,
            )

        layers = {item["key"]: item for item in audit["coverage_layers"]}
        self.assertTrue(audit["source_snapshot_valid"])
        self.assertEqual(layers["source_catalog"]["coverage_percent"], 100.0)
        self.assertEqual(layers["catalog_manifest"]["coverage_percent"], 90.0)
        self.assertEqual(layers["manifest_index"]["coverage_percent"], 100.0)
        self.assertEqual(layers["pages"]["coverage_percent"], 100.0)
        self.assertEqual(layers["catalog_manifest"]["status"], "incomplete")
        self.assertTrue(
            any(
                item["detail"]
                == "Entradas del catálogo que no llegaron al manifiesto"
                for item in audit["issues"]
            )
        )

    def test_invalid_snapshot_never_marks_source_as_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime(2026, 9, 2, 12, tzinfo=timezone.utc)
            integrity = {
                "generated_at": now.isoformat(),
                "manifest_documents": 1,
                "indexed_documents": 1,
                "missing_documents": [],
                "unexpected_documents": [],
                "documents_without_pages": [],
                "documents_without_chunks": [],
                "ocr_candidates": [],
                "page_inventory_pending": [],
                "sqlite_integrity": "ok",
                "pages_indexed": 1,
                "pdf_pages": 1,
                "text_page_coverage_percent": 100.0,
                "coverage_by_year": [],
                "regulatory_extraction_pending": [],
                "regulatory_extraction_errors": [],
            }
            catalog = {
                "source_checked": True,
                "discovered_records": 1,
                "catalog_records": 1,
                "currently_listed": 1,
                "manifest_documents": 1,
            }
            invalid_snapshot = {
                "status": "valid",
                "generated_at": now.isoformat(),
                "parser_version": "catalog-parser-v2",
                "html_bytes": 20,
                "html_sha256": "no-es-un-sha256",
                "discovered_records": 1,
            }
            for name, payload in (
                ("integrity.json", integrity),
                ("indexing.json", {"documents_failed": 0}),
                ("semantic.json", {"status": "built"}),
                ("catalog.json", catalog),
                ("snapshot.json", invalid_snapshot),
            ):
                (root / name).write_text(json.dumps(payload), encoding="utf-8")

            audit = audit_corpus_reports(
                root / "integrity.json",
                root / "indexing.json",
                root / "semantic.json",
                catalog_report_path=root / "catalog.json",
                source_snapshot_path=root / "snapshot.json",
                now=now,
            )

        source_layer = next(
            item for item in audit["coverage_layers"]
            if item["key"] == "source_catalog"
        )
        self.assertFalse(audit["source_snapshot_valid"])
        self.assertEqual(source_layer["status"], "unverified")
        self.assertIsNone(source_layer["coverage_percent"])
        self.assertEqual(audit["status"], "error")
        self.assertTrue(
            any(item["category"] == "source" for item in audit["issues"])
        )

    def test_malformed_snapshot_is_handled_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime(2026, 9, 2, 12, tzinfo=timezone.utc)
            integrity = {
                "generated_at": now.isoformat(),
                "manifest_documents": 1,
                "indexed_documents": 1,
                "missing_documents": [],
                "unexpected_documents": [],
                "documents_without_pages": [],
                "documents_without_chunks": [],
                "ocr_candidates": [],
                "page_inventory_pending": [],
                "sqlite_integrity": "ok",
                "pages_indexed": 1,
                "pdf_pages": 1,
                "text_page_coverage_percent": 100.0,
                "coverage_by_year": [],
                "regulatory_extraction_pending": [],
                "regulatory_extraction_errors": [],
            }
            (root / "integrity.json").write_text(
                json.dumps(integrity), encoding="utf-8"
            )
            (root / "catalog.json").write_text(
                json.dumps(
                    {
                        "source_checked": True,
                        "discovered_records": "valor-invalido",
                        "catalog_records": "valor-invalido",
                    }
                ),
                encoding="utf-8",
            )
            (root / "snapshot.json").write_text(
                json.dumps(
                    {
                        "status": "valid",
                        "generated_at": now.isoformat(),
                        "parser_version": "v2",
                        "html_bytes": "valor-invalido",
                        "html_sha256": "a" * 64,
                        "discovered_records": "valor-invalido",
                    }
                ),
                encoding="utf-8",
            )

            audit = audit_corpus_reports(
                root / "integrity.json",
                root / "missing-indexing.json",
                root / "missing-semantic.json",
                catalog_report_path=root / "catalog.json",
                source_snapshot_path=root / "snapshot.json",
                now=now,
            )

        self.assertEqual(audit["status"], "error")
        self.assertFalse(audit["source_snapshot_valid"])
        self.assertIsNone(audit["source_catalog_percent"])


if __name__ == "__main__":
    unittest.main()
