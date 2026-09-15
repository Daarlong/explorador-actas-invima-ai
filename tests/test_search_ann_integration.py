from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.ann import build_ann_index
from services.database import initialize_database, insert_document
from services.models import DocumentMetadata
from services.search import search_corpus
from services.semantic import (
    DEFAULT_NEURAL_MODEL_ID,
    DEFAULT_NEURAL_MODEL_REVISION,
    build_semantic_index,
)


class _Encoder:
    model_id = DEFAULT_NEURAL_MODEL_ID
    model_revision = DEFAULT_NEURAL_MODEL_REVISION

    def encode_passages(self, texts, *, batch_size):
        del batch_size
        return [
            [0.0, 1.0, 0.0, 0.0]
            if "mejoría" in text
            else [1.0, 0.0, 0.0, 0.0]
            for text in texts
        ]

    def encode_query(self, text):
        del text
        return [0.0, 1.0, 0.0, 0.0]


class SearchAnnIntegrationTests(unittest.TestCase):
    def test_search_reports_ann_only_when_it_really_executes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "actas.db"
            semantic = root / "semantic.db"
            ann = root / "semantic-ann.db"
            initialize_database(database)
            for number, (title, text) in enumerate(
                (
                    ("Acta textual", "beneficios dentro de indicaciones"),
                    ("Acta semántica", "mejoría terapéutica sostenida"),
                ),
                start=1,
            ):
                insert_document(
                    database,
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
            encoder = _Encoder()
            build_semantic_index(
                database,
                semantic,
                lexical_dimension=32,
                semantic_dimension=16,
                min_df=1,
                neural_enabled=True,
                neural_model_id=encoder.model_id,
                neural_model_revision=encoder.model_revision,
                neural_encoder=encoder,
            )
            build_ann_index(semantic, ann, table_count=4, bits_per_table=4)

            with patch("services.semantic._fastembed_encoder", return_value=encoder):
                response = search_corpus(
                    database,
                    semantic,
                    "beneficios dentro de indicaciones",
                    mode="hybrid",
                    top_k=10,
                    ann_index_path=ann,
                )

            self.assertTrue(response.ann_used)
            self.assertTrue(response.neural_used)
            self.assertEqual(response.semantic_backend, "neural_ann")
            self.assertEqual(response.retrieval_backend, "hybrid_ann")
            self.assertIn("Acta semántica", {item.title for item in response.results})


if __name__ == "__main__":
    unittest.main()
