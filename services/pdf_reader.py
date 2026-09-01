from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path


LOGGER = logging.getLogger(__name__)


def tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


def _ocr_page(
    page,
    page_number: int,
    working_directory: Path,
    *,
    languages: str,
    dpi: int,
    timeout_seconds: int,
) -> str:
    """Renderiza una página y la procesa con Tesseract sin usar shell."""
    with tempfile.TemporaryDirectory(
        prefix=f"acta-ocr-{page_number}-",
        dir=working_directory,
    ) as temporary_directory:
        image_path = Path(temporary_directory) / "page.png"
        pixmap = page.get_pixmap(dpi=dpi, alpha=False)
        pixmap.save(str(image_path))
        process = subprocess.run(
            [
                "tesseract",
                str(image_path),
                "stdout",
                "-l",
                languages,
                "--dpi",
                str(dpi),
                "--psm",
                "3",
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout_seconds,
        )
        if process.returncode:
            detail = " ".join(process.stderr.split())[:300]
            raise RuntimeError(
                f"Tesseract terminó con código {process.returncode}: {detail}"
            )
        return process.stdout.strip()


def extract_pdf_pages(
    pdf_path: Path,
    *,
    ocr_enabled: bool = False,
    ocr_languages: str = "spa+eng",
    ocr_dpi: int = 180,
    ocr_timeout_seconds: int = 120,
    ocr_min_chars: int = 20,
) -> tuple[list[dict], list[int]]:
    """Devuelve páginas con texto y la lista de páginas que podrían requerir OCR."""
    try:
        import pymupdf
    except ImportError as exc:  # pragma: no cover - depende del entorno
        raise RuntimeError("PyMuPDF no está instalado") from exc

    pages: list[dict] = []
    possible_scans: list[int] = []
    can_use_ocr = ocr_enabled and tesseract_available()

    with pymupdf.open(pdf_path) as document:
        for page_number, page in enumerate(document, start=1):
            text = page.get_text("text").strip()
            ocr_used = False
            if not text and can_use_ocr:
                try:
                    ocr_text = _ocr_page(
                        page,
                        page_number,
                        pdf_path.parent,
                        languages=ocr_languages,
                        dpi=ocr_dpi,
                        timeout_seconds=ocr_timeout_seconds,
                    )
                    if len(ocr_text) >= ocr_min_chars:
                        text = ocr_text
                        ocr_used = True
                except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                    LOGGER.warning(
                        "OCR falló en %s, página %s: %s",
                        pdf_path.name,
                        page_number,
                        exc,
                    )
            if text:
                pages.append(
                    {
                        "page": page_number,
                        "text": text,
                        "ocr_used": ocr_used,
                    }
                )
            else:
                possible_scans.append(page_number)

    return pages, possible_scans
