from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.downloader import download_pdf, download_pdf_resource


class _Response(io.BytesIO):
    def __init__(self, payload: bytes, content_type: str) -> None:
        super().__init__(payload)
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(payload)),
        }

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        self.close()


class DownloaderTests(unittest.TestCase):
    def test_downloads_direct_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "services.downloader.urlopen",
                return_value=_Response(b"%PDF-1.7\ncontenido", "application/pdf"),
            ):
                path = download_pdf(
                    "Documento",
                    "https://www.invima.gov.co/biblioteca/download/1",
                    Path(directory),
                    ("www.invima.gov.co",),
                    1024,
                )
            self.assertTrue(path.read_bytes().startswith(b"%PDF"))

    def test_resolves_library_detail_page(self) -> None:
        detail = (
            b'<html><body><a href="/biblioteca/download/123">'
            b"Descargar PDF Completo</a></body></html>"
        )

        def fake_urlopen(request, timeout):
            if request.full_url.endswith("/acta"):
                return _Response(detail, "text/html; charset=utf-8")
            if request.full_url.endswith("/download/123"):
                return _Response(b"%PDF-1.7\ncontenido", "application/pdf")
            raise AssertionError(request.full_url)

        with tempfile.TemporaryDirectory() as directory:
            with patch("services.downloader.urlopen", side_effect=fake_urlopen):
                downloaded = download_pdf_resource(
                    "Acta",
                    "https://www.invima.gov.co/biblioteca/acta",
                    Path(directory),
                    ("www.invima.gov.co",),
                    4096,
                )
            self.assertTrue(downloaded.path.read_bytes().startswith(b"%PDF"))
            self.assertEqual(
                downloaded.resolved_url,
                "https://www.invima.gov.co/biblioteca/download/123",
            )

    def test_rejects_link_to_unapproved_domain(self) -> None:
        detail = (
            b'<html><body><a href="https://example.com/document.pdf">'
            b"Descargar</a></body></html>"
        )
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "services.downloader.urlopen",
                return_value=_Response(detail, "text/html"),
            ):
                with self.assertRaisesRegex(ValueError, "enlace PDF permitido"):
                    download_pdf(
                        "Acta",
                        "https://www.invima.gov.co/biblioteca/acta",
                        Path(directory),
                        ("www.invima.gov.co",),
                        4096,
                    )


if __name__ == "__main__":
    unittest.main()
