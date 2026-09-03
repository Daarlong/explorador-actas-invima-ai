from __future__ import annotations

import tempfile
import unittest
import sqlite3
from pathlib import Path

from services.database import connect, initialize_database, insert_document
from services.integrity import (
    build_integrity_report,
    build_regulatory_quality_snapshot,
)
from services.models import DocumentMetadata


class IntegrityTests(unittest.TestCase):
    def test_regulatory_completeness_and_field_evidence_are_measured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "actas.db"
            initialize_database(database_path)
            document_id = insert_document(
                database_path,
                DocumentMetadata(
                    title="Acta de prueba",
                    url="https://www.invima.gov.co/biblioteca/download/10",
                    year=2018,
                    acta_number="10",
                    section="SEMPB",
                ),
                "hash-calidad",
                [{"page": 1, "chunks": ["Principio activo: Semaglutida"]}],
            )
            with connect(database_path) as connection:
                connection.execute(
                    """
                    INSERT INTO regulatory_records (
                        document_id, record_key, decision_uid, page_number,
                        end_page_number, numeral, product_name,
                        active_ingredient, interested_party, expediente, radicado,
                        concept_text, outcome_code, extraction_method, confidence,
                        extractor_version, created_at
                    ) VALUES (?, 'r1', 'uid-1', 1, 1, '3.1', 'Ozempic',
                              'Semaglutida', 'Interesado', '2018-1', NULL,
                              'Concepto favorable', 'aprobado', 'labels', 0.9,
                              '4', '2026-09-03T00:00:00+00:00')
                    """,
                    (document_id,),
                )
                record_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
                connection.execute(
                    """
                    INSERT INTO regulatory_field_evidence (
                        record_id, field_name, ordinal, literal_value,
                        normalized_value, page_number, end_page_number,
                        evidence_text, extraction_method, confidence
                    ) VALUES (?, 'principio_activo', 0, 'Semaglutida',
                              'semaglutida', 1, 1,
                              'Principio activo: Semaglutida',
                              'explicit_label', 0.98)
                    """,
                    (record_id,),
                )
                connection.execute(
                    """
                    INSERT INTO regulatory_records (
                        document_id, record_key, decision_uid, page_number,
                        end_page_number, outcome_code, extraction_method,
                        confidence, extractor_version, created_at
                    ) VALUES (?, 'r2', 'uid-2', 1, 1, 'sin_clasificar',
                              'legacy', 0.5, '4',
                              '2026-09-03T00:00:00+00:00')
                    """,
                    (document_id,),
                )
                connection.commit()

            snapshot = build_regulatory_quality_snapshot(database_path)

        fields = {item["key"]: item for item in snapshot["fields"]}
        self.assertEqual(snapshot["status"], "available")
        self.assertEqual(snapshot["records"], 2)
        self.assertEqual(fields["product"]["coverage_percent"], 50.0)
        self.assertEqual(fields["active_ingredient"]["present"], 1)
        self.assertEqual(fields["identifiers"]["present"], 1)
        self.assertEqual(fields["page_range"]["coverage_percent"], 100.0)
        self.assertEqual(fields["concept"]["missing"], 1)
        self.assertEqual(fields["outcome"]["present"], 1)
        self.assertTrue(snapshot["field_evidence"]["available"])
        self.assertEqual(snapshot["field_evidence"]["rows"], 1)
        self.assertEqual(
            snapshot["field_evidence"]["by_field"][0]["field"],
            "principio_activo",
        )
        self.assertEqual(
            snapshot["field_evidence"]["by_method"][0]["origin"],
            "explícito estructurado",
        )

    def test_regulatory_quality_supports_legacy_columns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "legacy.db"
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE regulatory_records (
                        id INTEGER PRIMARY KEY,
                        product_name TEXT,
                        active_ingredient TEXT,
                        expediente TEXT,
                        radicado TEXT,
                        concept_text TEXT,
                        outcome_code TEXT
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO regulatory_records VALUES (
                        1, 'Producto', NULL, '123', NULL, 'Concepto', 'aprobado'
                    )
                    """
                )

            snapshot = build_regulatory_quality_snapshot(database_path)

        fields = {item["key"]: item for item in snapshot["fields"]}
        self.assertEqual(snapshot["status"], "available")
        self.assertFalse(fields["numeral"]["available"])
        self.assertEqual(fields["product"]["coverage_percent"], 100.0)
        self.assertFalse(snapshot["field_evidence"]["available"])

    def test_reports_complete_corpus_and_ocr_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "actas.db"
            manifest_path = root / "manifest.csv"
            url = "https://www.invima.gov.co/biblioteca/download/1"
            manifest_path.write_text(
                "title,url,year,acta_number,section,part,source_type\n"
                f"Acta No 01 de 2026 SEMPB,{url},2026,01,SEMPB,,official\n",
                encoding="utf-8",
            )
            initialize_database(database_path)
            insert_document(
                database_path,
                DocumentMetadata(
                    title="Acta No 01 de 2026 SEMPB",
                    url=url,
                    year=2026,
                    acta_number="01",
                    section="SEMPB",
                ),
                "hash",
                [
                    {
                        "page": 1,
                        "text": "Texto del concepto regulatorio.",
                        "text_source": "native_pdf",
                        "text_quality": 0.95,
                        "text_extractor_version": "pymupdf-text-v1",
                        "chunks": ["Texto del concepto regulatorio."],
                    },
                    {
                        "page": 2,
                        "text": None,
                        "text_source": None,
                        "text_quality": 0.0,
                        "extraction_error": "Página sin texto; OCR no ejecutado",
                        "text_extractor_version": "pymupdf-text-v1",
                        "chunks": [],
                    },
                ],
                pdf_page_count=2,
                possible_scans=[2],
            )

            report = build_integrity_report(
                database_path,
                manifest_path,
                ("www.invima.gov.co",),
            )

        self.assertEqual(report["status"], "warning")
        self.assertEqual(report["manifest_documents"], 1)
        self.assertEqual(report["indexed_documents"], 1)
        self.assertEqual(report["ocr_candidates"][0]["pages"], [2])
        self.assertEqual(report["chunks"], report["fts_rows"])
        self.assertEqual(report["pages_recorded"], 2)
        self.assertEqual(report["pages_accounted"], 2)
        self.assertEqual(report["pages_with_source_text"], 1)
        self.assertEqual(report["pages_with_failed_extraction"], 1)
        self.assertEqual(report["pages_unaccounted"], 0)
        self.assertEqual(report["inventory_coverage_percent"], 100.0)
        self.assertEqual(report["text_page_coverage_percent"], 50.0)
        self.assertEqual(report["coverage_by_year"][0]["year"], 2026)
        self.assertEqual(report["coverage_by_year"][0]["coverage_percent"], 100.0)
        self.assertEqual(
            report["coverage_by_section"][0]["section"],
            "SEMPB",
        )

    def test_complete_inventory_with_a_missing_physical_page_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "actas.db"
            manifest_path = root / "manifest.csv"
            url = "https://www.invima.gov.co/biblioteca/download/2"
            manifest_path.write_text(
                "title,url\n" f"Acta incompleta,{url}\n",
                encoding="utf-8",
            )
            initialize_database(database_path)
            insert_document(
                database_path,
                DocumentMetadata(title="Acta incompleta", url=url),
                "hash-incompleto",
                [
                    {
                        "page": 1,
                        "text": "Texto verificable",
                        "text_quality": 1.0,
                        "text_extractor_version": "pymupdf-text-v1",
                        "chunks": ["Texto verificable"],
                    }
                ],
                pdf_page_count=2,
                page_inventory_complete=True,
            )

            report = build_integrity_report(
                database_path,
                manifest_path,
                ("www.invima.gov.co",),
            )

        self.assertEqual(report["status"], "error")
        self.assertEqual(report["pages_unaccounted"], 1)
        self.assertEqual(
            report["page_inventory_errors"][0]["missing_pages"],
            [2],
        )

    def test_reports_missing_database_coverage_by_year(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "missing.db"
            manifest_path = root / "manifest.csv"
            manifest_path.write_text(
                "title,url,year,acta_number,section\n"
                "Acta No 01 de 2013 SEMPB,"
                "https://www.invima.gov.co/biblioteca/download/1,2013,01,SEMPB\n"
                "Acta No 01 de 2026 SEMPB,"
                "https://www.invima.gov.co/biblioteca/download/2,2026,01,SEMPB\n",
                encoding="utf-8",
            )

            report = build_integrity_report(
                database_path,
                manifest_path,
                ("www.invima.gov.co",),
            )

        self.assertEqual(
            [item["year"] for item in report["coverage_by_year"]],
            [2026, 2013],
        )
        self.assertTrue(
            all(
                item["coverage_percent"] == 0.0
                for item in report["coverage_by_year"]
            )
        )
