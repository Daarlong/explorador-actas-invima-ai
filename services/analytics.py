from __future__ import annotations

"""Consultas agregadas y trazables para el tablero del corpus.

El modulo mide tres unidades distintas y no las mezcla:

* documentos/actas publicadas e indexadas;
* fichas regulatorias extraidas de esos documentos;
* valores de campos extraidos en las fichas.

Una base antigua puede no tener tablas o columnas estructuradas. En ese caso
la API conserva las metricas documentales y marca como no disponibles las
metricas que no se pueden calcular; nunca convierte esa ausencia en un cero.
"""

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Mapping
import sqlite3

from services.database import connect
from services.text_utils import normalize_text


MAX_TOP_LIMIT = 100
MAX_EVIDENCE_LIMIT = 20
MAX_DRILLDOWN_LIMIT = 500

ANALYTICS_DIMENSIONS = (
    "year",
    "outcome",
    "request_type",
    "active_ingredient",
    "interested_party",
)


@dataclass(frozen=True)
class AnalyticsFilters:
    """Filtros compartidos por todas las metricas del tablero."""

    years: tuple[int, ...] = ()
    sections: tuple[str, ...] = ()
    acta_numbers: tuple[str, ...] = ()
    outcomes: tuple[str, ...] = ()
    request_types: tuple[str, ...] = ()
    active_ingredients: tuple[str, ...] = ()
    interested_parties: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, list]:
        return {key: list(value) for key, value in asdict(self).items()}


def _unique_strings(values: Iterable[object]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            clean
            for value in values
            if (clean := str(value).strip())
        )
    )


def _coerce_filters(
    filters: AnalyticsFilters | Mapping[str, Iterable[object]] | None,
) -> AnalyticsFilters:
    if filters is None:
        return AnalyticsFilters()
    if isinstance(filters, AnalyticsFilters):
        return filters
    years: list[int] = []
    for value in filters.get("years", ()):
        try:
            years.append(int(value))
        except (TypeError, ValueError):
            continue
    return AnalyticsFilters(
        years=tuple(dict.fromkeys(years)),
        sections=_unique_strings(filters.get("sections", ())),
        acta_numbers=_unique_strings(filters.get("acta_numbers", ())),
        outcomes=_unique_strings(filters.get("outcomes", ())),
        request_types=_unique_strings(filters.get("request_types", ())),
        active_ingredients=_unique_strings(
            filters.get("active_ingredients", ())
        ),
        interested_parties=_unique_strings(
            filters.get("interested_parties", ())
        ),
    )


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    # ``table`` solo se recibe desde constantes internas.
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not exists:
        return set()
    return {
        str(row["name"])
        for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    }


def _placeholders(values: Iterable[object]) -> str:
    return ",".join("?" for _ in values)


def _document_filter_sql(
    filters: AnalyticsFilters,
    document_columns: set[str],
    *,
    alias: str = "d",
) -> tuple[list[str], list[object], list[str]]:
    clauses: list[str] = []
    parameters: list[object] = []
    unsupported: list[str] = []
    for key, column, values in (
        ("years", "year", filters.years),
        ("sections", "section", filters.sections),
        ("acta_numbers", "acta_number", filters.acta_numbers),
    ):
        if not values:
            continue
        if column not in document_columns:
            unsupported.append(key)
            continue
        clauses.append(f"{alias}.{column} IN ({_placeholders(values)})")
        parameters.extend(values)
    return clauses, parameters, unsupported


def _normalized_record_expression(
    record_columns: set[str], normalized: str, literal: str, alias: str = "r"
) -> str | None:
    if normalized in record_columns:
        return f"{alias}.{normalized}"
    if literal in record_columns:
        return f"LOWER(TRIM(COALESCE({alias}.{literal}, '')))"
    return None


def _record_filter_sql(
    filters: AnalyticsFilters,
    record_columns: set[str],
    *,
    alias: str = "r",
) -> tuple[list[str], list[object], list[str]]:
    clauses: list[str] = []
    parameters: list[object] = []
    unsupported: list[str] = []
    for key, column, values in (
        ("outcomes", "outcome_code", filters.outcomes),
        ("request_types", "request_type_code", filters.request_types),
    ):
        if not values:
            continue
        if column not in record_columns:
            unsupported.append(key)
            continue
        clauses.append(f"{alias}.{column} IN ({_placeholders(values)})")
        parameters.extend(values)

    for key, normalized, literal, values in (
        (
            "active_ingredients",
            "normalized_active_ingredient",
            "active_ingredient",
            filters.active_ingredients,
        ),
        (
            "interested_parties",
            "normalized_interested_party",
            "interested_party",
            filters.interested_parties,
        ),
    ):
        if not values:
            continue
        expression = _normalized_record_expression(
            record_columns, normalized, literal, alias
        )
        if expression is None:
            unsupported.append(key)
            continue
        # Un registro puede contener mas de un principio activo separado por
        # puntuacion. LIKE permite seleccionar uno sin fingir que el valor
        # combinado es una entidad diferente. Los valores siguen parametrizados.
        alternatives = [f"{expression} LIKE ?" for _ in values]
        clauses.append("(" + " OR ".join(alternatives) + ")")
        normalized_available = normalized in record_columns
        parameters.extend(
            f"%{normalize_text(value) if normalized_available else value.casefold()}%"
            for value in values
        )
    return clauses, parameters, unsupported


def _where(clauses: list[str]) -> str:
    return " WHERE " + " AND ".join(clauses) if clauses else ""


def _percent(numerator: int | None, denominator: int | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return round((numerator / denominator) * 100.0, 2)


def _metric(value: int | None, unit: str, *, available: bool = True) -> dict:
    return {"available": bool(available), "value": value, "unit": unit}


def _empty_dimension(unit: str, reason: str) -> dict:
    return {
        "available": False,
        "reason": reason,
        "unit": unit,
        "source": None,
        "denominator": {"value": None, "unit": None},
        "coverage": {"value": None, "percent": None},
        "total_values": None,
        "truncated": False,
        "items": [],
    }


def _empty_report(
    filters: AnalyticsFilters,
    top_limit: int,
    evidence_limit: int,
    reason: str,
) -> dict:
    dimensions = {
        "year": _empty_dimension("documents_and_records", reason),
        "outcome": _empty_dimension("extracted_records", reason),
        "request_type": _empty_dimension("extracted_records", reason),
        "active_ingredient": _empty_dimension("extracted_values", reason),
        "interested_party": _empty_dimension("extracted_values", reason),
    }
    return {
        "status": "unavailable",
        "reason": reason,
        "filters": filters.as_dict(),
        "filter_compatibility": {"available": False, "unsupported": []},
        "limits": {
            "top_values": top_limit,
            "evidence_per_value": evidence_limit,
        },
        "totals": {
            "documents": _metric(None, "documents", available=False),
            "unique_acts": _metric(None, "acts", available=False),
            "records": _metric(None, "extracted_records", available=False),
            "distinct_ingredient_and_party_values": _metric(
                None, "values", available=False
            ),
        },
        "coverage": {
            "document_extraction": {
                "available": False,
                "documents": None,
                "documents_processed": None,
                "documents_with_records": None,
                "documents_without_records": None,
                "documents_without_completed_extraction": None,
                "record_coverage_percent": None,
                "scope": "document_filters_only",
            },
            "fields": {},
        },
        "dimensions": dimensions,
    }


def _selected_document_sql(
    filters: AnalyticsFilters,
    document_columns: set[str],
    record_columns: set[str],
) -> tuple[list[str], list[object], list[str]]:
    clauses, parameters, unsupported = _document_filter_sql(
        filters, document_columns
    )
    record_clauses, record_parameters, record_unsupported = _record_filter_sql(
        filters, record_columns, alias="rf"
    )
    unsupported.extend(record_unsupported)
    if record_clauses:
        if not {"document_id"}.issubset(record_columns):
            unsupported.extend(
                key
                for key in (
                    "outcomes",
                    "request_types",
                    "active_ingredients",
                    "interested_parties",
                )
                if getattr(filters, key)
            )
        else:
            clauses.append(
                "EXISTS (SELECT 1 FROM regulatory_records rf "
                "WHERE rf.document_id=d.id AND "
                + " AND ".join(record_clauses)
                + ")"
            )
            parameters.extend(record_parameters)
    return clauses, parameters, list(dict.fromkeys(unsupported))


def _record_selection_sql(
    filters: AnalyticsFilters,
    document_columns: set[str],
    record_columns: set[str],
) -> tuple[list[str], list[object], list[str]]:
    clauses, parameters, unsupported = _document_filter_sql(
        filters, document_columns
    )
    record_clauses, record_parameters, record_unsupported = _record_filter_sql(
        filters, record_columns
    )
    clauses.extend(record_clauses)
    parameters.extend(record_parameters)
    unsupported.extend(record_unsupported)
    return clauses, parameters, list(dict.fromkeys(unsupported))


def _unique_act_expression(document_columns: set[str]) -> str:
    required = {"year", "section", "acta_number"}
    if not required.issubset(document_columns):
        return "CAST(d.id AS TEXT)"
    # Solo los tres metadatos juntos identifican un acta multipartes. Si
    # cualquiera falta, dos PDF distintos no se deben fusionar por accidente.
    return (
        "CASE WHEN d.year IS NOT NULL "
        "AND TRIM(COALESCE(d.section, '')) != '' "
        "AND TRIM(COALESCE(d.acta_number, '')) != '' "
        "THEN CAST(d.year AS TEXT) || '|' || TRIM(d.section) || '|' || "
        "TRIM(d.acta_number) ELSE 'document:' || CAST(d.id AS TEXT) END"
    )


def _record_field_coverage(
    connection: sqlite3.Connection,
    *,
    total_records: int,
    record_where: str,
    record_parameters: list[object],
    record_columns: set[str],
) -> dict[str, dict]:
    definitions = {
        "outcome": (
            "outcome_code",
            "LOWER(TRIM(COALESCE(r.outcome_code, ''))) NOT IN "
            "('', 'sin_clasificar', 'unclassified', 'unknown', 'none')",
        ),
        "request_type": (
            "request_type_code",
            "LOWER(TRIM(COALESCE(r.request_type_code, ''))) NOT IN "
            "('', 'otra_solicitud', 'unknown', 'none')",
        ),
        "active_ingredient": (
            "active_ingredient",
            "TRIM(COALESCE(r.active_ingredient, '')) != ''",
        ),
        "interested_party": (
            "interested_party",
            "TRIM(COALESCE(r.interested_party, '')) != ''",
        ),
    }
    result: dict[str, dict] = {}
    for key, (required, present_expression) in definitions.items():
        if required not in record_columns:
            result[key] = {
                "available": False,
                "records": total_records,
                "present": None,
                "missing": None,
                "coverage_percent": None,
            }
            continue
        clauses = record_where
        connector = " AND " if clauses else " WHERE "
        present = int(
            connection.execute(
                "SELECT COUNT(*) FROM regulatory_records r "
                "JOIN documents d ON d.id=r.document_id"
                + clauses
                + connector
                + present_expression,
                record_parameters,
            ).fetchone()[0]
        )
        result[key] = {
            "available": True,
            "records": total_records,
            "present": present,
            "missing": max(total_records - present, 0),
            "coverage_percent": _percent(present, total_records),
        }
    return result


def _document_detail_select(document_columns: set[str]) -> str:
    def value(column: str, default: str = "NULL") -> str:
        return f"d.{column}" if column in document_columns else default

    return (
        "d.id AS document_id, "
        f"{value('title', "''")} AS title, "
        f"{value('url', "''")} AS url, "
        f"{value('year')} AS year, "
        f"{value('acta_number')} AS acta_number, "
        f"{value('section')} AS section"
    )


def _record_detail_select(
    document_columns: set[str], record_columns: set[str], evidence_sql: str
) -> str:
    def rvalue(column: str, default: str = "NULL") -> str:
        return f"r.{column}" if column in record_columns else default

    return (
        _document_detail_select(document_columns)
        + ", "
        + f"{rvalue('id')} AS record_id, "
        + f"{rvalue('decision_uid')} AS decision_uid, "
        + f"{rvalue('page_number')} AS page, "
        + f"{rvalue('end_page_number')} AS end_page, "
        + f"{rvalue('numeral')} AS numeral, "
        + evidence_sql
        + " AS evidence_text"
    )


def _evidence_field_expression(evidence_columns: set[str]) -> str | None:
    candidates = [
        column
        for column in ("canonical_value", "normalized_value", "literal_value")
        if column in evidence_columns
    ]
    if not candidates:
        return None
    values = ", ".join(f"NULLIF(TRIM(fe.{column}), '')" for column in candidates)
    return f"LOWER(COALESCE({values}, ''))"


def _evidence_label_expression(evidence_columns: set[str]) -> str:
    candidates = [
        column
        for column in ("literal_value", "canonical_value", "normalized_value")
        if column in evidence_columns
    ]
    if not candidates:
        return "''"
    values = ", ".join(f"NULLIF(TRIM(fe.{column}), '')" for column in candidates)
    return f"COALESCE({values}, '')"


def _compound_dimension_values_sql(
    key: str,
    record_columns: set[str],
    evidence_columns: set[str],
) -> str | None:
    """Crea valores por ficha usando evidencia y fallback sin duplicarlos.

    La presencia de la *tabla* de evidencia no implica que cada ficha/campo
    tenga una fila. Para cada ficha se prefieren sus valores de evidencia
    individuales; solo cuando no existe ninguno se usa el valor normalizado
    almacenado en ``regulatory_records``.
    """

    mapping = {
        "active_ingredient": (
            "principio_activo",
            "normalized_active_ingredient",
            "active_ingredient",
        ),
        "interested_party": (
            "interesado",
            "normalized_interested_party",
            "interested_party",
        ),
    }
    definition = mapping.get(key)
    if not definition or "id" not in record_columns:
        return None
    field_name, normalized_column, literal_column = definition
    record_value = _normalized_record_expression(
        record_columns, normalized_column, literal_column, alias="r0"
    )
    if record_value is None:
        return None
    record_label = (
        f"r0.{literal_column}"
        if literal_column in record_columns
        else record_value
    )
    evidence_value = _evidence_field_expression(evidence_columns)
    if (
        evidence_value is None
        or not {"record_id", "field_name"}.issubset(evidence_columns)
    ):
        return (
            "SELECT r0.id AS record_id, "
            f"{record_value} AS value, {record_label} AS label, "
            "NULL AS evidence_text FROM regulatory_records r0 "
            f"WHERE {record_value} != ''"
        )
    evidence_label = _evidence_label_expression(evidence_columns)
    evidence_text = (
        "MIN(fe.evidence_text)"
        if "evidence_text" in evidence_columns
        else "NULL"
    )
    # field_name proviene de la tabla constante anterior, no de entrada del
    # usuario. Los filtros de usuario permanecen en parametros SQL.
    return f"""
        SELECT fe.record_id AS record_id, {evidence_value} AS value,
               MIN({evidence_label}) AS label, {evidence_text} AS evidence_text
        FROM regulatory_field_evidence fe
        WHERE fe.field_name = '{field_name}' AND {evidence_value} != ''
        GROUP BY fe.record_id, {evidence_value}
        UNION ALL
        SELECT r0.id AS record_id, {record_value} AS value,
               {record_label} AS label, NULL AS evidence_text
        FROM regulatory_records r0
        WHERE {record_value} != ''
          AND NOT EXISTS (
              SELECT 1 FROM regulatory_field_evidence fe
              WHERE fe.record_id = r0.id
                AND fe.field_name = '{field_name}'
                AND {evidence_value} != ''
          )
    """


def _dimension_source(
    key: str,
    record_columns: set[str],
    evidence_columns: set[str],
) -> tuple[str | None, str | None, str | None, str | None]:
    """Retorna source, key expression, label expression y field evidence."""

    if key == "outcome" and "outcome_code" in record_columns:
        expression = "LOWER(TRIM(COALESCE(r.outcome_code, '')))"
        return "regulatory_records", expression, "r.outcome_code", None
    if key == "request_type" and "request_type_code" in record_columns:
        expression = "LOWER(TRIM(COALESCE(r.request_type_code, '')))"
        return "regulatory_records", expression, "r.request_type_code", None

    evidence_field = {
        "active_ingredient": "principio_activo",
        "interested_party": "interesado",
    }.get(key)
    evidence_expression = _evidence_field_expression(evidence_columns)
    if (
        evidence_field
        and evidence_expression
        and {"record_id", "field_name"}.issubset(evidence_columns)
    ):
        return (
            "regulatory_field_evidence_with_record_fallback",
            "dv.value",
            "dv.label",
            evidence_field,
        )

    fallback = {
        "active_ingredient": (
            "normalized_active_ingredient",
            "active_ingredient",
        ),
        "interested_party": (
            "normalized_interested_party",
            "interested_party",
        ),
    }.get(key)
    if fallback:
        expression = _normalized_record_expression(
            record_columns, fallback[0], fallback[1]
        )
        if expression:
            label = (
                f"r.{fallback[1]}"
                if fallback[1] in record_columns
                else expression
            )
            return "regulatory_records_fallback", expression, label, None
    return None, None, None, None


def _record_dimension(
    connection: sqlite3.Connection,
    *,
    key: str,
    filters: AnalyticsFilters,
    document_columns: set[str],
    record_columns: set[str],
    evidence_columns: set[str],
    top_limit: int,
    evidence_limit: int,
) -> dict:
    source, value_expression, label_expression, evidence_field = _dimension_source(
        key, record_columns, evidence_columns
    )
    unit = "extracted_values" if key in {"active_ingredient", "interested_party"} else "extracted_records"
    if not source or not value_expression or not label_expression:
        return _empty_dimension(unit, "field_unavailable")

    clauses, parameters, unsupported = _record_selection_sql(
        filters, document_columns, record_columns
    )
    if unsupported:
        return _empty_dimension(unit, "unsupported_filter")
    from_sql = "regulatory_records r JOIN documents d ON d.id=r.document_id"
    if evidence_field:
        values_sql = _compound_dimension_values_sql(
            key, record_columns, evidence_columns
        )
        if values_sql is None:
            return _empty_dimension(unit, "field_unavailable")
        from_sql += f" JOIN ({values_sql}) dv ON dv.record_id=r.id"
    clauses.append(f"{value_expression} != ''")
    if key == "outcome":
        clauses.append(
            f"{value_expression} NOT IN "
            "('sin_clasificar', 'unclassified', 'unknown', 'none')"
        )
    elif key == "request_type":
        clauses.append(
            f"{value_expression} NOT IN ('otra_solicitud', 'unknown', 'none')"
        )
    where = _where(clauses)
    denominator_clauses, denominator_parameters, _ = _record_selection_sql(
        filters, document_columns, record_columns
    )
    denominator = int(
        connection.execute(
            "SELECT COUNT(*) FROM regulatory_records r "
            "JOIN documents d ON d.id=r.document_id"
            + _where(denominator_clauses),
            denominator_parameters,
        ).fetchone()[0]
    )
    covered_records = int(
        connection.execute(
            f"SELECT COUNT(DISTINCT r.id) FROM {from_sql}{where}", parameters
        ).fetchone()[0]
    )
    total_values = int(
        connection.execute(
            f"SELECT COUNT(DISTINCT {value_expression}) FROM {from_sql}{where}",
            parameters,
        ).fetchone()[0]
    )
    rows = connection.execute(
        f"""
        SELECT {value_expression} AS value,
               MIN({label_expression}) AS label,
               COUNT(DISTINCT r.document_id) AS documents,
               COUNT(DISTINCT r.id) AS records,
               COUNT(*) AS occurrences
        FROM {from_sql}
        {where}
        GROUP BY {value_expression}
        ORDER BY occurrences DESC, label COLLATE NOCASE
        LIMIT ?
        """,
        [*parameters, top_limit],
    ).fetchall()
    items: list[dict] = []
    for row in rows:
        value = str(row["value"])
        detail = _analytics_drilldown_connection(
            connection,
            dimension=key,
            value=value,
            filters=filters,
            document_columns=document_columns,
            record_columns=record_columns,
            evidence_columns=evidence_columns,
            limit=evidence_limit,
            offset=0,
            count_total=False,
        )
        items.append(
            {
                "value": value,
                "label": str(row["label"] or value),
                "documents": int(row["documents"]),
                "records": int(row["records"]),
                "occurrences": int(row["occurrences"]),
                "evidence": detail["items"],
                "drilldown": {"dimension": key, "value": value},
            }
        )
    return {
        "available": True,
        "reason": None,
        "unit": unit,
        "source": source,
        "denominator": {"value": denominator, "unit": "extracted_records"},
        "coverage": {
            "value": covered_records,
            "percent": _percent(covered_records, denominator),
        },
        "total_values": total_values,
        "truncated": total_values > top_limit,
        "items": items,
    }


def _year_dimension(
    connection: sqlite3.Connection,
    *,
    filters: AnalyticsFilters,
    document_columns: set[str],
    record_columns: set[str],
    evidence_columns: set[str],
    top_limit: int,
    evidence_limit: int,
) -> dict:
    if "year" not in document_columns:
        return _empty_dimension("documents_and_records", "field_unavailable")
    document_clauses, document_parameters, unsupported = _selected_document_sql(
        filters, document_columns, record_columns
    )
    if unsupported:
        return _empty_dimension("documents_and_records", "unsupported_filter")
    document_rows = connection.execute(
        "SELECT d.year AS value, COUNT(*) AS documents FROM documents d"
        + _where(document_clauses)
        + (" AND d.year IS NOT NULL" if document_clauses else " WHERE d.year IS NOT NULL")
        + " GROUP BY d.year ORDER BY d.year DESC LIMIT ?",
        [*document_parameters, top_limit],
    ).fetchall()
    denominator = int(
        connection.execute(
            "SELECT COUNT(*) FROM documents d" + _where(document_clauses),
            document_parameters,
        ).fetchone()[0]
    )
    covered_documents = int(
        connection.execute(
            "SELECT COUNT(*) FROM documents d"
            + _where(document_clauses)
            + (" AND d.year IS NOT NULL" if document_clauses else " WHERE d.year IS NOT NULL"),
            document_parameters,
        ).fetchone()[0]
    )
    total_values = int(
        connection.execute(
            "SELECT COUNT(DISTINCT d.year) FROM documents d"
            + _where(document_clauses)
            + (" AND d.year IS NOT NULL" if document_clauses else " WHERE d.year IS NOT NULL"),
            document_parameters,
        ).fetchone()[0]
    )

    records_by_year: dict[object, int] = {}
    records_available = {"id", "document_id"}.issubset(record_columns)
    if records_available:
        record_clauses, record_parameters, record_unsupported = _record_selection_sql(
            filters, document_columns, record_columns
        )
        if not record_unsupported:
            rows = connection.execute(
                "SELECT d.year AS value, COUNT(*) AS records "
                "FROM regulatory_records r JOIN documents d ON d.id=r.document_id"
                + _where(record_clauses)
                + " GROUP BY d.year",
                record_parameters,
            ).fetchall()
            records_by_year = {row["value"]: int(row["records"]) for row in rows}

    items: list[dict] = []
    for row in document_rows:
        value = row["value"]
        detail = _analytics_drilldown_connection(
            connection,
            dimension="year",
            value=value,
            filters=filters,
            document_columns=document_columns,
            record_columns=record_columns,
            evidence_columns=evidence_columns,
            limit=evidence_limit,
            offset=0,
            count_total=False,
        )
        items.append(
            {
                "value": value,
                "label": str(value) if value is not None else "Sin año extraído",
                "documents": int(row["documents"]),
                "records": records_by_year.get(value, 0) if records_available else None,
                "occurrences": int(row["documents"]),
                "evidence": detail["items"],
                "drilldown": {"dimension": "year", "value": value},
            }
        )
    return {
        "available": True,
        "reason": None,
        "unit": "documents_and_records",
        "source": "documents",
        "denominator": {"value": denominator, "unit": "documents"},
        "coverage": {
            "value": covered_documents,
            "percent": _percent(covered_documents, denominator),
        },
        "total_values": total_values,
        "truncated": total_values > top_limit,
        "items": items,
    }


def _evidence_text_expression(
    dimension: str,
    record_columns: set[str],
    evidence_columns: set[str],
    *,
    joined_evidence: bool,
) -> str:
    if joined_evidence and "evidence_text" in evidence_columns:
        return "fe.evidence_text"
    preferred = {
        "outcome": "concept_text",
        "request_type": "request_text",
    }.get(dimension)
    if preferred and preferred in record_columns:
        return f"r.{preferred}"
    if "concept_text" in record_columns:
        return "r.concept_text"
    if "request_text" in record_columns:
        return "r.request_text"
    return "NULL"


def _rows_to_evidence(rows: Iterable[sqlite3.Row], *, evidence_type: str) -> list[dict]:
    result: list[dict] = []
    seen: set[tuple] = set()
    for row in rows:
        item = dict(row)
        key = (
            item.get("record_id"),
            item.get("document_id"),
            item.get("page"),
        )
        if key in seen:
            continue
        seen.add(key)
        item["evidence_type"] = (
            "record"
            if evidence_type == "auto" and item.get("record_id") is not None
            else "document"
            if evidence_type == "auto"
            else evidence_type
        )
        # La URL es la fuente oficial; la pagina es la unidad verificable.
        item["page"] = int(item["page"]) if item.get("page") is not None else None
        item["end_page"] = (
            int(item["end_page"]) if item.get("end_page") is not None else None
        )
        result.append(item)
    return result


def _analytics_drilldown_connection(
    connection: sqlite3.Connection,
    *,
    dimension: str,
    value: object,
    filters: AnalyticsFilters,
    document_columns: set[str],
    record_columns: set[str],
    evidence_columns: set[str],
    limit: int,
    offset: int,
    count_total: bool,
) -> dict:
    if dimension not in ANALYTICS_DIMENSIONS:
        raise ValueError(f"Dimension analitica no soportada: {dimension}")

    if dimension == "year":
        if "year" not in document_columns:
            return {
                "available": False,
                "reason": "field_unavailable",
                "total": None,
                "items": [],
            }
        clauses, parameters, unsupported = _document_filter_sql(
            filters, document_columns
        )
        if value is None:
            clauses.append("d.year IS NULL")
        else:
            clauses.append("d.year=?")
            parameters.append(int(value))
        record_clauses, record_parameters, record_unsupported = _record_filter_sql(
            filters, record_columns
        )
        unsupported.extend(record_unsupported)
        if unsupported:
            return {"available": False, "reason": "unsupported_filter", "total": None, "items": []}
        records_available = {"id", "document_id"}.issubset(record_columns)
        cte = ""
        from_sql = "documents d"
        select_sql = (
            _document_detail_select(document_columns)
            + ", NULL AS record_id, NULL AS decision_uid, NULL AS page, "
            "NULL AS end_page, NULL AS numeral, NULL AS evidence_text"
        )
        query_parameters: list[object] = list(parameters)
        if records_available:
            order = []
            if "page_number" in record_columns:
                order.append("r.page_number")
            order.append("r.id")
            cte = (
                "WITH first_record AS (SELECT r.*, ROW_NUMBER() OVER ("
                "PARTITION BY r.document_id ORDER BY "
                + ", ".join(order)
                + ") AS analytics_row FROM regulatory_records r"
                + _where(record_clauses)
                + ") "
            )
            from_sql += (
                " LEFT JOIN first_record r ON r.document_id=d.id "
                "AND r.analytics_row=1"
            )
            if record_clauses:
                clauses.append("r.id IS NOT NULL")
            evidence_sql = _evidence_text_expression(
                "year",
                record_columns,
                evidence_columns,
                joined_evidence=False,
            )
            select_sql = _record_detail_select(
                document_columns, record_columns, evidence_sql
            )
            # Los parametros del CTE aparecen antes que los del WHERE.
            query_parameters = [*record_parameters, *parameters]
        where = _where(clauses)
        total = (
            int(
                connection.execute(
                    cte + "SELECT COUNT(*) FROM " + from_sql + where,
                    query_parameters,
                ).fetchone()[0]
            )
            if count_total
            else None
        )
        rows = connection.execute(
            cte
            + "SELECT "
            + select_sql
            + " FROM "
            + from_sql
            + where
            + " ORDER BY d.id LIMIT ? OFFSET ?",
            [*query_parameters, limit, offset],
        ).fetchall()
        return {
            "available": True,
            "reason": None,
            "total": total,
            "items": _rows_to_evidence(rows, evidence_type="auto"),
        }

    if not {"id", "document_id"}.issubset(record_columns):
        return {"available": False, "reason": "records_unavailable", "total": None, "items": []}
    clauses, parameters, unsupported = _record_selection_sql(
        filters, document_columns, record_columns
    )
    if unsupported:
        return {"available": False, "reason": "unsupported_filter", "total": None, "items": []}

    joined_evidence = False
    source, value_expression, _, evidence_field = _dimension_source(
        dimension, record_columns, evidence_columns
    ) if dimension != "year" else (None, None, None, None)
    from_sql = "regulatory_records r JOIN documents d ON d.id=r.document_id"
    if dimension == "year":
        if value is None:
            clauses.append("d.year IS NULL")
        else:
            clauses.append("d.year=?")
            parameters.append(int(value))
    else:
        if not source or not value_expression:
            return {"available": False, "reason": "field_unavailable", "total": None, "items": []}
        if evidence_field:
            values_sql = _compound_dimension_values_sql(
                dimension, record_columns, evidence_columns
            )
            if values_sql is None:
                return {"available": False, "reason": "field_unavailable", "total": None, "items": []}
            from_sql += f" JOIN ({values_sql}) dv ON dv.record_id=r.id"
            joined_evidence = True
        clauses.append(f"{value_expression}=?")
        parameters.append(str(value).strip().lower())
    where = _where(clauses)
    total = None
    if count_total:
        total = int(
            connection.execute(
                f"SELECT COUNT(DISTINCT r.id) FROM {from_sql}{where}", parameters
            ).fetchone()[0]
        )
    if joined_evidence:
        fallback_evidence = _evidence_text_expression(
            dimension,
            record_columns,
            evidence_columns,
            joined_evidence=False,
        )
        evidence_sql = (
            "COALESCE(MIN(dv.evidence_text), " + fallback_evidence + ")"
            if fallback_evidence != "NULL"
            else "MIN(dv.evidence_text)"
        )
    else:
        evidence_sql = _evidence_text_expression(
            dimension,
            record_columns,
            evidence_columns,
            joined_evidence=False,
        )
    detail_select = _record_detail_select(
        document_columns, record_columns, evidence_sql
    )
    order_parts = [
        "d.year DESC" if "year" in document_columns else None,
        "r.page_number" if "page_number" in record_columns else None,
        "r.id",
    ]
    order = ", ".join(part for part in order_parts if part)
    grouping = " GROUP BY r.id" if joined_evidence else ""
    rows = connection.execute(
        f"SELECT {detail_select} FROM {from_sql}{where} "
        f"{grouping} ORDER BY {order} LIMIT ? OFFSET ?",
        [*parameters, limit, offset],
    ).fetchall()
    return {
        "available": True,
        "reason": None,
        "total": total,
        "items": _rows_to_evidence(rows, evidence_type="record"),
    }


def build_corpus_analytics(
    database_path: Path,
    filters: AnalyticsFilters | Mapping[str, Iterable[object]] | None = None,
    *,
    top_limit: int = 20,
    evidence_limit: int = 3,
) -> dict:
    """Construye el tablero agregado sin IA y con trazabilidad a la fuente.

    ``top_limit`` controla categorias visibles (maximo 100) y
    ``evidence_limit`` las muestras por categoria (maximo 20). Los conteos y
    ``total_values`` permanecen globales aunque la presentacion este truncada.
    """

    selected_filters = _coerce_filters(filters)
    top_limit = max(1, min(int(top_limit), MAX_TOP_LIMIT))
    evidence_limit = max(0, min(int(evidence_limit), MAX_EVIDENCE_LIMIT))
    path = Path(database_path)
    if not path.exists():
        return _empty_report(
            selected_filters, top_limit, evidence_limit, "database_missing"
        )

    try:
        with connect(path) as connection:
            document_columns = _table_columns(connection, "documents")
            record_columns = _table_columns(connection, "regulatory_records")
            evidence_columns = _table_columns(
                connection, "regulatory_field_evidence"
            )
            extraction_columns = _table_columns(
                connection, "document_extractions"
            )
            if not {"id"}.issubset(document_columns):
                return _empty_report(
                    selected_filters,
                    top_limit,
                    evidence_limit,
                    "documents_unavailable",
                )

            document_clauses, document_parameters, document_unsupported = (
                _selected_document_sql(
                    selected_filters, document_columns, record_columns
                )
            )
            record_clauses, record_parameters, record_unsupported = (
                _record_selection_sql(
                    selected_filters, document_columns, record_columns
                )
            )
            unsupported = list(
                dict.fromkeys(document_unsupported + record_unsupported)
            )
            if unsupported:
                report = _empty_report(
                    selected_filters,
                    top_limit,
                    evidence_limit,
                    "unsupported_filter",
                )
                report["filter_compatibility"] = {
                    "available": False,
                    "unsupported": unsupported,
                }
                return report

            document_where = _where(document_clauses)
            document_row = connection.execute(
                "SELECT COUNT(*) AS documents, "
                f"COUNT(DISTINCT {_unique_act_expression(document_columns)}) "
                "AS unique_acts FROM documents d"
                + document_where,
                document_parameters,
            ).fetchone()
            document_total = int(document_row["documents"])
            unique_acts = int(document_row["unique_acts"])

            records_available = {"id", "document_id"}.issubset(record_columns)
            record_total: int | None = None
            if records_available:
                record_total = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM regulatory_records r "
                        "JOIN documents d ON d.id=r.document_id"
                        + _where(record_clauses),
                        record_parameters,
                    ).fetchone()[0]
                )

            # Cobertura usa solo filtros documentales. Un filtro por principio
            # activo no puede aplicarse honestamente a documentos sin ficha.
            coverage_clauses, coverage_parameters, _ = _document_filter_sql(
                selected_filters, document_columns
            )
            coverage_where = _where(coverage_clauses)
            coverage_documents = int(
                connection.execute(
                    "SELECT COUNT(*) FROM documents d" + coverage_where,
                    coverage_parameters,
                ).fetchone()[0]
            )
            with_records: int | None = None
            without_records: int | None = None
            if records_available:
                with_records = int(
                    connection.execute(
                        "SELECT COUNT(DISTINCT d.id) FROM documents d "
                        "JOIN regulatory_records r ON r.document_id=d.id"
                        + coverage_where,
                        coverage_parameters,
                    ).fetchone()[0]
                )
                without_records = max(coverage_documents - with_records, 0)

            processed: int | None = None
            without_completed: int | None = None
            processed_without_records: int | None = None
            if {"document_id", "status"}.issubset(extraction_columns):
                processed = int(
                    connection.execute(
                        "SELECT COUNT(DISTINCT d.id) FROM documents d "
                        "JOIN document_extractions e ON e.document_id=d.id "
                        + (_where(coverage_clauses) if coverage_clauses else " WHERE 1=1")
                        + " AND e.status='complete'",
                        coverage_parameters,
                    ).fetchone()[0]
                )
                without_completed = max(coverage_documents - processed, 0)
                if records_available:
                    processed_without_records = int(
                        connection.execute(
                            "SELECT COUNT(DISTINCT d.id) FROM documents d "
                            "JOIN document_extractions e ON e.document_id=d.id "
                            + (_where(coverage_clauses) if coverage_clauses else " WHERE 1=1")
                            + " AND e.status='complete' AND NOT EXISTS "
                            "(SELECT 1 FROM regulatory_records rx "
                            "WHERE rx.document_id=d.id)",
                            coverage_parameters,
                        ).fetchone()[0]
                    )

            record_where = _where(record_clauses)
            fields = (
                _record_field_coverage(
                    connection,
                    total_records=int(record_total or 0),
                    record_where=record_where,
                    record_parameters=record_parameters,
                    record_columns=record_columns,
                )
                if records_available
                else {
                    key: {
                        "available": False,
                        "records": None,
                        "present": None,
                        "missing": None,
                        "coverage_percent": None,
                    }
                    for key in (
                        "outcome",
                        "request_type",
                        "active_ingredient",
                        "interested_party",
                    )
                }
            )

            dimensions = {
                "year": _year_dimension(
                    connection,
                    filters=selected_filters,
                    document_columns=document_columns,
                    record_columns=record_columns,
                    evidence_columns=evidence_columns,
                    top_limit=top_limit,
                    evidence_limit=evidence_limit,
                )
            }
            for key in (
                "outcome",
                "request_type",
                "active_ingredient",
                "interested_party",
            ):
                dimensions[key] = _record_dimension(
                    connection,
                    key=key,
                    filters=selected_filters,
                    document_columns=document_columns,
                    record_columns=record_columns,
                    evidence_columns=evidence_columns,
                    top_limit=top_limit,
                    evidence_limit=evidence_limit,
                )
            dimensions["outcome"]["meaning"] = (
                "classification_extracted_or_derived_from_act_text"
            )
            dimensions["outcome"]["creates_new_official_decisions"] = False

            distinct_values: int | None = None
            available_value_dimensions = [
                dimensions[key]
                for key in ("active_ingredient", "interested_party")
                if dimensions[key]["available"]
            ]
            if available_value_dimensions:
                distinct_values = sum(
                    int(item["total_values"] or 0)
                    for item in available_value_dimensions
                )

            status = "available" if records_available else "partial"
            return {
                "status": status,
                "reason": None if records_available else "records_unavailable",
                "filters": selected_filters.as_dict(),
                "filter_compatibility": {
                    "available": True,
                    "unsupported": [],
                },
                "limits": {
                    "top_values": top_limit,
                    "evidence_per_value": evidence_limit,
                },
                "totals": {
                    "documents": _metric(document_total, "documents"),
                    "unique_acts": _metric(unique_acts, "acts"),
                    "records": _metric(
                        record_total,
                        "extracted_records",
                        available=records_available,
                    ),
                    "distinct_ingredient_and_party_values": _metric(
                        distinct_values,
                        "values",
                        available=distinct_values is not None,
                    ),
                },
                "coverage": {
                    "document_extraction": {
                        "available": records_available,
                        "documents": coverage_documents,
                        "documents_processed": processed,
                        "documents_with_records": with_records,
                        "documents_without_records": without_records,
                        "documents_processed_without_records": processed_without_records,
                        "documents_without_completed_extraction": without_completed,
                        "record_coverage_percent": _percent(
                            with_records, coverage_documents
                        ),
                        "scope": "document_filters_only",
                        "structured_filters_excluded": [
                            key
                            for key in (
                                "outcomes",
                                "request_types",
                                "active_ingredients",
                                "interested_parties",
                            )
                            if getattr(selected_filters, key)
                        ],
                    },
                    "fields": fields,
                },
                "dimensions": dimensions,
            }
    except sqlite3.Error:
        return _empty_report(
            selected_filters, top_limit, evidence_limit, "database_unreadable"
        )


def analytics_drilldown(
    database_path: Path,
    dimension: str,
    value: object,
    filters: AnalyticsFilters | Mapping[str, Iterable[object]] | None = None,
    *,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """Devuelve evidencia paginada de una barra/punto del tablero."""

    if dimension not in ANALYTICS_DIMENSIONS:
        raise ValueError(f"Dimension analitica no soportada: {dimension}")
    limit = max(1, min(int(limit), MAX_DRILLDOWN_LIMIT))
    offset = max(0, int(offset))
    selected_filters = _coerce_filters(filters)
    path = Path(database_path)
    result = {
        "available": False,
        "reason": "database_missing",
        "dimension": dimension,
        "value": value,
        "total": None,
        "limit": limit,
        "offset": offset,
        "has_more": None,
        "items": [],
    }
    if not path.exists():
        return result
    try:
        with connect(path) as connection:
            document_columns = _table_columns(connection, "documents")
            record_columns = _table_columns(connection, "regulatory_records")
            evidence_columns = _table_columns(
                connection, "regulatory_field_evidence"
            )
            detail = _analytics_drilldown_connection(
                connection,
                dimension=dimension,
                value=value,
                filters=selected_filters,
                document_columns=document_columns,
                record_columns=record_columns,
                evidence_columns=evidence_columns,
                limit=limit,
                offset=offset,
                count_total=True,
            )
    except sqlite3.Error:
        result["reason"] = "database_unreadable"
        return result
    result.update(detail)
    total = detail.get("total")
    result["has_more"] = (
        offset + len(detail.get("items", [])) < int(total)
        if total is not None
        else None
    )
    return result
