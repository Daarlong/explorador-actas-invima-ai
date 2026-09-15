from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from services.analytics import analytics_drilldown, build_corpus_analytics
from services.database import (
    DATABASE_SCHEMA_VERSION,
    connect,
    initialize_database,
    insert_document,
)
from services.facets import get_search_facets
from services.models import DocumentMetadata
from services.search import search_corpus_page
from validate_search import validate_published_corpus


ROOT = Path(__file__).resolve().parents[1]


class V011AcceptanceTests(unittest.TestCase):
    """Contratos transversales comprometidos para la version 0.11."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "actas.db"
        self.semantic = self.root / "semantic.db"
        self.ann = self.root / "semantic-ann.db"
        initialize_database(self.database)

        self.documents = (
            self._add_document(
                title="Acta 01 de 2025 - parte 1",
                url="https://www.invima.gov.co/biblioteca/download/101",
                year=2025,
                acta="01",
                section="SEMPB",
                part="1",
                text=(
                    "Certificacion de buenas practicas de manufactura. "
                    "El radicado 20231234567 acredita beneficio clinico."
                ),
                chunks=(
                    "Certificacion de buenas practicas de manufactura.",
                    "El radicado 20231234567 acredita beneficio clinico.",
                    "Otro fragmento repite beneficio dentro de la misma acta.",
                ),
            ),
            self._add_document(
                title="Acta 01 de 2025 - parte 2",
                url="https://www.invima.gov.co/biblioteca/download/102",
                year=2025,
                acta="01",
                section="SEMPB",
                part="2",
                text="La relacion beneficio riesgo fue discutida por la Sala.",
                chunks=("La relacion beneficio riesgo fue discutida por la Sala.",),
            ),
            self._add_document(
                title="Acta 02 de 2026",
                url="https://www.invima.gov.co/biblioteca/download/103",
                year=2026,
                acta="02",
                section="OTRA",
                part=None,
                text="El beneficio terapeutico fue revisado en sesion.",
                chunks=("El beneficio terapeutico fue revisado en sesion.",),
            ),
        )
        with connect(self.database) as connection:
            self._insert_record(
                connection,
                self.documents[0],
                key="decision-1",
                outcome="aprobado",
                request_type="indicaciones",
            )
            self._insert_record(
                connection,
                self.documents[1],
                key="decision-2",
                outcome="requerido",
                request_type="modificacion",
            )
            for document_id, record_count in zip(self.documents, (1, 1, 0)):
                connection.execute(
                    """
                    INSERT INTO document_extractions (
                        document_id, document_hash, extractor_version, status,
                        record_count, extracted_at
                    ) VALUES (?, ?, 'acceptance', 'complete', ?, '2026-09-15')
                    """,
                    (document_id, f"hash-{document_id}", record_count),
                )
            connection.commit()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _add_document(
        self,
        *,
        title: str,
        url: str,
        year: int,
        acta: str,
        section: str,
        part: str | None,
        text: str,
        chunks: tuple[str, ...],
    ) -> int:
        return insert_document(
            self.database,
            DocumentMetadata(
                title=title,
                url=url,
                year=year,
                acta_number=acta,
                section=section,
                part=part,
            ),
            f"hash-{url.rsplit('/', 1)[-1]}",
            [{"page": 1, "text": text, "chunks": list(chunks)}],
        )

    @staticmethod
    def _insert_record(
        connection,
        document_id: int,
        *,
        key: str,
        outcome: str,
        request_type: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO regulatory_records (
                document_id, record_key, decision_uid, page_number,
                end_page_number, numeral, request_type_code, product_name,
                normalized_product_name, active_ingredient,
                normalized_active_ingredient, interested_party,
                normalized_interested_party, request_text, concept_text,
                outcome_code, extraction_method, confidence,
                extractor_version, created_at
            ) VALUES (?, ?, ?, 1, 1, '3.1', ?, 'Producto de prueba',
                      'producto de prueba', 'Semaglutida', 'semaglutida',
                      'Laboratorio de prueba', 'laboratorio de prueba',
                      'Solicitud verificable', 'Concepto fuente verificable', ?,
                      'labels', 0.95, 'acceptance', '2026-09-15')
            """,
            (document_id, key, f"uid-{key}", request_type, outcome),
        )

    def test_expansion_recovers_a_variant_and_preserves_the_source_query(self) -> None:
        response = search_corpus_page(
            self.database,
            self.semantic,
            "BPM",
            mode="textual",
            page_size=10,
        )

        self.assertEqual(response.used_mode, "textual")
        self.assertEqual(response.query_terms_original, ("bpm",))
        self.assertIn(
            "buenas practicas de manufactura", response.query_terms_expanded
        )
        self.assertEqual(response.query_expansion_version, "2026.09.1")
        self.assertTrue(
            any("parte 1" in result.title.lower() for result in response.results)
        )

    def test_exact_phrases_and_identifiers_are_never_expanded(self) -> None:
        phrase = search_corpus_page(
            self.database,
            self.semantic,
            "balance beneficio riesgo",
            mode="textual",
            page_size=10,
            exact_phrase=True,
        )
        identifier = search_corpus_page(
            self.database,
            self.semantic,
            "20231234567",
            mode="textual",
            page_size=10,
        )

        self.assertEqual(phrase.query_terms_expanded, ())
        self.assertEqual(phrase.query_expansion_skipped_reason, "exact_phrase")
        self.assertEqual(identifier.query_terms_original, ("20231234567",))
        self.assertEqual(identifier.query_terms_expanded, ())
        self.assertEqual(
            identifier.query_expansion_skipped_reason, "identifier_present"
        )
        self.assertEqual(identifier.total_documents, 1)

    def test_facets_count_distinct_acts_respect_filters_and_label_scope(self) -> None:
        global_facets = get_search_facets(self.database, "beneficio")
        filtered = get_search_facets(
            self.database, "beneficio", filters={"years": [2025]}
        )
        with connect(self.database) as connection:
            bounded_ids = tuple(
                int(row[0])
                for row in connection.execute(
                    """
                    SELECT c.id FROM chunks c
                    JOIN pages p ON p.id=c.page_id
                    WHERE p.document_id=?
                    ORDER BY c.id
                    """,
                    (self.documents[0],),
                )
            )
        bounded = get_search_facets(
            self.database,
            "consulta semantica sin dependencia textual",
            candidate_chunk_ids=bounded_ids,
        )

        # Los totales preservan ambas unidades: tres PDF/partes y dos actas.
        self.assertEqual(global_facets.total_documents, 3)
        self.assertEqual(global_facets.total_acts, 2)
        self.assertGreater(global_facets.total_fragments, global_facets.total_documents)
        self.assertTrue(global_facets.exact)
        self.assertEqual(global_facets.scope, "full_textual")
        self.assertEqual(filtered.total_documents, 2)
        self.assertEqual(filtered.total_acts, 1)
        # Las dos partes de Acta 01 se presentan como una sola acta filtrable.
        self.assertEqual(
            {item.value: item.count for item in filtered.facets["sections"]},
            {"SEMPB": 1},
        )
        # La propia faceta de anio se autoexcluye, manteniendo los demas filtros.
        self.assertEqual(
            {item.value: item.count for item in filtered.facets["years"]},
            {2026: 1, 2025: 1},
        )
        self.assertEqual(filtered.metadata["years"].unit, "acts")
        self.assertFalse(bounded.exact)
        self.assertEqual(bounded.scope, "bounded_candidates")
        self.assertEqual(bounded.total_documents, 1)
        self.assertEqual(bounded.total_acts, 1)
        self.assertFalse(bounded.metadata["years"].exact)

    def test_analytics_keeps_units_denominators_and_traceable_drilldown(self) -> None:
        report = build_corpus_analytics(self.database)
        outcome = report["dimensions"]["outcome"]
        detail = analytics_drilldown(
            self.database, "outcome", "aprobado", limit=10
        )

        self.assertEqual(report["totals"]["documents"], {
            "available": True,
            "value": 3,
            "unit": "documents",
        })
        self.assertEqual(report["totals"]["unique_acts"]["value"], 2)
        self.assertEqual(report["totals"]["unique_acts"]["unit"], "acts")
        self.assertEqual(report["totals"]["records"]["value"], 2)
        self.assertEqual(report["totals"]["records"]["unit"], "extracted_records")
        self.assertEqual(outcome["denominator"], {
            "value": 2,
            "unit": "extracted_records",
        })
        self.assertFalse(outcome["creates_new_official_decisions"])
        self.assertIn("extracted", outcome["meaning"])
        self.assertTrue(detail["available"])
        self.assertEqual(detail["total"], 1)
        evidence = detail["items"][0]
        for key in ("document_id", "record_id", "title", "url", "page"):
            self.assertIn(key, evidence)
        self.assertTrue(evidence["url"].startswith("https://www.invima.gov.co/"))
        self.assertEqual(evidence["page"], 1)

    def test_published_validation_is_technical_and_relevance_is_advisory(self) -> None:
        config = {
            "format_version": 1,
            "schema": {
                "expected_version": DATABASE_SCHEMA_VERSION,
                "required_tables": ["documents", "pages", "chunks", "chunks_fts"],
                "minimum_rows": {"documents": 1},
            },
            "packages": {"required": [], "verify_sha256": False},
            "semantic": {
                "required": False,
                "require_neural_ready": False,
                "ann_required": False,
            },
            "probes": {
                "candidate_rows": 10,
                "textual": {"enabled": False},
                "phrase": {"enabled": False},
                "fields": {"enabled": False},
                "pagination": {"enabled": False},
            },
            "explicit_cases": [
                {
                    "name": "Caso orientativo sin resultado",
                    "query": "terminoquejamasaparece",
                    "minimum_results": 1,
                }
            ],
        }

        report = validate_published_corpus(
            database_path=self.database,
            semantic_path=self.semantic,
            ann_path=self.ann,
            config=config,
        )
        explicit = next(
            check for check in report["checks"] if check["check_id"] == "explicit.1"
        )

        self.assertEqual(report["scope"], "technical_search_validation")
        self.assertEqual(report["status"], "passed")
        self.assertEqual(explicit["status"], "warning")
        self.assertEqual(explicit["severity"], "advisory")
        self.assertTrue(any("No califica" in goal for goal in report["non_goals"]))

        workflow = ROOT / ".github" / "workflows" / "validate-search.yml"
        self.assertTrue(workflow.is_file())
        workflow_text = workflow.read_text(encoding="utf-8")
        self.assertIn("contents: read", workflow_text)
        self.assertIn("python validate_search.py", workflow_text)
        self.assertIn("actions/upload-artifact@v6", workflow_text)

    def test_navigation_exposes_the_analytics_page(self) -> None:
        page = ROOT / "pages" / "6_Analitica.py"
        self.assertTrue(page.is_file())
        home = (ROOT / "home.py").read_text(encoding="utf-8")
        self.assertIn("pages/6_Analitica.py", home)
        self.assertIn("Analítica", home)


if __name__ == "__main__":
    unittest.main()
