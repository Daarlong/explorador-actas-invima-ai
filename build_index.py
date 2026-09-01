from __future__ import annotations

import argparse
import json

from config import (
    ALLOWED_DOCUMENT_HOSTS,
    DATABASE_PATH,
    INTEGRITY_REPORT_PATH,
    MANIFEST_PATH,
    PDF_CACHE_DIR,
    ensure_directories,
)
from services.indexing import rebuild_index, update_index
from services.integrity import build_integrity_report, write_integrity_report


def print_progress(current: int, total: int, title: str) -> None:
    print(f"[{current}/{total}] {title}", flush=True)


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
    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    if report.documents_failed and not arguments.allow_partial:
        raise SystemExit(
            f"La construcción terminó con {report.documents_failed} documento(s) "
            "fallido(s); se conservó el índice anterior."
        )

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
