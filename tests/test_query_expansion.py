from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from services.database import initialize_database, insert_document
from services.models import DocumentMetadata
from services.query_expansion import (
    MAX_EXPANSIONS,
    expand_regulatory_query,
    load_regulatory_dictionary,
)
from services.search import search_corpus, search_corpus_page


class QueryExpansionTests(unittest.TestCase):
    def test_dictionary_is_versioned_and_auditable(self) -> None:
        dictionary = load_regulatory_dictionary()

        self.assertRegex(dictionary.version, r"^\d{4}\.\d{2}\.\d+$")
        self.assertEqual(dictionary.language, "es-CO")
        self.assertGreaterEqual(len(dictionary.entries), 10)
        self.assertEqual(
            len({entry.entry_id for entry in dictionary.entries}),
            len(dictionary.entries),
        )

    def test_acronym_expands_to_regulatory_name(self) -> None:
        expansion = expand_regulatory_query("certificación BPM")

        self.assertTrue(expansion.applied)
        self.assertIn("bpm", expansion.original_terms)
        self.assertIn("buenas practicas de manufactura", expansion.expanded_terms)
        self.assertEqual(expansion.matched_entries, ("bpm",))

    def test_dotted_acronym_and_accents_are_normalized(self) -> None:
        expansion = expand_regulatory_query("Certificación B.P.M.")

        self.assertIn("bpm", expansion.original_terms)
        self.assertIn("bpm", expansion.expanded_terms)
        self.assertIn("buenas practicas de manufactura", expansion.expanded_terms)

    def test_long_form_expands_to_acronym(self) -> None:
        expansion = expand_regulatory_query(
            "cumplimiento de buenas prácticas clínicas"
        )

        self.assertIn("bpc", expansion.expanded_terms)

    def test_expansion_is_deterministic_and_limited(self) -> None:
        query = "SEMPB INVIMA BPM BPC IFA DCI NEQ MVND"
        first = expand_regulatory_query(query)
        second = expand_regulatory_query(query)

        self.assertEqual(first, second)
        self.assertLessEqual(len(first.expanded_terms), MAX_EXPANSIONS)
        self.assertEqual(len(first.matched_entries), 4)

    def test_exact_phrase_is_never_expanded(self) -> None:
        expansion = expand_regulatory_query(
            "buenas prácticas de manufactura", exact_phrase=True
        )

        self.assertFalse(expansion.applied)
        self.assertEqual(expansion.expanded_terms, ())
        self.assertEqual(expansion.skipped_reason, "exact_phrase")

    def test_identifier_only_query_is_untouched(self) -> None:
        expansion = expand_regulatory_query("20231234567")

        self.assertEqual(expansion.original_query, "20231234567")
        self.assertEqual(expansion.original_terms, ("20231234567",))
        self.assertEqual(expansion.expanded_terms, ())
        self.assertEqual(expansion.skipped_reason, "identifier_present")

    def test_mixed_identifier_query_is_not_partially_expanded(self) -> None:
        expansion = expand_regulatory_query("BPM radicado 20231234567")

        self.assertFalse(expansion.applied)
        self.assertEqual(expansion.skipped_reason, "identifier_present")

    def test_unknown_product_is_not_guessed(self) -> None:
        expansion = expand_regulatory_query("semaglutida")

        self.assertFalse(expansion.applied)
        self.assertEqual(expansion.skipped_reason, "no_dictionary_match")

    def test_invalid_ambiguous_dictionary_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ambiguous.json"
            path.write_text(
                json.dumps(
                    {
                        "version": "1.0.0",
                        "language": "es-CO",
                        "entries": [
                            {"id": "one", "preferred": "BPM", "variants": ["uno"]},
                            {"id": "two", "preferred": "b.p.m.", "variants": ["dos"]},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "ambigua"):
                load_regulatory_dictionary(path)
class SearchExpansionIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.database_path = root / "actas.db"
        self.semantic_path = root / "missing-semantic.db"
        initialize_database(self.database_path)
        documents = [
            (
                "Acta con sigla",
                "La certificación BPM fue aportada por el interesado.",
            ),
            (
                "Acta con forma desarrollada",
                "Se verificaron las buenas prácticas de manufactura.",
            ),
            (
                "Acta con identificador",
                "BPM correspondiente al radicado 20231234567.",
            ),
            (
                "Acta con palabra genérica",
                "Se revisaron otras prácticas comerciales del interesado.",
            ),
        ]
        for number, (title, text) in enumerate(documents, start=1):
            insert_document(
                self.database_path,
                DocumentMetadata(
                    title=title,
                    url=f"https://example.test/{number}",
                    year=2026,
                    acta_number=f"{number:02d}",
                ),
                f"hash-{number}",
                [{"page": 1, "text": text, "chunks": [text]}],
            )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_textual_search_recovers_acronym_from_long_form_with_trace(self) -> None:
        response = search_corpus(
            self.database_path,
            self.semantic_path,
            "buenas prácticas de manufactura",
            mode="textual",
            top_k=10,
        )

        self.assertIn("Acta con sigla", {item.title for item in response.results})
        self.assertIn("bpm", response.query_terms_expanded)
        self.assertEqual(response.query_terms_original[:2], ("buenas", "practicas"))
        self.assertRegex(response.query_expansion_version or "", r"^\d{4}\.")

    def test_sql_pagination_counts_synonym_matches_globally(self) -> None:
        response = search_corpus_page(
            self.database_path,
            self.semantic_path,
            "BPM",
            mode="textual",
            page=1,
            page_size=10,
        )

        self.assertTrue(response.totals_exact)
        self.assertEqual(response.total_documents, 3)
        self.assertNotIn(
            "Acta con palabra genérica",
            {item.title for item in response.results},
        )
        self.assertIn(
            "buenas practicas de manufactura",
            response.query_terms_expanded,
        )

    def test_exact_phrase_does_not_use_synonyms(self) -> None:
        response = search_corpus(
            self.database_path,
            self.semantic_path,
            "buenas prácticas de manufactura",
            mode="textual",
            exact_phrase=True,
            top_k=10,
        )

        self.assertEqual(
            {item.title for item in response.results},
            {"Acta con forma desarrollada"},
        )
        self.assertEqual(response.query_terms_expanded, ())
        self.assertEqual(response.query_expansion_skipped_reason, "exact_phrase")

    def test_identifier_prevents_partial_expansion_in_real_search(self) -> None:
        response = search_corpus(
            self.database_path,
            self.semantic_path,
            "BPM 20231234567",
            mode="textual",
            top_k=10,
        )

        self.assertNotIn(
            "Acta con forma desarrollada",
            {item.title for item in response.results},
        )
        self.assertEqual(response.query_terms_expanded, ())
        self.assertEqual(
            response.query_expansion_skipped_reason,
            "identifier_present",
        )


if __name__ == "__main__":
    unittest.main()
