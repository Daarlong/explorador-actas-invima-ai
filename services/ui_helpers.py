from __future__ import annotations

import html
import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit

from services.manifest import is_allowed_url
from services.models import SearchResult
from services.text_utils import tokenize_query


def pdf_page_url(
    url: str,
    page: int,
    allowed_hosts: tuple[str, ...],
) -> str:
    if not is_allowed_url(url, allowed_hosts):
        return ""
    parsed = urlsplit(url)
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, parsed.query, f"page={max(1, page)}")
    )


def _normalized_with_positions(value: str) -> tuple[str, list[int]]:
    characters: list[str] = []
    positions: list[int] = []
    for position, character in enumerate(value):
        decomposed = unicodedata.normalize("NFKD", character.lower())
        for item in decomposed:
            if unicodedata.combining(item):
                continue
            characters.append(item)
            positions.append(position)
    return "".join(characters), positions


def highlight_query(text: str, query: str) -> str:
    """Resalta términos sin permitir que el texto documental inyecte HTML."""
    terms = tokenize_query(query)
    if not terms or not text:
        return html.escape(text)

    normalized, positions = _normalized_with_positions(text)
    intervals: list[tuple[int, int]] = []
    for term in sorted(terms, key=len, reverse=True):
        for match in re.finditer(
            rf"(?<!\w){re.escape(term)}(?!\w)",
            normalized,
            flags=re.UNICODE,
        ):
            start = positions[match.start()]
            end = positions[match.end() - 1] + 1
            intervals.append((start, end))

    if not intervals:
        return html.escape(text)
    merged: list[list[int]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    pieces: list[str] = []
    cursor = 0
    for start, end in merged:
        pieces.append(html.escape(text[cursor:start]))
        pieces.append(f"<mark>{html.escape(text[start:end])}</mark>")
        cursor = end
    pieces.append(html.escape(text[cursor:]))
    return "".join(pieces)


def group_search_results(
    results: list[SearchResult],
    *,
    order: str = "relevance",
) -> list[dict]:
    groups: dict[tuple[str, str], dict] = {}
    for result in results:
        key = (result.title, result.url)
        group = groups.setdefault(
            key,
            {
                "title": result.title,
                "url": result.url,
                "year": result.year,
                "acta_number": result.acta_number,
                "section": result.section,
                "part": result.part,
                "source_type": result.source_type,
                "score": result.score,
                "results": [],
            },
        )
        if not any(item.chunk_id == result.chunk_id for item in group["results"]):
            group["results"].append(result)
        group["score"] = max(float(group["score"]), result.score)

    values = list(groups.values())
    if order == "newest":
        values.sort(
            key=lambda item: (item["year"] or 0, item["score"]),
            reverse=True,
        )
    elif order == "oldest":
        values.sort(key=lambda item: (item["year"] or 9999, -item["score"]))
    else:
        values.sort(key=lambda item: item["score"], reverse=True)
    return values
