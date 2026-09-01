import unittest

from services.text_utils import chunk_text, normalize_text, tokenize_query


class TextUtilsTests(unittest.TestCase):
    def test_normalizes_accents_and_spaces(self) -> None:
        self.assertEqual(normalize_text("  Evaluación   farmacológica "), "evaluacion farmacologica")

    def test_tokenizes_without_common_stopwords(self) -> None:
        self.assertEqual(
            tokenize_query("¿Qué dice el acta sobre semaglutida 12345?"),
            ["dice", "acta", "semaglutida", "12345"],
        )

    def test_chunks_keep_overlap_and_do_not_exceed_reasonably(self) -> None:
        text = " ".join(f"palabra{i}" for i in range(250))
        chunks = chunk_text(text, chunk_size=180, overlap=30)
        self.assertGreater(len(chunks), 2)
        self.assertTrue(all(chunk.strip() for chunk in chunks))
        self.assertTrue(all(len(chunk) <= 180 for chunk in chunks))

    def test_rejects_invalid_overlap(self) -> None:
        with self.assertRaises(ValueError):
            chunk_text("texto", chunk_size=100, overlap=100)


if __name__ == "__main__":
    unittest.main()

