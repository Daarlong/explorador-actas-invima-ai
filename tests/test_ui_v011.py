from __future__ import annotations

import ast
from pathlib import Path
import unittest

from services.search import SearchResponse


ROOT = Path(__file__).resolve().parents[1]


class V011UiContractTests(unittest.TestCase):
    def test_analytics_page_is_valid_and_uses_traceable_backend(self) -> None:
        source = (ROOT / "pages" / "6_Analitica.py").read_text(encoding="utf-8")
        ast.parse(source)
        self.assertIn("build_corpus_analytics", source)
        self.assertIn("analytics_drilldown", source)
        self.assertIn("Fuente oficial", source)
        self.assertIn("crean ni sustituyen decisiones oficiales", source)
        self.assertIn("distinct_ingredient_and_party_values", source)
        self.assertIn("Principios activos + interesados distintos", source)
        self.assertNotIn("distinct_extracted_values", source)
        self.assertIn("analytics_explorer_state", source)
        self.assertIn('query_value=selected_item.get("value")', source)
        self.assertIn('pop("explorer_search_signature", None)', source)
        self.assertIn('pop("explorer_export_artifact", None)', source)

    def test_home_registers_analytics_page(self) -> None:
        source = (ROOT / "home.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(source.count('"pages/6_Analitica.py"'), 2)

    def test_explorer_exposes_facet_scope_and_query_expansion(self) -> None:
        source = (ROOT / "pages" / "1_Explorador.py").read_text(
            encoding="utf-8"
        )
        ast.parse(source)
        self.assertIn("cached_search_facets", source)
        self.assertIn("Conteos exactos de actas", source)
        self.assertIn("Conteos aproximados", source)
        self.assertIn("facet_uses_post_filters", source)
        self.assertIn("filtros se aplican después", source)
        self.assertIn("También se buscaron equivalencias regulatorias", source)
        self.assertIn("query_terms_expanded", source)
        self.assertIn("PDF/partes por página", source)
        self.assertIn("PDF/parte(s)", source)
        self.assertNotIn("Actas por página", source)

    def test_search_response_can_carry_bounded_facet_universe(self) -> None:
        response = SearchResponse(
            results=[],
            requested_mode="hybrid",
            used_mode="hybrid",
            semantic_available=True,
            semantic_message="",
            facet_candidate_ids=(10, 11),
        )
        self.assertEqual(response.facet_candidate_ids, (10, 11))


if __name__ == "__main__":
    unittest.main()
