"""Construcción segura de una base candidata para reprocesar el corpus.

La acción de reprocesamiento nunca trabaja directamente sobre los paquetes
publicados. Construye una candidata por lotes, conserva un punto de
continuación y solo permite copiarla a ``data/`` después de validar integridad,
trazabilidad, revisiones y una restauración real de los paquetes.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from config import (
    ALLOWED_DOCUMENT_HOSTS,
    MANIFEST_PATH,
    PDF_CACHE_DIR,
    REPROCESS_BATCH_SIZE,
    REPROCESS_MIN_FREE_MIB,
    REVIEW_LOG_PATH,
    ROOT_DIR,
)
from services.database import (
    indexed_document_catalog,
    reconcile_database_decision_uids,
)
from services.database_package import (
    materialize_database_package,
    package_manifest_path,
)
from services.indexing import rebuild_index, update_index
from services.integrity import (
    build_integrity_report,
    build_page_inventory_snapshot,
)
from services.search import search_corpus
from services.semantic import semantic_index_status, semantic_source_fingerprint
from services.reviews import latest_reviews, load_review_events
from services.text_utils import normalize_text


STATE_FORMAT_VERSION = 2
DEFAULT_WORKSPACE = ROOT_DIR / ".reprocess"
DATABASE_NAMES = ("actas.db", "semantic.db")
REPORT_NAMES = (
    "indexing-report.json",
    "integrity-report.json",
    "semantic-report.json",
    "reconciliation-report.json",
)
WORKFLOW_REPORT_EXPORTS = (
    ("run-state.json", "run-state.json"),
    ("candidate/checkpoint.json", "checkpoint.json"),
    ("reprocess-report.json", "reprocess-report.json"),
    ("candidate/data/indexing-report.json", "candidate-indexing-report.json"),
    ("candidate/data/integrity-report.json", "candidate-integrity-report.json"),
    ("candidate/data/semantic-report.json", "candidate-semantic-report.json"),
    (
        "candidate/data/semantic-progress.json",
        "candidate-semantic-progress.json",
    ),
    (
        "candidate/data/reconciliation-report.json",
        "candidate-reconciliation-report.json",
    ),
    ("baseline/data/integrity-report.json", "baseline-integrity-report.json"),
)
REQUIRED_COMPLETENESS_FIELDS = (
    "product",
    "active_ingredient",
    "numeral",
    "interested_party",
    "expediente",
    "radicado",
    "identifiers",
    "page_range",
    "concept",
    "outcome",
)
_FIELD_ALIASES = {
    "product": ("product", "producto"),
    "active_ingredient": (
        "active_ingredient",
        "active_ingredients",
        "principio_activo",
        "principios_activos",
    ),
    "numeral": ("numeral",),
    "interested_party": (
        "interested_party",
        "interesado",
        "titular",
        "solicitante",
    ),
    "expediente": ("expediente",),
    "radicado": ("radicado",),
    "identifiers": (
        "identifiers",
        "identificadores",
        "expediente_or_radicado",
        "expediente_o_radicado",
    ),
    "page_range": ("page_range", "pages", "rango_paginas", "paginas"),
    "concept": ("concept", "concepto", "concept_text"),
    "outcome": (
        "outcome",
        "outcomes",
        "resultado",
        "resultado_normalizado",
        "outcome_code",
    ),
}
_WORD_PATTERN = re.compile(r"\b[^\W\d_][\wáéíóúüñ-]{3,}\b", re.IGNORECASE)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _load_json(path: Path, *, required: bool = True) -> dict:
    if not path.exists():
        if required:
            raise ValueError(f"No existe el archivo requerido: {path}")
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"El archivo JSON no es válido: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"El archivo debe contener un objeto JSON: {path}")
    return payload


def _safe_workspace(path: Path) -> Path:
    resolved = path.resolve()
    allowed_root = (ROOT_DIR / ".reprocess").resolve()
    if resolved != allowed_root and allowed_root not in resolved.parents:
        raise ValueError(
            "El directorio de trabajo debe ser .reprocess o uno de sus "
            "subdirectorios"
        )
    return resolved


def _replace_directory(path: Path) -> None:
    safe = _safe_workspace(path)
    if safe.exists():
        shutil.rmtree(safe)
    safe.mkdir(parents=True)


def _file_snapshot(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"exists": False, "bytes": 0, "sha256": None}
    return {
        "exists": True,
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _database_set_snapshot(data_dir: Path) -> dict[str, dict[str, object]]:
    return {name: _file_snapshot(data_dir / name) for name in DATABASE_NAMES}


def _snapshot_fingerprint(snapshot: dict[str, dict[str, object]]) -> str:
    payload = json.dumps(
        snapshot,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _package_database_bytes(database_path: Path) -> int:
    manifest = package_manifest_path(database_path)
    if database_path.exists():
        return database_path.stat().st_size
    package = _load_json(manifest, required=False)
    try:
        return max(0, int(package.get("database_bytes", 0)))
    except (TypeError, ValueError):
        return 0


def _source_fingerprint(manifest_path: Path) -> str:
    digest = hashlib.sha256()
    tracked = (
        manifest_path,
        ROOT_DIR / "config.py",
        ROOT_DIR / "requirements.txt",
        Path(__file__).resolve(),
        ROOT_DIR / "services" / "catalog.py",
        ROOT_DIR / "services" / "database.py",
        ROOT_DIR / "services" / "downloader.py",
        ROOT_DIR / "services" / "ingredients.py",
        ROOT_DIR / "services" / "indexing.py",
        ROOT_DIR / "services" / "metadata.py",
        ROOT_DIR / "services" / "manifest.py",
        ROOT_DIR / "services" / "models.py",
        ROOT_DIR / "services" / "pdf_reader.py",
        ROOT_DIR / "services" / "regulatory.py",
        ROOT_DIR / "services" / "semantic.py",
        ROOT_DIR / "services" / "text_utils.py",
    )
    for path in tracked:
        if not path.exists():
            raise ValueError(f"Falta un archivo que define el reprocesamiento: {path}")
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    settings = (
        os.getenv("ACTAS_CHUNK_SIZE", ""),
        os.getenv("ACTAS_CHUNK_OVERLAP", ""),
        os.getenv("ACTAS_OCR_ENABLED", ""),
        os.getenv("ACTAS_OCR_LANGUAGES", ""),
        os.getenv("ACTAS_OCR_DPI", ""),
        os.getenv("ACTAS_OCR_MIN_CHARS", ""),
        os.getenv("ACTAS_OCR_TIMEOUT_SECONDS", ""),
        os.getenv("ACTAS_MAX_PDF_BYTES", ""),
        os.getenv("ACTAS_SEMANTIC_BACKEND", ""),
        os.getenv("ACTAS_SEMANTIC_LEXICAL_DIMENSION", ""),
        os.getenv("ACTAS_SEMANTIC_DISTRIBUTIONAL_DIMENSION", ""),
        os.getenv("ACTAS_SEMANTIC_NEURAL_MODEL_ID", ""),
        os.getenv("ACTAS_SEMANTIC_NEURAL_MODEL_REVISION", ""),
        os.getenv("ACTAS_SEMANTIC_NEURAL_BATCH_SIZE", ""),
    )
    digest.update("\x1f".join(settings).encode("utf-8"))
    digest.update(
        json.dumps(
            _runtime_versions(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    return digest.hexdigest()


def _runtime_versions() -> dict[str, str]:
    try:
        pymupdf_version = importlib.metadata.version("PyMuPDF")
    except importlib.metadata.PackageNotFoundError:
        pymupdf_version = "unavailable"
    try:
        process = subprocess.run(
            ["tesseract", "--version"],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
        tesseract_version = (
            process.stdout.splitlines()[0].strip()
            if process.returncode == 0 and process.stdout
            else "unavailable"
        )
    except (OSError, subprocess.TimeoutExpired):
        tesseract_version = "unavailable"
    return {
        "python": sys.version.split()[0],
        "sqlite": sqlite3.sqlite_version,
        "pymupdf": pymupdf_version,
        "tesseract": tesseract_version,
    }


def _read_manifest_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        rows = [dict(row) for row in reader]
    if not fields or "title" not in fields or "url" not in fields:
        raise ValueError("El manifiesto no contiene title y url")
    if not rows:
        raise ValueError("El manifiesto no contiene documentos")
    urls = [str(row.get("url") or "").strip() for row in rows]
    if any(not url for url in urls) or len(urls) != len(set(urls)):
        raise ValueError("El manifiesto contiene URL vacías o duplicadas")
    return fields, rows


def _write_manifest_prefix(
    path: Path,
    fields: list[str],
    rows: list[dict[str, str]],
    count: int,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows[:count])
    temporary.replace(path)


def _restore_published_database(source: Path, target: Path) -> bool:
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.exists():
        shutil.copy2(source, target)
        return True
    result = materialize_database_package(source, target)
    return result.exists()


def prepare_workspace(
    workspace: Path,
    *,
    published_data: Path,
    manifest_path: Path,
    review_log_path: Path,
    min_free_mib: int,
    resume_from: Path | None = None,
) -> dict:
    workspace = _safe_workspace(workspace)
    published_data = published_data.resolve()
    manifest_fields, manifest_rows = _read_manifest_rows(manifest_path)
    del manifest_fields
    fingerprint = _source_fingerprint(manifest_path)

    database_bytes = sum(
        _package_database_bytes(published_data / name) for name in DATABASE_NAMES
    )
    # Base publicada + candidata y su temporal + paquetes + restauración smoke.
    estimated_required = database_bytes * 5 + 1024 * 1024 * 1024
    configured_minimum = max(512, min_free_mib) * 1024 * 1024
    required_free = max(configured_minimum, estimated_required)
    free_bytes = shutil.disk_usage(workspace.parent).free
    if free_bytes < required_free:
        raise ValueError(
            "Espacio insuficiente: se requieren aproximadamente "
            f"{required_free / (1024 ** 3):.1f} GiB y hay "
            f"{free_bytes / (1024 ** 3):.1f} GiB libres"
        )

    _replace_directory(workspace)
    baseline_dir = workspace / "baseline"
    candidate_dir = workspace / "candidate"
    baseline_data = baseline_dir / "data"
    candidate_data = candidate_dir / "data"
    baseline_data.mkdir(parents=True)
    candidate_data.mkdir(parents=True)

    baseline_restored: dict[str, bool] = {}
    for name in DATABASE_NAMES:
        baseline_restored[name] = _restore_published_database(
            published_data / name,
            baseline_data / name,
        )
    if not baseline_restored["actas.db"]:
        raise ValueError("No fue posible restaurar la base publicada")
    baseline_databases = _database_set_snapshot(baseline_data)
    baseline_fingerprint = _snapshot_fingerprint(baseline_databases)

    resumed = False
    resumed_semantic = False
    if resume_from:
        resume_root = resume_from.resolve()
        resume_state_path = resume_root / "run-state.json"
        resume_checkpoint_path = resume_root / "candidate" / "checkpoint.json"
        resume_database = resume_root / "candidate" / "data" / "actas.db"
        resume_semantic = resume_root / "candidate" / "data" / "semantic.db"
        resume_semantic_checkpoint = (
            resume_root / "candidate" / "data" / "semantic.checkpoint.db"
        )
        resume_semantic_progress = (
            resume_root / "candidate" / "data" / "semantic-progress.json"
        )
        resume_state = _load_json(resume_state_path)
        resume_checkpoint = _load_json(resume_checkpoint_path)
        if (
            resume_state.get("source_fingerprint") != fingerprint
            or resume_checkpoint.get("source_fingerprint") != fingerprint
        ):
            raise ValueError(
                "El punto de continuación pertenece a otro catálogo o código"
            )
        resume_baseline = str(resume_state.get("baseline_fingerprint") or "")
        checkpoint_baseline = str(
            resume_checkpoint.get("baseline_fingerprint") or ""
        )
        if not resume_baseline or checkpoint_baseline != resume_baseline:
            raise ValueError(
                "El punto de continuación no identifica de forma verificable "
                "la base publicada de referencia"
            )
        if resume_baseline != baseline_fingerprint:
            raise ValueError(
                "La base publicada cambió desde el punto de continuación; "
                "inicia un reprocesamiento nuevo"
            )
        if int(resume_checkpoint.get("documents_completed", -1)) > len(manifest_rows):
            raise ValueError("El punto de continuación excede el manifiesto actual")
        if not resume_database.exists():
            raise ValueError("El punto de continuación no contiene actas.db")
        shutil.copy2(resume_database, candidate_data / "actas.db")
        if resume_semantic.exists():
            shutil.copy2(resume_semantic, candidate_data / "semantic.db")
            resumed_semantic = True
        if resume_semantic_checkpoint.exists():
            shutil.copy2(
                resume_semantic_checkpoint,
                candidate_data / "semantic.checkpoint.db",
            )
        if resume_semantic_progress.exists():
            shutil.copy2(
                resume_semantic_progress,
                candidate_data / "semantic-progress.json",
            )
        shutil.copy2(resume_checkpoint_path, candidate_dir / "checkpoint.json")
        resumed = True

    review_snapshot = _file_snapshot(review_log_path)
    state = {
        "format_version": STATE_FORMAT_VERSION,
        "created_at": _utc_now(),
        "source_fingerprint": fingerprint,
        "runtime_versions": _runtime_versions(),
        "manifest_sha256": _sha256(manifest_path),
        "manifest_documents": len(manifest_rows),
        "published_data": str(published_data),
        "review_log": review_snapshot,
        "preflight": {
            "free_bytes": free_bytes,
            "required_free_bytes": required_free,
            "published_database_bytes": database_bytes,
        },
        "baseline_restored": baseline_restored,
        "baseline_databases": baseline_databases,
        "baseline_fingerprint": baseline_fingerprint,
        "resumed": resumed,
        "resumed_semantic": resumed_semantic,
    }
    _atomic_json(workspace / "run-state.json", state)
    return state


def _progress(current: int, total: int, title: str) -> None:
    print(f"    [{current}/{total}] {title}", flush=True)


def build_candidate_batches(
    workspace: Path,
    *,
    manifest_path: Path,
    pdf_cache_dir: Path,
    batch_size: int,
    max_batches: int | None = None,
) -> dict:
    workspace = _safe_workspace(workspace)
    state = _load_json(workspace / "run-state.json")
    fingerprint = _source_fingerprint(manifest_path)
    if state.get("source_fingerprint") != fingerprint:
        raise ValueError("El catálogo o el extractor cambió después del preflight")
    fields, rows = _read_manifest_rows(manifest_path)
    if int(state.get("manifest_documents", -1)) != len(rows):
        raise ValueError("El manifiesto cambió después del preflight")

    candidate_dir = workspace / "candidate"
    database_path = candidate_dir / "data" / "actas.db"
    partial_manifest = candidate_dir / "documents_manifest.batch.csv"
    checkpoint_path = candidate_dir / "checkpoint.json"
    checkpoint = _load_json(checkpoint_path, required=False)
    completed = 0
    if database_path.exists():
        existing = indexed_document_catalog(database_path)
        completed = len(existing)
        expected_prefix = {
            str(row.get("url") or "").strip() for row in rows[:completed]
        }
        if set(existing) != expected_prefix:
            raise ValueError(
                "La candidata parcial no coincide con el prefijo del manifiesto"
            )
        if checkpoint and int(checkpoint.get("documents_completed", -1)) != completed:
            raise ValueError("La base y su punto de continuación no coinciden")
    elif checkpoint:
        raise ValueError("Existe un checkpoint sin base candidata")

    processed_batches = 0
    batch_size = max(1, batch_size)
    while completed < len(rows):
        if max_batches is not None and processed_batches >= max_batches:
            break
        target = min(completed + batch_size, len(rows))
        _write_manifest_prefix(partial_manifest, fields, rows, target)
        print(
            f"Lote {processed_batches + 1}: documentos {completed + 1}-{target}",
            flush=True,
        )
        if completed == 0:
            report = rebuild_index(
                partial_manifest,
                database_path,
                pdf_cache_dir,
                progress_callback=_progress,
                mode="historical_reprocess_batch",
                allow_partial=False,
            )
        else:
            report = update_index(
                partial_manifest,
                database_path,
                pdf_cache_dir,
                progress_callback=_progress,
                allow_partial=False,
            )
        if report.documents_failed or not database_path.exists():
            errors = " | ".join(report.errors or [])
            raise RuntimeError(
                f"Falló el lote que terminaba en {target}. {errors}".strip()
            )
        catalog = indexed_document_catalog(database_path)
        if len(catalog) != target:
            raise RuntimeError(
                f"El lote debía dejar {target} documentos y dejó {len(catalog)}"
            )
        completed = target
        processed_batches += 1
        checkpoint = {
            "format_version": STATE_FORMAT_VERSION,
            "updated_at": _utc_now(),
            "source_fingerprint": fingerprint,
            "baseline_fingerprint": state.get("baseline_fingerprint"),
            "manifest_documents": len(rows),
            "documents_completed": completed,
            "batch_size": batch_size,
            "complete": completed == len(rows),
            "last_report": report.as_dict(),
        }
        _atomic_json(checkpoint_path, checkpoint)

    if not checkpoint:
        checkpoint = {
            "format_version": STATE_FORMAT_VERSION,
            "updated_at": _utc_now(),
            "source_fingerprint": fingerprint,
            "baseline_fingerprint": state.get("baseline_fingerprint"),
            "manifest_documents": len(rows),
            "documents_completed": completed,
            "batch_size": batch_size,
            "complete": completed == len(rows),
        }
        _atomic_json(checkpoint_path, checkpoint)
    print(json.dumps(checkpoint, ensure_ascii=False, indent=2))
    return checkpoint


def write_baseline_integrity(
    workspace: Path,
    *,
    manifest_path: Path,
) -> dict:
    workspace = _safe_workspace(workspace)
    report = build_integrity_report(
        workspace / "baseline" / "data" / "actas.db",
        manifest_path,
        ALLOWED_DOCUMENT_HOSTS,
    )
    _atomic_json(workspace / "baseline" / "data" / "integrity-report.json", report)
    return report


def reconcile_candidate_identities(
    workspace: Path,
    *,
    manifest_path: Path,
    review_log_path: Path,
) -> dict:
    """Hereda UID inequívocos antes de medir o empaquetar la candidata."""
    workspace = _safe_workspace(workspace)
    state = _load_json(workspace / "run-state.json")
    if _file_snapshot(review_log_path) != state.get("review_log"):
        raise ValueError("El registro de revisiones cambió durante el proceso")
    baseline = workspace / "baseline" / "data" / "actas.db"
    candidate = workspace / "candidate" / "data" / "actas.db"
    # Solo una revisión humana vigente vuelve obligatorio conservar un UID
    # histórico ante una correspondencia dudosa. Las demás dudas se resuelven
    # de forma segura creando una identidad nueva, nunca heredando al azar.
    protected_uids = set(
        latest_reviews(load_review_events(review_log_path)).keys()
    )
    reconciliation = reconcile_database_decision_uids(
        baseline,
        candidate,
        protected_decision_uids=protected_uids,
    )
    review_reconciliation = _review_reconciliation(candidate, review_log_path)
    report = {
        "format_version": STATE_FORMAT_VERSION,
        "generated_at": _utc_now(),
        **reconciliation,
        "review_reconciliation": review_reconciliation,
    }
    target = workspace / "candidate" / "data" / "reconciliation-report.json"
    _atomic_json(target, report)

    # El cambio de UID no afecta fragmentos ni vectores, pero se regenera el
    # informe para que la evidencia publicada corresponda al estado final.
    integrity = build_integrity_report(
        candidate,
        manifest_path,
        ALLOWED_DOCUMENT_HOSTS,
    )
    _atomic_json(
        workspace / "candidate" / "data" / "integrity-report.json",
        integrity,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def _find_nested_mapping(payloads: Iterable[dict], key: str) -> dict:
    for payload in payloads:
        direct = payload.get(key)
        if isinstance(direct, dict):
            return direct
        for container_name in ("extractor_quality", "quality", "metrics"):
            container = payload.get(container_name)
            if isinstance(container, dict):
                nested = container.get(key)
                if isinstance(nested, dict):
                    return nested
                if key == "field_completeness" and isinstance(
                    container.get("fields"), dict
                ):
                    return container["fields"]
    return {}


def _field_metric(fields: dict, canonical: str) -> tuple[float | None, int | None]:
    value = None
    for alias in _FIELD_ALIASES[canonical]:
        if alias in fields:
            value = fields[alias]
            break
    if isinstance(value, (int, float)):
        return float(value), None
    if not isinstance(value, dict):
        return None, None
    populated = value.get("populated", value.get("present", value.get("completed")))
    total = value.get("total")
    percent = value.get(
        "coverage_percent",
        value.get("completeness_percent", value.get("percent")),
    )
    try:
        count = int(populated) if populated is not None else None
    except (TypeError, ValueError):
        count = None
    try:
        if percent is not None:
            return float(percent), count
        if populated is not None and total:
            return (float(populated) / float(total)) * 100.0, count
    except (TypeError, ValueError, ZeroDivisionError):
        return None, count
    return None, count


def _database_missing_decision_uids(database_path: Path) -> int:
    if not database_path.exists():
        return -1
    try:
        with sqlite3.connect(database_path) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'regulatory_records'"
            ).fetchone()
            if not exists:
                return 0
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM regulatory_records "
                    "WHERE decision_uid IS NULL OR TRIM(decision_uid) = ''"
                ).fetchone()[0]
            )
    except sqlite3.Error:
        return -1


def _review_rows(path: Path) -> int:
    if not path.exists() or not path.read_text(encoding="utf-8-sig").strip():
        return 0
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return sum(1 for _ in csv.DictReader(handle))


def _database_field_completeness(database_path: Path) -> dict[str, dict[str, object]]:
    """Calcula el contrato mínimo incluso si un reporte antiguo no lo incluye."""
    queries = {
        "product": "product_name IS NOT NULL AND TRIM(product_name) != ''",
        "active_ingredient": (
            "active_ingredient IS NOT NULL AND TRIM(active_ingredient) != ''"
        ),
        "numeral": "numeral IS NOT NULL AND TRIM(numeral) != ''",
        "interested_party": (
            "interested_party IS NOT NULL AND TRIM(interested_party) != ''"
        ),
        "expediente": "expediente IS NOT NULL AND TRIM(expediente) != ''",
        "radicado": "radicado IS NOT NULL AND TRIM(radicado) != ''",
        "identifiers": (
            "(expediente IS NOT NULL AND TRIM(expediente) != '') OR "
            "(radicado IS NOT NULL AND TRIM(radicado) != '')"
        ),
        "page_range": (
            "page_number IS NOT NULL AND end_page_number IS NOT NULL "
            "AND page_number > 0 AND end_page_number >= page_number"
        ),
        "concept": "concept_text IS NOT NULL AND TRIM(concept_text) != ''",
        "outcome": (
            "outcome_code IS NOT NULL AND "
            "LOWER(TRIM(outcome_code)) NOT IN "
            "('', 'sin_clasificar', 'unclassified', 'unknown', 'none')"
        ),
    }
    with sqlite3.connect(database_path) as connection:
        total = int(connection.execute("SELECT COUNT(*) FROM regulatory_records").fetchone()[0])
        result: dict[str, dict[str, object]] = {}
        for field, predicate in queries.items():
            populated = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM regulatory_records WHERE {predicate}"
                ).fetchone()[0]
            )
            result[field] = {
                "total": total,
                "populated": populated,
                "coverage_percent": round(
                    (populated / total) * 100.0 if total else 0.0,
                    4,
                ),
            }
    return result


def _compare_required_field_completeness(
    candidate_fields: dict,
    baseline_fields: dict,
) -> tuple[dict[str, dict[str, object]], list[dict[str, object]]]:
    """Compara campos derivados para informar cambios, sin calificar las actas."""

    summary: dict[str, dict[str, object]] = {}
    issues: list[dict[str, object]] = []
    for field in REQUIRED_COMPLETENESS_FIELDS:
        candidate_percent, candidate_count = _field_metric(candidate_fields, field)
        baseline_percent, baseline_count = _field_metric(baseline_fields, field)
        details = {
            "baseline_percent": baseline_percent,
            "candidate_percent": candidate_percent,
            "baseline_populated": baseline_count,
            "candidate_populated": candidate_count,
        }
        summary[field] = details
        if candidate_percent is None:
            issues.append(
                {
                    "code": "field_metric_missing",
                    "message": f"Falta la métrica de completitud para {field}",
                }
            )
        elif (
            baseline_percent is not None
            and candidate_percent + 0.01 < baseline_percent
        ):
            issues.append(
                {
                    "code": "field_regression",
                    "message": f"La completitud de {field} disminuyó",
                    "details": details,
                }
            )
    return summary, issues


def _source_text_inventory(database_path: Path) -> dict[str, object]:
    snapshot = build_page_inventory_snapshot(database_path)
    pages = int(snapshot.get("pages_recorded", 0) or 0)
    with_source = int(snapshot.get("pages_with_source_text", 0) or 0)
    # Las claves históricas se conservan para que informes anteriores sigan
    # siendo legibles; el gate nuevo usa ``inventory_complete`` y distingue un
    # fallo de extracción registrado de una página realmente no contabilizada.
    return {
        **snapshot,
        "pages": pages,
        "pages_missing_source_text": pages - with_source,
    }


def _page_inventory_gate_passes(snapshot: dict[str, object]) -> bool:
    return bool(
        snapshot.get("available")
        and snapshot.get("inventory_complete")
        and int(snapshot.get("pages_unaccounted", 0) or 0) == 0
    )


def _review_reconciliation(database_path: Path, review_log_path: Path) -> dict[str, int]:
    """Cruza solo revisiones vigentes, no contadores internos del extractor."""
    reviews = latest_reviews(load_review_events(review_log_path))
    result = {
        "reviews_total": len(reviews),
        "reconciled": 0,
        "orphaned": 0,
        "ambiguous": 0,
        "needs_reconfirmation": 0,
    }
    if not reviews:
        return result
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT r.decision_uid, r.record_key, d.document_hash
            FROM regulatory_records r
            JOIN documents d ON d.id = r.document_id
            """
        ).fetchall()
    by_uid: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        by_uid.setdefault(str(row["decision_uid"] or ""), []).append(row)
    for uid, event in reviews.items():
        matches = by_uid.get(uid, [])
        if not matches:
            result["orphaned"] += 1
            continue
        same_document = [
            row
            for row in matches
            if str(row["document_hash"] or "") == event.source_document_hash
        ]
        if len(same_document) == 1:
            result["reconciled"] += 1
            if str(same_document[0]["record_key"] or "") != event.source_record_key:
                result["needs_reconfirmation"] += 1
        else:
            # UID duplicado o PDF modificado harían que la corrección quedara
            # obsoleta; nunca se aprueban silenciosamente.
            result["ambiguous"] += 1
    return result


def _choose_smoke_query(database_path: Path) -> str:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT text FROM chunks WHERE TRIM(text) != '' ORDER BY id LIMIT 1"
        ).fetchone()
    if not row:
        raise ValueError("La base restaurada no contiene texto para el smoke test")
    normalized = normalize_text(str(row[0]))
    ignored = {"acta", "pagina", "sala", "comision", "revisora", "invima"}
    for token in _WORD_PATTERN.findall(normalized):
        if token not in ignored:
            return token
    raise ValueError("No fue posible obtener una consulta para el smoke test")


def _validate_restored_packages(
    workspace: Path,
    *,
    manifest_path: Path,
) -> dict:
    candidate_data = workspace / "candidate" / "data"
    smoke_data = workspace / "smoke" / "data"
    if smoke_data.parent.exists():
        shutil.rmtree(smoke_data.parent)
    smoke_data.mkdir(parents=True)

    restored_actas = materialize_database_package(
        candidate_data / "actas.db",
        smoke_data / "actas.db",
    )
    if not restored_actas.exists():
        raise ValueError("No se pudo restaurar el paquete candidato de actas")
    restored_semantic = materialize_database_package(
        candidate_data / "semantic.db",
        smoke_data / "semantic.db",
    )
    integrity = build_integrity_report(
        restored_actas,
        manifest_path,
        ALLOWED_DOCUMENT_HOSTS,
    )
    query = _choose_smoke_query(restored_actas)
    response = search_corpus(
        restored_actas,
        restored_semantic,
        query,
        mode="textual",
        top_k=1,
    )
    if not response.results:
        raise ValueError("La búsqueda textual del paquete restaurado no respondió")

    semantic = semantic_index_status(restored_semantic)
    semantic_current = bool(
        restored_semantic.exists()
        and semantic.get("available")
        and semantic.get("source_fingerprint")
        == semantic_source_fingerprint(restored_actas)
    )
    return {
        "integrity": integrity,
        "query": query,
        "search_results": len(response.results),
        "semantic": semantic,
        "semantic_current": semantic_current,
    }


def validate_candidate(
    workspace: Path,
    *,
    manifest_path: Path,
    review_log_path: Path,
    mode: str,
) -> dict:
    workspace = _safe_workspace(workspace)
    if mode not in {"diagnostic", "publish"}:
        raise ValueError("mode debe ser diagnostic o publish")
    state = _load_json(workspace / "run-state.json")
    checkpoint = _load_json(workspace / "candidate" / "checkpoint.json")
    candidate_data = workspace / "candidate" / "data"
    baseline_data = workspace / "baseline" / "data"
    candidate_integrity = _load_json(candidate_data / "integrity-report.json")
    baseline_integrity = _load_json(baseline_data / "integrity-report.json")
    candidate_indexing = _load_json(candidate_data / "indexing-report.json")
    candidate_semantic = _load_json(candidate_data / "semantic-report.json")
    identity_reconciliation = _load_json(
        candidate_data / "reconciliation-report.json"
    )
    issues: list[dict[str, object]] = []

    def reject(code: str, message: str, details: object | None = None) -> None:
        item: dict[str, object] = {"code": code, "message": message}
        if details is not None:
            item["details"] = details
        issues.append(item)

    manifest_count = len(_read_manifest_rows(manifest_path)[1])
    if not checkpoint.get("complete") or int(
        checkpoint.get("documents_completed", -1)
    ) != manifest_count:
        reject("incomplete_checkpoint", "El reprocesamiento por lotes no terminó")

    fatal_integrity = any(
        (
            str(candidate_integrity.get("sqlite_integrity", "")).lower() != "ok",
            candidate_integrity.get("schema_version")
            != candidate_integrity.get("expected_schema_version"),
            bool(candidate_integrity.get("missing_documents")),
            bool(candidate_integrity.get("unexpected_documents")),
            bool(candidate_integrity.get("documents_without_pages")),
            bool(candidate_integrity.get("documents_without_chunks")),
            bool(candidate_integrity.get("foreign_key_errors")),
            bool(candidate_integrity.get("fts_rowid_mismatches")),
            candidate_integrity.get("chunks") != candidate_integrity.get("fts_rows"),
            bool(candidate_integrity.get("page_inventory_errors")),
            bool(candidate_integrity.get("pages_unaccounted")),
            bool(candidate_integrity.get("regulatory_extraction_pending")),
            bool(candidate_integrity.get("regulatory_extraction_errors")),
        )
    )
    if fatal_integrity:
        reject("candidate_integrity", "La candidata no supera la integridad técnica")

    source_text = _source_text_inventory(candidate_data / "actas.db")
    if not _page_inventory_gate_passes(source_text):
        reject(
            "page_inventory_incomplete",
            "La candidata no contabiliza cada página física con texto "
            "fuente o un error de extracción verificable",
            source_text,
        )

    candidate_docs = int(candidate_integrity.get("indexed_documents", 0) or 0)
    baseline_docs = int(baseline_integrity.get("indexed_documents", 0) or 0)
    candidate_pages = int(candidate_integrity.get("pages_indexed", 0) or 0)
    baseline_pages = int(baseline_integrity.get("pages_indexed", 0) or 0)
    if candidate_docs < max(baseline_docs, manifest_count):
        reject(
            "document_regression",
            "La candidata pierde documentos publicados",
            {"baseline": baseline_docs, "candidate": candidate_docs},
        )
    if candidate_pages < baseline_pages:
        reject(
            "page_regression",
            "La candidata pierde páginas consultables",
            {"baseline": baseline_pages, "candidate": candidate_pages},
        )

    missing_decision_uids = _database_missing_decision_uids(
        candidate_data / "actas.db"
    )
    if missing_decision_uids != 0:
        reject(
            "missing_decision_uids",
            "La candidata contiene fichas sin identidad estable",
            {"records_without_uid": missing_decision_uids},
        )

    try:
        ambiguous_identities = int(
            identity_reconciliation.get("ambiguous", 0) or 0
        )
        ambiguous_documents = int(
            identity_reconciliation.get("documents_ambiguous", 0) or 0
        )
    except (TypeError, ValueError):
        ambiguous_identities = ambiguous_documents = -1
    if ambiguous_identities != 0 or ambiguous_documents != 0:
        reject(
            "identity_reconciliation_ambiguous",
            "La reconciliación contiene identidades ambiguas",
            {
                "records": ambiguous_identities,
                "documents": ambiguous_documents,
            },
        )

    expected_review = state.get("review_log") or {}
    current_review = _file_snapshot(review_log_path)
    if current_review != expected_review:
        reject(
            "review_log_changed",
            "El registro de revisiones cambió durante el reprocesamiento",
        )
    review_rows = _review_rows(review_log_path)
    reconciliation = _review_reconciliation(
        candidate_data / "actas.db",
        review_log_path,
    )
    reported_reviews = identity_reconciliation.get("review_reconciliation")
    if reported_reviews != reconciliation:
        reject(
            "reconciliation_report_changed",
            "La reconciliación de revisiones ya no coincide con la candidata",
        )
    if review_rows:
        if not reconciliation:
            reject(
                "reconciliation_missing",
                "No existe informe de reconciliación para las revisiones humanas",
            )
        else:
            try:
                reviews_total = int(reconciliation.get("reviews_total", 0) or 0)
                orphaned = int(reconciliation.get("orphaned", 0) or 0)
                ambiguous = int(reconciliation.get("ambiguous", 0) or 0)
                reconciled = int(reconciliation.get("reconciled", 0) or 0)
            except (TypeError, ValueError):
                reviews_total = orphaned = ambiguous = reconciled = -1
            if reviews_total < 1 or reconciled + orphaned + ambiguous < reviews_total:
                reject(
                    "reconciliation_incomplete",
                    "El informe no cubre todas las revisiones",
                    reconciliation,
                )
            if orphaned or ambiguous:
                reject(
                    "review_identity_regression",
                    "Hay revisiones huérfanas o ambiguas",
                    reconciliation,
                )

    candidate_fields = _find_nested_mapping(
        (candidate_indexing, candidate_integrity),
        "field_completeness",
    )
    baseline_fields = _find_nested_mapping(
        (baseline_integrity,),
        "field_completeness",
    )
    if not candidate_fields:
        candidate_fields = _database_field_completeness(
            candidate_data / "actas.db"
        )
    if not baseline_fields:
        baseline_fields = _database_field_completeness(
            baseline_data / "actas.db"
        )
    completeness_summary, completeness_issues = (
        _compare_required_field_completeness(candidate_fields, baseline_fields)
    )
    # Los campos estructurados ayudan a filtrar y construir cronologías, pero
    # el texto y la página del acta son la fuente de consulta. Una variación en
    # campos derivados se informa sin bloquear una base documental íntegra.
    advisories = completeness_issues

    if str(candidate_semantic.get("status", "")).lower() not in {"built", "reused"}:
        reject("semantic_report", "El índice semántico candidato no está vigente")

    try:
        smoke = _validate_restored_packages(workspace, manifest_path=manifest_path)
    except Exception as exc:
        smoke = {"error": str(exc)}
        reject("package_smoke", "Falló la restauración y smoke test", str(exc))
    else:
        restored_integrity = smoke["integrity"]
        if any(
            (
                str(restored_integrity.get("sqlite_integrity", "")).lower() != "ok",
                bool(restored_integrity.get("missing_documents")),
                bool(restored_integrity.get("unexpected_documents")),
                restored_integrity.get("chunks") != restored_integrity.get("fts_rows"),
                not smoke.get("semantic_current"),
            )
        ):
            reject(
                "restored_package_integrity",
                "Los paquetes restaurados no equivalen a los índices candidatos",
            )

    package_fingerprints: dict[str, object] = {}
    for name in DATABASE_NAMES:
        package = package_manifest_path(candidate_data / name)
        if not package.exists():
            reject("package_missing", f"Falta el paquete candidato {name}")
            continue
        payload = _load_json(package)
        parts: dict[str, str] = {}
        for item in payload.get("parts", []):
            part = candidate_data / str(item.get("name", ""))
            if not part.exists():
                reject("package_part_missing", f"Falta {part.name}")
                continue
            parts[part.name] = _sha256(part)
        package_fingerprints[name] = {
            "manifest_sha256": _sha256(package),
            "parts": parts,
        }
    report_fingerprints = {
        name: _sha256(candidate_data / name)
        for name in REPORT_NAMES
        if (candidate_data / name).exists()
    }

    report = {
        "format_version": STATE_FORMAT_VERSION,
        "generated_at": _utc_now(),
        "mode": mode,
        "status": (
            "accepted"
            if mode == "publish" and not issues
            else ("rejected" if issues else "diagnostic_complete")
        ),
        "publication_ready": bool(mode == "publish" and not issues),
        "source_fingerprint": state.get("source_fingerprint"),
        "baseline_fingerprint": state.get("baseline_fingerprint"),
        "review_log": current_review,
        "baseline": {"documents": baseline_docs, "pages": baseline_pages},
        "candidate": {"documents": candidate_docs, "pages": candidate_pages},
        "source_text": source_text,
        "review_reconciliation": reconciliation,
        "identity_reconciliation": identity_reconciliation,
        "field_completeness": completeness_summary,
        "advisories": advisories,
        "smoke": smoke,
        "packages": package_fingerprints,
        "reports": report_fingerprints,
        "issues": issues,
    }
    _atomic_json(workspace / "reprocess-report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def _verified_candidate_files(candidate_data: Path) -> list[Path]:
    files: list[Path] = []
    for name in DATABASE_NAMES:
        manifest_path = package_manifest_path(candidate_data / name)
        package = _load_json(manifest_path)
        files.append(manifest_path)
        for item in package.get("parts", []):
            part_name = str(item.get("name", ""))
            if Path(part_name).name != part_name or not part_name.startswith(
                f"{name}.gz.part-"
            ):
                raise ValueError("El paquete candidato contiene un nombre no permitido")
            part = candidate_data / part_name
            if not part.exists() or _sha256(part) != item.get("sha256"):
                raise ValueError(f"El fragmento candidato no es válido: {part_name}")
            files.append(part)
    for name in REPORT_NAMES:
        path = candidate_data / name
        if not path.exists():
            raise ValueError(f"Falta el reporte candidato: {name}")
        files.append(path)
    return files


def publish_candidate(
    workspace: Path,
    *,
    published_data: Path,
    review_log_path: Path,
) -> dict:
    workspace = _safe_workspace(workspace)
    validation_path = workspace / "reprocess-report.json"
    validation = _load_json(validation_path)
    if validation.get("mode") != "publish" or validation.get("status") != "accepted":
        raise ValueError("La candidata no tiene una validación de publicación aprobada")
    state = _load_json(workspace / "run-state.json")
    if _file_snapshot(review_log_path) != state.get("review_log"):
        raise ValueError("El registro de revisiones cambió; se cancela la publicación")

    candidate_data = workspace / "candidate" / "data"
    sources = _verified_candidate_files(candidate_data)
    expected_packages = validation.get("packages") or {}
    for name in DATABASE_NAMES:
        manifest = package_manifest_path(candidate_data / name)
        expected = (expected_packages.get(name) or {}).get("manifest_sha256")
        if not expected or _sha256(manifest) != expected:
            raise ValueError(f"{name} cambió después de la validación")
    expected_reports = validation.get("reports") or {}
    for name in REPORT_NAMES:
        path = candidate_data / name
        if not expected_reports.get(name) or _sha256(path) != expected_reports[name]:
            raise ValueError(f"{name} cambió después de la validación")

    published_data.mkdir(parents=True, exist_ok=True)
    staging = workspace / "publish-staging"
    _replace_directory(staging)
    for source in sources:
        shutil.copy2(source, staging / source.name)
    shutil.copy2(validation_path, staging / "reprocess-report.json")

    # Se reemplazan solo archivos enumerados; regulatory-review-log.csv nunca
    # forma parte del staging ni de los globs de limpieza.
    for source in staging.iterdir():
        temporary = published_data / f"{source.name}.publishing"
        shutil.copy2(source, temporary)
        temporary.replace(published_data / source.name)
    new_names = {path.name for path in staging.iterdir()}
    for database_name in DATABASE_NAMES:
        for old_part in published_data.glob(f"{database_name}.gz.part-*"):
            if old_part.name not in new_names:
                old_part.unlink()
    # La 0.7.2 retiró el módulo de evaluación. Si una publicación anterior
    # dejó este reporte, se elimina de forma explícita y recuperable por Git.
    (published_data / "evaluation-report.json").unlink(missing_ok=True)

    if _file_snapshot(review_log_path) != state.get("review_log"):
        raise RuntimeError("La publicación alteró el registro de revisiones")
    result = {
        "published_at": _utc_now(),
        "files": sorted(new_names),
        "review_log_preserved": True,
    }
    _atomic_json(workspace / "publication.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def _github_command_value(value: object) -> str:
    return (
        str(value or "")
        .replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
    )


def _markdown_cell(value: object) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace(
        "\n", " "
    )


def export_workflow_reports(
    workspace: Path,
    *,
    github_summary_path: Path | None = None,
) -> dict[str, object]:
    """Exporta los informes técnicos con nombres inequívocos."""

    workspace = _safe_workspace(workspace)
    export_dir = workspace / "export"
    _replace_directory(export_dir)
    exported: list[str] = []
    for source_name, exported_name in WORKFLOW_REPORT_EXPORTS:
        source = workspace / source_name
        if not source.exists() or not source.is_file():
            continue
        shutil.copy2(source, export_dir / exported_name)
        exported.append(exported_name)

    report = _load_json(workspace / "reprocess-report.json", required=False)
    checkpoint = _load_json(
        workspace / "candidate" / "checkpoint.json",
        required=False,
    )
    issues = report.get("issues") if isinstance(report.get("issues"), list) else []
    advisories = (
        report.get("advisories")
        if isinstance(report.get("advisories"), list)
        else []
    )
    if report:
        status = str(report.get("status") or "unknown")
        mode = str(report.get("mode") or "unknown")
    elif checkpoint and not checkpoint.get("complete"):
        status = "continuation_pending"
        mode = "unknown"
    else:
        status = "report_unavailable"
        mode = "unknown"

    readme = (
        "Informes del reprocesamiento\n"
        "=============================\n\n"
        "reprocess-report.json es el resultado consolidado y autoritativo.\n"
        "Los demás archivos indican candidate- o baseline- en su nombre.\n"
        "Las advertencias de campos derivados son informativas y no corrigen "
        "el contenido oficial de las actas.\n"
    )
    (export_dir / "LEEME-INFORMES.txt").write_text(readme, encoding="utf-8")
    exported.append("LEEME-INFORMES.txt")
    index = {
        "format_version": STATE_FORMAT_VERSION,
        "generated_at": _utc_now(),
        "status": status,
        "mode": mode,
        "issues": len(issues),
        "advisories": len(advisories),
        "files": sorted(exported),
    }
    _atomic_json(export_dir / "report-index.json", index)
    exported.append("report-index.json")

    if status == "rejected":
        print(
            "::warning title=La candidata no está aprobada::"
            + _github_command_value(
                f"El diagnóstico encontró {len(issues)} bloqueo(s). "
                "Un workflow verde solo confirma que el diagnóstico terminó."
            ),
            flush=True,
        )
        for issue in issues[:10]:
            if not isinstance(issue, dict):
                continue
            print(
                "::warning title="
                + _github_command_value(issue.get("code") or "Bloqueo")
                + "::"
                + _github_command_value(issue.get("message") or issue),
                flush=True,
            )
    elif status == "report_unavailable":
        print(
            "::warning title=Informe consolidado no disponible::"
            "La ejecución terminó antes de producir reprocess-report.json.",
            flush=True,
        )

    if github_summary_path is not None:
        candidate = report.get("candidate") if isinstance(report.get("candidate"), dict) else {}
        lines = [
            "## Resultado del reprocesamiento",
            "",
            "| Campo | Valor |",
            "|---|---|",
            f"| Estado | `{_markdown_cell(status)}` |",
            f"| Modo | `{_markdown_cell(mode)}` |",
            f"| Documentos candidatos | {_markdown_cell(candidate.get('documents', ''))} |",
            f"| Páginas candidatas | {_markdown_cell(candidate.get('pages', ''))} |",
            f"| Bloqueos técnicos | {len(issues)} |",
            f"| Advertencias informativas | {len(advisories)} |",
            "",
        ]
        if status == "continuation_pending":
            run_id = os.getenv("GITHUB_RUN_ID", "").strip()
            lines.extend(
                [
                    "> La candidata es parcial. Continúa con "
                    f"`resume_run_id={_markdown_cell(run_id or 'ID_DE_ESTA_EJECUCIÓN')}`.",
                    "",
                ]
            )
        elif status == "rejected":
            lines.extend(
                [
                    "> **No publiques esta candidata.** El color verde indica que "
                    "el diagnóstico pudo terminar, no que la candidata haya sido aprobada.",
                    "",
                    "### Bloqueos detectados",
                    "",
                    "| Código | Descripción |",
                    "|---|---|",
                ]
            )
            for issue in issues:
                if isinstance(issue, dict):
                    lines.append(
                        f"| `{_markdown_cell(issue.get('code', ''))}` | "
                        f"{_markdown_cell(issue.get('message', ''))} |"
                    )
            lines.append("")
        elif status == "diagnostic_complete":
            lines.extend(
                [
                    "> El diagnóstico técnico terminó sin bloqueos.",
                    "",
                ]
            )
        elif status == "accepted":
            lines.extend(["> La candidata superó los controles técnicos.", ""])

        if advisories:
            lines.extend(
                [
                    "### Advertencias sobre campos derivados",
                    "",
                    "| Código | Detalle |",
                    "|---|---|",
                ]
            )
            for item in advisories:
                lines.append(
                    f"| `{_markdown_cell(item.get('code', ''))}` | "
                    f"{_markdown_cell(item.get('message', ''))} |"
                )
            lines.append("")
        lines.extend(
            [
                "Los archivos descargables usan nombres distintos para la candidata "
                "y la base de referencia.",
                "",
            ]
        )
        github_summary_path.parent.mkdir(parents=True, exist_ok=True)
        with github_summary_path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines))

    result = {**index, "files": sorted(exported)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepara, construye y valida un reprocesamiento seguro"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    prepare.add_argument("--published-data", type=Path, default=ROOT_DIR / "data")
    prepare.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    prepare.add_argument("--review-log", type=Path, default=REVIEW_LOG_PATH)
    prepare.add_argument("--min-free-mib", type=int, default=REPROCESS_MIN_FREE_MIB)
    prepare.add_argument("--resume-from", type=Path)

    build = subparsers.add_parser("build")
    build.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    build.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    build.add_argument("--pdf-cache", type=Path, default=PDF_CACHE_DIR)
    build.add_argument("--batch-size", type=int, default=REPROCESS_BATCH_SIZE)
    build.add_argument("--max-batches", type=int)

    baseline = subparsers.add_parser("baseline-integrity")
    baseline.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    baseline.add_argument("--manifest", type=Path, default=MANIFEST_PATH)

    reconcile = subparsers.add_parser("reconcile")
    reconcile.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    reconcile.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    reconcile.add_argument("--review-log", type=Path, default=REVIEW_LOG_PATH)

    validate = subparsers.add_parser("validate")
    validate.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    validate.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    validate.add_argument("--review-log", type=Path, default=REVIEW_LOG_PATH)
    validate.add_argument(
        "--mode", choices=("diagnostic", "publish"), default="diagnostic"
    )

    export_reports = subparsers.add_parser("export-reports")
    export_reports.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    export_reports.add_argument("--github-summary", type=Path)

    publish = subparsers.add_parser("publish")
    publish.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    publish.add_argument("--published-data", type=Path, default=ROOT_DIR / "data")
    publish.add_argument("--review-log", type=Path, default=REVIEW_LOG_PATH)
    return parser.parse_args()


def main() -> int:
    arguments = parse_args()
    try:
        if arguments.command == "prepare":
            result = prepare_workspace(
                arguments.workspace,
                published_data=arguments.published_data,
                manifest_path=arguments.manifest,
                review_log_path=arguments.review_log,
                min_free_mib=arguments.min_free_mib,
                resume_from=arguments.resume_from,
            )
        elif arguments.command == "build":
            result = build_candidate_batches(
                arguments.workspace,
                manifest_path=arguments.manifest,
                pdf_cache_dir=arguments.pdf_cache,
                batch_size=arguments.batch_size,
                max_batches=arguments.max_batches,
            )
        elif arguments.command == "baseline-integrity":
            result = write_baseline_integrity(
                arguments.workspace,
                manifest_path=arguments.manifest,
            )
        elif arguments.command == "reconcile":
            result = reconcile_candidate_identities(
                arguments.workspace,
                manifest_path=arguments.manifest,
                review_log_path=arguments.review_log,
            )
        elif arguments.command == "validate":
            result = validate_candidate(
                arguments.workspace,
                manifest_path=arguments.manifest,
                review_log_path=arguments.review_log,
                mode=arguments.mode,
            )
            if arguments.mode == "publish" and result["status"] != "accepted":
                return 1
        elif arguments.command == "export-reports":
            result = export_workflow_reports(
                arguments.workspace,
                github_summary_path=arguments.github_summary,
            )
        else:
            result = publish_candidate(
                arguments.workspace,
                published_data=arguments.published_data,
                review_log_path=arguments.review_log,
            )
        if arguments.command not in {
            "build",
            "reconcile",
            "validate",
            "publish",
            "export-reports",
        }:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
