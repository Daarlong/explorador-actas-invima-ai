"""Extraccion deterministica de decisiones regulatorias en actas de INVIMA.

El extractor trabaja sobre el texto ya separado por pagina. No intenta interpretar
todo el documento: identifica bloques rotulados (Producto, Expediente, Solicitud,
Concepto, etc.), conserva las continuaciones entre paginas y normaliza solamente
el resultado general de la decision.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import re
from typing import Any, Mapping, Sequence
import unicodedata

from services.ingredients import parse_active_ingredients


RESULT_APPROVED = "aprobado"
RESULT_DENIED = "negado"
RESULT_REQUIRED = "requerido"
RESULT_WITHDRAWN = "desistido"
RESULT_ARCHIVED = "archivado"
RESULT_FAVORABLE = "favorable"
RESULT_UNFAVORABLE = "no_favorable"
RESULT_UNCLASSIFIED = "sin_clasificar"


@dataclass(frozen=True)
class FieldEvidence:
    """Procedencia auditable de un valor extraido.

    ``valor_canonico`` es solamente una forma estable de presentacion. Para
    ingredientes no equipara sales, derivados ni nombres comerciales.
    """

    campo: str
    valor_literal: str | None
    valor_normalizado: str | None
    valor_canonico: str | None
    pagina: int
    pagina_final: int
    fragmento: str
    metodo: str
    confianza: float
    ordinal: int = 1


@dataclass(frozen=True)
class _CapturedPart:
    value: str
    page: int
    fragment: str
    method: str
    page_final: int | None = None


@dataclass(frozen=True)
class RegulatoryRecord:
    """Decision regulatoria extraida de un bloque de un acta."""

    producto: str | None
    principio_activo: str | None
    interesado: str | None
    expediente: str | None
    radicado: str | None
    solicitud: str | None
    concepto: str | None
    resultado_normalizado: str
    pagina: int
    pagina_final: int | None = None
    numeral: str | None = None
    titulo_numeral: str | None = None
    fecha_sesion: str | None = None
    fecha_sesion_original: str | None = None
    tipo_solicitud: str | None = None
    principios_activos: tuple[str, ...] = ()
    principios_activos_normalizados: tuple[str, ...] = ()
    principios_activos_canonicos: tuple[str, ...] = ()
    evidencias_campos: tuple[FieldEvidence, ...] = ()

    @property
    def page(self) -> int:
        """Alias util para integraciones que ya usan el nombre ``page``."""

        return self.pagina

    def as_dict(self) -> dict[str, Any]:
        """Devuelve una representacion compuesta solo por tipos serializables."""

        return asdict(self)

    def evidence_for(self, field: str) -> tuple[FieldEvidence, ...]:
        """Devuelve las evidencias de un campo en el orden publicado."""

        return tuple(item for item in self.evidencias_campos if item.campo == field)


def normalize_field_evidence_ordinals(
    evidences: Sequence[FieldEvidence],
) -> tuple[FieldEvidence, ...]:
    """Deduplica fuentes identicas y asigna ordinales unicos por campo.

    Cada registro permite varias evidencias para un mismo campo (por ejemplo,
    varios principios activos o dos fragmentos del acta que sustentan el mismo
    valor). Los ordinales que produce cada bloque empiezan en uno; al fusionar
    bloques historicos esos ordinales pueden colisionar. Esta normalizacion
    conserva todas las fuentes distintas, elimina solo duplicados exactos y
    renumera en el orden determinista en que aparecen en el documento.
    """

    unique: list[FieldEvidence] = []
    positions: dict[tuple[object, ...], int] = {}
    for evidence in evidences:
        source_key = (
            evidence.campo,
            evidence.valor_literal,
            evidence.valor_normalizado,
            evidence.valor_canonico,
            int(evidence.pagina),
            int(evidence.pagina_final),
            evidence.fragmento,
            evidence.metodo,
        )
        existing_position = positions.get(source_key)
        if existing_position is None:
            positions[source_key] = len(unique)
            unique.append(evidence)
            continue
        if evidence.confianza > unique[existing_position].confianza:
            unique[existing_position] = evidence

    next_ordinal: dict[str, int] = {}
    normalized: list[FieldEvidence] = []
    for evidence in unique:
        ordinal = next_ordinal.get(evidence.campo, 0) + 1
        next_ordinal[evidence.campo] = ordinal
        normalized.append(replace(evidence, ordinal=ordinal))
    return tuple(normalized)


# Los patrones se aplican a una copia sin acentos del texto. El orden importa:
# los rotulos mas especificos deben evaluarse antes que los cortos.
_FIELD_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        "principio_activo",
        r"(?:principio|ingrediente)s?\s+activo(?:s)?|"
        r"(?:denominacion\s+comun\s+internacional|d\.?\s*c\.?\s*i\.?|"
        r"i\.?\s*f\.?\s*a\.?)|"
        r"composicion(?:\s+(?:cualitativa|cuantitativa|del\s+producto))?",
    ),
    (
        "producto",
        r"(?:nombre\s+(?:comercial\s+)?del\s+)?(?:producto|medicamento)",
    ),
    (
        "interesado",
        r"(?:interesad(?:o|a)(?:s|\(a?s\))?|solicitante(?:s)?|"
        r"peticionari(?:o|a)(?:s)?|"
        r"titular(?:es)?(?:\s+del\s+registro\s+sanitario)?)",
    ),
    (
        "expediente",
        r"(?:(?:n\.?[°º]|n(?:\.?o|ro|umero)?\.?)\s*(?:de\s+)?)?"
        r"expediente(?:s|\(s\))?"
        r"(?:\s*(?:n\.?[°º]|n(?:\.?o|ro|umero)?\.?))?",
    ),
    (
        "radicado",
        r"(?:(?:n\.?[°º]|n(?:\.?o|ro|umero)?\.?)\s*(?:de\s+)?)?"
        r"radic(?:ado(?:s|\(s\))?|acion(?:es)?)"
        r"(?:\s*(?:n\.?[°º]|n(?:\.?o|ro|umero)?\.?))?",
    ),
    (
        "solicitud",
        r"(?:solicitud(?:\s+(?:del?\s+)?(?:interesado|peticionario))?|peticion)",
    ),
    (
        "concepto",
        r"(?:concepto(?:\s+(?:(?:de|emitido\s+por)\s+la\s+)?"
        r"(?:sala(?:\s+especializada)?|comision\s+revisora))?"
        r"|decision|conclusion|resultado)",
    ),
)

_STOP_PATTERNS: tuple[str, ...] = (
    r"(?:forma\s+farmaceutica|concentracion|via\s+de\s+administracion)",
    r"(?:fabricante|importador|acondicionador|pais\s+de\s+origen)",
    r"(?:indicacion(?:es)?|contraindicacion(?:es)?)",
    r"(?:fecha(?:\s+de\s+radicacion)?|asunto|antecedentes?)",
    r"(?:registro\s+sanitario|modalidad|grupo\s+farmacologico)",
)

_LABEL_PATTERN_BY_FIELD = {field: pattern for field, pattern in _FIELD_PATTERNS}
_LABEL_ALTERNATIVES = [
    f"(?P<f{index}>{pattern})"
    for index, (_, pattern) in enumerate(_FIELD_PATTERNS)
] + [
    f"(?P<s{index}>{pattern})"
    for index, pattern in enumerate(_STOP_PATTERNS)
]
_LABEL_RE = re.compile(
    r"(?<![\w/])(?:"
    + "|".join(_LABEL_ALTERNATIVES)
    + r")\s*(?::|;|[\-–—])\s*",
    flags=re.IGNORECASE,
)
_BARE_LABEL_RE = re.compile(
    r"^(?:" + "|".join(_LABEL_ALTERNATIVES) + r")\s*$",
    flags=re.IGNORECASE,
)
_SECTION_HEADING_RE = re.compile(
    r"^\s*(?:(?i:numeral)\s+)?"
    # El look-ahead y las mayusculas del romano son deliberados: evitan partir
    # frases como "La Sala" o "De acuerdo" como L/D + supuesto titulo.
    r"(?P<numeral>(?:\d{1,3}(?:\.\d{1,3}){0,7}|"
    r"[IVXLCDM]{1,8}(?=[.)\s\-–—])))"
    r"(?:[.)]\s*[-–—]?|\s*[-–—])?\s*"
    r"(?P<title>[A-ZÁÉÍÓÚÜÑ][^:]{1,180})?$",
)
_HEADING_WITH_CONTENT_RE = re.compile(
    r"^\s*(?:(?i:numeral)\s+)?"
    r"(?P<numeral>(?:\d{1,3}(?:\.\d{1,3}){0,7}|"
    r"[IVXLCDM]{1,8}(?=[.)\s\-–—])))"
    r"(?:[.)]\s*[-–—]?|\s*[-–—])?\s+"
    r"(?P<content>(?i:(?:producto|medicamento|nombre\s+"
    r"(?:comercial\s+)?del\s+(?:producto|medicamento))\s*[:;\-–—].+))$",
)
# En actas historicas el recipiente incluye forma y calificadores (por ejemplo,
# ``Cada tableta recubierta contiene`` o ``Cada 1 mL de solucion contiene``).
# La lista sigue siendo cerrada y excluye de forma explicita negaciones para no
# convertir frases narrativas como ``no contiene`` en una composicion.
_DOSAGE_CONTAINER_PATTERN = (
    r"(?:tableta|comprimido|capsula|ampolla|vial|frasco|jeringa|dosis|ml|"
    r"mililitro|gramo|g|sobre|parche|unidad)\w*"
)
_DOSAGE_QUALIFIER_PATTERN = (
    r"(?:\s+(?!(?:contiene|no|sin|que)\b)[a-z][a-z0-9-]*){0,3}"
)
_DOSAGE_STATEMENT_RE = re.compile(
    r"^\s*(?P<label>cada\s+(?:\d+(?:[.,]\d+)?\s*)?"
    + _DOSAGE_CONTAINER_PATTERN
    + _DOSAGE_QUALIFIER_PATTERN
    + r"\s+contiene)"
    r"\s*(?::|;|[\-–—])?\s*(?P<value>.+)$",
    flags=re.IGNORECASE,
)
_NEGATED_DOSAGE_STATEMENT_RE = re.compile(
    r"^\s*cada\s+(?:\d+(?:[.,]\d+)?\s*)?"
    + _DOSAGE_CONTAINER_PATTERN
    + _DOSAGE_QUALIFIER_PATTERN
    + r"\s+(?:no\s+contiene|sin\b).*$",
    flags=re.IGNORECASE,
)
_ACTIVE_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*•·]|\(?\d+[.)](?!\d))\s*")
_PAGE_NOISE_RE = re.compile(
    r"^(?:pagina\s+)?\d+\s+(?:de|/\s*)\s*\d+$|^pagina\s+\d+$",
    flags=re.IGNORECASE,
)
_DOCUMENT_HEADER_RE = re.compile(
    r"^(?:acta\s+(?:no\.?|n[°º]?)?\s*\d+|"
    r"instituto\s+nacional\s+de\s+vigilancia\s+de\s+medicamentos\s+y\s+alimentos|"
    r"comision\s+revisora|sala\s+especializada\s+de\s+medicamentos|"
    r"ministerio\s+de\s+salud|republica\s+de\s+colombia)",
    flags=re.IGNORECASE,
)
_DOCUMENT_FOOTER_RE = re.compile(
    r"^(?:el\s+formato\s+impreso\s+de\s+este\s+documento\s+es\s+una\s+"
    r"copia\s+no\s+controlada|carrera\s+68\s*d|pbx\s*\(?57|"
    r"avenida\s+carrera\s+\d+|linea\s+gratuita\s+nacional|"
    r"f\d{2}\s*[-–—]?\s*p[mn]\d{2}\b|bogota\s*,?\s*d\.?\s*c\.?\b)",
    flags=re.IGNORECASE,
)
_INLINE_DOCUMENT_FOOTER_RE = re.compile(
    r"(?:\s+|(?<=\d))(?:el\s*formato\s*impreso\s*de\s*este\s*documento\s*"
    r"es\s*una\s*copia\s*no\s*controlada|carrera\s*68\s*d\b|pbx\s*\(?57|"
    r"www\.invima\.gov\.co\b|pagina\s+\d+\s+(?:de|/)\s*\d+\b)",
    flags=re.IGNORECASE,
)

_FIELDS = tuple(field for field, _ in _FIELD_PATTERNS)
_IDENTITY_FIELDS = {"producto", "expediente", "radicado"}
_NARRATIVE_FIELDS = {"solicitud", "concepto"}

_SPANISH_MONTHS = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}

_SESSION_DATE_RE = re.compile(
    r"\b(?:fecha(?:\s+de\s+(?:la\s+)?(?:sesion|reunion))?|"
    r"sesion(?:\s+(?:realizada|celebrada))?)\s*(?::|-)?\s*"
    r"(?P<day>\d{1,2})\s+(?:de\s+)?"
    r"(?P<month>enero|febrero|marzo|abril|mayo|junio|julio|agosto|"
    r"septiembre|setiembre|octubre|noviembre|diciembre)\s+(?:de\s+)?"
    r"(?P<year>20\d{2})\b",
    flags=re.IGNORECASE,
)

_SESSION_NUMERIC_DATE_RE = re.compile(
    r"\b(?:fecha(?:\s+de\s+(?:la\s+)?(?:sesion|reunion))?|"
    r"sesion(?:\s+(?:realizada|celebrada))?)\s*(?::|-)?\s*"
    r"(?P<day>\d{1,2})[/-](?P<month>\d{1,2})[/-](?P<year>20\d{2})\b",
    flags=re.IGNORECASE,
)

_REQUEST_TYPES: tuple[tuple[str, str], ...] = (
    ("renovacion_registro", r"\brenovacion(?:\s+del)?\s+registro\s+sanitario\b"),
    ("modificacion_registro", r"\bmodificacion(?:\s+del)?\s+registro\s+sanitario\b"),
    ("evaluacion_farmacologica", r"\bevaluacion\s+farmacologica\b"),
    ("registro_sanitario", r"\b(?:nuevo\s+)?registro\s+sanitario\b"),
    (
        "indicaciones",
        r"\b(?:nueva|ampliacion|modificacion|inclusion|cambio)\s+de\s+"
        r"indicacion(?:es)?\b|\bindicacion(?:es)?\b",
    ),
    (
        "informacion_prescribir",
        r"\binformacion\s+(?:para|de)\s+prescribir\b",
    ),
    ("cambio_fabricante", r"\bcambio\s+(?:de\s+)?fabricante\b"),
    ("cambio_titular", r"\bcambio\s+(?:de\s+)?titular\b"),
    ("recurso_reposicion", r"\brecurso\s+de\s+reposicion\b"),
    ("cancelacion", r"\bcancelacion(?:\s+voluntaria)?\b"),
)


def _without_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(character for character in normalized if not unicodedata.combining(character))


def _normalized(text: str | None) -> str:
    if not text:
        return ""
    value = _without_accents(text).lower()
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def _clean_value(text: str) -> str:
    value = re.sub(r"\s+", " ", text).strip()
    value = re.sub(r"^[\s:;,.\-–—]+", "", value)
    return value.strip()


def _clean_field_value(field: str, text: str) -> str:
    """Limpia un valor sin adivinar datos regulatorios ausentes.

    Algunos PDF historicos unen el pie institucional a la ultima celda de una
    tabla. Solo se recorta ese repertorio cerrado y solo en campos de identidad;
    el texto narrativo de Solicitud/Concepto se conserva literalmente.
    """

    value = _clean_value(text)
    if field not in {"interesado", "expediente", "radicado"} or not value:
        return value
    footer = _INLINE_DOCUMENT_FOOTER_RE.search(value)
    if footer:
        value = _clean_value(value[: footer.start()])
    return value


def normalize_request_type(request_text: str | None) -> str | None:
    """Clasifica conservadoramente el tipo general de solicitud."""

    comparable = _normalized(request_text)
    if not comparable:
        return None
    for code, pattern in _REQUEST_TYPES:
        if re.search(pattern, comparable, flags=re.IGNORECASE):
            return code
    return "otra_solicitud"


def _extract_session_date_capture(
    pages: Sequence[Mapping[str, object]],
    *,
    maximum_pages: int = 8,
) -> tuple[str | None, str | None, _CapturedPart | None]:
    """Extrae una fecha únicamente cuando está rotulada como sesión/reunión.

    No usa fechas aisladas para evitar confundir la fecha de publicación, un
    radicado o un antecedente con la fecha de la sesión del acta.
    """

    for page_index, page_data in enumerate(
        list(pages)[: max(1, maximum_pages)], start=1
    ):
        try:
            page_number = int(page_data.get("page", page_index))
        except (TypeError, ValueError):
            page_number = page_index
        raw_text = page_data.get("text", "")
        text = raw_text if isinstance(raw_text, str) else str(raw_text or "")
        comparable = _without_accents(text)
        for pattern in (_SESSION_DATE_RE, _SESSION_NUMERIC_DATE_RE):
            match = pattern.search(comparable)
            if not match:
                continue
            try:
                day = int(match.group("day"))
                raw_month = match.group("month")
                month = (
                    int(raw_month)
                    if raw_month.isdigit()
                    else _SPANISH_MONTHS[raw_month.lower()]
                )
                year = int(match.group("year"))
                from datetime import date

                normalized_date = date(year, month, day).isoformat()
            except (KeyError, TypeError, ValueError):
                continue
            original = " ".join(text[match.start() : match.end()].split())
            line_start = text.rfind("\n", 0, match.start()) + 1
            line_end = text.find("\n", match.end())
            if line_end < 0:
                line_end = len(text)
            fragment = _clean_value(text[line_start:line_end]) or original
            return (
                normalized_date,
                original,
                _CapturedPart(
                    value=original,
                    page=max(1, page_number),
                    fragment=fragment[:1000],
                    method="session_date_label",
                ),
            )
    return None, None, None


def extract_session_date(
    pages: Sequence[Mapping[str, object]],
    *,
    maximum_pages: int = 8,
) -> tuple[str | None, str | None]:
    """Extrae fecha normalizada y su texto literal rotulado."""

    normalized, original, _capture = _extract_session_date_capture(
        pages, maximum_pages=maximum_pages
    )
    return normalized, original


def _field_from_match(match: re.Match[str]) -> str | None:
    for index, (field, _) in enumerate(_FIELD_PATTERNS):
        if match.group(f"f{index}") is not None:
            return field
    return None


def _method_from_match(match: re.Match[str]) -> str:
    label = _normalized(match.group(0))
    if "composicion" in label:
        return "composition_label"
    return "explicit_label"


def _labeled_segments(
    line: str,
) -> tuple[str, list[tuple[str | None, str, str]]]:
    """Separa el prefijo libre y los pares (campo, valor) de una linea."""

    comparable = _without_accents(line)
    matches = list(_LABEL_RE.finditer(comparable))
    if not matches:
        if _NEGATED_DOSAGE_STATEMENT_RE.fullmatch(comparable):
            # Es información de ausencia/excipientes, no un ingrediente activo
            # y tampoco debe prolongar una captura de composición anterior.
            return "", [(None, "", "explicit_stop")]
        dosage_match = _DOSAGE_STATEMENT_RE.fullmatch(comparable)
        if dosage_match:
            return "", [
                (
                    "principio_activo",
                    line[dosage_match.start("value") : dosage_match.end("value")],
                    "dosage_statement",
                )
            ]
        bare_match = _BARE_LABEL_RE.fullmatch(comparable.strip())
        if bare_match:
            return "", [
                (
                    _field_from_match(bare_match),
                    "",
                    _method_from_match(bare_match),
                )
            ]
        return line, []

    prefix = line[: matches[0].start()]
    segments: list[tuple[str | None, str, str]] = []
    for index, match in enumerate(matches):
        value_end = matches[index + 1].start() if index + 1 < len(matches) else len(line)
        segments.append(
            (
                _field_from_match(match),
                line[match.end() : value_end],
                _method_from_match(match),
            )
        )
    return prefix, segments


def _field_from_fragment(fragment: str) -> str | None:
    comparable = _without_accents(fragment)
    match = _LABEL_RE.search(comparable)
    if match:
        return _field_from_match(match)
    if _DOSAGE_STATEMENT_RE.fullmatch(comparable):
        return "principio_activo"
    return None


def _is_noise(line: str, repeated_margins: set[str] | None = None) -> bool:
    comparable = _without_accents(_clean_value(line))
    if not comparable:
        return True
    if repeated_margins and _normalized(comparable) in repeated_margins:
        return True
    if _PAGE_NOISE_RE.fullmatch(comparable):
        return True
    if _DOCUMENT_FOOTER_RE.match(comparable):
        return True
    if _DOCUMENT_HEADER_RE.match(comparable) and ":" not in comparable:
        return True
    return comparable.startswith(("www.invima.gov.co", "codigo:", "direccion:"))


def _detect_repeated_margins(
    pages: Sequence[Mapping[str, object]],
) -> set[str]:
    """Detecta encabezados/pies repetidos sin eliminar rotulos regulatorios."""

    if len(pages) < 3:
        return set()
    occurrences: dict[str, set[int]] = {}
    for page_index, page_data in enumerate(pages):
        raw_text = page_data.get("text", "")
        text = raw_text if isinstance(raw_text, str) else str(raw_text or "")
        lines = [_clean_value(line) for line in text.splitlines()]
        lines = [line for line in lines if line]
        margins = [*lines[:3], *lines[-3:]]
        for line in margins:
            comparable = _without_accents(line)
            if _LABEL_RE.search(comparable) or _DOSAGE_STATEMENT_RE.fullmatch(comparable):
                continue
            key = _normalized(line)
            if len(key) < 8:
                continue
            occurrences.setdefault(key, set()).add(page_index)
    threshold = max(3, (len(pages) + 1) // 2)
    return {
        line
        for line, page_indexes in occurrences.items()
        if len(page_indexes) >= threshold
    }


def _is_heading_title(title: str | None, *, explicit_numeral: bool) -> bool:
    if not title:
        return explicit_numeral
    normalized = _normalized(title)
    if any(
        token in normalized
        for token in (
            "evaluacion",
            "medicamento",
            "producto",
            "solicitud",
            "registro sanitario",
            "concepto",
            "recurso",
        )
    ):
        return True
    letters = [character for character in title if character.isalpha()]
    uppercase = [character for character in letters if character.isupper()]
    return bool(letters) and len(uppercase) / len(letters) >= 0.75


def _section_heading(
    line: str,
    *,
    has_active_record: bool,
) -> tuple[str, str | None] | None:
    match = _SECTION_HEADING_RE.fullmatch(line)
    if not match:
        return None
    explicit_numeral = _normalized(line).startswith("numeral ")
    numeral = match.group("numeral").rstrip(".)")
    title = _clean_value(match.group("title") or "") or None
    if title is None and not explicit_numeral:
        if has_active_record or "." not in numeral:
            return None
    elif not _is_heading_title(title, explicit_numeral=explicit_numeral):
        return None
    # Dentro de un concepto, "1. Presentar..." es una instruccion enumerada,
    # no una nueva decision. Los numerales de un solo nivel solo son seguros si
    # son explicitos o su titulo luce como un encabezado en mayusculas.
    if has_active_record and numeral.isdigit() and not explicit_numeral:
        return None
    return numeral, title


def _append_value(values: dict[str, str], field: str, value: str) -> None:
    clean = _clean_field_value(field, value)
    if not clean:
        return
    previous = values.get(field)
    if not previous:
        values[field] = clean
        return

    previous_normalized = _normalized(previous)
    clean_normalized = _normalized(clean)
    if clean_normalized == previous_normalized:
        if len(clean) > len(previous):
            values[field] = clean
        return
    if clean_normalized and clean_normalized in previous_normalized:
        return
    if previous_normalized and previous_normalized in clean_normalized:
        values[field] = clean
        return
    values[field] = f"{previous} {clean}"


_METHOD_CONFIDENCE = {
    "explicit_label": 0.98,
    "composition_label": 0.82,
    "dosage_statement": 0.78,
    "numbered_heading": 0.95,
    "numbered_heading_product": 0.90,
    "session_date_label": 0.98,
    "request_type_inference": 0.85,
    "outcome_inference": 0.85,
    "page_span": 1.0,
}


def _product_candidate_from_heading(
    numeral: str | None,
    title: str | None,
) -> str | None:
    """Usa titulos de numerales hoja solo cuando no parecen categorias."""

    if not numeral or not title or not numeral[0].isdigit():
        return None
    if len(numeral.split(".")) < 3:
        return None
    comparable = _normalized(title)
    category_tokens = (
        "evaluacion",
        "solicitud",
        "concepto",
        "registro sanitario",
        "modificacion",
        "informacion para prescribir",
        "indicaciones",
        "recursos",
        "temas varios",
    )
    if not comparable or any(token in comparable for token in category_tokens):
        return None
    return _clean_value(title) or None


def _capture_part(
    captures: dict[str, list[_CapturedPart]],
    *,
    field: str,
    value: str,
    page: int,
    fragment: str,
    method: str,
) -> None:
    clean = _clean_field_value(field, value)
    if not clean:
        return
    normalized = _normalized(clean)
    if any(
        item.page == page and _normalized(item.value) == normalized
        for item in captures.get(field, ())
    ):
        return
    captures.setdefault(field, []).append(
        _CapturedPart(
            value=clean,
            page=max(1, int(page)),
            fragment=_clean_value(fragment)[:1000],
            method=method,
        )
    )


def _canonical_field_value(value: str | None) -> str | None:
    if not value:
        return None
    clean = unicodedata.normalize("NFKC", value)
    clean = " ".join(clean.split()).strip(" ;,.-")
    return clean or None


def _active_ingredients_from_captures(
    value: str | None,
    captures: Mapping[str, Sequence[_CapturedPart]],
) -> tuple[tuple[str, str, str, _CapturedPart | None], ...]:
    result: list[tuple[str, str, str, _CapturedPart | None]] = []
    seen: set[str] = set()
    source_parts = list(captures.get("principio_activo", ()))
    if not value:
        return ()
    if source_parts:
        methods = {part.method for part in source_parts}
        method = (
            "composition_label"
            if "composition_label" in methods
            else "dosage_statement"
            if "dosage_statement" in methods
            else "numbered_heading_product"
            if "numbered_heading_product" in methods
            else "explicit_label"
        )
        combined_part = _CapturedPart(
            value=value,
            page=min(part.page for part in source_parts),
            fragment="\n".join(
                dict.fromkeys(part.fragment for part in source_parts if part.fragment)
            )[:2000],
            method=method,
            page_final=max(part.page for part in source_parts),
        )
    else:
        combined_part = _CapturedPart(
            value=value,
            page=1,
            fragment=value,
            method="explicit_label",
        )
    bullet_parts = [
        part for part in source_parts if _ACTIVE_LIST_ITEM_RE.match(part.value)
    ]
    explicitly_labeled_parts = [
        part
        for part in source_parts
        if _field_from_fragment(part.fragment) == "principio_activo"
    ]
    if len(bullet_parts) >= 2:
        parse_sources = [(part.value, part) for part in bullet_parts]
    elif len(explicitly_labeled_parts) >= 2:
        parse_sources = [(part.value, part) for part in explicitly_labeled_parts]
    else:
        parse_sources = [(value, combined_part)]

    for source_value, source_part in parse_sources:
        for ingredient in parse_active_ingredients(source_value):
            if ingredient.normalized in seen:
                continue
            seen.add(ingredient.normalized)
            result.append(
                (
                    ingredient.literal,
                    ingredient.normalized,
                    ingredient.canonical,
                    source_part,
                )
            )
    return tuple(result)


def _build_field_evidence(
    *,
    cleaned: Mapping[str, str | None],
    captures: Mapping[str, Sequence[_CapturedPart]],
    active_ingredients: Sequence[tuple[str, str, str, _CapturedPart | None]],
    numeral: str | None,
    numeral_title: str | None,
    numeral_page: int | None,
    numeral_fragment: str | None,
    session_date: str | None,
    session_date_raw: str | None,
    session_date_part: _CapturedPart | None,
    request_type: str | None,
    outcome: str,
    page: int,
    end_page: int,
) -> tuple[FieldEvidence, ...]:
    evidences: list[FieldEvidence] = []
    for field in _FIELDS:
        if field == "principio_activo":
            continue
        literal = cleaned.get(field)
        field_parts = list(captures.get(field, ()))
        if not literal or not field_parts:
            continue
        methods = {part.method for part in field_parts}
        method = (
            "composition_label"
            if "composition_label" in methods
            else "dosage_statement"
            if "dosage_statement" in methods
            else "numbered_heading_product"
            if "numbered_heading_product" in methods
            else "explicit_label"
        )
        evidences.append(
            FieldEvidence(
                campo=field,
                valor_literal=literal,
                valor_normalizado=_normalized(literal) or None,
                valor_canonico=_canonical_field_value(literal),
                pagina=min(part.page for part in field_parts),
                pagina_final=max(part.page for part in field_parts),
                fragmento="\n".join(
                    dict.fromkeys(part.fragment for part in field_parts if part.fragment)
                )[:2000],
                metodo=method,
                confianza=_METHOD_CONFIDENCE[method],
            )
        )

    for ordinal, (literal, normalized, canonical, part) in enumerate(
        active_ingredients,
        start=1,
    ):
        method = part.method if part else "explicit_label"
        evidence_page = part.page if part else page
        evidences.append(
            FieldEvidence(
                campo="principio_activo",
                valor_literal=literal,
                valor_normalizado=normalized,
                valor_canonico=canonical,
                pagina=evidence_page,
                pagina_final=(part.page_final or evidence_page) if part else evidence_page,
                fragmento=(part.fragment if part else literal)[:1000],
                metodo=method,
                confianza=_METHOD_CONFIDENCE.get(method, 0.75),
                ordinal=ordinal,
            )
        )

    if numeral:
        evidences.append(
            FieldEvidence(
                campo="numeral",
                valor_literal=numeral,
                valor_normalizado=_normalized(numeral) or None,
                valor_canonico=numeral,
                pagina=numeral_page or page,
                pagina_final=numeral_page or page,
                fragmento=(numeral_fragment or " ".join(
                    item for item in (numeral, numeral_title) if item
                ))[:1000],
                metodo="numbered_heading",
                confianza=_METHOD_CONFIDENCE["numbered_heading"],
            )
        )
    if numeral_title:
        evidences.append(
            FieldEvidence(
                campo="titulo_numeral",
                valor_literal=numeral_title,
                valor_normalizado=_normalized(numeral_title) or None,
                valor_canonico=_canonical_field_value(numeral_title),
                pagina=numeral_page or page,
                pagina_final=numeral_page or page,
                fragmento=(
                    numeral_fragment
                    or " ".join(item for item in (numeral, numeral_title) if item)
                )[:1000],
                metodo="numbered_heading",
                confianza=_METHOD_CONFIDENCE["numbered_heading"],
            )
        )

    if session_date and session_date_raw:
        date_part = session_date_part or _CapturedPart(
            value=session_date_raw,
            page=page,
            fragment=session_date_raw,
            method="session_date_label",
        )
        evidences.extend(
            (
                FieldEvidence(
                    campo="fecha_sesion",
                    valor_literal=session_date_raw,
                    valor_normalizado=session_date,
                    valor_canonico=session_date,
                    pagina=date_part.page,
                    pagina_final=date_part.page_final or date_part.page,
                    fragmento=date_part.fragment[:1000],
                    metodo="session_date_label",
                    confianza=_METHOD_CONFIDENCE["session_date_label"],
                ),
                FieldEvidence(
                    campo="fecha_sesion_original",
                    valor_literal=session_date_raw,
                    valor_normalizado=_normalized(session_date_raw) or None,
                    valor_canonico=_canonical_field_value(session_date_raw),
                    pagina=date_part.page,
                    pagina_final=date_part.page_final or date_part.page,
                    fragmento=date_part.fragment[:1000],
                    metodo="session_date_label",
                    confianza=_METHOD_CONFIDENCE["session_date_label"],
                ),
            )
        )

    request_parts = list(captures.get("solicitud", ()))
    if request_type and cleaned.get("solicitud") and request_parts:
        request_confidence = (
            _METHOD_CONFIDENCE["request_type_inference"]
            if request_type != "otra_solicitud"
            else 0.45
        )
        evidences.append(
            FieldEvidence(
                campo="tipo_solicitud",
                valor_literal=cleaned["solicitud"],
                valor_normalizado=request_type,
                valor_canonico=request_type,
                pagina=min(part.page for part in request_parts),
                pagina_final=max(part.page for part in request_parts),
                fragmento="\n".join(
                    dict.fromkeys(
                        part.fragment for part in request_parts if part.fragment
                    )
                )[:2000],
                metodo="request_type_inference",
                confianza=request_confidence,
            )
        )

    concept_parts = list(captures.get("concepto", ()))
    if cleaned.get("concepto") and concept_parts:
        outcome_confidence = (
            _METHOD_CONFIDENCE["outcome_inference"]
            if outcome != RESULT_UNCLASSIFIED
            else 0.40
        )
        evidences.append(
            FieldEvidence(
                campo="resultado_normalizado",
                valor_literal=cleaned["concepto"],
                valor_normalizado=outcome,
                valor_canonico=outcome,
                pagina=min(part.page for part in concept_parts),
                pagina_final=max(part.page for part in concept_parts),
                fragmento="\n".join(
                    dict.fromkeys(
                        part.fragment for part in concept_parts if part.fragment
                    )
                )[:2000],
                metodo="outcome_inference",
                confianza=outcome_confidence,
            )
        )

    range_literal = str(page) if page == end_page else f"{page}-{end_page}"
    evidences.append(
        FieldEvidence(
            campo="rango_paginas",
            valor_literal=range_literal,
            valor_normalizado=range_literal,
            valor_canonico=range_literal,
            pagina=page,
            pagina_final=end_page,
            fragmento=f"Paginas {range_literal}",
            metodo="page_span",
            confianza=_METHOD_CONFIDENCE["page_span"],
        )
    )
    return normalize_field_evidence_ordinals(evidences)


def normalize_regulatory_result(concept: str | None) -> str:
    """Clasifica un concepto mediante reglas conservadoras y ordenadas."""

    text = _normalized(concept)
    if not text:
        return RESULT_UNCLASSIFIED

    if re.search(r"\b(?:desistir|desistido|desistimiento)\b", text):
        return RESULT_WITHDRAWN
    if re.search(r"\b(?:archivar|archivado|archivo\s+del\s+tramite)\b", text):
        return RESULT_ARCHIVED
    if re.search(
        r"\b(?:no\s+(?:es\s+)?procedente\s+(?:aprobar|aceptar)|"
        r"no\s+aprobar|no\s+(?:se\s+)?(?:aprueba|acepta)|"
        r"negar|niega|nego|negado|denegar|deniega|denego|denegado|"
        r"rechazar|rechazado)\b",
        text,
    ):
        return RESULT_DENIED
    if re.search(
        r"\b(?:concepto|evaluacion|respuesta|decision)?\s*"
        r"(?:es\s+)?(?:no\s+favorable|desfavorable)\b",
        text,
    ):
        return RESULT_UNFAVORABLE
    requirement_text = re.sub(
        r"\b(?:no\s+se\s+requiere|no\s+requiere|sin\s+requerir)\b",
        "",
        text,
    )
    if re.search(
        r"\b(?:se\s+requiere|requerir|requerido|requiere\s+al|"
        r"requiere\s+(?:allegar|aportar|presentar|aclarar|subsanar)|"
        r"debera\s+(?:allegar|aportar|presentar|aclarar|subsanar)|"
        r"debe\s+(?:allegar|aportar|presentar|aclarar|subsanar))\b",
        requirement_text,
    ):
        return RESULT_REQUIRED
    if re.search(
        r"\b(?:es\s+procedente\s+(?:aprobar|aceptar)|"
        r"aprobar|aprueba|aprobo|aprobado|se\s+acepta|aceptado)\b",
        text,
    ):
        return RESULT_APPROVED
    if re.search(
        r"\b(?:concepto|evaluacion|respuesta|decision)?\s*"
        r"(?:es\s+)?favorable(?:mente)?\b",
        text,
    ):
        return RESULT_FAVORABLE
    return RESULT_UNCLASSIFIED


def _has_enough_evidence(values: Mapping[str, str]) -> bool:
    populated = {field for field in _FIELDS if values.get(field)}
    if populated & _NARRATIVE_FIELDS and len(populated) >= 2:
        return True
    return len(populated) >= 3 and bool(populated & _IDENTITY_FIELDS)


def _build_decision(
    values: Mapping[str, str],
    page: int,
    end_page: int,
    *,
    captures: Mapping[str, Sequence[_CapturedPart]] | None = None,
    numeral: str | None = None,
    numeral_title: str | None = None,
    numeral_page: int | None = None,
    numeral_fragment: str | None = None,
    heading_product: str | None = None,
    session_date: str | None = None,
    session_date_raw: str | None = None,
    session_date_part: _CapturedPart | None = None,
) -> RegulatoryRecord | None:
    working_values = dict(values)
    working_captures = {
        field: list(parts) for field, parts in (captures or {}).items()
    }
    if heading_product and not working_values.get("producto"):
        working_values["producto"] = heading_product
        working_captures.setdefault("producto", []).append(
            _CapturedPart(
                value=heading_product,
                page=numeral_page or page,
                fragment=numeral_fragment or heading_product,
                method="numbered_heading_product",
            )
        )
    if not _has_enough_evidence(working_values):
        return None
    cleaned = {
        field: _clean_value(working_values.get(field, "")) or None
        for field in _FIELDS
    }
    captures = working_captures
    active_ingredients = _active_ingredients_from_captures(
        cleaned["principio_activo"],
        captures,
    )
    if active_ingredients:
        cleaned["principio_activo"] = "; ".join(
            ingredient[2] for ingredient in active_ingredients
        )
    request_type = normalize_request_type(cleaned["solicitud"])
    outcome = normalize_regulatory_result(cleaned["concepto"])
    evidences = _build_field_evidence(
        cleaned=cleaned,
        captures=captures,
        active_ingredients=active_ingredients,
        numeral=numeral,
        numeral_title=numeral_title,
        numeral_page=numeral_page,
        numeral_fragment=numeral_fragment,
        session_date=session_date,
        session_date_raw=session_date_raw,
        session_date_part=session_date_part,
        request_type=request_type,
        outcome=outcome,
        page=max(1, int(page)),
        end_page=max(int(page), int(end_page)),
    )
    return RegulatoryRecord(
        producto=cleaned["producto"],
        principio_activo=cleaned["principio_activo"],
        interesado=cleaned["interesado"],
        expediente=cleaned["expediente"],
        radicado=cleaned["radicado"],
        solicitud=cleaned["solicitud"],
        concepto=cleaned["concepto"],
        resultado_normalizado=outcome,
        pagina=max(1, int(page)),
        pagina_final=max(int(page), int(end_page)),
        numeral=numeral,
        titulo_numeral=numeral_title,
        fecha_sesion=session_date,
        fecha_sesion_original=session_date_raw,
        tipo_solicitud=request_type,
        principios_activos=tuple(item[0] for item in active_ingredients),
        principios_activos_normalizados=tuple(item[1] for item in active_ingredients),
        principios_activos_canonicos=tuple(item[2] for item in active_ingredients),
        evidencias_campos=evidences,
    )


def _should_start_new_record(
    values: Mapping[str, str],
    field: str,
    incoming: str,
) -> bool:
    existing = values.get(field)
    if not existing or not _has_enough_evidence(values):
        return False

    if values.get("concepto") and field in _IDENTITY_FIELDS:
        return True

    existing_normalized = _normalized(existing)
    incoming_normalized = _normalized(incoming)
    if field in _IDENTITY_FIELDS and incoming_normalized:
        return existing_normalized != incoming_normalized
    return field in _NARRATIVE_FIELDS and bool(values.get("concepto"))


def _identity_key(decision: RegulatoryRecord) -> tuple[str, ...] | None:
    if decision.radicado:
        return ("radicado", _normalized(decision.radicado))
    if decision.expediente and decision.producto:
        return (
            "expediente_producto",
            _normalized(decision.expediente),
            _normalized(decision.producto),
        )
    return None


def _compatible_duplicates(first: RegulatoryRecord, second: RegulatoryRecord) -> bool:
    if _identity_key(first) is None or _identity_key(first) != _identity_key(second):
        return False
    for field in ("producto", "principio_activo", "interesado", "solicitud", "concepto"):
        left = _normalized(getattr(first, field))
        right = _normalized(getattr(second, field))
        if left and right and left != right and left not in right and right not in left:
            return False
    return True


def _merge_duplicate(first: RegulatoryRecord, second: RegulatoryRecord) -> RegulatoryRecord:
    merged: dict[str, str | None] = {}
    for field in _FIELDS:
        left = getattr(first, field)
        right = getattr(second, field)
        if not left:
            merged[field] = right
        elif not right:
            merged[field] = left
        else:
            merged[field] = right if len(right) > len(left) else left
    active_items: list[tuple[str, str, str]] = []
    active_seen: set[str] = set()
    for literal, normalized, canonical in (
        *zip(
            first.principios_activos,
            first.principios_activos_normalizados,
            first.principios_activos_canonicos,
        ),
        *zip(
            second.principios_activos,
            second.principios_activos_normalizados,
            second.principios_activos_canonicos,
        ),
    ):
        if normalized in active_seen:
            continue
        active_seen.add(normalized)
        active_items.append((literal, normalized, canonical))

    merged_evidences = normalize_field_evidence_ordinals(
        (*first.evidencias_campos, *second.evidencias_campos)
    )

    return RegulatoryRecord(
        producto=merged["producto"],
        principio_activo=merged["principio_activo"],
        interesado=merged["interesado"],
        expediente=merged["expediente"],
        radicado=merged["radicado"],
        solicitud=merged["solicitud"],
        concepto=merged["concepto"],
        resultado_normalizado=normalize_regulatory_result(merged["concepto"]),
        pagina=min(first.pagina, second.pagina),
        pagina_final=max(
            first.pagina_final or first.pagina,
            second.pagina_final or second.pagina,
        ),
        numeral=first.numeral or second.numeral,
        titulo_numeral=first.titulo_numeral or second.titulo_numeral,
        fecha_sesion=first.fecha_sesion or second.fecha_sesion,
        fecha_sesion_original=(
            first.fecha_sesion_original or second.fecha_sesion_original
        ),
        tipo_solicitud=(
            first.tipo_solicitud
            if first.tipo_solicitud not in {None, "otra_solicitud"}
            else second.tipo_solicitud
        ),
        principios_activos=tuple(item[0] for item in active_items),
        principios_activos_normalizados=tuple(item[1] for item in active_items),
        principios_activos_canonicos=tuple(item[2] for item in active_items),
        evidencias_campos=merged_evidences,
    )


def _deduplicate(decisions: Sequence[RegulatoryRecord]) -> list[RegulatoryRecord]:
    unique: list[RegulatoryRecord] = []
    exact_seen: set[tuple[str, ...]] = set()
    for decision in decisions:
        exact_key = tuple(
            _normalized(getattr(decision, field))
            for field in _FIELDS
        )
        if exact_key in exact_seen:
            continue

        merged = False
        for index, existing in enumerate(unique):
            if _compatible_duplicates(existing, decision):
                unique[index] = _merge_duplicate(existing, decision)
                merged = True
                break
        if not merged:
            unique.append(decision)
        exact_seen.add(exact_key)
    return unique


def extract_regulatory_records(
    pages: Sequence[Mapping[str, object]],
) -> list[RegulatoryRecord]:
    """Extrae decisiones desde paginas ``{"page": int, "text": str}``.

    Las paginas se procesan en el orden recibido para no perder continuaciones.
    Un bloque incompleto se omite cuando no hay evidencia suficiente para afirmar
    que corresponde a una decision regulatoria.
    """

    decisions: list[RegulatoryRecord] = []
    values: dict[str, str] = {}
    captures: dict[str, list[_CapturedPart]] = {}
    active_field: str | None = None
    active_method = "explicit_label"
    start_page = 1
    record_end_page = 1
    current_numeral: str | None = None
    current_numeral_title: str | None = None
    current_numeral_page: int | None = None
    current_numeral_fragment: str | None = None
    record_numeral: str | None = None
    record_numeral_title: str | None = None
    record_numeral_page: int | None = None
    record_numeral_fragment: str | None = None
    session_date, session_date_raw, session_date_part = _extract_session_date_capture(
        pages
    )
    repeated_margins = _detect_repeated_margins(pages)

    def flush() -> None:
        nonlocal values, captures, active_field, active_method
        nonlocal start_page, record_end_page
        nonlocal record_numeral, record_numeral_title
        nonlocal record_numeral_page, record_numeral_fragment
        decision = _build_decision(
            values,
            start_page,
            record_end_page,
            captures=captures,
            numeral=record_numeral,
            numeral_title=record_numeral_title,
            numeral_page=record_numeral_page,
            numeral_fragment=record_numeral_fragment,
            heading_product=_product_candidate_from_heading(
                record_numeral,
                record_numeral_title,
            ),
            session_date=session_date,
            session_date_raw=session_date_raw,
            session_date_part=session_date_part,
        )
        if decision is not None:
            decisions.append(decision)
        values = {}
        captures = {}
        active_field = None
        active_method = "explicit_label"
        record_numeral = current_numeral
        record_numeral_title = current_numeral_title
        record_numeral_page = current_numeral_page
        record_numeral_fragment = current_numeral_fragment

    for page_index, page_data in enumerate(pages, start=1):
        try:
            page_number = int(page_data.get("page", page_index))
        except (TypeError, ValueError):
            page_number = page_index
        raw_text = page_data.get("text", "")
        text = raw_text if isinstance(raw_text, str) else str(raw_text or "")

        for raw_line in text.splitlines():
            line = _clean_value(raw_line)
            if _is_noise(line, repeated_margins):
                continue

            comparable_line = _without_accents(line)
            heading_with_content = _HEADING_WITH_CONTENT_RE.fullmatch(comparable_line)
            if heading_with_content:
                if values:
                    flush()
                current_numeral = heading_with_content.group("numeral").rstrip(".)")
                current_numeral_title = None
                current_numeral_page = page_number
                current_numeral_fragment = line
                record_numeral = current_numeral
                record_numeral_title = current_numeral_title
                record_numeral_page = current_numeral_page
                record_numeral_fragment = current_numeral_fragment
                start_page = page_number
                content_start = heading_with_content.start("content")
                line = line[content_start:]
            else:
                section = _section_heading(line, has_active_record=bool(values))
                if section:
                    if values:
                        flush()
                    current_numeral, current_numeral_title = section
                    current_numeral_page = page_number
                    current_numeral_fragment = line
                    record_numeral = current_numeral
                    record_numeral_title = current_numeral_title
                    record_numeral_page = current_numeral_page
                    record_numeral_fragment = current_numeral_fragment
                    start_page = page_number
                    continue

            prefix, segments = _labeled_segments(line)
            if not segments:
                if active_field:
                    _append_value(values, active_field, line)
                    _capture_part(
                        captures,
                        field=active_field,
                        value=line,
                        page=page_number,
                        fragment=line,
                        method=active_method,
                    )
                    record_end_page = page_number
                continue

            if active_field and _clean_value(prefix):
                _append_value(values, active_field, prefix)
                _capture_part(
                    captures,
                    field=active_field,
                    value=prefix,
                    page=page_number,
                    fragment=line,
                    method=active_method,
                )
                record_end_page = page_number

            for field, value, method in segments:
                if field is None:
                    active_field = None
                    active_method = "explicit_label"
                    continue

                if _should_start_new_record(values, field, value):
                    flush()
                    start_page = page_number
                    record_end_page = page_number
                elif not values:
                    start_page = page_number
                    record_end_page = page_number
                    record_numeral = current_numeral
                    record_numeral_title = current_numeral_title
                    record_numeral_page = current_numeral_page
                    record_numeral_fragment = current_numeral_fragment

                active_field = field
                active_method = method
                _append_value(values, field, value)
                if _clean_value(value):
                    _capture_part(
                        captures,
                        field=field,
                        value=value,
                        page=page_number,
                        fragment=line,
                        method=method,
                    )
                    record_end_page = page_number

    flush()
    return _deduplicate(decisions)


def extract_regulatory_dicts(
    pages: Sequence[Mapping[str, object]],
) -> list[dict[str, Any]]:
    """Atajo para consumidores que prefieren diccionarios serializables."""

    return [record.as_dict() for record in extract_regulatory_records(pages)]
