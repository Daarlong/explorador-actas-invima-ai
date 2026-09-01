from __future__ import annotations

import re

from services.models import DocumentMetadata


PART_PATTERN = re.compile(
    r"\b(Primera|Segunda|Tercera|Cuarta|Quinta|Sexta)\s+Parte\b",
    flags=re.IGNORECASE,
)
ACT_PATTERN = re.compile(
    r"\bActa\s+(?:No\.?|N[.º°]?)?\s*(\d+)\s+de\s+(\d{4})(?:\s+(.+))?$",
    flags=re.IGNORECASE,
)


def parse_document_metadata(title: str, url: str) -> DocumentMetadata:
    clean_title = " ".join(title.split())
    part_match = PART_PATTERN.search(clean_title)
    part = part_match.group(0).title() if part_match else None

    title_without_part = clean_title
    if part_match:
        title_without_part = (
            clean_title[: part_match.start()] + clean_title[part_match.end() :]
        ).strip()

    act_match = ACT_PATTERN.search(title_without_part)
    if not act_match:
        return DocumentMetadata(title=clean_title, url=url, part=part)

    number, year, section = act_match.groups()
    return DocumentMetadata(
        title=clean_title,
        url=url,
        year=int(year),
        acta_number=number.zfill(2),
        section=section.strip().upper() if section else None,
        part=part,
    )

