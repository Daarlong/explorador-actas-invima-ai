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
    lexical_score: float | None = None
    semantic_score: float | None = None
    match_type: str = "textual"

    @property
    def source_label(self) -> str:
        return f"{self.title} — página {self.page}"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SearchResult":
        allowed = {field.name for field in cls.__dataclass_fields__.values()}
        return cls(**{key: item for key, item in value.items() if key in allowed})
