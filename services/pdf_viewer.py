from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from services.downloader import download_pdf_resource
from services.models import SearchResult


@dataclass(frozen=True)
class RenderedPdfPage:
    image_bytes: bytes
    page_number: int
    page_count: int
    resolved_url: str
    width: int
    height: int
    text: str
    dpi: int
    rotation: int


@dataclass(frozen=True)
class PageTextSearch:
    """Resultado compacto de buscar una frase dentro de una página PDF."""

    occurrence_count: int
    snippets: tuple[str, ...]


@dataclass(frozen=True)
class ViewerText:
    """Texto disponible en el visor y alcance que puede afirmarse de él."""

    text: str
    source: str
    is_full_page: bool


def select_viewer_text(native_page_text: str, indexed_fragment: str) -> ViewerText:
    """Prefiere texto nativo completo y usa el fragmento indexado como respaldo."""

    native = str(native_page_text or "").strip()
    if native:
        return ViewerText(native, "native_pdf_page", True)
    fragment = str(indexed_fragment or "").strip()
    if fragment:
        return ViewerText(fragment, "indexed_fragment", False)
    return ViewerText("", "unavailable", False)


def _normalized_with_positions(value: str) -> tuple[str, list[int]]:
    normalized: list[str] = []
    positions: list[int] = []
    for position, character in enumerate(value):
        decomposed = unicodedata.normalize("NFKD", character.casefold())
        for item in decomposed:
            if unicodedata.combining(item):
                continue
            normalized.append(item)
            positions.append(position)
    return "".join(normalized), positions


def search_pdf_page_text(
    text: str,
    query: str,
    *,
    maximum_snippets: int = 8,
    context_characters: int = 90,
) -> PageTextSearch:
    """Busca una frase literal sin distinguir mayúsculas ni tildes.

    Los fragmentos conservan el texto original para que el usuario pueda
    verificarlo y copiarlo. ``occurrence_count`` siempre informa el total,
    aunque se limite la cantidad de fragmentos mostrados.
    """

    clean_query = re.sub(r"\s+", " ", query).strip()
    if not text or not clean_query:
        return PageTextSearch(0, ())
    if maximum_snippets < 1:
        raise ValueError("La cantidad máxima de fragmentos debe ser positiva")
    if context_characters < 0:
        raise ValueError("El contexto no puede ser negativo")

    normalized_text, positions = _normalized_with_positions(text)
    normalized_query, _ = _normalized_with_positions(clean_query)
    # El PDF puede separar una frase con saltos o varios espacios. La búsqueda
    # literal normaliza esos espacios sin alterar los caracteres que se copian.
    normalized_query = re.sub(r"\s+", " ", normalized_query)
    if not normalized_query:
        return PageTextSearch(0, ())

    # Conserva la correspondencia de posiciones al colapsar espacios.
    collapsed: list[str] = []
    collapsed_positions: list[int] = []
    previous_space = False
    for character, position in zip(normalized_text, positions):
        is_space = character.isspace()
        if is_space and previous_space:
            continue
        collapsed.append(" " if is_space else character)
        collapsed_positions.append(position)
        previous_space = is_space
    searchable = "".join(collapsed)

    starts = [
        match.start()
        for match in re.finditer(re.escape(normalized_query), searchable)
    ]
    snippets: list[str] = []
    for start in starts[:maximum_snippets]:
        end = start + len(normalized_query)
        original_start = collapsed_positions[start]
        original_end = collapsed_positions[end - 1] + 1
        snippet_start = max(0, original_start - context_characters)
        snippet_end = min(len(text), original_end + context_characters)
        snippet = re.sub(r"\s+", " ", text[snippet_start:snippet_end]).strip()
        if snippet_start:
            snippet = f"…{snippet}"
        if snippet_end < len(text):
            snippet = f"{snippet}…"
        snippets.append(snippet)
    return PageTextSearch(len(starts), tuple(snippets))


def viewer_source_payload(
    source: SearchResult | Mapping[str, Any],
    *,
    page: int | None = None,
) -> dict[str, Any]:
    """Normaliza una fuente para abrir el visor desde cualquier página UI."""

    payload = source.as_dict() if isinstance(source, SearchResult) else dict(source)
    try:
        payload["title"] = str(payload["title"])
        payload["url"] = str(payload["url"])
        payload["page"] = int(page if page is not None else payload["page"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("La fuente no contiene título, URL y página válidos") from exc
    if not payload["title"].strip() or not payload["url"].strip():
        raise ValueError("La fuente no contiene título y URL válidos")
    if payload["page"] < 1:
        raise ValueError("La página debe ser mayor o igual a 1")
    return payload


def viewer_session_values(
    source: SearchResult | Mapping[str, Any],
    *,
    page: int | None = None,
    origin_page: str | None = None,
) -> dict[str, Any]:
    """Construye las claves de sesión compartidas por las páginas Streamlit.

    Una página llamadora solo necesita ejecutar
    ``st.session_state.update(viewer_session_values(source, origin_page=...))``
    y cambiar a ``pages/1_Explorador.py``. El visor acepta fuentes mínimas con
    ``title``, ``url`` y ``page``; si hay ``text``, lo utiliza como respaldo
    explícitamente rotulado cuando el PDF no expone texto nativo.
    """

    payload = viewer_source_payload(source, page=page)
    values: dict[str, Any] = {
        "viewer_source": payload,
        "viewer_page": payload["page"],
    }
    if origin_page:
        values["viewer_origin_page"] = str(origin_page)
    return values


def render_pdf_page(
    title: str,
    url: str,
    page_number: int,
    cache_directory: Path,
    allowed_hosts: tuple[str, ...],
    max_pdf_bytes: int,
    *,
    dpi: int = 125,
    rotation: int = 0,
) -> RenderedPdfPage:
    if page_number < 1:
        raise ValueError("La página debe ser mayor o igual a 1")
    if not 72 <= dpi <= 240:
        raise ValueError("La resolución debe estar entre 72 y 240 DPI")
    if rotation not in {0, 90, 180, 270}:
        raise ValueError("La rotación debe ser 0, 90, 180 o 270 grados")

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
        zoom = dpi / 72
        matrix = pymupdf.Matrix(zoom, zoom).prerotate(rotation)
        pixmap = page.get_pixmap(matrix=matrix, alpha=False)
        image_bytes = pixmap.tobytes("png")
        width, height = pixmap.width, pixmap.height
        text = str(page.get_text("text") or "").strip()

    return RenderedPdfPage(
        image_bytes=image_bytes,
        page_number=page_number,
        page_count=page_count,
        resolved_url=downloaded.resolved_url,
        width=width,
        height=height,
        text=text,
        dpi=dpi,
        rotation=rotation,
    )
