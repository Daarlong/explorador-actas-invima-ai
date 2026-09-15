from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import services.search as search_service
from services.database import connect, initialize_database, insert_document
from services.models import DocumentMetadata


READY_NEURAL_STATE = {
    "available": True,
    "reason": "ready",
    "message": "Índice semántico neuronal disponible.",
    "neural_status": "ready",
    "neural_runtime_installed": True,
}


class _SearchFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.database_path = root / "actas.db"
        self.semantic_path = root / "semantic.db"
        initialize_database(self.database_path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def insert(
        self,
        title: str,
        chunks: list[str],
        number: int,
        *,
        year: int = 2026,
    ) -> list[int]:
        insert_document(
            self.database_path,
            DocumentMetadata(
                title=title,
                url=f"https://www.invima.gov.co/biblioteca/download/{number}",
                year=year,
                acta_number=f"{number:02d}",
                section="SEMPB",
            ),
            f"hash-{number}",
            [{"page": 1, "text": " ".join(chunks), "chunks": chunks}],
        )
        with connect(self.database_path) as connection:
            rows = connection.execute(
                """
                SELECT c.id
                FROM chunks c
                JOIN pages p ON p.id = c.page_id
                JOIN documents d ON d.id = p.document_id
                WHERE d.title = ?
                ORDER BY c.id
                """,
                (title,),
            ).fetchall()
        return [int(row[0]) for row in rows]


class GlobalHybridAcceptanceTests(_SearchFixture):
    """Contratos del carril semántico global e independiente de FTS5."""

    def test_hybrid_can_include_a_semantic_only_result(self) -> None:
        lexical_id = self.insert(
            "Acta textual",
            ["Se discutieron beneficios dentro de indicaciones."],
            1,
        )[0]
        semantic_id = self.insert(
            "Acta conceptualmente relacionada",
            ["La terapia produjo una mejoría clínica sostenida."],
            2,
        )[0]
        observed_allowed_ids: list[list[int] | None] = []

        def fake_semantic_search(_path, _query, *, top_k, allowed_ids=None, **_kwargs):
            del top_k
            normalized = None if allowed_ids is None else list(allowed_ids)
            observed_allowed_ids.append(normalized)
            candidates = [(semantic_id, 0.99), (lexical_id, 0.20)]
            if normalized is None:
                return candidates
            allowed = set(normalized)
            return [item for item in candidates if item[0] in allowed]

        with (
            patch(
                "services.search.semantic_index_status",
                return_value=READY_NEURAL_STATE,
            ),
            patch(
                "services.search.semantic_search",
                side_effect=fake_semantic_search,
            ),
        ):
            response = search_service.search_corpus(
                self.database_path,
                self.semantic_path,
                "beneficios dentro de indicaciones",
                mode="hybrid",
                top_k=10,
            )

        self.assertEqual(observed_allowed_ids, [None])
        self.assertEqual(
            {result.chunk_id for result in response.results},
            {lexical_id, semantic_id},
        )

    def test_hybrid_global_lane_respects_filters_without_becoming_lexical_only(
        self,
    ) -> None:
        lexical_id = self.insert(
            "Acta filtrada textual",
            ["Beneficios para la indicación solicitada."],
            10,
            year=2025,
        )[0]
        semantic_id = self.insert(
            "Acta filtrada semántica",
            ["Se observó una mejoría terapéutica mantenida."],
            11,
            year=2025,
        )[0]
        outside_id = self.insert(
            "Acta fuera del filtro",
            ["Mejoría clínica fuera del periodo consultado."],
            12,
            year=2024,
        )[0]
        observed_allowed_ids: list[list[int] | None] = []

        def fake_semantic_search(_path, _query, *, top_k, allowed_ids=None, **_kwargs):
            del top_k
            normalized = None if allowed_ids is None else list(allowed_ids)
            observed_allowed_ids.append(normalized)
            candidates = [
                (outside_id, 1.0),
                (semantic_id, 0.99),
                (lexical_id, 0.20),
            ]
            if normalized is None:
                return candidates
            allowed = set(normalized)
            return [item for item in candidates if item[0] in allowed]

        with (
            patch(
                "services.search.semantic_index_status",
                return_value=READY_NEURAL_STATE,
            ),
            patch(
                "services.search.semantic_search",
                side_effect=fake_semantic_search,
            ),
        ):
            response = search_service.search_corpus(
                self.database_path,
                self.semantic_path,
                "beneficios indicación",
                mode="hybrid",
                top_k=10,
                filters={"years": [2025]},
            )

        self.assertEqual(len(observed_allowed_ids), 1)
        self.assertEqual(set(observed_allowed_ids[0] or ()), {lexical_id, semantic_id})
        returned = {result.chunk_id for result in response.results}
        self.assertIn(semantic_id, returned)
        self.assertNotIn(outside_id, returned)

    def test_hybrid_deduplicates_items_present_in_both_lanes(self) -> None:
        shared_id = self.insert(
            "Acta compartida",
            ["Balance beneficio riesgo favorable."],
            20,
        )[0]

        with (
            patch(
                "services.search.semantic_index_status",
                return_value=READY_NEURAL_STATE,
            ),
            patch(
                "services.search.semantic_search",
                return_value=[(shared_id, 0.98)],
            ),
        ):
            response = search_service.search_corpus(
                self.database_path,
                self.semantic_path,
                "balance beneficio riesgo",
                mode="hybrid",
                top_k=10,
            )

        self.assertEqual(
            [item.chunk_id for item in response.results].count(shared_id),
            1,
        )


class SearchEngineTraceAcceptanceTests(_SearchFixture):
    """La respuesta debe describir el motor usado, no el solicitado."""

    def test_neural_hybrid_reports_backend_without_fallback(self) -> None:
        chunk_id = self.insert("Acta neuronal", ["Eficacia terapéutica."], 30)[0]
        with (
            patch(
                "services.search.semantic_index_status",
                return_value=READY_NEURAL_STATE,
            ),
            patch(
                "services.search.semantic_search",
                return_value=[(chunk_id, 0.95)],
            ),
        ):
            response = search_service.search_corpus(
                self.database_path,
                self.semantic_path,
                "beneficio clínico",
                mode="hybrid",
                top_k=5,
            )

        self.assertEqual(response.used_mode, "hybrid")
        self.assertEqual(response.semantic_backend, "neural")
        self.assertIsNone(response.fallback_reason)

    def test_missing_semantic_index_reports_the_real_textual_fallback(self) -> None:
        self.insert("Acta textual", ["Envase de aluminio."], 31)

        response = search_service.search_corpus(
            self.database_path,
            self.semantic_path,
            "aluminio",
            mode="semantic",
            top_k=5,
        )

        self.assertEqual(response.requested_mode, "semantic")
        self.assertEqual(response.used_mode, "textual")
        self.assertEqual(response.semantic_backend, "none")
        self.assertTrue(response.fallback_reason)

    def test_installed_embeddings_without_runtime_report_local_fallback(self) -> None:
        chunk_id = self.insert("Acta local", ["Estabilidad del medicamento."], 32)[0]
        local_state = {
            **READY_NEURAL_STATE,
            "message": "Se usará el respaldo semántico local.",
            "neural_runtime_installed": False,
        }
        with (
            patch(
                "services.search.semantic_index_status",
                return_value=local_state,
            ),
            patch(
                "services.search.semantic_search",
                return_value=[(chunk_id, 0.75)],
            ),
        ):
            response = search_service.search_corpus(
                self.database_path,
                self.semantic_path,
                "conservación farmacéutica",
                mode="semantic",
                top_k=5,
            )

        self.assertEqual(response.used_mode, "semantic")
        self.assertEqual(response.semantic_backend, "local_fallback")
        self.assertTrue(response.fallback_reason)


class SearchPaginationAcceptanceTests(_SearchFixture):
    """Paginación por acta con conteos SQL verificables."""

    def setUp(self) -> None:
        super().setUp()
        for number in range(1, 28):
            self.insert(
                f"Acta paginada {number:02d}",
                [
                    f"Precedente común, fragmento A del acta {number}.",
                    f"Precedente común, fragmento B del acta {number}.",
                ],
                100 + number,
                year=2020 + (number % 5),
            )

    def search_page(self, **kwargs):
        operation = getattr(search_service, "search_corpus_page", None)
        self.assertIsNotNone(
            operation,
            "Falta la API pública search_corpus_page",
        )
        return operation(
            self.database_path,
            self.semantic_path,
            "precedente común",
            **kwargs,
        )

    def test_textual_pages_do_not_overlap_and_report_exact_global_totals(self) -> None:
        first = self.search_page(
            mode="textual",
            page=1,
            page_size=10,
            order="relevance",
        )
        second = self.search_page(
            mode="textual",
            page=2,
            page_size=10,
            order="relevance",
        )

        first_documents = {item.url for item in first.results}
        second_documents = {item.url for item in second.results}
        self.assertEqual(len(first_documents), 10)
        self.assertEqual(len(second_documents), 10)
        self.assertTrue(first_documents.isdisjoint(second_documents))
        self.assertEqual(first.total_documents, 27)
        self.assertEqual(first.total_fragments, 54)
        self.assertEqual(first.total_pages, 3)
        self.assertTrue(first.has_next)
        self.assertTrue(first.totals_exact)
        self.assertIsNone(first.facet_candidate_ids)
        self.assertEqual(first.page, 1)
        self.assertEqual(first.page_size, 10)

    def test_filters_are_applied_before_pagination_and_totals(self) -> None:
        page = self.search_page(
            mode="textual",
            page=1,
            page_size=100,
            order="relevance",
            filters={"years": [2024]},
        )

        expected_documents = sum(1 for number in range(1, 28) if number % 5 == 4)
        self.assertEqual(page.total_documents, expected_documents)
        self.assertEqual(page.total_fragments, expected_documents * 2)
        self.assertEqual({item.year for item in page.results}, {2024})
        self.assertTrue(page.totals_exact)

    def test_newest_and_oldest_order_apply_to_the_entire_result_set(self) -> None:
        newest = self.search_page(
            mode="textual",
            page=1,
            page_size=5,
            order="newest",
        )
        oldest = self.search_page(
            mode="textual",
            page=1,
            page_size=5,
            order="oldest",
        )

        self.assertEqual({item.year for item in newest.results}, {2024})
        self.assertEqual({item.year for item in oldest.results}, {2020})

    def test_semantic_pagination_never_labels_a_candidate_pool_as_exact_total(
        self,
    ) -> None:
        with (
            patch(
                "services.search.semantic_index_status",
                return_value=READY_NEURAL_STATE,
            ),
            patch(
                "services.search.semantic_search",
                return_value=[(1, 0.99), (3, 0.90), (5, 0.80)],
            ),
        ):
            page = self.search_page(
                mode="semantic",
                page=1,
                page_size=2,
                order="relevance",
            )

        self.assertFalse(page.totals_exact)
        self.assertEqual(set(page.facet_candidate_ids or ()), {1, 3, 5})


if __name__ == "__main__":
    unittest.main()
