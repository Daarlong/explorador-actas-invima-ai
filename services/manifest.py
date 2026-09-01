from __future__ import annotations

import csv
from pathlib import Path
from urllib.parse import urlparse

from services.metadata import parse_document_metadata
from services.models import DocumentMetadata


def is_allowed_url(url: str, allowed_hosts: tuple[str, ...]) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and host in allowed_hosts


def load_manifest(
    manifest_path: Path,
    allowed_hosts: tuple[str, ...],
) -> list[DocumentMetadata]:
    if not manifest_path.exists():
        return []

    documents: list[DocumentMetadata] = []
    with manifest_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not {"title", "url"}.issubset(reader.fieldnames):
            raise ValueError("El manifiesto debe contener las columnas title y url")

        for line_number, row in enumerate(reader, start=2):
            title = (row.get("title") or "").strip()
            url = (row.get("url") or "").strip()
            if not title or not url:
                continue
            if not is_allowed_url(url, allowed_hosts):
                raise ValueError(
                    f"URL no permitida en la línea {line_number}: {url}"
                )

            parsed = parse_document_metadata(title, url)
            source_type = (row.get("source_type") or "official").strip().lower()
            if source_type not in {"official", "historical_mirror"}:
                raise ValueError(
                    f"Tipo de fuente no reconocido en la línea {line_number}: "
                    f"{source_type}"
                )
            documents.append(
                DocumentMetadata(
                    title=parsed.title,
                    url=parsed.url,
                    year=int(row["year"]) if row.get("year") else parsed.year,
                    acta_number=(row.get("acta_number") or parsed.acta_number),
                    section=(row.get("section") or parsed.section),
                    part=(row.get("part") or parsed.part),
                    source_type=source_type,
                )
            )

    unique: dict[str, DocumentMetadata] = {}
    for document in documents:
        unique[document.url] = document
    return list(unique.values())
