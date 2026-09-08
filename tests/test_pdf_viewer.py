from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf

from services.downloader import DownloadedPdf
from services.models import SearchResult
from services.pdf_viewer import (
    render_pdf_page,
    search_pdf_page_text,
    select_viewer_text,
    viewer_session_values,
    viewer_source_payload,
)


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
        self.assertIn("Página dos", rendered.text)
        self.assertEqual(rendered.dpi, 125)
        self.assertEqual(rendered.rotation, 0)

    def test_renders_rotation_and_custom_dpi(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf_path = root / "document.pdf"
            document = pymupdf.open()
            document.new_page(width=400, height=200).insert_text((72, 72), "Texto")
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
                    1,
                    root,
                    ("www.invima.gov.co",),
                    10_000_000,
                    dpi=144,
                    rotation=90,
                )

        self.assertGreater(rendered.height, rendered.width)
        self.assertEqual(rendered.dpi, 144)
        self.assertEqual(rendered.rotation, 90)

    def test_searches_page_text_accent_insensitively(self) -> None:
        result = search_pdf_page_text(
            "Primera evaluación\n\nde Semaglutida. Otra EVALUACIÓN.",
            "evaluacion",
            maximum_snippets=1,
        )
        self.assertEqual(result.occurrence_count, 2)
        self.assertEqual(len(result.snippets), 1)
        self.assertIn("evaluación", result.snippets[0])

    def test_normalizes_viewer_source_payload(self) -> None:
        source = SearchResult(
            chunk_id=1,
            title="Acta 01",
            url="https://www.invima.gov.co/biblioteca/download/1",
            page=4,
            text="Texto",
            year=2026,
            acta_number="01",
            section="SEMPB",
            part=None,
            source_type="official",
            score=1.0,
        )
        payload = viewer_source_payload(source, page=7)
        self.assertEqual(payload["page"], 7)
        self.assertEqual(payload["title"], "Acta 01")

        minimal = viewer_source_payload(
            {
                "title": "Acta mínima",
                "url": "https://www.invima.gov.co/biblioteca/download/2",
                "page": "3",
            }
        )
        self.assertEqual(minimal["page"], 3)

        session = viewer_session_values(
            minimal,
            origin_page="pages/7_Comparar.py",
        )
        self.assertEqual(session["viewer_page"], 3)
        self.assertEqual(session["viewer_source"]["title"], "Acta mínima")
        self.assertEqual(session["viewer_origin_page"], "pages/7_Comparar.py")

    def test_uses_indexed_fragment_without_calling_it_full_page_text(self) -> None:
        selected = select_viewer_text("", "Fragmento producido por OCR")
        self.assertEqual(selected.text, "Fragmento producido por OCR")
        self.assertEqual(selected.source, "indexed_fragment")
        self.assertFalse(selected.is_full_page)

        native = select_viewer_text("Texto nativo completo", "Fragmento")
        self.assertEqual(native.source, "native_pdf_page")
        self.assertTrue(native.is_full_page)

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
