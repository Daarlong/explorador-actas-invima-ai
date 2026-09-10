from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.database import initialize_database, insert_document, search_chunks
from services.models import DocumentMetadata
from services.search import search_corpus


class PhraseSearchRegressionTests(unittest.TestCase):
    """Contratos de búsqueda para el esquema FTS compacto de producción."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary.name) / "actas.db"
        self.semantic_path = Path(self.temporary.name) / "semantic.db"
        initialize_database(self.database_path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _insert(self, title: str, text: str, number: int) -> None:
        insert_document(
            self.database_path,
            DocumentMetadata(
                title=title,
                url=f"https://www.invima.gov.co/biblioteca/download/{number}",
                year=2026,
                acta_number=f"{number:02d}",
                section="SEMPB",
            ),
            f"hash-{number}",
            [{"page": 1, "text": text, "chunks": [text]}],
        )

    def test_exact_phrase_works_with_detail_column_fts_index(self) -> None:
        self._insert(
            "Acta con frase literal",
            "La Sala recomienda aprobar la indicación solicitada.",
            1,
        )
        with sqlite3.connect(self.database_path) as connection:
            fts_sql = str(
                connection.execute(
                    "SELECT sql FROM sqlite_master WHERE name = 'chunks_fts'"
                ).fetchone()[0]
            ).lower()
        self.assertIn("detail=column", fts_sql)

        results = search_chunks(
            self.database_path,
            "recomienda aprobar la indicación solicitada",
            exact_phrase=True,
        )

        self.assertEqual([item.title for item in results], ["Acta con frase literal"])

    def test_exact_phrase_normalizes_stopwords_accents_and_line_breaks(self) -> None:
        self._insert(
            "Acta con salto de línea",
            "La Comisión Revisora emitió\nel concepto favorable solicitado.",
            1,
        )
        self._insert(
            "Acta con palabras reordenadas",
            "El concepto solicitado fue emitido por la comisión revisora.",
            2,
        )

        results = search_chunks(
            self.database_path,
            "la comision revisora emitio el concepto favorable",
            exact_phrase=True,
        )

        self.assertEqual([item.title for item in results], ["Acta con salto de línea"])

    def test_literal_lane_filters_before_the_per_document_limit(self) -> None:
        chunks = [
            f"La sala solicita información técnica adicional del lote {number}."
            for number in range(8)
        ]
        chunks.append(
            "La sala concluye que el balance beneficio riesgo es favorable."
        )
        insert_document(
            self.database_path,
            DocumentMetadata(
                title="Acta con numerosos fragmentos",
                url="https://www.invima.gov.co/biblioteca/download/99",
                year=2026,
                acta_number="99",
                section="SEMPB",
            ),
            "hash-many-chunks",
            [{"page": 1, "text": " ".join(chunks), "chunks": chunks}],
        )

        results = search_chunks(
            self.database_path,
            "balance beneficio riesgo es favorable",
            top_k=1,
            exact_phrase=True,
            max_chunks_per_document=1,
        )

        self.assertEqual(len(results), 1)
        self.assertIn("balance beneficio riesgo", results[0].text.lower())

    def test_punctuation_in_text_query_does_not_create_an_fts_phrase(self) -> None:
        self._insert(
            "Acta beneficio-riesgo",
            "Se analizó cuidadosamente la relación beneficio riesgo.",
            3,
        )

        for query in ("beneficio-riesgo", "beneficio_riesgo", "beneficio/riesgo"):
            with self.subTest(query=query):
                results = search_chunks(self.database_path, query)

                self.assertEqual(
                    [item.title for item in results],
                    ["Acta beneficio-riesgo"],
                )
                exact_results = search_chunks(
                    self.database_path,
                    query,
                    exact_phrase=True,
                )
                self.assertEqual(
                    [item.title for item in exact_results],
                    ["Acta beneficio-riesgo"],
                )

    def test_hybrid_ranks_literal_phrase_before_semantic_paraphrases(self) -> None:
        phrase = (
            "la sala considera que el balance beneficio riesgo es favorable "
            "para la indicación solicitada"
        )
        self._insert("Acta literal", phrase, 1)
        reordered = (
            "indicación solicitada riesgo favorable considera sala balance "
            "beneficio"
        )
        for number in range(2, 22):
            self._insert(f"Acta semántica {number}", reordered, number)

        # Aísla la regla de fusión: simula que el modelo semántico prefiere las
        # paráfrasis y coloca la coincidencia literal al final de su lista.
        semantic_order = [*range(2, 22), 1]
        semantic_hits = [
            (chunk_id, float(100 - position))
            for position, chunk_id in enumerate(semantic_order)
        ]
        with (
            patch(
                "services.search.semantic_index_status",
                return_value={
                    "available": True,
                    "reason": "ready",
                    "message": "Índice semántico disponible.",
                },
            ),
            patch("services.search.semantic_search", return_value=semantic_hits),
        ):
            response = search_corpus(
                self.database_path,
                self.semantic_path,
                phrase,
                mode="hybrid",
                top_k=1,
            )

        self.assertEqual(response.used_mode, "hybrid")
        self.assertEqual(response.results[0].title, "Acta literal")

    def test_textual_mode_also_prioritizes_a_literal_phrase(self) -> None:
        phrase = "concepto favorable para la indicación solicitada"
        self._insert("Acta literal textual", phrase, 1)
        for number in range(2, 22):
            self._insert(
                f"Acta palabras dispersas {number}",
                "indicación concepto solicitado favorable para otro trámite",
                number,
            )

        response = search_corpus(
            self.database_path,
            self.semantic_path,
            phrase,
            mode="textual",
            top_k=1,
        )

        self.assertEqual(response.used_mode, "textual")
        self.assertEqual(response.results[0].title, "Acta literal textual")


if __name__ == "__main__":
    unittest.main()
