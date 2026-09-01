import unittest

from services.models import SearchResult
from services.retrieval import build_context, build_grounded_prompt, validate_citations


class RetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.result = SearchResult(
            chunk_id=1,
            title="Acta No 01 de 2026 SEMPB Primera Parte",
            url="https://www.invima.gov.co/biblioteca/download/1",
            page=4,
            text="Evidencia documental.",
            year=2026,
            acta_number="01",
            section="SEMPB",
            part="Primera Parte",
            score=1.0,
        )

    def test_context_has_stable_source_label(self) -> None:
        context = build_context([self.result])
        self.assertIn("[F1]", context)
        self.assertIn("página 4", context)
        self.assertIn(self.result.url, context)

    def test_prompt_requires_grounded_citations(self) -> None:
        prompt = build_grounded_prompt("¿Qué decidió la comisión?", [self.result])
        self.assertIn("exclusivamente", prompt)
        self.assertIn("[F#]", prompt)
        self.assertIn("Fuentes consultadas", prompt)

    def test_validates_source_labels(self) -> None:
        self.assertEqual(validate_citations("Decisión [F1].", 1), (True, []))
        self.assertEqual(validate_citations("Decisión sin fuente.", 1), (False, []))
        self.assertEqual(validate_citations("Decisión [F2].", 1), (False, [2]))


if __name__ == "__main__":
    unittest.main()
