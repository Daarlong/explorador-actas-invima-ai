from __future__ import annotations

import csv
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from services.comparison import (
    comparison_matrix,
    comparison_to_csv,
    load_selected_decisions,
    printable_html_report,
    search_timeline,
    timeline_to_csv,
)
from services.database import connect, initialize_database, insert_document
from services.models import DocumentMetadata
from services.reviews import ReviewEvent


class ComparisonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "actas.db"
        initialize_database(self.database_path)
        first_document = insert_document(
            self.database_path,
            DocumentMetadata(
                title="Acta <script>01</script> de 2024",
                url="https://www.invima.gov.co/biblioteca/download/1",
                year=2024,
                acta_number="02",
                section="SEMPB",
            ),
            "hash-1",
            [
                {
                    "page": 10,
                    "chunks": [
                        "Evidencia <b>uno</b> para Ozempic.",
                        "Segundo fragmento de la misma decision.",
                    ],
                },
                {"page": 12, "chunks": ["Texto sin ficha estructurada."]},
            ],
        )
        second_document = insert_document(
            self.database_path,
            DocumentMetadata(
                title="Acta 01 de 2025",
                url="https://www.invima.gov.co/biblioteca/download/2",
                year=2025,
                acta_number="01",
                section="SEMPB",
            ),
            "hash-2",
            [{"page": 5, "chunks": ["Decision posterior sobre Ozempic."]}],
        )

        with connect(self.database_path) as connection:
            now = "2026-01-01T00:00:00+00:00"
            self._insert_record(
                connection,
                first_document,
                "record-1",
                10,
                "Ozempic",
                "Semaglutida",
                "=HYPERLINK(\"https://evil.test\")",
                "EXP-100",
                "RAD-100",
                "Evaluacion farmacologica",
                "La Sala requiere informacion adicional.",
                "requerido",
                now,
            )
            self._insert_record(
                connection,
                second_document,
                "record-2",
                5,
                "Ozempic",
                "Semaglutida",
                "Compania Ejemplo",
                "EXP-100",
                "RAD-200",
                "Modificacion",
                "La Sala emite concepto favorable.",
                "favorable",
                now,
            )
            rows = connection.execute(
                "SELECT id, text FROM chunks ORDER BY id"
            ).fetchall()
            self.chunk_ids = [int(row["id"]) for row in rows]

    @staticmethod
    def _insert_record(
        connection,
        document_id,
        key,
        page,
        product,
        ingredient,
        interested,
        expediente,
        radicado,
        request,
        concept,
        outcome,
        created_at,
    ) -> None:
        connection.execute(
            """
            INSERT INTO regulatory_records (
                document_id, record_key, decision_uid, page_number, end_page_number,
                product_name, normalized_product_name, active_ingredient,
                normalized_active_ingredient, interested_party,
                normalized_interested_party, expediente, normalized_expediente,
                radicado, normalized_radicado, request_text, concept_text,
                outcome_code, extraction_method, confidence, needs_review,
                extractor_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      'test', 0.9, 1, 'test', ?)
            """,
            (
                document_id,
                key,
                "dec_" + key,
                page,
                page,
                product,
                product.lower(),
                ingredient,
                ingredient.lower(),
                interested,
                interested.lower(),
                expediente,
                expediente.lower().replace("-", ""),
                radicado,
                radicado.lower().replace("-", ""),
                request,
                concept,
                outcome,
                created_at,
            ),
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_loads_current_sources_and_deduplicates_the_same_record(self) -> None:
        selected = [
            {
                "chunk_id": self.chunk_ids[0],
                "text": "Evidencia <b>uno</b> para Ozempic.",
            },
            {"chunk_id": self.chunk_ids[1]},
            {"chunk_id": self.chunk_ids[2]},
            {"chunk_id": "invalido"},
        ]

        decisions = load_selected_decisions(self.database_path, selected)

        self.assertEqual(len(decisions), 2)
        structured = decisions[0]
        self.assertEqual(structured.product_name, "Ozempic")
        self.assertEqual(structured.decision_uid, "dec_record-1")
        self.assertEqual(structured.key, "decision:dec_record-1")
        self.assertEqual(len(structured.evidences), 2)
        self.assertIn("Evidencia <b>uno</b>", structured.evidences[0].text)
        self.assertIsNone(decisions[1].record_id)

    def test_discards_a_stale_or_tampered_complete_snapshot(self) -> None:
        with connect(self.database_path) as connection:
            row = connection.execute(
                """
                SELECT c.id AS chunk_id, c.text, p.page_number AS page,
                       d.title, d.url, d.year, d.acta_number, d.section,
                       d.part, d.source_type
                FROM chunks c
                JOIN pages p ON p.id = c.page_id
                JOIN documents d ON d.id = p.document_id
                WHERE c.id = ?
                """,
                (self.chunk_ids[0],),
            ).fetchone()
        snapshot = dict(row)
        snapshot["text"] = "Otro texto que reutilizo el mismo ID"

        self.assertEqual(
            load_selected_decisions(self.database_path, [snapshot]),
            [],
        )

    def test_comparison_matrix_contains_regulatory_fields(self) -> None:
        decisions = load_selected_decisions(
            self.database_path,
            [{"chunk_id": self.chunk_ids[0]}, {"chunk_id": self.chunk_ids[3]}],
        )
        matrix = comparison_matrix(decisions)
        by_field = {row["Campo"]: row for row in matrix}

        self.assertEqual(by_field["Producto"]["Decision 1"], "Ozempic")
        self.assertEqual(by_field["Resultado"]["Decision 1"], "Requerimiento")
        self.assertEqual(by_field["Resultado"]["Decision 2"], "Favorable")
        self.assertIn(
            "Evidencia <b>uno</b>",
            by_field["Evidencia seleccionada"]["Decision 1"],
        )

        unstructured = load_selected_decisions(
            self.database_path,
            [{"chunk_id": self.chunk_ids[2]}],
        )
        unstructured_matrix = comparison_matrix(unstructured)
        unstructured_by_field = {
            row["Campo"]: row for row in unstructured_matrix
        }
        self.assertEqual(
            unstructured_by_field["Estado de revisión"]["Decision 1"],
            "Sin ficha estructurada",
        )

    def test_timeline_uses_allowed_field_and_parameterized_like(self) -> None:
        product = search_timeline(self.database_path, "product", "OZEM")
        expediente = search_timeline(
            self.database_path, "expediente", "EXP-100"
        )
        injection = search_timeline(
            self.database_path,
            "product",
            "%' OR 1=1 --",
        )

        self.assertEqual([item.year for item in product], [2024, 2025])
        self.assertEqual(len(expediente), 2)
        self.assertEqual(injection, [])
        with self.assertRaises(ValueError):
            search_timeline(self.database_path, "unsafe_column", "Ozempic")
        with self.assertRaises(ValueError):
            search_timeline(self.database_path, "product", "x")

    def test_comparison_and_timeline_apply_current_human_review(self) -> None:
        review = ReviewEvent(
            event_id="event-1",
            decision_uid="dec_record-1",
            source_record_key="record-1",
            source_document_hash="hash-1",
            status="approved",
            reviewer="Revisora",
            reviewed_at="2026-01-02T00:00:00+00:00",
            notes="Verificado contra el PDF",
            corrections={
                "product_name": "Wegovy",
                "request_type_code": "registro_sanitario",
                "session_date": "2024-02-03",
            },
        )
        decisions = load_selected_decisions(
            self.database_path,
            [{"chunk_id": self.chunk_ids[0]}],
            review_events=[review],
        )
        corrected = search_timeline(
            self.database_path,
            "product",
            "Wegovy",
            review_events=[review],
        )
        old_value = search_timeline(
            self.database_path,
            "product",
            "Ozempic",
            review_events=[review],
        )

        self.assertEqual(decisions[0].product_name, "Wegovy")
        self.assertEqual(decisions[0].review_status, "approved")
        self.assertFalse(decisions[0].needs_review)
        self.assertEqual(decisions[0].request_type, "registro_sanitario")
        self.assertEqual([item.decision_uid for item in corrected], ["dec_record-1"])
        self.assertEqual([item.decision_uid for item in old_value], ["dec_record-2"])

        stale_review = ReviewEvent(
            event_id="event-stale",
            decision_uid="dec_record-1",
            source_record_key="record-1",
            source_document_hash="otro-hash",
            status="approved",
            reviewer="Revisora",
            reviewed_at="2026-01-03T00:00:00+00:00",
            notes="Revision de otra version",
            corrections={"product_name": "Producto incorrecto"},
        )
        with connect(self.database_path) as connection:
            connection.execute(
                "UPDATE regulatory_records SET needs_review = 0 "
                "WHERE decision_uid = ?",
                ("dec_record-1",),
            )
        stale = load_selected_decisions(
            self.database_path,
            [{"chunk_id": self.chunk_ids[0]}],
            review_events=[stale_review],
        )[0]
        self.assertEqual(stale.product_name, "Ozempic")
        self.assertEqual(stale.review_status, "stale")
        self.assertTrue(stale.review_stale)
        self.assertTrue(stale.needs_review)

    def test_page_corrections_reassociate_evidence_to_the_effective_range(self) -> None:
        review = ReviewEvent(
            event_id="event-page-range",
            decision_uid="dec_record-1",
            source_record_key="record-1",
            source_document_hash="hash-1",
            status="approved",
            reviewer="Revisora",
            reviewed_at="2026-01-04T00:00:00+00:00",
            notes="La ficha inicia realmente en la pagina 12",
            corrections={"page_number": 12, "end_page_number": 12},
        )

        old_page = load_selected_decisions(
            self.database_path,
            [{"chunk_id": self.chunk_ids[0]}],
            review_events=[review],
        )
        corrected_page = load_selected_decisions(
            self.database_path,
            [{"chunk_id": self.chunk_ids[2]}],
            review_events=[review],
        )

        self.assertEqual(len(old_page), 1)
        self.assertIsNone(old_page[0].record_id)
        self.assertEqual(
            [item.decision_uid for item in corrected_page],
            ["dec_record-1"],
        )
        self.assertEqual(corrected_page[0].page_number, 12)
        self.assertEqual(corrected_page[0].evidences[0].page, 12)

    def test_timeline_filters_reviews_before_applying_the_result_limit(self) -> None:
        bulk_document = insert_document(
            self.database_path,
            DocumentMetadata(
                title="Acta masiva 03 de 2023",
                url="https://www.invima.gov.co/biblioteca/download/3",
                year=2023,
                acta_number="03",
                section="SEMPB",
            ),
            "hash-bulk",
            [{"page": 1, "chunks": ["Decisiones masivas"]}],
        )
        with connect(self.database_path) as connection:
            for index in range(600):
                self._insert_record(
                    connection,
                    bulk_document,
                    f"bulk-{index:03d}",
                    1,
                    "Producto Foo",
                    "Ingrediente",
                    "Titular",
                    f"EXP-{index:03d}",
                    f"RAD-{index:03d}",
                    "Solicitud",
                    "Concepto",
                    "sin_clasificar",
                    "2026-01-01T00:00:00+00:00",
                )
        reviews = [
            ReviewEvent(
                event_id=f"review-{index:03d}",
                decision_uid=f"dec_bulk-{index:03d}",
                source_record_key=f"bulk-{index:03d}",
                source_document_hash="hash-bulk",
                status="approved",
                reviewer="Revisora",
                reviewed_at="2026-01-05T00:00:00+00:00",
                notes="",
                corrections={"product_name": "Producto Bar"},
            )
            for index in range(500)
        ]

        results = search_timeline(
            self.database_path,
            "product",
            "Producto Foo",
            limit=100,
            review_events=reviews,
        )

        self.assertEqual(len(results), 100)
        self.assertEqual(results[0].decision_uid, "dec_bulk-500")
        self.assertEqual(results[-1].decision_uid, "dec_bulk-599")

    def test_inputs_and_selected_evidence_are_bounded(self) -> None:
        with self.assertRaises(ValueError):
            search_timeline(
                self.database_path,
                "product",
                "x" * 501,
            )
        self.assertEqual(
            load_selected_decisions(
                self.database_path,
                [{"chunk_id": 2**80}],
            ),
            [],
        )

        with connect(self.database_path) as connection:
            page_id = int(
                connection.execute(
                    "SELECT id FROM pages WHERE page_number = 10"
                ).fetchone()[0]
            )
            connection.executemany(
                "INSERT INTO chunks (page_id, chunk_index, text) VALUES (?, ?, ?)",
                [
                    (page_id, index, f"Evidencia adicional {index}")
                    for index in range(2, 7)
                ],
            )
            chunk_ids = [
                int(row[0])
                for row in connection.execute(
                    "SELECT id FROM chunks WHERE page_id = ? ORDER BY chunk_index",
                    (page_id,),
                )
            ]

        decision = load_selected_decisions(
            self.database_path,
            [{"chunk_id": chunk_id} for chunk_id in chunk_ids],
        )[0]
        self.assertEqual(len(decision.evidences), 6)
        self.assertTrue(decision.evidence_truncated)

        review = ReviewEvent(
            event_id="bounded-review",
            decision_uid="dec_record-1",
            source_record_key="record-1",
            source_document_hash="hash-1",
            status="approved",
            reviewer="Revisora",
            reviewed_at="2026-01-05T00:00:00+00:00",
            notes="",
            corrections={},
        )
        with patch("services.comparison.MAX_REVIEW_EVENTS", 1):
            with self.assertRaises(ValueError):
                search_timeline(
                    self.database_path,
                    "product",
                    "Ozempic",
                    review_events=[review, review],
                )

    def test_preserves_two_distinct_records_on_the_same_page(self) -> None:
        with connect(self.database_path) as connection:
            document_id = int(
                connection.execute(
                    "SELECT document_id FROM pages WHERE page_number = 10"
                ).fetchone()[0]
            )
            self._insert_record(
                connection,
                document_id,
                "record-extra",
                10,
                "Producto diferente",
                "Ingrediente B",
                "Titular B",
                "EXP-200",
                "RAD-300",
                "Solicitud B",
                "Concepto B",
                "sin_clasificar",
                "2026-01-01T00:00:00+00:00",
            )

        decisions = load_selected_decisions(
            self.database_path,
            [{"chunk_id": self.chunk_ids[0]}],
        )

        self.assertEqual(len(decisions), 2)
        self.assertEqual(
            {item.decision_uid for item in decisions},
            {"dec_record-1", "dec_record-extra"},
        )
        exact = load_selected_decisions(
            self.database_path,
            [
                {
                    "chunk_id": self.chunk_ids[0],
                    "decision_uid": "dec_record-extra",
                }
            ],
        )
        self.assertEqual([item.decision_uid for item in exact], ["dec_record-extra"])

    def test_csv_is_valid_utf8_and_neutralizes_formulas(self) -> None:
        decisions = load_selected_decisions(
            self.database_path,
            [{"chunk_id": self.chunk_ids[0]}],
        )
        content = comparison_to_csv(decisions).decode("utf-8-sig")
        rows = list(csv.DictReader(StringIO(content)))

        self.assertEqual(rows[0]["producto"], "Ozempic")
        self.assertTrue(rows[0]["interesado"].startswith("'="))
        self.assertIn("#page=10", rows[0]["enlaces_evidencia"])

        timeline = search_timeline(self.database_path, "product", "Ozempic")
        timeline_content = timeline_to_csv(timeline).decode("utf-8-sig")
        self.assertEqual(len(list(csv.DictReader(StringIO(timeline_content)))), 2)

    def test_printable_html_escapes_document_content_and_has_page_links(self) -> None:
        decisions = load_selected_decisions(
            self.database_path,
            [{"chunk_id": self.chunk_ids[0]}],
        )
        timeline = search_timeline(self.database_path, "product", "Ozempic")
        report = printable_html_report(
            decisions,
            timeline=timeline,
            title="Reporte <privado>",
        ).decode("utf-8")

        self.assertIn("Reporte &lt;privado&gt;", report)
        self.assertIn("Acta &lt;script&gt;01&lt;/script&gt;", report)
        self.assertIn("Evidencia &lt;b&gt;uno&lt;/b&gt;", report)
        self.assertNotIn("<script>", report)
        self.assertIn("#page=10", report)
        self.assertIn("<th>Decision 1</th>", report)

        timeline_only = printable_html_report(
            [],
            timeline=timeline,
            title="Solo cronologia",
        ).decode("utf-8")
        self.assertNotIn("Comparación lado a lado", timeline_only)
        self.assertIn("<h2>Cronología</h2>", timeline_only)
        self.assertIn("La Sala emite concepto favorable.", timeline_only)

    def test_exports_enforce_total_size_limits(self) -> None:
        decisions = load_selected_decisions(
            self.database_path,
            [{"chunk_id": self.chunk_ids[0]}],
        )
        with patch("services.comparison.MAX_CSV_BYTES", 10):
            with self.assertRaises(ValueError):
                comparison_to_csv(decisions)
        with patch("services.comparison.MAX_HTML_BYTES", 10):
            with self.assertRaises(ValueError):
                printable_html_report(decisions)


if __name__ == "__main__":
    unittest.main()
