import tempfile
import unittest
from pathlib import Path

import pymupdf

from services.pdf_reader import extract_pdf_pages


class PdfReaderTests(unittest.TestCase):
    def test_extracts_text_and_reports_empty_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.pdf"
            document = pymupdf.open()
            text_page = document.new_page()
            text_page.insert_text((72, 72), "Texto verificable de un acta")
            document.new_page()
            document.save(path)
            document.close()

            pages, possible_scans = extract_pdf_pages(path)

        self.assertEqual(pages[0]["page"], 1)
        self.assertIn("Texto verificable", pages[0]["text"])
        self.assertEqual(possible_scans, [2])


if __name__ == "__main__":
    unittest.main()
