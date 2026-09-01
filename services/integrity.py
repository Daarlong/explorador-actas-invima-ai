from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from services.database import (
    DATABASE_SCHEMA_VERSION,
    connect,
    database_schema_version,
)
from services.manifest import load_manifest


def _page_numbers(value: str | None) -> list[int]:
    if not value:
        return []
    return [int(item) for item in value.split(",") if item.strip().isdigit()]


def build_integrity_report(
    database_path: Path,
    manifest_path: Path,
    allowed_hosts: tuple[str, ...],
) -> dict:
    manifest = load_manifest(manifest_path, allowed_hosts)
    generated_at = datetime.now(timezone.utc).isoformat()
    if not database_path.exists():
        return {
            "status": "error",
            "generated_at": generated_at,
            "schema_version": 0,
            "expected_schema_version": DATABASE_SCHEMA_VERSION,
            "manifest_documents": len(manifest),
            "indexed_documents": 0,
            "missing_documents": [document.title for document in manifest],
            "unexpected_documents": [],
            "documents_without_pages": [],
            "documents_without_chunks": [],
            "ocr_candidates": [],
            "page_inventory_pending": [],
            "sqlite_integrity": "missing_database",
            "database_bytes": 0,
            "database_mib": 0.0,
            "pages_indexed": 0,
            "pdf_pages": 0,
            "chunks": 0,
            "fts_rows": 0,
        }

    with connect(database_path) as connection:
        sqlite_integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        counts = {
            table: int(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
            for table in ("documents", "pages", "chunks", "chunks_fts")
        }
        document_rows = connection.execute(
            """
            SELECT manifest_url, title, pdf_page_count, indexed_page_count,
                   ocr_candidate_pages, page_inventory_complete
            FROM documents
            ORDER BY year, acta_number, part, title
            """
        ).fetchall()
        without_pages = [
            row["title"]
            for row in connection.execute(
                """
                SELECT d.title
                FROM documents d
                LEFT JOIN pages p ON p.document_id = d.id
                GROUP BY d.id
                HAVING COUNT(p.id) = 0
                """
            ).fetchall()
        ]
        without_chunks = [
            row["title"]
            for row in connection.execute(
                """
                SELECT d.title
                FROM documents d
                LEFT JOIN pages p ON p.document_id = d.id
                LEFT JOIN chunks c ON c.page_id = p.id
                GROUP BY d.id
                HAVING COUNT(c.id) = 0
                """
            ).fetchall()
        ]

    manifest_by_url = {document.url: document.title for document in manifest}
    indexed_by_url = {row["manifest_url"]: row["title"] for row in document_rows}
    missing = [
        manifest_by_url[url]
        for url in manifest_by_url.keys() - indexed_by_url.keys()
    ]
    unexpected = [
        indexed_by_url[url]
        for url in indexed_by_url.keys() - manifest_by_url.keys()
    ]
    ocr_candidates = [
        {"title": row["title"], "pages": pages}
        for row in document_rows
        if (pages := _page_numbers(row["ocr_candidate_pages"]))
    ]
    page_inventory_pending = [
        row["title"]
        for row in document_rows
        if not bool(row["page_inventory_complete"])
    ]
    pdf_pages = sum(int(row["pdf_page_count"]) for row in document_rows)
    indexed_pages = sum(int(row["indexed_page_count"]) for row in document_rows)
    schema_version = database_schema_version(database_path)

    has_error = any(
        (
            sqlite_integrity.lower() != "ok",
            schema_version != DATABASE_SCHEMA_VERSION,
            bool(missing),
            bool(unexpected),
            bool(without_pages),
            bool(without_chunks),
            counts["chunks"] != counts["chunks_fts"],
        )
    )
    status = (
        "error"
        if has_error
        else ("warning" if ocr_candidates or page_inventory_pending else "ok")
    )
    database_bytes = database_path.stat().st_size
    return {
        "status": status,
        "generated_at": generated_at,
        "schema_version": schema_version,
        "expected_schema_version": DATABASE_SCHEMA_VERSION,
        "manifest_documents": len(manifest),
        "indexed_documents": counts["documents"],
        "missing_documents": sorted(missing),
        "unexpected_documents": sorted(unexpected),
        "documents_without_pages": sorted(without_pages),
        "documents_without_chunks": sorted(without_chunks),
        "ocr_candidates": ocr_candidates,
        "page_inventory_pending": sorted(page_inventory_pending),
        "sqlite_integrity": sqlite_integrity,
        "database_bytes": database_bytes,
        "database_mib": round(database_bytes / (1024 * 1024), 1),
        "pages_indexed": counts["pages"],
        "pdf_pages": pdf_pages,
        "chunks": counts["chunks"],
        "fts_rows": counts["chunks_fts"],
        "text_page_coverage_percent": (
            round((indexed_pages / pdf_pages) * 100, 2) if pdf_pages else 0.0
        ),
    }


def write_integrity_report(report: dict, target_path: Path) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    existing = load_integrity_report(target_path)
    if existing:
        comparable_existing = {
            key: value for key, value in existing.items() if key != "generated_at"
        }
        comparable_new = {
            key: value for key, value in report.items() if key != "generated_at"
        }
        if comparable_existing == comparable_new:
            return
    target_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_integrity_report(report_path: Path) -> dict | None:
    if not report_path.exists():
        return None
    try:
        value = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None
