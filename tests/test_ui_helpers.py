from __future__ import annotations

import unittest

from services.models import SearchResult
from services.ui_helpers import group_search_results, highlight_query, pdf_page_url


def result(title: str, year: int, chunk_id: int, score: float) -> SearchResult:
    return SearchResult(
        chunk_id=chunk_id,
        title=title,
        url=f"https://www.invima.gov.co/biblioteca/download/{title}",
        page=chunk_id,
        text="Evaluación farmacológica de semaglutida.",
        year=year,
        acta_number="01",
        section="SEMPB",
        part=None,
        source_type="official",
        score=score,
    )


class UiHelperTests(unittest.TestCase):
    def test_highlight_is_accent_insensitive_and_escapes_html(self) -> None:
        rendered = highlight_query(
            "Evaluación <script>alert(1)</script>",
            "evaluacion",
        )
        self.assertIn("<mark>Evaluación</mark>", rendered)
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)

    def test_pdf_page_url_replaces_fragment_and_validates_host(self) -> None:
        allowed = ("www.invima.gov.co",)
        self.assertEqual(
            pdf_page_url(
                "https://www.invima.gov.co/doc.pdf#old",
                7,
                allowed,
            ),
            "https://www.invima.gov.co/doc.pdf#page=7",
        )
        self.assertEqual(
            pdf_page_url("https://example.com/doc.pdf", 1, allowed),
            "",
        )

    def test_groups_fragments_and_orders_documents(self) -> None:
        grouped = group_search_results(
            [
                result("Acta A", 2025, 1, 0.8),
                result("Acta A", 2025, 2, 0.7),
                result("Acta B", 2026, 3, 0.6),
            ],
            order="newest",
        )
        self.assertEqual([item["title"] for item in grouped], ["Acta B", "Acta A"])
        self.assertEqual(len(grouped[1]["results"]), 2)


if __name__ == "__main__":
    unittest.main()
