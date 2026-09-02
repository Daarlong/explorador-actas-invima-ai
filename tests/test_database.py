import tempfile
import unittest
import sqlite3
from pathlib import Path

from services.database import (
    DATABASE_SCHEMA_VERSION,
    connect,
    count_regulatory_records,
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
        self.assertEqual(
            database_schema_version(self.database_path), DATABASE_SCHEMA_VERSION
        )
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
        self.assertEqual(
            database_schema_version(self.database_path), DATABASE_SCHEMA_VERSION
        )
        with connect(self.database_path) as connection:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(regulatory_records)"
                )
            }
        self.assertIn("end_page_number", columns)

    def test_migrates_v4_to_v5_without_changing_index_counts(self) -> None:
        legacy_path = Path(self.temp_dir.name) / "schema-v4.db"
        with sqlite3.connect(legacy_path) as connection:
            connection.executescript(
                """
                PRAGMA foreign_keys = ON;

                CREATE TABLE app_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT INTO app_metadata (key, value)
                VALUES ('schema_version', '4');

                CREATE TABLE documents (
                    id INTEGER PRIMARY KEY,
                    title TEXT NOT NULL,
                    normalized_title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    manifest_url TEXT NOT NULL UNIQUE,
                    year INTEGER,
                    acta_number TEXT,
                    section TEXT,
                    part TEXT,
                    source_type TEXT NOT NULL DEFAULT 'official',
                    document_hash TEXT NOT NULL,
                    pdf_page_count INTEGER NOT NULL,
                    indexed_page_count INTEGER NOT NULL,
                    ocr_candidate_pages TEXT NOT NULL DEFAULT '',
                    page_inventory_complete INTEGER NOT NULL DEFAULT 1,
                    indexed_at TEXT NOT NULL
                );
                CREATE TABLE pages (
                    id INTEGER PRIMARY KEY,
                    document_id INTEGER NOT NULL
                        REFERENCES documents(id) ON DELETE CASCADE,
                    page_number INTEGER NOT NULL,
                    UNIQUE(document_id, page_number)
                );
                CREATE TABLE chunks (
                    id INTEGER PRIMARY KEY,
                    page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
                    chunk_index INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    UNIQUE(page_id, chunk_index)
                );
                CREATE VIRTUAL TABLE chunks_fts USING fts5(
                    title,
                    text,
                    content='',
                    detail=column,
                    tokenize = 'unicode61 remove_diacritics 2'
                );
                CREATE TABLE document_extractions (
                    document_id INTEGER PRIMARY KEY
                        REFERENCES documents(id) ON DELETE CASCADE,
                    document_hash TEXT NOT NULL,
                    extractor_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    record_count INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT,
                    extracted_at TEXT NOT NULL
                );
                CREATE TABLE regulatory_records (
                    id INTEGER PRIMARY KEY,
                    document_id INTEGER NOT NULL
                        REFERENCES documents(id) ON DELETE CASCADE,
                    record_key TEXT NOT NULL,
                    page_number INTEGER NOT NULL,
                    end_page_number INTEGER NOT NULL,
                    product_name TEXT,
                    normalized_product_name TEXT,
                    active_ingredient TEXT,
                    normalized_active_ingredient TEXT,
                    interested_party TEXT,
                    normalized_interested_party TEXT,
                    expediente TEXT,
                    normalized_expediente TEXT,
                    radicado TEXT,
                    normalized_radicado TEXT,
                    request_text TEXT,
                    concept_text TEXT,
                    outcome_code TEXT NOT NULL,
                    extraction_method TEXT NOT NULL,
                    confidence REAL,
                    needs_review INTEGER NOT NULL DEFAULT 1,
                    extractor_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(document_id, record_key),
                    CHECK(page_number >= 1),
                    CHECK(end_page_number >= page_number),
                    CHECK(confidence IS NULL OR confidence BETWEEN 0 AND 1)
                );
                """
            )
            connection.execute(
                """
                INSERT INTO documents (
                    id, title, normalized_title, url, manifest_url, year,
                    acta_number, section, part, source_type, document_hash,
                    pdf_page_count, indexed_page_count, ocr_candidate_pages,
                    page_inventory_complete, indexed_at
                ) VALUES (
                    1, 'Acta No 01 de 2024 SEMPB',
                    'acta no 01 de 2024 sempb',
                    'https://www.invima.gov.co/biblioteca/download/v4',
                    'https://www.invima.gov.co/biblioteca/download/v4',
                    2024, '01', 'SEMPB', NULL, 'official', 'pdf-hash-v4',
                    1, 1, '', 1, '2024-01-02T00:00:00+00:00'
                )
                """
            )
            connection.execute(
                "INSERT INTO pages (id, document_id, page_number) VALUES (1, 1, 3)"
            )
            chunk_text = "Concepto de la Sala sobre semaglutida."
            connection.execute(
                "INSERT INTO chunks (id, page_id, chunk_index, text) "
                "VALUES (1, 1, 0, ?)",
                (chunk_text,),
            )
            connection.execute(
                "INSERT INTO chunks_fts (rowid, title, text) VALUES (1, ?, ?)",
                ("Acta No 01 de 2024 SEMPB", chunk_text),
            )
            connection.execute(
                """
                INSERT INTO regulatory_records (
                    id, document_id, record_key, page_number, end_page_number,
                    product_name, normalized_product_name, active_ingredient,
                    normalized_active_ingredient, expediente,
                    normalized_expediente, request_text, concept_text,
                    outcome_code, extraction_method, confidence, needs_review,
                    extractor_version, created_at
                ) VALUES (
                    1, 1, 'record-key-v4', 3, 3, 'OZEMPIC', 'ozempic',
                    'Semaglutida', 'semaglutida', 'EXP-4', 'exp4',
                    'Evaluación farmacológica', 'La Sala aprueba.',
                    'aprobado', 'deterministic', 0.9, 1, '2',
                    '2024-01-02T00:00:00+00:00'
                )
                """
            )
            connection.execute(
                """
                INSERT INTO document_extractions (
                    document_id, document_hash, extractor_version, status,
                    record_count, error_message, extracted_at
                ) VALUES (
                    1, 'pdf-hash-v4', '2', 'complete', 1, NULL,
                    '2024-01-02T00:00:00+00:00'
                )
                """
            )
            before = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "documents",
                    "pages",
                    "chunks",
                    "chunks_fts",
                    "regulatory_records",
                )
            }

        self.assertTrue(migrate_database_schema(legacy_path))
        self.assertFalse(migrate_database_schema(legacy_path))

        with connect(legacy_path) as connection:
            after = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in before
            }
            record_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(regulatory_records)")
            }
            document_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(documents)")
            }
            indexes = {
                row[1]
                for row in connection.execute("PRAGMA index_list(regulatory_records)")
            }
            record = connection.execute(
                "SELECT record_key, product_name, active_ingredient, outcome_code "
                "FROM regulatory_records WHERE id = 1"
            ).fetchone()
        self.assertEqual(before, after)
        self.assertEqual(database_schema_version(legacy_path), DATABASE_SCHEMA_VERSION)
        self.assertIn("decision_uid", record_columns)
        self.assertIn("request_type_code", record_columns)
        self.assertIn("catalog_id", document_columns)
        self.assertIn("publication_date", document_columns)
        self.assertIn("idx_records_decision_uid", indexes)
        self.assertEqual(tuple(record), ("record-key-v4", "OZEMPIC", "Semaglutida", "aprobado"))
        self.assertEqual(len(search_chunks(legacy_path, "semaglutida")), 1)

    def test_regulatory_uid_is_stable_when_reextracted(self) -> None:
        insert_document(
            self.database_path,
            DocumentMetadata(
                title="Acta No 04 de 2026 SEMPB",
                url="https://www.invima.gov.co/biblioteca/download/4",
                year=2026,
                acta_number="04",
                section="SEMPB",
            ),
            "hash-4",
            [
                {
                    "page": 12,
                    "chunks": [
                        "3.1.1 EVALUACIONES FARMACOLÓGICAS\n"
                        "Producto: ALFA\nExpediente: 12345\n"
                        "Solicitud: Evaluación farmacológica.\n"
                        "Concepto: La Sala aprueba lo solicitado."
                    ],
                }
            ],
        )
        sync_regulatory_extractions(self.database_path)
        with connect(self.database_path) as connection:
            first = connection.execute(
                "SELECT decision_uid FROM regulatory_records "
                "WHERE document_id = (SELECT id FROM documents WHERE title LIKE 'Acta No 04%')"
            ).fetchone()[0]
            connection.execute(
                "UPDATE document_extractions SET extractor_version = 'anterior' "
                "WHERE document_id = (SELECT id FROM documents WHERE title LIKE 'Acta No 04%')"
            )
        sync_regulatory_extractions(self.database_path)
        with connect(self.database_path) as connection:
            second = connection.execute(
                "SELECT decision_uid FROM regulatory_records "
                "WHERE document_id = (SELECT id FROM documents WHERE title LIKE 'Acta No 04%')"
            ).fetchone()[0]

        self.assertEqual(first, second)

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
        self.assertEqual(
            count_regulatory_records(
                self.database_path,
                years=[2026],
                outcomes=["requerido"],
            ),
            1,
        )
        self.assertEqual(
            count_regulatory_records(
                self.database_path,
                missing_field="request_type",
            ),
            0,
        )

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
