from __future__ import annotations

import unittest

from services.models import SearchResult
from services.ui_helpers import (
    app_style_css,
    badge_html,
    group_search_results,
    highlight_query,
    metadata_line,
    pdf_page_url,
    result_metadata,
)


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
    def test_app_style_uses_semantic_hooks_and_respects_active_theme(self) -> None:
        css = app_style_css()
        self.assertIn('[data-testid="stAppViewContainer"]', css)
        self.assertIn('[data-testid="stMainBlockContainer"]', css)
        self.assertIn("var(--primary-color", css)
        self.assertNotIn("<script", css.lower())

    def test_badge_escapes_content_and_allow_lists_tone(self) -> None:
        rendered = badge_html(
            '<img src=x onerror="alert(1)">',
            tone='danger" onclick="alert(2)',
            title='Concepto "oficial"',
        )
        self.assertIn('data-tone="neutral"', rendered)
        self.assertIn("&lt;img", rendered)
        self.assertIn("&quot;oficial&quot;", rendered)
        self.assertNotIn("onclick", rendered)
        self.assertEqual(badge_html("   "), "")

    def test_metadata_helpers_drop_empty_values_and_normalize_spacing(self) -> None:
        self.assertEqual(
            metadata_line(("  Acta 08  ", None, "", "Página\n17")),
            "Acta 08 · Página 17",
        )
        self.assertEqual(
            result_metadata(
                year=2026,
                acta_number="08",
                section="SEMPB",
                part=2,
                page=17,
            ),
            "Acta 08 · 2026 · SEMPB · Parte 2 · p. 17",
        )

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
