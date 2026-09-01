from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from services.downloader import download_pdf_resource


@dataclass(frozen=True)
class RenderedPdfPage:
    image_bytes: bytes
    page_number: int
    page_count: int
    resolved_url: str
    width: int
    height: int


def render_pdf_page(
    title: str,
    url: str,
    page_number: int,
    cache_directory: Path,
    allowed_hosts: tuple[str, ...],
    max_pdf_bytes: int,
    *,
    dpi: int = 125,
) -> RenderedPdfPage:
    if page_number < 1:
        raise ValueError("La página debe ser mayor o igual a 1")
    if not 72 <= dpi <= 180:
        raise ValueError("La resolución debe estar entre 72 y 180 DPI")

    downloaded = download_pdf_resource(
        title,
        url,
        cache_directory,
        allowed_hosts,
        max_pdf_bytes,
    )
    try:
        import pymupdf
    except ImportError as exc:  # pragma: no cover - depende del entorno
        raise RuntimeError("PyMuPDF no está instalado") from exc

    with pymupdf.open(downloaded.path) as document:
        page_count = len(document)
        if page_number > page_count:
            raise ValueError(
                f"El documento tiene {page_count} páginas; no existe la {page_number}"
            )
        page = document.load_page(page_number - 1)
        pixmap = page.get_pixmap(dpi=dpi, alpha=False)
        image_bytes = pixmap.tobytes("png")
        width, height = pixmap.width, pixmap.height

    return RenderedPdfPage(
        image_bytes=image_bytes,
        page_number=page_number,
        page_count=page_count,
        resolved_url=downloaded.resolved_url,
        width=width,
        height=height,
    )
