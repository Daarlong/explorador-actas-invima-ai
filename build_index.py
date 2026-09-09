from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from config import (
    ALLOWED_DOCUMENT_HOSTS,
    DATABASE_PATH,
    INDEXING_REPORT_PATH,
    INTEGRITY_REPORT_PATH,
    MANIFEST_PATH,
    PDF_CACHE_DIR,
    SEMANTIC_DISTRIBUTIONAL_DIMENSION,
    SEMANTIC_CHECKPOINT_PATH,
    SEMANTIC_ENABLED,
    SEMANTIC_INDEX_PATH,
    SEMANTIC_LEXICAL_DIMENSION,
    SEMANTIC_MAX_SECONDS,
    SEMANTIC_MAX_UNIQUE,
    SEMANTIC_NEURAL_BATCH_SIZE,
    SEMANTIC_NEURAL_ENABLED,
    SEMANTIC_NEURAL_MODEL_ID,
    SEMANTIC_NEURAL_MODEL_REVISION,
    SEMANTIC_PROGRESS_PATH,
    SEMANTIC_REPORT_PATH,
    ensure_directories,
)
from services.database import sync_regulatory_extractions
from services.indexing import rebuild_index, update_index, write_indexing_report
from services.integrity import build_integrity_report, write_integrity_report
from services.semantic import (
    build_or_resume_semantic_index,
    build_semantic_index,
    semantic_build_spec,
    semantic_index_status,
    semantic_source_fingerprint,
)


def write_json_report(payload: dict, target_path) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    comparable_new = {
        key: value for key, value in payload.items() if key != "generated_at"
    }
    try:
        existing = json.loads(target_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        existing = None
    if isinstance(existing, dict):
        comparable_existing = {
            key: value for key, value in existing.items() if key != "generated_at"
        }
        if comparable_existing == comparable_new:
            return
    temporary = target_path.with_suffix(f"{target_path.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target_path)


def print_progress(current: int, total: int, title: str) -> None:
    print(f"[{current}/{total}] {title}", flush=True)


def remove_semantic_checkpoint() -> None:
    """Descarta solo la candidata; nunca toca el último índice publicado."""

    SEMANTIC_CHECKPOINT_PATH.unlink(missing_ok=True)
    SEMANTIC_CHECKPOINT_PATH.with_name(
        f"{SEMANTIC_CHECKPOINT_PATH.name}-shm"
    ).unlink(missing_ok=True)
    SEMANTIC_CHECKPOINT_PATH.with_name(
        f"{SEMANTIC_CHECKPOINT_PATH.name}-wal"
    ).unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Construye el índice de actas")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Reconstruye todos los documentos aunque exista un índice vigente",
    )
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help=(
            "Conserva los documentos correctos y deja los fallidos pendientes "
            "para la siguiente ejecución"
        ),
    )
    parser.add_argument(
        "--fail-on-semantic-error",
        action="store_true",
        help=(
            "Falla si el índice semántico no puede construirse; se usa para "
            "validar una base candidata antes de publicarla"
        ),
    )
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    ensure_directories()
    indexer = rebuild_index if arguments.full else update_index
    report = indexer(
        MANIFEST_PATH,
        DATABASE_PATH,
        PDF_CACHE_DIR,
        progress_callback=print_progress,
        allow_partial=arguments.allow_partial,
    )
    if report.documents_failed and not arguments.allow_partial:
        write_indexing_report(report, INDEXING_REPORT_PATH)
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
        raise SystemExit(
            f"La construcción terminó con {report.documents_failed} documento(s) "
            "fallido(s); se conservó el índice anterior."
        )

    regulatory = sync_regulatory_extractions(DATABASE_PATH)
    report.regulatory_documents_processed = int(
        regulatory.get("documents_processed", 0)
    )
    report.regulatory_records_extracted = int(
        regulatory.get("records_extracted", 0)
    )
    print(json.dumps({"regulatory_extraction": regulatory}, ensure_ascii=False, indent=2))

    semantic_report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "disabled",
        "message": "La construcción semántica está deshabilitada.",
    }
    semantic_progress = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "complete": not SEMANTIC_ENABLED,
        "progress_made": False,
        "before": 0,
        "after": 0,
        "remaining": 0,
        "segments_completed": 0,
    }
    if SEMANTIC_ENABLED:
        try:
            source_fingerprint = semantic_source_fingerprint(DATABASE_PATH)
            desired_method, desired_signature = semantic_build_spec(
                neural_enabled=SEMANTIC_NEURAL_ENABLED,
                neural_model_id=SEMANTIC_NEURAL_MODEL_ID,
                neural_model_revision=SEMANTIC_NEURAL_MODEL_REVISION,
            )
            current = semantic_index_status(
                SEMANTIC_INDEX_PATH,
                DATABASE_PATH,
            )
            is_current = (
                current.get("available")
                and current.get("source_fingerprint") == source_fingerprint
                and int(current.get("lexical_dimension", -1))
                == SEMANTIC_LEXICAL_DIMENSION
                and int(current.get("semantic_dimension", -1))
                == SEMANTIC_DISTRIBUTIONAL_DIMENSION
                and current.get("method") == desired_method
                and current.get("build_signature") == desired_signature
                and not arguments.full
            )
            if is_current:
                semantic_report = {
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "status": "reused",
                    "message": "El índice semántico ya cubre todos los fragmentos.",
                    **current,
                }
                report.semantic_index_status = "reused"
                report.semantic_documents_indexed = int(
                    current.get("documents", 0)
                )
                SEMANTIC_CHECKPOINT_PATH.unlink(missing_ok=True)
                semantic_progress.update(
                    {
                        "complete": True,
                        "before": int(current.get("documents", 0)),
                        "after": int(current.get("documents", 0)),
                        "remaining": 0,
                        "source_fingerprint": source_fingerprint,
                        "build_signature": desired_signature,
                    }
                )
            elif SEMANTIC_NEURAL_ENABLED:
                previous_segments = 0
                try:
                    previous_progress = json.loads(
                        SEMANTIC_PROGRESS_PATH.read_text(encoding="utf-8")
                    )
                    if (
                        isinstance(previous_progress, dict)
                        and previous_progress.get("source_fingerprint")
                        == source_fingerprint
                        and previous_progress.get("build_signature")
                        == desired_signature
                    ):
                        previous_segments = int(
                            previous_progress.get("segments_completed", 0)
                        )
                except (OSError, TypeError, ValueError, json.JSONDecodeError):
                    pass

                semantic = build_or_resume_semantic_index(
                    DATABASE_PATH,
                    SEMANTIC_INDEX_PATH,
                    SEMANTIC_CHECKPOINT_PATH,
                    lexical_dimension=SEMANTIC_LEXICAL_DIMENSION,
                    semantic_dimension=SEMANTIC_DISTRIBUTIONAL_DIMENSION,
                    neural_model_id=SEMANTIC_NEURAL_MODEL_ID,
                    neural_model_revision=SEMANTIC_NEURAL_MODEL_REVISION,
                    neural_batch_size=SEMANTIC_NEURAL_BATCH_SIZE,
                    max_unique_texts=SEMANTIC_MAX_UNIQUE,
                    max_seconds=SEMANTIC_MAX_SECONDS,
                )
                report.semantic_documents_indexed = semantic.documents_after
                progress_made = semantic.progress_made
                semantic_progress = {
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "complete": semantic.complete,
                    "progress_made": progress_made,
                    "before": semantic.documents_before,
                    "after": semantic.documents_after,
                    "remaining": semantic.documents_remaining,
                    "unique_before": semantic.unique_texts_before,
                    "unique_after": semantic.unique_texts_after,
                    "unique_remaining": max(
                        0,
                        semantic.unique_texts_total
                        - semantic.unique_texts_after,
                    ),
                    "segments_completed": previous_segments
                    + (1 if progress_made else 0),
                    "source_fingerprint": semantic.source_fingerprint,
                    "build_signature": semantic.build_signature,
                    "checkpoint_reused": semantic.checkpoint_reused,
                }
                if semantic.complete:
                    built_state = semantic_index_status(
                        SEMANTIC_INDEX_PATH,
                        DATABASE_PATH,
                    )
                    if not built_state.get("available"):
                        raise RuntimeError(
                            "El índice neuronal final no quedó disponible"
                        )
                    semantic_report = {
                        "generated_at": datetime.now(timezone.utc).isoformat(),
                        "status": "built",
                        "message": (
                            "Índice semántico neuronal multilingüe construido."
                        ),
                        "neural_status": "ready",
                        "documents": semantic.documents_total,
                        "neural_documents": semantic.documents_after,
                        **semantic.as_dict(),
                    }
                    report.semantic_index_status = "built"
                else:
                    semantic_report = {
                        "generated_at": datetime.now(timezone.utc).isoformat(),
                        "status": "building",
                        "message": (
                            "La construcción neuronal quedó confirmada y "
                            "continuará automáticamente en otra ejecución."
                        ),
                        "neural_status": "building",
                        "documents": semantic.documents_total,
                        "neural_documents": semantic.documents_after,
                        **semantic.as_dict(),
                    }
                    report.semantic_index_status = "building"
            else:
                semantic = build_semantic_index(
                    DATABASE_PATH,
                    SEMANTIC_INDEX_PATH,
                    lexical_dimension=SEMANTIC_LEXICAL_DIMENSION,
                    semantic_dimension=SEMANTIC_DISTRIBUTIONAL_DIMENSION,
                    neural_enabled=SEMANTIC_NEURAL_ENABLED,
                    neural_model_id=SEMANTIC_NEURAL_MODEL_ID,
                    neural_model_revision=SEMANTIC_NEURAL_MODEL_REVISION,
                    neural_batch_size=SEMANTIC_NEURAL_BATCH_SIZE,
                )
                built_state = semantic_index_status(
                    SEMANTIC_INDEX_PATH,
                    DATABASE_PATH,
                )
                semantic_report = {
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "status": "built",
                    "message": (
                        "Índice semántico neuronal multilingüe construido."
                        if semantic.neural_status == "ready"
                        else "Índice semántico local construido; el complemento "
                        "neuronal no estuvo disponible."
                    ),
                    **semantic.as_dict(),
                    "method": built_state.get("method"),
                    "build_signature": built_state.get("build_signature"),
                }
                report.semantic_index_status = "built"
                report.semantic_documents_indexed = semantic.documents_indexed
                semantic_progress.update(
                    {
                        "complete": True,
                        "before": semantic.documents_indexed,
                        "after": semantic.documents_indexed,
                        "remaining": 0,
                        "source_fingerprint": semantic.source_fingerprint,
                        "build_signature": desired_signature,
                    }
                )
        except Exception as exc:
            report.semantic_index_status = "error"
            semantic_report = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "status": "error",
                "message": str(exc),
            }
            print(
                "ADVERTENCIA: la búsqueda textual seguirá disponible, pero no "
                f"fue posible actualizar el índice semántico: {exc}",
                flush=True,
            )
            semantic_progress.update(
                {
                    "complete": False,
                    "progress_made": False,
                    "error": str(exc),
                }
            )
    write_json_report(semantic_report, SEMANTIC_REPORT_PATH)
    write_json_report(semantic_progress, SEMANTIC_PROGRESS_PATH)
    write_indexing_report(report, INDEXING_REPORT_PATH)
    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))

    integrity = build_integrity_report(
        DATABASE_PATH,
        MANIFEST_PATH,
        ALLOWED_DOCUMENT_HOSTS,
    )
    write_integrity_report(integrity, INTEGRITY_REPORT_PATH)
    print(json.dumps(integrity, ensure_ascii=False, indent=2))
    fatal_integrity_error = any(
        (
            str(integrity.get("sqlite_integrity", "")).lower() != "ok",
            integrity.get("schema_version")
            != integrity.get("expected_schema_version"),
            bool(integrity.get("unexpected_documents")),
            bool(integrity.get("documents_without_pages")),
            bool(integrity.get("documents_without_chunks")),
            integrity.get("chunks") != integrity.get("fts_rows"),
            bool(integrity.get("foreign_key_errors")),
            bool(integrity.get("fts_rowid_mismatches")),
        )
    )
    if integrity["status"] == "error" and (
        fatal_integrity_error or not arguments.allow_partial
    ):
        raise SystemExit("El informe de integridad detectó errores en el índice")
    if report.documents_failed:
        print(
            f"ADVERTENCIA: {report.documents_failed} documento(s) quedaron "
            "pendientes y se reintentarán en la siguiente ejecución.",
            flush=True,
        )
    semantic_unusable = semantic_report.get("status") not in {
        "built",
        "reused",
        "building",
    }
    neural_unavailable = (
        SEMANTIC_NEURAL_ENABLED
        and semantic_report.get("neural_status") not in {"ready", "building"}
    )
    if arguments.fail_on_semantic_error and (
        semantic_unusable or neural_unavailable
    ):
        raise SystemExit(
            "La candidata requiere el índice semántico configurado y vigente "
            "antes de publicarse"
        )
