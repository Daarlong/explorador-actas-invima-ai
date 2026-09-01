from __future__ import annotations

import re
import unicodedata


SPANISH_STOPWORDS = {
    "a", "al", "como", "con", "cual", "cuando", "de", "del", "donde",
    "el", "en", "es", "la", "las", "lo", "los", "o", "para", "por",
    "que", "quien", "se", "sin", "sobre", "son", "su", "sus", "un",
    "una", "unos", "unas", "y",
}


def remove_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(
        character
        for character in normalized
        if not unicodedata.combining(character)
    )


def normalize_text(text: str) -> str:
    text = remove_accents(text.lower())
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def tokenize_query(query: str) -> list[str]:
    tokens = re.findall(r"\b[\w-]+\b", normalize_text(query), flags=re.UNICODE)
    useful = [
        token
        for token in tokens
        if token not in SPANISH_STOPWORDS and (len(token) > 2 or token.isdigit())
    ]
    return list(dict.fromkeys(useful))


def chunk_text(text: str, chunk_size: int = 1200, overlap: int = 180) -> list[str]:
    """Divide texto sin cortar palabras y conserva solapamiento entre fragmentos."""
    if chunk_size <= 0:
        raise ValueError("chunk_size debe ser mayor que cero")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap debe estar entre cero y chunk_size - 1")

    clean = re.sub(r"\s+", " ", text).strip()
    if not clean:
        return []
    if len(clean) <= chunk_size:
        return [clean]

    chunks: list[str] = []
    start = 0
    text_length = len(clean)

    while start < text_length:
        tentative_end = min(start + chunk_size, text_length)
        end = tentative_end

        if tentative_end < text_length:
            boundary = clean.rfind(" ", start + int(chunk_size * 0.75), tentative_end)
            if boundary > start:
                end = boundary

        chunk = clean[start:end].strip()
        if chunk:
            chunks.append(chunk)

        if end >= text_length:
            break

        next_start = max(0, end - overlap)
        next_boundary = clean.find(" ", next_start, end)
        if next_boundary != -1:
            next_start = next_boundary + 1
        if next_start <= start:
            next_start = end
        start = next_start

    return chunks

