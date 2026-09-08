from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.downloader import download_pdf, download_pdf_resource, safe_filename


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
    def test_cache_name_is_stable_by_url_and_avoids_equal_basenames(self) -> None:
        first_url = "https://www.invima.gov.co/a/acta.pdf"
        second_url = "https://www.invima.gov.co/b/acta.pdf"

        self.assertEqual(
            safe_filename("Título original", first_url),
            safe_filename("Título corregido", first_url),
        )
        self.assertNotEqual(
            safe_filename("Acta", first_url),
            safe_filename("Acta", second_url),
        )

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

    def test_equal_pdf_basenames_use_independent_cache_entries(self) -> None:
        first_url = "https://www.invima.gov.co/a/acta.pdf"
        second_url = "https://www.invima.gov.co/b/acta.pdf"

        def fake_urlopen(request, timeout):
            del timeout
            marker = b"primero" if request.full_url == first_url else b"segundo"
            return _Response(b"%PDF-1.7\n" + marker, "application/pdf")

        with tempfile.TemporaryDirectory() as directory:
            with patch("services.downloader.urlopen", side_effect=fake_urlopen):
                first = download_pdf_resource(
                    "Acta",
                    first_url,
                    Path(directory),
                    ("www.invima.gov.co",),
                    4096,
                )
                second = download_pdf_resource(
                    "Acta",
                    second_url,
                    Path(directory),
                    ("www.invima.gov.co",),
                    4096,
                )

            self.assertNotEqual(first.path, second.path)
            self.assertIn(b"primero", first.path.read_bytes())
            self.assertIn(b"segundo", second.path.read_bytes())

    def test_valid_sidecar_allows_cache_hit_without_network(self) -> None:
        url = "https://www.invima.gov.co/biblioteca/download/cache"
        with tempfile.TemporaryDirectory() as directory:
            target_dir = Path(directory)
            with patch(
                "services.downloader.urlopen",
                return_value=_Response(b"%PDF-1.7\ncontenido", "application/pdf"),
            ):
                first = download_pdf_resource(
                    "Acta",
                    url,
                    target_dir,
                    ("www.invima.gov.co",),
                    4096,
                )

            metadata_path = first.path.with_name(f"{first.path.name}.meta.json")
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["source_url"], url)
            self.assertEqual(metadata["resolved_url"], url)
            self.assertEqual(metadata["size"], first.path.stat().st_size)

            with patch(
                "services.downloader.urlopen",
                side_effect=AssertionError("no debe acceder a la red"),
            ):
                second = download_pdf_resource(
                    "Otro título",
                    url,
                    target_dir,
                    ("www.invima.gov.co",),
                    4096,
                )
            self.assertEqual(second.path, first.path)

    def test_mismatched_metadata_forces_a_fresh_download(self) -> None:
        url = "https://www.invima.gov.co/biblioteca/download/cache"
        with tempfile.TemporaryDirectory() as directory:
            target_dir = Path(directory)
            with patch(
                "services.downloader.urlopen",
                return_value=_Response(b"%PDF-1.7\nversion-1", "application/pdf"),
            ):
                cached = download_pdf_resource(
                    "Acta",
                    url,
                    target_dir,
                    ("www.invima.gov.co",),
                    4096,
                )

            metadata_path = cached.path.with_name(f"{cached.path.name}.meta.json")
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["source_identity"] = (
                "https://www.invima.gov.co/biblioteca/download/otro"
            )
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

            with patch(
                "services.downloader.urlopen",
                return_value=_Response(b"%PDF-1.7\nversion-2", "application/pdf"),
            ) as urlopen_mock:
                refreshed = download_pdf_resource(
                    "Acta",
                    url,
                    target_dir,
                    ("www.invima.gov.co",),
                    4096,
                )

            urlopen_mock.assert_called_once()
            self.assertIn(b"version-2", refreshed.path.read_bytes())

    def test_tampered_cached_pdf_forces_a_fresh_download(self) -> None:
        url = "https://www.invima.gov.co/biblioteca/download/cache"
        with tempfile.TemporaryDirectory() as directory:
            target_dir = Path(directory)
            with patch(
                "services.downloader.urlopen",
                return_value=_Response(b"%PDF-1.7\nversion-1", "application/pdf"),
            ):
                cached = download_pdf_resource(
                    "Acta",
                    url,
                    target_dir,
                    ("www.invima.gov.co",),
                    4096,
                )

            # Conserva cabecera y tamaño: la huella del sidecar debe detectar el
            # cambio aunque las validaciones superficiales no lo hagan.
            cached.path.write_bytes(b"%PDF-1.7\nversion-X")
            with patch(
                "services.downloader.urlopen",
                return_value=_Response(b"%PDF-1.7\nversion-2", "application/pdf"),
            ) as urlopen_mock:
                refreshed = download_pdf_resource(
                    "Acta",
                    url,
                    target_dir,
                    ("www.invima.gov.co",),
                    4096,
                )

            urlopen_mock.assert_called_once()
            self.assertIn(b"version-2", refreshed.path.read_bytes())

    def test_migrates_matching_legacy_cache_without_downloading(self) -> None:
        url = "https://www.invima.gov.co/documentos/acta.pdf"
        with tempfile.TemporaryDirectory() as directory:
            target_dir = Path(directory)
            legacy_path = target_dir / "acta.pdf"
            legacy_path.write_bytes(b"%PDF-1.7\nlegado")
            legacy_path.with_suffix(".pdf.url").write_text(url, encoding="utf-8")

            with patch(
                "services.downloader.urlopen",
                side_effect=AssertionError("no debe descargar el PDF legado"),
            ):
                migrated = download_pdf_resource(
                    "Acta",
                    url,
                    target_dir,
                    ("www.invima.gov.co",),
                    4096,
                )

            self.assertEqual(migrated.path.name, safe_filename("Acta", url))
            self.assertFalse(legacy_path.exists())
            self.assertTrue(
                migrated.path.with_name(
                    f"{migrated.path.name}.meta.json"
                ).exists()
            )

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

    def test_rejects_pdf_larger_than_configured_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "services.downloader.urlopen",
                return_value=_Response(b"%PDF-1.7\ncontenido-extenso", "application/pdf"),
            ):
                with self.assertRaisesRegex(ValueError, "tamaño máximo"):
                    download_pdf_resource(
                        "Acta",
                        "https://www.invima.gov.co/biblioteca/download/grande",
                        Path(directory),
                        ("www.invima.gov.co",),
                        8,
                    )


if __name__ == "__main__":
    unittest.main()
