from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from services.analytics import (
    AnalyticsFilters,
    analytics_drilldown,
    build_corpus_analytics,
)
from services.database import connect, initialize_database, insert_document
from services.models import DocumentMetadata


class AnalyticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary.name) / "actas.db"
        initialize_database(self.database_path)
        self.documents = [
            self._insert_document(2025, "01", "https://invima.test/2025-01"),
            self._insert_document(2026, "02", "https://invima.test/2026-02"),
            self._insert_document(2024, "03", "https://invima.test/2024-03"),
        ]
        with connect(self.database_path) as connection:
            first = self._insert_record(
                connection,
                self.documents[0],
                "r1",
                page=7,
                active="Semaglutida",
                party="Novo Nordisk Colombia S.A.S.",
                outcome="aprobado",
                request_type="evaluacion_farmacologica",
            )
            second = self._insert_record(
                connection,
                self.documents[0],
                "r2",
                page=11,
                active="Liraglutida",
                party="Novo Nordisk Colombia S.A.S.",
                outcome="requerido",
                request_type="modificacion",
            )
            third = self._insert_record(
                connection,
                self.documents[1],
                "r3",
                page=3,
                active="Semaglutida",
                party="Laboratorio Ejemplo S.A.S.",
                outcome="sin_clasificar",
                request_type="otra_solicitud",
            )
            for record_id, active, party, page in (
                (first, "Semaglutida", "Novo Nordisk Colombia S.A.S.", 7),
                (second, "Liraglutida", "Novo Nordisk Colombia S.A.S.", 11),
                (third, "Semaglutida", "Laboratorio Ejemplo S.A.S.", 3),
            ):
                self._insert_evidence(
                    connection, record_id, "principio_activo", active, page
                )
                self._insert_evidence(
                    connection, record_id, "interesado", party, page
                )
            for document_id in self.documents:
                connection.execute(
                    """
                    INSERT INTO document_extractions (
                        document_id, document_hash, extractor_version, status,
                        record_count, extracted_at
                    ) VALUES (?, ?, 'test', 'complete', ?, '2026-09-15')
                    """,
                    (
                        document_id,
                        f"hash-{document_id}",
                        2 if document_id == self.documents[0] else (
                            1 if document_id == self.documents[1] else 0
                        ),
                    ),
                )
            connection.commit()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _insert_document(self, year: int, number: str, url: str) -> int:
        return insert_document(
            self.database_path,
            DocumentMetadata(
                title=f"Acta {number} de {year}",
                url=url,
                year=year,
                acta_number=number,
                section="SEMPB",
            ),
            f"hash-{year}-{number}",
            [
                {
                    "page": 1,
                    "text": f"Texto del acta {number}",
                    "chunks": [f"Texto del acta {number}"],
                }
            ],
        )

    @staticmethod
    def _insert_record(
        connection,
        document_id: int,
        key: str,
        *,
        page: int,
        active: str,
        party: str,
        outcome: str,
        request_type: str,
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO regulatory_records (
                document_id, record_key, decision_uid, page_number,
                end_page_number, numeral, request_type_code, product_name,
                normalized_product_name, active_ingredient,
                normalized_active_ingredient, interested_party,
                normalized_interested_party, request_text, concept_text,
                outcome_code, extraction_method, confidence,
                extractor_version, created_at
            ) VALUES (?, ?, ?, ?, ?, '3.1', ?, 'Producto', 'producto', ?, ?,
                      ?, ?, 'Solicitud de prueba', 'Concepto verificable', ?,
                      'labels', 0.9, 'test', '2026-09-15')
            """,
            (
                document_id,
                key,
                f"uid-{key}",
                page,
                page,
                request_type,
                active,
                active.casefold(),
                party,
                party.casefold(),
                outcome,
            ),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _insert_evidence(
        connection,
        record_id: int,
        field: str,
        value: str,
        page: int,
    ) -> None:
        connection.execute(
            """
            INSERT INTO regulatory_field_evidence (
                record_id, field_name, ordinal, literal_value,
                normalized_value, canonical_value, page_number,
                end_page_number, evidence_text, extraction_method, confidence
            ) VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, 'explicit_label', 0.98)
            """,
            (
                record_id,
                field,
                value,
                value.casefold(),
                value.casefold(),
                page,
                page,
                f"{field}: {value}",
            ),
        )

    def test_dashboard_distinguishes_documents_records_values_and_coverage(self) -> None:
        report = build_corpus_analytics(self.database_path)

        self.assertEqual(report["status"], "available")
        self.assertEqual(report["totals"]["documents"]["value"], 3)
        self.assertEqual(report["totals"]["documents"]["unit"], "documents")
        self.assertEqual(report["totals"]["unique_acts"]["value"], 3)
        self.assertEqual(report["totals"]["records"]["value"], 3)
        self.assertEqual(report["totals"]["records"]["unit"], "extracted_records")
        self.assertEqual(
            report["totals"]["distinct_ingredient_and_party_values"]["value"],
            4,
        )

        coverage = report["coverage"]["document_extraction"]
        self.assertEqual(coverage["documents"], 3)
        self.assertEqual(coverage["documents_processed"], 3)
        self.assertEqual(coverage["documents_with_records"], 2)
        self.assertEqual(coverage["documents_without_records"], 1)
        self.assertEqual(coverage["documents_processed_without_records"], 1)
        self.assertEqual(coverage["record_coverage_percent"], 66.67)
        self.assertEqual(
            report["coverage"]["fields"]["outcome"]["coverage_percent"],
            66.67,
        )

    def test_dimensions_have_units_denominators_and_honest_outcomes(self) -> None:
        report = build_corpus_analytics(self.database_path)
        dimensions = report["dimensions"]

        self.assertEqual(dimensions["year"]["denominator"]["value"], 3)
        self.assertEqual(dimensions["year"]["denominator"]["unit"], "documents")
        self.assertEqual(dimensions["year"]["coverage"]["percent"], 100.0)
        self.assertEqual(dimensions["outcome"]["denominator"]["value"], 3)
        self.assertEqual(dimensions["outcome"]["coverage"]["value"], 2)
        self.assertEqual(dimensions["outcome"]["coverage"]["percent"], 66.67)
        self.assertFalse(dimensions["outcome"]["creates_new_official_decisions"])
        self.assertIn("extracted", dimensions["outcome"]["meaning"])

        outcomes = {item["value"]: item for item in dimensions["outcome"]["items"]}
        self.assertEqual(set(outcomes), {"aprobado", "requerido"})
        self.assertNotIn("sin_clasificar", outcomes)
        self.assertEqual(outcomes["aprobado"]["records"], 1)

        years = {item["value"]: item for item in dimensions["year"]["items"]}
        self.assertEqual(years[2024]["documents"], 1)
        self.assertEqual(years[2024]["records"], 0)

    def test_active_ingredient_uses_individual_evidence_and_traces_source(self) -> None:
        report = build_corpus_analytics(self.database_path, evidence_limit=2)
        dimension = report["dimensions"]["active_ingredient"]
        items = {item["value"]: item for item in dimension["items"]}

        self.assertEqual(dimension["unit"], "extracted_values")
        self.assertEqual(
            dimension["source"],
            "regulatory_field_evidence_with_record_fallback",
        )
        self.assertEqual(dimension["total_values"], 2)
        self.assertEqual(items["semaglutida"]["records"], 2)
        self.assertEqual(items["semaglutida"]["documents"], 2)
        evidence = items["semaglutida"]["evidence"][0]
        self.assertTrue(evidence["url"].startswith("https://invima.test/"))
        self.assertIsInstance(evidence["record_id"], int)
        self.assertIsInstance(evidence["document_id"], int)
        self.assertIsInstance(evidence["page"], int)
        self.assertTrue(evidence["title"].startswith("Acta"))

    def test_filters_apply_to_totals_dimensions_and_coverage_scope_is_explicit(self) -> None:
        report = build_corpus_analytics(
            self.database_path,
            AnalyticsFilters(outcomes=("requerido",)),
        )

        self.assertEqual(report["totals"]["documents"]["value"], 1)
        self.assertEqual(report["totals"]["records"]["value"], 1)
        self.assertEqual(
            report["dimensions"]["outcome"]["items"][0]["value"], "requerido"
        )
        extraction = report["coverage"]["document_extraction"]
        self.assertEqual(extraction["documents"], 3)
        self.assertEqual(extraction["scope"], "document_filters_only")
        self.assertEqual(extraction["structured_filters_excluded"], ["outcomes"])

    def test_empty_evidence_table_falls_back_to_populated_record_fields(self) -> None:
        with connect(self.database_path) as connection:
            connection.execute("DELETE FROM regulatory_field_evidence")
            connection.commit()

        report = build_corpus_analytics(self.database_path)
        active = report["dimensions"]["active_ingredient"]
        parties = report["dimensions"]["interested_party"]
        active_items = {item["value"]: item for item in active["items"]}

        self.assertTrue(active["available"])
        self.assertEqual(active["total_values"], 2)
        self.assertEqual(active_items["semaglutida"]["records"], 2)
        self.assertEqual(active_items["semaglutida"]["occurrences"], 2)
        self.assertEqual(parties["total_values"], 2)
        detail = analytics_drilldown(
            self.database_path, "active_ingredient", "semaglutida"
        )
        self.assertEqual(detail["total"], 2)
        self.assertTrue(all(item["url"] for item in detail["items"]))

    def test_partial_evidence_uses_per_record_fallback_without_double_counting(self) -> None:
        with connect(self.database_path) as connection:
            # La segunda acta queda sin evidencia de principio activo.
            connection.execute(
                "DELETE FROM regulatory_field_evidence "
                "WHERE record_id = 3 AND field_name = 'principio_activo'"
            )
            # La primera ficha tiene una evidencia duplicada del mismo valor;
            # debe seguir contando como una ocurrencia por ficha/valor.
            connection.execute(
                """
                INSERT INTO regulatory_field_evidence (
                    record_id, field_name, ordinal, literal_value,
                    normalized_value, canonical_value, page_number,
                    end_page_number, evidence_text, extraction_method, confidence
                ) VALUES (1, 'principio_activo', 1, 'Semaglutida',
                          'semaglutida', 'semaglutida', 7, 7,
                          'Otra mención: Semaglutida', 'explicit_label', 0.95)
                """
            )
            connection.commit()

        report = build_corpus_analytics(self.database_path)
        active_items = {
            item["value"]: item
            for item in report["dimensions"]["active_ingredient"]["items"]
        }

        self.assertEqual(active_items["semaglutida"]["records"], 2)
        self.assertEqual(active_items["semaglutida"]["documents"], 2)
        self.assertEqual(active_items["semaglutida"]["occurrences"], 2)
        detail = analytics_drilldown(
            self.database_path, "active_ingredient", "semaglutida", limit=10
        )
        self.assertEqual(detail["total"], 2)
        self.assertEqual(len(detail["items"]), 2)
        self.assertTrue(all(item["evidence_text"] for item in detail["items"]))

    def test_limits_are_clamped_and_truncation_is_reported(self) -> None:
        report = build_corpus_analytics(
            self.database_path, top_limit=1, evidence_limit=999
        )

        self.assertEqual(report["limits"]["top_values"], 1)
        self.assertEqual(report["limits"]["evidence_per_value"], 20)
        self.assertEqual(len(report["dimensions"]["active_ingredient"]["items"]), 1)
        self.assertTrue(report["dimensions"]["active_ingredient"]["truncated"])

    def test_drilldown_is_paginated_and_returns_verifiable_evidence(self) -> None:
        first = analytics_drilldown(
            self.database_path, "active_ingredient", "semaglutida", limit=1
        )
        second = analytics_drilldown(
            self.database_path,
            "active_ingredient",
            "semaglutida",
            limit=1,
            offset=1,
        )

        self.assertTrue(first["available"])
        self.assertEqual(first["total"], 2)
        self.assertTrue(first["has_more"])
        self.assertEqual(len(first["items"]), 1)
        self.assertFalse(second["has_more"])
        self.assertNotEqual(
            first["items"][0]["record_id"], second["items"][0]["record_id"]
        )
        for key in ("document_id", "record_id", "title", "url", "page"):
            self.assertIn(key, first["items"][0])

    def test_all_user_values_are_bound_parameters(self) -> None:
        report = build_corpus_analytics(
            self.database_path,
            {"outcomes": ["aprobado' OR 1=1 --"]},
        )

        self.assertEqual(report["status"], "available")
        self.assertEqual(report["totals"]["documents"]["value"], 0)
        self.assertEqual(report["totals"]["records"]["value"], 0)
        with connect(self.database_path) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
                3,
            )

    def test_missing_database_uses_none_instead_of_fabricated_zero(self) -> None:
        report = build_corpus_analytics(Path(self.temporary.name) / "missing.db")

        self.assertEqual(report["status"], "unavailable")
        self.assertIsNone(report["totals"]["documents"]["value"])
        self.assertFalse(report["totals"]["records"]["available"])
        self.assertIsNone(
            report["coverage"]["document_extraction"]["documents_without_records"]
        )

    def test_legacy_document_database_keeps_year_trend_but_not_record_zeros(self) -> None:
        legacy = Path(self.temporary.name) / "legacy.db"
        with sqlite3.connect(legacy) as connection:
            connection.execute(
                """
                CREATE TABLE documents (
                    id INTEGER PRIMARY KEY, title TEXT, url TEXT, year INTEGER,
                    acta_number TEXT, section TEXT
                )
                """
            )
            connection.execute(
                "INSERT INTO documents VALUES "
                "(1, 'Acta antigua', 'https://invima.test/old', 2013, '01', 'SEMPB')"
            )

        report = build_corpus_analytics(legacy)

        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["totals"]["documents"]["value"], 1)
        self.assertIsNone(report["totals"]["records"]["value"])
        self.assertFalse(report["dimensions"]["outcome"]["available"])
        self.assertEqual(report["dimensions"]["year"]["items"][0]["value"], 2013)
        self.assertIsNone(report["dimensions"]["year"]["items"][0]["records"])

        detail = analytics_drilldown(legacy, "year", 2013)
        self.assertTrue(detail["available"])
        self.assertEqual(detail["total"], 1)
        self.assertEqual(detail["items"][0]["evidence_type"], "document")
        self.assertIsNone(detail["items"][0]["page"])

    def test_invalid_dimension_is_rejected_before_building_sql(self) -> None:
        with self.assertRaises(ValueError):
            analytics_drilldown(self.database_path, "outcome; DROP TABLE documents", "x")

    def test_unique_act_falls_back_to_document_when_any_identity_part_is_missing(self) -> None:
        database_path = Path(self.temporary.name) / "act-identity.db"
        initialize_database(database_path)
        for suffix, acta_number, part in (
            ("missing-a", None, "Primera Parte"),
            ("missing-b", None, "Segunda Parte"),
            ("complete-a", "08", "Primera Parte"),
            ("complete-b", "08", "Segunda Parte"),
        ):
            insert_document(
                database_path,
                DocumentMetadata(
                    title=f"Acta {suffix}",
                    url=f"https://invima.test/{suffix}",
                    year=2026,
                    acta_number=acta_number,
                    section="SEMPB",
                    part=part,
                ),
                f"hash-{suffix}",
                [{"page": 1, "text": suffix, "chunks": [suffix]}],
            )

        report = build_corpus_analytics(database_path)

        # Los dos PDF completos son partes de una misma acta; cada PDF sin
        # numero se conserva como documento independiente.
        self.assertEqual(report["totals"]["documents"]["value"], 4)
        self.assertEqual(report["totals"]["unique_acts"]["value"], 3)


if __name__ == "__main__":
    unittest.main()
