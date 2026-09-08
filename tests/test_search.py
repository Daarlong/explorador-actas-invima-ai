from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from services.database import initialize_database, insert_document
from services.models import DocumentMetadata
from services.search import search_corpus
from services.semantic import build_semantic_index


class SearchServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.database_path = root / "actas.db"
        self.semantic_path = root / "semantic.db"
        initialize_database(self.database_path)
        samples = [
            ("Acta felino", "El felino doméstico duerme tranquilo en casa."),
            ("Acta gato", "El gato doméstico duerme tranquilo en casa."),
            ("Acta envase", "Se evaluó un envase de aluminio para el producto."),
        ]
        for number, (title, text) in enumerate(samples, start=1):
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
                [{"page": 1, "chunks": [text]}],
            )
        build_semantic_index(
            self.database_path,
            self.semantic_path,
            lexical_dimension=64,
            semantic_dimension=32,
            min_df=1,
            max_vocabulary=500,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_hybrid_search_uses_both_signals(self) -> None:
        response = search_corpus(
            self.database_path,
            self.semantic_path,
            "felino doméstico",
            mode="hybrid",
            top_k=3,
        )
        self.assertEqual(response.used_mode, "hybrid")
        self.assertTrue(response.semantic_available)
        self.assertEqual(response.results[0].title, "Acta felino")
        self.assertTrue(all(item.match_type == "hybrid" for item in response.results))

    def test_missing_semantic_index_falls_back_to_text(self) -> None:
        response = search_corpus(
            self.database_path,
            Path(self.temporary.name) / "missing.db",
            "aluminio",
            mode="semantic",
        )
        self.assertEqual(response.used_mode, "textual")
        self.assertEqual(response.results[0].title, "Acta envase")

    def test_stale_semantic_index_falls_back_without_hydrating_wrong_ids(self) -> None:
        insert_document(
            self.database_path,
            DocumentMetadata(
                title="Acta nueva",
                url="https://www.invima.gov.co/biblioteca/download/nueva",
                year=2026,
                acta_number="04",
                section="SEMPB",
            ),
            "hash-nueva",
            [{"page": 1, "chunks": ["liraglutida incorporada recientemente"]}],
        )
        response = search_corpus(
            self.database_path,
            self.semantic_path,
            "liraglutida",
            mode="hybrid",
            top_k=3,
        )
        self.assertEqual(response.used_mode, "textual")
        self.assertFalse(response.semantic_available)
        self.assertIn("no corresponde", response.semantic_message)
        self.assertEqual(response.results[0].title, "Acta nueva")

    def test_semantic_results_are_diversified_before_final_limit(self) -> None:
        root = Path(self.temporary.name)
        database = root / "diverse-actas.db"
        semantic = root / "diverse-semantic.db"
        initialize_database(database)
        insert_document(
            database,
            DocumentMetadata(
                title="Acta dominante",
                url="https://www.invima.gov.co/biblioteca/download/dominante",
            ),
            "dominante",
            [
                {
                    "page": 1,
                    "chunks": [
                        f"estabilidad medicamento común {index}"
                        for index in range(20)
                    ],
                }
            ],
        )
        for index in range(5):
            insert_document(
                database,
                DocumentMetadata(
                    title=f"Acta alternativa {index}",
                    url=(
                        "https://www.invima.gov.co/biblioteca/download/"
                        f"alternativa-{index}"
                    ),
                ),
                f"alternativa-{index}",
                [{"page": 1, "chunks": ["estabilidad medicamento común"]}],
            )
        build_semantic_index(
            database,
            semantic,
            lexical_dimension=64,
            semantic_dimension=32,
            min_df=1,
        )

        response = search_corpus(
            database,
            semantic,
            "estabilidad medicamento",
            mode="semantic",
            top_k=8,
        )
        self.assertEqual(len(response.results), 8)
        self.assertGreater(len({item.url for item in response.results}), 1)


if __name__ == "__main__":
    unittest.main()
