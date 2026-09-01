import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

            pages, possible_scans = extract_pdf_pages(path, ocr_enabled=False)

        self.assertEqual(pages[0]["page"], 1)
        self.assertIn("Texto verificable", pages[0]["text"])
        self.assertFalse(pages[0]["ocr_used"])
        self.assertEqual(possible_scans, [2])

    def test_uses_ocr_for_page_without_native_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scan.pdf"
            document = pymupdf.open()
            document.new_page()
            document.save(path)
            document.close()

            with patch(
                "services.pdf_reader.tesseract_available",
                return_value=True,
            ), patch(
                "services.pdf_reader._ocr_page",
                return_value="Concepto recuperado mediante reconocimiento óptico.",
            ) as ocr:
                pages, possible_scans = extract_pdf_pages(
                    path,
                    ocr_enabled=True,
                )

        self.assertEqual(possible_scans, [])
        self.assertEqual(pages[0]["page"], 1)
        self.assertTrue(pages[0]["ocr_used"])
        self.assertEqual(ocr.call_count, 1)

    def test_keeps_failed_ocr_page_as_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scan.pdf"
            document = pymupdf.open()
            document.new_page()
            document.save(path)
            document.close()

            with patch(
                "services.pdf_reader.tesseract_available",
                return_value=True,
            ), patch(
                "services.pdf_reader._ocr_page",
                side_effect=RuntimeError("fallo simulado"),
            ):
                pages, possible_scans = extract_pdf_pages(
                    path,
                    ocr_enabled=True,
                )

        self.assertEqual(pages, [])
        self.assertEqual(possible_scans, [1])


if __name__ == "__main__":
    unittest.main()
