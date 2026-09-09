from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.semantic import (
    DEFAULT_NEURAL_MODEL_ID,
    DEFAULT_NEURAL_MODEL_REVISION,
    NEURAL_SEMANTIC_METHOD,
    build_or_resume_semantic_index,
    semantic_build_spec,
    semantic_content_hash,
    semantic_index_info,
)


class RecordingDenseEncoder:
    """Codificador determinista que permite observar el trabajo neuronal."""

    def __init__(
        self,
        *,
        model_id: str = DEFAULT_NEURAL_MODEL_ID,
        model_revision: str = DEFAULT_NEURAL_MODEL_REVISION,
    ) -> None:
        self.model_id = model_id
        self.model_revision = model_revision
        self.batches: list[list[str]] = []

    @staticmethod
    def _vector(text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [((value / 255.0) * 2.0) - 1.0 for value in digest[:8]]

    def encode_passages(self, texts, *, batch_size):
        del batch_size
        batch = [str(text) for text in texts]
        self.batches.append(batch)
        return [self._vector(text) for text in batch]

    def encode_query(self, text):
        return self._vector(str(text))

    @property
    def encoded_texts(self) -> list[str]:
        return [text for batch in self.batches for text in batch]


class FailingBatchEncoder(RecordingDenseEncoder):
    def __init__(self, *, fail_on_call: int) -> None:
        super().__init__()
        self.fail_on_call = fail_on_call
        self.calls = 0

    def encode_passages(self, texts, *, batch_size):
        self.calls += 1
        if self.calls == self.fail_on_call:
            raise RuntimeError("interrupcion simulada")
        return super().encode_passages(texts, batch_size=batch_size)


class SemanticResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.published = self.root / "semantic.db"
        self.checkpoint = self.root / "semantic.checkpoint.db"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _database(
        self,
        name: str,
        rows: list[tuple[int, str]],
    ) -> Path:
        path = self.root / name
        with sqlite3.connect(path) as connection:
            connection.execute(
                "CREATE TABLE chunks (id INTEGER PRIMARY KEY, text TEXT NOT NULL)"
            )
            connection.executemany(
                "INSERT INTO chunks (id, text) VALUES (?, ?)",
                rows,
            )
        return path

    def _build(self, database: Path, encoder, **overrides):
        arguments = {
            "lexical_dimension": 64,
            "semantic_dimension": 32,
            "neural_batch_size": 2,
            "neural_encoder": encoder,
        }
        arguments.update(overrides)
        return build_or_resume_semantic_index(
            database,
            self.published,
            self.checkpoint,
            **arguments,
        )

    @staticmethod
    def _metadata(path: Path) -> dict[str, object]:
        with sqlite3.connect(path) as connection:
            return {
                str(key): json.loads(value)
                for key, value in connection.execute(
                    "SELECT key, value FROM metadata"
                )
            }

    @staticmethod
    def _neural_rows(path: Path) -> dict[bytes, bytes]:
        with sqlite3.connect(path) as connection:
            return {
                bytes(content_hash): bytes(vector)
                for content_hash, vector in connection.execute(
                    "SELECT content_hash, neural_vector FROM neural_embeddings"
                )
            }

    def test_partial_build_preserves_published_index_until_resume_completes(self) -> None:
        shared_a = "precedente regulatorio compartido alfa"
        shared_b = "precedente regulatorio compartido beta"
        original = self._database(
            "original.db",
            [(1, shared_a), (2, shared_b)],
        )
        self.assertTrue(self._build(original, RecordingDenseEncoder()).complete)
        published_before = self.published.read_bytes()

        expanded = self._database(
            "expanded.db",
            [
                (101, shared_a),
                (102, shared_b),
                (103, "concepto regulatorio nuevo gamma"),
                (104, "concepto regulatorio nuevo delta"),
                (105, "concepto regulatorio nuevo epsilon"),
            ],
        )
        partial_encoder = RecordingDenseEncoder()
        partial = self._build(
            expanded,
            partial_encoder,
            max_unique_texts=1,
        )

        self.assertFalse(partial.complete)
        self.assertEqual(partial.documents_reused, 2)
        self.assertEqual(partial.documents_after, 3)
        self.assertEqual(partial.documents_remaining, 2)
        self.assertEqual(len(partial_encoder.encoded_texts), 1)
        self.assertTrue(self.checkpoint.exists())
        self.assertEqual(self.published.read_bytes(), published_before)
        self.assertEqual(semantic_index_info(self.published)["documents"], 2)
        self.assertEqual(self._metadata(self.checkpoint)["neural_status"], "building")

        resume_encoder = RecordingDenseEncoder()
        completed = self._build(expanded, resume_encoder)

        self.assertTrue(completed.complete)
        self.assertTrue(completed.checkpoint_reused)
        self.assertEqual(completed.documents_before, 3)
        self.assertEqual(completed.documents_encoded, 2)
        self.assertEqual(completed.documents_remaining, 0)
        self.assertEqual(len(resume_encoder.encoded_texts), 2)
        self.assertFalse(self.checkpoint.exists())
        self.assertNotEqual(self.published.read_bytes(), published_before)
        self.assertEqual(semantic_index_info(self.published)["documents"], 5)

    def test_reuses_hashes_when_item_ids_change_and_encodes_only_new_text(self) -> None:
        alpha = "evaluacion clinica de producto alfa"
        beta = "antecedente farmacologico de producto beta"
        first = self._database("first.db", [(1, alpha), (2, beta)])
        self.assertTrue(self._build(first, RecordingDenseEncoder()).complete)
        old_vectors = self._neural_rows(self.published)

        gamma = "nueva indicacion terapeutica gamma"
        second = self._database(
            "second.db",
            [(501, beta), (777, alpha), (999, gamma)],
        )
        encoder = RecordingDenseEncoder()
        summary = self._build(second, encoder)

        self.assertTrue(summary.complete)
        self.assertEqual(summary.documents_reused, 2)
        self.assertEqual(summary.documents_encoded, 1)
        self.assertEqual(encoder.encoded_texts, [gamma])
        with sqlite3.connect(self.published) as connection:
            item_ids = {
                int(row[0])
                for row in connection.execute("SELECT item_id FROM items")
            }
        self.assertEqual(item_ids, {501, 777, 999})
        new_vectors = self._neural_rows(self.published)
        self.assertEqual(
            new_vectors[semantic_content_hash(alpha)],
            old_vectors[semantic_content_hash(alpha)],
        )
        self.assertEqual(
            new_vectors[semantic_content_hash(beta)],
            old_vectors[semantic_content_hash(beta)],
        )

    def test_duplicate_text_is_encoded_once_but_covers_every_item(self) -> None:
        duplicate = "mismo concepto regulatorio duplicado"
        distinct = "concepto regulatorio completamente distinto"
        database = self._database(
            "duplicates.db",
            [(1, duplicate), (2, duplicate), (3, distinct)],
        )
        encoder = RecordingDenseEncoder()

        summary = self._build(database, encoder)

        self.assertTrue(summary.complete)
        self.assertEqual(summary.documents_total, 3)
        self.assertEqual(summary.documents_encoded, 3)
        self.assertEqual(summary.unique_texts_total, 2)
        self.assertEqual(summary.unique_texts_encoded, 2)
        self.assertCountEqual(encoder.encoded_texts, [duplicate, distinct])
        self.assertEqual(len(self._neural_rows(self.published)), 2)
        info = semantic_index_info(self.published)
        self.assertEqual(info["neural_documents"], 3)
        self.assertEqual(info["neural_unique_texts"], 2)

    def test_changed_and_new_texts_are_encoded_and_deleted_hash_is_pruned(self) -> None:
        stable = "concepto estable para reutilizar"
        old_version = "concepto anterior que sera reemplazado"
        deleted = "concepto retirado del corpus vigente"
        first = self._database(
            "before-change.db",
            [(1, stable), (2, old_version), (3, deleted)],
        )
        self.assertTrue(self._build(first, RecordingDenseEncoder()).complete)
        stable_vector = self._neural_rows(self.published)[
            semantic_content_hash(stable)
        ]

        changed = "concepto actualizado que reemplaza al anterior"
        added = "concepto completamente nuevo en el corpus"
        second = self._database(
            "after-change.db",
            [(10, stable), (20, changed), (40, added)],
        )
        encoder = RecordingDenseEncoder()
        summary = self._build(second, encoder)

        self.assertTrue(summary.complete)
        self.assertEqual(summary.documents_reused, 1)
        self.assertEqual(summary.documents_encoded, 2)
        self.assertCountEqual(encoder.encoded_texts, [changed, added])
        vectors = self._neural_rows(self.published)
        self.assertEqual(
            set(vectors),
            {
                semantic_content_hash(stable),
                semantic_content_hash(changed),
                semantic_content_hash(added),
            },
        )
        self.assertNotIn(semantic_content_hash(old_version), vectors)
        self.assertNotIn(semantic_content_hash(deleted), vectors)
        self.assertEqual(vectors[semantic_content_hash(stable)], stable_vector)

    def test_incompatible_model_does_not_reuse_old_vectors(self) -> None:
        rows = [
            (1, "informacion regulatoria modelo uno"),
            (2, "informacion regulatoria modelo dos"),
        ]
        database = self._database("models.db", rows)
        model_a = "test/model-a"
        model_b = "test/model-b"
        first_encoder = RecordingDenseEncoder(model_id=model_a)
        first = self._build(
            database,
            first_encoder,
            neural_model_id=model_a,
        )
        self.assertTrue(first.complete)

        second_encoder = RecordingDenseEncoder(model_id=model_b)
        second = self._build(
            database,
            second_encoder,
            neural_model_id=model_b,
        )

        self.assertTrue(second.complete)
        self.assertEqual(second.documents_reused, 0)
        self.assertEqual(second.documents_encoded, 2)
        self.assertCountEqual(second_encoder.encoded_texts, [text for _, text in rows])
        info = semantic_index_info(self.published)
        expected_method, expected_signature = semantic_build_spec(
            neural_enabled=True,
            neural_model_id=model_b,
            neural_model_revision=DEFAULT_NEURAL_MODEL_REVISION,
        )
        self.assertEqual(info["method"], expected_method)
        self.assertEqual(info["method"], NEURAL_SEMANTIC_METHOD)
        self.assertEqual(info["build_signature"], expected_signature)
        self.assertEqual(info["neural_model_id"], model_b)

    def test_incompatible_build_signature_forces_reencoding(self) -> None:
        rows = [
            (1, "informacion regulatoria firma uno"),
            (2, "informacion regulatoria firma dos"),
        ]
        database = self._database("signature.db", rows)
        self.assertTrue(self._build(database, RecordingDenseEncoder()).complete)
        with sqlite3.connect(self.published) as connection:
            connection.execute(
                "UPDATE metadata SET value = ? WHERE key = 'build_signature'",
                (json.dumps("firma-obsoleta"),),
            )

        encoder = RecordingDenseEncoder()
        summary = self._build(database, encoder)

        self.assertTrue(summary.complete)
        self.assertEqual(summary.documents_reused, 0)
        self.assertEqual(summary.documents_encoded, 2)
        self.assertCountEqual(encoder.encoded_texts, [text for _, text in rows])
        _, expected_signature = semantic_build_spec(neural_enabled=True)
        self.assertEqual(
            semantic_index_info(self.published)["build_signature"],
            expected_signature,
        )

    def test_failed_batch_preserves_previously_committed_batches_for_resume(self) -> None:
        rows = [
            (1, "contenido regulatorio lote uno"),
            (2, "contenido regulatorio lote dos"),
            (3, "contenido regulatorio lote tres"),
            (4, "contenido regulatorio lote cuatro"),
            (5, "contenido regulatorio lote cinco"),
        ]
        database = self._database("failure.db", rows)
        failing_encoder = FailingBatchEncoder(fail_on_call=2)

        with self.assertRaisesRegex(RuntimeError, "interrupcion simulada"):
            self._build(database, failing_encoder)

        self.assertFalse(self.published.exists())
        self.assertTrue(self.checkpoint.exists())
        self.assertEqual(len(self._neural_rows(self.checkpoint)), 2)
        checkpoint_metadata = self._metadata(self.checkpoint)
        self.assertEqual(checkpoint_metadata["neural_status"], "building")
        self.assertEqual(checkpoint_metadata["neural_documents"], 2)
        self.assertEqual(checkpoint_metadata["neural_unique_texts"], 2)

        resume_encoder = RecordingDenseEncoder()
        summary = self._build(database, resume_encoder)

        self.assertTrue(summary.complete)
        self.assertTrue(summary.checkpoint_reused)
        self.assertEqual(summary.documents_before, 2)
        self.assertEqual(summary.documents_encoded, 3)
        self.assertEqual(
            resume_encoder.encoded_texts,
            [text for _, text in rows[2:]],
        )
        self.assertFalse(self.checkpoint.exists())
        info = semantic_index_info(self.published)
        self.assertEqual(info["neural_status"], "ready")
        self.assertEqual(info["neural_documents"], 5)

    def test_time_budget_stops_before_next_batch_and_keeps_checkpoint(self) -> None:
        rows = [
            (1, "contenido temporal uno"),
            (2, "contenido temporal dos"),
            (3, "contenido temporal tres"),
            (4, "contenido temporal cuatro"),
        ]
        database = self._database("time-budget.db", rows)
        encoder = RecordingDenseEncoder()

        with patch(
            "services.semantic.time.monotonic",
            side_effect=[0.0, 0.1, 2.0],
        ):
            summary = self._build(
                database,
                encoder,
                max_seconds=1.0,
            )

        self.assertFalse(summary.complete)
        self.assertEqual(summary.documents_after, 2)
        self.assertEqual(summary.documents_remaining, 2)
        self.assertEqual(len(encoder.batches), 1)
        self.assertTrue(self.checkpoint.exists())
        self.assertFalse(self.published.exists())

    def test_publish_failure_keeps_old_index_and_complete_checkpoint(self) -> None:
        stable = "contenido publicado que debe permanecer"
        original = self._database("published-source.db", [(1, stable)])
        self.assertTrue(self._build(original, RecordingDenseEncoder()).complete)
        published_before = self.published.read_bytes()

        added = "contenido nuevo pendiente de publicacion"
        expanded = self._database(
            "publish-candidate.db",
            [(10, stable), (20, added)],
        )
        real_replace = os.replace

        def fail_only_final_promotion(source, destination):
            if Path(destination).resolve() == self.published.resolve():
                raise OSError("fallo simulado al publicar")
            return real_replace(source, destination)

        with patch(
            "services.semantic.os.replace",
            side_effect=fail_only_final_promotion,
        ):
            with self.assertRaisesRegex(OSError, "fallo simulado al publicar"):
                self._build(expanded, RecordingDenseEncoder())

        self.assertEqual(self.published.read_bytes(), published_before)
        self.assertEqual(semantic_index_info(self.published)["documents"], 1)
        self.assertTrue(self.checkpoint.exists())
        self.assertEqual(self._metadata(self.checkpoint)["neural_status"], "ready")

        resume_encoder = RecordingDenseEncoder()
        resumed = self._build(expanded, resume_encoder)

        self.assertTrue(resumed.complete)
        self.assertTrue(resumed.checkpoint_reused)
        self.assertEqual(resumed.documents_before, 2)
        self.assertEqual(resumed.documents_encoded, 0)
        self.assertEqual(resume_encoder.encoded_texts, [])
        self.assertFalse(self.checkpoint.exists())
        self.assertEqual(semantic_index_info(self.published)["documents"], 2)

    def test_injected_encoder_must_match_declared_model(self) -> None:
        database = self._database(
            "encoder-mismatch.db",
            [(1, "contenido que no debe etiquetarse con otro modelo")],
        )
        encoder = RecordingDenseEncoder(model_id="test/real-model")

        with self.assertRaisesRegex(ValueError, "codificador.*modelo"):
            self._build(
                database,
                encoder,
                neural_model_id="test/declared-model",
            )

        self.assertEqual(encoder.encoded_texts, [])
        self.assertFalse(self.published.exists())

    def test_source_published_and_checkpoint_paths_must_be_pairwise_distinct(self) -> None:
        for alias in ("published", "checkpoint"):
            with self.subTest(alias=alias):
                source = self._database(
                    f"source-{alias}.db",
                    [(1, "base documental que nunca debe sobrescribirse")],
                )
                source_before = source.read_bytes()
                published = source if alias == "published" else self.root / f"{alias}.semantic.db"
                checkpoint = source if alias == "checkpoint" else self.root / f"{alias}.checkpoint.db"

                with self.assertRaisesRegex(ValueError, "distint"):
                    build_or_resume_semantic_index(
                        source,
                        published,
                        checkpoint,
                        lexical_dimension=64,
                        semantic_dimension=32,
                        neural_batch_size=2,
                        neural_encoder=RecordingDenseEncoder(),
                    )

                self.assertEqual(source.read_bytes(), source_before)
                with sqlite3.connect(source) as connection:
                    self.assertEqual(
                        connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
                        1,
                    )

    def test_logically_corrupt_checkpoint_is_rebuilt_without_a_retry_loop(self) -> None:
        first = "fragmento correcto ya confirmado"
        second = "fragmento pendiente que debe conservar su identidad"
        database = self._database(
            "logical-corruption.db",
            [(1, first), (2, second)],
        )
        partial = self._build(
            database,
            RecordingDenseEncoder(),
            max_unique_texts=1,
        )
        self.assertFalse(partial.complete)

        with sqlite3.connect(self.checkpoint) as connection:
            connection.execute(
                "UPDATE items SET content_hash = ? WHERE item_id = 2",
                (semantic_content_hash(first),),
            )

        encoder = RecordingDenseEncoder()
        completed = self._build(database, encoder)

        self.assertTrue(completed.complete)
        self.assertFalse(completed.checkpoint_reused)
        self.assertEqual(encoder.encoded_texts, [second])
        self.assertFalse(self.checkpoint.exists())
        self.assertEqual(semantic_index_info(self.published)["documents"], 2)


if __name__ == "__main__":
    unittest.main()
