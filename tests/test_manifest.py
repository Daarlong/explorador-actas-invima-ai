import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()

