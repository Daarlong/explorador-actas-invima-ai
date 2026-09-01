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
    SEMANTIC_ENABLED,
    SEMANTIC_INDEX_PATH,
    SEMANTIC_LEXICAL_DIMENSION,
    SEMANTIC_REPORT_PATH,
    ensure_directories,
)
from services.database import sync_regulatory_extractions
from services.indexing import rebuild_index, update_index, write_indexing_report
from services.integrity import build_integrity_report, write_integrity_report
from services.semantic import (
    SEMANTIC_BUILD_SIGNATURE,
    SEMANTIC_METHOD,
    build_semantic_index,
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


def remove_semantic_artifacts() -> None:
    """Evita publicar un índice anterior contra una base documental nueva."""
    for path in (
        SEMANTIC_INDEX_PATH,
        SEMANTIC_INDEX_PATH.with_name(f"{SEMANTIC_INDEX_PATH.name}.gz"),
        SEMANTIC_INDEX_PATH.with_name(
            f"{SEMANTIC_INDEX_PATH.name}.package.json"
        ),
        SEMANTIC_INDEX_PATH.with_name(
            f"{SEMANTIC_INDEX_PATH.name}.package-id"
        ),
    ):
        path.unlink(missing_ok=True)
    for part in SEMANTIC_INDEX_PATH.parent.glob(
        f"{SEMANTIC_INDEX_PATH.name}.gz.part-*"
    ):
        part.unlink(missing_ok=True)


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
    if SEMANTIC_ENABLED:
        try:
            source_fingerprint = semantic_source_fingerprint(DATABASE_PATH)
            current = semantic_index_status(SEMANTIC_INDEX_PATH)
            is_current = (
                current.get("available")
                and current.get("source_fingerprint") == source_fingerprint
                and int(current.get("lexical_dimension", -1))
                == SEMANTIC_LEXICAL_DIMENSION
                and int(current.get("semantic_dimension", -1))
                == SEMANTIC_DISTRIBUTIONAL_DIMENSION
                and current.get("method") == SEMANTIC_METHOD
                and current.get("build_signature") == SEMANTIC_BUILD_SIGNATURE
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
            else:
                semantic = build_semantic_index(
                    DATABASE_PATH,
                    SEMANTIC_INDEX_PATH,
                    lexical_dimension=SEMANTIC_LEXICAL_DIMENSION,
                    semantic_dimension=SEMANTIC_DISTRIBUTIONAL_DIMENSION,
                )
                semantic_report = {
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "status": "built",
                    "message": "Índice semántico local construido correctamente.",
                    **semantic.as_dict(),
                }
                report.semantic_index_status = "built"
                report.semantic_documents_indexed = semantic.documents_indexed
        except Exception as exc:
            remove_semantic_artifacts()
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
    write_json_report(semantic_report, SEMANTIC_REPORT_PATH)
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
