from __future__ import annotations

import tempfile
import unittest
import sqlite3
from pathlib import Path
from unittest.mock import patch

from services.catalog import CatalogRecord, catalog_id_for, write_catalog
from services.database import database_stats, is_current_schema, search_chunks
from services.downloader import DownloadedPdf
from services.indexing import (
    IndexingReport,
    load_indexing_report,
    rebuild_index,
    update_index,
    write_indexing_report,
)


class IndexingTests(unittest.TestCase):
    def test_migrates_legacy_index_without_downloading_pdfs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = root / "manifest.csv"
            database_path = root / "legacy.db"
            cache_path = root / "cache"
            url = "https://www.invima.gov.co/biblioteca/download/1"
            title = "Acta No 01 de 2026 SEMPB"
            manifest_path.write_text(
                f"title,url\n{title},{url}\n",
                encoding="utf-8",
            )
            with sqlite3.connect(database_path) as connection:
                connection.executescript(
                    """
                    CREATE TABLE documents (
                        id INTEGER PRIMARY KEY,
                        title TEXT NOT NULL,
                        url TEXT NOT NULL,
                        document_hash TEXT NOT NULL
                    );
                    CREATE TABLE pages (
                        id INTEGER PRIMARY KEY,
                        document_id INTEGER NOT NULL,
                        page_number INTEGER NOT NULL,
                        text TEXT NOT NULL
                    );
                    CREATE TABLE chunks (
                        id INTEGER PRIMARY KEY,
                        page_id INTEGER NOT NULL,
                        chunk_index INTEGER NOT NULL,
                        text TEXT NOT NULL,
                        normalized_text TEXT NOT NULL
                    );
                    """
                )
                connection.execute(
                    "INSERT INTO documents VALUES (1, ?, ?, 'hash')",
                    (title, url),
                )
                connection.execute(
                    "INSERT INTO pages VALUES (1, 1, 1, 'Texto completo')"
                )
                connection.execute(
                    "INSERT INTO chunks VALUES "
                    "(1, 1, 0, 'Concepto sobre semaglutida.', "
                    "'concepto sobre semaglutida.')"
                )

            with patch("services.indexing.download_pdf_resource") as downloader:
                report = update_index(manifest_path, database_path, cache_path)

            self.assertEqual(report.mode, "legacy_migration")
            self.assertEqual(report.documents_migrated, 1)
            self.assertEqual(downloader.call_count, 0)
            self.assertTrue(is_current_schema(database_path))
            self.assertEqual(len(search_chunks(database_path, "semaglutida")), 1)

    def test_incremental_update_downloads_only_new_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = root / "manifest.csv"
            database_path = root / "actas.db"
            cache_path = root / "cache"
            pdf_path = root / "sample.pdf"
            pdf_path.write_bytes(b"%PDF-test")
            first_url = "https://www.invima.gov.co/biblioteca/download/1"
            second_url = "https://www.invima.gov.co/biblioteca/download/2"
            manifest_path.write_text(
                "title,url\n"
                f"Acta No 01 de 2026 SEMPB,{first_url}\n",
                encoding="utf-8",
            )

            def fake_download(title, url, *args, **kwargs):
                return DownloadedPdf(pdf_path, url)

            extracted = ([{"page": 1, "text": "Concepto sobre semaglutida."}], [])
            with patch(
                "services.indexing.download_pdf_resource",
                side_effect=fake_download,
            ), patch("services.indexing.extract_pdf_pages", return_value=extracted):
                first = rebuild_index(manifest_path, database_path, cache_path)

            self.assertEqual(first.documents_indexed, 1)
            manifest_path.write_text(
                "title,url\n"
                f"Acta No 01 de 2026 SEMPB,{first_url}\n"
                f"Acta No 02 de 2026 SEMPB,{second_url}\n",
                encoding="utf-8",
            )
            with patch(
                "services.indexing.download_pdf_resource",
                side_effect=fake_download,
            ) as downloader, patch(
                "services.indexing.extract_pdf_pages", return_value=extracted
            ):
                second = update_index(manifest_path, database_path, cache_path)

            self.assertEqual(second.mode, "incremental")
            self.assertEqual(second.documents_existing, 1)
            self.assertEqual(second.documents_indexed, 1)
            self.assertEqual(downloader.call_count, 1)
            self.assertEqual(database_stats(database_path)["documents"], 2)

    def test_partial_incremental_update_keeps_successes_and_retries_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = root / "manifest.csv"
            database_path = root / "actas.db"
            cache_path = root / "cache"
            pdf_path = root / "sample.pdf"
            pdf_path.write_bytes(b"%PDF-test")
            urls = [
                f"https://www.invima.gov.co/biblioteca/download/{number}"
                for number in (1, 2, 3)
            ]
            extracted = ([{"page": 1, "text": "Concepto regulatorio."}], [])

            manifest_path.write_text(
                f"title,url\nActa No 01 de 2026 SEMPB,{urls[0]}\n",
                encoding="utf-8",
            )
            with patch(
                "services.indexing.download_pdf_resource",
                return_value=DownloadedPdf(pdf_path, urls[0]),
            ), patch("services.indexing.extract_pdf_pages", return_value=extracted):
                rebuild_index(manifest_path, database_path, cache_path)

            manifest_path.write_text(
                "title,url\n"
                f"Acta No 01 de 2026 SEMPB,{urls[0]}\n"
                f"Acta No 02 de 2026 SEMPB,{urls[1]}\n"
                f"Acta No 03 de 2026 SEMPB,{urls[2]}\n",
                encoding="utf-8",
            )

            def one_failure(title, url, *args, **kwargs):
                if url == urls[2]:
                    raise ValueError("enlace temporalmente roto")
                return DownloadedPdf(pdf_path, url)

            with patch(
                "services.indexing.download_pdf_resource",
                side_effect=one_failure,
            ), patch("services.indexing.extract_pdf_pages", return_value=extracted):
                partial = update_index(
                    manifest_path,
                    database_path,
                    cache_path,
                    allow_partial=True,
                )

            self.assertEqual(partial.documents_indexed, 1)
            self.assertEqual(partial.documents_failed, 1)
            self.assertEqual(database_stats(database_path)["documents"], 2)

            with patch(
                "services.indexing.download_pdf_resource",
                return_value=DownloadedPdf(pdf_path, urls[2]),
            ) as downloader, patch(
                "services.indexing.extract_pdf_pages", return_value=extracted
            ):
                retried = update_index(
                    manifest_path,
                    database_path,
                    cache_path,
                    allow_partial=True,
                )

            self.assertEqual(retried.documents_indexed, 1)
            self.assertEqual(retried.documents_failed, 0)
            self.assertEqual(downloader.call_count, 1)
            self.assertEqual(database_stats(database_path)["documents"], 3)

    def test_uses_catalog_alternate_url_and_counts_ocr_pages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = root / "manifest.csv"
            catalog_path = root / "catalog.csv"
            database_path = root / "actas.db"
            cache_path = root / "cache"
            pdf_path = root / "sample.pdf"
            pdf_path.write_bytes(b"%PDF-test")
            primary_url = "https://www.invima.gov.co/biblioteca/download/broken"
            alternate_url = "https://www.invima.gov.co/biblioteca/download/working"
            title = "Acta No 01 de 2013 SEMPB"
            manifest_path.write_text(
                f"title,url\n{title},{primary_url}\n",
                encoding="utf-8",
            )
            write_catalog(
                [
                    CatalogRecord(
                        catalog_id=catalog_id_for(2013, "01", "SEMPB", None),
                        title=title,
                        published_title=title,
                        url=primary_url,
                        year=2013,
                        acta_number="01",
                        section="SEMPB",
                        alternate_urls=(alternate_url,),
                    )
                ],
                catalog_path,
            )

            def fake_download(title, url, *args, **kwargs):
                if url == primary_url:
                    raise ValueError("enlace primario roto")
                return DownloadedPdf(pdf_path, url)

            extracted = (
                [
                    {
                        "page": 1,
                        "text": "Texto reconocido de un acta histórica.",
                        "ocr_used": True,
                    }
                ],
                [],
            )
            with patch(
                "services.indexing.ACTAS_CATALOG_PATH",
                catalog_path,
            ), patch(
                "services.indexing.download_pdf_resource",
                side_effect=fake_download,
            ) as downloader, patch(
                "services.indexing.extract_pdf_pages",
                return_value=extracted,
            ):
                report = rebuild_index(
                    manifest_path,
                    database_path,
                    cache_path,
                )

        self.assertEqual(downloader.call_count, 2)
        self.assertEqual(report.documents_indexed, 1)
        self.assertEqual(report.ocr_pages_indexed, 1)
        self.assertEqual(report.alternate_links_used[0]["used_url"], alternate_url)

    def test_indexing_report_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "indexing-report.json"
            write_indexing_report(
                IndexingReport(documents_indexed=2, ocr_pages_indexed=3),
                path,
            )
            loaded = load_indexing_report(path)

        self.assertEqual(loaded["documents_indexed"], 2)
        self.assertEqual(loaded["ocr_pages_indexed"], 3)
        self.assertIn("generated_at", loaded)
