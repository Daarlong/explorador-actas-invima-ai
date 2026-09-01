import unittest

from services.metadata import parse_document_metadata


class MetadataTests(unittest.TestCase):
    def test_parses_standard_sempb_title(self) -> None:
        result = parse_document_metadata(
            "Acta No 03 de 2026 SEMPB Tercera Parte",
            "https://www.invima.gov.co/biblioteca/download/123",
        )
        self.assertEqual(result.year, 2026)
        self.assertEqual(result.acta_number, "03")
        self.assertEqual(result.section, "SEMPB")
        self.assertEqual(result.part, "Tercera Parte")

    def test_keeps_unknown_title_usable(self) -> None:
        result = parse_document_metadata(
            "Documento extraordinario",
            "https://www.invima.gov.co/biblioteca/download/456",
        )
        self.assertEqual(result.title, "Documento extraordinario")
        self.assertIsNone(result.year)


if __name__ == "__main__":
    unittest.main()

