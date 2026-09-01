from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class DocumentMetadata:
    title: str
    url: str
    year: int | None = None
    acta_number: str | None = None
    section: str | None = None
    part: str | None = None
    source_type: str = "official"

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

    @property
    def source_label(self) -> str:
        return f"{self.title} — página {self.page}"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
