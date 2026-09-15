from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from services.database import (
    REGULATORY_SEARCH_SCOPES,
    connect,
    decompress_page_text,
)
from services.text_utils import normalize_phrase, normalize_text, tokenize_query


@dataclass(frozen=True)
class FacetValue:
    """Clave aplicable, conteo y etiqueta opcional para presentar una faceta."""

    value: str | int
    count: int
    label: str | None = None


@dataclass(frozen=True)
class FacetMetadata:
    """Contrato de presentacion de una dimension de facetas."""

    unit: str
    basis: str
    exact: bool
    truncated: bool
    limit: int


@dataclass(frozen=True)
class FacetSummary:
    """Conteos navegables para el universo de una consulta.

    ``exact`` describe el alcance de la busqueda, no la calidad de los campos
    extraidos. En modo textual el universo se obtiene completo desde FTS5. Si
    se entregan ``candidate_chunk_ids``, los conteos describen solamente ese
    conjunto candidato y se marcan como acotados por defecto.
    """

    facets: dict[str, tuple[FacetValue, ...]]
    total_documents: int
    total_acts: int
    total_fragments: int
    exact: bool
    scope: str
    metadata: dict[str, FacetMetadata]
    candidate_limit: int | None = None


FACET_KEYS = (
    "years",
    "sections",
    "outcomes",
    "request_types",
    "active_ingredients",
    "interested_parties",
    "products",
)

# Una consulta extremadamente común no debe bloquear la interfaz mientras se
# materializan cientos de miles de filas. El conteo previo de FTS5 es barato;
# por encima de este presupuesto se usa una muestra determinista por rowid y
# el resultado se marca, sin excepciones, como acotado/aproximado.
DEFAULT_EXACT_CHUNK_BUDGET = 120_000
DEFAULT_BOUNDED_CHUNK_LIMIT = 40_000
DEFAULT_EXACT_PAGE_BUDGET = 2_000
DEFAULT_BOUNDED_PAGE_LIMIT = 500

_DOCUMENT_FACETS = {
    "years": ("d.year", "d.year"),
    "sections": ("d.section", "d.section"),
}

_RECORD_FACETS = {
    "outcomes": ("fr.outcome_code", "fr.outcome_code"),
    "request_types": ("fr.request_type_code", "fr.request_type_code"),
}

_VALUE_FACETS = {
    "active_ingredients": "active_ingredient",
    "interested_parties": "interested_party",
    "products": "product",
}

_COMPOUND_VALUE_FIELDS = {
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
    "product": (
        "producto",
        "normalized_product_name",
        "product_name",
    ),
}

_HIGH_CARDINALITY_FACETS = frozenset(
    {"active_ingredients", "interested_parties", "products"}
)

_STRUCTURED_FILTER_KEYS = frozenset(
    {
        "outcomes",
        "request_types",
        "products",
        "active_ingredients",
        "interested_parties",
        "identifiers",
    }
)

_FIELD_FTS_COLUMNS = {
    "request": "request_text",
    "concept": "concept_text",
    "product": "product_text",
    "active_ingredient": "active_ingredient_text",
    "interested_party": "interested_party_text",
    "expediente": "expediente_text",
    "radicado": "radicado_text",
    "record": "record_text",
    "outcome": "outcome_text",
}

_FIELD_TEXT_COLUMNS = {
    "request": "rr.request_text",
    "concept": "rr.concept_text",
    "product": "rr.product_name",
    "active_ingredient": "rr.active_ingredient",
    "interested_party": "rr.interested_party",
    "expediente": "rr.expediente",
    "radicado": "rr.radicado",
    "record": (
        "COALESCE(rr.numeral, '') || CHAR(10) || "
        "COALESCE(rr.numeral_title, '') || CHAR(10) || "
        "COALESCE(rr.product_name, '') || CHAR(10) || "
        "COALESCE(rr.active_ingredient, '') || CHAR(10) || "
        "COALESCE(rr.interested_party, '') || CHAR(10) || "
        "COALESCE(rr.expediente, '') || CHAR(10) || "
        "COALESCE(rr.radicado, '') || CHAR(10) || "
        "COALESCE(rr.request_type_code, '') || CHAR(10) || "
        "COALESCE(rr.request_text, '') || CHAR(10) || "
        "COALESCE(rr.concept_text, '') || CHAR(10) || "
        "COALESCE(rr.outcome_code, '')"
    ),
    "outcome": (
        "COALESCE(rr.outcome_code, '') || CHAR(10) || "
        "COALESCE(rr.concept_text, '')"
    ),
}

# Una acta puede estar publicada en varios PDF/partes. Los conteos visibles de
# facetas usan su identidad regulatoria; si algún metadato esencial falta, el
# PDF conserva una identidad propia para evitar fusionar documentos distintos.
_ACT_KEY_SQL = """
CASE
    WHEN d.year IS NOT NULL
     AND NULLIF(TRIM(d.section), '') IS NOT NULL
     AND NULLIF(TRIM(d.acta_number), '') IS NOT NULL
    THEN printf(
        'act:%s|%d:%s|%d:%s',
        d.year,
        LENGTH(TRIM(d.section)), TRIM(d.section),
        LENGTH(TRIM(d.acta_number)), TRIM(d.acta_number)
    )
    ELSE 'document:' || d.id
END
""".strip()


def _facet_basis(
    key: str, *, field_scope: str, bounded: bool = False
) -> str:
    if key in _DOCUMENT_FACETS:
        return "document_metadata"
    if field_scope == "all" or bounded:
        return "record_overlapping_matching_page"
    return "matching_record"


def _empty_summary(
    *,
    exact: bool,
    scope: str,
    max_values: int,
    field_scope: str,
    bounded: bool = False,
) -> FacetSummary:
    return FacetSummary(
        facets={key: () for key in FACET_KEYS},
        total_documents=0,
        total_acts=0,
        total_fragments=0,
        exact=exact,
        scope=scope,
        metadata={
            key: FacetMetadata(
                unit="acts",
                basis=_facet_basis(
                    key, field_scope=field_scope, bounded=bounded
                ),
                exact=exact,
                truncated=False,
                limit=max_values if key in _HIGH_CARDINALITY_FACETS else 500,
            )
            for key in FACET_KEYS
        },
    )


def _fts_terms(query: str, *, exact_phrase: bool) -> tuple[str, str]:
    normalized = normalize_phrase(query)
    if not normalized:
        return "", ""
    if exact_phrase:
        terms = list(dict.fromkeys(normalized.split()))
        operator = " AND "
    else:
        terms = list(
            dict.fromkeys(
                atom
                for token in tokenize_query(query)
                for atom in re.findall(r"[^\W_]+", token, flags=re.UNICODE)
            )
        )
        if not terms:
            terms = list(dict.fromkeys(normalized.split()))
        operator = " OR "
    escaped = [term.replace('"', "") for term in terms if term.replace('"', "")]
    return operator.join(f'"{term}"' for term in escaped), normalized


def _fts_with_alternatives(
    base_query: str,
    alternatives: Iterable[str],
) -> str:
    """Replica el contrato OR-de-ramas-conjuntivas de la busqueda textual."""

    clauses = [f"({base_query})"] if base_query else []
    seen: set[tuple[str, ...]] = set()
    for alternative in alternatives:
        normalized = normalize_phrase(str(alternative or ""))
        terms = tuple(
            dict.fromkeys(
                atom
                for token in tokenize_query(normalized)
                for atom in re.findall(r"[^\W_]+", token, flags=re.UNICODE)
            )
        )
        if not terms:
            terms = tuple(dict.fromkeys(normalized.split()))
        if not terms or terms in seen:
            continue
        seen.add(terms)
        escaped = [term.replace('"', "") for term in terms]
        clauses.append("(" + " AND ".join(f'"{term}"' for term in escaped) + ")")
    return " OR ".join(clauses)


def _without_facet(
    filters: dict[str, list] | None,
    facet_key: str | None,
) -> dict[str, list]:
    clean = {key: list(values or ()) for key, values in (filters or {}).items()}
    if facet_key:
        clean[facet_key] = []
    return clean


def _facet_filter_sql(
    filters: dict[str, list] | None,
    *,
    ignored_facet: str | None = None,
    record_alias: str | None = None,
) -> tuple[str, list[object]]:
    """Filtros parametrizados sobre ``facet_query_universe`` (alias ``u``).

    En busquedas de campo ``u.record_id`` identifica la ficha que hizo match;
    en busquedas documentales el EXISTS conserva la semantica historica de
    exigir que la ficha coincida con la pagina encontrada.
    """

    filters = _without_facet(filters, ignored_facet)
    clauses: list[str] = []
    parameters: list[object] = []
    for key, column in (
        ("years", "d.year"),
        ("sections", "d.section"),
        ("parts", "d.part"),
        ("acta_numbers", "d.acta_number"),
    ):
        values = [value for value in filters.get(key, ()) if value not in (None, "")]
        if values:
            placeholders = ",".join("?" for _ in values)
            clauses.append(f"{column} IN ({placeholders})")
            parameters.extend(values)

    record_clauses: list[str] = []
    record_parameters: list[object] = []
    for key, column in (
        ("outcomes", "outcome_code"),
        ("request_types", "request_type_code"),
    ):
        values = [value for value in filters.get(key, ()) if value not in (None, "")]
        if values:
            placeholders = ",".join("?" for _ in values)
            alias = record_alias or "rr"
            record_clauses.append(f"{alias}.{column} IN ({placeholders})")
            record_parameters.extend(values)

    for key, column, dimension in (
        ("products", "normalized_product_name", "product"),
        (
            "active_ingredients",
            "normalized_active_ingredient",
            "active_ingredient",
        ),
        (
            "interested_parties",
            "normalized_interested_party",
            "interested_party",
        ),
    ):
        values = [normalize_text(str(value)) for value in filters.get(key, ()) if value]
        if values:
            # La UI actual admite un valor libre por estos campos. Mantener un
            # LIKE sobre el consolidado conserva ese contrato; consultar
            # tambien la clave compuesta garantiza que una opcion facetada
            # canonical/normalizada siempre pueda volver a aplicarse.
            alias = record_alias or "rr"
            record_clauses.append(
                "(EXISTS ("
                "SELECT 1 FROM facet_query_values selected_value "
                f"WHERE selected_value.record_id = {alias}.id "
                "AND selected_value.dimension = ? "
                "AND selected_value.value LIKE ?"
                f") OR {alias}.{column} LIKE ?)"
            )
            pattern = f"%{values[0]}%"
            record_parameters.extend((dimension, pattern, pattern))

    identifiers = [value for value in filters.get("identifiers", ()) if value]
    if identifiers:
        normalized_identifier = re.sub(
            r"[^a-z0-9]+", "", normalize_text(str(identifiers[0]))
        )
        alias = record_alias or "rr"
        record_clauses.append(
            f"({alias}.normalized_expediente LIKE ? "
            f"OR {alias}.normalized_radicado LIKE ?)"
        )
        record_parameters.extend(
            (f"%{normalized_identifier}%", f"%{normalized_identifier}%")
        )

    if record_clauses:
        if record_alias:
            clauses.extend(record_clauses)
        else:
            clauses.append(
            "EXISTS (SELECT 1 FROM facet_query_records rr "
            "WHERE rr.document_id = u.document_id "
            "AND " + " AND ".join(record_clauses) + ")"
        )
        parameters.extend(record_parameters)
    return (" AND " + " AND ".join(clauses) if clauses else "", parameters)


def _create_universe_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TEMP TABLE facet_query_universe (
            unit_key TEXT PRIMARY KEY,
            chunk_id INTEGER NOT NULL,
            document_id INTEGER NOT NULL,
            page_id INTEGER NOT NULL,
            page_number INTEGER NOT NULL,
            record_id INTEGER,
            match_count INTEGER NOT NULL DEFAULT 1
        ) WITHOUT ROWID
        """
    )
    connection.execute(
        "CREATE INDEX facet_universe_document "
        "ON facet_query_universe(document_id, page_number)"
    )
    connection.execute(
        """
        CREATE TEMP TABLE facet_query_records (
            id INTEGER PRIMARY KEY,
            document_id INTEGER NOT NULL,
            outcome_code TEXT,
            request_type_code TEXT,
            product_name TEXT,
            normalized_product_name TEXT,
            active_ingredient TEXT,
            normalized_active_ingredient TEXT,
            interested_party TEXT,
            normalized_interested_party TEXT,
            normalized_expediente TEXT,
            normalized_radicado TEXT
        ) WITHOUT ROWID
        """
    )
    connection.execute(
        "CREATE INDEX facet_records_document "
        "ON facet_query_records(document_id)"
    )
    connection.execute(
        """
        CREATE TEMP TABLE facet_query_documents (
            document_id INTEGER PRIMARY KEY,
            match_count INTEGER NOT NULL
        ) WITHOUT ROWID
        """
    )
    connection.execute(
        """
        CREATE TEMP TABLE facet_query_values (
            record_id INTEGER NOT NULL,
            dimension TEXT NOT NULL,
            value TEXT NOT NULL,
            label TEXT NOT NULL,
            PRIMARY KEY(record_id, dimension, value)
        ) WITHOUT ROWID
        """
    )
    connection.execute(
        "CREATE INDEX facet_values_dimension "
        "ON facet_query_values(dimension, value, record_id)"
    )


def _insert_bounded_candidates(
    connection: sqlite3.Connection, chunk_ids: Iterable[int]
) -> int:
    ids = list(dict.fromkeys(int(value) for value in chunk_ids))
    connection.execute(
        "CREATE TEMP TABLE facet_candidate_ids "
        "(chunk_id INTEGER PRIMARY KEY) WITHOUT ROWID"
    )
    connection.executemany(
        "INSERT OR IGNORE INTO facet_candidate_ids(chunk_id) VALUES (?)",
        ((value,) for value in ids),
    )
    connection.execute(
        """
        INSERT INTO facet_query_universe (
            unit_key, chunk_id, document_id, page_id, page_number,
            record_id, match_count
        )
        SELECT 'p:' || p.id, MIN(c.id), p.document_id, p.id, p.page_number,
               NULL, COUNT(*)
        FROM facet_candidate_ids selected
        JOIN chunks c ON c.id = selected.chunk_id
        JOIN pages p ON p.id = c.page_id
        GROUP BY p.id
        """
    )
    return len(ids)


def _insert_textual_universe(
    connection: sqlite3.Connection,
    *,
    fts_query: str,
    chunk_limit: int | None = None,
) -> None:
    limit_clause = ""
    parameters: list[object] = [fts_query]
    if chunk_limit is not None:
        limit_clause = "ORDER BY chunks_fts.rowid LIMIT ?"
        parameters.append(chunk_limit)
    connection.execute(
        f"""
        INSERT INTO facet_query_universe (
            unit_key, chunk_id, document_id, page_id, page_number,
            record_id, match_count
        )
        SELECT 'p:' || matched.page_id, MIN(matched.chunk_id),
               matched.document_id, matched.page_id, matched.page_number,
               NULL, COUNT(*)
        FROM (
            SELECT c.id AS chunk_id, p.document_id, p.id AS page_id,
                   p.page_number
            FROM chunks_fts
            JOIN chunks c ON c.id = chunks_fts.rowid
            JOIN pages p ON p.id = c.page_id
            WHERE chunks_fts MATCH ?
            {limit_clause}
        ) AS matched
        GROUP BY matched.page_id
        """,
        parameters,
    )


def _insert_exact_page_universe(
    connection: sqlite3.Connection, chunk_ids: Iterable[int]
) -> None:
    ids = list(dict.fromkeys(int(value) for value in chunk_ids))
    for start in range(0, len(ids), 800):
        batch = ids[start : start + 800]
        if not batch:
            continue
        placeholders = ",".join("?" for _ in batch)
        connection.execute(
            f"""
            INSERT OR IGNORE INTO facet_query_universe (
                unit_key, chunk_id, document_id, page_id, page_number,
                record_id, match_count
            )
            SELECT 'p:' || p.id, MIN(c.id), p.document_id, p.id,
                   p.page_number, NULL, 1
            FROM chunks c
            JOIN pages p ON p.id = c.page_id
            WHERE c.id IN ({placeholders})
            GROUP BY p.id
            """,
            batch,
        )


def _merge_page_chunks(values: Iterable[str], maximum_overlap: int = 400) -> str:
    chunks = [str(value) for value in values if str(value)]
    if not chunks:
        return ""
    merged = chunks[0]
    for chunk in chunks[1:]:
        overlap = 0
        for size in range(min(maximum_overlap, len(merged), len(chunk)), 19, -1):
            if merged[-size:] == chunk[:size]:
                overlap = size
                break
        merged += ("" if overlap else "\n") + chunk[overlap:]
    return merged


def _bounded_exact_page_chunks(
    database_path: Path,
    query: str,
    *,
    exact_page_budget: int,
    bounded_page_limit: int,
) -> tuple[list[int], bool, int | None]:
    """Confirma una frase sin cargar un número ilimitado de páginas.

    La palabra menos frecuente es una sonda completa: toda página que contiene
    la frase debe contenerla. Si esa sonda supera el presupuesto se revisa un
    prefijo estable de páginas y el consumidor recibe ``exact=False``.
    """

    normalized_phrase = normalize_phrase(query)
    terms = list(dict.fromkeys(normalized_phrase.split()))
    if not terms:
        return [], True, None
    # La frase completa siempre se valida después. Cualquier palabra de la
    # frase es una sonda completa, así que basta medir una muestra estable para
    # elegir una razonablemente selectiva. Esto limita a ocho consultas FTS
    # incluso si el usuario pega un párrafo entero.
    if len(terms) <= 8:
        probe_terms = terms
    else:
        probe_terms = [
            terms[round(index * (len(terms) - 1) / 7)]
            for index in range(8)
        ]
    with connect(database_path) as connection:
        frequencies: list[tuple[int, str]] = []
        for term in probe_terms:
            escaped = term.replace('"', "")
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM chunks_fts "
                    "WHERE chunks_fts MATCH ?",
                    (f'text : "{escaped}"',),
                ).fetchone()[0]
            )
            if count == 0:
                return [], True, None
            frequencies.append((count, escaped))
        _, probe_term = min(frequencies, key=lambda item: (item[0], item[1]))
        cursor = connection.execute(
            """
            SELECT c.id AS chunk_id, p.id AS page_id
            FROM chunks_fts
            JOIN chunks c ON c.id = chunks_fts.rowid
            JOIN pages p ON p.id = c.page_id
            WHERE chunks_fts MATCH ?
            ORDER BY c.id
            """,
            (f'text : "{probe_term}"',),
        )
        page_anchors: dict[int, int] = {}
        exhaustive = True
        while True:
            rows = cursor.fetchmany(1_000)
            if not rows:
                break
            for row in rows:
                page_id = int(row["page_id"])
                page_anchors.setdefault(page_id, int(row["chunk_id"]))
                if len(page_anchors) > exact_page_budget:
                    exhaustive = False
                    break
            if not exhaustive:
                break
        ordered_page_ids = sorted(page_anchors)
        scan_limit = len(ordered_page_ids) if exhaustive else min(
            bounded_page_limit, exact_page_budget
        )
        selected_page_ids = ordered_page_ids[:scan_limit]

        page_rows: dict[int, sqlite3.Row] = {}
        chunks_by_page: dict[int, list[str]] = {}
        for start in range(0, len(selected_page_ids), 800):
            batch = selected_page_ids[start : start + 800]
            placeholders = ",".join("?" for _ in batch)
            for row in connection.execute(
                f"""
                SELECT id AS page_id, raw_text_compressed, raw_text_codec
                FROM pages
                WHERE id IN ({placeholders})
                """,
                batch,
            ).fetchall():
                page_rows[int(row["page_id"])] = row
            for row in connection.execute(
                f"""
                SELECT page_id, text
                FROM chunks
                WHERE page_id IN ({placeholders})
                ORDER BY page_id, chunk_index, id
                """,
                batch,
            ).fetchall():
                chunks_by_page.setdefault(int(row["page_id"]), []).append(
                    str(row["text"])
                )

        matches: list[int] = []
        for page_id in selected_page_ids:
            row = page_rows.get(page_id)
            if row is None:
                continue
            page_text: str | None = None
            if row["raw_text_compressed"] is not None:
                try:
                    page_text = decompress_page_text(
                        row["raw_text_compressed"],
                        str(row["raw_text_codec"] or ""),
                    )
                except ValueError:
                    page_text = None
            if page_text is None:
                page_text = _merge_page_chunks(chunks_by_page.get(page_id, ()))
            if normalized_phrase in normalize_phrase(page_text or ""):
                matches.append(page_anchors[page_id])
    return matches, exhaustive, (None if exhaustive else scan_limit)


def _insert_field_universe(
    connection: sqlite3.Connection,
    *,
    field_scope: str,
    fts_query: str,
    normalized_phrase: str,
    exact_phrase: bool,
) -> None:
    fts_column = _FIELD_FTS_COLUMNS[field_scope]
    field_text = _FIELD_TEXT_COLUMNS[field_scope]
    exact_clause = ""
    parameters: list[object] = [f"{fts_column} : ({fts_query})"]
    if exact_phrase:
        connection.create_function(
            "facet_phrase_contains",
            2,
            lambda value, phrase: int(
                bool(phrase)
                and str(phrase) in normalize_phrase(str(value or ""))
            ),
            deterministic=True,
        )
        exact_clause = f" AND facet_phrase_contains({field_text}, ?) = 1"
        parameters.append(normalized_phrase)
    connection.execute(
        f"""
        INSERT OR IGNORE INTO facet_query_universe (
            unit_key, chunk_id, document_id, page_id, page_number,
            record_id, match_count
        )
        SELECT 'r:' || rr.id,
               (SELECT MIN(c.id) FROM chunks c WHERE c.page_id = p.id),
               rr.document_id, p.id, p.page_number, rr.id, 1
        FROM regulatory_records_fts
        JOIN regulatory_records rr ON rr.id = regulatory_records_fts.rowid
        JOIN pages p ON p.document_id = rr.document_id
                    AND p.page_number = rr.page_number
        WHERE regulatory_records_fts MATCH ?
          AND (SELECT MIN(c.id) FROM chunks c WHERE c.page_id = p.id) IS NOT NULL
          {exact_clause}
        """,
        parameters,
    )


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
    )


_FACET_RECORD_COLUMNS = """
    id, document_id, outcome_code, request_type_code,
    product_name, normalized_product_name,
    active_ingredient, normalized_active_ingredient,
    interested_party, normalized_interested_party,
    normalized_expediente, normalized_radicado
""".strip()


def _finalize_universe(
    connection: sqlite3.Connection,
    *,
    structured_available: bool,
) -> None:
    """Deduplica páginas a documentos y fichas una sola vez por consulta."""

    connection.execute(
        """
        INSERT INTO facet_query_documents(document_id, match_count)
        SELECT document_id, SUM(match_count)
        FROM facet_query_universe
        GROUP BY document_id
        """
    )
    if not structured_available:
        return

    # Las búsquedas de campo ya conocen exactamente la ficha que coincidió.
    connection.execute(
        f"""
        INSERT OR IGNORE INTO facet_query_records ({_FACET_RECORD_COLUMNS})
        SELECT {_FACET_RECORD_COLUMNS.replace('id, document_id', 'rr.id, rr.document_id')}
        FROM facet_query_universe u
        JOIN regulatory_records rr ON rr.id = u.record_id
        WHERE u.record_id IS NOT NULL
        """
    )
    # En texto libre se conservan únicamente fichas cuyo rango regulatorio
    # incluye una página coincidente; múltiples fragmentos/páginas no vuelven
    # a multiplicar la misma ficha.
    connection.execute(
        f"""
        INSERT OR IGNORE INTO facet_query_records ({_FACET_RECORD_COLUMNS})
        SELECT {_FACET_RECORD_COLUMNS.replace('id, document_id', 'rr.id, rr.document_id')}
        FROM facet_query_universe u
        JOIN regulatory_records rr
          ON rr.document_id = u.document_id
         AND u.page_number BETWEEN rr.page_number AND rr.end_page_number
        WHERE u.record_id IS NULL
        """
    )

    evidence_columns = {
        str(row["name"])
        for row in connection.execute(
            "PRAGMA table_info(regulatory_field_evidence)"
        ).fetchall()
    }
    evidence_ready = {
        "record_id",
        "field_name",
        "canonical_value",
        "normalized_value",
        "literal_value",
    }.issubset(evidence_columns)
    evidence_value = (
        "LOWER(COALESCE(NULLIF(TRIM(fe.canonical_value), ''), "
        "NULLIF(TRIM(fe.normalized_value), ''), "
        "NULLIF(TRIM(fe.literal_value), ''), ''))"
    )
    evidence_label = (
        "COALESCE(NULLIF(TRIM(fe.literal_value), ''), "
        "NULLIF(TRIM(fe.canonical_value), ''), "
        "NULLIF(TRIM(fe.normalized_value), ''), '')"
    )
    for dimension, (
        evidence_field,
        normalized_column,
        literal_column,
    ) in _COMPOUND_VALUE_FIELDS.items():
        if evidence_ready:
            connection.execute(
                f"""
                INSERT OR IGNORE INTO facet_query_values (
                    record_id, dimension, value, label
                )
                SELECT fe.record_id, ?, {evidence_value},
                       MIN({evidence_label})
                FROM regulatory_field_evidence fe
                JOIN facet_query_records fr ON fr.id = fe.record_id
                WHERE fe.field_name = ? AND {evidence_value} != ''
                GROUP BY fe.record_id, {evidence_value}
                """,
                (dimension, evidence_field),
            )
        # El fallback es por ficha y campo: tener evidencia para otro campo no
        # impide usar el valor consolidado, y una evidencia duplicada jamás
        # crea una segunda categoría para la misma ficha/valor.
        connection.execute(
            f"""
            INSERT OR IGNORE INTO facet_query_values (
                record_id, dimension, value, label
            )
            SELECT fr.id, ?, fr.{normalized_column}, fr.{literal_column}
            FROM facet_query_records fr
            WHERE NULLIF(TRIM(fr.{normalized_column}), '') IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM facet_query_values fv
                  WHERE fv.record_id = fr.id AND fv.dimension = ?
              )
            """,
            (dimension, dimension),
        )


def _counts_for_dimension(
    connection: sqlite3.Connection,
    facet_key: str,
    *,
    filters: dict[str, list] | None,
    structured_available: bool,
    max_values: int,
) -> tuple[tuple[FacetValue, ...], bool]:
    if facet_key in _DOCUMENT_FACETS:
        group_expression, display_expression = _DOCUMENT_FACETS[facet_key]
        source = "facet_query_documents u"
        document_reference = "u.document_id"
        filter_clause, parameters = _facet_filter_sql(
            filters, ignored_facet=facet_key
        )
    elif facet_key in _RECORD_FACETS:
        if not structured_available:
            return (), False
        group_expression, display_expression = _RECORD_FACETS[facet_key]
        source = "facet_query_records fr"
        document_reference = "fr.document_id"
        # El valor agrupado y los demas filtros regulatorios deben pertenecer
        # a la misma ficha. Compartir documento —e incluso pagina— no basta
        # para atribuir una decision a otro producto o principio activo.
        filter_clause, parameters = _facet_filter_sql(
            filters,
            ignored_facet=facet_key,
            record_alias="fr",
        )
    elif facet_key in _VALUE_FACETS:
        if not structured_available:
            return (), False
        dimension = _VALUE_FACETS[facet_key]
        group_expression, display_expression = "fv.value", "fv.label"
        source = (
            "facet_query_values fv JOIN facet_query_records fr "
            "ON fr.id = fv.record_id AND fv.dimension = ?"
        )
        document_reference = "fr.document_id"
        filter_clause, parameters = _facet_filter_sql(
            filters,
            ignored_facet=facet_key,
            record_alias="fr",
        )
        parameters = [dimension, *parameters]
    else:
        raise ValueError(f"Faceta desconocida: {facet_key}")

    if facet_key in _VALUE_FACETS:
        # `facet_value` es la clave estable que la UI debe enviar al filtro;
        # el literal humano se mantiene por separado para no romper la ida y
        # vuelta cuando contiene dosis u otra redaccion ausente del consolidado.
        value_selector = group_expression
        label_selector = f"MIN({display_expression})"
    else:
        value_selector = (
            group_expression
            if facet_key == "years"
            else f"MIN({display_expression})"
        )
        label_selector = "NULL"
    ordering = (
        "facet_value DESC"
        if facet_key == "years"
        else "document_count DESC, facet_label COLLATE NOCASE, "
        "facet_value COLLATE NOCASE"
    )
    dimension_limit = max_values if facet_key in _HIGH_CARDINALITY_FACETS else 500
    rows = connection.execute(
        f"""
        SELECT {value_selector} AS facet_value,
               {label_selector} AS facet_label,
               COUNT(DISTINCT {_ACT_KEY_SQL}) AS document_count
        FROM {source}
        JOIN documents d ON d.id = {document_reference}
        WHERE {group_expression} IS NOT NULL
          AND TRIM(CAST({group_expression} AS TEXT)) != ''
          {filter_clause}
        GROUP BY {group_expression}
        ORDER BY {ordering}
        LIMIT ?
        """,
        [*parameters, dimension_limit + 1],
    ).fetchall()
    truncated = len(rows) > dimension_limit
    values = tuple(
        FacetValue(
            value=(int(row["facet_value"]) if facet_key == "years" else str(row["facet_value"])),
            count=int(row["document_count"]),
            label=(
                str(row["facet_label"])
                if row["facet_label"] not in (None, "")
                else None
            ),
        )
        for row in rows[:dimension_limit]
    )
    return values, truncated


def get_search_facets(
    database_path: Path,
    query: str,
    *,
    filters: dict[str, list] | None = None,
    exact_phrase: bool = False,
    field_scope: str = "all",
    candidate_chunk_ids: Iterable[int] | None = None,
    query_alternatives: Iterable[str] = (),
    counts_exact: bool | None = None,
    max_values: int = 50,
    exact_chunk_budget: int = DEFAULT_EXACT_CHUNK_BUDGET,
    bounded_chunk_limit: int = DEFAULT_BOUNDED_CHUNK_LIMIT,
    exact_page_budget: int = DEFAULT_EXACT_PAGE_BUDGET,
    bounded_page_limit: int = DEFAULT_BOUNDED_PAGE_LIMIT,
) -> FacetSummary:
    """Calcula facetas globales antes de la paginacion del Explorador.

    Cada dimension omite su propio filtro y conserva la consulta y los demas
    filtros. Esto permite cambiar de año o resultado sin tener que limpiar
    primero la seleccion vigente. Los conteos representan actas distintas.

    ``candidate_chunk_ids`` permite reutilizar un conjunto recuperado por ANN.
    En ese caso los conteos son acotados y nunca se presentan como globales por
    omision. Sin candidatos se recorre el universo textual completo de FTS5.
    """

    if field_scope != "all" and field_scope not in REGULATORY_SEARCH_SCOPES:
        raise ValueError(f"Campo regulatorio desconocido: {field_scope}")
    if max_values < 1 or max_values > 500:
        raise ValueError("max_values debe estar entre 1 y 500")
    if exact_chunk_budget < 1:
        raise ValueError("exact_chunk_budget debe ser mayor que cero")
    if bounded_chunk_limit < 1:
        raise ValueError("bounded_chunk_limit debe ser mayor que cero")
    if exact_page_budget < 1:
        raise ValueError("exact_page_budget debe ser mayor que cero")
    if bounded_page_limit < 1:
        raise ValueError("bounded_page_limit debe ser mayor que cero")
    bounded = candidate_chunk_ids is not None
    default_exact = not bounded
    exact = False if bounded else (
        default_exact if counts_exact is None else bool(counts_exact)
    )
    scope = "bounded_candidates" if bounded else (
        "full_textual" if exact else "textual_projection"
    )
    if not database_path.exists():
        return _empty_summary(
            exact=exact,
            scope=scope,
            max_values=max_values,
            field_scope=field_scope,
            bounded=bounded,
        )
    query = query.strip()
    fts_query, normalized_phrase = _fts_terms(query, exact_phrase=exact_phrase)
    if not exact_phrase:
        fts_query = _fts_with_alternatives(fts_query, query_alternatives)
    if not bounded and not fts_query:
        return _empty_summary(
            exact=exact,
            scope=scope,
            max_values=max_values,
            field_scope=field_scope,
            bounded=bounded,
        )

    exact_page_chunk_ids: list[int] | None = None
    exact_page_candidate_limit: int | None = None
    if not bounded and exact_phrase and field_scope == "all":
        (
            exact_page_chunk_ids,
            phrase_exhaustive,
            exact_page_candidate_limit,
        ) = _bounded_exact_page_chunks(
            database_path,
            query,
            exact_page_budget=exact_page_budget,
            bounded_page_limit=bounded_page_limit,
        )
        if not phrase_exhaustive:
            exact = False
            scope = "bounded_phrase"

    with connect(database_path) as connection:
        _create_universe_table(connection)
        structured_available = _has_table(connection, "regulatory_records")
        if not structured_available and any(
            (filters or {}).get(key) for key in _STRUCTURED_FILTER_KEYS
        ):
            return _empty_summary(
                exact=exact,
                scope=scope,
                max_values=max_values,
                field_scope=field_scope,
                bounded=bounded,
            )
        if bounded:
            bounded_ids = list(candidate_chunk_ids or ())
            candidate_limit = _insert_bounded_candidates(connection, bounded_ids)
        else:
            candidate_limit = exact_page_candidate_limit
            if exact_page_chunk_ids is not None:
                _insert_exact_page_universe(connection, exact_page_chunk_ids)
            elif field_scope == "all":
                lexical_matches = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM chunks_fts "
                        "WHERE chunks_fts MATCH ?",
                        (fts_query,),
                    ).fetchone()[0]
                )
                chunk_limit: int | None = None
                if lexical_matches > exact_chunk_budget:
                    exact = False
                    scope = "bounded_textual"
                    chunk_limit = min(bounded_chunk_limit, lexical_matches)
                    candidate_limit = chunk_limit
                _insert_textual_universe(
                    connection,
                    fts_query=fts_query,
                    chunk_limit=chunk_limit,
                )
            elif structured_available and _has_table(
                connection, "regulatory_records_fts"
            ):
                _insert_field_universe(
                    connection,
                    field_scope=field_scope,
                    fts_query=fts_query,
                    normalized_phrase=normalized_phrase,
                    exact_phrase=exact_phrase,
                )

        _finalize_universe(
            connection,
            structured_available=structured_available,
        )

        all_filter_clause, all_parameters = _facet_filter_sql(filters)
        totals = connection.execute(
            f"""
            SELECT COUNT(DISTINCT u.document_id) AS total_documents,
                   COUNT(DISTINCT {_ACT_KEY_SQL}) AS total_acts,
                   COALESCE(SUM(u.match_count), 0) AS total_fragments
            FROM facet_query_documents u
            JOIN documents d ON d.id = u.document_id
            WHERE 1 = 1 {all_filter_clause}
            """,
            all_parameters,
        ).fetchone()
        counted = {
            key: _counts_for_dimension(
                connection,
                key,
                filters=filters,
                structured_available=structured_available,
                max_values=max_values,
            )
            for key in FACET_KEYS
        }
        facet_values = {key: value[0] for key, value in counted.items()}
        facet_metadata = {
            key: FacetMetadata(
                unit="acts",
                basis=_facet_basis(
                    key, field_scope=field_scope, bounded=bounded
                ),
                exact=exact,
                truncated=value[1],
                limit=(
                    max_values if key in _HIGH_CARDINALITY_FACETS else 500
                ),
            )
            for key, value in counted.items()
        }

    return FacetSummary(
        facets=facet_values,
        total_documents=int(totals["total_documents"] or 0),
        total_acts=int(totals["total_acts"] or 0),
        total_fragments=int(totals["total_fragments"] or 0),
        exact=exact,
        scope=scope,
        candidate_limit=candidate_limit,
        metadata=facet_metadata,
    )
