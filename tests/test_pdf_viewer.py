from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf

from services.downloader import DownloadedPdf
from services.pdf_viewer import render_pdf_page


class PdfViewerTests(unittest.TestCase):
    def test_renders_exact_pdf_page_as_png(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf_path = root / "document.pdf"
            document = pymupdf.open()
            document.new_page().insert_text((72, 72), "Página uno")
            document.new_page().insert_text((72, 72), "Página dos")
            document.save(pdf_path)
            document.close()
            url = "https://www.invima.gov.co/biblioteca/download/1"

            with patch(
                "services.pdf_viewer.download_pdf_resource",
                return_value=DownloadedPdf(pdf_path, url),
            ):
                rendered = render_pdf_page(
                    "Acta",
                    url,
                    2,
                    root,
                    ("www.invima.gov.co",),
                    10_000_000,
                )

        self.assertEqual(rendered.page_number, 2)
        self.assertEqual(rendered.page_count, 2)
        self.assertTrue(rendered.image_bytes.startswith(b"\x89PNG"))

    def test_rejects_page_outside_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf_path = root / "document.pdf"
            document = pymupdf.open()
            document.new_page()
            document.save(pdf_path)
            document.close()
            url = "https://www.invima.gov.co/biblioteca/download/1"

            with patch(
                "services.pdf_viewer.download_pdf_resource",
                return_value=DownloadedPdf(pdf_path, url),
            ):
                with self.assertRaisesRegex(ValueError, "no existe"):
                    render_pdf_page(
                        "Acta",
                        url,
                        2,
                        root,
                        ("www.invima.gov.co",),
                        10_000_000,
                    )


if __name__ == "__main__":
    unittest.main()
