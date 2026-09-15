from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import numpy as np
except (ImportError, ModuleNotFoundError):  # pragma: no cover - entorno mínimo
    np = None

from services.ann import (
    ANN_METHOD,
    AnnIndexError,
    ann_index_status,
    build_ann_index,
    query_ann_index,
)


@unittest.skipIf(np is None, "NumPy no está instalado")
class AnnIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.semantic_path = self.root / "semantic.db"
        self.ann_path = self.root / "semantic-ann.db"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _quantized(vector: list[float]) -> bytes:
        values = np.asarray(vector, dtype=np.float32)
        values /= np.linalg.norm(values)
        return np.rint(values * 127.0).clip(-127, 127).astype(np.int8).tobytes()

    def _semantic_database(
        self,
        vectors: dict[int, list[float]],
        *,
        source_fingerprint: str = "corpus-vigente",
    ) -> None:
        dimension = len(next(iter(vectors.values())))
        with sqlite3.connect(self.semantic_path) as connection:
            connection.executescript(
                """
                CREATE TABLE metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE items (
                    item_id INTEGER PRIMARY KEY,
                    content_hash BLOB NOT NULL,
                    lexical_vector BLOB NOT NULL,
                    distributional_vector BLOB NOT NULL
                );
                CREATE TABLE neural_embeddings (
                    content_hash BLOB PRIMARY KEY,
                    neural_vector BLOB NOT NULL
                );
                """
            )
            metadata = {
                "format_version": 2,
                "documents": len(vectors),
                "source_fingerprint": source_fingerprint,
                "build_signature": "semantic-build-test-v1",
                "neural_status": "ready",
                "neural_documents": len(vectors),
                "neural_dimension": dimension,
                "neural_model_id": "test/multilingual-model",
                "neural_model_revision": "test-revision",
                "neural_vector_encoding": "signed_int8_unit_vector",
            }
            connection.executemany(
                "INSERT INTO metadata VALUES (?, ?)",
                [
                    (key, json.dumps(value, ensure_ascii=False))
                    for key, value in metadata.items()
                ],
            )
            for item_id, vector in vectors.items():
                content_hash = int(item_id).to_bytes(32, "little")
                connection.execute(
                    "INSERT INTO items VALUES (?, ?, ?, ?)",
                    (item_id, content_hash, b"x", b"y"),
                )
                connection.execute(
                    "INSERT INTO neural_embeddings VALUES (?, ?)",
                    (content_hash, self._quantized(vector)),
                )

    def test_build_reuses_persisted_vectors_and_status_matches_semantic(self) -> None:
        self._semantic_database(
            {
                10: [1.0, 0.0, 0.0, 0.0],
                20: [0.0, 1.0, 0.0, 0.0],
                30: [0.0, 0.0, 1.0, 0.0],
            }
        )

        summary = build_ann_index(
            self.semantic_path,
            self.ann_path,
            table_count=4,
            bits_per_table=4,
            batch_size=2,
        )

        self.assertEqual(summary.items_indexed, 3)
        self.assertEqual(summary.memberships, 12)
        state = ann_index_status(self.ann_path, self.semantic_path)
        self.assertTrue(state["available"], state)
        self.assertEqual(state["method"], ANN_METHOD)
        self.assertEqual(state["items"], 3)
        self.assertEqual(state["table_count"], 4)

    def test_query_finds_global_nearest_vector_and_reports_trace(self) -> None:
        self._semantic_database(
            {
                1: [1.0, 0.0, 0.0, 0.0],
                2: [0.0, 1.0, 0.0, 0.0],
                3: [-1.0, 0.0, 0.0, 0.0],
                4: [0.0, 0.0, 1.0, 0.0],
            }
        )
        build_ann_index(
            self.semantic_path,
            self.ann_path,
            table_count=6,
            bits_per_table=5,
        )

        result = query_ann_index(
            self.ann_path,
            self.semantic_path,
            [0.99, 0.01, 0.0, 0.0],
            top_k=2,
            minimum_candidates=2,
            max_probe_radius=2,
        )

        self.assertEqual(result.hits[0].item_id, 1)
        self.assertGreater(result.hits[0].score, result.hits[1].score)
        self.assertEqual(result.trace.engine, "neural_ann_lsh")
        self.assertGreater(result.trace.probes, 0)
        self.assertGreaterEqual(result.trace.candidates_scored, 2)

    def test_ann_retrieves_without_scanning_the_complete_corpus(self) -> None:
        generator = np.random.default_rng(20260914)
        matrix = generator.normal(size=(2_000, 32)).astype(np.float32)
        matrix /= np.linalg.norm(matrix, axis=1, keepdims=True)
        target_id = 1_337
        self._semantic_database(
            {
                item_id: matrix[item_id - 1].tolist()
                for item_id in range(1, len(matrix) + 1)
            }
        )
        build_ann_index(self.semantic_path, self.ann_path)

        result = query_ann_index(
            self.ann_path,
            self.semantic_path,
            matrix[target_id - 1].tolist(),
            top_k=5,
            minimum_candidates=20,
            max_probe_radius=1,
        )

        self.assertEqual(result.hits[0].item_id, target_id)
        self.assertLess(result.trace.candidates_scored, len(matrix))
        self.assertFalse(result.trace.exact_filter_fallback)

    def test_narrow_filter_uses_exact_safe_fallback(self) -> None:
        self._semantic_database(
            {
                11: [1.0, 0.0, 0.0, 0.0],
                22: [0.0, 1.0, 0.0, 0.0],
                33: [0.0, 0.0, 1.0, 0.0],
            }
        )
        build_ann_index(
            self.semantic_path,
            self.ann_path,
            table_count=3,
            bits_per_table=4,
        )

        result = query_ann_index(
            self.ann_path,
            self.semantic_path,
            [0.0, 1.0, 0.0, 0.0],
            allowed_ids=[11, 22],
            top_k=1,
            minimum_candidates=2,
            max_probe_radius=0,
        )

        self.assertEqual(result.hits[0].item_id, 22)
        self.assertTrue(result.trace.exact_filter_fallback)
        self.assertEqual(result.trace.engine, "neural_exact_filtered")
        self.assertFalse(result.trace.degraded)

    def test_stale_ann_is_rejected_after_semantic_identity_changes(self) -> None:
        self._semantic_database(
            {
                1: [1.0, 0.0, 0.0, 0.0],
                2: [0.0, 1.0, 0.0, 0.0],
            }
        )
        build_ann_index(
            self.semantic_path,
            self.ann_path,
            table_count=3,
            bits_per_table=3,
        )
        with sqlite3.connect(self.semantic_path) as connection:
            connection.execute(
                "UPDATE metadata SET value = ? WHERE key = 'source_fingerprint'",
                (json.dumps("corpus-nuevo"),),
            )

        state = ann_index_status(self.ann_path, self.semantic_path)

        self.assertFalse(state["available"])
        self.assertEqual(state["reason"], "stale")
        with self.assertRaisesRegex(AnnIndexError, "no corresponde"):
            query_ann_index(
                self.ann_path,
                self.semantic_path,
                [1.0, 0.0, 0.0, 0.0],
            )

    def test_corrupt_declared_coverage_is_rejected(self) -> None:
        self._semantic_database(
            {
                1: [1.0, 0.0, 0.0, 0.0],
                2: [0.0, 1.0, 0.0, 0.0],
            }
        )
        build_ann_index(
            self.semantic_path,
            self.ann_path,
            table_count=3,
            bits_per_table=3,
        )
        with sqlite3.connect(self.ann_path) as connection:
            connection.execute(
                "UPDATE metadata SET value = ? WHERE key = 'memberships'",
                (json.dumps(999),),
            )

        state = ann_index_status(self.ann_path, self.semantic_path)

        self.assertFalse(state["available"])
        self.assertEqual(state["reason"], "invalid")
        self.assertIn("cobertura", state["message"])

    def test_build_is_deterministic_at_the_logical_level(self) -> None:
        second_ann = self.root / "semantic-ann-second.db"
        self._semantic_database(
            {
                5: [0.2, 0.8, 0.0, 0.0],
                8: [0.8, 0.2, 0.0, 0.0],
                13: [0.0, 0.0, 0.2, 0.8],
            }
        )
        arguments = {
            "table_count": 5,
            "bits_per_table": 4,
            "batch_size": 2,
        }
        build_ann_index(self.semantic_path, self.ann_path, **arguments)
        build_ann_index(self.semantic_path, second_ann, **arguments)

        def rows(path: Path):
            with sqlite3.connect(path) as connection:
                return (
                    connection.execute(
                        "SELECT * FROM ann_tables ORDER BY table_id"
                    ).fetchall(),
                    connection.execute(
                        "SELECT * FROM ann_buckets ORDER BY table_id, signature"
                    ).fetchall(),
                    connection.execute(
                        "SELECT * FROM metadata ORDER BY key"
                    ).fetchall(),
                )

        self.assertEqual(rows(self.ann_path), rows(second_ann))

    def test_cli_build_and_status(self) -> None:
        self._semantic_database(
            {
                1: [1.0, 0.0, 0.0, 0.0],
                2: [0.0, 1.0, 0.0, 0.0],
            }
        )
        script = Path(__file__).resolve().parents[1] / "build_ann.py"
        build = subprocess.run(
            [
                sys.executable,
                str(script),
                "--semantic",
                str(self.semantic_path),
                "--output",
                str(self.ann_path),
                "--tables",
                "3",
                "--bits",
                "3",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(build.returncode, 0, build.stderr)
        self.assertEqual(json.loads(build.stdout)["items_indexed"], 2)

        status = subprocess.run(
            [
                sys.executable,
                str(script),
                "--semantic",
                str(self.semantic_path),
                "--output",
                str(self.ann_path),
                "--status",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertTrue(json.loads(status.stdout)["available"])


if __name__ == "__main__":
    unittest.main()
