from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.database import (
    DATABASE_SCHEMA_VERSION,
    connect,
    database_schema_version,
    initialize_database,
    insert_document,
    migrate_database_schema,
    rebuild_regulatory_search_index,
    search_pages_exact,
    search_regulatory_fields,
    sync_regulatory_extractions,
)
from services.models import DocumentMetadata
from services.regulatory import RegulatoryRecord
from services.semantic import semantic_source_fingerprint


class FieldAndPageSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary.name) / "actas.db"
        initialize_database(self.database_path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _insert_document(
        self,
        *,
        title: str = "Acta 01 de 2026",
        year: int = 2026,
        number: str = "01",
        pages: list[dict] | None = None,
    ) -> int:
        return insert_document(
            self.database_path,
            DocumentMetadata(
                title=title,
                url=f"https://www.invima.gov.co/biblioteca/download/{number}",
                year=year,
                acta_number=number,
                section="SEMPB",
            ),
            f"hash-{number}",
            pages
            or [
                {
                    "page": 7,
                    "text": (
                        "Solicitud: ampliación de la indicación terapéutica.\n"
                        "Concepto: La Sala concluye que el balance beneficio "
                        "riesgo es favorable."
                    ),
                    "chunks": [
                        "Solicitud: ampliación de la indicación terapéutica.",
                        "Concepto: La Sala concluye que el balance beneficio "
                        "riesgo es favorable.",
                    ],
                }
            ],
        )

    def _sync_record(self, *, product: str = "PRODUCTO ALFA") -> None:
        record = RegulatoryRecord(
            producto=product,
            principio_activo="Semaglutida",
            interesado="Laboratorio de prueba",
            expediente="EXP-123",
            radicado="RAD-456",
            solicitud="Ampliación de la indicación terapéutica",
            concepto=(
                "La Sala concluye que el balance beneficio riesgo es favorable."
            ),
            resultado_normalizado="aprobado",
            pagina=7,
            pagina_final=7,
            tipo_solicitud="indicaciones",
        )
        with patch(
            "services.database.extract_regulatory_records", return_value=[record]
        ):
            summary = sync_regulatory_extractions(self.database_path)
        self.assertEqual(summary["documents_failed"], 0)

    def test_schema_has_compact_contentless_regulatory_fts(self) -> None:
        with connect(self.database_path) as connection:
            sql = str(
                connection.execute(
                    "SELECT sql FROM sqlite_master "
                    "WHERE name='regulatory_records_fts'"
                ).fetchone()[0]
            ).lower()

        self.assertIn("content=''", sql)
        self.assertIn("detail=column", sql)

    def test_searches_only_the_requested_persisted_field(self) -> None:
        self._insert_document()
        self._sync_record()

        request = search_regulatory_fields(
            self.database_path,
            "ampliación de la indicación",
            scope="request",
            exact_phrase=True,
        )
        concept = search_regulatory_fields(
            self.database_path,
            "balance beneficio riesgo",
            scope="concept",
            exact_phrase=True,
        )
        wrong_field = search_regulatory_fields(
            self.database_path,
            "balance beneficio riesgo",
            scope="request",
            exact_phrase=True,
        )

        self.assertEqual(len(request), 1)
        self.assertEqual(request[0].matched_field, "request")
        self.assertEqual(request[0].evidence_scope, "regulatory_field")
        self.assertEqual(request[0].page, 7)
        self.assertGreater(request[0].chunk_id, 0)
        self.assertEqual(len(concept), 1)
        self.assertEqual(concept[0].matched_field, "concept")
        self.assertEqual(wrong_field, [])

    def test_record_and_outcome_scopes_use_traceable_existing_values(self) -> None:
        self._insert_document()
        self._sync_record()

        record = search_regulatory_fields(
            self.database_path, "PRODUCTO ALFA EXP-123", scope="record"
        )
        outcome = search_regulatory_fields(
            self.database_path, "aprobado", scope="outcome"
        )

        self.assertEqual(len(record), 1)
        self.assertIn("PRODUCTO ALFA", record[0].match_excerpt or "")
        self.assertIn("EXP-123", record[0].match_excerpt or "")
        self.assertEqual(len(outcome), 1)
        self.assertTrue((outcome[0].match_excerpt or "").startswith("aprobado\n"))
        self.assertIn(
            "balance beneficio riesgo", outcome[0].match_excerpt or ""
        )

    def test_identity_fields_are_searchable_independently(self) -> None:
        self._insert_document()
        self._sync_record()

        examples = {
            "product": "PRODUCTO ALFA",
            "active_ingredient": "Semaglutida",
            "interested_party": "Laboratorio de prueba",
            "expediente": "EXP-123",
            "radicado": "RAD-456",
        }
        for scope, query in examples.items():
            with self.subTest(scope=scope):
                results = search_regulatory_fields(
                    self.database_path,
                    query,
                    scope=scope,
                    exact_phrase=True,
                )
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0].matched_field, scope)
                self.assertEqual(results[0].match_excerpt, query)

        self.assertEqual(
            search_regulatory_fields(
                self.database_path,
                "Semaglutida",
                scope="product",
            ),
            [],
        )

    def test_field_filters_apply_to_the_same_record(self) -> None:
        self._insert_document()
        self._sync_record(product="PRODUCTO ALFA")

        matching = search_regulatory_fields(
            self.database_path,
            "balance beneficio",
            scope="concept",
            filters={"products": ["Producto Alfa"], "years": [2026]},
        )
        excluded = search_regulatory_fields(
            self.database_path,
            "balance beneficio",
            scope="concept",
            filters={"products": ["Producto Beta"]},
        )

        self.assertEqual(len(matching), 1)
        self.assertEqual(excluded, [])

    def test_exact_page_phrase_can_cross_two_chunks(self) -> None:
        self._insert_document(
            pages=[
                {
                    "page": 12,
                    "text": (
                        "La Sala considera el balance beneficio-\nriesgo favorable."
                    ),
                    "chunks": [
                        "La Sala considera el balance beneficio",
                        "riesgo favorable.",
                    ],
                }
            ]
        )

        results = search_pages_exact(
            self.database_path,
            "balance beneficio riesgo favorable",
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].page, 12)
        self.assertEqual(results[0].evidence_scope, "page")
        self.assertEqual(results[0].match_type, "page_exact")
        self.assertIn(
            "balance beneficio riesgo favorable", results[0].match_excerpt or ""
        )

    def test_exact_page_phrase_never_crosses_physical_pages(self) -> None:
        self._insert_document(
            pages=[
                {
                    "page": 1,
                    "text": "La Sala considera el balance beneficio",
                    "chunks": ["La Sala considera el balance beneficio"],
                },
                {
                    "page": 2,
                    "text": "riesgo favorable para el producto.",
                    "chunks": ["riesgo favorable para el producto."],
                },
            ]
        )

        self.assertEqual(
            search_pages_exact(
                self.database_path,
                "balance beneficio riesgo favorable",
            ),
            [],
        )

    def test_exact_page_phrase_falls_back_to_merged_legacy_chunks(self) -> None:
        self._insert_document(
            pages=[
                {
                    "page": 3,
                    "text": (
                        "La Sala acepta la nueva indicación terapéutica solicitada."
                    ),
                    "chunks": [
                        "La Sala acepta la nueva indicación terapéutica",
                        "nueva indicación terapéutica solicitada.",
                    ],
                }
            ]
        )
        with connect(self.database_path) as connection:
            connection.execute(
                "UPDATE pages SET raw_text_compressed=NULL, raw_text_codec=NULL"
            )

        results = search_pages_exact(
            self.database_path,
            "acepta la nueva indicación terapéutica solicitada",
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].page, 3)

    def test_v6_to_v7_migration_preserves_chunk_ids_and_semantic_fingerprint(self) -> None:
        self._insert_document()
        self._sync_record()
        with connect(self.database_path) as connection:
            chunk_ids_before = [
                int(row[0]) for row in connection.execute("SELECT id FROM chunks")
            ]
            connection.execute("DROP TABLE regulatory_records_fts")
            connection.execute(
                "UPDATE app_metadata SET value='6' WHERE key='schema_version'"
            )
        fingerprint_before = semantic_source_fingerprint(self.database_path)

        self.assertTrue(migrate_database_schema(self.database_path))

        with connect(self.database_path) as connection:
            chunk_ids_after = [
                int(row[0]) for row in connection.execute("SELECT id FROM chunks")
            ]
            fts_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM regulatory_records_fts"
                ).fetchone()[0]
            )
        self.assertEqual(database_schema_version(self.database_path), DATABASE_SCHEMA_VERSION)
        self.assertEqual(chunk_ids_before, chunk_ids_after)
        self.assertEqual(fingerprint_before, semantic_source_fingerprint(self.database_path))
        self.assertEqual(fts_count, 1)

    def test_rebuild_does_not_change_record_or_chunk_ids(self) -> None:
        self._insert_document()
        self._sync_record()
        with connect(self.database_path) as connection:
            before = (
                [row[0] for row in connection.execute("SELECT id FROM chunks")],
                [row[0] for row in connection.execute("SELECT id FROM regulatory_records")],
            )

        self.assertEqual(rebuild_regulatory_search_index(self.database_path), 1)

        with connect(self.database_path) as connection:
            after = (
                [row[0] for row in connection.execute("SELECT id FROM chunks")],
                [row[0] for row in connection.execute("SELECT id FROM regulatory_records")],
            )
        self.assertEqual(before, after)

    def test_rejects_unimplemented_field_scope(self) -> None:
        with self.assertRaisesRegex(ValueError, "Campo regulatorio desconocido"):
            search_regulatory_fields(
                self.database_path, "semaglutida", scope="indication"
            )


if __name__ == "__main__":
    unittest.main()
