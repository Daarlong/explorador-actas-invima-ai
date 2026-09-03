from __future__ import annotations

import csv
import io
import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from services.effective_records import EFFECTIVE_FIELDS


REVIEW_STATUSES = ("reviewed", "approved", "reopened")
OUTCOME_CODES = (
    "aprobado",
    "negado",
    "requerido",
    "desistido",
    "archivado",
    "favorable",
    "no_favorable",
    "sin_clasificar",
)
REQUEST_TYPE_CODES = (
    "renovacion_registro",
    "modificacion_registro",
    "evaluacion_farmacologica",
    "registro_sanitario",
    "indicaciones",
    "informacion_prescribir",
    "cambio_fabricante",
    "cambio_titular",
    "recurso_reposicion",
    "cancelacion",
    "otra_solicitud",
)
REVIEW_FIELDS = EFFECTIVE_FIELDS
CSV_FIELDS = (
    "event_id",
    "decision_uid",
    "source_record_key",
    "source_document_hash",
    "status",
    "reviewer",
    "reviewed_at",
    "notes",
    "corrections_json",
)


@dataclass(frozen=True)
class ReviewEvent:
    event_id: str
    decision_uid: str
    source_record_key: str
    source_document_hash: str
    status: str
    reviewer: str
    reviewed_at: str
    notes: str
    corrections: dict[str, object]

    def as_csv_dict(self) -> dict[str, str]:
        values = asdict(self)
        values.pop("corrections")
        values["corrections_json"] = json.dumps(
            self.corrections,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return values


def _clean_text(value: object, *, maximum: int) -> str:
    return " ".join(str(value or "").split())[:maximum]


def _utc_timestamp(value: object) -> tuple[str, datetime]:
    """Valida un instante inequívoco y lo normaliza a UTC."""

    text = _clean_text(value, maximum=80)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Fecha de revisión inválida") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("La fecha de revisión debe incluir zona horaria")
    utc = parsed.astimezone(timezone.utc)
    return utc.isoformat(), utc


def validate_corrections(corrections: Mapping[str, object]) -> dict[str, object]:
    unknown = set(corrections) - set(REVIEW_FIELDS)
    if unknown:
        raise ValueError(f"Campos de corrección desconocidos: {sorted(unknown)}")
    clean: dict[str, object] = {}
    for field, value in corrections.items():
        if field in {"page_number", "end_page_number"}:
            try:
                number = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{field} debe ser un número entero") from exc
            if number < 1:
                raise ValueError(f"{field} debe ser mayor que cero")
            clean[field] = number
            continue
        maximum = 20000 if field in {"request_text", "concept_text"} else 1000
        clean[field] = _clean_text(value, maximum=maximum)
    if (
        "page_number" in clean
        and "end_page_number" in clean
        and int(clean["end_page_number"]) < int(clean["page_number"])
    ):
        raise ValueError("La página final no puede ser anterior a la inicial")
    session_date = str(clean.get("session_date", ""))
    if session_date:
        try:
            datetime.strptime(session_date, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("La fecha de sesión debe usar AAAA-MM-DD") from exc
    outcome = str(clean.get("outcome_code", ""))
    if outcome and outcome not in OUTCOME_CODES:
        raise ValueError("Resultado normalizado no permitido")
    request_type = str(clean.get("request_type_code", ""))
    if request_type and request_type not in REQUEST_TYPE_CODES:
        raise ValueError("Tipo de solicitud normalizado no permitido")
    return clean


def new_review_event(
    record: Mapping[str, object],
    *,
    status: str,
    reviewer: str,
    notes: str,
    corrections: Mapping[str, object],
    reviewed_at: str | None = None,
) -> ReviewEvent:
    if status not in REVIEW_STATUSES:
        raise ValueError("Estado de revisión no permitido")
    clean_reviewer = _clean_text(reviewer, maximum=200)
    if not clean_reviewer:
        raise ValueError("Debes identificar al revisor")
    decision_uid = _clean_text(record.get("decision_uid"), maximum=100)
    if not decision_uid:
        raise ValueError("La ficha no tiene un identificador estable")
    when, _ = _utc_timestamp(reviewed_at or datetime.now(timezone.utc).isoformat())
    clean_corrections = validate_corrections(corrections)
    effective_start = int(
        clean_corrections.get("page_number", record.get("page_number") or 1)
    )
    effective_end = int(
        clean_corrections.get(
            "end_page_number",
            record.get("end_page_number") or effective_start,
        )
    )
    if effective_end < effective_start:
        raise ValueError("La página final no puede ser anterior a la inicial")
    pdf_page_count = int(record.get("pdf_page_count") or 0)
    if pdf_page_count and effective_end > pdf_page_count:
        raise ValueError(
            f"La página final no puede superar las {pdf_page_count} páginas del PDF"
        )
    source_record_key = _clean_text(record.get("record_key"), maximum=128)
    source_document_hash = _clean_text(record.get("document_hash"), maximum=128)
    if not source_record_key or not source_document_hash:
        raise ValueError("La ficha no conserva las huellas de su extracción y PDF")
    return ReviewEvent(
        event_id=str(uuid.uuid4()),
        decision_uid=decision_uid,
        source_record_key=source_record_key,
        source_document_hash=source_document_hash,
        status=status,
        reviewer=clean_reviewer,
        reviewed_at=when,
        notes=_clean_text(notes, maximum=4000),
        corrections=clean_corrections,
    )


def parse_review_events(payload: str) -> list[ReviewEvent]:
    if not payload.strip():
        return []
    reader = csv.DictReader(io.StringIO(payload))
    if not reader.fieldnames or not set(CSV_FIELDS).issubset(reader.fieldnames):
        raise ValueError("El archivo de revisiones no contiene las columnas esperadas")
    events: list[ReviewEvent] = []
    seen: set[str] = set()
    for line_number, row in enumerate(reader, start=2):
        event_id = _clean_text(row.get("event_id"), maximum=100)
        if not event_id or event_id in seen:
            raise ValueError(f"event_id ausente o duplicado en la línea {line_number}")
        seen.add(event_id)
        status = _clean_text(row.get("status"), maximum=40)
        if status not in REVIEW_STATUSES:
            raise ValueError(f"Estado inválido en la línea {line_number}")
        decision_uid = _clean_text(row.get("decision_uid"), maximum=100)
        reviewer = _clean_text(row.get("reviewer"), maximum=200)
        reviewed_at = _clean_text(row.get("reviewed_at"), maximum=80)
        if not decision_uid or not reviewer:
            raise ValueError(
                f"decision_uid o reviewer vacío en la línea {line_number}"
            )
        try:
            reviewed_at, _ = _utc_timestamp(reviewed_at)
        except ValueError as exc:
            raise ValueError(f"reviewed_at inválido en la línea {line_number}: {exc}") from exc
        source_record_key = _clean_text(row.get("source_record_key"), maximum=128)
        source_document_hash = _clean_text(
            row.get("source_document_hash"), maximum=128
        )
        if not source_record_key or not source_document_hash:
            raise ValueError(
                "Las huellas source_record_key/source_document_hash son "
                f"obligatorias en la línea {line_number}"
            )
        try:
            corrections_raw = json.loads(row.get("corrections_json") or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"corrections_json inválido en la línea {line_number}"
            ) from exc
        if not isinstance(corrections_raw, dict):
            raise ValueError(f"Correcciones inválidas en la línea {line_number}")
        events.append(
            ReviewEvent(
                event_id=event_id,
                decision_uid=decision_uid,
                source_record_key=source_record_key,
                source_document_hash=source_document_hash,
                status=status,
                reviewer=reviewer,
                reviewed_at=reviewed_at,
                notes=_clean_text(row.get("notes"), maximum=4000),
                corrections=validate_corrections(corrections_raw),
            )
        )
    return events


def serialize_review_events(events: Iterable[ReviewEvent]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_FIELDS)
    writer.writeheader()
    writer.writerows(event.as_csv_dict() for event in events)
    return output.getvalue()


def load_review_events(path: Path) -> list[ReviewEvent]:
    if not path.exists():
        return []
    return parse_review_events(path.read_text(encoding="utf-8-sig"))


def write_review_events(events: Iterable[ReviewEvent], path: Path) -> None:
    payload = serialize_review_events(events)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f"{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(payload)
        Path(temporary_name).replace(path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


def append_review_event(path: Path, event: ReviewEvent) -> list[ReviewEvent]:
    events = load_review_events(path)
    if any(existing.event_id == event.event_id for existing in events):
        return events
    events.append(event)
    write_review_events(events, path)
    return events


def latest_reviews(events: Iterable[ReviewEvent]) -> dict[str, ReviewEvent]:
    latest: dict[str, ReviewEvent] = {}
    for event in events:
        current = latest.get(event.decision_uid)
        event_instant = _utc_timestamp(event.reviewed_at)[1]
        current_key = (
            (_utc_timestamp(current.reviewed_at)[1], current.event_id)
            if current is not None
            else None
        )
        if current_key is None or (event_instant, event.event_id) >= current_key:
            latest[event.decision_uid] = event
    return latest


def effective_record(
    record: Mapping[str, object],
    event: ReviewEvent | None,
) -> dict[str, object]:
    # Importación local para conservar la API histórica y evitar que el módulo
    # de valores vigentes dependa del formato CSV de revisiones.
    from services.effective_records import effective_record as resolve_record

    return resolve_record(record, event)


def apply_latest_reviews(
    records: Iterable[Mapping[str, object]],
    events: Iterable[ReviewEvent],
) -> list[dict[str, object]]:
    from services.effective_records import apply_effective_records

    current = latest_reviews(events)
    return apply_effective_records(records, current)
