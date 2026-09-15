from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal


PageTextSource = Literal["native_pdf", "ocr"]


@dataclass(frozen=True)
class PageSourceText:
    """Texto fuente preservado antes de dividirlo para búsqueda."""

    page_number: int
    text: str
    source: PageTextSource
    extractor_version: str
    quality: float | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.page_number < 1:
            raise ValueError("El número de página debe ser positivo")
        if self.source not in {"native_pdf", "ocr"}:
            raise ValueError("La fuente del texto debe ser native_pdf u ocr")
        if self.quality is not None and not 0 <= self.quality <= 1:
            raise ValueError("La calidad del texto debe estar entre 0 y 1")
        if not self.extractor_version.strip():
            raise ValueError("La versión del extractor de texto es obligatoria")


@dataclass(frozen=True)
class DocumentMetadata:
    title: str
    url: str
    year: int | None = None
    acta_number: str | None = None
    section: str | None = None
    part: str | None = None
    source_type: str = "official"
    catalog_id: str | None = None
    publication_date: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SearchResult:
    chunk_id: int
    title: str
    url: str
    page: int
    text: str
    year: int | None
    acta_number: str | None
    section: str | None
    part: str | None
    source_type: str
    score: float
    lexical_score: float | None = None
    semantic_score: float | None = None
    match_type: str = "textual"
    # Trazabilidad de la unidad que produjo la coincidencia. ``chunk_id``
    # continúa siendo siempre un fragmento real para conservar compatibilidad
    # con el visor y el analista; estos campos explican si la coincidencia se
    # confirmó en la página completa o en una ficha estructurada.
    evidence_scope: str = "chunk"
    matched_field: str | None = None
    match_excerpt: str | None = None
    document_id: int | None = None

    @property
    def source_label(self) -> str:
        return f"{self.title} — página {self.page}"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SearchResult":
        allowed = {field.name for field in cls.__dataclass_fields__.values()}
        return cls(**{key: item for key, item in value.items() if key in allowed})


@dataclass(frozen=True)
class SearchDocument:
    """Resultado agrupado por documento para una página de búsqueda."""

    document_id: int | None
    title: str
    url: str
    year: int | None
    acta_number: str | None
    section: str | None
    part: str | None
    source_type: str
    score: float
    match_count: int
    fragments: tuple[SearchResult, ...]

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["fragments"] = [item.as_dict() for item in self.fragments]
        return value
