"""Extraccion deterministica de decisiones regulatorias en actas de INVIMA.

El extractor trabaja sobre el texto ya separado por pagina. No intenta interpretar
todo el documento: identifica bloques rotulados (Producto, Expediente, Solicitud,
Concepto, etc.), conserva las continuaciones entre paginas y normaliza solamente
el resultado general de la decision.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any, Mapping, Sequence
import unicodedata


RESULT_APPROVED = "aprobado"
RESULT_DENIED = "negado"
RESULT_REQUIRED = "requerido"
RESULT_WITHDRAWN = "desistido"
RESULT_ARCHIVED = "archivado"
RESULT_FAVORABLE = "favorable"
RESULT_UNFAVORABLE = "no_favorable"
RESULT_UNCLASSIFIED = "sin_clasificar"


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

    @property
    def page(self) -> int:
        """Alias util para integraciones que ya usan el nombre ``page``."""

        return self.pagina

    def as_dict(self) -> dict[str, Any]:
        """Devuelve una representacion compuesta solo por tipos serializables."""

        return asdict(self)


# Los patrones se aplican a una copia sin acentos del texto. El orden importa:
# los rotulos mas especificos deben evaluarse antes que los cortos.
_FIELD_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        "principio_activo",
        r"(?:principio|ingrediente)s?\s+activo(?:s)?",
    ),
    (
        "producto",
        r"(?:nombre\s+(?:comercial\s+)?del\s+)?(?:producto|medicamento)",
    ),
    (
        "interesado",
        r"(?:interesado(?:s)?|solicitante|titular(?:\s+del\s+registro\s+sanitario)?)",
    ),
    (
        "expediente",
        r"(?:(?:n\.?[°º]|n(?:\.?o|ro|umero)?\.?)\s*(?:de\s+)?)?expediente(?:s)?",
    ),
    (
        "radicado",
        r"(?:(?:n\.?[°º]|n(?:\.?o|ro|umero)?\.?)\s*(?:de\s+)?)?radicado(?:s)?",
    ),
    (
        "solicitud",
        r"(?:solicitud(?:\s+del\s+interesado)?|peticion)",
    ),
    (
        "concepto",
        r"(?:concepto(?:\s+(?:de|emitido\s+por)\s+la\s+"
        r"(?:sala(?:\s+especializada)?|comision\s+revisora))?"
        r"|decision|conclusion|resultado)",
    ),
)

_STOP_PATTERNS: tuple[str, ...] = (
    r"(?:forma\s+farmaceutica|concentracion|via\s+de\s+administracion)",
    r"(?:fabricante|importador|acondicionador|pais\s+de\s+origen)",
    r"(?:indicacion(?:es)?|contraindicacion(?:es)?|composicion)",
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
    r"^\s*(?P<numeral>\d+(?:\.\d+){1,7})\.?\s*"
    r"(?P<title>[A-ZÁÉÍÓÚÑ][^:]{0,160})?$"
)
_PAGE_NOISE_RE = re.compile(
    r"^(?:pagina\s+)?\d+\s+(?:de|/\s*)\s*\d+$|^pagina\s+\d+$",
    flags=re.IGNORECASE,
)
_DOCUMENT_HEADER_RE = re.compile(
    r"^(?:acta\s+(?:no\.?|n[°º]?)?\s*\d+|"
    r"instituto\s+nacional\s+de\s+vigilancia\s+de\s+medicamentos\s+y\s+alimentos|"
    r"comision\s+revisora|sala\s+especializada\s+de\s+medicamentos)",
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


def normalize_request_type(request_text: str | None) -> str | None:
    """Clasifica conservadoramente el tipo general de solicitud."""

    comparable = _normalized(request_text)
    if not comparable:
        return None
    for code, pattern in _REQUEST_TYPES:
        if re.search(pattern, comparable, flags=re.IGNORECASE):
            return code
    return "otra_solicitud"


def extract_session_date(
    pages: Sequence[Mapping[str, object]],
    *,
    maximum_pages: int = 8,
) -> tuple[str | None, str | None]:
    """Extrae una fecha únicamente cuando está rotulada como sesión/reunión.

    No usa fechas aisladas para evitar confundir la fecha de publicación, un
    radicado o un antecedente con la fecha de la sesión del acta.
    """

    for page_data in list(pages)[: max(1, maximum_pages)]:
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
            return normalized_date, original
    return None, None


def _field_from_match(match: re.Match[str]) -> str | None:
    for index, (field, _) in enumerate(_FIELD_PATTERNS):
        if match.group(f"f{index}") is not None:
            return field
    return None


def _labeled_segments(line: str) -> tuple[str, list[tuple[str | None, str]]]:
    """Separa el prefijo libre y los pares (campo, valor) de una linea."""

    comparable = _without_accents(line)
    matches = list(_LABEL_RE.finditer(comparable))
    if not matches:
        bare_match = _BARE_LABEL_RE.fullmatch(comparable.strip())
        if bare_match:
            return "", [(_field_from_match(bare_match), "")]
        return line, []

    prefix = line[: matches[0].start()]
    segments: list[tuple[str | None, str]] = []
    for index, match in enumerate(matches):
        value_end = matches[index + 1].start() if index + 1 < len(matches) else len(line)
        segments.append((_field_from_match(match), line[match.end() : value_end]))
    return prefix, segments


def _is_noise(line: str) -> bool:
    comparable = _without_accents(_clean_value(line))
    if not comparable:
        return True
    if _PAGE_NOISE_RE.fullmatch(comparable):
        return True
    if _DOCUMENT_HEADER_RE.match(comparable) and ":" not in comparable:
        return True
    return comparable.startswith(("www.invima.gov.co", "codigo:", "direccion:"))


def _append_value(values: dict[str, str], field: str, value: str) -> None:
    clean = _clean_value(value)
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
    numeral: str | None = None,
    numeral_title: str | None = None,
    session_date: str | None = None,
    session_date_raw: str | None = None,
) -> RegulatoryRecord | None:
    if not _has_enough_evidence(values):
        return None
    cleaned = {field: _clean_value(values.get(field, "")) or None for field in _FIELDS}
    return RegulatoryRecord(
        producto=cleaned["producto"],
        principio_activo=cleaned["principio_activo"],
        interesado=cleaned["interesado"],
        expediente=cleaned["expediente"],
        radicado=cleaned["radicado"],
        solicitud=cleaned["solicitud"],
        concepto=cleaned["concepto"],
        resultado_normalizado=normalize_regulatory_result(cleaned["concepto"]),
        pagina=max(1, int(page)),
        pagina_final=max(int(page), int(end_page)),
        numeral=numeral,
        titulo_numeral=numeral_title,
        fecha_sesion=session_date,
        fecha_sesion_original=session_date_raw,
        tipo_solicitud=normalize_request_type(cleaned["solicitud"]),
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
    active_field: str | None = None
    start_page = 1
    record_end_page = 1
    current_numeral: str | None = None
    current_numeral_title: str | None = None
    record_numeral: str | None = None
    record_numeral_title: str | None = None
    session_date, session_date_raw = extract_session_date(pages)

    def flush() -> None:
        nonlocal values, active_field, start_page, record_end_page
        nonlocal record_numeral, record_numeral_title
        decision = _build_decision(
            values,
            start_page,
            record_end_page,
            numeral=record_numeral,
            numeral_title=record_numeral_title,
            session_date=session_date,
            session_date_raw=session_date_raw,
        )
        if decision is not None:
            decisions.append(decision)
        values = {}
        active_field = None
        record_numeral = current_numeral
        record_numeral_title = current_numeral_title

    for page_index, page_data in enumerate(pages, start=1):
        try:
            page_number = int(page_data.get("page", page_index))
        except (TypeError, ValueError):
            page_number = page_index
        raw_text = page_data.get("text", "")
        text = raw_text if isinstance(raw_text, str) else str(raw_text or "")

        for raw_line in text.splitlines():
            line = _clean_value(raw_line)
            if _is_noise(line):
                continue
            section_match = _SECTION_HEADING_RE.fullmatch(line)
            if section_match and (section_match.group("title") or not values):
                if values:
                    flush()
                current_numeral = section_match.group("numeral")
                current_numeral_title = _clean_value(
                    section_match.group("title") or ""
                ) or None
                record_numeral = current_numeral
                record_numeral_title = current_numeral_title
                start_page = page_number
                continue

            prefix, segments = _labeled_segments(line)
            if not segments:
                if active_field:
                    _append_value(values, active_field, line)
                    record_end_page = page_number
                continue

            if active_field and _clean_value(prefix):
                _append_value(values, active_field, prefix)
                record_end_page = page_number

            for field, value in segments:
                if field is None:
                    active_field = None
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

                active_field = field
                _append_value(values, field, value)
                if _clean_value(value):
                    record_end_page = page_number

    flush()
    return _deduplicate(decisions)


def extract_regulatory_dicts(
    pages: Sequence[Mapping[str, object]],
) -> list[dict[str, Any]]:
    """Atajo para consumidores que prefieren diccionarios serializables."""

    return [record.as_dict() for record in extract_regulatory_records(pages)]
