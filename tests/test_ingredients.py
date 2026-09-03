import unittest

from services.ingredients import (
    canonicalize_ingredient,
    normalize_ingredient,
    parse_active_ingredients,
)


class ActiveIngredientTests(unittest.TestCase):
    def test_keeps_literal_and_builds_comparable_forms(self) -> None:
        ingredient = parse_active_ingredients("1.34 mg de Semaglutida")[0]

        self.assertEqual(ingredient.literal, "1.34 mg de Semaglutida")
        self.assertEqual(ingredient.normalized, "1.34 mg de semaglutida")
        self.assertEqual(ingredient.canonical, "Semaglutida")
        one_line = parse_active_ingredients(
            "Cada tableta contiene Semaglutida 14 mg"
        )[0]
        self.assertEqual(one_line.literal, "Semaglutida 14 mg")
        self.assertEqual(one_line.canonical, "Semaglutida")

    def test_strips_historical_container_variants(self) -> None:
        variants = {
            "Cada tableta recubierta contiene Semaglutida 14 mg": "Semaglutida",
            "Cada frasco contiene Liraglutida 6 mg/mL": "Liraglutida",
            "Cada gramo contiene Dapagliflozina 5 mg": "Dapagliflozina",
            "Cada 1 mL de solución contiene Empagliflozina 10 mg": "Empagliflozina",
        }
        for statement, expected in variants.items():
            with self.subTest(statement=statement):
                ingredient = parse_active_ingredients(statement)[0]
                self.assertEqual(ingredient.canonical, expected)
                self.assertNotIn("Cada", ingredient.literal)
                self.assertNotIn("contiene", ingredient.literal)

    def test_never_promotes_a_negated_dosage_statement(self) -> None:
        self.assertEqual(
            parse_active_ingredients("Cada tableta no contiene lactosa"),
            (),
        )

    def test_splits_only_clear_delimiters_and_deduplicates(self) -> None:
        ingredients = parse_active_ingredients(
            "metformina 500 mg + semaglutida 1 mg; METFORMINA 500 mg"
        )

        self.assertEqual(
            tuple(item.canonical.casefold() for item in ingredients),
            ("metformina", "semaglutida"),
        )

    def test_does_not_collapse_salts_or_derivatives(self) -> None:
        ingredients = parse_active_ingredients(
            "semaglutida + semaglutida sódica"
        )

        self.assertEqual(len(ingredients), 2)
        self.assertNotEqual(ingredients[0].normalized, ingredients[1].normalized)

    def test_removes_trailing_excipients_but_not_the_active_literal(self) -> None:
        ingredients = parse_active_ingredients(
            "liraglutida 6 mg/mL; excipientes c.s."
        )

        self.assertEqual(len(ingredients), 1)
        self.assertEqual(ingredients[0].canonical, "liraglutida")

    def test_normalizers_are_conservative(self) -> None:
        self.assertEqual(normalize_ingredient("Ácido acetilsalicílico"), "acido acetilsalicilico")
        self.assertEqual(
            canonicalize_ingredient("Semaglutida sódica 2 mg/mL"),
            "Semaglutida sódica",
        )


if __name__ == "__main__":
    unittest.main()
