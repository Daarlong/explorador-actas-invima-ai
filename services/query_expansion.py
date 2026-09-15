from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from services.text_utils import normalize_phrase


DEFAULT_DICTIONARY_PATH = Path(__file__).with_name("regulatory_synonyms.json")
MAX_EXPANSIONS = 8
MAX_MATCHED_ENTRIES = 4
MAX_QUERY_CHARACTERS = 400


@dataclass(frozen=True)
class SynonymEntry:
    entry_id: str
    preferred: str
    variants: tuple[str, ...]

    @property
    def forms(self) -> tuple[str, ...]:
        return (self.preferred, *self.variants)


@dataclass(frozen=True)
class RegulatoryDictionary:
    version: str
    language: str
    entries: tuple[SynonymEntry, ...]


@dataclass(frozen=True)
class QueryExpansion:
    """Resultado auditable de una expansión, sin modificar la consulta fuente."""

    original_query: str
    original_terms: tuple[str, ...]
    expanded_terms: tuple[str, ...]
    matched_entries: tuple[str, ...]
    dictionary_version: str
    skipped_reason: str | None = None

    @property
    def applied(self) -> bool:
        return bool(self.expanded_terms)


def _normalize_matching_text(value: str) -> str:
    """Normaliza tildes y también acrónimos escritos como ``B.P.M.``."""

    words = normalize_phrase(value).split()
    normalized: list[str] = []
    position = 0
    while position < len(words):
        end = position
        while end < len(words) and len(words[end]) == 1 and words[end].isalpha():
            end += 1
        if end - position >= 2:
            normalized.append("".join(words[position:end]))
            position = end
            continue
        normalized.append(words[position])
        position += 1
    return " ".join(normalized)


def _clean_display_form(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _validate_dictionary(payload: Any) -> RegulatoryDictionary:
    if not isinstance(payload, dict):
        raise ValueError("El diccionario regulatorio debe ser un objeto JSON")
    version = _clean_display_form(payload.get("version", ""))
    language = _clean_display_form(payload.get("language", ""))
    raw_entries = payload.get("entries")
    if not version or not language or not isinstance(raw_entries, list):
        raise ValueError("El diccionario requiere version, language y entries")

    entries: list[SynonymEntry] = []
    entry_ids: set[str] = set()
    alias_owners: dict[str, str] = {}
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            raise ValueError("Cada equivalencia regulatoria debe ser un objeto")
        entry_id = _clean_display_form(raw_entry.get("id", ""))
        preferred = _clean_display_form(raw_entry.get("preferred", ""))
        raw_variants = raw_entry.get("variants")
        if not entry_id or not preferred or not isinstance(raw_variants, list):
            raise ValueError("Cada equivalencia requiere id, preferred y variants")
        if entry_id in entry_ids:
            raise ValueError(f"ID regulatorio duplicado: {entry_id}")
        entry_ids.add(entry_id)

        display_forms = [preferred, *(_clean_display_form(item) for item in raw_variants)]
        forms: list[str] = []
        normalized_in_entry: set[str] = set()
        for display_form in display_forms:
            normalized = _normalize_matching_text(display_form)
            if not normalized or normalized in normalized_in_entry:
                continue
            owner = alias_owners.get(normalized)
            if owner is not None and owner != entry_id:
                raise ValueError(
                    f"Equivalencia ambigua {display_form!r}: {owner} y {entry_id}"
                )
            alias_owners[normalized] = entry_id
            normalized_in_entry.add(normalized)
            forms.append(display_form)
        if len(forms) < 2:
            raise ValueError(f"La equivalencia {entry_id} necesita al menos dos formas")
        entries.append(
            SynonymEntry(
                entry_id=entry_id,
                preferred=forms[0],
                variants=tuple(forms[1:]),
            )
        )
    return RegulatoryDictionary(version, language, tuple(entries))


@lru_cache(maxsize=8)
def load_regulatory_dictionary(
    path: str | Path = DEFAULT_DICTIONARY_PATH,
) -> RegulatoryDictionary:
    dictionary_path = Path(path)
    with dictionary_path.open("r", encoding="utf-8") as source:
        return _validate_dictionary(json.load(source))


def _contains_identifier(query: str) -> bool:
    """Detecta radicados/expedientes/CUM y evita modificar su recuperación."""

    # Los identificadores regulatorios contienen normalmente números; también
    # se protegen URL y correos porque dividirlos produciría falsos sinónimos.
    if re.search(r"https?://|www\.|\S+@\S+", query, flags=re.IGNORECASE):
        return True
    return bool(
        re.search(
            r"(?<!\w)(?=[\w./-]*\d)[A-Za-z0-9][A-Za-z0-9._/-]{2,}(?!\w)",
            query,
        )
    )


def _contains_form(query_tokens: tuple[str, ...], form: str) -> bool:
    form_tokens = tuple(_normalize_matching_text(form).split())
    if not form_tokens or len(form_tokens) > len(query_tokens):
        return False
    width = len(form_tokens)
    return any(
        query_tokens[start : start + width] == form_tokens
        for start in range(len(query_tokens) - width + 1)
    )


def expand_regulatory_query(
    query: str,
    *,
    exact_phrase: bool = False,
    dictionary_path: str | Path = DEFAULT_DICTIONARY_PATH,
    max_expansions: int = MAX_EXPANSIONS,
) -> QueryExpansion:
    """Añade equivalencias regulatorias conservando siempre la consulta original.

    La expansión es local, determinista y de un solo salto: una forma presente
    en la consulta habilita las demás formas de su entrada, pero las formas
    añadidas no disparan nuevas entradas. Frases exactas e identificadores se
    mantienen totalmente intactos.
    """

    if max_expansions < 0 or max_expansions > MAX_EXPANSIONS:
        raise ValueError(f"max_expansions debe estar entre 0 y {MAX_EXPANSIONS}")
    raw_query = str(query or "").strip()
    dictionary = load_regulatory_dictionary(dictionary_path)
    normalized_query = _normalize_matching_text(raw_query)
    original_terms = tuple(normalized_query.split())

    common = {
        "original_query": raw_query,
        "original_terms": original_terms,
        "expanded_terms": (),
        "matched_entries": (),
        "dictionary_version": dictionary.version,
    }
    if not raw_query or not original_terms:
        return QueryExpansion(**common, skipped_reason="empty_query")
    if exact_phrase:
        return QueryExpansion(**common, skipped_reason="exact_phrase")
    if len(raw_query) > MAX_QUERY_CHARACTERS:
        return QueryExpansion(**common, skipped_reason="query_too_long")
    if _contains_identifier(raw_query):
        return QueryExpansion(**common, skipped_reason="identifier_present")
    if max_expansions == 0:
        return QueryExpansion(**common, skipped_reason="disabled")

    # FTS tokeniza ``B.P.M.`` como letras aisladas. Al reconocer una entrada se
    # añadirá también la sigla canónica si esa forma no aparece en la superficie
    # original, sin sustituir ni ocultar lo escrito por el usuario.
    surface_terms = tuple(normalize_phrase(raw_query).split())
    expansions: list[str] = []
    matched_entries: list[str] = []
    normalized_expansions: set[str] = set(expansions)
    for entry in dictionary.entries:
        matched_forms = [
            form for form in entry.forms if _contains_form(original_terms, form)
        ]
        if not matched_forms:
            continue
        matched_entries.append(entry.entry_id)
        matched_normalized = {
            _normalize_matching_text(form) for form in matched_forms
        }
        for normalized in sorted(matched_normalized):
            if _contains_form(surface_terms, normalized):
                continue
            expansions.append(normalized)
            normalized_expansions.add(normalized)
            if len(expansions) == max_expansions:
                break
        if len(expansions) == max_expansions:
            break
        for form in entry.forms:
            normalized = _normalize_matching_text(form)
            if normalized in matched_normalized or normalized in normalized_expansions:
                continue
            expansions.append(normalized)
            normalized_expansions.add(normalized)
            if len(expansions) == max_expansions:
                break
        if (
            len(expansions) == max_expansions
            or len(matched_entries) == MAX_MATCHED_ENTRIES
        ):
            break

    return QueryExpansion(
        original_query=raw_query,
        original_terms=original_terms,
        expanded_terms=tuple(expansions),
        matched_entries=tuple(matched_entries),
        dictionary_version=dictionary.version,
        skipped_reason=None if expansions else "no_dictionary_match",
    )
