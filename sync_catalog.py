from __future__ import annotations

import argparse
import json
from pathlib import Path

from config import (
    ACTAS_CATALOG_PATH,
    ALLOWED_DOCUMENT_HOSTS,
    CATALOG_REPORT_PATH,
    CATALOG_SOURCE_URL,
    INDEX_START_YEAR,
    MANIFEST_PATH,
    SOURCE_SNAPSHOT_PATH,
)
from services.catalog import (
    build_catalog_report,
    build_source_snapshot,
    catalog_content_signature,
    fetch_catalog_html,
    load_catalog,
    manifest_rows,
    merge_catalog,
    parse_catalog_html,
    validate_discovery,
    write_catalog,
    write_catalog_report,
    write_manifest_from_catalog,
    write_source_snapshot,
)
from services.manifest import load_manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Actualiza el catálogo desde la página oficial de la sala"
    )
    parser.add_argument(
        "--html-file",
        type=Path,
        help="Usa una copia HTML local en lugar de consultar INVIMA",
    )
    parser.add_argument(
        "--bootstrap-only",
        action="store_true",
        help="Crea el catálogo inicial desde el manifiesto, sin consultar INVIMA",
    )
    parser.add_argument(
        "--no-strict",
        action="store_true",
        help="Omite las validaciones mínimas del histórico (solo para pruebas)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reescribe los archivos aunque el contenido documental no cambie",
    )
    parser.add_argument(
        "--index-from-year",
        type=int,
        default=INDEX_START_YEAR,
        help="Año mínimo de las actas que se incorporan al índice de texto",
    )
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    existing = load_catalog(ACTAS_CATALOG_PATH)
    manifest_documents = load_manifest(MANIFEST_PATH, ALLOWED_DOCUMENT_HOSTS)

    source_html = ""
    if arguments.bootstrap_only:
        discovered = []
    else:
        source_html = (
            arguments.html_file.read_text(encoding="utf-8")
            if arguments.html_file
            else fetch_catalog_html(CATALOG_SOURCE_URL, ALLOWED_DOCUMENT_HOSTS)
        )
        discovered = parse_catalog_html(
            source_html,
            CATALOG_SOURCE_URL,
            ALLOWED_DOCUMENT_HOSTS,
        )
        if not arguments.no_strict:
            validate_discovery(discovered, existing)

    records = merge_catalog(
        existing,
        discovered,
        manifest_documents,
        CATALOG_SOURCE_URL,
        source_checked=not arguments.bootstrap_only,
    )
    rows = manifest_rows(records, minimum_year=arguments.index_from_year)
    current_manifest_signature = {
        document.url: (
            document.title,
            document.year,
            document.acta_number,
            document.section,
            document.part,
            document.source_type,
        )
        for document in manifest_documents
    }
    desired_manifest_signature = {
        str(row["url"]): (
            str(row["title"]),
            int(row["year"]),
            str(row["acta_number"]),
            str(row["section"]),
            str(row["part"]) or None,
            str(row["source_type"]),
        )
        for row in rows
    }
    content_unchanged = (
        existing
        and not arguments.force
        and catalog_content_signature(existing) == catalog_content_signature(records)
        and current_manifest_signature == desired_manifest_signature
    )
    if not content_unchanged:
        write_catalog(records, ACTAS_CATALOG_PATH)
        write_manifest_from_catalog(
            records,
            MANIFEST_PATH,
            minimum_year=arguments.index_from_year,
        )
    report = build_catalog_report(
        records,
        discovered,
        len(rows),
        CATALOG_SOURCE_URL,
        source_checked=not arguments.bootstrap_only,
    )
    report["content_status"] = "unchanged" if content_unchanged else "changed"
    write_catalog_report(report, CATALOG_REPORT_PATH)
    if source_html:
        write_source_snapshot(
            build_source_snapshot(discovered, CATALOG_SOURCE_URL, source_html),
            SOURCE_SNAPSHOT_PATH,
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
