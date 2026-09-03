"""Normalizacion conservadora de ingredientes activos.

Este modulo no contiene una ontologia farmacologica ni intenta relacionar un
producto comercial con un ingrediente.  Su unica responsabilidad es conservar
el texto publicado, producir claves comparables y separar listas cuando el
documento usa delimitadores inequivocos.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata


@dataclass(frozen=True)
class ActiveIngredient:
    """Una mencion de ingrediente sin inferencias farmacologicas."""

    literal: str
    normalized: str
    canonical: str


_TRAILING_NON_ACTIVE_RE = re.compile(
    r"\b(?:excipiente(?:s)?|vehiculo|c\.\s*s\.|cantidad\s+suficiente)\b.*$",
    flags=re.IGNORECASE,
)
_DOSAGE_CONTAINER_PREFIX_RE = re.compile(
    r"^\s*cada\s+(?:\d+(?:[.,]\d+)?\s*)?"
    r"(?:tableta|comprimido|c[aá]psula|ampolla|vial|frasco|jeringa|dosis|ml|"
    r"mililitro|gramo|g|sobre|parche|unidad)\w*"
    r"(?:\s+(?!(?:contiene|no|sin|que)\b)"
    r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ][A-Za-zÁÉÍÓÚÜÑáéíóúüñ0-9-]*){0,3}"
    r"\s+contiene\s*(?::|;|[\-–—])?\s*",
    flags=re.IGNORECASE,
)
_NEGATED_DOSAGE_PREFIX_RE = re.compile(
    r"^\s*cada\s+(?:\d+(?:[.,]\d+)?\s*)?"
    r"(?:tableta|comprimido|c[aá]psula|ampolla|vial|frasco|jeringa|dosis|ml|"
    r"mililitro|gramo|g|sobre|parche|unidad)\w*"
    r"(?:\s+(?!(?:contiene|no|sin|que)\b)"
    r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ][A-Za-zÁÉÍÓÚÜÑáéíóúüñ0-9-]*){0,3}"
    r"\s+(?:no\s+contiene|sin\b)",
    flags=re.IGNORECASE,
)
_LEADING_BULLET_RE = re.compile(r"^\s*(?:[-*•·]|\(?\d+[.)](?!\d))\s*")
_STRENGTH_ONLY_RE = re.compile(
    r"^(?:\d+(?:[.,]\d+)?\s*)?(?:mg|mcg|ug|g|kg|ml|l|ui|%)"
    r"(?:\s*/\s*(?:mg|g|ml|l|dosis))?$",
    flags=re.IGNORECASE,
)
_LEADING_STRENGTH_RE = re.compile(
    r"^\s*\d+(?:[.,]\d+)?\s*(?:mg|mcg|ug|g|kg|ml|l|ui|u|%)"
    r"(?:\s*/\s*(?:mg|g|ml|l|dosis))?\s+(?:de\s+)?",
    flags=re.IGNORECASE,
)
_TRAILING_STRENGTH_RE = re.compile(
    r"\s+\d+(?:[.,]\d+)?\s*(?:mg|mcg|ug|g|kg|ml|l|ui|u|%)"
    r"(?:\s*/\s*(?:mg|g|ml|l|dosis))?\s*$",
    flags=re.IGNORECASE,
)


def normalize_ingredient(value: str) -> str:
    """Devuelve una clave comparable sin equiparar sales ni derivados."""

    decomposed = unicodedata.normalize("NFKD", value)
    without_accents = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    without_accents = without_accents.casefold()
    without_accents = re.sub(r"[^\w%/+.-]+", " ", without_accents)
    return " ".join(without_accents.split()).strip(" ;,.-")


def canonicalize_ingredient(value: str) -> str:
    """Genera una forma de presentacion estable, no una equivalencia clinica.

    Se conservan sales y formas químicas, pero se retira una concentración
    claramente separada para facilitar la comparación. El valor literal sigue
    intacto; por ejemplo, ``semaglutida`` y ``semaglutida sodica`` nunca se
    unen automáticamente.
    """

    clean = unicodedata.normalize("NFKC", value)
    clean = _LEADING_BULLET_RE.sub("", clean)
    clean = " ".join(clean.split()).strip(" ;,.-")
    clean = _LEADING_STRENGTH_RE.sub("", clean)
    clean = _TRAILING_STRENGTH_RE.sub("", clean)
    clean = clean.strip(" ;,.-")
    return clean


def _safe_segments(value: str) -> list[str]:
    # Los delimitadores elegidos son deliberadamente conservadores. No se
    # divide por coma (puede formar parte de un nombre o una concentracion) ni
    # por barra (p. ej. mg/mL).
    segments = re.split(
        r"\s*(?:;|\+|\n|\r|\s+[yY]\s+(?=[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]))\s*",
        value,
    )
    return [segment for segment in segments if segment and segment.strip()]


def parse_active_ingredients(value: str) -> tuple[ActiveIngredient, ...]:
    """Separa ingredientes solo ante delimitadores suficientemente claros.

    Las menciones de excipientes se descartan y los duplicados se eliminan por
    su clave normalizada. Una cadena ambigua se conserva como una sola mencion.
    """

    raw = unicodedata.normalize("NFKC", value or "")
    if _NEGATED_DOSAGE_PREFIX_RE.match(raw):
        return ()
    raw = _DOSAGE_CONTAINER_PREFIX_RE.sub("", raw)
    raw = _TRAILING_NON_ACTIVE_RE.sub("", raw).strip()
    if not raw:
        return ()

    result: list[ActiveIngredient] = []
    seen: set[str] = set()
    for candidate in _safe_segments(raw):
        literal = _LEADING_BULLET_RE.sub("", candidate).strip(" ;,.-")
        if not literal or _STRENGTH_ONLY_RE.fullmatch(literal):
            continue
        normalized = normalize_ingredient(literal)
        canonical = canonicalize_ingredient(literal)
        if not normalized or not canonical or normalized in seen:
            continue
        seen.add(normalized)
        result.append(
            ActiveIngredient(
                literal=literal,
                normalized=normalized,
                canonical=canonical,
            )
        )
    return tuple(result)
