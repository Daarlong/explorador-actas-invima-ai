from __future__ import annotations

from pathlib import Path


def extract_pdf_pages(pdf_path: Path) -> tuple[list[dict], list[int]]:
    """Devuelve páginas con texto y la lista de páginas que podrían requerir OCR."""
    try:
        import pymupdf
    except ImportError as exc:  # pragma: no cover - depende del entorno
        raise RuntimeError("PyMuPDF no está instalado") from exc

    pages: list[dict] = []
    possible_scans: list[int] = []

    with pymupdf.open(pdf_path) as document:
        for page_number, page in enumerate(document, start=1):
            text = page.get_text("text").strip()
            if text:
                pages.append({"page": page_number, "text": text})
            else:
                possible_scans.append(page_number)

    return pages, possible_scans
