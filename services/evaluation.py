"""Evaluación reproducible de la recuperación y auditoría del corpus.

El banco de consultas usa CSV para que pueda revisarse en Excel, incluirse en
el repositorio y ejecutarse de nuevo después de cada cambio del índice. Las
referencias esperadas admiten tres formatos:

* ``url:https://...``
* ``title:Acta No 01 de 2026 SEMPB``
* ``acta:2026:01:SEMPB`` (la sala es opcional)

Las métricas se calculan sobre documentos únicos, no sobre fragmentos. Esto
evita premiar a un modo por devolver varias páginas de una misma acta.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from services.database import connect
from services.integrity import REGULATORY_COMPLETENESS_FIELDS
from services.models import SearchResult
from services.search import SEARCH_MODES, search_corpus
from services.text_utils import normalize_text


EVALUATION_COLUMNS = (
    "case_id",
    "query",
    "expected_refs",
    "filters_json",
    "k",
    "exact_phrase",
    "notes",
    "enabled",
)
ALLOWED_FILTERS = frozenset(
    {
        "years",
        "acta_numbers",
        "sections",
        "parts",
        "outcomes",
        "request_types",
        "products",
        "active_ingredients",
        "interested_parties",
        "identifiers",
    }
)
MAX_CASES = 250
MAX_TOP_K = 50
RELEASE_MIN_ENABLED_CASES = 15
RELEASE_MIN_EXPECTED_DECISIONS = 30
RELEASE_COMPLETENESS_FIELDS = (
    "numeral",
    "product",
    "active_ingredient",
    "interested_party",
    "expediente",
    "radicado",
)
_CASE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_DANGEROUS_CSV_PREFIXES = ("=", "+", "-", "@")


@dataclass(frozen=True)
class EvaluationCase:
    case_id: str
    query: str
    expected_refs: tuple[str, ...]
    filters: dict[str, list]
    k: int = 10
    exact_phrase: bool = False
    notes: str = ""
    enabled: bool = True

    def as_row(self) -> dict[str, str]:
        return {
            "case_id": self.case_id,
            "query": self.query,
            "expected_refs": " | ".join(self.expected_refs),
            "filters_json": json.dumps(
                self.filters,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "k": str(self.k),
            "exact_phrase": "true" if self.exact_phrase else "false",
            "notes": self.notes,
            "enabled": "true" if self.enabled else "false",
        }


@dataclass(frozen=True)
class ExpectedReference:
    raw: str
    kind: str
    value: str
    year: int | None = None
    acta_number: str | None = None
    section: str | None = None


@dataclass(frozen=True)
class EvaluationResult:
    case_id: str
    query: str
    requested_mode: str
    used_mode: str
    k: int
    expected_count: int
    matched_count: int
    retrieved_documents: int
    relevant_documents: int
    hit_at_k: float
    precision_at_k: float
    recall_at_k: float
    reciprocal_rank: float
    first_relevant_rank: int | None
    duration_ms: float
    matched_refs: tuple[str, ...]
    top_documents: tuple[str, ...]
    semantic_available: bool
    semantic_message: str
    error: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)

    def as_export_row(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "consulta": self.query,
            "modo_solicitado": self.requested_mode,
            "modo_usado": self.used_mode,
            "k": self.k,
            "esperados": self.expected_count,
            "coincidencias": self.matched_count,
            "documentos_recuperados": self.retrieved_documents,
            "documentos_relevantes": self.relevant_documents,
            "hit_at_k": round(self.hit_at_k, 6),
            "precision_at_k": round(self.precision_at_k, 6),
            "recall_at_k": round(self.recall_at_k, 6),
            "mrr": round(self.reciprocal_rank, 6),
            "primera_posicion_relevante": self.first_relevant_rank or "",
            "duracion_ms": round(self.duration_ms, 2),
            "referencias_encontradas": " | ".join(self.matched_refs),
            "primeros_documentos": " | ".join(self.top_documents),
            "semantica_disponible": self.semantic_available,
            "mensaje_semantico": self.semantic_message,
            "error": self.error or "",
        }


def _parse_boolean(value: object, *, default: bool) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "si", "sí", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"valor booleano no reconocido: {value}")


def _normalize_acta_number(value: object) -> str:
    normalized = normalize_text(str(value or "")).strip()
    if normalized.isdigit():
        return str(int(normalized))
    return normalized


def _canonical_url(value: str) -> str:
    text = value.strip()
    parts = urlsplit(text)
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        raise ValueError("la referencia URL debe ser http o https")
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), path, parts.query, "")
    )


def parse_expected_reference(value: str) -> ExpectedReference:
    raw = value.strip()
    if not raw:
        raise ValueError("la referencia esperada está vacía")
    prefix, separator, payload = raw.partition(":")
    if not separator:
        raise ValueError(
            "usa url:, title: o acta: antes de cada referencia esperada"
        )
    kind = prefix.strip().lower()
    payload = payload.strip()
    if kind == "url":
        return ExpectedReference(raw=raw, kind=kind, value=_canonical_url(payload))
    if kind == "title":
        normalized = normalize_text(payload)
        if not normalized:
            raise ValueError("title: requiere el título completo")
        return ExpectedReference(raw=raw, kind=kind, value=normalized)
    if kind == "acta":
        parts = [item.strip() for item in payload.split(":")]
        if len(parts) not in {2, 3}:
            raise ValueError("acta: debe usar acta:AÑO:NÚMERO[:SALA]")
        try:
            year = int(parts[0])
        except ValueError as exc:
            raise ValueError("el año de acta: debe ser numérico") from exc
        if year < 1900 or year > 2200:
            raise ValueError("el año de acta: está fuera de rango")
        acta_number = _normalize_acta_number(parts[1])
        if not acta_number:
            raise ValueError("acta: requiere un número")
        section = normalize_text(parts[2]) if len(parts) == 3 else None
        return ExpectedReference(
            raw=raw,
            kind=kind,
            value=raw,
            year=year,
            acta_number=acta_number,
            section=section or None,
        )
    raise ValueError(f"tipo de referencia no admitido: {prefix}")


def _split_expected_refs(value: str) -> tuple[str, ...]:
    refs = tuple(item.strip() for item in re.split(r"[|\n]", value) if item.strip())
    if not refs:
        raise ValueError("se requiere al menos una referencia esperada")
    for ref in refs:
        parse_expected_reference(ref)
    return refs


def _normalize_filters(value: object) -> dict[str, list]:
    if value in (None, ""):
        return {}
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"filters_json no es JSON válido: {exc.msg}") from exc
    else:
        decoded = value
    if not isinstance(decoded, dict):
        raise ValueError("filters_json debe ser un objeto JSON")
    unknown = sorted(set(decoded) - ALLOWED_FILTERS)
    if unknown:
        raise ValueError("filtros desconocidos: " + ", ".join(unknown))
    filters: dict[str, list] = {}
    for key, raw_values in decoded.items():
        values = raw_values if isinstance(raw_values, list) else [raw_values]
        clean = [item for item in values if item not in (None, "")]
        if key == "years":
            try:
                clean = [int(item) for item in clean]
            except (TypeError, ValueError) as exc:
                raise ValueError("years solo admite años numéricos") from exc
        filters[key] = clean
    return filters


def parse_evaluation_cases_csv(
    content: str,
    *,
    max_cases: int = MAX_CASES,
) -> tuple[list[EvaluationCase], list[str]]:
    """Lee casos válidos y devuelve los errores por fila sin abortar la UI."""
    if not content.strip():
        return [], []
    try:
        reader = csv.DictReader(io.StringIO(content.lstrip("\ufeff")))
    except csv.Error as exc:
        return [], [f"No fue posible leer el CSV: {exc}"]
    headers = set(reader.fieldnames or [])
    required = {"case_id", "query", "expected_refs"}
    missing = sorted(required - headers)
    if missing:
        return [], ["Faltan columnas obligatorias: " + ", ".join(missing)]

    cases: list[EvaluationCase] = []
    errors: list[str] = []
    identifiers: set[str] = set()
    try:
        rows = enumerate(reader, start=2)
        for row_number, row in rows:
            if len(cases) >= max_cases:
                errors.append(f"El banco supera el máximo de {max_cases} casos.")
                break
            if not any(str(value or "").strip() for value in row.values()):
                continue
            try:
                case_id = str(row.get("case_id") or "").strip()
                if not _CASE_ID_PATTERN.fullmatch(case_id):
                    raise ValueError(
                        "case_id debe empezar por letra o número y usar solo "
                        "letras, números, punto, guion o guion bajo"
                    )
                if case_id in identifiers:
                    raise ValueError(f"case_id duplicado: {case_id}")
                query = str(row.get("query") or "").strip()
                if not query:
                    raise ValueError("query está vacía")
                if len(query) > 1000:
                    raise ValueError("query supera 1000 caracteres")
                expected_refs = _split_expected_refs(
                    str(row.get("expected_refs") or "")
                )
                filters = _normalize_filters(row.get("filters_json"))
                raw_k = str(row.get("k") or "10").strip()
                k = int(raw_k)
                if k < 1 or k > MAX_TOP_K:
                    raise ValueError(f"k debe estar entre 1 y {MAX_TOP_K}")
                case = EvaluationCase(
                    case_id=case_id,
                    query=query,
                    expected_refs=expected_refs,
                    filters=filters,
                    k=k,
                    exact_phrase=_parse_boolean(
                        row.get("exact_phrase"), default=False
                    ),
                    notes=str(row.get("notes") or "").strip(),
                    enabled=_parse_boolean(row.get("enabled"), default=True),
                )
            except (TypeError, ValueError) as exc:
                errors.append(f"Fila {row_number}: {exc}")
                continue
            identifiers.add(case.case_id)
            cases.append(case)
    except csv.Error as exc:
        errors.append(f"CSV inválido: {exc}")
    return cases, errors


def load_evaluation_cases(path: Path) -> tuple[list[EvaluationCase], list[str]]:
    if not path.exists():
        return [], []
    try:
        content = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        return [], [f"No fue posible leer {path.name}: {exc}"]
    return parse_evaluation_cases_csv(content)


def evaluation_cases_to_csv(cases: Iterable[EvaluationCase]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=EVALUATION_COLUMNS)
    writer.writeheader()
    for case in cases:
        writer.writerow(case.as_row())
    return output.getvalue()


def evaluation_template_csv() -> str:
    """Plantilla segura: ningún caso se habilita sin validación humana."""
    examples = [
        EvaluationCase(
            case_id="ejemplo-001",
            query="producto o concepto que deseas recuperar",
            expected_refs=("title:REEMPLAZA POR EL TÍTULO OFICIAL VALIDADO",),
            filters={},
            k=10,
            notes="Completa la fuente esperada y habilita solo después de revisarla.",
            enabled=False,
        ),
        EvaluationCase(
            case_id="semaglutida-2017",
            query="Semaglutida",
            expected_refs=("acta:2017:14:SEMPB",),
            filters={"years": [2017]},
            k=10,
            notes=(
                "Caso obligatorio 0.7: comprueba la fuente oficial y cambia enabled "
                "a true solo después de la revisión humana."
            ),
            enabled=False,
        ),
    ]
    return evaluation_cases_to_csv(examples)


def _result_matches(
    result: SearchResult,
    expected: ExpectedReference,
    *,
    url_aliases: Sequence[str] = (),
) -> bool:
    if expected.kind == "url":
        for value in (result.url, *url_aliases):
            try:
                if _canonical_url(value) == expected.value:
                    return True
            except ValueError:
                continue
        return False
    if expected.kind == "title":
        return normalize_text(result.title) == expected.value
    if expected.kind == "acta":
        if result.year != expected.year:
            return False
        if _normalize_acta_number(result.acta_number) != expected.acta_number:
            return False
        return not expected.section or (
            normalize_text(result.section or "") == expected.section
        )
    return False


def _unique_documents(
    results: Sequence[SearchResult],
    *,
    limit: int,
) -> list[SearchResult]:
    selected: list[SearchResult] = []
    identities: set[tuple] = set()
    for result in results:
        try:
            url = _canonical_url(result.url)
        except ValueError:
            url = result.url.strip()
        identity = (
            url,
            normalize_text(result.title),
            result.year,
            _normalize_acta_number(result.acta_number),
            normalize_text(result.part or ""),
        )
        if identity in identities:
            continue
        identities.add(identity)
        selected.append(result)
        if len(selected) >= limit:
            break
    return selected


def _document_url_aliases(
    database_path: Path,
    chunk_ids: Iterable[int],
) -> dict[int, tuple[str, ...]]:
    """Obtiene URL original y resuelta para evaluar enlaces sin falsos fallos."""
    values = list(dict.fromkeys(int(value) for value in chunk_ids))
    if not values or not database_path.exists():
        return {}
    aliases: dict[int, tuple[str, ...]] = {}
    try:
        with connect(database_path) as connection:
            for start in range(0, len(values), 800):
                batch = values[start : start + 800]
                placeholders = ",".join("?" for _ in batch)
                rows = connection.execute(
                    f"""
                    SELECT c.id AS chunk_id, d.url, d.manifest_url
                    FROM chunks c
                    JOIN pages p ON p.id = c.page_id
                    JOIN documents d ON d.id = p.document_id
                    WHERE c.id IN ({placeholders})
                    """,
                    batch,
                ).fetchall()
                for row in rows:
                    aliases[int(row["chunk_id"])] = tuple(
                        dict.fromkeys(
                            value
                            for value in (row["url"], row["manifest_url"])
                            if value
                        )
                    )
    except Exception:
        # Los bancos con title:/acta: siguen siendo evaluables en esquemas
        # antiguos o copias parciales que no tengan manifest_url.
        return {}
    return aliases


def _empty_result(
    case: EvaluationCase,
    mode: str,
    *,
    duration_ms: float,
    error: str,
) -> EvaluationResult:
    return EvaluationResult(
        case_id=case.case_id,
        query=case.query,
        requested_mode=mode,
        used_mode="error",
        k=case.k,
        expected_count=len(case.expected_refs),
        matched_count=0,
        retrieved_documents=0,
        relevant_documents=0,
        hit_at_k=0.0,
        precision_at_k=0.0,
        recall_at_k=0.0,
        reciprocal_rank=0.0,
        first_relevant_rank=None,
        duration_ms=duration_ms,
        matched_refs=(),
        top_documents=(),
        semantic_available=False,
        semantic_message="",
        error=error,
    )


def evaluate_case(
    database_path: Path,
    semantic_index_path: Path,
    case: EvaluationCase,
    mode: str,
) -> EvaluationResult:
    if mode not in SEARCH_MODES:
        raise ValueError(f"Modo desconocido: {mode}")
    started = time.perf_counter()
    try:
        response = search_corpus(
            database_path,
            semantic_index_path,
            case.query,
            mode=mode,
            top_k=max(50, min(case.k * 5, 250)),
            filters=case.filters,
            exact_phrase=case.exact_phrase,
        )
        documents = _unique_documents(response.results, limit=case.k)
        url_aliases = _document_url_aliases(
            database_path,
            (result.chunk_id for result in documents),
        )
        expected = [parse_expected_reference(ref) for ref in case.expected_refs]
        matched_refs: list[str] = []
        relevant_ranks: set[int] = set()
        for reference in expected:
            matching_rank = next(
                (
                    rank
                    for rank, result in enumerate(documents, start=1)
                    if _result_matches(
                        result,
                        reference,
                        url_aliases=url_aliases.get(result.chunk_id, ()),
                    )
                ),
                None,
            )
            if matching_rank is not None:
                matched_refs.append(reference.raw)
                relevant_ranks.add(matching_rank)
        first_rank = min(relevant_ranks) if relevant_ranks else None
        expected_count = len(expected)
        return EvaluationResult(
            case_id=case.case_id,
            query=case.query,
            requested_mode=mode,
            used_mode=response.used_mode,
            k=case.k,
            expected_count=expected_count,
            matched_count=len(matched_refs),
            retrieved_documents=len(documents),
            relevant_documents=len(relevant_ranks),
            hit_at_k=1.0 if relevant_ranks else 0.0,
            precision_at_k=len(relevant_ranks) / case.k,
            recall_at_k=(len(matched_refs) / expected_count) if expected_count else 0.0,
            reciprocal_rank=(1.0 / first_rank) if first_rank else 0.0,
            first_relevant_rank=first_rank,
            duration_ms=(time.perf_counter() - started) * 1000,
            matched_refs=tuple(matched_refs),
            top_documents=tuple(
                f"{rank}. {result.title} — p. {result.page}"
                for rank, result in enumerate(documents, start=1)
            ),
            semantic_available=response.semantic_available,
            semantic_message=response.semantic_message,
        )
    except Exception as exc:  # una consulta no debe interrumpir todo el banco
        return _empty_result(
            case,
            mode,
            duration_ms=(time.perf_counter() - started) * 1000,
            error=str(exc)[:500],
        )


def evaluate_cases(
    database_path: Path,
    semantic_index_path: Path,
    cases: Iterable[EvaluationCase],
    modes: Iterable[str] = SEARCH_MODES,
) -> list[EvaluationResult]:
    requested_modes = tuple(dict.fromkeys(modes))
    invalid = [mode for mode in requested_modes if mode not in SEARCH_MODES]
    if invalid:
        raise ValueError("Modos desconocidos: " + ", ".join(invalid))
    results: list[EvaluationResult] = []
    for case in cases:
        if not case.enabled:
            continue
        for mode in requested_modes:
            results.append(
                evaluate_case(database_path, semantic_index_path, case, mode)
            )
    return results


def summarize_evaluation(
    results: Iterable[EvaluationResult],
) -> list[dict[str, object]]:
    grouped: dict[str, list[EvaluationResult]] = {}
    for result in results:
        grouped.setdefault(result.requested_mode, []).append(result)
    summary: list[dict[str, object]] = []
    for mode in SEARCH_MODES:
        rows = grouped.get(mode, [])
        valid = [row for row in rows if not row.error]
        divisor = len(valid)

        def mean(attribute: str) -> float:
            if not divisor:
                return 0.0
            return sum(float(getattr(row, attribute)) for row in valid) / divisor

        summary.append(
            {
                "mode": mode,
                "cases": len(rows),
                "evaluated": divisor,
                "errors": len(rows) - divisor,
                "fallbacks": sum(
                    row.used_mode != row.requested_mode for row in valid
                ),
                "hit_at_k": mean("hit_at_k"),
                "precision_at_k": mean("precision_at_k"),
                "recall_at_k": mean("recall_at_k"),
                "mrr": mean("reciprocal_rank"),
                "mean_duration_ms": mean("duration_ms"),
            }
        )
    return [row for row in summary if row["cases"]]


def evaluation_bank_profile(cases: Iterable[EvaluationCase]) -> dict:
    """Describe si el banco cumple el mínimo acordado sin inventar casos oro."""
    rows = list(cases)
    enabled = [case for case in rows if case.enabled]
    placeholder_refs = sorted(
        {
            reference.strip()
            for case in enabled
            for reference in case.expected_refs
            if any(
                marker in normalize_text(reference)
                for marker in ("reemplaza", "placeholder", "por validar")
            )
        }
    )
    expected_refs = {
        reference.strip()
        for case in enabled
        for reference in case.expected_refs
        if reference.strip()
        and reference.strip() not in placeholder_refs
    }
    semaglutida_cases = [
        case.case_id
        for case in enabled
        if "semaglutida" in normalize_text(f"{case.query} {case.notes}")
        and not any(reference in placeholder_refs for reference in case.expected_refs)
    ]
    return {
        "cases_total": len(rows),
        "cases_enabled": len(enabled),
        "unique_expected_refs": len(expected_refs),
        "placeholder_refs": placeholder_refs,
        "semaglutida_case_ids": semaglutida_cases,
        "has_semaglutida_case": bool(semaglutida_cases),
        "minimum_enabled_cases": RELEASE_MIN_ENABLED_CASES,
        "minimum_expected_decisions": RELEASE_MIN_EXPECTED_DECISIONS,
        "signature": evaluation_bank_signature(rows),
    }


def evaluation_bank_signature(cases: Iterable[EvaluationCase]) -> str:
    enabled = [case for case in cases if case.enabled]
    payload = evaluation_cases_to_csv(enabled).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _quality_payload(value: Mapping | None) -> Mapping | None:
    if not value:
        return None
    nested = value.get("extractor_quality")
    if isinstance(nested, Mapping):
        return nested
    nested = value.get("regulatory_quality_snapshot")
    if isinstance(nested, Mapping):
        return nested
    return value if "fields" in value and "records" in value else None


def compare_extractor_quality(
    current: Mapping | None,
    baseline: Mapping | None,
) -> dict:
    """Compara cobertura automática; precisión requiere anotación humana aparte."""
    current_quality = _quality_payload(current)
    baseline_quality = _quality_payload(baseline)
    if not current_quality or not baseline_quality:
        return {
            "status": "unavailable",
            "reason": "No existe una línea base comparable de extracción.",
            "record_delta": None,
            "fields": [],
        }
    if (
        current_quality.get("status") != "available"
        or baseline_quality.get("status") != "available"
    ):
        return {
            "status": "unavailable",
            "reason": "La extracción actual o la línea base no está disponible.",
            "record_delta": None,
            "fields": [],
        }

    def by_key(payload: Mapping) -> dict[str, Mapping]:
        return {
            str(item.get("key")): item
            for item in payload.get("fields") or []
            if isinstance(item, Mapping) and item.get("key")
        }

    current_fields = by_key(current_quality)
    baseline_fields = by_key(baseline_quality)
    ordered_keys = list(
        dict.fromkeys(
            [item[0] for item in REGULATORY_COMPLETENESS_FIELDS]
            + list(baseline_fields)
            + list(current_fields)
        )
    )
    comparisons: list[dict] = []
    for key in ordered_keys:
        before = baseline_fields.get(key)
        after = current_fields.get(key)
        before_percent = (
            _safe_float(before.get("coverage_percent"))
            if before and before.get("coverage_percent") is not None
            else None
        )
        after_percent = (
            _safe_float(after.get("coverage_percent"))
            if after and after.get("coverage_percent") is not None
            else None
        )
        comparable = bool(
            before
            and after
            and before.get("available")
            and after.get("available")
            and before_percent is not None
            and after_percent is not None
        )
        comparisons.append(
            {
                "key": key,
                "label": str(
                    (after or before or {}).get("label") or key.replace("_", " ")
                ),
                "comparable": comparable,
                "baseline_present": _safe_int((before or {}).get("present")),
                "current_present": _safe_int((after or {}).get("present")),
                "present_delta": (
                    _safe_int((after or {}).get("present"))
                    - _safe_int((before or {}).get("present"))
                    if comparable
                    else None
                ),
                "baseline_percent": before_percent,
                "current_percent": after_percent,
                "delta_percentage_points": round(after_percent - before_percent, 4)
                if comparable
                else None,
            }
        )
    return {
        "status": "comparable",
        "reason": "",
        "baseline_records": _safe_int(baseline_quality.get("records")),
        "current_records": _safe_int(current_quality.get("records")),
        "record_delta": _safe_int(current_quality.get("records"))
        - _safe_int(baseline_quality.get("records")),
        "fields": comparisons,
        "note": (
            "La comparación mide completitud automática. No estima precisión sin "
            "un patrón oro de campos revisados."
        ),
    }


def compare_retrieval_summaries(
    current: Sequence[Mapping],
    baseline: Sequence[Mapping] | None,
    *,
    current_bank_signature: str | None = None,
    baseline_bank_signature: str | None = None,
) -> dict:
    if (
        current_bank_signature is not None
        and baseline_bank_signature != current_bank_signature
    ):
        return {
            "status": "unavailable",
            "reason": "La línea base corresponde a otro banco de consultas.",
            "rows": [],
        }
    if not baseline:
        return {
            "status": "unavailable",
            "reason": "No existe una línea base comparable de recuperación.",
            "rows": [],
        }
    before_by_mode = {
        str(row.get("mode")): row for row in baseline if isinstance(row, Mapping)
    }
    after_by_mode = {
        str(row.get("mode")): row for row in current if isinstance(row, Mapping)
    }
    rows: list[dict] = []
    for mode in SEARCH_MODES:
        before = before_by_mode.get(mode)
        after = after_by_mode.get(mode)
        if not before or not after:
            rows.append({"mode": mode, "comparable": False})
            continue
        row: dict[str, object] = {"mode": mode, "comparable": True}
        for metric in ("hit_at_k", "recall_at_k", "mrr"):
            before_value = _safe_float(before.get(metric))
            after_value = _safe_float(after.get(metric))
            row[f"baseline_{metric}"] = before_value
            row[f"current_{metric}"] = after_value
            row[f"delta_{metric}"] = round(after_value - before_value, 6)
        rows.append(row)
    return {
        "status": "comparable"
        if rows and all(row.get("comparable") for row in rows)
        else "incomplete",
        "reason": "",
        "rows": rows,
    }


def build_quality_gate(
    *,
    release_mode: bool,
    cases: Iterable[EvaluationCase],
    results: Iterable[EvaluationResult],
    extractor_quality: Mapping | None,
    extractor_comparison: Mapping | None,
    retrieval_comparison: Mapping | None,
    integrity_report: Mapping | None,
    bank_errors: Sequence[str] = (),
    minimum_enabled_cases: int = RELEASE_MIN_ENABLED_CASES,
    minimum_expected_decisions: int = RELEASE_MIN_EXPECTED_DECISIONS,
    max_retrieval_drop: float = 0.0,
    max_completeness_drop_pp: float = 0.0,
) -> dict:
    """Construye un gate auditable; solo ``release_mode`` bloquea publicación."""
    case_rows = list(cases)
    result_rows = list(results)
    profile = evaluation_bank_profile(case_rows)
    checks: list[dict[str, object]] = []

    def check(key: str, label: str, passed: bool, detail: str) -> None:
        checks.append(
            {
                "key": key,
                "label": label,
                "passed": bool(passed),
                "blocking": bool(release_mode and not passed),
                "detail": detail,
            }
        )

    check(
        "valid_bank",
        "Banco sin errores",
        not bank_errors and not profile["placeholder_refs"],
        "Sin errores de formato."
        if not bank_errors and not profile["placeholder_refs"]
        else (
            f"{len(bank_errors)} error(es) y "
            f"{len(profile['placeholder_refs'])} referencia(s) de plantilla."
        ),
    )
    check(
        "minimum_cases",
        "Mínimo de consultas verificadas",
        profile["cases_enabled"] >= minimum_enabled_cases,
        f"{profile['cases_enabled']} habilitadas; mínimo {minimum_enabled_cases}.",
    )
    check(
        "minimum_decisions",
        "Mínimo de decisiones esperadas",
        profile["unique_expected_refs"] >= minimum_expected_decisions,
        f"{profile['unique_expected_refs']} referencias únicas; mínimo "
        f"{minimum_expected_decisions}.",
    )
    check(
        "semaglutida_case",
        "Caso verificable de semaglutida",
        bool(profile["has_semaglutida_case"]),
        "Caso(s): " + ", ".join(profile["semaglutida_case_ids"])
        if profile["has_semaglutida_case"]
        else "No hay un caso habilitado; la plantilla incluye uno por completar.",
    )

    enabled_count = int(profile["cases_enabled"])
    expected_runs = enabled_count * len(SEARCH_MODES)
    check(
        "evaluation_completed",
        "Evaluación completa de los tres modos",
        len(result_rows) == expected_runs and expected_runs > 0,
        f"{len(result_rows)} ejecuciones de {expected_runs} esperadas.",
    )
    result_errors = sum(bool(result.error) for result in result_rows)
    check(
        "evaluation_errors",
        "Consultas ejecutadas sin error",
        result_errors == 0 and bool(result_rows),
        f"{result_errors} ejecución(es) con error.",
    )
    fallbacks = sum(
        result.used_mode != result.requested_mode and not result.error
        for result in result_rows
    )
    check(
        "search_modes_available",
        "Modos evaluados sin fallback",
        fallbacks == 0 and bool(result_rows),
        f"{fallbacks} ejecución(es) usaron otro modo.",
    )
    semaglutida_ids = set(profile["semaglutida_case_ids"])
    semaglutida_hybrid = [
        result
        for result in result_rows
        if result.case_id in semaglutida_ids and result.requested_mode == "hybrid"
    ]
    check(
        "semaglutida_recovered",
        "Semaglutida recuperada en modo híbrido",
        bool(semaglutida_hybrid)
        and all(result.hit_at_k == 1.0 and not result.error for result in semaglutida_hybrid),
        "La fuente esperada apareció en Hit@K."
        if semaglutida_hybrid
        and all(result.hit_at_k == 1.0 and not result.error for result in semaglutida_hybrid)
        else "El caso falta o no recuperó su fuente esperada.",
    )

    integrity_ok = bool(integrity_report) and str(
        (integrity_report or {}).get("status")
    ) in {"ok", "warning"}
    check(
        "integrity",
        "Integridad técnica del candidato",
        integrity_ok,
        "Informe disponible sin errores."
        if integrity_ok
        else "Falta el informe o reporta errores.",
    )

    quality = _quality_payload(extractor_quality)
    quality_available = bool(quality and quality.get("status") == "available")
    check(
        "extractor_metrics",
        "Métricas del extractor disponibles",
        quality_available and _safe_int((quality or {}).get("records")) > 0,
        f"{_safe_int((quality or {}).get('records'))} fichas medidas.",
    )

    extractor_comparable = bool(
        extractor_comparison
        and extractor_comparison.get("status") == "comparable"
    )
    check(
        "extractor_baseline",
        "Línea base del extractor comparable",
        extractor_comparable,
        "Comparación antes/después disponible."
        if extractor_comparable
        else "No hay línea base comparable.",
    )
    field_rows = {
        str(row.get("key")): row
        for row in (extractor_comparison or {}).get("fields") or []
        if isinstance(row, Mapping)
    }
    for field in RELEASE_COMPLETENESS_FIELDS:
        row = field_rows.get(field)
        delta = row.get("delta_percentage_points") if row else None
        passed = bool(
            row
            and row.get("comparable")
            and delta is not None
            and float(delta) >= -abs(max_completeness_drop_pp)
        )
        label = str((row or {}).get("label") or field.replace("_", " "))
        detail = (
            f"Cambio: {float(delta):+.2f} puntos porcentuales; caída máxima "
            f"permitida: {abs(max_completeness_drop_pp):.2f}."
            if delta is not None
            else "Campo no comparable con la línea base."
        )
        check(f"completeness_{field}", f"Completitud: {label}", passed, detail)

    retrieval_comparable = bool(
        retrieval_comparison
        and retrieval_comparison.get("status") == "comparable"
    )
    check(
        "retrieval_baseline",
        "Línea base de recuperación comparable",
        retrieval_comparable,
        "Comparación antes/después disponible."
        if retrieval_comparable
        else "No hay línea base comparable para los tres modos.",
    )
    retrieval_rows = (retrieval_comparison or {}).get("rows") or []
    regressions = [
        (str(row.get("mode")), metric, float(row.get(f"delta_{metric}") or 0.0))
        for row in retrieval_rows
        if row.get("comparable")
        for metric in ("hit_at_k", "recall_at_k", "mrr")
        if float(row.get(f"delta_{metric}") or 0.0) < -abs(max_retrieval_drop)
    ]
    check(
        "retrieval_regression",
        "Sin regresiones de recuperación",
        retrieval_comparable and not regressions,
        "Sin caídas sobre el umbral."
        if retrieval_comparable and not regressions
        else (
            f"{len(regressions)} caída(s) superan {abs(max_retrieval_drop):.3f}."
            if retrieval_comparable
            else "No fue posible comparar recuperación."
        ),
    )

    failed = [item for item in checks if not item["passed"]]
    return {
        "mode": "release" if release_mode else "advisory",
        "status": "pass" if not failed else ("fail" if release_mode else "advisory"),
        "can_publish": not failed if release_mode else True,
        "checks_passed": len(checks) - len(failed),
        "checks_total": len(checks),
        "blocking_failures": sum(bool(item["blocking"]) for item in checks),
        "thresholds": {
            "minimum_enabled_cases": minimum_enabled_cases,
            "minimum_expected_decisions": minimum_expected_decisions,
            "max_retrieval_drop": abs(max_retrieval_drop),
            "max_completeness_drop_pp": abs(max_completeness_drop_pp),
        },
        "checks": checks,
    }


def _safe_csv_value(value: object) -> object:
    if not isinstance(value, str):
        return value
    stripped = value.lstrip()
    if stripped.startswith(_DANGEROUS_CSV_PREFIXES):
        return "'" + value
    return value


def evaluation_results_to_csv(results: Iterable[EvaluationResult]) -> str:
    rows = [result.as_export_row() for result in results]
    if not rows:
        return ""
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(rows[0]))
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _safe_csv_value(value) for key, value in row.items()})
    return output.getvalue()


def _load_json_report(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def load_evaluation_report(path: Path) -> dict | None:
    """Carga de forma segura un reporte previo para la comparación de release."""
    return _load_json_report(path)


def _parse_report_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_hours(value: object, current_time: datetime) -> float | None:
    generated = _parse_report_time(value)
    if generated is None:
        return None
    return max(0.0, (current_time - generated).total_seconds() / 3600)


def _coverage_percent(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round((numerator / denominator) * 100, 2)


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def _layer_status(percent: float | None, *, verified: bool = True) -> str:
    if not verified or percent is None:
        return "unverified"
    return "ok" if percent >= 100.0 else "incomplete"


def _valid_source_snapshot(snapshot: Mapping | None) -> bool:
    if not snapshot or snapshot.get("status") != "valid":
        return False
    html_hash = str(snapshot.get("html_sha256") or "")
    return bool(
        _parse_report_time(snapshot.get("generated_at"))
        and _safe_int(snapshot.get("discovered_records")) > 0
        and _safe_int(snapshot.get("html_bytes")) > 0
        and re.fullmatch(r"[0-9a-fA-F]{64}", html_hash)
        and str(snapshot.get("parser_version") or "").strip()
    )


def audit_corpus_reports(
    integrity_report_path: Path,
    indexing_report_path: Path,
    semantic_report_path: Path,
    *,
    catalog_report_path: Path | None = None,
    source_snapshot_path: Path | None = None,
    now: datetime | None = None,
) -> dict:
    """Resume cobertura, faltantes, OCR y antigüedad sin abrir el índice."""
    integrity = _load_json_report(integrity_report_path)
    indexing = _load_json_report(indexing_report_path)
    semantic = _load_json_report(semantic_report_path)
    catalog = _load_json_report(catalog_report_path) if catalog_report_path else None
    source_snapshot = (
        _load_json_report(source_snapshot_path) if source_snapshot_path else None
    )
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    source_snapshot_valid = _valid_source_snapshot(source_snapshot)
    source_checked = bool(catalog and catalog.get("source_checked"))
    catalog_records = _safe_int((catalog or {}).get("catalog_records"))
    currently_listed = _safe_int((catalog or {}).get("currently_listed"))
    manifest_from_catalog = _safe_int(
        (catalog or {}).get("manifest_documents")
    )
    source_discovered = _safe_int(
        (source_snapshot or {}).get(
            "discovered_records",
            (catalog or {}).get("discovered_records", 0),
        )
    )
    source_layer_verified = source_checked and source_snapshot_valid
    source_catalog_percent = (
        _coverage_percent(currently_listed, source_discovered)
        if source_layer_verified
        else None
    )
    catalog_manifest_percent = _coverage_percent(
        manifest_from_catalog,
        catalog_records,
    )
    if not integrity:
        coverage_layers = [
            {
                "key": "source_catalog",
                "label": "Fuente → catálogo",
                "numerator": currently_listed,
                "denominator": source_discovered,
                "coverage_percent": source_catalog_percent,
                "status": _layer_status(
                    source_catalog_percent,
                    verified=source_layer_verified,
                ),
            },
            {
                "key": "catalog_manifest",
                "label": "Catálogo → manifiesto",
                "numerator": manifest_from_catalog,
                "denominator": catalog_records,
                "coverage_percent": catalog_manifest_percent,
                "status": _layer_status(catalog_manifest_percent),
            },
            {
                "key": "manifest_index",
                "label": "Manifiesto → índice",
                "numerator": 0,
                "denominator": 0,
                "coverage_percent": None,
                "status": "unverified",
            },
            {
                "key": "pages",
                "label": "Páginas consultables",
                "numerator": 0,
                "denominator": 0,
                "coverage_percent": None,
                "status": "unverified",
            },
        ]
        return {
            "status": "unavailable",
            "message": "No existe un informe de integridad válido.",
            "integrity_available": False,
            "indexing_available": bool(indexing),
            "semantic_available": bool(semantic),
            "generated_at": None,
            "age_hours": None,
            "freshness": "unknown",
            "manifest_documents": 0,
            "indexed_documents": 0,
            "document_coverage_percent": 0.0,
            "text_page_coverage_percent": 0.0,
            "missing_documents": [],
            "ocr_candidate_documents": 0,
            "ocr_candidate_pages": 0,
            "incomplete_years": [],
            "issues": [
                {
                    "severity": "error",
                    "category": "report",
                    "detail": "Ejecuta el workflow Construir índice.",
                    "count": 1,
                }
            ],
            "source_checked": source_checked,
            "source_snapshot_available": bool(source_snapshot),
            "source_snapshot_valid": source_snapshot_valid,
            "source_age_hours": _age_hours(
                (source_snapshot or {}).get("generated_at"), current_time
            ),
            "source_discovered_records": source_discovered,
            "currently_listed_records": currently_listed,
            "source_catalog_percent": source_catalog_percent,
            "catalog_records": catalog_records,
            "catalog_manifest_documents": manifest_from_catalog,
            "catalog_manifest_percent": catalog_manifest_percent,
            "coverage_layers": coverage_layers,
        }

    expected = _safe_int(integrity.get("manifest_documents"))
    indexed = _safe_int(integrity.get("indexed_documents"))
    missing_documents = list(integrity.get("missing_documents") or [])
    ocr_candidates = list(integrity.get("ocr_candidates") or [])
    ocr_pages = sum(
        len(item.get("pages") or [])
        for item in ocr_candidates
        if isinstance(item, Mapping)
    )
    coverage_rows = list(integrity.get("coverage_by_year") or [])
    incomplete_years = [
        {
            "year": item.get("year"),
            "expected": _safe_int(item.get("manifest_documents")),
            "indexed": _safe_int(item.get("indexed_documents")),
            "missing": _safe_int(item.get("missing_documents")),
            "coverage_percent": _safe_float(item.get("coverage_percent")),
        }
        for item in coverage_rows
        if isinstance(item, Mapping)
        and _safe_float(item.get("coverage_percent")) < 100.0
    ]
    generated = _parse_report_time(integrity.get("generated_at"))
    age_hours = _age_hours(integrity.get("generated_at"), current_time)
    if age_hours is None:
        freshness = "unknown"
    elif age_hours <= 24 * 7:
        freshness = "fresh"
    elif age_hours <= 24 * 30:
        freshness = "aging"
    else:
        freshness = "stale"

    issues: list[dict[str, object]] = []

    def add_issue(severity: str, category: str, detail: str, count: int) -> None:
        if count:
            issues.append(
                {
                    "severity": severity,
                    "category": category,
                    "detail": detail,
                    "count": count,
                }
            )

    add_issue(
        "error",
        "coverage",
        "Documentos del manifiesto que no están indexados",
        len(missing_documents),
    )
    add_issue(
        "error",
        "documents",
        "Documentos sin páginas",
        len(integrity.get("documents_without_pages") or []),
    )
    add_issue(
        "error",
        "documents",
        "Documentos sin fragmentos",
        len(integrity.get("documents_without_chunks") or []),
    )
    add_issue(
        "error",
        "documents",
        "Documentos inesperados en el índice",
        len(integrity.get("unexpected_documents") or []),
    )
    add_issue(
        "warning",
        "ocr",
        "Páginas candidatas para OCR",
        ocr_pages,
    )
    add_issue(
        "warning",
        "inventory",
        "Documentos con inventario de páginas pendiente",
        len(integrity.get("page_inventory_pending") or []),
    )
    add_issue(
        "warning",
        "regulatory",
        "Documentos pendientes de extracción regulatoria",
        len(integrity.get("regulatory_extraction_pending") or []),
    )
    add_issue(
        "error",
        "regulatory",
        "Errores de extracción regulatoria",
        len(integrity.get("regulatory_extraction_errors") or []),
    )
    if catalog_report_path is not None and not source_checked:
        add_issue(
            "error",
            "source",
            "El catálogo no acredita una revisión válida de la página oficial",
            1,
        )
    if source_snapshot_path is not None and not source_snapshot:
        add_issue(
            "error",
            "source",
            "No existe snapshot verificable de la última consulta oficial",
            1,
        )
    elif source_snapshot_path is not None and not source_snapshot_valid:
        add_issue(
            "error",
            "source",
            "El snapshot oficial existe, pero no contiene evidencia válida",
            1,
        )
    catalog_discovered = _safe_int((catalog or {}).get("discovered_records"))
    if source_layer_verified and catalog_discovered != source_discovered:
        add_issue(
            "error",
            "source",
            "El snapshot y el reporte del catálogo no coinciden en publicaciones",
            abs(catalog_discovered - source_discovered) or 1,
        )
    if source_layer_verified and currently_listed != source_discovered:
        add_issue(
            "error",
            "coverage",
            "Las publicaciones detectadas no coinciden con las vigentes del catálogo",
            abs(currently_listed - source_discovered) or 1,
        )
    records_without_url = _safe_int((catalog or {}).get("records_without_url"))
    shared_urls = len((catalog or {}).get("shared_url_conflicts") or [])
    add_issue(
        "warning",
        "catalog",
        "Publicaciones registradas sin enlace descargable",
        records_without_url,
    )
    add_issue(
        "warning",
        "catalog",
        "Enlaces compartidos por varias entradas del catálogo",
        shared_urls,
    )
    if catalog_report_path is not None and catalog_records <= 0:
        add_issue(
            "error",
            "coverage",
            "El reporte no contiene un catálogo verificable",
            1,
        )
    elif (
        catalog_manifest_percent is not None
        and catalog_manifest_percent < 100.0
    ):
        add_issue(
            "warning",
            "coverage",
            "Entradas del catálogo que no llegaron al manifiesto",
            max(catalog_records - manifest_from_catalog, 1),
        )
    if expected <= 0:
        add_issue(
            "error",
            "coverage",
            "El informe no contiene documentos en el manifiesto",
            1,
        )
    source_age_hours = _age_hours(
        (source_snapshot or {}).get("generated_at"), current_time
    )
    if source_layer_verified and source_age_hours is not None:
        if source_age_hours > 24 * 30:
            add_issue(
                "error",
                "source_freshness",
                "La página oficial no se valida desde hace más de 30 días",
                1,
            )
        elif source_age_hours > 24 * 7:
            add_issue(
                "warning",
                "source_freshness",
                "La página oficial no se valida desde hace más de 7 días",
                1,
            )
    if indexing:
        add_issue(
            "warning",
            "indexing",
            "Documentos fallidos en la última indexación",
            _safe_int(indexing.get("documents_failed")),
        )
    if freshness == "aging":
        add_issue(
            "warning",
            "freshness",
            "El informe tiene más de 7 días",
            1,
        )
    elif freshness == "stale":
        add_issue(
            "error",
            "freshness",
            "El informe tiene más de 30 días",
            1,
        )
    elif freshness == "unknown":
        add_issue(
            "warning",
            "freshness",
            "El informe no contiene una fecha válida",
            1,
        )
    if str(integrity.get("sqlite_integrity", "")).lower() != "ok":
        add_issue(
            "error",
            "database",
            "La verificación de integridad SQLite no es correcta",
            1,
        )
    semantic_status = str((semantic or {}).get("status", "missing"))
    if semantic_status in {"error", "missing"}:
        add_issue(
            "warning",
            "semantic",
            "El reporte semántico no confirma un índice disponible",
            1,
        )

    manifest_index_percent = _coverage_percent(indexed, expected)
    indexed_pages = _safe_int(integrity.get("pages_indexed"))
    pdf_pages = _safe_int(integrity.get("pdf_pages"))
    page_percent = _safe_float(integrity.get("text_page_coverage_percent"))
    if pdf_pages > 0 and page_percent < 100.0 and not ocr_pages:
        add_issue(
            "warning",
            "pages",
            "Páginas del PDF que no están disponibles como texto consultable",
            max(pdf_pages - indexed_pages, 1),
        )
    error_count = sum(item["severity"] == "error" for item in issues)
    warning_count = sum(item["severity"] == "warning" for item in issues)
    status = "error" if error_count else ("warning" if warning_count else "ok")
    coverage_layers = [
        {
            "key": "source_catalog",
            "label": "Fuente → catálogo",
            "numerator": currently_listed,
            "denominator": source_discovered,
            "coverage_percent": source_catalog_percent,
            "status": _layer_status(
                source_catalog_percent,
                verified=source_layer_verified,
            ),
        },
        {
            "key": "catalog_manifest",
            "label": "Catálogo → manifiesto",
            "numerator": manifest_from_catalog,
            "denominator": catalog_records,
            "coverage_percent": catalog_manifest_percent,
            "status": _layer_status(catalog_manifest_percent),
        },
        {
            "key": "manifest_index",
            "label": "Manifiesto → índice",
            "numerator": indexed,
            "denominator": expected,
            "coverage_percent": manifest_index_percent,
            "status": _layer_status(manifest_index_percent),
        },
        {
            "key": "pages",
            "label": "Páginas consultables",
            "numerator": indexed_pages,
            "denominator": pdf_pages,
            "coverage_percent": page_percent if pdf_pages else None,
            "status": _layer_status(page_percent if pdf_pages else None),
        },
    ]
    return {
        "status": status,
        "message": "Auditoría construida a partir de los reportes del índice.",
        "integrity_available": True,
        "indexing_available": bool(indexing),
        "semantic_available": bool(semantic),
        "generated_at": generated.isoformat() if generated else None,
        "age_hours": round(age_hours, 2) if age_hours is not None else None,
        "freshness": freshness,
        "manifest_documents": expected,
        "indexed_documents": indexed,
        "document_coverage_percent": manifest_index_percent or 0.0,
        "text_page_coverage_percent": page_percent,
        "missing_documents": missing_documents,
        "ocr_candidate_documents": len(ocr_candidates),
        "ocr_candidate_pages": ocr_pages,
        "incomplete_years": incomplete_years,
        "coverage_by_year": coverage_rows,
        "semantic_status": semantic_status,
        "issues": issues,
        "source_checked": source_checked,
        "source_snapshot_available": bool(source_snapshot),
        "source_snapshot_valid": source_snapshot_valid,
        "source_snapshot_generated_at": (source_snapshot or {}).get("generated_at"),
        "source_snapshot_parser_version": (source_snapshot or {}).get(
            "parser_version"
        ),
        "source_snapshot_html_sha256": (source_snapshot or {}).get("html_sha256"),
        "source_age_hours": (
            round(source_age_hours, 2) if source_age_hours is not None else None
        ),
        "source_discovered_records": source_discovered,
        "currently_listed_records": currently_listed,
        "source_catalog_percent": source_catalog_percent,
        "catalog_records": catalog_records,
        "catalog_manifest_documents": manifest_from_catalog,
        "catalog_manifest_percent": catalog_manifest_percent,
        "records_without_url": records_without_url,
        "shared_url_conflicts": shared_urls,
        "coverage_layers": coverage_layers,
    }
