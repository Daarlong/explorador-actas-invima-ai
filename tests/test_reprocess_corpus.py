from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from reprocess_corpus import (
    REQUIRED_COMPLETENESS_FIELDS,
    _compare_required_field_completeness,
    _database_field_completeness,
    _database_missing_decision_uids,
    _file_snapshot,
    _page_inventory_gate_passes,
    _review_reconciliation,
    _safe_workspace,
    _source_fingerprint,
    _source_text_inventory,
    _atomic_json,
    build_candidate_batches,
    export_workflow_reports,
    main,
    prepare_workspace,
    publish_candidate,
)
from services.database import initialize_database, insert_document
from services.indexing import IndexingReport
from services.models import DocumentMetadata
from services.reviews import ReviewEvent, serialize_review_events


class ReprocessCorpusTests(unittest.TestCase):
    def _project_temporary(self):
        from config import ROOT_DIR

        test_root = ROOT_DIR / ".reprocess" / "tests"
        test_root.mkdir(parents=True, exist_ok=True)
        return tempfile.TemporaryDirectory(dir=test_root)

    def test_prepare_restores_published_database_and_snapshots_review_log(self) -> None:
        with self._project_temporary() as directory:
            root = Path(directory)
            published = root / "published"
            published.mkdir()
            initialize_database(published / "actas.db")
            manifest = root / "manifest.csv"
            manifest.write_text(
                "title,url,year,acta_number,section\n"
                "Acta 01,https://www.invima.gov.co/biblioteca/download/1,"
                "2026,01,SEMPB\n",
                encoding="utf-8",
            )
            review = root / "reviews.csv"
            review.write_text("", encoding="utf-8")
            workspace = root / "work"

            state = prepare_workspace(
                workspace,
                published_data=published,
                manifest_path=manifest,
                review_log_path=review,
                min_free_mib=1,
            )

            self.assertTrue((workspace / "baseline/data/actas.db").exists())
            self.assertEqual(state["manifest_documents"], 1)
            self.assertTrue(state["review_log"]["exists"])
            self.assertFalse(state["resumed"])

    def test_resume_restores_semantic_and_rejects_changed_baseline(self) -> None:
        with self._project_temporary() as directory:
            root = Path(directory)
            published = root / "published"
            published.mkdir()
            initialize_database(published / "actas.db")
            (published / "semantic.db").write_bytes(b"semantic-baseline")
            manifest = root / "manifest.csv"
            manifest.write_text(
                "title,url,year,acta_number,section\n"
                "Acta 01,https://www.invima.gov.co/biblioteca/download/1,"
                "2026,01,SEMPB\n",
                encoding="utf-8",
            )
            review = root / "reviews.csv"
            review.write_text("", encoding="utf-8")
            initial_workspace = root / "initial"
            state = prepare_workspace(
                initial_workspace,
                published_data=published,
                manifest_path=manifest,
                review_log_path=review,
                min_free_mib=1,
            )

            resume = root / "resume"
            (resume / "candidate/data").mkdir(parents=True)
            shutil.copy2(initial_workspace / "run-state.json", resume / "run-state.json")
            initialize_database(resume / "candidate/data/actas.db")
            (resume / "candidate/data/semantic.db").write_bytes(
                b"semantic-candidate"
            )
            _atomic_json(
                resume / "candidate/checkpoint.json",
                {
                    "format_version": state["format_version"],
                    "source_fingerprint": state["source_fingerprint"],
                    "baseline_fingerprint": state["baseline_fingerprint"],
                    "manifest_documents": 1,
                    "documents_completed": 0,
                    "complete": False,
                },
            )

            resumed_workspace = root / "resumed"
            resumed = prepare_workspace(
                resumed_workspace,
                published_data=published,
                manifest_path=manifest,
                review_log_path=review,
                min_free_mib=1,
                resume_from=resume,
            )
            self.assertTrue(resumed["resumed"])
            self.assertTrue(resumed["resumed_semantic"])
            self.assertEqual(
                (resumed_workspace / "candidate/data/semantic.db").read_bytes(),
                b"semantic-candidate",
            )

            with sqlite3.connect(published / "actas.db") as connection:
                connection.execute("CREATE TABLE baseline_changed (id INTEGER)")
            with self.assertRaisesRegex(ValueError, "base publicada cambió"):
                prepare_workspace(
                    root / "changed-baseline",
                    published_data=published,
                    manifest_path=manifest,
                    review_log_path=review,
                    min_free_mib=1,
                    resume_from=resume,
                )

    def test_workflow_reports_export_technical_status_and_advisories(self) -> None:
        with self._project_temporary() as directory:
            workspace = Path(directory) / "workflow-report"
            (workspace / "candidate/data").mkdir(parents=True)
            (workspace / "baseline/data").mkdir(parents=True)
            _atomic_json(
                workspace / "reprocess-report.json",
                {
                    "status": "rejected",
                    "mode": "diagnostic",
                    "candidate": {"documents": 638, "pages": 130809},
                    "issues": [
                        {
                            "code": "candidate_integrity",
                            "message": "La candidata no supera la integridad",
                        }
                    ],
                    "advisories": [
                        {
                            "code": "field_regression",
                            "message": "Cambió un campo derivado.",
                        }
                    ],
                },
            )
            _atomic_json(
                workspace / "candidate/data/integrity-report.json",
                {"origin": "candidate"},
            )
            summary = Path(directory) / "github-summary.md"
            output = StringIO()
            with redirect_stdout(output):
                result = export_workflow_reports(
                    workspace,
                    github_summary_path=summary,
                )

            exported = workspace / "export"
            self.assertEqual(
                json.loads(
                    (exported / "candidate-integrity-report.json").read_text(
                        encoding="utf-8"
                    )
                )["origin"],
                "candidate",
            )
            self.assertNotIn("candidate-evaluation-report.json", result["files"])
            self.assertNotIn("baseline-evaluation-report.json", result["files"])
            self.assertEqual(result["advisories"], 1)
            self.assertIn("::warning title=La candidata no está aprobada", output.getvalue())
            summary_text = summary.read_text(encoding="utf-8")
            self.assertIn("No publiques esta candidata", summary_text)
            self.assertIn("candidate_integrity", summary_text)

    def test_field_completeness_is_calculated_from_database(self) -> None:
        with self._project_temporary() as directory:
            database = Path(directory) / "actas.db"
            initialize_database(database)
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    INSERT INTO documents (
                        title, normalized_title, url, manifest_url, source_type,
                        document_hash, pdf_page_count, indexed_page_count,
                        ocr_candidate_pages, page_inventory_complete, indexed_at
                    ) VALUES ('Acta', 'acta', 'https://example.test/1',
                              'https://example.test/1', 'official', 'hash',
                              1, 1, '', 1, '2026-01-01T00:00:00+00:00')
                    """
                )
                document_id = connection.execute(
                    "SELECT id FROM documents"
                ).fetchone()[0]
                connection.execute(
                    """
                    INSERT INTO regulatory_records (
                        document_id, record_key, decision_uid, page_number,
                        end_page_number, numeral, product_name, active_ingredient,
                        interested_party, expediente, radicado,
                        outcome_code, extraction_method, extractor_version, created_at
                    ) VALUES (?, 'key', 'uid', 1, 1, '3.1', 'Ozempic',
                              NULL, 'Novo Nordisk', 'EXP-1', NULL,
                              'aprobado', 'explicit', '4',
                              '2026-01-01T00:00:00+00:00')
                    """,
                    (document_id,),
                )

            metrics = _database_field_completeness(database)

            self.assertEqual(metrics["product"]["coverage_percent"], 100.0)
            self.assertEqual(metrics["active_ingredient"]["coverage_percent"], 0.0)
            self.assertEqual(metrics["interested_party"]["coverage_percent"], 100.0)
            self.assertEqual(metrics["expediente"]["coverage_percent"], 100.0)
            self.assertEqual(metrics["radicado"]["coverage_percent"], 0.0)
            self.assertEqual(metrics["identifiers"]["coverage_percent"], 100.0)
            self.assertEqual(metrics["page_range"]["populated"], 1)
            self.assertEqual(metrics["concept"]["coverage_percent"], 0.0)
            self.assertEqual(metrics["outcome"]["coverage_percent"], 100.0)

            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    UPDATE regulatory_records
                    SET expediente = NULL, radicado = 'RAD-1',
                        concept_text = 'La Sala requiere información.',
                        outcome_code = 'sin_clasificar'
                    """
                )

            metrics = _database_field_completeness(database)

            self.assertEqual(metrics["expediente"]["coverage_percent"], 0.0)
            self.assertEqual(metrics["radicado"]["coverage_percent"], 100.0)
            self.assertEqual(metrics["identifiers"]["coverage_percent"], 100.0)
            self.assertEqual(metrics["concept"]["coverage_percent"], 100.0)
            self.assertEqual(metrics["outcome"]["coverage_percent"], 0.0)

    def test_completeness_advisory_covers_structured_fields(self) -> None:
        self.assertEqual(
            REQUIRED_COMPLETENESS_FIELDS,
            (
                "product",
                "active_ingredient",
                "numeral",
                "interested_party",
                "expediente",
                "radicado",
                "identifiers",
                "page_range",
                "concept",
                "outcome",
            ),
        )
        baseline = {
            field: {"coverage_percent": 90.0, "populated": 90}
            for field in REQUIRED_COMPLETENESS_FIELDS
        }
        candidate = {
            "producto": {"coverage_percent": 90.0, "populated": 90},
            "principios_activos": {
                "coverage_percent": 90.0,
                "populated": 90,
            },
            "numeral": {"coverage_percent": 90.0, "populated": 90},
            "interesado": {"coverage_percent": 80.0, "populated": 80},
            "expediente": {"coverage_percent": 90.0, "populated": 90},
            "radicado": {"coverage_percent": 70.0, "populated": 70},
            "identificadores": {"coverage_percent": 85.0, "populated": 85},
            "rango_paginas": {"coverage_percent": 90.0, "populated": 90},
            "concepto": {"coverage_percent": 80.0, "populated": 80},
            "resultado_normalizado": {
                "coverage_percent": 75.0,
                "populated": 75,
            },
        }

        summary, issues = _compare_required_field_completeness(
            candidate, baseline
        )

        self.assertEqual(set(summary), set(REQUIRED_COMPLETENESS_FIELDS))
        self.assertEqual(
            [issue["message"] for issue in issues],
            [
                "La completitud de interested_party disminuyó",
                "La completitud de radicado disminuyó",
                "La completitud de identifiers disminuyó",
                "La completitud de concept disminuyó",
                "La completitud de outcome disminuyó",
            ],
        )

    def test_review_reconciliation_allows_record_key_change_with_same_pdf(self) -> None:
        with self._project_temporary() as directory:
            root = Path(directory)
            database = root / "actas.db"
            initialize_database(database)
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    INSERT INTO documents (
                        title, normalized_title, url, manifest_url, source_type,
                        document_hash, pdf_page_count, indexed_page_count,
                        ocr_candidate_pages, page_inventory_complete, indexed_at
                    ) VALUES ('Acta', 'acta', 'https://example.test/1',
                              'https://example.test/1', 'official', 'pdf-hash',
                              1, 1, '', 1, '2026-01-01T00:00:00+00:00')
                    """
                )
                document_id = connection.execute(
                    "SELECT id FROM documents"
                ).fetchone()[0]
                connection.execute(
                    """
                    INSERT INTO regulatory_records (
                        document_id, record_key, decision_uid, page_number,
                        end_page_number, outcome_code, extraction_method,
                        extractor_version, created_at
                    ) VALUES (?, 'new-key', 'stable-uid', 1, 1, 'aprobado',
                              'explicit', '4', '2026-01-01T00:00:00+00:00')
                    """,
                    (document_id,),
                )
            review_path = root / "reviews.csv"
            review_path.write_text(
                serialize_review_events(
                    [
                        ReviewEvent(
                            event_id="event-1",
                            decision_uid="stable-uid",
                            source_record_key="old-key",
                            source_document_hash="pdf-hash",
                            status="approved",
                            reviewer="Revisor",
                            reviewed_at="2026-01-01T00:00:00+00:00",
                            notes="",
                            corrections={"active_ingredient": "Semaglutida"},
                        )
                    ]
                ),
                encoding="utf-8",
            )

            summary = _review_reconciliation(database, review_path)

            self.assertEqual(summary["reconciled"], 1)
            self.assertEqual(summary["needs_reconfirmation"], 1)
            self.assertEqual(summary["orphaned"], 0)
            self.assertEqual(summary["ambiguous"], 0)

    def test_page_inventory_gate_accepts_recorded_failure_not_missing_page(self) -> None:
        with self._project_temporary() as directory:
            root = Path(directory)
            complete = root / "complete.db"
            initialize_database(complete)
            insert_document(
                complete,
                DocumentMetadata(title="Acta", url="https://example.test/1"),
                "hash",
                [
                    {
                        "page": 1,
                        "text": "Texto fuente",
                        "text_quality": 1.0,
                        "text_extractor_version": "pymupdf-text-v1",
                        "chunks": ["Texto fuente"],
                    },
                    {
                        "page": 2,
                        "text": None,
                        "text_quality": 0.0,
                        "extraction_error": "OCR falló",
                        "text_extractor_version": (
                            "pymupdf-text-v1+tesseract-ocr-v1"
                        ),
                        "chunks": [],
                    },
                ],
                pdf_page_count=2,
                possible_scans=[2],
            )
            complete_snapshot = _source_text_inventory(complete)

            incomplete = root / "incomplete.db"
            initialize_database(incomplete)
            insert_document(
                incomplete,
                DocumentMetadata(title="Acta", url="https://example.test/2"),
                "hash-2",
                [
                    {
                        "page": 1,
                        "text": "Texto fuente",
                        "text_quality": 1.0,
                        "text_extractor_version": "pymupdf-text-v1",
                        "chunks": ["Texto fuente"],
                    }
                ],
                pdf_page_count=2,
            )
            incomplete_snapshot = _source_text_inventory(incomplete)

        self.assertTrue(_page_inventory_gate_passes(complete_snapshot))
        self.assertEqual(complete_snapshot["pages_with_failed_extraction"], 1)
        self.assertFalse(_page_inventory_gate_passes(incomplete_snapshot))
        self.assertEqual(incomplete_snapshot["pages_unaccounted"], 1)

    def test_missing_decision_uid_check_fails_closed(self) -> None:
        with self._project_temporary() as directory:
            database = Path(directory) / "actas.db"
            initialize_database(database)
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    INSERT INTO documents (
                        title, normalized_title, url, manifest_url, source_type,
                        document_hash, pdf_page_count, indexed_page_count,
                        ocr_candidate_pages, page_inventory_complete, indexed_at
                    ) VALUES ('Acta', 'acta', 'https://example.test/1',
                              'https://example.test/1', 'official', 'hash',
                              1, 1, '', 1, '2026-01-01T00:00:00+00:00')
                    """
                )
                document_id = connection.execute(
                    "SELECT id FROM documents"
                ).fetchone()[0]
                connection.execute(
                    """
                    INSERT INTO regulatory_records (
                        document_id, record_key, decision_uid, page_number,
                        end_page_number, outcome_code, extraction_method,
                        extractor_version, created_at
                    ) VALUES (?, 'key', '', 1, 1, 'sin_clasificar',
                              'legacy', '3', '2026-01-01T00:00:00+00:00')
                    """,
                    (document_id,),
                )

            self.assertEqual(_database_missing_decision_uids(database), 1)
            self.assertEqual(
                _database_missing_decision_uids(Path(directory) / "corrupt.db"),
                -1,
            )

    def test_diagnostic_is_advisory_but_publish_rejection_fails(self) -> None:
        common = {
            "command": "validate",
            "workspace": Path(".reprocess"),
            "manifest": Path("documents_manifest.csv"),
            "review_log": Path("data/regulatory-review-log.csv"),
        }
        with (
            patch(
                "reprocess_corpus.parse_args",
                return_value=Namespace(**common, mode="diagnostic"),
            ),
            patch(
                "reprocess_corpus.validate_candidate",
                return_value={"status": "rejected"},
            ),
        ):
            self.assertEqual(main(), 0)
        with (
            patch(
                "reprocess_corpus.parse_args",
                return_value=Namespace(**common, mode="publish"),
            ),
            patch(
                "reprocess_corpus.validate_candidate",
                return_value={"status": "rejected"},
            ),
        ):
            self.assertEqual(main(), 1)

    def test_workspace_guard_rejects_project_directories(self) -> None:
        from config import ROOT_DIR

        with self.assertRaisesRegex(ValueError, "debe ser .reprocess"):
            _safe_workspace(ROOT_DIR / "services")

    def test_source_fingerprint_changes_with_extraction_runtime(self) -> None:
        with self._project_temporary() as directory:
            manifest = Path(directory) / "manifest.csv"
            manifest.write_text(
                "title,url\nActa,https://www.invima.gov.co/biblioteca/download/1\n",
                encoding="utf-8",
            )
            with patch(
                "reprocess_corpus._runtime_versions",
                return_value={"python": "3.12.1", "pymupdf": "1.26.1"},
            ):
                first = _source_fingerprint(manifest)
            with patch(
                "reprocess_corpus._runtime_versions",
                return_value={"python": "3.12.1", "pymupdf": "1.26.2"},
            ):
                second = _source_fingerprint(manifest)

        self.assertNotEqual(first, second)

    def test_publish_refuses_a_byte_changed_review_log(self) -> None:
        with self._project_temporary() as directory:
            root = Path(directory)
            workspace = root / "work"
            workspace.mkdir()
            review = root / "reviews.csv"
            review.write_bytes(b"original\n")
            _atomic_json(
                workspace / "run-state.json",
                {"review_log": _file_snapshot(review)},
            )
            _atomic_json(
                workspace / "reprocess-report.json",
                {"mode": "publish", "status": "accepted"},
            )
            review.write_bytes(b"cambiado\n")

            with self.assertRaisesRegex(ValueError, "registro de revisiones cambió"):
                publish_candidate(
                    workspace,
                    published_data=root / "published",
                    review_log_path=review,
                )

    def test_workflow_has_two_modes_and_single_validation_command(self) -> None:
        from config import ROOT_DIR

        workflow = (
            ROOT_DIR / ".github/workflows/reprocess-corpus.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("- diagnostic", workflow)
        self.assertIn("- publish", workflow)
        self.assertNotIn("full_rebuild", workflow)
        self.assertIn("include-hidden-files: true", workflow)
        self.assertEqual(workflow.count("overwrite: true"), 2)
        self.assertEqual(workflow.count("${{ always() && ("), 1)
        self.assertIn('[[ ! "$RESUME_RUN_ID" =~ ^[0-9]+$ ]]', workflow)
        self.assertNotIn('[ -n "${{ inputs.resume_run_id }}" ]', workflow)
        self.assertIn('[[ ! "$MODE" =~ ^(diagnostic|publish)$ ]]', workflow)
        self.assertNotIn('if [ "${{ inputs.mode }}"', workflow)
        self.assertNotIn("evaluate_search.py", workflow)
        self.assertNotIn("evaluation-report.json", workflow)
        self.assertIn("python reprocess_corpus.py export-reports", workflow)
        self.assertIn("--github-summary \"$GITHUB_STEP_SUMMARY\"", workflow)
        self.assertIn("path: .reprocess/export/", workflow)
        self.assertIn(".reprocess/candidate/data/semantic.db", workflow)
        self.assertEqual(
            workflow.count("python reprocess_corpus.py validate"),
            1,
        )
        self.assertEqual(
            workflow.count("python reprocess_corpus.py publish"),
            1,
        )
        self.assertEqual(workflow.count("python reprocess_corpus.py reconcile"), 1)
        self.assertLess(
            workflow.index("python reprocess_corpus.py reconcile"),
            workflow.index("python package_index.py create"),
        )

    def test_batched_candidate_can_continue_from_checkpoint(self) -> None:
        with self._project_temporary() as directory:
            root = Path(directory)
            workspace = root / "work"
            (workspace / "candidate/data").mkdir(parents=True)
            manifest = root / "manifest.csv"
            manifest.write_text(
                "title,url,year,acta_number,section\n"
                + "".join(
                    f"Acta {number},https://www.invima.gov.co/biblioteca/download/{number},"
                    f"2026,{number},SEMPB\n"
                    for number in range(1, 4)
                ),
                encoding="utf-8",
            )
            fingerprint = _source_fingerprint(manifest)
            _atomic_json(
                workspace / "run-state.json",
                {
                    "source_fingerprint": fingerprint,
                    "manifest_documents": 3,
                },
            )
            indexed: dict[str, dict] = {}

            def fake_index(partial_manifest, database_path, *_args, **_kwargs):
                lines = partial_manifest.read_text(encoding="utf-8").splitlines()[1:]
                indexed.clear()
                for line in lines:
                    url = line.split(",")[1]
                    indexed[url] = {}
                database_path.write_bytes(b"candidate")
                return IndexingReport(
                    documents_total=len(lines),
                    documents_indexed=len(lines),
                    errors=[],
                )

            with (
                patch("reprocess_corpus.rebuild_index", side_effect=fake_index),
                patch("reprocess_corpus.update_index", side_effect=fake_index),
                patch("reprocess_corpus.indexed_document_catalog", side_effect=lambda _p: dict(indexed)),
            ):
                first = build_candidate_batches(
                    workspace,
                    manifest_path=manifest,
                    pdf_cache_dir=root / "cache",
                    batch_size=2,
                    max_batches=1,
                )
                second = build_candidate_batches(
                    workspace,
                    manifest_path=manifest,
                    pdf_cache_dir=root / "cache",
                    batch_size=2,
                )

            self.assertFalse(first["complete"])
            self.assertEqual(first["documents_completed"], 2)
            self.assertTrue(second["complete"])
            self.assertEqual(second["documents_completed"], 3)


if __name__ == "__main__":
    unittest.main()
