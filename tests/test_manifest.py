import tempfile
import unittest
from collections import Counter
from pathlib import Path

from config import ALLOWED_DOCUMENT_HOSTS, INDEX_START_YEAR, ROOT_DIR
from services.manifest import load_manifest


class ManifestTests(unittest.TestCase):
    def test_default_index_starts_in_2013(self) -> None:
        self.assertEqual(INDEX_START_YEAR, 2013)

    def test_loads_and_enriches_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.csv"
            path.write_text(
                "title,url\n"
                "Acta No 02 de 2026 SEMPB Segunda Parte,"
                "https://www.invima.gov.co/biblioteca/download/2\n",
                encoding="utf-8",
            )
            documents = load_manifest(path, ("www.invima.gov.co",))
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0].acta_number, "02")
        self.assertEqual(documents[0].part, "Segunda Parte")

    def test_rejects_unapproved_host(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.csv"
            path.write_text(
                "title,url\nDocumento,https://example.com/documento.pdf\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_manifest(path, ("www.invima.gov.co",))

    def test_project_catalog_preserves_verified_2020_through_acta_08_2026(self) -> None:
        documents = load_manifest(
            ROOT_DIR / "documents_manifest.csv",
            ALLOWED_DOCUMENT_HOSTS,
        )
        self.assertGreaterEqual(len(documents), 179)
        counts = Counter(document.year for document in documents)
        minimum_counts = {
            2020: 25,
            2021: 39,
            2022: 24,
            2023: 18,
            2024: 27,
            2025: 29,
            2026: 17,
        }
        for year, minimum in minimum_counts.items():
            self.assertGreaterEqual(counts[year], minimum)
        expected_acta_ranges = {
            2020: 24,
            2021: 21,
            2022: 15,
            2023: 16,
            2024: 27,
            2025: 9,
            2026: 8,
        }
        for year, maximum in expected_acta_ranges.items():
            self.assertTrue(
                {str(number).zfill(2) for number in range(1, maximum + 1)}
                .issubset(
                    {
                        document.acta_number
                        for document in documents
                        if document.year == year
                    }
                )
            )
        documents_2026 = [
            document for document in documents if document.year == 2026
        ]
        self.assertTrue(
            {"01", "02", "03", "04", "05", "06", "07", "08"}.issubset(
                {document.acta_number for document in documents_2026}
            )
        )
        self.assertTrue(
            {"Primera Parte", "Segunda Parte"}.issubset(
                {
                    document.part
                    for document in documents_2026
                    if document.acta_number == "08"
                }
            )
        )
        self.assertIn(
            "SEMNNIMB",
            {document.section for document in documents if document.year <= 2024},
        )
        self.assertIn(
            "SEMPB",
            {document.section for document in documents if document.year >= 2025},
        )
        self.assertEqual(
            len({document.url for document in documents}),
            len(documents),
        )
        historical_mirrors = [
            document
            for document in documents
            if document.source_type == "historical_mirror"
        ]
        self.assertTrue(
            any(
                document.year == 2022
                and document.acta_number == "01"
                and document.part == "Segunda Parte"
                for document in historical_mirrors
            )
        )


if __name__ == "__main__":
    unittest.main()
