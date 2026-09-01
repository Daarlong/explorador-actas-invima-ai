from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from services.database import initialize_database, insert_document
from services.integrity import build_integrity_report
from services.models import DocumentMetadata


class IntegrityTests(unittest.TestCase):
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
                [{"page": 1, "chunks": ["Texto del concepto regulatorio."]}],
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
        self.assertEqual(report["coverage_by_year"][0]["year"], 2026)
        self.assertEqual(report["coverage_by_year"][0]["coverage_percent"], 100.0)
        self.assertEqual(
            report["coverage_by_section"][0]["section"],
            "SEMPB",
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
