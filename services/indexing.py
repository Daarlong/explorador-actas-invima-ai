from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from config import (
    ACTAS_CATALOG_PATH,
    ALLOWED_DOCUMENT_HOSTS,
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    MAX_PDF_BYTES,
    OCR_DPI,
    OCR_ENABLED,
    OCR_LANGUAGES,
    OCR_MIN_CHARS,
    OCR_TIMEOUT_SECONDS,
)
from services.catalog import load_catalog
from services.database import (
    connect,
    indexed_document_catalog,
    initialize_database,
    insert_document,
    is_current_schema,
    migrate_database_schema,
    optimize_database,
    sync_document_metadata,
)
from services.downloader import download_pdf_resource, file_sha256
from services.manifest import load_manifest
from services.models import DocumentMetadata
from services.pdf_reader import extract_pdf_pages
from services.text_utils import chunk_text


@dataclass
class IndexingReport:
    mode: str = "full"
    documents_total: int = 0
    documents_existing: int = 0
    documents_migrated: int = 0
    documents_metadata_updated: int = 0
    documents_indexed: int = 0
    documents_skipped: int = 0
    documents_failed: int = 0
    pages_indexed: int = 0
    chunks_indexed: int = 0
    possible_scanned_pages: int = 0
    ocr_pages_indexed: int = 0
    regulatory_documents_processed: int = 0
    regulatory_records_extracted: int = 0
    semantic_index_status: str = "not_run"
    semantic_documents_indexed: int = 0
    database_size_bytes: int = 0
    errors: list[str] | None = None
    alternate_links_used: list[dict[str, str]] | None = None

    def as_dict(self) -> dict:
        return asdict(self)


ProgressCallback = Callable[[int, int, str], None]


def _alternate_urls_by_primary() -> dict[str, tuple[str, ...]]:
    try:
        records = load_catalog(ACTAS_CATALOG_PATH)
    except (OSError, ValueError):
        return {}
    alternatives: dict[str, list[str]] = {}
    for record in records:
        if not record.url:
            continue
        values = alternatives.setdefault(record.url, [])
        for url in record.alternate_urls:
            if url and url != record.url and url not in values:
                values.append(url)
    return {url: tuple(values) for url, values in alternatives.items() if values}


def _download_with_fallback(
    metadata: DocumentMetadata,
    pdf_cache_dir: Path,
    alternate_urls: tuple[str, ...],
):
    errors: list[str] = []
    candidates = tuple(dict.fromkeys((metadata.url, *alternate_urls)))
    for candidate in candidates:
        try:
            downloaded = download_pdf_resource(
                metadata.title,
                candidate,
                pdf_cache_dir,
                ALLOWED_DOCUMENT_HOSTS,
                MAX_PDF_BYTES,
            )
            return downloaded, candidate
        except Exception as exc:  # cada URL se intenta de forma independiente
            errors.append(f"{candidate}: {str(exc)[:240]}")
    raise ValueError("Ningún enlace candidato produjo un PDF. " + " | ".join(errors))


def write_indexing_report(report: IndexingReport, target_path: Path) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **report.as_dict(),
    }
    existing = load_indexing_report(target_path)
    if existing:
        comparable_existing = {
            key: value for key, value in existing.items() if key != "generated_at"
        }
        comparable_new = {
            key: value for key, value in payload.items() if key != "generated_at"
        }
        if comparable_existing == comparable_new:
            return
    temporary_path = target_path.with_suffix(f"{target_path.suffix}.tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(target_path)


def load_indexing_report(report_path: Path) -> dict | None:
    if not report_path.exists():
        return None
    try:
        value = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _remove_database_files(database_path: Path) -> None:
    database_path.unlink(missing_ok=True)
    database_path.with_name(f"{database_path.name}-shm").unlink(missing_ok=True)
    database_path.with_name(f"{database_path.name}-wal").unlink(missing_ok=True)


def _metadata_signature(document: DocumentMetadata) -> dict:
    return {
        "title": document.title,
        "year": document.year,
        "acta_number": document.acta_number,
        "section": document.section,
        "part": document.part,
        "source_type": document.source_type,
    }


def _index_documents(
    documents: Iterable[DocumentMetadata],
    database_path: Path,
    pdf_cache_dir: Path,
    report: IndexingReport,
    progress_callback: ProgressCallback | None,
) -> None:
    pending_documents = list(documents)
    alternate_urls_lookup = _alternate_urls_by_primary()
    for position, metadata in enumerate(pending_documents, start=1):
        if progress_callback:
            progress_callback(position, len(pending_documents), metadata.title)
        try:
            downloaded, candidate_url = _download_with_fallback(
                metadata,
                pdf_cache_dir,
                alternate_urls_lookup.get(metadata.url, ()),
            )
            pages, possible_scans = extract_pdf_pages(
                downloaded.path,
                ocr_enabled=OCR_ENABLED,
                ocr_languages=OCR_LANGUAGES,
                ocr_dpi=OCR_DPI,
                ocr_timeout_seconds=OCR_TIMEOUT_SECONDS,
                ocr_min_chars=OCR_MIN_CHARS,
            )
            if not pages:
                raise ValueError("No se extrajo texto; el documento podría requerir OCR")

            indexed_pages: list[dict] = []
            for page in pages:
                chunks = chunk_text(
                    page["text"],
                    chunk_size=CHUNK_SIZE,
                    overlap=CHUNK_OVERLAP,
                )
                if not chunks:
                    continue
                indexed_pages.append({**page, "chunks": chunks})
                report.chunks_indexed += len(chunks)

            if not indexed_pages:
                raise ValueError("El PDF no produjo fragmentos consultables")

            insert_document(
                database_path,
                replace(metadata, url=downloaded.resolved_url),
                file_sha256(downloaded.path),
                indexed_pages,
                manifest_url=metadata.url,
                pdf_page_count=len(pages) + len(possible_scans),
                possible_scans=possible_scans,
            )
            report.documents_indexed += 1
            report.pages_indexed += len(indexed_pages)
            report.ocr_pages_indexed += sum(
                bool(page.get("ocr_used")) for page in indexed_pages
            )
            report.possible_scanned_pages += len(possible_scans)
            if candidate_url != metadata.url:
                if report.alternate_links_used is None:
                    report.alternate_links_used = []
                report.alternate_links_used.append(
                    {
                        "title": metadata.title,
                        "manifest_url": metadata.url,
                        "used_url": candidate_url,
                    }
                )
        except Exception as exc:  # el informe conserva errores por documento
            report.documents_failed += 1
            if report.errors is None:
                report.errors = []
            report.errors.append(f"{metadata.title}: {exc}")


def _migrate_legacy_index(
    database_path: Path,
    documents: list[DocumentMetadata],
    progress_callback: ProgressCallback | None,
) -> tuple[int, int, int, int] | None:
    """Compacta el esquema anterior usando sus fragmentos, sin descargar PDF."""
    manifest_by_title = {document.title: document for document in documents}
    if len(manifest_by_title) != len(documents):
        return None

    temporary_database = database_path.with_suffix(".migrating.db")
    _remove_database_files(temporary_database)
    try:
        with connect(database_path) as source:
            table_names = {
                row[0]
                for row in source.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                ).fetchall()
            }
            if not {"documents", "pages", "chunks"}.issubset(table_names):
                return None
            legacy_documents = source.execute(
                """
                SELECT id, title, url, document_hash
                FROM documents
                ORDER BY id
                """
            ).fetchall()
            if not legacy_documents or any(
                row["title"] not in manifest_by_title for row in legacy_documents
            ):
                return None

            initialize_database(temporary_database)
            migrated_pages = 0
            migrated_chunks = 0
            inferred_scan_pages = 0
            for position, row in enumerate(legacy_documents, start=1):
                if progress_callback:
                    progress_callback(position, len(legacy_documents), row["title"])
                page_rows = source.execute(
                    """
                    SELECT id, page_number
                    FROM pages
                    WHERE document_id = ?
                    ORDER BY page_number
                    """,
                    (row["id"],),
                ).fetchall()
                pages_by_id = {
                    int(page["id"]): {
                        "page": int(page["page_number"]),
                        "chunks": [],
                    }
                    for page in page_rows
                }
                chunk_rows = source.execute(
                    """
                    SELECT c.page_id, c.text
                    FROM chunks c
                    JOIN pages p ON p.id = c.page_id
                    WHERE p.document_id = ?
                    ORDER BY p.page_number, c.chunk_index
                    """,
                    (row["id"],),
                ).fetchall()
                for chunk in chunk_rows:
                    pages_by_id[int(chunk["page_id"])]["chunks"].append(chunk["text"])
                indexed_pages = [
                    page for page in pages_by_id.values() if page["chunks"]
                ]
                if not indexed_pages:
                    raise ValueError(
                        f"El índice anterior no contiene fragmentos para {row['title']}"
                    )

                page_numbers = {page["page"] for page in indexed_pages}
                maximum_page = max(page_numbers)
                possible_scans = sorted(set(range(1, maximum_page + 1)) - page_numbers)
                manifest_document = manifest_by_title[row["title"]]
                insert_document(
                    temporary_database,
                    replace(manifest_document, url=row["url"]),
                    row["document_hash"],
                    indexed_pages,
                    manifest_url=manifest_document.url,
                    pdf_page_count=maximum_page,
                    possible_scans=possible_scans,
                    page_inventory_complete=False,
                )
                migrated_pages += len(indexed_pages)
                migrated_chunks += len(chunk_rows)
                inferred_scan_pages += len(possible_scans)

        optimize_database(temporary_database)
        temporary_database.replace(database_path)
        return (
            len(legacy_documents),
            migrated_pages,
            migrated_chunks,
            inferred_scan_pages,
        )
    except Exception:
        _remove_database_files(temporary_database)
        return None


def rebuild_index(
    manifest_path: Path,
    database_path: Path,
    pdf_cache_dir: Path,
    progress_callback: ProgressCallback | None = None,
    *,
    mode: str = "full",
    allow_partial: bool = False,
) -> IndexingReport:
    documents = load_manifest(manifest_path, ALLOWED_DOCUMENT_HOSTS)
    report = IndexingReport(
        mode=mode,
        documents_total=len(documents),
        errors=[],
    )
    if not documents:
        return report

    temporary_database = database_path.with_suffix(".building.db")
    _remove_database_files(temporary_database)
    initialize_database(temporary_database)
    _index_documents(
        documents,
        temporary_database,
        pdf_cache_dir,
        report,
        progress_callback,
    )

    if report.documents_indexed == 0 or (
        report.documents_failed and not allow_partial
    ):
        _remove_database_files(temporary_database)
        return report

    optimize_database(temporary_database)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_database.replace(database_path)
    report.database_size_bytes = database_path.stat().st_size
    return report


def update_index(
    manifest_path: Path,
    database_path: Path,
    pdf_cache_dir: Path,
    progress_callback: ProgressCallback | None = None,
    *,
    allow_partial: bool = False,
) -> IndexingReport:
    documents = load_manifest(manifest_path, ALLOWED_DOCUMENT_HOSTS)
    if not database_path.exists():
        return rebuild_index(
            manifest_path,
            database_path,
            pdf_cache_dir,
            progress_callback,
            mode="full_initial",
            allow_partial=allow_partial,
        )
    schema_migrated = migrate_database_schema(database_path)
    migration: tuple[int, int, int, int] | None = None
    if not is_current_schema(database_path):
        migration = _migrate_legacy_index(
            database_path,
            documents,
            progress_callback,
        )
        if migration is None:
            return rebuild_index(
                manifest_path,
                database_path,
                pdf_cache_dir,
                progress_callback,
                mode="full_schema_upgrade",
                allow_partial=allow_partial,
            )

    existing = indexed_document_catalog(database_path)
    metadata_updated = sync_document_metadata(database_path, documents)
    expected = {document.url: _metadata_signature(document) for document in documents}
    existing_urls = set(existing)
    expected_urls = set(expected)
    metadata_changed = any(
        existing[url] != expected[url] for url in existing_urls & expected_urls
    )
    if existing_urls - expected_urls or metadata_changed:
        return rebuild_index(
            manifest_path,
            database_path,
            pdf_cache_dir,
            progress_callback,
            mode="full_catalog_change",
            allow_partial=allow_partial,
        )

    new_documents = [
        document for document in documents if document.url not in existing_urls
    ]
    report = IndexingReport(
        mode=(
            "legacy_migration"
            if migration
            else ("schema_migration" if schema_migrated else "incremental")
        ),
        documents_total=len(documents),
        documents_existing=len(existing),
        documents_migrated=migration[0] if migration else 0,
        documents_metadata_updated=metadata_updated,
        documents_skipped=len(existing),
        pages_indexed=migration[1] if migration else 0,
        chunks_indexed=migration[2] if migration else 0,
        possible_scanned_pages=migration[3] if migration else 0,
        errors=[],
    )
    if not new_documents:
        report.database_size_bytes = database_path.stat().st_size
        return report

    if migration:
        report.mode = "legacy_migration_incremental"
    elif schema_migrated:
        report.mode = "schema_migration_incremental"

    temporary_database = database_path.with_suffix(".updating.db")
    _remove_database_files(temporary_database)
    shutil.copy2(database_path, temporary_database)
    _index_documents(
        new_documents,
        temporary_database,
        pdf_cache_dir,
        report,
        progress_callback,
    )

    if report.documents_indexed == 0 or (
        report.documents_failed and not allow_partial
    ):
        _remove_database_files(temporary_database)
        return report

    optimize_database(temporary_database)
    temporary_database.replace(database_path)
    report.database_size_bytes = database_path.stat().st_size
    return report
