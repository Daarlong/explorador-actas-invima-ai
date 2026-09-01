import tempfile
import unittest
import sqlite3
from pathlib import Path

from services.database import (
    DATABASE_SCHEMA_VERSION,
    connect,
    database_schema_version,
    database_stats,
    get_filter_options,
    initialize_database,
    insert_document,
    migrate_database_schema,
    optimize_database,
    regulatory_records_for_chunks,
    search_chunks,
    sync_regulatory_extractions,
    table_exists,
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

    def test_compact_schema_and_optimization_keep_search_working(self) -> None:
        with connect(self.database_path) as connection:
            page_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(pages)")
            }
            chunk_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(chunks)")
            }
            fts_sql = str(
                connection.execute(
                    "SELECT sql FROM sqlite_master WHERE name = 'chunks_fts'"
                ).fetchone()[0]
            ).lower()
        self.assertNotIn("text", page_columns)
        self.assertNotIn("normalized_text", chunk_columns)
        self.assertIn("content=''", fts_sql)
        self.assertEqual(database_schema_version(self.database_path), DATABASE_SCHEMA_VERSION)

        optimize_database(self.database_path)
        self.assertEqual(len(search_chunks(self.database_path, "semaglutida")), 1)

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

    def test_migrates_v2_additively_without_changing_text_index(self) -> None:
        with connect(self.database_path) as connection:
            connection.execute("DROP TABLE regulatory_records")
            connection.execute("DROP TABLE document_extractions")
            connection.execute(
                "UPDATE app_metadata SET value = '2' WHERE key = 'schema_version'"
            )

        migrated = migrate_database_schema(self.database_path)

        self.assertTrue(migrated)
        self.assertEqual(database_schema_version(self.database_path), 4)
        self.assertTrue(table_exists(self.database_path, "regulatory_records"))
        with connect(self.database_path) as connection:
            record_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(regulatory_records)"
                )
            }
        self.assertIn("end_page_number", record_columns)
        self.assertEqual(len(search_chunks(self.database_path, "semaglutida")), 1)

    def test_migrates_v3_records_to_page_ranges(self) -> None:
        with connect(self.database_path) as connection:
            columns = [
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(regulatory_records)"
                )
                if row[1] != "end_page_number"
            ]
            selected = ", ".join(columns)
            connection.execute(
                f"CREATE TABLE regulatory_records_v3 AS "
                f"SELECT {selected} FROM regulatory_records"
            )
            connection.execute("DROP TABLE regulatory_records")
            connection.execute(
                "ALTER TABLE regulatory_records_v3 RENAME TO regulatory_records"
            )
            connection.execute(
                "UPDATE app_metadata SET value = '3' WHERE key = 'schema_version'"
            )

        self.assertTrue(migrate_database_schema(self.database_path))
        self.assertEqual(database_schema_version(self.database_path), 4)
        with connect(self.database_path) as connection:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(regulatory_records)"
                )
            }
        self.assertIn("end_page_number", columns)

    def test_extracts_and_filters_structured_regulatory_fields(self) -> None:
        insert_document(
            self.database_path,
            DocumentMetadata(
                title="Acta No 02 de 2026 SEMPB",
                url="https://www.invima.gov.co/biblioteca/download/2",
                year=2026,
                acta_number="02",
                section="SEMPB",
            ),
            "hash-2",
            [
                {
                    "page": 10,
                    "chunks": [
                        "Producto: Ozempic\nPrincipio activo: Semaglutida\n"
                        "Interesado: Compañía Ejemplo\nExpediente: 123456\n"
                        "Radicado: 2026123456\nSolicitud: Evaluación farmacológica\n"
                        "Concepto: La Sala requiere aportar información adicional."
                    ],
                }
            ],
        )

        summary = sync_regulatory_extractions(self.database_path)
        results = search_chunks(
            self.database_path,
            "información adicional",
            filters={"products": ["Ozempic"], "outcomes": ["requerido"]},
        )
        mapped = regulatory_records_for_chunks(
            self.database_path,
            [results[0].chunk_id],
        )

        self.assertGreaterEqual(summary["records_extracted"], 1)
        self.assertEqual(results[0].acta_number, "02")
        record = mapped[results[0].chunk_id][0]
        self.assertEqual(record["product_name"], "Ozempic")
        self.assertEqual(record["radicado"], "2026123456")
        self.assertEqual(record["outcome_code"], "requerido")

    def test_structured_filter_does_not_leak_to_another_decision(self) -> None:
        insert_document(
            self.database_path,
            DocumentMetadata(
                title="Acta No 03 de 2026 SEMPB",
                url="https://www.invima.gov.co/biblioteca/download/3",
                year=2026,
                acta_number="03",
                section="SEMPB",
            ),
            "hash-3",
            [
                {
                    "page": 20,
                    "chunks": [
                        "Producto: ALFA\nExpediente: 11111\n"
                        "Solicitud: Renovación.\nConcepto: La Sala aprueba."
                    ],
                },
                {
                    "page": 21,
                    "chunks": [
                        "Producto: BETA\nExpediente: 22222\n"
                        "Solicitud: Evaluación de estabilidad.\n"
                        "Concepto: La Sala niega lo solicitado."
                    ],
                },
            ],
        )
        sync_regulatory_extractions(self.database_path)

        self.assertEqual(
            search_chunks(
                self.database_path,
                "estabilidad",
                filters={"products": ["ALFA"]},
            ),
            [],
        )
        beta = search_chunks(
            self.database_path,
            "estabilidad",
            filters={"products": ["BETA"], "outcomes": ["negado"]},
        )
        self.assertEqual(len(beta), 1)

    def test_search_diversifies_fragments_before_grouping_documents(self) -> None:
        diverse_path = Path(self.temp_dir.name) / "diverse.db"
        initialize_database(diverse_path)
        insert_document(
            diverse_path,
            DocumentMetadata(
                title="Acta dominante",
                url="https://www.invima.gov.co/biblioteca/download/dominante",
            ),
            "dominante",
            [
                {
                    "page": 1,
                    "chunks": [
                        f"semaglutida fragmento dominante {index}"
                        for index in range(400)
                    ],
                }
            ],
        )
        for index in range(20):
            insert_document(
                diverse_path,
                DocumentMetadata(
                    title=f"Acta breve {index}",
                    url=(
                        "https://www.invima.gov.co/biblioteca/download/"
                        f"breve-{index}"
                    ),
                ),
                f"breve-{index}",
                [
                    {
                        "page": 1,
                        "chunks": [f"semaglutida evidencia breve {index}"],
                    }
                ],
            )

        results = search_chunks(
            diverse_path,
            "semaglutida",
            top_k=360,
            max_chunks_per_document=3,
        )
        self.assertEqual(len({result.url for result in results}), 21)
        dominant = [
            result for result in results if result.title == "Acta dominante"
        ]
        self.assertEqual(len(dominant), 3)


if __name__ == "__main__":
    unittest.main()
