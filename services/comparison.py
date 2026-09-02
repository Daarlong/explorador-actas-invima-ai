"""Comparacion y cronologias verificables de decisiones regulatorias.

Este modulo no confia en el contenido guardado en ``st.session_state``. Las
selecciones del Explorador aportan unicamente los identificadores de fragmento;
los textos, metadatos y fichas se vuelven a leer de la base vigente antes de
mostrarlos o exportarlos.
"""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, replace
from html import escape
from io import StringIO
from itertools import islice
from pathlib import Path
import re
from typing import Iterable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from services.database import connect
from services.models import SearchResult
from services.reviews import ReviewEvent, effective_record, latest_reviews
from services.text_utils import normalize_text


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
SQLITE_MAX_INTEGER = 9_223_372_036_854_775_807


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

    record_id: int
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

    @property
    def label(self) -> str:
        acta = f"Acta {self.acta_number}" if self.acta_number else "Acta sin numero"
        year = str(self.year) if self.year is not None else "sin ano"
        return f"{year} · {acta} · pagina {self.page_number}"

    def as_dict(self) -> dict:
        return asdict(self)


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
    "product": ("normalized_product_name", "product_name", False),
    "active_ingredient": (
        "normalized_active_ingredient",
        "active_ingredient",
        False,
    ),
    "expediente": ("normalized_expediente", "expediente", True),
    "radicado": ("normalized_radicado", "radicado", True),
}


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
        )
        for row in rows:
            raw = dict(row)
            uid = str(raw.get("decision_uid") or "")
            record = effective_record(raw, current_reviews.get(uid))
            page_start = int(record["page_number"])
            page_end = int(record["end_page_number"] or page_start)
            for chunk_id, evidence_page in chunks_by_document.get(
                int(row["document_id"]),
                [],
            ):
                if (
                    page_start <= evidence_page <= page_end
                    and len(grouped[chunk_id]) <= MAX_COMPARISON_ITEMS
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
    snapshots: dict[int, Mapping[str, object]] = {}
    for source in stored_sources:
        if not isinstance(source, Mapping):
            continue
        try:
            chunk_id = int(source.get("chunk_id", 0))
        except (TypeError, ValueError):
            continue
        snapshots.setdefault(chunk_id, source)

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
        for source in current_sources:
            snapshot = snapshots.get(source.chunk_id, {})
            current = source.as_dict()
            if any(
                field in snapshot and snapshot.get(field) != current.get(field)
                for field in identity_fields
            ):
                continue
            verified_sources.append(source)
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
        if not records:
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
            requested_uid = str(
                snapshots.get(source.chunk_id, {}).get("decision_uid", "")
            ).strip()
            record_uid = str(record.get("decision_uid") or "").strip()
            if requested_uid and record_uid != requested_uid:
                continue
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


def _normalized_identifier(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", normalize_text(value))


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


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
        item.record_id,
    )


def _timeline_entry(record: Mapping[str, object]) -> TimelineEntry:
    return TimelineEntry(
        record_id=int(record["record_id"]),
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
    )


def search_timeline(
    database_path: Path,
    field: str,
    value: str,
    *,
    limit: int = 100,
    review_events: Iterable[ReviewEvent] = (),
) -> list[TimelineEntry]:
    """Busca una cronologia por un campo permitido mediante SQL parametrizado."""

    if field not in _TIMELINE_FIELDS:
        raise ValueError("Campo de cronologia no permitido")
    if not database_path.exists():
        return []
    bounded_limit = max(1, min(int(limit), MAX_TIMELINE_ROWS))
    normalized_column, display_column, is_identifier = _TIMELINE_FIELDS[field]
    if isinstance(value, str):
        if len(value) > MAX_TIMELINE_QUERY_CHARS:
            raise ValueError(
                f"La consulta no puede superar {MAX_TIMELINE_QUERY_CHARS} caracteres"
            )
        raw_value = value
    else:
        raw_value = str(value or "")[:MAX_TIMELINE_QUERY_CHARS]
    normalized_value = (
        _normalized_identifier(raw_value)
        if is_identifier
        else normalize_text(raw_value)
    )[:200]
    if len(normalized_value) < 2:
        raise ValueError("Escribe al menos dos caracteres para la cronologia")
    pattern = f"%{_escape_like(normalized_value)}%"

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
        comparable = (
            _normalized_identifier(corrected_value)
            if is_identifier
            else normalize_text(corrected_value)
        )
        if normalized_value in comparable:
            corrected_uids.append(uid)

    values: list[TimelineEntry] = []
    seen_records: set[str] = set()

    def add_row(row) -> None:
        raw = dict(row)
        uid = str(raw.get("decision_uid") or "")
        record = effective_record(raw, current_reviews.get(uid))
        effective_value = _bounded_text(
            record.get(display_column),
            MAX_CELL_CHARS,
        )
        effective_comparable = (
            _normalized_identifier(effective_value)
            if is_identifier
            else normalize_text(effective_value)
        )
        if normalized_value not in effective_comparable:
            return
        record_identity = uid or f"record:{int(record['record_id'])}"
        if record_identity in seen_records:
            return
        seen_records.add(record_identity)
        values.append(_timeline_entry(record))
        # Mantiene solo los N eventos cronologicamente mas tempranos. Es seguro
        # descartar los posteriores del lote ya procesado porque ningun registro
        # futuro puede hacer que vuelvan a entrar en el top N.
        if len(values) >= bounded_limit * 2:
            values.sort(key=_chronology_key)
            del values[bounded_limit:]

    with connect(database_path) as connection:
        connection.execute("BEGIN")
        columns = _record_columns(connection)
        if not columns:
            return []
        optional_sql = ",\n                   ".join(_optional_selects(columns))
        select_sql = f"""
            SELECT r.id AS record_id, r.record_key, d.title, d.url, d.year,
                   d.acta_number, d.section, d.part, d.source_type,
                   d.document_hash,
                   r.page_number, r.end_page_number, r.product_name,
                   r.active_ingredient, r.interested_party, r.expediente,
                   r.radicado, r.request_text, r.concept_text,
                   r.outcome_code,
                   {optional_sql}
            FROM regulatory_records r
            JOIN documents d ON d.id = r.document_id
        """
        cursor = connection.execute(
            select_sql
            + f" WHERE r.{normalized_column} LIKE ? ESCAPE '\\'",
            (pattern,),
        )
        while True:
            batch = cursor.fetchmany(500)
            if not batch:
                break
            for row in batch:
                add_row(row)

        # Una correccion puede introducir el valor buscado aunque el valor
        # automatico no coincida; esas fichas se recuperan por UID en lotes.
        if corrected_uids and "decision_uid" in columns:
            for start in range(0, len(corrected_uids), 800):
                uid_batch = corrected_uids[start : start + 800]
                placeholders = ",".join("?" for _ in uid_batch)
                corrected_cursor = connection.execute(
                    select_sql
                    + f" WHERE r.decision_uid IN ({placeholders})",
                    uid_batch,
                )
                for row in corrected_cursor:
                    add_row(row)

    values.sort(key=_chronology_key)
    return values[:bounded_limit]


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
}


def outcome_label(value: str | None) -> str:
    if not value:
        return "Sin ficha estructurada"
    return _OUTCOME_LABELS.get(value, value.replace("_", " ").title())


def request_type_label(value: str | None) -> str:
    if not value:
        return "No extraido"
    return value.replace("_", " ").capitalize()


def review_status_label(value: str | None) -> str:
    if not value:
        return "Automática"
    return _REVIEW_STATUS_LABELS.get(value, value.replace("_", " ").capitalize())


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
        (
            "Estado de revisión",
            _decision_review_label,
        ),
        ("Revisor", lambda item: item.reviewer),
        ("Fecha de revisión", lambda item: item.reviewed_at),
        ("Notas de revisión", lambda item: item.review_notes),
    ]
    matrix: list[dict[str, str]] = []
    for field_label, getter in fields:
        row = {"Campo": field_label}
        for index, decision in enumerate(selected, start=1):
            row[f"Decision {index}"] = _bounded_text(getter(decision)) or "—"
        matrix.append(row)
    return matrix


def _safe_csv_cell(value: object) -> str:
    text = _bounded_text(value)
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
        "estado_revision",
        "revisor",
        "fecha_revision",
        "notas_revision",
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
                _decision_review_label(item),
                item.reviewer,
                item.reviewed_at,
                item.review_notes,
                _page_href(item.url, item.page_number),
            )
        )
    return _csv_bytes(headers, rows)


def timeline_to_csv(entries: Sequence[TimelineEntry]) -> bytes:
    headers = (
        "decision_uid",
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
        "estado_revision",
        "revisor",
        "fecha_revision",
        "notas_revision",
        "fuente",
    )
    rows = (
        (
            item.decision_uid,
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
            review_status_label(item.review_status),
            item.reviewer,
            item.reviewed_at,
            item.review_notes,
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
    text = _bounded_text(value)
    return escape(text).replace("\n", "<br>") if text else "—"


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
                f"<td>{_html_value(item.expediente)}</td>"
                f"<td>{_html_value(item.radicado)}</td>"
                f"<td>{_html_value(outcome_label(item.outcome_code))}</td>"
                f"<td>{_html_value(review_status_label(item.review_status))}</td>"
                f"<td>{_html_link(item.url, item.page_number, 'Pagina ' + str(item.page_number))}</td>"
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
            details.append(
                "<article class='timeline-detail'>"
                f"<h3>{index}. {_html_value(item.label)}</h3>"
                f"<p><strong>Producto:</strong> {_html_value(item.product_name)}"
                f" · <strong>Resultado:</strong> "
                f"{_html_value(outcome_label(item.outcome_code))}</p>"
                f"<p><strong>Solicitud:</strong> {_html_value(request)}</p>"
                f"<p><strong>Concepto:</strong> {_html_value(concept)}</p>"
                f"<p>{_html_link(item.url, item.page_number, 'Abrir evidencia')}</p>"
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
            "<th>Producto</th><th>Expediente</th><th>Radicado</th>"
            "<th>Resultado</th><th>Revision</th><th>Fuente</th>"
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
<p>Extracciones automáticas: verifica siempre los campos contra las páginas enlazadas.</p>
{comparison_section}
{evidence_section}
{timeline_section}
</body></html>"""
    encoded = document.encode("utf-8")
    if len(encoded) > MAX_HTML_BYTES:
        raise ValueError("El reporte supera el limite de tamano permitido")
    return encoded
