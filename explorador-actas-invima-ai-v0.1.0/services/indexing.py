from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from config import (
    ALLOWED_DOCUMENT_HOSTS,
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    MAX_PDF_BYTES,
)
from services.database import initialize_database, insert_document
from services.downloader import download_pdf, file_sha256
from services.manifest import load_manifest
from services.pdf_reader import extract_pdf_pages
from services.text_utils import chunk_text


@dataclass
class IndexingReport:
    documents_total: int = 0
    documents_indexed: int = 0
    documents_failed: int = 0
    pages_indexed: int = 0
    chunks_indexed: int = 0
    possible_scanned_pages: int = 0
    errors: list[str] | None = None

    def as_dict(self) -> dict:
        return asdict(self)


ProgressCallback = Callable[[int, int, str], None]


def rebuild_index(
    manifest_path: Path,
    database_path: Path,
    pdf_cache_dir: Path,
    progress_callback: ProgressCallback | None = None,
) -> IndexingReport:
    documents = load_manifest(manifest_path, ALLOWED_DOCUMENT_HOSTS)
    report = IndexingReport(documents_total=len(documents), errors=[])
    if not documents:
        return report

    temporary_database = database_path.with_suffix(".building.db")
    if temporary_database.exists():
        temporary_database.unlink()
    initialize_database(temporary_database)

    for position, metadata in enumerate(documents, start=1):
        if progress_callback:
            progress_callback(position, len(documents), metadata.title)
        try:
            pdf_path = download_pdf(
                metadata.title,
                metadata.url,
                pdf_cache_dir,
                ALLOWED_DOCUMENT_HOSTS,
                MAX_PDF_BYTES,
            )
            pages, possible_scans = extract_pdf_pages(pdf_path)
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

            insert_document(
                temporary_database,
                metadata,
                file_sha256(pdf_path),
                indexed_pages,
            )
            report.documents_indexed += 1
            report.pages_indexed += len(indexed_pages)
            report.possible_scanned_pages += len(possible_scans)
        except Exception as exc:  # el informe conserva errores por documento
            report.documents_failed += 1
            report.errors.append(f"{metadata.title}: {exc}")

    if report.documents_indexed == 0:
        temporary_database.unlink(missing_ok=True)
        return report

    database_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_database.replace(database_path)
    return report

