import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf

from services.pdf_reader import estimate_text_quality, extract_pdf_pages


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
        self.assertEqual(pages[0]["text_source"], "native_pdf")
        self.assertEqual(pages[0]["text_extractor_version"], "pymupdf-text-v1")
        self.assertGreater(pages[0]["text_quality"], 0)
        self.assertLessEqual(pages[0]["text_quality"], 1)
        self.assertEqual(pages[1]["page"], 2)
        self.assertIsNone(pages[1]["text"])
        self.assertIsNone(pages[1]["text_source"])
        self.assertEqual(pages[1]["text_quality"], 0.0)
        self.assertIn("OCR no ejecutado", pages[1]["extraction_error"])
        self.assertEqual(pages[1]["text_extractor_version"], "pymupdf-text-v1")
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
        self.assertEqual(pages[0]["text_source"], "ocr")
        self.assertEqual(pages[0]["text_extractor_version"], "tesseract-ocr-v1")
        self.assertGreater(pages[0]["text_quality"], 0)
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

        self.assertEqual(len(pages), 1)
        self.assertIsNone(pages[0]["text"])
        self.assertEqual(pages[0]["text_quality"], 0.0)
        self.assertIn("fallo simulado", pages[0]["extraction_error"])
        self.assertEqual(
            pages[0]["text_extractor_version"],
            "pymupdf-text-v1+tesseract-ocr-v1",
        )
        self.assertEqual(possible_scans, [1])

    def test_records_unavailable_ocr_as_a_page_level_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scan.pdf"
            document = pymupdf.open()
            document.new_page()
            document.save(path)
            document.close()

            with patch(
                "services.pdf_reader.tesseract_available",
                return_value=False,
            ):
                pages, possible_scans = extract_pdf_pages(
                    path,
                    ocr_enabled=True,
                )

        self.assertEqual(possible_scans, [1])
        self.assertEqual(len(pages), 1)
        self.assertIsNone(pages[0]["text"])
        self.assertIn("Tesseract no está instalado", pages[0]["extraction_error"])

    def test_quality_metric_is_deterministic_bounded_and_penalizes_damage(self) -> None:
        clean = "Producto: OZEMPIC\nPrincipio activo: Semaglutida 1 mg"
        damaged = "����" + clean

        self.assertEqual(estimate_text_quality(clean), estimate_text_quality(clean))
        self.assertGreater(estimate_text_quality(clean), estimate_text_quality(damaged))
        self.assertGreaterEqual(estimate_text_quality("\x00"), 0)
        self.assertLessEqual(estimate_text_quality(clean), 1)
        self.assertEqual(estimate_text_quality(" \n\t"), 0)


if __name__ == "__main__":
    unittest.main()
