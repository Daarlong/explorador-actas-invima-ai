from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from services.manifest import is_allowed_url
from services.models import SearchResult
from services.text_utils import tokenize_query


_BADGE_TONES = frozenset({"neutral", "info", "success", "warning", "danger"})


def app_style_css() -> str:
    """Return the shared, theme-aware presentation layer for the application.

    The stylesheet deliberately targets Streamlit's semantic ``data-testid``
    hooks and native roles instead of generated class names.  Colour values are
    derived from the active Streamlit theme, so the same CSS works when the user
    switches between the light and dark themes defined in ``config.toml``.
    """
    return """
    [data-testid="stAppViewContainer"] {
        --actas-primary: var(--primary-color, #0b668e);
        --actas-surface: var(--background-color, #ffffff);
        --actas-surface-muted: var(--secondary-background-color, #f3f7fa);
        --actas-text: var(--text-color, #172433);
        --actas-border: color-mix(in srgb, var(--actas-text) 15%, transparent);
        --actas-border-strong: color-mix(in srgb, var(--actas-text) 24%, transparent);
        --actas-primary-soft: color-mix(in srgb, var(--actas-primary) 11%, transparent);
        --actas-shadow: 0 0.5rem 1.5rem color-mix(in srgb, var(--actas-text) 9%, transparent);
        --actas-shadow-soft: 0 0.18rem 0.75rem color-mix(in srgb, var(--actas-text) 7%, transparent);
    }

    [data-testid="stMainBlockContainer"] {
        width: min(100%, 92rem);
        padding-top: clamp(1.4rem, 3vw, 2.6rem);
        padding-bottom: 4rem;
    }

    [data-testid="stMainBlockContainer"] h1,
    [data-testid="stMainBlockContainer"] h2,
    [data-testid="stMainBlockContainer"] h3 {
        letter-spacing: -0.025em;
        line-height: 1.15;
        text-wrap: balance;
    }

    [data-testid="stMainBlockContainer"] p,
    [data-testid="stMainBlockContainer"] li {
        line-height: 1.62;
    }

    [data-testid="stSidebar"] {
        border-right: 1px solid var(--actas-border);
    }

    [data-testid="stSidebarContent"] {
        padding-top: 1.1rem;
    }

    [data-testid="stSidebarNav"] a {
        border-radius: 0.65rem;
        margin-block: 0.12rem;
        transition: background-color 140ms ease, color 140ms ease;
    }

    [data-testid="stSidebarNav"] a[aria-current="page"] {
        background: var(--actas-primary-soft);
        font-weight: 650;
    }

    [data-testid="stMetric"] {
        min-height: 7.1rem;
        padding: 1rem 1.05rem;
        border: 1px solid var(--actas-border);
        border-radius: 0.85rem;
        background: color-mix(in srgb, var(--actas-surface) 94%, var(--actas-primary) 6%);
        box-shadow: var(--actas-shadow-soft);
    }

    [data-testid="stMetricLabel"] {
        color: color-mix(in srgb, var(--actas-text) 72%, transparent);
        font-weight: 600;
    }

    [data-testid="stMetricValue"] {
        letter-spacing: -0.035em;
        line-height: 1.05;
    }

    [data-testid="stForm"],
    [data-testid="stExpander"],
    [data-testid="stVerticalBlockBorderWrapper"] {
        border-color: var(--actas-border);
        border-radius: 0.85rem;
        box-shadow: var(--actas-shadow-soft);
    }

    [data-testid="stAlert"] {
        border-radius: 0.75rem;
        border-width: 1px;
    }

    [data-testid="stButton"] button,
    [data-testid="stDownloadButton"] button,
    [data-testid="stLinkButton"] a {
        min-height: 2.65rem;
        font-weight: 650;
        letter-spacing: 0.005em;
        transition: transform 120ms ease, box-shadow 120ms ease, border-color 120ms ease;
    }

    [data-testid="stButton"] button:hover,
    [data-testid="stDownloadButton"] button:hover,
    [data-testid="stLinkButton"] a:hover {
        transform: translateY(-1px);
        box-shadow: var(--actas-shadow-soft);
    }

    [data-testid="stButton"] button:active,
    [data-testid="stDownloadButton"] button:active,
    [data-testid="stLinkButton"] a:active {
        transform: translateY(0);
        box-shadow: none;
    }

    [data-testid="stTextInput"] input,
    [data-testid="stNumberInput"] input,
    [data-testid="stTextArea"] textarea,
    [data-testid="stSelectbox"] [role="combobox"],
    [data-testid="stMultiSelect"] [role="combobox"] {
        caret-color: var(--actas-primary);
    }

    [data-testid="stTextInput"] input:focus-visible,
    [data-testid="stNumberInput"] input:focus-visible,
    [data-testid="stTextArea"] textarea:focus-visible,
    [data-testid="stButton"] button:focus-visible,
    [data-testid="stDownloadButton"] button:focus-visible,
    [data-testid="stLinkButton"] a:focus-visible {
        outline: 3px solid color-mix(in srgb, var(--actas-primary) 32%, transparent);
        outline-offset: 2px;
    }

    [data-testid="stTabs"] [role="tablist"] {
        gap: 0.35rem;
        border-bottom: 1px solid var(--actas-border);
    }

    [data-testid="stTabs"] [role="tab"] {
        min-height: 2.75rem;
        border-radius: 0.55rem 0.55rem 0 0;
        font-weight: 600;
    }

    [data-testid="stDataFrame"],
    [data-testid="stTable"] {
        overflow: hidden;
        border: 1px solid var(--actas-border);
        border-radius: 0.75rem;
    }

    [data-testid="stMarkdownContainer"] a {
        text-underline-offset: 0.18em;
        text-decoration-thickness: 0.08em;
    }

    [data-testid="stMarkdownContainer"] mark {
        padding: 0.08em 0.2em;
        border-radius: 0.25rem;
        background: color-mix(in srgb, #f5b942 42%, var(--actas-surface));
        color: var(--actas-text);
    }

    [data-testid="stMarkdownContainer"] hr {
        border-color: var(--actas-border);
    }

    .actas-ui-card {
        padding: 1rem 1.1rem;
        border: 1px solid var(--actas-border);
        border-radius: 0.85rem;
        background: color-mix(in srgb, var(--actas-surface) 97%, var(--actas-primary) 3%);
        box-shadow: var(--actas-shadow-soft);
    }

    .actas-ui-meta {
        color: color-mix(in srgb, var(--actas-text) 68%, transparent);
        font-size: 0.875rem;
        line-height: 1.45;
    }

    .actas-ui-badge {
        display: inline-flex;
        align-items: center;
        max-width: 100%;
        padding: 0.24rem 0.55rem;
        border: 1px solid var(--actas-border);
        border-radius: 999px;
        background: var(--actas-surface-muted);
        color: var(--actas-text);
        font-size: 0.78rem;
        font-weight: 650;
        line-height: 1.2;
        overflow-wrap: anywhere;
    }

    .actas-ui-badge[data-tone="info"] {
        border-color: color-mix(in srgb, var(--actas-primary) 35%, transparent);
        background: var(--actas-primary-soft);
        color: color-mix(in srgb, var(--actas-primary) 82%, var(--actas-text));
    }

    .actas-ui-badge[data-tone="success"] {
        border-color: color-mix(in srgb, #168557 36%, transparent);
        background: color-mix(in srgb, #168557 12%, transparent);
        color: color-mix(in srgb, #168557 72%, var(--actas-text));
    }

    .actas-ui-badge[data-tone="warning"] {
        border-color: color-mix(in srgb, #b46a09 38%, transparent);
        background: color-mix(in srgb, #b46a09 12%, transparent);
        color: color-mix(in srgb, #b46a09 70%, var(--actas-text));
    }

    .actas-ui-badge[data-tone="danger"] {
        border-color: color-mix(in srgb, #c43d4b 38%, transparent);
        background: color-mix(in srgb, #c43d4b 12%, transparent);
        color: color-mix(in srgb, #c43d4b 72%, var(--actas-text));
    }

    @media (max-width: 48rem) {
        [data-testid="stMainBlockContainer"] {
            padding-top: 1rem;
            padding-inline: 1rem;
        }

        [data-testid="stMetric"] {
            min-height: auto;
        }
    }

    @media (prefers-reduced-motion: reduce) {
        [data-testid="stAppViewContainer"] * {
            scroll-behavior: auto !important;
            transition-duration: 0.01ms !important;
            animation-duration: 0.01ms !important;
            animation-iteration-count: 1 !important;
        }
    }
    """.strip()


def apply_app_style() -> None:
    """Inject the shared application stylesheet in the current Streamlit page."""
    import streamlit as st

    st.markdown(f"<style>{app_style_css()}</style>", unsafe_allow_html=True)


def metadata_line(values: Iterable[Any], *, separator: str = " · ") -> str:
    """Join present metadata values into a compact, human-readable line."""
    clean_values: list[str] = []
    for value in values:
        if value is None:
            continue
        normalized = " ".join(str(value).split())
        if normalized:
            clean_values.append(normalized)
    return separator.join(clean_values)


def result_metadata(
    *,
    year: int | str | None = None,
    acta_number: str | None = None,
    section: str | None = None,
    part: str | int | None = None,
    page: int | str | None = None,
) -> str:
    """Format the common source coordinates used by search-result cards."""
    return metadata_line(
        (
            f"Acta {acta_number}" if acta_number else None,
            year,
            section,
            f"Parte {part}" if part not in (None, "") else None,
            f"p. {page}" if page not in (None, "") else None,
        )
    )


def badge_html(label: Any, *, tone: str = "neutral", title: str | None = None) -> str:
    """Build an escaped badge for use in trusted application templates.

    ``tone`` is allow-listed, while both visible and title text are HTML
    escaped.  An empty label intentionally produces no markup.
    """
    normalized_label = " ".join(str(label).split()) if label is not None else ""
    if not normalized_label:
        return ""
    safe_tone = tone if tone in _BADGE_TONES else "neutral"
    title_attribute = ""
    if title:
        title_attribute = f' title="{html.escape(str(title), quote=True)}"'
    return (
        f'<span class="actas-ui-badge" data-tone="{safe_tone}"'
        f"{title_attribute}>{html.escape(normalized_label)}</span>"
    )


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
