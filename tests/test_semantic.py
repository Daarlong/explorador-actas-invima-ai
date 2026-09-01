import sqlite3
import tempfile
import unittest
from pathlib import Path

from services.semantic import (
    SemanticDocument,
    SemanticIndexError,
    build_semantic_index,
    build_semantic_documents_index,
    combine_rankings,
    query_semantic_index,
    semantic_index_info,
    semantic_index_status,
    semantic_search,
    semantic_source_fingerprint,
)


class SemanticIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.index_path = Path(self.temp_dir.name) / "semantic.sqlite"
        self.documents = [
            SemanticDocument(
                1,
                "El felino doméstico pequeño duerme tranquilo dentro de la casa.",
            ),
            SemanticDocument(
                2,
                "El gato doméstico pequeño duerme tranquilo dentro de la casa.",
            ),
            SemanticDocument(
                3,
                "El expediente contiene un radicado para evaluar el envase de aluminio.",
            ),
        ]

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _build(self):
        return build_semantic_documents_index(
            self.documents,
            self.index_path,
            lexical_dimension=128,
            semantic_dimension=48,
            context_window=3,
            min_df=1,
            max_vocabulary=500,
        )

    def test_builds_persistent_index_and_reports_metadata(self) -> None:
        summary = self._build()
        self.assertTrue(self.index_path.exists())
        self.assertEqual(summary.documents_indexed, 3)
        self.assertEqual(summary.documents_skipped_empty, 0)
        self.assertGreater(summary.vocabulary_size, 5)
        self.assertGreater(summary.index_size_bytes, 0)

        info = semantic_index_info(self.index_path)
        self.assertEqual(info["documents"], 3)
        self.assertEqual(info["method"], "hashed_tfidf+distributional_random_indexing")
        self.assertEqual(info["semantic_dimension"], 48)

    def test_exact_term_remains_retrievable_without_external_model(self) -> None:
        self._build()
        hits = query_semantic_index(
            self.index_path,
            "radicado aluminio",
            top_k=3,
            lexical_weight=1.0,
            distributional_weight=0.0,
        )
        self.assertTrue(hits)
        self.assertEqual(hits[0].item_id, 3)
        self.assertGreater(hits[0].lexical_score, 0.0)

    def test_distributional_signal_links_terms_used_in_similar_contexts(self) -> None:
        self._build()
        hits = query_semantic_index(
            self.index_path,
            "felino",
            top_k=3,
            lexical_weight=0.0,
            distributional_weight=1.0,
            min_score=0.0,
        )
        positions = {hit.item_id: position for position, hit in enumerate(hits)}
        self.assertLess(positions[2], positions[3])
        feline_neighbor = next(hit for hit in hits if hit.item_id == 2)
        unrelated = next(hit for hit in hits if hit.item_id == 3)
        self.assertGreater(
            feline_neighbor.distributional_score,
            unrelated.distributional_score,
        )

    def test_can_rerank_only_explicit_candidates(self) -> None:
        self._build()
        hits = query_semantic_index(
            self.index_path,
            "doméstico tranquilo",
            candidate_ids=[2, 3, 999],
            top_k=5,
            min_score=0.0,
        )
        self.assertEqual({hit.item_id for hit in hits}, {2, 3})
        self.assertEqual(hits[0].item_id, 2)

    def test_empty_text_is_skipped_and_duplicate_ids_are_rejected(self) -> None:
        summary = build_semantic_documents_index(
            [SemanticDocument(7, " "), SemanticDocument(8, "semaglutida")],
            self.index_path,
            min_df=1,
        )
        self.assertEqual(summary.documents_indexed, 1)
        self.assertEqual(summary.documents_skipped_empty, 1)

        with self.assertRaisesRegex(ValueError, "duplicado"):
            build_semantic_documents_index(
                [SemanticDocument(1, "uno"), SemanticDocument(1, "dos")],
                self.index_path,
                min_df=1,
            )
        self.assertEqual(semantic_index_info(self.index_path)["documents"], 1)

    def test_missing_or_incompatible_index_is_handled_explicitly(self) -> None:
        self.assertEqual(query_semantic_index(self.index_path, "consulta"), [])
        self.assertEqual(semantic_index_status(self.index_path)["reason"], "missing")
        with self.assertRaises(SemanticIndexError):
            semantic_index_info(self.index_path)

        self.index_path.write_text("no es sqlite", encoding="utf-8")
        with self.assertRaises(SemanticIndexError):
            semantic_index_info(self.index_path)
        self.assertEqual(semantic_index_status(self.index_path)["reason"], "invalid")

    def test_detects_incomplete_semantic_index(self) -> None:
        self._build()
        with sqlite3.connect(self.index_path) as connection:
            connection.execute("DELETE FROM items WHERE item_id = 1")
        with self.assertRaisesRegex(SemanticIndexError, "cobertura"):
            semantic_index_info(self.index_path)
        self.assertEqual(semantic_index_status(self.index_path)["reason"], "invalid")

    def test_public_api_builds_from_main_chunks_database(self) -> None:
        database_path = Path(self.temp_dir.name) / "actas.db"
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                "CREATE TABLE chunks (id INTEGER PRIMARY KEY, text TEXT NOT NULL)"
            )
            connection.executemany(
                "INSERT INTO chunks (id, text) VALUES (?, ?)",
                [(11, "semaglutida diabetes"), (12, "insulina diabetes")],
            )

        summary = build_semantic_index(
            database_path,
            self.index_path,
            min_df=1,
            lexical_dimension=64,
            semantic_dimension=32,
        )
        self.assertEqual(summary.documents_indexed, 2)
        self.assertEqual(
            summary.source_fingerprint,
            semantic_source_fingerprint(database_path),
        )
        self.assertEqual(
            semantic_index_info(self.index_path)["source_fingerprint"],
            summary.source_fingerprint,
        )
        compact_hits = semantic_search(
            self.index_path,
            "semaglutida",
            top_k=2,
            allowed_ids=[11, 12],
        )
        self.assertTrue(compact_hits)
        self.assertEqual(compact_hits[0][0], 11)
        self.assertTrue(semantic_index_status(self.index_path)["available"])

        with self.assertRaisesRegex(ValueError, "distinto"):
            build_semantic_index(database_path, database_path, min_df=1)

    def test_semantic_only_unknown_term_returns_no_results(self) -> None:
        build_semantic_documents_index(
            [SemanticDocument(1, "semaglutida diabetes")],
            self.index_path,
            min_df=2,
        )
        self.assertEqual(
            query_semantic_index(
                self.index_path,
                "término-inexistente",
                lexical_weight=0.0,
                distributional_weight=1.0,
            ),
            [],
        )


class HybridRankingTests(unittest.TestCase):
    def test_combines_normalized_lexical_and_semantic_scores(self) -> None:
        combined = combine_rankings(
            {1: 10.0, 2: 5.0},
            {2: 1.0, 3: 0.8},
            lexical_weight=0.6,
            semantic_weight=0.4,
            top_k=3,
        )
        self.assertEqual([item.item_id for item in combined], [2, 1, 3])
        self.assertEqual(combined[0].lexical_score, 0.5)
        self.assertEqual(combined[0].semantic_score, 1.0)

    def test_can_restrict_merge_to_lexical_candidates(self) -> None:
        combined = combine_rankings(
            {1: 1.0},
            {2: 1.0},
            include_semantic_only=False,
        )
        self.assertEqual([item.item_id for item in combined], [1])

    def test_rejects_invalid_weights(self) -> None:
        with self.assertRaises(ValueError):
            combine_rankings({}, {}, lexical_weight=0.0, semantic_weight=0.0)


if __name__ == "__main__":
    unittest.main()
