"""Comparacion y cronologias verificables de decisiones regulatorias.

Este modulo no confia en el contenido guardado en ``st.session_state``. Las
selecciones del Explorador aportan unicamente los identificadores de fragmento;
los textos, metadatos y fichas se vuelven a leer de la base vigente antes de
mostrarlos o exportarlos.
"""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, replace
from datetime import date
from html import escape
from io import StringIO
from itertools import islice
import math
from pathlib import Path
import re
from typing import Iterable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from services.database import connect
from services.effective_records import display_value
from services.models import SearchResult
from services.reviews import ReviewEvent, effective_record, latest_reviews
from services.text_utils import normalize_text, tokenize_query


MAX_SELECTED_SOURCES = 24
MAX_COMPARISON_ITEMS = 16
MAX_EVIDENCE_PER_DECISION = 6
MAX_TIMELINE_ROWS = 250
MAX_CELL_CHARS = 12_000
MAX_CSV_BYTES = 8_000_000
MAX_HTML_BYTES = 2_000_000
MAX_HTML_TIMELINE_DETAILS = 50
MAX_HTML_TIMELINE_TEXT_CHARS = 2_000
MAX_TIMELINE_QUERY_CHARS = 500
MAX_REVIEW_EVENTS = 50_000
MAX_TIMELINE_TEXT_HITS = 1_500
MAX_DIRECT_SEARCH_PAGE_SIZE = 25
SQLITE_MAX_INTEGER = 9_223_372_036_854_775_807
HUMAN_CORRECTION_WITHOUT_SOURCE_FRAGMENT = (
    "El valor estructurado no tiene un fragmento específico asociado; "
    "verifica la página enlazada en el PDF."
)

TIMELINE_MATCH_ORIGINS = (
    "verified",
    "reviewed",
    "structured",
    "inferred",
    "textual",
)
TIMELINE_REVIEW_STATUSES = (
    "automatic",
    "reviewed",
    "approved",
    "reopened",
    "stale",
    "unstructured",
)


@dataclass(frozen=True)
class Evidence:
    """Fragmento vigente que sustenta una decision seleccionada."""

    chunk_id: int
    page: int
    text: str


@dataclass(frozen=True)
class ComparisonDecision:
    """Ficha comparable enriquecida con una o mas evidencias seleccionadas."""

    record_id: int | None
    decision_uid: str | None
    title: str
    url: str
    year: int | None
    acta_number: str | None
    section: str | None
    part: str | None
    source_type: str
    page_number: int
    end_page_number: int
    product_name: str | None = None
    active_ingredient: str | None = None
    interested_party: str | None = None
    expediente: str | None = None
    radicado: str | None = None
    request_text: str | None = None
    concept_text: str | None = None
    outcome_code: str | None = None
    confidence: float | None = None
    needs_review: bool = True
    numeral: str | None = None
    numeral_title: str | None = None
    session_date: str | None = None
    session_date_raw: str | None = None
    request_type: str | None = None
    identity_strategy: str | None = None
    review_status: str | None = None
    reviewer: str | None = None
    reviewed_at: str | None = None
    review_notes: str | None = None
    review_stale: bool = False
    evidences: tuple[Evidence, ...] = ()
    evidence_truncated: bool = False

    @property
    def key(self) -> str:
        if self.decision_uid:
            return f"decision:{self.decision_uid}"
        if self.record_id is not None:
            return f"record:{self.record_id}"
        chunk_id = self.evidences[0].chunk_id if self.evidences else 0
        return f"chunk:{chunk_id}"

    @property
    def label(self) -> str:
        identity = (
            self.product_name
            or self.active_ingredient
            or self.expediente
            or self.radicado
            or self.title
        )
        acta = f"Acta {self.acta_number}" if self.acta_number else "Acta sin numero"
        year = str(self.year) if self.year is not None else "sin ano"
        return f"{identity} · {acta} ({year}) · p. {self.page_number}"

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class TimelineEntry:
    """Un registro regulatorio ordenado dentro de una cronologia."""

    record_id: int | None
    decision_uid: str | None
    title: str
    url: str
    year: int | None
    acta_number: str | None
    section: str | None
    part: str | None
    source_type: str
    page_number: int
    end_page_number: int
    product_name: str | None
    active_ingredient: str | None
    interested_party: str | None
    expediente: str | None
    radicado: str | None
    request_text: str | None
    concept_text: str | None
    outcome_code: str
    numeral: str | None = None
    numeral_title: str | None = None
    session_date: str | None = None
    session_date_raw: str | None = None
    request_type: str | None = None
    identity_strategy: str | None = None
    review_status: str | None = None
    reviewer: str | None = None
    reviewed_at: str | None = None
    review_notes: str | None = None
    review_stale: bool = False
    match_origin: str = "structured"
    match_evidence: str | None = None
    confidence: float | None = None
    match_page: int | None = None
    match_evidences: tuple[Evidence, ...] = ()

    @property
    def review_identifier(self) -> str:
        """Identificador portable para ubicar la ficha o la mención."""

        if self.decision_uid:
            return str(self.decision_uid)
        if self.record_id is not None:
            return f"record:{self.record_id}"
        if self.match_evidences:
            return f"fragmento:{self.match_evidences[0].chunk_id}"
        return f"mencion:{self.year or 0}:{self.acta_number or 's-a'}:{self.page_number}"

    @property
    def page(self) -> int:
        """Página exacta de la coincidencia (alias estable para la interfaz)."""

        return int(self.match_page or self.page_number)

    @property
    def label(self) -> str:
        acta = f"Acta {self.acta_number}" if self.acta_number else "Acta sin numero"
        year = str(self.year) if self.year is not None else "sin ano"
        return f"{year} · {acta} · pagina {self.page_number}"

    def as_dict(self) -> dict:
        value = asdict(self)
        value["page"] = self.page
        value["review_identifier"] = self.review_identifier
        return value


@dataclass(frozen=True)
class ComparisonSearchItem:
    """Ficha localizada directamente desde la página de comparación."""

    record_id: int
    decision_uid: str
    title: str
    year: int | None
    acta_number: str | None
    page_number: int
    product_name: str | None
    active_ingredient: str | None
    interested_party: str | None
    expediente: str | None
    radicado: str | None
    request_type: str | None
    outcome_code: str | None
    selection: dict[str, object]

    @property
    def key(self) -> str:
        return f"decision:{self.decision_uid}"

    @property
    def label(self) -> str:
        identity = (
            self.product_name
            or self.active_ingredient
            or self.expediente
            or self.radicado
            or self.title
        )
        acta = f"Acta {self.acta_number}" if self.acta_number else "Acta sin número"
        year = str(self.year) if self.year is not None else "sin año"
        return f"{identity} · {acta} ({year}) · p. {self.page_number}"


@dataclass(frozen=True)
class ComparisonSearchPage:
    """Página exacta de fichas disponibles para añadir a una comparación."""

    items: tuple[ComparisonSearchItem, ...]
    total_matches: int
    page: int
    page_size: int
    total_pages: int


@dataclass(frozen=True)
class TimelineSearchPage:
    """Ventana paginada de una cronología y su estado de truncamiento."""

    entries: tuple[TimelineEntry, ...]
    total_loaded: int
    page: int
    page_size: int
    total_pages: int
    truncated: bool


def comparison_source_payload(
    item: ComparisonDecision | TimelineEntry,
    *,
    page: int | None = None,
    text: str | None = None,
) -> dict[str, object]:
    """Crea el mapping mínimo y trazable que consume el visor compartido."""

    target_page = int(
        page
        or getattr(item, "match_page", None)
        or item.page_number
    )
    evidences = tuple(getattr(item, "match_evidences", ())) or tuple(
        getattr(item, "evidences", ())
    )
    selected_evidence = next(
        (evidence for evidence in evidences if evidence.page == target_page),
        evidences[0] if evidences else None,
    )
    payload: dict[str, object] = {
        "title": item.title,
        "url": item.url,
        "page": target_page,
        "text": _bounded_text(
            text
            if text is not None
            else (
                selected_evidence.text
                if selected_evidence is not None
                else getattr(item, "match_evidence", "")
            )
        ),
        "year": item.year,
        "acta_number": item.acta_number,
        "section": item.section,
        "part": item.part,
        "source_type": item.source_type,
    }
    if selected_evidence is not None:
        payload["chunk_id"] = selected_evidence.chunk_id
    if item.decision_uid:
        payload["decision_uid"] = item.decision_uid
    return payload


_OPTIONAL_COLUMN_CANDIDATES = {
    "decision_uid": ("decision_uid",),
    "numeral": ("numeral", "subsection_number"),
    "numeral_title": ("numeral_title", "subsection_title"),
    "session_date": ("session_date", "decision_date"),
    "session_date_raw": ("session_date_raw",),
    "request_type_code": (
        "request_type_code",
        "request_type",
        "procedure_type",
    ),
    "identity_strategy": ("identity_strategy",),
}

_TIMELINE_FIELDS = {
    "product": ("normalized_product_name", "product_name", False, True),
    "active_ingredient": (
        "normalized_active_ingredient",
        "active_ingredient",
        False,
        True,
    ),
    "interested_party": (
        "normalized_interested_party",
        "interested_party",
        False,
        True,
    ),
    "expediente": ("normalized_expediente", "expediente", True, True),
    "radicado": ("normalized_radicado", "radicado", True, True),
    # Resultado y tipo de solicitud son clasificaciones de la ficha. Una simple
    # mención de esas palabras en el PDF no demuestra que clasifiquen la decisión.
    "outcome": ("outcome_code", "outcome_code", False, False),
    "request_type": ("request_type_code", "request_type_code", False, False),
}

# La tabla aparece a partir del esquema 0.7. Los alias mantienen la cronología
# compatible con bases 0.6 y con migraciones candidatas que usaron nombres
# provisionales durante el desarrollo.
_FIELD_EVIDENCE_COLUMN_CANDIDATES = {
    "record_id": ("record_id", "regulatory_record_id"),
    "field_name": ("field_name",),
    "ordinal": ("ordinal",),
    "value_literal": ("value_literal", "literal_value", "value"),
    "value_normalized": ("value_normalized", "normalized_value"),
    "value_canonical": ("value_canonical", "canonical_value"),
    "page_number": ("page_number", "source_page"),
    "end_page_number": ("end_page_number",),
    "evidence_text": ("evidence_text", "source_text"),
    "method": ("method", "extraction_method"),
    "confidence": ("confidence",),
}

_FIELD_EVIDENCE_NAMES = {
    "product": ("product_name", "product", "producto"),
    "active_ingredient": ("active_ingredient", "principio_activo"),
    "interested_party": ("interested_party", "interesado", "titular"),
    "expediente": ("expediente",),
    "radicado": ("radicado",),
    "outcome": ("outcome_code", "resultado"),
    "request_type": ("request_type_code", "tipo_solicitud"),
}

_MATCH_ORIGIN_PRIORITY = {
    "verified": 0,
    "reviewed": 1,
    "structured": 2,
    "inferred": 3,
    "textual": 4,
}

_INFERRED_METHOD_MARKERS = (
    "infer",
    "alias",
    "dictionary",
    "association",
    "propagat",
)


def _table_exists(connection, table_name: str) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
    )


def _record_columns(connection) -> set[str]:
    if not _table_exists(connection, "regulatory_records"):
        return set()
    return {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(regulatory_records)")
    }


def _field_evidence_columns(connection) -> dict[str, str]:
    """Resuelve únicamente columnas conocidas de la tabla opcional de 0.7."""

    if not _table_exists(connection, "regulatory_field_evidence"):
        return {}
    available = {
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info(regulatory_field_evidence)"
        )
    }
    resolved: dict[str, str] = {}
    for output_name, candidates in _FIELD_EVIDENCE_COLUMN_CANDIDATES.items():
        source = next((name for name in candidates if name in available), None)
        if source:
            resolved[output_name] = source
    required = {"record_id", "field_name", "value_literal"}
    return resolved if required.issubset(resolved) else {}


def _hydrate_rows_with_field_evidence(
    connection,
    rows: Iterable[Mapping[str, object]],
    cache: dict[int, list[dict[str, object]]] | None = None,
) -> list[dict[str, object]]:
    """Adjunta todas las evidencias antes de resolver el valor vigente."""

    hydrated = [dict(row) for row in rows]
    evidence_columns = _field_evidence_columns(connection)
    if not evidence_columns:
        return hydrated
    evidence_cache = cache if cache is not None else {}
    record_ids = {
        int(row["record_id"])
        for row in hydrated
        if row.get("record_id") is not None
    }
    missing_ids = sorted(record_ids - set(evidence_cache))

    def selected(name: str, output_name: str) -> str:
        column = evidence_columns.get(name)
        return f"f.{column} AS {output_name}" if column else f"NULL AS {output_name}"

    for start in range(0, len(missing_ids), 800):
        batch = missing_ids[start : start + 800]
        for record_id in batch:
            evidence_cache.setdefault(record_id, [])
        placeholders = ",".join("?" for _ in batch)
        query = (
            f"SELECT f.{evidence_columns['record_id']} AS record_id, "
            f"f.{evidence_columns['field_name']} AS field_name, "
            + selected("ordinal", "ordinal")
            + ", "
            + selected("value_literal", "literal_value")
            + ", "
            + selected("value_normalized", "normalized_value")
            + ", "
            + selected("value_canonical", "canonical_value")
            + ", "
            + selected("page_number", "page_number")
            + ", "
            + selected("end_page_number", "end_page_number")
            + ", "
            + selected("evidence_text", "evidence_text")
            + ", "
            + selected("method", "extraction_method")
            + ", "
            + selected("confidence", "confidence")
            + " FROM regulatory_field_evidence f "
            + f"WHERE f.{evidence_columns['record_id']} IN ({placeholders}) "
            + f"ORDER BY f.{evidence_columns['record_id']}, "
            + (
                f"f.{evidence_columns['ordinal']}"
                if evidence_columns.get("ordinal")
                else f"f.{evidence_columns['field_name']}"
            )
        )
        for evidence in connection.execute(query, batch):
            evidence_cache[int(evidence["record_id"])].append(dict(evidence))
    for row in hydrated:
        record_id = row.get("record_id")
        if record_id is not None and evidence_cache.get(int(record_id)):
            row["field_evidence"] = list(evidence_cache[int(record_id)])
    return hydrated


def _optional_selects(columns: set[str], alias: str = "r") -> list[str]:
    selects: list[str] = []
    for output_name, candidates in _OPTIONAL_COLUMN_CANDIDATES.items():
        source = next((value for value in candidates if value in columns), None)
        if source:
            selects.append(f"{alias}.{source} AS {output_name}")
        else:
            selects.append(f"NULL AS {output_name}")
    return selects


def _selected_chunk_ids(
    selected_sources: Iterable[Mapping[str, object]],
) -> list[int]:
    ids: list[int] = []
    seen: set[int] = set()
    for source in selected_sources:
        if not isinstance(source, Mapping):
            continue
        try:
            chunk_id = int(source.get("chunk_id", 0))
        except (TypeError, ValueError):
            continue
        if (
            chunk_id <= 0
            or chunk_id > SQLITE_MAX_INTEGER
            or chunk_id in seen
        ):
            continue
        seen.add(chunk_id)
        ids.append(chunk_id)
        if len(ids) >= MAX_SELECTED_SOURCES:
            break
    return ids


def _regulatory_rows_for_chunks(
    connection,
    chunk_ids: Sequence[int],
    review_events: Iterable[ReviewEvent] = (),
):
    """Asocia fichas efectivas, incluidas páginas corregidas, con fragmentos."""

    if not chunk_ids:
        return {}
    grouped: dict[int, list[dict]] = {chunk_id: [] for chunk_id in chunk_ids}
    current_reviews = latest_reviews(review_events)
    columns = _record_columns(connection)
    if not columns:
        return grouped
    optional_sql = ",\n                       ".join(_optional_selects(columns))
    chunks_by_document: dict[int, list[tuple[int, int]]] = {}
    evidence_page_by_chunk: dict[int, int] = {}
    for start in range(0, len(chunk_ids), 800):
        batch = list(chunk_ids[start : start + 800])
        placeholders = ",".join("?" for _ in batch)
        chunk_rows = connection.execute(
            f"""
            SELECT c.id AS chunk_id, p.document_id, p.page_number
            FROM chunks c
            JOIN pages p ON p.id = c.page_id
            WHERE c.id IN ({placeholders})
            """,
            batch,
        ).fetchall()
        for row in chunk_rows:
            chunk_id = int(row["chunk_id"])
            evidence_page = int(row["page_number"])
            chunks_by_document.setdefault(int(row["document_id"]), []).append(
                (chunk_id, evidence_page)
            )
            evidence_page_by_chunk[chunk_id] = evidence_page

    document_ids = list(chunks_by_document)
    for start in range(0, len(document_ids), 800):
        batch = document_ids[start : start + 800]
        placeholders = ",".join("?" for _ in batch)
        rows = connection.execute(
            f"""
            SELECT r.document_id, r.id AS record_id, r.record_key,
                   r.page_number, r.end_page_number, r.product_name,
                   r.active_ingredient, r.interested_party, r.expediente,
                   r.radicado, r.request_text, r.concept_text,
                   r.outcome_code, r.confidence, r.needs_review,
                   d.document_hash,
                   {optional_sql}
            FROM regulatory_records r
            JOIN documents d ON d.id = r.document_id
            WHERE r.document_id IN ({placeholders})
            ORDER BY r.document_id, r.page_number, r.id
            """,
            batch,
        ).fetchall()
        hydrated_rows = _hydrate_rows_with_field_evidence(connection, rows)
        for raw in hydrated_rows:
            uid = str(raw.get("decision_uid") or "")
            record = effective_record(raw, current_reviews.get(uid))
            page_start = int(record["page_number"])
            page_end = int(record["end_page_number"] or page_start)
            for chunk_id, evidence_page in chunks_by_document.get(
                int(raw["document_id"]),
                [],
            ):
                if (
                    page_start <= evidence_page <= page_end
                    and len(grouped[chunk_id]) < MAX_COMPARISON_ITEMS
                ):
                    grouped[chunk_id].append(record)

    for chunk_id, records in grouped.items():
        records.sort(
            key=lambda record: (
                0
                if int(record["page_number"]) == evidence_page_by_chunk[chunk_id]
                else 1,
                int(record["end_page_number"] or record["page_number"])
                - int(record["page_number"]),
                int(record["record_id"]),
            )
        )
    return grouped


def _get_chunks_by_ids_in_snapshot(
    connection,
    chunk_ids: Sequence[int],
) -> list[SearchResult]:
    rows_by_id = {}
    for start in range(0, len(chunk_ids), 800):
        batch = list(chunk_ids[start : start + 800])
        placeholders = ",".join("?" for _ in batch)
        rows = connection.execute(
            f"""
            SELECT c.id AS chunk_id, c.text, p.page_number,
                   d.title, d.url, d.year, d.acta_number, d.section,
                   d.part, d.source_type
            FROM chunks c
            JOIN pages p ON p.id = c.page_id
            JOIN documents d ON d.id = p.document_id
            WHERE c.id IN ({placeholders})
            """,
            batch,
        ).fetchall()
        rows_by_id.update({int(row["chunk_id"]): row for row in rows})
    results = []
    for position, chunk_id in enumerate(chunk_ids):
        row = rows_by_id.get(chunk_id)
        if row is None:
            continue
        results.append(
            SearchResult(
                chunk_id=chunk_id,
                title=row["title"],
                url=row["url"],
                page=int(row["page_number"]),
                text=row["text"],
                year=row["year"],
                acta_number=row["acta_number"],
                section=row["section"],
                part=row["part"],
                source_type=row["source_type"],
                score=1.0 - (position / max(len(chunk_ids), 1)),
            )
        )
    return results


def _bounded_text(value: object, max_chars: int = MAX_CELL_CHARS) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\x00", "")
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)] + "…"


def _bounded_review_events(
    review_events: Iterable[ReviewEvent],
) -> list[ReviewEvent]:
    """Materializa un registro de revisiones con un limite predecible."""

    values = list(islice(review_events, MAX_REVIEW_EVENTS + 1))
    if len(values) > MAX_REVIEW_EVENTS:
        raise ValueError(
            f"El registro supera el limite de {MAX_REVIEW_EVENTS} revisiones"
        )
    return values


def load_selected_decisions(
    database_path: Path,
    selected_sources: Iterable[Mapping[str, object]],
    *,
    review_events: Iterable[ReviewEvent] = (),
) -> list[ComparisonDecision]:
    """Recarga y enriquece evidencias seleccionadas usando la base vigente.

    Un mismo registro puede estar respaldado por varios fragmentos seleccionados;
    en ese caso se devuelve una sola decision con todas esas evidencias. Los
    fragmentos sin ficha estructurada tambien se conservan para comparacion.
    """

    stored_sources = list(islice(selected_sources, MAX_SELECTED_SOURCES))
    chunk_ids = _selected_chunk_ids(stored_sources)
    if not chunk_ids or not database_path.exists():
        return []
    snapshots: dict[int, list[Mapping[str, object]]] = {}
    for source in stored_sources:
        if not isinstance(source, Mapping):
            continue
        try:
            chunk_id = int(source.get("chunk_id", 0))
        except (TypeError, ValueError):
            continue
        snapshots.setdefault(chunk_id, []).append(source)

    review_values = _bounded_review_events(review_events)
    with connect(database_path) as connection:
        connection.execute("BEGIN")
        current_sources = _get_chunks_by_ids_in_snapshot(connection, chunk_ids)
        # Si la seleccion contiene un snapshot completo (como los creados por
        # el Explorador), se verifica para evitar que un ID reciclado despues
        # de una reconstruccion apunte silenciosamente a otro fragmento.
        identity_fields = (
            "title",
            "url",
            "page",
            "text",
            "year",
            "acta_number",
            "section",
            "part",
            "source_type",
        )
        verified_sources = []
        requested_uids_by_chunk: dict[int, set[str] | None] = {}
        for source in current_sources:
            current = source.as_dict()
            valid_snapshots = [
                snapshot
                for snapshot in snapshots.get(source.chunk_id, [])
                if not any(
                    field in snapshot and snapshot.get(field) != current.get(field)
                    for field in identity_fields
                )
            ]
            if not valid_snapshots:
                continue
            verified_sources.append(source)
            # Un fragmento normal, sin UID, solicita todas las fichas que lo
            # solapan. Las selecciones provenientes del historial sí pueden
            # solicitar varias fichas distintas sobre el mismo fragmento.
            if any(
                not str(snapshot.get("decision_uid") or "").strip()
                for snapshot in valid_snapshots
            ):
                requested_uids_by_chunk[source.chunk_id] = None
            else:
                requested_uids_by_chunk[source.chunk_id] = {
                    str(snapshot.get("decision_uid") or "").strip()
                    for snapshot in valid_snapshots
                    if str(snapshot.get("decision_uid") or "").strip()
                }
        current_sources = verified_sources
        structured = _regulatory_rows_for_chunks(
            connection,
            [source.chunk_id for source in current_sources],
            review_values,
        )

    decisions: list[ComparisonDecision] = []
    positions: dict[str, int] = {}
    for source in current_sources:
        evidence = Evidence(
            chunk_id=source.chunk_id,
            page=source.page,
            text=_bounded_text(source.text),
        )
        records = structured.get(source.chunk_id, [])
        requested_uids = requested_uids_by_chunk.get(source.chunk_id)
        if requested_uids is not None:
            records = [
                record
                for record in records
                if str(record.get("decision_uid") or "").strip()
                in requested_uids
            ]
        if not records:
            if requested_uids is not None:
                # Una selección dirigida a una ficha que ya no existe no se
                # degrada silenciosamente a una mención textual distinta.
                continue
            decision = ComparisonDecision(
                record_id=None,
                decision_uid=None,
                title=source.title,
                url=source.url,
                year=source.year,
                acta_number=source.acta_number,
                section=source.section,
                part=source.part,
                source_type=source.source_type,
                page_number=source.page,
                end_page_number=source.page,
                evidences=(evidence,),
            )
            decisions.append(decision)
            if len(decisions) >= MAX_COMPARISON_ITEMS:
                break
            continue

        # Una pagina puede solaparse con mas de una ficha. Se preservan como
        # decisiones independientes, pero se deduplican los registros ya vistos.
        for record in records:
            record_uid = str(record.get("decision_uid") or "").strip()
            key = (
                f"decision:{record_uid}"
                if record_uid
                else f"record:{int(record['record_id'])}"
            )
            existing_position = positions.get(key)
            if existing_position is not None:
                existing = decisions[existing_position]
                evidence_is_new = evidence.chunk_id not in {
                    item.chunk_id for item in existing.evidences
                }
                if len(existing.evidences) < MAX_EVIDENCE_PER_DECISION and evidence_is_new:
                    decisions[existing_position] = replace(
                        existing,
                        evidences=(*existing.evidences, evidence),
                    )
                elif evidence_is_new and not existing.evidence_truncated:
                    decisions[existing_position] = replace(
                        existing,
                        evidence_truncated=True,
                    )
                continue
            decision = ComparisonDecision(
                record_id=int(record["record_id"]),
                decision_uid=record_uid or None,
                title=source.title,
                url=source.url,
                year=source.year,
                acta_number=source.acta_number,
                section=source.section,
                part=source.part,
                source_type=source.source_type,
                page_number=int(record["page_number"]),
                end_page_number=int(
                    record["end_page_number"] or record["page_number"]
                ),
                product_name=record.get("product_name"),
                active_ingredient=record.get("active_ingredient"),
                interested_party=record.get("interested_party"),
                expediente=record.get("expediente"),
                radicado=record.get("radicado"),
                request_text=record.get("request_text"),
                concept_text=record.get("concept_text"),
                outcome_code=record.get("outcome_code"),
                confidence=(
                    float(record["confidence"])
                    if record.get("confidence") is not None
                    else None
                ),
                needs_review=(
                    record.get("review_status") in {"reopened", "stale"}
                    or (
                        bool(record.get("needs_review", 1))
                        and record.get("review_status")
                        not in {"reviewed", "approved"}
                    )
                ),
                numeral=record.get("numeral"),
                numeral_title=record.get("numeral_title"),
                session_date=record.get("session_date"),
                session_date_raw=record.get("session_date_raw"),
                request_type=record.get("request_type_code"),
                identity_strategy=record.get("identity_strategy"),
                review_status=record.get("review_status"),
                reviewer=record.get("reviewer"),
                reviewed_at=record.get("reviewed_at"),
                review_notes=record.get("review_notes"),
                review_stale=bool(record.get("review_stale", False)),
                evidences=(evidence,),
            )
            positions[key] = len(decisions)
            decisions.append(decision)
            if len(decisions) >= MAX_COMPARISON_ITEMS:
                return decisions
    return decisions


def search_comparison_decisions(
    database_path: Path,
    query: str,
    *,
    page: int = 1,
    page_size: int = 10,
    years: Sequence[int] | None = None,
) -> ComparisonSearchPage:
    """Busca fichas seleccionables sin obligar a volver al Explorador.

    Solo devuelve registros que tienen ``decision_uid`` y al menos un fragmento
    vigente dentro de su rango de páginas. El ``selection`` de cada elemento es
    un snapshot completo y puede pasarse directamente a
    :func:`load_selected_decisions`.
    """

    if not database_path.exists():
        return ComparisonSearchPage((), 0, 1, max(1, int(page_size)), 1)
    try:
        requested_page = int(page)
        bounded_page_size = int(page_size)
    except (TypeError, ValueError) as exc:
        raise ValueError("Parámetros de paginación inválidos") from exc
    if requested_page < 1 or bounded_page_size < 1:
        raise ValueError("Parámetros de paginación inválidos")
    bounded_page_size = min(bounded_page_size, MAX_DIRECT_SEARCH_PAGE_SIZE)
    raw_query = str(query or "").strip()
    if len(raw_query) > MAX_TIMELINE_QUERY_CHARS:
        raise ValueError(
            f"La consulta no puede superar {MAX_TIMELINE_QUERY_CHARS} caracteres"
        )
    normalized_query = normalize_text(raw_query)
    if len(normalized_query) < 2:
        raise ValueError("Escribe al menos dos caracteres para buscar decisiones")

    selected_years: list[int] = []
    for value in years or ():
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("El filtro de año no es válido") from exc
        if parsed < 1900 or parsed > 2200:
            raise ValueError("El filtro de año no es válido")
        if parsed not in selected_years:
            selected_years.append(parsed)

    text_pattern = f"%{_escape_like(normalized_query)}%"
    identifier_pattern = f"%{_escape_like(_normalized_identifier(raw_query))}%"
    clauses = [
        "TRIM(COALESCE(r.decision_uid, '')) != ''",
        "EXISTS ("
        "SELECT 1 FROM pages ep JOIN chunks ec ON ec.page_id = ep.id "
        "WHERE ep.document_id = r.document_id "
        "AND ep.page_number BETWEEN r.page_number AND r.end_page_number"
        ")",
        "(r.normalized_product_name LIKE ? ESCAPE '\\' OR "
        "r.normalized_active_ingredient LIKE ? ESCAPE '\\' OR "
        "r.normalized_interested_party LIKE ? ESCAPE '\\' OR "
        "r.normalized_expediente LIKE ? ESCAPE '\\' OR "
        "r.normalized_radicado LIKE ? ESCAPE '\\' OR "
        "LOWER(COALESCE(r.numeral, '')) LIKE ? ESCAPE '\\' OR "
        "LOWER(COALESCE(r.request_text, '')) LIKE ? ESCAPE '\\' OR "
        "LOWER(COALESCE(r.concept_text, '')) LIKE ? ESCAPE '\\' OR "
        "LOWER(REPLACE(COALESCE(r.request_type_code, ''), '_', ' ')) "
        "LIKE ? ESCAPE '\\' OR "
        "LOWER(REPLACE(COALESCE(r.outcome_code, ''), '_', ' ')) "
        "LIKE ? ESCAPE '\\')",
    ]
    parameters: list[object] = [
        text_pattern,
        text_pattern,
        text_pattern,
        identifier_pattern,
        identifier_pattern,
        text_pattern,
        text_pattern,
        text_pattern,
        text_pattern,
        text_pattern,
    ]
    if selected_years:
        placeholders = ",".join("?" for _ in selected_years)
        clauses.append(f"d.year IN ({placeholders})")
        parameters.extend(sorted(selected_years))
    where_sql = " WHERE " + " AND ".join(clauses)

    with connect(database_path) as connection:
        columns = _record_columns(connection)
        required = {
            "decision_uid",
            "normalized_product_name",
            "normalized_active_ingredient",
            "normalized_interested_party",
            "normalized_expediente",
            "normalized_radicado",
        }
        if not required.issubset(columns):
            return ComparisonSearchPage((), 0, 1, bounded_page_size, 1)
        total_row = connection.execute(
            "SELECT COUNT(*) FROM regulatory_records r "
            "JOIN documents d ON d.id = r.document_id" + where_sql,
            parameters,
        ).fetchone()
        total_matches = int(total_row[0] if total_row else 0)
        total_pages = max(1, math.ceil(total_matches / bounded_page_size))
        current_page = min(requested_page, total_pages)
        offset = (current_page - 1) * bounded_page_size
        rows = connection.execute(
            """
            SELECT r.id AS record_id, r.decision_uid, r.page_number,
                   r.product_name, r.active_ingredient, r.interested_party,
                   r.expediente, r.radicado, r.request_type_code,
                   r.outcome_code, d.title, d.url, d.year, d.acta_number,
                   d.section, d.part, d.source_type,
                   (SELECT ec.id FROM pages ep JOIN chunks ec ON ec.page_id = ep.id
                    WHERE ep.document_id = r.document_id
                      AND ep.page_number BETWEEN r.page_number AND r.end_page_number
                    ORDER BY ep.page_number, ec.chunk_index, ec.id LIMIT 1) AS chunk_id,
                   (SELECT ec.text FROM pages ep JOIN chunks ec ON ec.page_id = ep.id
                    WHERE ep.document_id = r.document_id
                      AND ep.page_number BETWEEN r.page_number AND r.end_page_number
                    ORDER BY ep.page_number, ec.chunk_index, ec.id LIMIT 1) AS chunk_text,
                   (SELECT ep.page_number FROM pages ep
                    WHERE ep.document_id = r.document_id
                      AND ep.page_number BETWEEN r.page_number AND r.end_page_number
                      AND EXISTS (SELECT 1 FROM chunks ec WHERE ec.page_id = ep.id)
                    ORDER BY ep.page_number LIMIT 1) AS evidence_page
            FROM regulatory_records r
            JOIN documents d ON d.id = r.document_id
            """
            + where_sql
            + " ORDER BY d.year DESC, d.acta_number DESC, r.page_number, r.id "
            "LIMIT ? OFFSET ?",
            [*parameters, bounded_page_size, offset],
        ).fetchall()

    items: list[ComparisonSearchItem] = []
    for row in rows:
        snapshot = {
            "chunk_id": int(row["chunk_id"]),
            "decision_uid": str(row["decision_uid"]),
            "title": str(row["title"]),
            "url": str(row["url"]),
            "page": int(row["evidence_page"]),
            "text": str(row["chunk_text"]),
            "year": row["year"],
            "acta_number": row["acta_number"],
            "section": row["section"],
            "part": row["part"],
            "source_type": str(row["source_type"] or "official"),
        }
        items.append(
            ComparisonSearchItem(
                record_id=int(row["record_id"]),
                decision_uid=str(row["decision_uid"]),
                title=str(row["title"]),
                year=row["year"],
                acta_number=row["acta_number"],
                page_number=int(row["page_number"]),
                product_name=row["product_name"],
                active_ingredient=row["active_ingredient"],
                interested_party=row["interested_party"],
                expediente=row["expediente"],
                radicado=row["radicado"],
                request_type=row["request_type_code"],
                outcome_code=row["outcome_code"],
                selection=snapshot,
            )
        )
    return ComparisonSearchPage(
        items=tuple(items),
        total_matches=total_matches,
        page=current_page,
        page_size=bounded_page_size,
        total_pages=total_pages,
    )


def _normalized_identifier(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", normalize_text(value))


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _timeline_comparable(value: object, *, identifier: bool, code: bool) -> str:
    text = str(value or "")
    if code:
        text = text.replace("_", " ")
    return _normalized_identifier(text) if identifier else normalize_text(text)


def _date_bound(value: object | None, label: str) -> str | None:
    if value in (None, ""):
        return None
    text = str(value)[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValueError(f"La {label} no es válida; usa AAAA-MM-DD") from exc


def _chronology_key(item: TimelineEntry):
    fallback_year = int(item.year or 0)
    effective_date = item.session_date or f"{fallback_year:04d}-01-01"
    acta_match = re.search(r"\d+", str(item.acta_number or ""))
    acta_order = int(acta_match.group()) if acta_match else 0
    return (
        effective_date,
        fallback_year,
        acta_order,
        str(item.acta_number or ""),
        item.page_number,
        item.record_id if item.record_id is not None else SQLITE_MAX_INTEGER,
    )


def _timeline_entry(
    record: Mapping[str, object],
    *,
    match_origin: str = "structured",
    match_evidence: str | None = None,
    match_page: int | None = None,
    confidence: float | None = None,
    match_evidences: Sequence[Evidence] = (),
) -> TimelineEntry:
    if match_origin == "textual":
        resolved_confidence = None
    else:
        try:
            resolved_confidence = (
                float(confidence)
                if confidence is not None
                else (
                    float(record["confidence"])
                    if record.get("confidence") is not None
                    else None
                )
            )
        except (TypeError, ValueError):
            resolved_confidence = None
    return TimelineEntry(
        record_id=(
            int(record["record_id"])
            if record.get("record_id") is not None
            else None
        ),
        decision_uid=record.get("decision_uid"),
        title=str(record["title"]),
        url=str(record["url"]),
        year=record.get("year"),
        acta_number=record.get("acta_number"),
        section=record.get("section"),
        part=record.get("part"),
        source_type=str(record["source_type"]),
        page_number=int(record["page_number"]),
        end_page_number=int(
            record.get("end_page_number") or record["page_number"]
        ),
        product_name=record.get("product_name"),
        active_ingredient=record.get("active_ingredient"),
        interested_party=record.get("interested_party"),
        expediente=record.get("expediente"),
        radicado=record.get("radicado"),
        request_text=record.get("request_text"),
        concept_text=record.get("concept_text"),
        outcome_code=str(record.get("outcome_code") or "sin_clasificar"),
        numeral=record.get("numeral"),
        numeral_title=record.get("numeral_title"),
        session_date=record.get("session_date"),
        session_date_raw=record.get("session_date_raw"),
        request_type=record.get("request_type_code"),
        identity_strategy=record.get("identity_strategy"),
        review_status=record.get("review_status"),
        reviewer=record.get("reviewer"),
        reviewed_at=record.get("reviewed_at"),
        review_notes=record.get("review_notes"),
        review_stale=bool(record.get("review_stale", False)),
        match_origin=match_origin,
        match_evidence=_bounded_text(match_evidence) or None,
        confidence=resolved_confidence,
        match_page=int(match_page or record["page_number"]),
        match_evidences=tuple(match_evidences[:MAX_EVIDENCE_PER_DECISION]),
    )


def search_timeline(
    database_path: Path,
    field: str,
    value: str,
    *,
    limit: int = 100,
    review_events: Iterable[ReviewEvent] = (),
    years: Sequence[int] | None = None,
    origins: Sequence[str] | None = None,
    review_statuses: Sequence[str] | None = None,
    start_date: object | None = None,
    end_date: object | None = None,
) -> list[TimelineEntry]:
    """Construye una cronología híbrida, trazable y sin afirmar menciones.

    La precedencia al deduplicar es: ficha aprobada, ficha revisada, campo
    estructurado, inferencia automática y, finalmente, mención textual FTS5.
    La salida se ordena cronológicamente después de seleccionar los resultados
    de mayor calidad.
    """

    if field not in _TIMELINE_FIELDS:
        raise ValueError("Campo de cronologia no permitido")
    if not database_path.exists():
        return []
    bounded_limit = max(1, min(int(limit), MAX_TIMELINE_ROWS))
    (
        normalized_column,
        display_column,
        is_identifier,
        allows_textual_fallback,
    ) = _TIMELINE_FIELDS[field]
    is_code_field = field in {"outcome", "request_type"}
    if isinstance(value, str):
        if len(value) > MAX_TIMELINE_QUERY_CHARS:
            raise ValueError(
                f"La consulta no puede superar {MAX_TIMELINE_QUERY_CHARS} caracteres"
            )
        raw_value = value
    else:
        raw_value = str(value or "")[:MAX_TIMELINE_QUERY_CHARS]
    normalized_value = _timeline_comparable(
        raw_value,
        identifier=is_identifier,
        code=is_code_field,
    )[:200]
    if len(normalized_value) < 2:
        raise ValueError("Escribe al menos dos caracteres para la cronologia")
    pattern = f"%{_escape_like(normalized_value)}%"

    selected_years: set[int] = set()
    for year in years or ():
        try:
            parsed_year = int(year)
        except (TypeError, ValueError) as exc:
            raise ValueError("El filtro de año no es válido") from exc
        if parsed_year < 1900 or parsed_year > 2200:
            raise ValueError("El filtro de año no es válido")
        selected_years.add(parsed_year)
    selected_origins = {str(item) for item in origins or ()}
    if selected_origins - set(TIMELINE_MATCH_ORIGINS):
        raise ValueError("El filtro de procedencia no es válido")
    selected_statuses = {str(item) for item in review_statuses or ()}
    if selected_statuses - set(TIMELINE_REVIEW_STATUSES):
        raise ValueError("El filtro de revisión no es válido")
    normalized_start_date = _date_bound(start_date, "fecha inicial")
    normalized_end_date = _date_bound(end_date, "fecha final")
    if (
        normalized_start_date
        and normalized_end_date
        and normalized_start_date > normalized_end_date
    ):
        raise ValueError("La fecha inicial no puede ser posterior a la fecha final")

    review_values = _bounded_review_events(review_events)
    current_reviews = latest_reviews(review_values)
    corrected_uids: list[str] = []
    for uid, event in current_reviews.items():
        if event.status == "reopened" or display_column not in event.corrections:
            continue
        corrected_value = _bounded_text(
            event.corrections.get(display_column),
            MAX_CELL_CHARS,
        )
        comparable = _timeline_comparable(
            corrected_value,
            identifier=is_identifier,
            code=is_code_field,
        )
        if normalized_value in comparable:
            corrected_uids.append(uid)

    values: dict[str, TimelineEntry] = {}

    def origin_for(
        record: Mapping[str, object],
        field_meta: Mapping[str, object],
        evidence_method: object | None,
    ) -> str:
        status = str(record.get("review_status") or "automatic")
        if status == "approved":
            return "verified"
        if status == "reviewed":
            return "reviewed"
        normalized_method = normalize_text(str(evidence_method or "")).replace(
            " ",
            "_",
        )
        # Cuando la coincidencia viene de una fila de evidencia concreta, su
        # método es más preciso que la procedencia agregada del campo. Esto es
        # importante en combinaciones donde un ingrediente fue explícito y
        # otro se obtuvo mediante una inferencia conservadora.
        if evidence_method not in (None, ""):
            if any(
                marker in normalized_method for marker in _INFERRED_METHOD_MARKERS
            ):
                return "inferred"
            return "structured"
        provenance = str(field_meta.get("provenance") or "")
        if provenance == "inferred":
            return "inferred"
        return "structured"

    def identity_for(item: TimelineEntry) -> str:
        if item.decision_uid:
            return f"decision:{item.decision_uid}"
        if item.record_id is not None:
            return f"record:{item.record_id}"
        return f"mention:{item.url}:{item.page_number}"

    def allowed(item: TimelineEntry) -> bool:
        if selected_years and item.year not in selected_years:
            return False
        if normalized_start_date or normalized_end_date:
            try:
                session_date = date.fromisoformat(
                    str(item.session_date or "")[:10]
                ).isoformat()
            except ValueError:
                return False
            if normalized_start_date and session_date < normalized_start_date:
                return False
            if normalized_end_date and session_date > normalized_end_date:
                return False
        if selected_origins and item.match_origin not in selected_origins:
            return False
        status = item.review_status or (
            "unstructured" if item.record_id is None else "automatic"
        )
        return not selected_statuses or status in selected_statuses

    def merge_entry(item: TimelineEntry) -> None:
        if not allowed(item):
            return
        identity = identity_for(item)
        current = values.get(identity)
        if current is None:
            values[identity] = item
            return
        evidences = list(current.match_evidences)
        evidence_ids = {evidence.chunk_id for evidence in evidences}
        for evidence in item.match_evidences:
            if (
                evidence.chunk_id not in evidence_ids
                and len(evidences) < MAX_EVIDENCE_PER_DECISION
            ):
                evidences.append(evidence)
                evidence_ids.add(evidence.chunk_id)
        current_priority = _MATCH_ORIGIN_PRIORITY.get(current.match_origin, 99)
        item_priority = _MATCH_ORIGIN_PRIORITY.get(item.match_origin, 99)
        preferred = item if item_priority < current_priority else current
        values[identity] = replace(
            preferred,
            match_evidences=tuple(evidences),
        )

    def add_structured_row(
        row,
        *,
        evidence_value: object | None = None,
        evidence_text: object | None = None,
        evidence_page: object | None = None,
        evidence_confidence: object | None = None,
        evidence_method: object | None = None,
    ) -> None:
        raw = dict(row)
        uid = str(raw.get("decision_uid") or "")
        record = effective_record(raw, current_reviews.get(uid))
        review = current_reviews.get(uid)
        metadata_by_field = record.get("field_metadata")
        field_meta = (
            dict(metadata_by_field.get(display_column) or {})
            if isinstance(metadata_by_field, Mapping)
            and isinstance(metadata_by_field.get(display_column), Mapping)
            else {}
        )
        # Una corrección vigente gana sobre la evidencia automática antigua.
        review_controls_field = bool(
            review
            and not record.get("review_stale")
            and review.status != "reopened"
            and display_column in review.corrections
        )
        if review_controls_field:
            # Una fila de evidencia automática puede corresponder al valor
            # anterior. No se reutiliza como si sustentara la corrección humana.
            evidence_text = None
            evidence_page = field_meta.get("page") or record.get("page_number")
            evidence_confidence = field_meta.get("confidence")
            evidence_method = "human_review"
        else:
            if evidence_text in (None, "") and field_meta.get("fragment"):
                evidence_text = field_meta.get("fragment")
            if evidence_page in (None, "") and field_meta.get("page"):
                evidence_page = field_meta.get("page")
            if evidence_confidence is None and field_meta.get("confidence") is not None:
                evidence_confidence = field_meta.get("confidence")
            if evidence_method in (None, "") and field_meta.get("method"):
                evidence_method = field_meta.get("method")
        effective_value = _bounded_text(record.get(display_column), MAX_CELL_CHARS)
        candidate_value = effective_value
        if not review_controls_field and evidence_value not in (None, ""):
            candidate_value = _bounded_text(evidence_value, MAX_CELL_CHARS)
        effective_comparable = _timeline_comparable(
            candidate_value,
            identifier=is_identifier,
            code=is_code_field,
        )
        if normalized_value not in effective_comparable:
            return
        if not effective_value and evidence_value not in (None, ""):
            # Las evidencias estructuradas 0.7 son valores del campo, no simples
            # menciones; se pueden mostrar como valor vigente de compatibilidad.
            record[display_column] = candidate_value
        elif (
            not review_controls_field
            and evidence_value not in (None, "")
            and normalize_text(candidate_value) not in normalize_text(effective_value)
        ):
            # Una tabla hija puede contener combinaciones de varios principios
            # activos que no caben en la columna heredada de 0.6.
            record[display_column] = f"{effective_value}; {candidate_value}"
        try:
            match_confidence = (
                float(evidence_confidence)
                if evidence_confidence is not None
                else (
                    float(record["confidence"])
                    if record.get("confidence") is not None
                    else None
                )
            )
        except (TypeError, ValueError):
            match_confidence = None
        origin = origin_for(record, field_meta, evidence_method)
        if origin in {"verified", "reviewed"}:
            match_confidence = 1.0 if origin == "verified" else max(
                match_confidence or 0.0,
                0.95,
            )
        merge_entry(
            _timeline_entry(
                record,
                match_origin=origin,
                match_evidence=(
                    _bounded_text(evidence_text)
                    if evidence_text not in (None, "")
                    else None
                ),
                match_page=(
                    int(evidence_page)
                    if evidence_page not in (None, "")
                    else int(record["page_number"])
                ),
                confidence=match_confidence,
            )
        )

    with connect(database_path) as connection:
        connection.execute("BEGIN")
        columns = _record_columns(connection)
        if not columns:
            return []
        field_evidence_cache: dict[int, list[dict[str, object]]] = {}
        optional_sql = ",\n                   ".join(_optional_selects(columns))
        select_sql = f"""
            SELECT r.id AS record_id, r.record_key, d.title, d.url, d.year,
                   d.acta_number, d.section, d.part, d.source_type,
                   d.document_hash,
                   r.page_number, r.end_page_number, r.product_name,
                   r.active_ingredient, r.interested_party, r.expediente,
                   r.radicado, r.request_text, r.concept_text,
                   r.outcome_code, r.confidence,
                   {optional_sql}
            FROM regulatory_records r
            JOIN documents d ON d.id = r.document_id
        """
        year_clause = ""
        year_parameters: list[object] = []
        if selected_years:
            year_placeholders = ",".join("?" for _ in selected_years)
            year_clause = f" AND d.year IN ({year_placeholders})"
            year_parameters = sorted(selected_years)
        normalized_sql = f"r.{normalized_column}"
        if is_code_field:
            normalized_sql = f"LOWER(REPLACE(r.{normalized_column}, '_', ' '))"
        cursor = connection.execute(
            select_sql
            + f" WHERE {normalized_sql} LIKE ? ESCAPE '\\'"
            + year_clause,
            (pattern, *year_parameters),
        )
        while True:
            batch = cursor.fetchmany(500)
            if not batch:
                break
            for row in _hydrate_rows_with_field_evidence(
                connection,
                batch,
                field_evidence_cache,
            ):
                add_structured_row(row)

        # Una correccion puede introducir el valor buscado aunque el valor
        # automatico no coincida; esas fichas se recuperan por UID en lotes.
        if corrected_uids and "decision_uid" in columns:
            for start in range(0, len(corrected_uids), 800):
                uid_batch = corrected_uids[start : start + 800]
                placeholders = ",".join("?" for _ in uid_batch)
                corrected_rows = connection.execute(
                    select_sql
                    + f" WHERE r.decision_uid IN ({placeholders})"
                    + year_clause,
                    [*uid_batch, *year_parameters],
                ).fetchall()
                for row in _hydrate_rows_with_field_evidence(
                    connection,
                    corrected_rows,
                    field_evidence_cache,
                ):
                    add_structured_row(row)

        # Una base 0.7 puede representar varios valores y su procedencia en una
        # tabla hija. Si no existe, la consulta anterior mantiene compatibilidad
        # total con 0.6.
        evidence_columns = _field_evidence_columns(connection)
        if evidence_columns:
            record_fk = evidence_columns["record_id"]
            field_name_column = evidence_columns["field_name"]
            literal_column = evidence_columns["value_literal"]
            normalized_evidence_column = evidence_columns.get(
                "value_normalized",
                evidence_columns.get("value_canonical", literal_column),
            )
            field_names = _FIELD_EVIDENCE_NAMES[field]
            field_placeholders = ",".join("?" for _ in field_names)

            def optional_evidence_select(name: str) -> str:
                column = evidence_columns.get(name)
                return f"f.{column}" if column else "NULL"

            evidence_select = select_sql.replace(
                "FROM regulatory_records r",
                ", "
                f"f.{literal_column} AS _match_value, "
                f"{optional_evidence_select('evidence_text')} AS _match_evidence, "
                f"{optional_evidence_select('page_number')} AS _match_page, "
                f"{optional_evidence_select('confidence')} AS _match_confidence, "
                f"{optional_evidence_select('method')} AS _match_method "
                "FROM regulatory_records r",
            ).replace(
                "JOIN documents d ON d.id = r.document_id",
                "JOIN documents d ON d.id = r.document_id "
                "JOIN regulatory_field_evidence f "
                f"ON f.{record_fk} = r.id",
            )
            evidence_value_sql = f"f.{normalized_evidence_column}"
            if is_code_field:
                evidence_value_sql = (
                    f"LOWER(REPLACE(f.{normalized_evidence_column}, '_', ' '))"
                )
            evidence_rows = connection.execute(
                evidence_select
                + f" WHERE f.{field_name_column} IN ({field_placeholders})"
                + f" AND {evidence_value_sql} LIKE ? ESCAPE '\\'"
                + year_clause,
                [*field_names, pattern, *year_parameters],
            ).fetchall()
            for row in _hydrate_rows_with_field_evidence(
                connection,
                evidence_rows,
                field_evidence_cache,
            ):
                add_structured_row(
                    row,
                    evidence_value=row["_match_value"],
                    evidence_text=row["_match_evidence"],
                    evidence_page=row["_match_page"],
                    evidence_confidence=row["_match_confidence"],
                    evidence_method=row["_match_method"],
                )

        # FTS5 es el último respaldo. El valor consultado nunca se copia al
        # campo regulatorio: se conserva como una mención textual con evidencia.
        if allows_textual_fallback and (
            not selected_origins or "textual" in selected_origins
        ):
            clean_phrase = normalize_text(raw_value).replace('"', " ").strip()
            query_tokens = [
                token
                for term in tokenize_query(clean_phrase)
                for token in re.findall(r"[a-z0-9]+", term)
                if token
            ]
            if query_tokens:
                # ``chunks_fts`` usa detail=column y no admite frases de varios
                # términos; AND recupera candidatos y la comparación Python de
                # abajo exige luego la cadena normalizada completa.
                fts_query = " AND ".join(f'"{token}"' for token in query_tokens)
                text_limit = min(
                    MAX_TIMELINE_TEXT_HITS,
                    max(bounded_limit * 6, 150),
                )
                fts_rows = connection.execute(
                    """
                    SELECT c.id AS chunk_id, c.text, p.page_number,
                           p.document_id, d.title, d.url, d.year,
                           d.acta_number, d.section, d.part, d.source_type
                    FROM chunks_fts
                    JOIN chunks c ON c.id = chunks_fts.rowid
                    JOIN pages p ON p.id = c.page_id
                    JOIN documents d ON d.id = p.document_id
                    WHERE chunks_fts MATCH ?
                    """
                    + (
                        f" AND d.year IN ({','.join('?' for _ in selected_years)})"
                        if selected_years
                        else ""
                    )
                    + " ORDER BY bm25(chunks_fts, 3.0, 1.0), c.id LIMIT ?",
                    [fts_query, *sorted(selected_years), text_limit],
                ).fetchall()
                matching_rows = []
                for row in fts_rows:
                    text_comparable = _timeline_comparable(
                        row["text"],
                        identifier=is_identifier,
                        code=is_code_field,
                    )
                    if normalized_value in text_comparable:
                        matching_rows.append(row)
                chunk_ids = [int(row["chunk_id"]) for row in matching_rows]
                records_by_chunk = _regulatory_rows_for_chunks(
                    connection,
                    chunk_ids,
                    review_values,
                )
                for row in matching_rows:
                    evidence = Evidence(
                        chunk_id=int(row["chunk_id"]),
                        page=int(row["page_number"]),
                        text=_bounded_text(row["text"]),
                    )
                    matching_records = records_by_chunk.get(evidence.chunk_id, [])
                    if matching_records:
                        for record in matching_records:
                            record_uid = str(record.get("decision_uid") or "")
                            current_review = current_reviews.get(record_uid)
                            if (
                                current_review
                                and not record.get("review_stale")
                                and current_review.status != "reopened"
                                and display_column in current_review.corrections
                            ):
                                corrected_value = _bounded_text(
                                    record.get(display_column),
                                    MAX_CELL_CHARS,
                                )
                                corrected_comparable = _timeline_comparable(
                                    corrected_value,
                                    identifier=is_identifier,
                                    code=is_code_field,
                                )
                                if normalized_value not in corrected_comparable:
                                    # La mención puede corresponder al valor
                                    # automático antiguo. Una corrección humana
                                    # vigente no se contradice con ese texto.
                                    continue
                            merged_record = {
                                **record,
                                "title": row["title"],
                                "url": row["url"],
                                "year": row["year"],
                                "acta_number": row["acta_number"],
                                "section": row["section"],
                                "part": row["part"],
                                "source_type": row["source_type"],
                            }
                            merge_entry(
                                _timeline_entry(
                                    merged_record,
                                    match_origin="textual",
                                    match_evidence=evidence.text,
                                    match_page=evidence.page,
                                    confidence=None,
                                    match_evidences=(evidence,),
                                )
                            )
                    else:
                        merge_entry(
                            _timeline_entry(
                                {
                                    "record_id": None,
                                    "decision_uid": None,
                                    "title": row["title"],
                                    "url": row["url"],
                                    "year": row["year"],
                                    "acta_number": row["acta_number"],
                                    "section": row["section"],
                                    "part": row["part"],
                                    "source_type": row["source_type"],
                                    "page_number": row["page_number"],
                                    "end_page_number": row["page_number"],
                                    "outcome_code": "sin_clasificar",
                                    "review_status": "unstructured",
                                },
                                match_origin="textual",
                                match_evidence=evidence.text,
                                match_page=evidence.page,
                                confidence=None,
                                match_evidences=(evidence,),
                            )
                        )

    prioritized = sorted(
        values.values(),
        key=lambda item: (
            _MATCH_ORIGIN_PRIORITY.get(item.match_origin, 99),
            _chronology_key(item),
        ),
    )[:bounded_limit]
    prioritized.sort(key=_chronology_key)
    return prioritized


def search_timeline_page(
    database_path: Path,
    field: str,
    value: str,
    *,
    page: int = 1,
    page_size: int = 25,
    review_events: Iterable[ReviewEvent] = (),
    years: Sequence[int] | None = None,
    origins: Sequence[str] | None = None,
    review_statuses: Sequence[str] | None = None,
    start_date: object | None = None,
    end_date: object | None = None,
) -> TimelineSearchPage:
    """Pagina una cronología y hace explícito el límite de seguridad.

    El total es exacto mientras sea menor que ``MAX_TIMELINE_ROWS``. Cuando se
    alcanza ese máximo, ``truncated`` indica que pueden existir más decisiones.
    """

    loaded = search_timeline(
        database_path,
        field,
        value,
        limit=MAX_TIMELINE_ROWS,
        review_events=review_events,
        years=years,
        origins=origins,
        review_statuses=review_statuses,
        start_date=start_date,
        end_date=end_date,
    )
    return paginate_timeline(loaded, page=page, page_size=page_size)


def paginate_timeline(
    entries: Sequence[TimelineEntry],
    *,
    page: int = 1,
    page_size: int = 25,
) -> TimelineSearchPage:
    """Pagina resultados ya recuperados para no repetir la consulta en la UI."""

    try:
        requested_page = int(page)
        bounded_page_size = int(page_size)
    except (TypeError, ValueError) as exc:
        raise ValueError("Parámetros de paginación inválidos") from exc
    if requested_page < 1 or bounded_page_size < 1:
        raise ValueError("Parámetros de paginación inválidos")
    bounded_page_size = min(bounded_page_size, 100)
    loaded = list(entries[:MAX_TIMELINE_ROWS])
    total_loaded = len(loaded)
    total_pages = max(1, math.ceil(total_loaded / bounded_page_size))
    current_page = min(requested_page, total_pages)
    start = (current_page - 1) * bounded_page_size
    return TimelineSearchPage(
        entries=tuple(loaded[start : start + bounded_page_size]),
        total_loaded=total_loaded,
        page=current_page,
        page_size=bounded_page_size,
        total_pages=total_pages,
        truncated=total_loaded >= MAX_TIMELINE_ROWS,
    )


_OUTCOME_LABELS = {
    "aprobado": "Aprobado",
    "negado": "Negado",
    "requerido": "Requerimiento",
    "desistido": "Desistido",
    "archivado": "Archivado",
    "favorable": "Favorable",
    "no_favorable": "No favorable",
    "sin_clasificar": "Sin clasificar",
}

_REVIEW_STATUS_LABELS = {
    "automatic": "Automática",
    "reviewed": "Revisada",
    "approved": "Aprobada",
    "reopened": "Reabierta",
    "stale": "Revisión desactualizada",
    "unstructured": "Sin ficha estructurada",
}

_MATCH_ORIGIN_LABELS = {
    "verified": "Ficha estructurada",
    "reviewed": "Ficha estructurada",
    "structured": "Ficha estructurada",
    "inferred": "Inferida automáticamente",
    "textual": "Mención textual",
}


def outcome_label(value: str | None) -> str:
    if not value:
        return "No extraído"
    return _OUTCOME_LABELS.get(value, value.replace("_", " ").title())


def request_type_label(value: str | None) -> str:
    if not value:
        return "No extraído"
    return value.replace("_", " ").capitalize()


def review_status_label(value: str | None) -> str:
    if not value:
        return "Automática"
    return _REVIEW_STATUS_LABELS.get(value, value.replace("_", " ").capitalize())


def match_origin_label(value: str | None) -> str:
    if not value:
        return "Estructurada automática"
    return _MATCH_ORIGIN_LABELS.get(value, value.replace("_", " ").capitalize())


def _decision_review_label(item: ComparisonDecision) -> str:
    if item.record_id is None:
        return "Sin ficha estructurada"
    return review_status_label(item.review_status)


def _pages_label(start: int, end: int) -> str:
    return str(start) if start == end else f"{start}-{end}"


def comparison_matrix(decisions: Sequence[ComparisonDecision]) -> list[dict[str, str]]:
    """Crea una matriz sin HTML para mostrar las decisiones lado a lado."""

    selected = list(decisions[:MAX_COMPARISON_ITEMS])
    fields = [
        ("Acta", lambda item: item.acta_number),
        ("Año", lambda item: item.year),
        ("Numeral", lambda item: item.numeral),
        ("Título del numeral", lambda item: item.numeral_title),
        ("Fecha de sesión", lambda item: item.session_date),
        ("Producto", lambda item: item.product_name),
        ("Principio activo", lambda item: item.active_ingredient),
        ("Interesado", lambda item: item.interested_party),
        ("Expediente", lambda item: item.expediente),
        ("Radicado", lambda item: item.radicado),
        ("Tipo de solicitud", lambda item: request_type_label(item.request_type)),
        ("Resultado", lambda item: outcome_label(item.outcome_code)),
        ("Páginas", lambda item: _pages_label(item.page_number, item.end_page_number)),
        ("Solicitud", lambda item: item.request_text),
        ("Concepto", lambda item: item.concept_text),
        (
            "Evidencia seleccionada",
            lambda item: "\n\n".join(
                f"Pagina {source.page}: {source.text}"
                for source in item.evidences
            ),
        ),
    ]
    matrix: list[dict[str, str]] = []
    for field_label, getter in fields:
        row = {"Campo": field_label}
        for index, decision in enumerate(selected, start=1):
            row[f"Decision {index}"] = str(display_value(getter(decision)))
        matrix.append(row)
    return matrix


def comparison_differences(
    decisions: Sequence[ComparisonDecision],
) -> list[dict[str, object]]:
    """Resume únicamente los campos cuyo valor cambia entre decisiones."""

    selected = list(decisions[:MAX_COMPARISON_ITEMS])
    if len(selected) < 2:
        return []
    ignored = {
        "Evidencia seleccionada",
        "Estado de revisión",
        "Revisor",
        "Fecha de revisión",
        "Notas de revisión",
    }
    differences: list[dict[str, object]] = []
    for row in comparison_matrix(selected):
        field = str(row["Campo"])
        if field in ignored:
            continue
        values = [str(row[f"Decision {index}"]) for index in range(1, len(selected) + 1)]
        comparable = {
            normalize_text(value).replace("\n", " ")
            for value in values
        }
        if len(comparable) <= 1:
            continue
        details = "\n".join(
            f"D{index}: {_bounded_text(value, 600)}"
            for index, value in enumerate(values, start=1)
        )
        differences.append(
            {
                "Campo": field,
                "Valores distintos": len(comparable),
                "Detalle": details,
            }
        )
    return differences


def _safe_csv_cell(value: object) -> str:
    text = _bounded_text(display_value(value))
    # Evita que Excel/LibreOffice interpreten contenido documental como formula.
    if text.lstrip().startswith(("=", "+", "-", "@")):
        text = "'" + text
    return text


def _csv_bytes(headers: Sequence[str], rows: Iterable[Sequence[object]]) -> bytes:
    output = bytearray(b"\xef\xbb\xbf")

    def append_row(row: Sequence[object]) -> None:
        buffer = StringIO(newline="")
        csv.writer(buffer, lineterminator="\n").writerow(
            [_safe_csv_cell(value) for value in row]
        )
        encoded_row = buffer.getvalue().encode("utf-8")
        if len(output) + len(encoded_row) > MAX_CSV_BYTES:
            raise ValueError(
                "La exportación CSV supera el límite de tamaño permitido"
            )
        output.extend(encoded_row)

    append_row(headers)
    for row in rows:
        append_row(row)
    return bytes(output)


def comparison_to_csv(decisions: Sequence[ComparisonDecision]) -> bytes:
    selected = list(decisions[:MAX_COMPARISON_ITEMS])
    headers = (
        "decision",
        "decision_uid",
        "ano",
        "acta",
        "numeral",
        "titulo_numeral",
        "fecha_sesion",
        "sala",
        "parte",
        "producto",
        "principio_activo",
        "interesado",
        "expediente",
        "radicado",
        "tipo_solicitud",
        "solicitud",
        "concepto",
        "resultado",
        "paginas",
        "evidencia_seleccionada",
        "enlaces_evidencia",
        "fuente",
    )
    rows = []
    for index, item in enumerate(selected, start=1):
        evidence = "\n\n".join(
            f"Pagina {source.page}: {source.text}" for source in item.evidences
        )
        evidence_links = "\n".join(
            _page_href(item.url, source.page) for source in item.evidences
        )
        rows.append(
            (
                index,
                item.decision_uid,
                item.year,
                item.acta_number,
                item.numeral,
                item.numeral_title,
                item.session_date,
                item.section,
                item.part,
                item.product_name,
                item.active_ingredient,
                item.interested_party,
                item.expediente,
                item.radicado,
                request_type_label(item.request_type),
                item.request_text,
                item.concept_text,
                outcome_label(item.outcome_code),
                _pages_label(item.page_number, item.end_page_number),
                evidence,
                evidence_links,
                _page_href(item.url, item.page_number),
            )
        )
    return _csv_bytes(headers, rows)


def timeline_to_csv(entries: Sequence[TimelineEntry]) -> bytes:
    headers = (
        "decision_uid",
        "identificador_decision",
        "ano",
        "acta",
        "fecha_sesion",
        "numeral",
        "titulo_numeral",
        "producto",
        "principio_activo",
        "interesado",
        "expediente",
        "radicado",
        "tipo_solicitud",
        "resultado",
        "paginas",
        "solicitud",
        "concepto",
        "origen_coincidencia",
        "confianza",
        "pagina_coincidencia",
        "evidencia_coincidencia",
        "enlaces_evidencia_coincidencia",
        "fuente",
    )
    rows = (
        (
            item.decision_uid,
            item.review_identifier,
            item.year,
            item.acta_number,
            item.session_date,
            item.numeral,
            item.numeral_title,
            item.product_name,
            item.active_ingredient,
            item.interested_party,
            item.expediente,
            item.radicado,
            request_type_label(item.request_type),
            outcome_label(item.outcome_code),
            _pages_label(item.page_number, item.end_page_number),
            item.request_text,
            item.concept_text,
            match_origin_label(item.match_origin),
            item.confidence,
            item.match_page,
            _timeline_evidence_value(item),
            "\n".join(
                _page_href(item.url, evidence.page)
                for evidence in item.match_evidences
            ),
            _page_href(item.url, item.page_number),
        )
        for item in entries[:MAX_TIMELINE_ROWS]
    )
    return _csv_bytes(headers, rows)


def _page_href(url: str, page: int) -> str:
    try:
        parsed = urlsplit(str(url))
    except (TypeError, ValueError):
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, parsed.query, f"page={max(1, page)}")
    )


def _html_value(value: object) -> str:
    text = _bounded_text(display_value(value))
    return escape(text).replace("\n", "<br>")


def _timeline_evidence_value(item: TimelineEntry) -> str | None:
    if item.match_evidence:
        return item.match_evidence
    if item.match_origin in {"verified", "reviewed"} and not item.match_evidences:
        return HUMAN_CORRECTION_WITHOUT_SOURCE_FRAGMENT
    return None


def _html_link(url: str, page: int, label: str) -> str:
    href = _page_href(url, page)
    if not href:
        return "Fuente no disponible"
    return (
        f'<a href="{escape(href, quote=True)}" target="_blank" '
        f'rel="noopener noreferrer">{escape(label)}</a>'
    )


def printable_html_report(
    decisions: Sequence[ComparisonDecision],
    *,
    timeline: Sequence[TimelineEntry] = (),
    title: str = "Comparacion de decisiones INVIMA",
) -> bytes:
    """Genera un HTML autocontenido y seguro para imprimir como PDF."""

    selected = list(decisions[:MAX_COMPARISON_ITEMS])
    timeline_rows = list(timeline[:MAX_TIMELINE_ROWS])
    matrix = comparison_matrix(selected)
    column_headers = "".join(
        f"<th>Decision {index}</th>" for index in range(1, len(selected) + 1)
    )
    matrix_rows = []
    for row in matrix:
        cells = "".join(
            f"<td>{_html_value(row.get(f'Decision {index}'))}</td>"
            for index in range(1, len(selected) + 1)
        )
        matrix_rows.append(f"<tr><th>{escape(row['Campo'])}</th>{cells}</tr>")

    comparison_section = ""
    if selected:
        comparison_section = (
            "<section><h2>Comparación lado a lado</h2>"
            "<table><thead><tr><th>Campo</th>"
            + column_headers
            + "</tr></thead><tbody>"
            + "".join(matrix_rows)
            + "</tbody></table></section>"
        )

    evidence_sections = []
    for index, item in enumerate(selected, start=1):
        blocks = "".join(
            "<div class='evidence'>"
            f"<strong>{_html_link(item.url, source.page, 'Página ' + str(source.page))}"
            f"</strong><br>{_html_value(source.text)}"
            "</div>"
            for source in item.evidences
        ) or "<p>Sin fragmento disponible.</p>"
        evidence_sections.append(
            f"<section><h3>Decision {index}: {_html_value(item.label)}</h3>"
            f"<p><strong>Documento:</strong> {_html_value(item.title)}</p>"
            f"<p>{_html_link(item.url, item.page_number, 'Abrir fuente')}</p>"
            f"{blocks}</section>"
        )

    evidence_section = (
        "<section><h2>Evidencia seleccionada</h2>"
        + "".join(evidence_sections)
        + "</section>"
        if evidence_sections
        else ""
    )

    timeline_section = ""
    if timeline_rows:
        rows = []
        for item in timeline_rows:
            rows.append(
                "<tr>"
                f"<td>{_html_value(item.year)}</td>"
                f"<td>{_html_value(item.session_date)}</td>"
                f"<td>{_html_value(item.acta_number)}</td>"
                f"<td>{_html_value(item.numeral)}</td>"
                f"<td>{_html_value(item.product_name)}</td>"
                f"<td>{_html_value(item.active_ingredient)}</td>"
                f"<td>{_html_value(item.expediente)}</td>"
                f"<td>{_html_value(item.radicado)}</td>"
                f"<td>{_html_value(outcome_label(item.outcome_code))}</td>"
                f"<td>{_html_value(match_origin_label(item.match_origin))}</td>"
                f"<td>{_html_link(item.url, item.match_page or item.page_number, 'Pagina ' + str(item.match_page or item.page_number))}</td>"
                "</tr>"
            )
        details = []
        for index, item in enumerate(
            timeline_rows[:MAX_HTML_TIMELINE_DETAILS],
            start=1,
        ):
            request = _bounded_text(
                item.request_text,
                MAX_HTML_TIMELINE_TEXT_CHARS,
            )
            concept = _bounded_text(
                item.concept_text,
                MAX_HTML_TIMELINE_TEXT_CHARS,
            )
            grouped_mentions = "".join(
                "<div class='evidence'>"
                f"<strong>{_html_link(item.url, evidence.page, 'Página ' + str(evidence.page))}</strong><br>"
                f"{_html_value(_bounded_text(evidence.text, MAX_HTML_TIMELINE_TEXT_CHARS))}"
                "</div>"
                for evidence in item.match_evidences
            )
            primary_evidence = (
                ""
                if item.match_evidences
                else "<p><strong>Evidencia de coincidencia:</strong> "
                f"{_html_value(_timeline_evidence_value(item))}</p>"
            )
            details.append(
                "<article class='timeline-detail'>"
                f"<h3>{index}. {_html_value(item.label)}</h3>"
                f"<p><strong>Producto:</strong> {_html_value(item.product_name)}"
                f" · <strong>Resultado:</strong> "
                f"{_html_value(outcome_label(item.outcome_code))}</p>"
                f"<p><strong>Origen de la coincidencia:</strong> "
                f"{_html_value(match_origin_label(item.match_origin))}"
                f" · <strong>Página:</strong> {_html_value(item.match_page)}</p>"
                f"{primary_evidence}"
                f"<p><strong>Identificador de decisión:</strong> "
                f"{_html_value(item.review_identifier)}</p>"
                f"{grouped_mentions}"
                f"<p><strong>Solicitud:</strong> {_html_value(request)}</p>"
                f"<p><strong>Concepto:</strong> {_html_value(concept)}</p>"
                f"<p>{_html_link(item.url, item.match_page or item.page_number, 'Abrir evidencia')}</p>"
                "</article>"
            )
        omitted_note = ""
        if len(timeline_rows) > MAX_HTML_TIMELINE_DETAILS:
            omitted_note = (
                f"<p>El detalle se limita a {MAX_HTML_TIMELINE_DETAILS} decisiones; "
                "la tabla y el CSV conservan el conjunto completo.</p>"
            )
        timeline_section = (
            "<section><h2>Cronología</h2><table><thead><tr>"
            "<th>Ano</th><th>Fecha</th><th>Acta</th><th>Numeral</th>"
            "<th>Producto</th><th>Principio activo</th><th>Expediente</th>"
            "<th>Radicado</th><th>Resultado</th>"
            "<th>Coincidencia</th><th>Fuente</th>"
            "</tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table>"
            + omitted_note
            + "<h2>Detalle de la cronología</h2>"
            + "".join(details)
            + "</section>"
        )

    document = f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(_bounded_text(title, 200))}</title>
<style>
@page {{ size: landscape; margin: 12mm; }}
body {{ color:#17202a; font-family:Arial,sans-serif; font-size:10pt; }}
h1,h2,h3 {{ page-break-after:avoid; }}
table {{ border-collapse:collapse; width:100%; table-layout:fixed; margin:12px 0; }}
th,td {{ border:1px solid #9aa4ad; padding:7px; text-align:left;
         vertical-align:top; overflow-wrap:anywhere; }}
th {{ background:#eef2f5; }}
.evidence {{ border-left:4px solid #536d7a; margin:8px 0; padding:8px;
             page-break-inside:avoid; }}
.timeline-detail {{ border-top:1px solid #9aa4ad; padding-top:6px;
                    page-break-inside:avoid; }}
a {{ color:#075985; }}
section {{ margin-top:20px; }}
@media print {{ a {{ color:#000; }} }}
</style></head><body>
<h1>{escape(_bounded_text(title, 200))}</h1>
<p>Verifica siempre los campos contra las páginas enlazadas. Una mención textual
no confirma por sí sola que el término sea el producto o principio activo de la decisión.</p>
{comparison_section}
{evidence_section}
{timeline_section}
</body></html>"""
    encoded = document.encode("utf-8")
    if len(encoded) > MAX_HTML_BYTES:
        raise ValueError("El reporte supera el limite de tamano permitido")
    return encoded
