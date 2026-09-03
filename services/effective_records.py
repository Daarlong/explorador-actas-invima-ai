"""Vista unica y auditable de una ficha regulatoria.

La base conserva los valores producidos por el extractor y el registro CSV
conserva las decisiones humanas. Este modulo es el unico lugar donde se
combinan ambas capas. No escribe en SQLite ni modifica el historial de
revisiones.

El lector de evidencias es deliberadamente tolerante: funciona con las fichas
0.6 y tambien con la salida estructurada del extractor v4 cuando las
evidencias se entregan como objetos, listas o JSON.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, is_dataclass, replace
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from services.text_utils import normalize_text


EFFECTIVE_FIELDS = (
    "numeral",
    "numeral_title",
    "session_date",
    "request_type_code",
    "product_name",
    "active_ingredient",
    "interested_party",
    "expediente",
    "radicado",
    "request_text",
    "concept_text",
    "outcome_code",
    "page_number",
    "end_page_number",
)

PROVENANCE_LABELS = {
    "explicit": "Explícito",
    "inferred": "Inferido",
    "mixed": "Mixto",
    "verified": "Verificado",
    "textual_mention": "Mención textual",
    "legacy_automatic": "Automático anterior",
    "not_extracted": "No extraído",
}

_FIELD_ALIASES = {
    "numeral": "numeral",
    "titulo_numeral": "numeral_title",
    "numeral_title": "numeral_title",
    "fecha_sesion": "session_date",
    # La fecha original es evidencia auxiliar del mismo valor vigente. No se
    # convierte en un segundo campo editable para evitar correcciones dobles.
    "fecha_sesion_original": "session_date",
    "session_date": "session_date",
    "tipo_solicitud": "request_type_code",
    "request_type_code": "request_type_code",
    "producto": "product_name",
    "product_name": "product_name",
    "principio_activo": "active_ingredient",
    "principios_activos": "active_ingredient",
    "active_ingredient": "active_ingredient",
    "interesado": "interested_party",
    "titular": "interested_party",
    "interested_party": "interested_party",
    "expediente": "expediente",
    "radicado": "radicado",
    "solicitud": "request_text",
    "request_text": "request_text",
    "concepto": "concept_text",
    "concept_text": "concept_text",
    "resultado": "outcome_code",
    "resultado_normalizado": "outcome_code",
    "outcome_code": "outcome_code",
    "pagina": "page_number",
    "page_number": "page_number",
    "pagina_final": "end_page_number",
    "end_page_number": "end_page_number",
}

_INFERRED_METHOD_MARKERS = (
    "infer",
    "alias",
    "dictionary",
    "association",
    "propagat",
)


def is_missing(value: object) -> bool:
    """Indica ausencia real sin confundir cero o ``False`` con vacío."""

    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().casefold() in {"", "none", "null"}
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


def display_value(value: object, *, missing_label: str = "No extraído") -> object:
    """Devuelve una representación segura para tablas de la interfaz."""

    if is_missing(value):
        return missing_label
    if isinstance(value, (list, tuple, set)):
        visible = [str(item).strip() for item in value if not is_missing(item)]
        return "; ".join(visible) if visible else missing_label
    return value


def provenance_label(value: object) -> str:
    return PROVENANCE_LABELS.get(str(value or ""), str(value or "No extraído"))


def _as_mapping(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value):
        fields = getattr(value, "__dataclass_fields__", {})
        return {name: getattr(value, name) for name in fields}
    return {}


def _json_value(value: object) -> object:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{":
        return value
    try:
        return json.loads(stripped)
    except (TypeError, ValueError):
        return value


def _canonical_field(value: object) -> str:
    key = normalize_text(str(value or "")).replace(" ", "_")
    return _FIELD_ALIASES.get(key, key if key in EFFECTIVE_FIELDS else "")


def _evidence_field_targets(value: object) -> tuple[str, ...]:
    """Mapea una evidencia a uno o varios campos vigentes.

    ``rango_paginas`` es una sola evidencia física, pero sustenta tanto la
    página inicial como la final. El resto conserva el mapeo histórico 1:1.
    """

    key = normalize_text(str(value or "")).replace(" ", "_")
    if key == "rango_paginas":
        return ("page_number", "end_page_number")
    field = _canonical_field(key)
    return (field,) if field else ()


def _evidence_items(record: Mapping[str, object]) -> list[dict[str, object]]:
    raw: object = None
    for key in (
        "field_evidence",
        "field_evidences",
        "field_evidence_json",
        "evidencias_campos",
        "evidencias_campos_json",
    ):
        if not is_missing(record.get(key)):
            raw = _json_value(record.get(key))
            break
    if raw is None:
        return []
    if isinstance(raw, Mapping):
        expanded: list[dict[str, object]] = []
        for field, evidence in raw.items():
            candidates = evidence if isinstance(evidence, (list, tuple)) else [evidence]
            for candidate in candidates:
                item = _as_mapping(candidate)
                item.setdefault("campo", field)
                expanded.append(item)
        return expanded
    if isinstance(raw, (list, tuple)):
        return [_as_mapping(item) for item in raw if _as_mapping(item)]
    return []


def _inferred_values(record: Mapping[str, object]) -> dict[str, object]:
    for key in ("inferred_values", "inferred_values_json", "valores_inferidos"):
        raw = _json_value(record.get(key))
        if isinstance(raw, Mapping):
            result: dict[str, object] = {}
            for field, value in raw.items():
                canonical = _canonical_field(field)
                if canonical:
                    result[canonical] = value
            return result
    return {}


def _provenance_for_method(method: object) -> str:
    normalized = normalize_text(str(method or "")).replace(" ", "_")
    if any(marker in normalized for marker in _INFERRED_METHOD_MARKERS):
        return "inferred"
    return "explicit"


def _float_or_none(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, number))


def _metadata_from_evidence(record: Mapping[str, object]) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    existing = _json_value(record.get("field_metadata"))
    if isinstance(existing, Mapping):
        for raw_field, raw_metadata in existing.items():
            field = _canonical_field(raw_field)
            metadata = _as_mapping(raw_metadata)
            if field and metadata:
                result[field] = metadata
    provenance_map = _json_value(record.get("field_provenance"))
    confidence_map = _json_value(record.get("field_confidence"))
    if isinstance(provenance_map, Mapping):
        for raw_field, provenance in provenance_map.items():
            field = _canonical_field(raw_field)
            if field:
                result.setdefault(field, {})["provenance"] = provenance
    if isinstance(confidence_map, Mapping):
        for raw_field, confidence in confidence_map.items():
            field = _canonical_field(raw_field)
            if field:
                result.setdefault(field, {})["confidence"] = _float_or_none(confidence)
    evidence_by_field: dict[str, list[dict[str, object]]] = {}
    for item in _evidence_items(record):
        raw_field = item.get("campo") or item.get("field_name")
        target_fields = _evidence_field_targets(raw_field)
        if not target_fields:
            continue
        method = item.get("metodo") or item.get("extraction_method") or item.get("method")
        confidence = _float_or_none(item.get("confianza", item.get("confidence")))
        general_value = (
            item.get("valor_canonico")
            or item.get("canonical_value")
            or item.get("valor_normalizado")
            or item.get("normalized_value")
            or item.get("valor_literal")
            or item.get("literal_value")
            or item.get("value")
        )
        source_field = normalize_text(str(raw_field or "")).replace(" ", "_")
        for field in target_fields:
            value = general_value
            if source_field == "rango_paginas":
                value = (
                    item.get("pagina", item.get("page_number"))
                    if field == "page_number"
                    else item.get("pagina_final", item.get("end_page_number"))
                )
            candidate = {
                "provenance": _provenance_for_method(method),
                "confidence": confidence,
                "method": str(method or ""),
                "page": item.get("pagina", item.get("page_number")),
                "end_page": item.get("pagina_final", item.get("end_page_number")),
                "fragment": str(
                    item.get("fragmento") or item.get("evidence_text") or ""
                ),
                "literal_value": item.get(
                    "valor_literal", item.get("literal_value")
                ),
                "normalized_value": item.get(
                    "valor_normalizado", item.get("normalized_value")
                ),
                "canonical_value": item.get(
                    "valor_canonico", item.get("canonical_value")
                ),
                "source_field": source_field,
                "value": value,
            }
            evidence_by_field.setdefault(field, []).append(candidate)
    for field, candidates in evidence_by_field.items():
        current = result.get(field)
        if current and current.get("provenance") == "verified":
            continue
        primary = max(
            candidates,
            key=lambda item: (
                _float_or_none(item.get("confidence"))
                if _float_or_none(item.get("confidence")) is not None
                else -1.0
            ),
        )
        confidences = [
            value
            for item in candidates
            for value in [_float_or_none(item.get("confidence"))]
            if value is not None
        ]
        aggregate = dict(primary)
        # Para combinaciones, la ficha completa es tan confiable como el
        # ingrediente menos confiable; mostrar el máximo sobreafirmaría calidad.
        aggregate["confidence"] = min(confidences) if confidences else None
        candidate_provenances = {
            str(item.get("provenance") or "") for item in candidates
        }
        aggregate["provenance"] = (
            next(iter(candidate_provenances))
            if len(candidate_provenances) == 1
            else "mixed"
        )
        aggregate["evidence_count"] = len(candidates)
        result[field] = aggregate
    return result


def _event_value(event: object, name: str, default: object = "") -> object:
    if isinstance(event, Mapping):
        return event.get(name, default)
    return getattr(event, name, default)


def effective_record(
    record: Mapping[str, object],
    event: object | None = None,
) -> dict[str, object]:
    """Combina extracción, inferencias y la revisión humana vigente.

    Una revisión desactualizada se conserva para auditoría pero nunca se aplica.
    ``reopened`` restaura el valor del extractor/inferencia.
    """

    result = dict(record)
    automatic_values = {field: record.get(field) for field in EFFECTIVE_FIELDS}
    inferred_values = _inferred_values(record)
    metadata = _metadata_from_evidence(record)
    global_confidence = _float_or_none(record.get("confidence"))
    extraction_method = str(record.get("extraction_method") or "")

    for field in EFFECTIVE_FIELDS:
        value = automatic_values[field]
        field_meta = metadata.get(field)
        if is_missing(value) and field_meta and not is_missing(field_meta.get("value")):
            value = field_meta["value"]
        if is_missing(value) and not is_missing(inferred_values.get(field)):
            value = inferred_values[field]
            field_meta = {
                "provenance": "inferred",
                "confidence": global_confidence,
                "method": "inferred_values",
                "page": record.get("page_number"),
                "end_page": record.get("end_page_number"),
                "fragment": "",
                "literal_value": inferred_values[field],
                "value": inferred_values[field],
            }
        result[field] = value
        if field_meta is None:
            if is_missing(value):
                field_meta = {
                    "provenance": "not_extracted",
                    "confidence": None,
                    "method": "",
                }
            else:
                field_meta = {
                    "provenance": "legacy_automatic",
                    "confidence": global_confidence,
                    "method": extraction_method,
                    "page": record.get("page_number"),
                    "end_page": record.get("end_page_number"),
                    "fragment": "",
                    "literal_value": value,
                    "value": value,
                }
        metadata[field] = field_meta

    result.update(
        {
            "review_status": "automatic",
            "reviewer": "",
            "reviewed_at": "",
            "review_notes": "",
            "review_stale": False,
            "review_stale_reason": "",
            "review_extraction_changed": False,
            "review_needs_reconfirmation": False,
            "automatic_values": automatic_values,
            "inferred_values": inferred_values,
        }
    )

    if event is not None:
        event_uid = str(_event_value(event, "decision_uid") or "")
        record_uid = str(record.get("decision_uid") or "")
        identity_changed = bool(event_uid and record_uid and event_uid != record_uid)
        document_changed = bool(
            _event_value(event, "source_document_hash")
            and str(_event_value(event, "source_document_hash"))
            != str(record.get("document_hash") or "")
        )
        extraction_changed = bool(
            _event_value(event, "source_record_key")
            and str(_event_value(event, "source_record_key"))
            != str(record.get("record_key") or "")
        )
        # Una reextracción esperada puede cambiar record_key sin cambiar la
        # fuente ni el UID estable. En ese caso se conserva la corrección, pero
        # se solicita reconfirmación. Un PDF o identidad distintos sí invalidan
        # el overlay para evitar aplicar una revisión a otra decisión.
        stale = document_changed or identity_changed
        status = str(_event_value(event, "status") or "automatic")
        result["review_stale"] = stale
        result["review_stale_reason"] = (
            "document_changed"
            if document_changed
            else "identity_changed"
            if identity_changed
            else ""
        )
        result["review_extraction_changed"] = extraction_changed
        result["review_needs_reconfirmation"] = (
            extraction_changed and not stale and status != "reopened"
        )
        result["review_status"] = "stale" if stale else status
        result["reviewer"] = str(_event_value(event, "reviewer") or "")
        result["reviewed_at"] = str(_event_value(event, "reviewed_at") or "")
        result["review_notes"] = str(_event_value(event, "notes") or "")
        corrections = _event_value(event, "corrections", {})
        if not stale and status != "reopened" and isinstance(corrections, Mapping):
            canonical_corrections: dict[str, object] = {}
            for raw_field, value in corrections.items():
                field = _canonical_field(raw_field)
                if not field:
                    continue
                canonical_corrections[field] = value
            result.update(canonical_corrections)
            for field, value in canonical_corrections.items():
                metadata[field] = {
                    "provenance": "verified",
                    "confidence": 1.0,
                    "method": "human_review",
                    "page": result.get("page_number"),
                    "end_page": result.get("end_page_number"),
                    "fragment": "",
                    "literal_value": value,
                    "value": value,
                    "review_event_id": str(_event_value(event, "event_id") or ""),
                }

    result["field_metadata"] = metadata
    result["field_provenance"] = {
        field: str(meta.get("provenance") or "not_extracted")
        for field, meta in metadata.items()
    }
    result["field_confidence"] = {
        field: _float_or_none(meta.get("confidence")) for field, meta in metadata.items()
    }
    result["effective_values"] = {
        field: result.get(field) for field in EFFECTIVE_FIELDS
    }
    return result


def apply_effective_records(
    records: Iterable[Mapping[str, object]],
    latest_events: Mapping[str, object] | None = None,
) -> list[dict[str, object]]:
    events = latest_events or {}
    return [
        effective_record(record, events.get(str(record.get("decision_uid") or "")))
        for record in records
    ]


def hydrate_field_evidence(
    database_path: Path,
    records: Iterable[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Adjunta evidencias v4 a fichas sin acoplar las consultas principales.

    La función es compatible con bases 0.6: si la tabla aún no existe devuelve
    las fichas intactas. Se consulta en lotes para respetar el límite de
    parámetros de SQLite.
    """

    hydrated = [dict(record) for record in records]
    ids = [
        int(record["record_id"])
        for record in hydrated
        if record.get("record_id") is not None
    ]
    if not ids or not database_path.exists():
        return hydrated
    evidence_by_id: dict[int, list[dict[str, object]]] = {}
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'regulatory_field_evidence'"
        ).fetchone()
        if exists is None:
            return hydrated
        for start in range(0, len(ids), 800):
            batch = ids[start : start + 800]
            placeholders = ",".join("?" for _ in batch)
            rows = connection.execute(
                "SELECT record_id, field_name, ordinal, literal_value, "
                "normalized_value, canonical_value, page_number, "
                "end_page_number, evidence_text, extraction_method, confidence "
                "FROM regulatory_field_evidence "
                f"WHERE record_id IN ({placeholders}) "
                "ORDER BY record_id, field_name, ordinal",
                batch,
            ).fetchall()
            for row in rows:
                evidence_by_id.setdefault(int(row["record_id"]), []).append(
                    {
                        "field_name": row["field_name"],
                        "ordinal": row["ordinal"],
                        "literal_value": row["literal_value"],
                        "normalized_value": row["normalized_value"],
                        "canonical_value": row["canonical_value"],
                        "page_number": row["page_number"],
                        "end_page_number": row["end_page_number"],
                        "evidence_text": row["evidence_text"],
                        "extraction_method": row["extraction_method"],
                        "confidence": row["confidence"],
                    }
                )
    finally:
        connection.close()
    for record in hydrated:
        record_id = record.get("record_id")
        if record_id is not None and int(record_id) in evidence_by_id:
            record["field_evidence"] = evidence_by_id[int(record_id)]
    return hydrated


def field_metadata(record: Mapping[str, object], field: str) -> dict[str, object]:
    canonical = _canonical_field(field)
    metadata = record.get("field_metadata")
    if isinstance(metadata, Mapping) and isinstance(metadata.get(canonical), Mapping):
        return dict(metadata[canonical])
    return {
        "provenance": "not_extracted" if is_missing(record.get(canonical)) else "legacy_automatic",
        "confidence": _float_or_none(record.get("confidence")),
        "method": str(record.get("extraction_method") or ""),
    }


def field_evidence_details(
    record: Mapping[str, object], fields: Iterable[str] = ()
) -> list[dict[str, object]]:
    """Devuelve todas las evidencias, incluidas combinaciones multiactivo."""

    wanted = {_canonical_field(field) for field in fields if _canonical_field(field)}
    details: list[dict[str, object]] = []
    for item in _evidence_items(record):
        raw_field = item.get("campo") or item.get("field_name")
        source_field = normalize_text(str(raw_field or "")).replace(" ", "_")
        target_fields = _evidence_field_targets(raw_field)
        if not target_fields:
            continue
        method = item.get("metodo") or item.get("extraction_method") or item.get("method")
        for field in target_fields:
            if wanted and field not in wanted:
                continue
            details.append(
                {
                    "field": field,
                    "source_field": source_field,
                    "ordinal": int(item.get("ordinal") or 0),
                    "literal_value": item.get(
                        "valor_literal", item.get("literal_value")
                    ),
                    "normalized_value": item.get(
                        "valor_normalizado", item.get("normalized_value")
                    ),
                    "canonical_value": item.get(
                        "valor_canonico", item.get("canonical_value")
                    ),
                    "page": item.get("pagina", item.get("page_number")),
                    "end_page": item.get(
                        "pagina_final", item.get("end_page_number")
                    ),
                    "fragment": str(
                        item.get("fragmento") or item.get("evidence_text") or ""
                    ),
                    "method": str(method or ""),
                    "confidence": _float_or_none(
                        item.get("confianza", item.get("confidence"))
                    ),
                    "provenance": _provenance_for_method(method),
                }
            )
    return sorted(
        details,
        key=lambda detail: (
            str(detail["field"]),
            int(detail["ordinal"]),
            int(detail["page"] or 0),
        ),
    )


def effective_record_matches(
    record: Mapping[str, object],
    *,
    query: str = "",
    products: Iterable[str] = (),
    active_ingredients: Iterable[str] = (),
    interested_parties: Iterable[str] = (),
    identifiers: Iterable[str] = (),
    outcomes: Iterable[str] = (),
    request_types: Iterable[str] = (),
    years: Iterable[int] = (),
    missing_field: str = "",
    provenances: Iterable[str] = (),
    confidence_minimum: float | None = None,
    review_statuses: Iterable[str] = (),
    provenance_field: str = "active_ingredient",
) -> bool:
    """Filtra sobre el valor vigente, incluidas correcciones sin reindexar."""

    def contains(field_value: object, expected: object) -> bool:
        return normalize_text(str(expected or "")) in normalize_text(str(field_value or ""))

    normalized_query = normalize_text(query)
    if normalized_query:
        searchable = " ".join(
            str(record.get(field) or "")
            for field in EFFECTIVE_FIELDS
        )
        if normalized_query not in normalize_text(searchable):
            return False
    wanted_years = {int(value) for value in years}
    if wanted_years:
        try:
            record_year = int(record.get("year"))
        except (TypeError, ValueError):
            return False
        if record_year not in wanted_years:
            return False
    for field, expected_values in (
        ("product_name", products),
        ("active_ingredient", active_ingredients),
        ("interested_party", interested_parties),
    ):
        values = [value for value in expected_values if not is_missing(value)]
        if values and not any(contains(record.get(field), value) for value in values):
            return False
    wanted_identifiers = [value for value in identifiers if not is_missing(value)]
    if wanted_identifiers and not any(
        contains(f"{record.get('expediente') or ''} {record.get('radicado') or ''}", value)
        for value in wanted_identifiers
    ):
        return False
    for field, expected_values in (
        ("outcome_code", outcomes),
        ("request_type_code", request_types),
        ("review_status", review_statuses),
    ):
        values = {str(value) for value in expected_values if not is_missing(value)}
        if values and str(record.get(field) or "") not in values:
            return False
    missing_aliases = {
        "product": "product_name",
        "producto": "product_name",
        "active_ingredient": "active_ingredient",
        "principio_activo": "active_ingredient",
        "request_type": "request_type_code",
        "tipo_solicitud": "request_type_code",
        "outcome": "outcome_code",
        "resultado": "outcome_code",
    }
    clean_missing = normalize_text(missing_field).replace(" ", "_")
    canonical_missing = missing_aliases.get(clean_missing) or _canonical_field(clean_missing)
    if canonical_missing:
        current_value = record.get(canonical_missing)
        classified_as_missing = is_missing(current_value)
        if canonical_missing == "request_type_code":
            classified_as_missing = classified_as_missing or current_value == "otra_solicitud"
        elif canonical_missing == "outcome_code":
            classified_as_missing = classified_as_missing or current_value == "sin_clasificar"
        if not classified_as_missing:
            return False
    meta = field_metadata(record, provenance_field)
    wanted_provenances = {
        str(value) for value in provenances if not is_missing(value)
    }
    if wanted_provenances:
        available_provenances = {str(meta.get("provenance") or "")}
        if meta.get("provenance") != "verified":
            available_provenances.update(
                str(detail.get("provenance") or "")
                for detail in field_evidence_details(record, [provenance_field])
            )
        if not wanted_provenances.intersection(available_provenances):
            return False
    confidence = _float_or_none(meta.get("confidence"))
    if confidence_minimum is not None and (
        confidence is None or confidence < float(confidence_minimum)
    ):
        return False
    return True


def filter_effective_records(
    records: Iterable[Mapping[str, object]],
    **filters: object,
) -> list[dict[str, object]]:
    return [dict(record) for record in records if effective_record_matches(record, **filters)]


def associate_effective_records_to_chunks(
    records_by_chunk: Mapping[int, Sequence[Mapping[str, object]]],
    chunk_context: Mapping[int, Mapping[str, object]],
    supplemental_records: Iterable[Mapping[str, object]] = (),
) -> dict[int, list[dict[str, object]]]:
    """Reasocia fichas después de aplicar correcciones de rango de páginas.

    ``records_by_chunk`` puede provenir de la relación automática antigua. Las
    fichas suplementarias (normalmente las que tienen revisión de páginas) se
    usan para añadir la asociación nueva. URL+página impiden cruzar decisiones
    entre documentos.
    """

    supplements = [dict(record) for record in supplemental_records]

    def identity(record: Mapping[str, object]) -> tuple[str, object]:
        uid = str(record.get("decision_uid") or "")
        if uid:
            return ("uid", uid)
        return ("id", record.get("record_id"))

    output: dict[int, list[dict[str, object]]] = {}
    for raw_chunk_id, initial in records_by_chunk.items():
        chunk_id = int(raw_chunk_id)
        context = chunk_context.get(chunk_id, {})
        page = int(context.get("page") or 0)
        url = str(context.get("url") or "")
        candidates = [dict(record) for record in initial]
        candidates.extend(
            record for record in supplements if url and str(record.get("url") or "") == url
        )
        accepted: dict[tuple[str, object], dict[str, object]] = {}
        for record in candidates:
            try:
                start_page = int(record.get("page_number") or 0)
                end_page = int(record.get("end_page_number") or start_page)
            except (TypeError, ValueError):
                continue
            if page and start_page <= page <= end_page:
                accepted[identity(record)] = record
        output[chunk_id] = list(accepted.values())
    return output


def matching_review_uids(events: Iterable[object], query: str) -> set[str]:
    """Busca una consulta en las correcciones humanas vigentes.

    Este helper permite recuperar la ficha por UID aun si FTS no contiene el
    valor corregido. La vigencia respecto del PDF se valida después, al aplicar
    la revisión sobre la ficha recuperada.
    """

    needle = normalize_text(query)
    if not needle:
        return set()
    latest: dict[str, object] = {}
    for event in events:
        uid = str(_event_value(event, "decision_uid") or "")
        if uid:
            current = latest.get(uid)
            if current is None or (
                str(_event_value(event, "reviewed_at")), str(_event_value(event, "event_id"))
            ) >= (
                str(_event_value(current, "reviewed_at")), str(_event_value(current, "event_id"))
            ):
                latest[uid] = event
    matches: set[str] = set()
    for uid, event in latest.items():
        if str(_event_value(event, "status")) == "reopened":
            continue
        corrections = _event_value(event, "corrections", {})
        if not isinstance(corrections, Mapping):
            continue
        searchable = " ".join(str(value or "") for value in corrections.values())
        if needle in normalize_text(searchable):
            matches.add(uid)
    return matches


@dataclass(frozen=True)
class ReconciliationPlan:
    """Plan estricto; nunca adivina una identidad ambigua."""

    uid_mapping: dict[str, str]
    exact_event_ids: tuple[str, ...]
    relinked_event_ids: tuple[str, ...]
    unresolved_event_ids: tuple[str, ...]
    ambiguous_event_ids: tuple[str, ...]


@dataclass(frozen=True)
class EffectiveRecordPage:
    records: tuple[dict[str, object], ...]
    total_matches: int
    scanned_records: int
    truncated: bool


def evidence_results_for_records(
    database_path: Path,
    records: Iterable[Mapping[str, object]],
    *,
    maximum: int = 100,
) -> tuple[dict[str, object], bool]:
    """Recupera un fragmento seleccionable para cada ficha vigente.

    Se usa para que una coincidencia hallada solo en el CSV humano pueda seguir
    el mismo flujo de selección, comparación y exportación que una coincidencia
    FTS. El fragmento siempre se relee del índice vigente.
    """

    if maximum < 1:
        raise ValueError("maximum debe ser positivo")
    values = [dict(record) for record in records]
    truncated = len(values) > maximum
    values = values[:maximum]
    clauses: list[str] = []
    parameters: list[object] = []
    for record in values:
        if record.get("document_id") is None:
            continue
        start = int(record.get("page_number") or 1)
        end = int(record.get("end_page_number") or start)
        clauses.append("(p.document_id = ? AND p.page_number BETWEEN ? AND ?)")
        parameters.extend((int(record["document_id"]), start, end))
    if not clauses or not database_path.exists():
        return {}, truncated
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT c.id AS chunk_id, c.text, p.document_id, p.page_number, "
            "d.title, d.url, d.year, d.acta_number, d.section, d.part, "
            "d.source_type, c.chunk_index "
            "FROM chunks c JOIN pages p ON p.id = c.page_id "
            "JOIN documents d ON d.id = p.document_id WHERE "
            + " OR ".join(clauses)
            + " ORDER BY p.document_id, p.page_number, c.chunk_index",
            parameters,
        ).fetchall()
    finally:
        connection.close()
    rows_by_document: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        rows_by_document.setdefault(int(row["document_id"]), []).append(row)
    from services.models import SearchResult

    results: dict[str, object] = {}
    for record in values:
        uid = str(record.get("decision_uid") or "")
        document_id = record.get("document_id")
        if not uid or document_id is None:
            continue
        start = int(record.get("page_number") or 1)
        end = int(record.get("end_page_number") or start)
        row = next(
            (
                candidate
                for candidate in rows_by_document.get(int(document_id), [])
                if start <= int(candidate["page_number"]) <= end
            ),
            None,
        )
        if row is None:
            continue
        results[uid] = SearchResult(
            chunk_id=int(row["chunk_id"]),
            title=str(row["title"]),
            url=str(row["url"]),
            page=int(row["page_number"]),
            text=str(row["text"]),
            year=int(row["year"]) if row["year"] is not None else None,
            acta_number=row["acta_number"],
            section=row["section"],
            part=row["part"],
            source_type=str(row["source_type"] or "official"),
            score=1.0,
            lexical_score=None,
            semantic_score=None,
            match_type="human_review",
        )
    return results, truncated


def evidence_selection_payload(result: object, decision_uid: str) -> dict[str, object]:
    """Añade la identidad de ficha al snapshot de un fragmento compartido."""

    as_dict = getattr(result, "as_dict", None)
    if not callable(as_dict):
        raise TypeError("La evidencia no puede serializarse")
    uid = str(decision_uid or "").strip()
    if not uid:
        raise ValueError("La ficha no tiene decision_uid")
    return {**as_dict(), "decision_uid": uid}


def scan_effective_record_page(
    database_path: Path,
    latest_events: Mapping[str, object],
    *,
    page: int,
    page_size: int,
    batch_size: int = 500,
    maximum_records: int = 100_000,
    **filters: object,
) -> EffectiveRecordPage:
    """Pagina filtros vigentes recorriendo el corpus en lotes acotados.

    Se usa en la cola administrativa, donde exactitud prima sobre latencia. No
    retiene solicitud/concepto de todo el corpus: cada lote se descarta después
    de contar y conservar solo la página solicitada.
    """

    if page < 1 or page_size < 1 or batch_size < 1 or maximum_records < 1:
        raise ValueError("Parámetros de paginación inválidos")
    from services.database import list_regulatory_records

    start_match = (page - 1) * page_size
    end_match = start_match + page_size
    selected: list[dict[str, object]] = []
    total_matches = 0
    scanned = 0
    offset = 0
    # El año no es corregible y puede reducir el barrido de forma segura.
    safe_years = filters.get("years", ())
    while scanned < maximum_records:
        requested = min(batch_size, maximum_records - scanned)
        batch = list_regulatory_records(
            database_path,
            years=safe_years if isinstance(safe_years, Iterable) else (),
            limit=requested,
            offset=offset,
        )
        if not batch:
            break
        effective_batch = apply_effective_records(
            hydrate_field_evidence(database_path, batch), latest_events
        )
        for record in effective_batch:
            if effective_record_matches(record, **filters):
                if start_match <= total_matches < end_match:
                    selected.append(record)
                total_matches += 1
        count = len(batch)
        scanned += count
        offset += count
        if count < requested:
            break
    truncated = scanned >= maximum_records and bool(
        list_regulatory_records(
            database_path,
            years=safe_years if isinstance(safe_years, Iterable) else (),
            limit=1,
            offset=offset,
        )
    )
    return EffectiveRecordPage(
        records=tuple(selected),
        total_matches=total_matches,
        scanned_records=scanned,
        truncated=truncated,
    )


def plan_review_reconciliation(
    events: Iterable[object],
    records: Iterable[Mapping[str, object]],
    *,
    identity_aliases: Mapping[str, str] | None = None,
) -> ReconciliationPlan:
    """Relaciona eventos antiguos usando UID o ambas huellas de origen.

    Las coincidencias por una sola huella no son suficientes. Las asociaciones
    adicionales deben llegar como ``identity_aliases`` desde un proceso de
    migración auditable.
    """

    records_list = list(records)
    by_uid = {
        str(record.get("decision_uid") or ""): record
        for record in records_list
        if str(record.get("decision_uid") or "")
    }
    by_fingerprint: dict[tuple[str, str], list[str]] = {}
    for record in records_list:
        uid = str(record.get("decision_uid") or "")
        fingerprint = (
            str(record.get("record_key") or ""),
            str(record.get("document_hash") or ""),
        )
        if uid and all(fingerprint):
            by_fingerprint.setdefault(fingerprint, []).append(uid)

    aliases = {str(key): str(value) for key, value in (identity_aliases or {}).items()}
    mapping: dict[str, str] = {}
    exact: list[str] = []
    relinked: list[str] = []
    unresolved: list[str] = []
    ambiguous: list[str] = []
    for event in events:
        event_id = str(_event_value(event, "event_id") or "")
        old_uid = str(_event_value(event, "decision_uid") or "")
        if old_uid in by_uid:
            exact.append(event_id)
            continue
        alias = aliases.get(old_uid, "")
        if alias:
            if alias in by_uid:
                mapping[old_uid] = alias
                relinked.append(event_id)
            else:
                unresolved.append(event_id)
            continue
        fingerprint = (
            str(_event_value(event, "source_record_key") or ""),
            str(_event_value(event, "source_document_hash") or ""),
        )
        candidates = list(dict.fromkeys(by_fingerprint.get(fingerprint, [])))
        if len(candidates) == 1:
            mapping[old_uid] = candidates[0]
            relinked.append(event_id)
        elif len(candidates) > 1:
            ambiguous.append(event_id)
        else:
            unresolved.append(event_id)
    return ReconciliationPlan(
        uid_mapping=mapping,
        exact_event_ids=tuple(exact),
        relinked_event_ids=tuple(relinked),
        unresolved_event_ids=tuple(unresolved),
        ambiguous_event_ids=tuple(ambiguous),
    )


def apply_reconciliation_plan(
    events: Iterable[object], plan: ReconciliationPlan
) -> list[object]:
    """Aplica únicamente el mapa previamente auditado, preservando eventos."""

    output: list[object] = []
    for event in events:
        old_uid = str(_event_value(event, "decision_uid") or "")
        new_uid = plan.uid_mapping.get(old_uid)
        if not new_uid:
            output.append(event)
        elif is_dataclass(event):
            output.append(replace(event, decision_uid=new_uid))
        elif isinstance(event, Mapping):
            output.append({**event, "decision_uid": new_uid})
        else:
            raise TypeError("El evento no permite reconciliar decision_uid")
    return output
