import tempfile
import unittest
import sqlite3
from pathlib import Path

from services.database import (
    connect,
    database_stats,
    get_filter_options,
    initialize_database,
    insert_document,
    search_chunks,
)
from services.models import DocumentMetadata


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "test.db"
        initialize_database(self.database_path)
        insert_document(
            self.database_path,
            DocumentMetadata(
                title="Acta No 01 de 2026 SEMPB Primera Parte",
                url="https://www.invima.gov.co/biblioteca/download/1",
                year=2026,
                acta_number="01",
                section="SEMPB",
                part="Primera Parte",
            ),
            "hash-1",
            [
                {
                    "page": 7,
                    "text": "La comisión evaluó semaglutida para el trámite.",
                    "chunks": ["La comisión evaluó semaglutida para el trámite."],
                }
            ],
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_search_returns_page_and_source(self) -> None:
        results = search_chunks(self.database_path, "semaglutida")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].page, 7)
        self.assertEqual(results[0].acta_number, "01")

    def test_filters_are_applied(self) -> None:
        self.assertEqual(
            search_chunks(
                self.database_path,
                "semaglutida",
                filters={"years": [2025]},
            ),
            [],
        )

    def test_stats_and_filter_options(self) -> None:
        stats = database_stats(self.database_path)
        self.assertEqual(stats, {"documents": 1, "pages": 1, "chunks": 1})
        options = get_filter_options(self.database_path)
        self.assertEqual(options["years"], [2026])
        self.assertEqual(options["sections"], ["SEMPB"])

    def test_adds_source_type_to_legacy_database(self) -> None:
        legacy_path = Path(self.temp_dir.name) / "legacy.db"
        with sqlite3.connect(legacy_path) as legacy_connection:
            legacy_connection.execute(
                "CREATE TABLE documents (id INTEGER PRIMARY KEY, title TEXT NOT NULL)"
            )
        with connect(legacy_path) as migrated_connection:
            columns = {
                row[1]
                for row in migrated_connection.execute(
                    "PRAGMA table_info(documents)"
                ).fetchall()
            }
        self.assertIn("source_type", columns)


if __name__ == "__main__":
    unittest.main()
