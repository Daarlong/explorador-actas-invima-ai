import tempfile
import unittest
from collections import Counter
from pathlib import Path

from config import ALLOWED_DOCUMENT_HOSTS, ROOT_DIR
from services.manifest import load_manifest


class ManifestTests(unittest.TestCase):
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

    def test_project_catalog_covers_2020_through_acta_08_2026(self) -> None:
        documents = load_manifest(
            ROOT_DIR / "documents_manifest.csv",
            ALLOWED_DOCUMENT_HOSTS,
        )
        self.assertEqual(len(documents), 179)
        self.assertEqual(
            Counter(document.year for document in documents),
            {
                2020: 25,
                2021: 39,
                2022: 24,
                2023: 18,
                2024: 27,
                2025: 29,
                2026: 17,
            },
        )
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
            self.assertEqual(
                {
                    document.acta_number
                    for document in documents
                    if document.year == year
                },
                {str(number).zfill(2) for number in range(1, maximum + 1)},
            )
        documents_2026 = [
            document for document in documents if document.year == 2026
        ]
        self.assertEqual(
            sorted({document.acta_number for document in documents_2026}),
            ["01", "02", "03", "04", "05", "06", "07", "08"],
        )
        self.assertEqual(
            {
                document.part
                for document in documents_2026
                if document.acta_number == "08"
            },
            {"Primera Parte", "Segunda Parte"},
        )
        self.assertEqual(
            {document.section for document in documents if document.year <= 2024},
            {"SEMNNIMB"},
        )
        self.assertEqual(
            {document.section for document in documents if document.year >= 2025},
            {"SEMPB"},
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
        self.assertEqual(len(historical_mirrors), 1)
        self.assertEqual(historical_mirrors[0].year, 2022)
        self.assertEqual(historical_mirrors[0].acta_number, "01")
        self.assertEqual(historical_mirrors[0].part, "Segunda Parte")


if __name__ == "__main__":
    unittest.main()
