from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import zlib
from difflib import SequenceMatcher
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from services.models import DocumentMetadata, SearchResult
from services.regulatory import (
    RegulatoryRecord,
    extract_regulatory_records,
    normalize_field_evidence_ordinals,
)
from services.text_utils import normalize_text, tokenize_query


DATABASE_SCHEMA_VERSION = 6

REGULATORY_EXTRACTOR_VERSION = "5"
PAGE_TEXT_CODEC = "zlib-utf8-v1"
PAGE_TEXT_EXTRACTOR_VERSION = "pymupdf-text-v1"
PAGE_TEXT_SOURCES = frozenset({"native_pdf", "ocr"})
UID_RECONCILIATION_KEYS = (
    "uids_reconciled",
    "uids_generated",
    "uids_ambiguous",
    "uids_orphaned",
)

FEATURE_SCHEMA = """
CREATE TABLE IF NOT EXISTS document_extractions (
    document_id INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    document_hash TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    status TEXT NOT NULL,
    record_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    extracted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS regulatory_records (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    record_key TEXT NOT NULL,
    decision_uid TEXT NOT NULL DEFAULT '',
    page_number INTEGER NOT NULL,
    end_page_number INTEGER NOT NULL,
    numeral TEXT,
    numeral_title TEXT,
    session_date TEXT,
    session_date_raw TEXT,
    request_type_code TEXT,
    identity_strategy TEXT,
    product_name TEXT,
    normalized_product_name TEXT,
    active_ingredient TEXT,
    normalized_active_ingredient TEXT,
    interested_party TEXT,
    normalized_interested_party TEXT,
    expediente TEXT,
    normalized_expediente TEXT,
    radicado TEXT,
    normalized_radicado TEXT,
    request_text TEXT,
    concept_text TEXT,
    outcome_code TEXT NOT NULL,
    extraction_method TEXT NOT NULL,
    confidence REAL,
    needs_review INTEGER NOT NULL DEFAULT 1,
    extractor_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(document_id, record_key),
    CHECK(page_number >= 1),
    CHECK(end_page_number >= page_number),
    CHECK(confidence IS NULL OR confidence BETWEEN 0 AND 1)
);

CREATE TABLE IF NOT EXISTS regulatory_field_evidence (
    id INTEGER PRIMARY KEY,
    record_id INTEGER NOT NULL REFERENCES regulatory_records(id) ON DELETE CASCADE,
    field_name TEXT NOT NULL,
    ordinal INTEGER NOT NULL DEFAULT 0,
    literal_value TEXT,
    normalized_value TEXT,
    canonical_value TEXT,
    page_number INTEGER NOT NULL,
    end_page_number INTEGER NOT NULL,
    evidence_text TEXT,
    extraction_method TEXT NOT NULL,
    confidence REAL,
    UNIQUE(record_id, field_name, ordinal),
    CHECK(page_number >= 1),
    CHECK(end_page_number >= page_number),
    CHECK(confidence IS NULL OR confidence BETWEEN 0 AND 1)
);

CREATE INDEX IF NOT EXISTS idx_records_document
    ON regulatory_records(document_id, page_number);
CREATE INDEX IF NOT EXISTS idx_records_product
    ON regulatory_records(normalized_product_name);
CREATE INDEX IF NOT EXISTS idx_records_active
    ON regulatory_records(normalized_active_ingredient);
CREATE INDEX IF NOT EXISTS idx_records_interested
    ON regulatory_records(normalized_interested_party);
CREATE INDEX IF NOT EXISTS idx_records_expediente
    ON regulatory_records(normalized_expediente);
CREATE INDEX IF NOT EXISTS idx_records_radicado
    ON regulatory_records(normalized_radicado);
CREATE INDEX IF NOT EXISTS idx_records_outcome
    ON regulatory_records(outcome_code);
CREATE INDEX IF NOT EXISTS idx_records_numeral
    ON regulatory_records(numeral);
CREATE INDEX IF NOT EXISTS idx_records_request_type
    ON regulatory_records(request_type_code);
CREATE UNIQUE INDEX IF NOT EXISTS idx_records_decision_uid
    ON regulatory_records(decision_uid) WHERE decision_uid != '';
CREATE INDEX IF NOT EXISTS idx_field_evidence_name_normalized
    ON regulatory_field_evidence(field_name, normalized_value);
CREATE INDEX IF NOT EXISTS idx_field_evidence_record
    ON regulatory_field_evidence(record_id);
"""

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS app_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    url TEXT NOT NULL,
    manifest_url TEXT NOT NULL UNIQUE,
    year INTEGER,
    acta_number TEXT,
    section TEXT,
    part TEXT,
    source_type TEXT NOT NULL DEFAULT 'official',
    catalog_id TEXT,
    publication_date TEXT,
    document_hash TEXT NOT NULL,
    pdf_page_count INTEGER NOT NULL,
    indexed_page_count INTEGER NOT NULL,
    ocr_candidate_pages TEXT NOT NULL DEFAULT '',
    page_inventory_complete INTEGER NOT NULL DEFAULT 1,
    indexed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pages (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL,
    raw_text_compressed BLOB,
    raw_text_codec TEXT,
    text_source TEXT CHECK(text_source IS NULL OR text_source IN ('native_pdf', 'ocr')),
    text_quality REAL CHECK(text_quality IS NULL OR text_quality BETWEEN 0 AND 1),
    extraction_error TEXT,
    text_extractor_version TEXT,
    CHECK(
        (raw_text_compressed IS NULL AND raw_text_codec IS NULL)
        OR
        (raw_text_compressed IS NOT NULL AND raw_text_codec = 'zlib-utf8-v1')
    ),
    UNIQUE(document_id, page_number)
);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    text TEXT NOT NULL,
    UNIQUE(page_id, chunk_index)
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    title,
    text,
    content='',
    detail=column,
    tokenize = 'unicode61 remove_diacritics 2'
);

""" + FEATURE_SCHEMA + """

CREATE INDEX IF NOT EXISTS idx_documents_year ON documents(year);
CREATE INDEX IF NOT EXISTS idx_documents_acta ON documents(acta_number);
CREATE INDEX IF NOT EXISTS idx_documents_section ON documents(section);
CREATE INDEX IF NOT EXISTS idx_pages_document ON pages(document_id);
CREATE INDEX IF NOT EXISTS idx_chunks_page ON chunks(page_id);
"""


def _ensure_legacy_columns(connection: sqlite3.Connection) -> None:
    """Mantiene consultable el índice anterior durante una actualización."""
    changed = False
    table_exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'documents'"
    ).fetchone()
    if not table_exists:
        return
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(documents)").fetchall()
    }
    document_additions = {
        "source_type": "TEXT NOT NULL DEFAULT 'official'",
        "catalog_id": "TEXT",
        "publication_date": "TEXT",
        # Una base anterior no puede afirmar que conserva todas las páginas
        # físicas hasta que se reprocesen los PDF originales.
        "page_inventory_complete": "INTEGER NOT NULL DEFAULT 0",
    }
    for column, definition in document_additions.items():
        if column not in columns:
            connection.execute(f"ALTER TABLE documents ADD COLUMN {column} {definition}")
            changed = True
    pages_exist = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'pages'"
    ).fetchone()
    if pages_exist:
        page_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(pages)").fetchall()
        }
        source_inventory_added = any(
            column not in page_columns
            for column in (
                "raw_text_compressed",
                "raw_text_codec",
                "text_source",
                "text_quality",
                "extraction_error",
                "text_extractor_version",
            )
        )
        page_additions = {
            "raw_text_compressed": "BLOB",
            "raw_text_codec": "TEXT",
            "text_source": (
                "TEXT CHECK(text_source IS NULL "
                "OR text_source IN ('native_pdf', 'ocr'))"
            ),
            "text_quality": (
                "REAL CHECK(text_quality IS NULL OR text_quality BETWEEN 0 AND 1)"
            ),
            "extraction_error": "TEXT",
            "text_extractor_version": "TEXT",
        }
        for column, definition in page_additions.items():
            if column not in page_columns:
                connection.execute(f"ALTER TABLE pages ADD COLUMN {column} {definition}")
                changed = True
        if source_inventory_added:
            connection.execute("UPDATE documents SET page_inventory_complete = 0")
            changed = True
    records_exist = connection.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type = 'table' AND name = 'regulatory_records'"
    ).fetchone()
    if records_exist:
        record_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(regulatory_records)"
            ).fetchall()
        }
        additions = {
            "decision_uid": "TEXT NOT NULL DEFAULT ''",
            "numeral": "TEXT",
            "numeral_title": "TEXT",
            "session_date": "TEXT",
            "session_date_raw": "TEXT",
            "request_type_code": "TEXT",
            "identity_strategy": "TEXT",
        }
        for column, definition in additions.items():
            if column not in record_columns:
                connection.execute(
                    f"ALTER TABLE regulatory_records ADD COLUMN {column} {definition}"
                )
                changed = True
        if changed:
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_records_numeral "
                "ON regulatory_records(numeral)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_records_request_type "
                "ON regulatory_records(request_type_code)"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_records_decision_uid "
                "ON regulatory_records(decision_uid) WHERE decision_uid != ''"
            )
    if changed:
        connection.commit()


def connect(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    _ensure_legacy_columns(connection)
    return connection


def initialize_database(database_path: Path) -> None:
    with connect(database_path) as connection:
        try:
            connection.executescript(SCHEMA)
            connection.execute(
                "INSERT OR REPLACE INTO app_metadata (key, value) VALUES (?, ?)",
                ("schema_version", str(DATABASE_SCHEMA_VERSION)),
            )
        except sqlite3.OperationalError as exc:
            if "fts5" in str(exc).lower():
                raise RuntimeError(
                    "La instalación de SQLite no incluye soporte FTS5"
                ) from exc
            raise


def database_schema_version(database_path: Path) -> int:
    if not database_path.exists():
        return 0
    try:
        with sqlite3.connect(database_path) as connection:
            row = connection.execute(
                "SELECT value FROM app_metadata WHERE key = 'schema_version'"
            ).fetchone()
    except sqlite3.Error:
        return 0
    if not row:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def is_current_schema(database_path: Path) -> bool:
    return database_schema_version(database_path) == DATABASE_SCHEMA_VERSION


def table_exists(database_path: Path, table_name: str) -> bool:
    if not database_path.exists():
        return False
    with sqlite3.connect(database_path) as connection:
        return bool(
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,),
            ).fetchone()
        )


def _backfill_missing_decision_uids(connection: sqlite3.Connection) -> int:
    """Asigna UID deterministas a fichas heredadas que aún no tienen uno.

    La migración es el único momento en que se usa esta identidad provisional.
    Un reprocesamiento posterior puede reconciliarla contra la fuente, pero la
    base nunca queda en un estado publicable con UID vacíos.
    """

    record_columns = {
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info(regulatory_records)"
        ).fetchall()
    }
    if not {"id", "document_id", "record_key", "decision_uid"}.issubset(
        record_columns
    ):
        return 0
    document_columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(documents)").fetchall()
    }
    catalog_expression = (
        "d.catalog_id" if "catalog_id" in document_columns else "NULL"
    )
    rows = connection.execute(
        f"""
        SELECT r.id, r.document_id, r.record_key,
               {catalog_expression} AS catalog_id, d.manifest_url
        FROM regulatory_records r
        JOIN documents d ON d.id = r.document_id
        WHERE r.decision_uid IS NULL OR TRIM(r.decision_uid) = ''
        ORDER BY r.document_id, r.id
        """
    ).fetchall()
    reserved = {
        str(row[0])
        for row in connection.execute(
            "SELECT decision_uid FROM regulatory_records "
            "WHERE decision_uid IS NOT NULL AND TRIM(decision_uid) != ''"
        ).fetchall()
    }
    updated = 0
    for row in rows:
        record_id, document_id, record_key, catalog_id, manifest_url = row
        identity = str(catalog_id or manifest_url or document_id)
        salt = 0
        while True:
            payload = (
                f"migration-v6|{identity}|{record_key}|{record_id}|{salt}"
            )
            decision_uid = (
                "dec_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
            )
            if decision_uid not in reserved:
                break
            salt += 1
        if "identity_strategy" in record_columns:
            connection.execute(
                """
                UPDATE regulatory_records
                SET decision_uid = ?,
                    identity_strategy = COALESCE(
                        NULLIF(TRIM(identity_strategy), ''),
                        'migration_generated_v6'
                    )
                WHERE id = ?
                """,
                (decision_uid, record_id),
            )
        else:
            connection.execute(
                "UPDATE regulatory_records SET decision_uid = ? WHERE id = ?",
                (decision_uid, record_id),
            )
        reserved.add(decision_uid)
        updated += 1
    return updated


def migrate_database_schema(database_path: Path) -> bool:
    """Aplica migraciones aditivas v2-v5→v6 sobre una copia verificada."""
    version = database_schema_version(database_path)
    if version == DATABASE_SCHEMA_VERSION:
        return False
    if version > DATABASE_SCHEMA_VERSION:
        raise ValueError(
            f"La base usa el esquema {version}, superior al soportado "
            f"({DATABASE_SCHEMA_VERSION})"
        )
    if version not in {2, 3, 4, 5}:
        return False

    temporary_path = database_path.with_suffix(".schema-v6.db")
    temporary_path.unlink(missing_ok=True)
    shutil.copy2(database_path, temporary_path)
    tables_to_verify = ("documents", "pages", "chunks", "chunks_fts")
    try:
        with sqlite3.connect(temporary_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            before = {
                table: int(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
                for table in tables_to_verify
            }
            has_regulatory_records = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='regulatory_records'"
            ).fetchone()
            if has_regulatory_records:
                before["regulatory_records"] = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM regulatory_records"
                    ).fetchone()[0]
                )
                record_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(regulatory_records)"
                    ).fetchall()
                }
                if "end_page_number" not in record_columns:
                    connection.execute(
                        "ALTER TABLE regulatory_records ADD COLUMN "
                        "end_page_number INTEGER NOT NULL DEFAULT 1"
                    )
                    connection.execute(
                        "UPDATE regulatory_records "
                        "SET end_page_number = page_number"
                    )
                additions = {
                    "decision_uid": "TEXT NOT NULL DEFAULT ''",
                    "numeral": "TEXT",
                    "numeral_title": "TEXT",
                    "session_date": "TEXT",
                    "session_date_raw": "TEXT",
                    "request_type_code": "TEXT",
                    "identity_strategy": "TEXT",
                }
                for column, definition in additions.items():
                    if column not in record_columns:
                        connection.execute(
                            "ALTER TABLE regulatory_records ADD COLUMN "
                            f"{column} {definition}"
                        )
                _backfill_missing_decision_uids(connection)
                id_column = next(
                    (
                        row
                        for row in connection.execute(
                            "PRAGMA table_info(regulatory_records)"
                        ).fetchall()
                        if row[1] == "id"
                    ),
                    None,
                )
                if id_column is not None and not int(id_column[5]):
                    # Algunas bases v3 de prueba se generaron mediante CTAS y
                    # perdieron la marca PRIMARY KEY. Un índice UNIQUE mantiene
                    # sus identificadores y permite la FK aditiva de evidencias.
                    connection.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS "
                        "idx_records_id_legacy_unique ON regulatory_records(id)"
                    )
            document_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(documents)").fetchall()
            }
            for column, definition in {
                "catalog_id": "TEXT",
                "publication_date": "TEXT",
                "page_inventory_complete": "INTEGER NOT NULL DEFAULT 0",
            }.items():
                if column not in document_columns:
                    connection.execute(
                        f"ALTER TABLE documents ADD COLUMN {column} {definition}"
                    )
            page_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(pages)").fetchall()
            }
            source_inventory_added = any(
                column not in page_columns
                for column in (
                    "raw_text_compressed",
                    "raw_text_codec",
                    "text_source",
                    "text_quality",
                    "extraction_error",
                    "text_extractor_version",
                )
            )
            page_additions = {
                "raw_text_compressed": "BLOB",
                "raw_text_codec": "TEXT",
                "text_source": (
                    "TEXT CHECK(text_source IS NULL "
                    "OR text_source IN ('native_pdf', 'ocr'))"
                ),
                "text_quality": (
                    "REAL CHECK(text_quality IS NULL OR text_quality BETWEEN 0 AND 1)"
                ),
                "extraction_error": "TEXT",
                "text_extractor_version": "TEXT",
            }
            for column, definition in page_additions.items():
                if column not in page_columns:
                    connection.execute(
                        f"ALTER TABLE pages ADD COLUMN {column} {definition}"
                    )
            if source_inventory_added:
                connection.execute(
                    "UPDATE documents SET page_inventory_complete = 0"
                )
            connection.executescript(FEATURE_SCHEMA)
            connection.execute(
                "INSERT OR REPLACE INTO app_metadata (key, value) VALUES (?, ?)",
                ("schema_version", str(DATABASE_SCHEMA_VERSION)),
            )
            integrity = str(
                connection.execute("PRAGMA integrity_check").fetchone()[0]
            )
            foreign_key_errors = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            after = {
                table: int(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
                for table in tables_to_verify
            }
            if has_regulatory_records:
                after["regulatory_records"] = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM regulatory_records"
                    ).fetchone()[0]
                )
            if integrity.lower() != "ok" or foreign_key_errors or before != after:
                raise RuntimeError(
                    "La verificación de la migración aditiva no fue satisfactoria"
                )
        temporary_path.replace(database_path)
        return True
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def clear_database(database_path: Path) -> None:
    with connect(database_path) as connection:
        fts_sql_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'chunks_fts'"
        ).fetchone()
        fts_sql = str(fts_sql_row[0]).lower() if fts_sql_row else ""
        if "content=''" in fts_sql or 'content=""' in fts_sql:
            connection.execute(
                "INSERT INTO chunks_fts(chunks_fts) VALUES('delete-all')"
            )
        else:
            connection.execute("DELETE FROM chunks_fts")
        connection.execute("DELETE FROM documents")


def compress_page_text(text: str) -> bytes:
    """Codifica texto UTF-8 con un formato versionado y sin normalizarlo."""

    return zlib.compress(str(text).encode("utf-8"), level=6)


def decompress_page_text(payload: bytes, codec: str) -> str:
    """Recupera exactamente el texto fuente almacenado en una página."""

    if codec != PAGE_TEXT_CODEC:
        raise ValueError(f"Códec de texto fuente no soportado: {codec}")
    try:
        return zlib.decompress(bytes(payload)).decode("utf-8")
    except (TypeError, ValueError, UnicodeDecodeError, zlib.error) as exc:
        raise ValueError("El texto fuente comprimido está dañado") from exc


def _page_source_values(
    page: dict,
) -> tuple[
    bytes | None,
    str | None,
    str | None,
    float | None,
    str | None,
    str | None,
]:
    raw_value = page.get("text")
    if raw_value is None:
        source = page.get("text_source")
        if source not in (None, ""):
            raise ValueError("Una página sin texto no puede declarar text_source")
        quality_value = page.get("text_quality")
        quality = float(quality_value) if quality_value is not None else None
        if quality is not None and not 0 <= quality <= 1:
            raise ValueError("La calidad del texto debe estar entre 0 y 1")
        error_value = page.get("extraction_error")
        error = str(error_value)[:2000] if error_value else None
        version_value = page.get("text_extractor_version")
        extractor_version = (
            str(version_value).strip()[:200]
            if version_value is not None
            else None
        )
        return None, None, None, quality, error, extractor_version or None
    raw_text = str(raw_value)
    source = page.get("text_source")
    if source is None:
        source = "ocr" if page.get("ocr_used") else "native_pdf"
    source = str(source)
    if source not in PAGE_TEXT_SOURCES:
        raise ValueError("La fuente del texto debe ser native_pdf u ocr")

    quality_value = page.get("text_quality")
    quality = float(quality_value) if quality_value is not None else None
    if quality is not None and not 0 <= quality <= 1:
        raise ValueError("La calidad del texto debe estar entre 0 y 1")
    error_value = page.get("extraction_error")
    error = str(error_value)[:2000] if error_value else None
    extractor_version = str(
        page.get("text_extractor_version") or PAGE_TEXT_EXTRACTOR_VERSION
    ).strip()
    if not extractor_version:
        raise ValueError("La versión del extractor de texto es obligatoria")
    return (
        compress_page_text(raw_text),
        PAGE_TEXT_CODEC,
        source,
        quality,
        error,
        extractor_version,
    )


def source_pages_for_document(
    database_path: Path,
    document_id: int,
    *,
    legacy_fallback: bool = True,
) -> list[dict]:
    """Devuelve páginas para reextracción, prefiriendo el texto fuente v0.7.

    Una página migrada desde v0.6 puede no tener el BLOB. En ese caso se
    reconstruye desde los fragmentos para mantener compatibilidad, pero queda
    marcada como ``legacy_chunks`` y nunca se confunde con la fuente original.
    """

    with connect(database_path) as connection:
        page_rows = connection.execute(
            """
            SELECT id, page_number, raw_text_compressed, raw_text_codec,
                   text_source, text_quality, extraction_error,
                   text_extractor_version
            FROM pages
            WHERE document_id = ?
            ORDER BY page_number
            """,
            (int(document_id),),
        ).fetchall()
        output: list[dict] = []
        for row in page_rows:
            text: str | None = None
            source = row["text_source"]
            raw_error: str | None = None
            if row["raw_text_compressed"] is not None:
                try:
                    text = decompress_page_text(
                        row["raw_text_compressed"], str(row["raw_text_codec"] or "")
                    )
                except ValueError as exc:
                    raw_error = str(exc)
            if text is None and legacy_fallback:
                chunks = connection.execute(
                    """
                    SELECT text FROM chunks
                    WHERE page_id = ?
                    ORDER BY chunk_index
                    """,
                    (int(row["id"]),),
                ).fetchall()
                if chunks:
                    text = _merge_overlapping_chunks(
                        str(chunk["text"]) for chunk in chunks
                    )
                    source = "legacy_chunks"
            if text is None:
                continue
            output.append(
                {
                    "page": int(row["page_number"]),
                    "text": text,
                    "text_source": source,
                    "text_quality": row["text_quality"],
                    "extraction_error": raw_error or row["extraction_error"],
                    "text_extractor_version": row["text_extractor_version"],
                }
            )
    return output


def insert_document(
    database_path: Path,
    metadata: DocumentMetadata,
    document_hash: str,
    pages: Iterable[dict],
    *,
    manifest_url: str | None = None,
    pdf_page_count: int | None = None,
    possible_scans: Iterable[int] = (),
    page_inventory_complete: bool = True,
) -> int:
    indexed_at = datetime.now(timezone.utc).isoformat()
    indexed_pages = list(pages)
    scan_pages = [int(page) for page in possible_scans]
    manifest_url = manifest_url or metadata.url
    pdf_page_count = (
        int(pdf_page_count)
        if pdf_page_count is not None
        else len(
            {
                *(int(page["page"]) for page in indexed_pages),
                *scan_pages,
            }
        )
    )
    consultable_page_count = sum(
        bool(page.get("chunks")) for page in indexed_pages
    )

    with connect(database_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO documents (
                title, normalized_title, url, manifest_url, year, acta_number,
                section, part, source_type, catalog_id, publication_date,
                document_hash, pdf_page_count,
                indexed_page_count, ocr_candidate_pages, page_inventory_complete,
                indexed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                metadata.title,
                normalize_text(metadata.title),
                metadata.url,
                manifest_url,
                metadata.year,
                metadata.acta_number,
                metadata.section,
                metadata.part,
                metadata.source_type,
                metadata.catalog_id,
                metadata.publication_date,
                document_hash,
                pdf_page_count,
                consultable_page_count,
                ",".join(str(page) for page in scan_pages),
                int(page_inventory_complete),
                indexed_at,
            ),
        )
        document_id = int(cursor.lastrowid)

        for page in indexed_pages:
            (
                raw_text_compressed,
                raw_text_codec,
                text_source,
                text_quality,
                extraction_error,
                text_extractor_version,
            ) = _page_source_values(page)
            page_cursor = connection.execute(
                """
                INSERT INTO pages (
                    document_id, page_number, raw_text_compressed, raw_text_codec,
                    text_source, text_quality, extraction_error,
                    text_extractor_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id,
                    int(page["page"]),
                    raw_text_compressed,
                    raw_text_codec,
                    text_source,
                    text_quality,
                    extraction_error,
                    text_extractor_version,
                ),
            )
            page_id = int(page_cursor.lastrowid)
            for chunk_index, chunk in enumerate(page.get("chunks", [])):
                chunk_cursor = connection.execute(
                    """
                    INSERT INTO chunks (page_id, chunk_index, text)
                    VALUES (?, ?, ?)
                    """,
                    (page_id, chunk_index, chunk),
                )
                chunk_id = int(chunk_cursor.lastrowid)
                connection.execute(
                    "INSERT INTO chunks_fts (rowid, title, text) VALUES (?, ?, ?)",
                    (chunk_id, metadata.title, chunk),
                )
    return document_id


def _normalized_identifier(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", normalize_text(value or ""))


def _regulatory_record_key(record: RegulatoryRecord) -> str:
    # Contrato histórico de v0.6. Los campos enriquecidos/evidencias de v0.7
    # se auditan aparte y no deben invalidar una revisión por sí solos.
    historical_payload = {
        "producto": record.producto,
        "principio_activo": record.principio_activo,
        "interesado": record.interesado,
        "expediente": record.expediente,
        "radicado": record.radicado,
        "solicitud": record.solicitud,
        "concepto": record.concepto,
        "resultado_normalizado": record.resultado_normalizado,
        "pagina": record.pagina,
        "pagina_final": record.pagina_final,
        "numeral": record.numeral,
        "titulo_numeral": record.titulo_numeral,
        "fecha_sesion": record.fecha_sesion,
        "fecha_sesion_original": record.fecha_sesion_original,
        "tipo_solicitud": record.tipo_solicitud,
    }
    payload = json.dumps(
        historical_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _decision_locator(record: RegulatoryRecord) -> tuple[str, str]:
    """Devuelve una identidad estable y la estrategia usada para construirla."""

    numeral = normalize_text(record.numeral or "")
    radicado = _normalized_identifier(record.radicado)
    expediente = _normalized_identifier(record.expediente)
    product = normalize_text(record.producto or "")
    if numeral and radicado:
        return f"numeral:{numeral}|radicado:{radicado}", "numeral_radicado"
    if numeral and expediente:
        return f"numeral:{numeral}|expediente:{expediente}", "numeral_expediente"
    if radicado:
        return f"radicado:{radicado}", "radicado"
    if expediente and product:
        return (
            f"expediente:{expediente}|producto:{product}",
            "expediente_producto",
        )
    if numeral:
        return f"numeral:{numeral}", "numeral"
    return f"pagina:{record.pagina}", "pagina_ordinal"


def _decision_uid(
    document_identity: str,
    record: RegulatoryRecord,
    occurrence: int,
) -> tuple[str, str]:
    locator, strategy = _decision_locator(record)
    payload = f"{document_identity}|{locator}|ocurrencia:{occurrence}"
    return "dec_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32], strategy


def _record_text_signature(request_text: str | None, concept_text: str | None) -> str:
    return normalize_text(f"{request_text or ''} {concept_text or ''}")


def _unique_old_uid(
    old_records: list[dict],
    unused_uids: set[str],
    key: str,
    value: str,
) -> tuple[str | None, bool]:
    if not value:
        return None, False
    matches = [
        item["decision_uid"]
        for item in old_records
        if item["decision_uid"] in unused_uids and item[key] == value
    ]
    return (matches[0], False) if len(matches) == 1 else (None, len(matches) > 1)


def _reconcile_decision_uid(
    record: RegulatoryRecord,
    record_key: str,
    old_records: list[dict],
    unused_uids: set[str],
) -> tuple[str | None, bool]:
    """Conserva un UID anterior solo cuando la correspondencia es inequívoca."""

    exact = [
        item["decision_uid"]
        for item in old_records
        if item["decision_uid"] in unused_uids and item["record_key"] == record_key
    ]
    if len(exact) == 1:
        return exact[0], False

    ambiguous = len(exact) > 1
    identifiers = (
        ("radicado", _normalized_identifier(record.radicado)),
        ("expediente", _normalized_identifier(record.expediente)),
        ("numeral", normalize_text(record.numeral or "")),
    )
    for key, value in identifiers:
        uid, multiple = _unique_old_uid(old_records, unused_uids, key, value)
        ambiguous = ambiguous or multiple
        if uid:
            return uid, False

    new_text = _record_text_signature(record.solicitud, record.concepto)
    if new_text:
        candidates: list[tuple[float, str]] = []
        new_end_page = int(record.pagina_final or record.pagina)
        for item in old_records:
            uid = item["decision_uid"]
            if uid not in unused_uids:
                continue
            overlaps = not (
                new_end_page < item["page_number"]
                or record.pagina > item["end_page_number"]
            )
            if not overlaps or not item["text_signature"]:
                continue
            similarity = SequenceMatcher(
                None, new_text, item["text_signature"], autojunk=False
            ).ratio()
            if similarity >= 0.85:
                candidates.append((similarity, uid))
        candidates.sort(reverse=True)
        if len(candidates) == 1 or (
            len(candidates) > 1 and candidates[0][0] - candidates[1][0] >= 0.08
        ):
            return candidates[0][1], False
        ambiguous = ambiguous or bool(candidates)
    return None, ambiguous


def _merge_overlapping_chunks(chunks: Iterable[str], maximum_overlap: int = 400) -> str:
    """Reconstruye una página sin duplicar el solapamiento de sus fragmentos."""

    values = [str(value) for value in chunks if str(value)]
    if not values:
        return ""
    merged = values[0]
    for value in values[1:]:
        limit = min(maximum_overlap, len(merged), len(value))
        overlap = 0
        for size in range(limit, 19, -1):
            if merged[-size:] == value[:size]:
                overlap = size
                break
        separator = "" if overlap else "\n"
        merged += separator + value[overlap:]
    return merged


def _store_regulatory_records(
    connection: sqlite3.Connection,
    document_id: int,
    document_hash: str,
    records: Iterable[RegulatoryRecord],
    *,
    extraction_method: str,
    extractor_version: str,
) -> dict[str, int]:
    values = list(records)
    now = datetime.now(timezone.utc).isoformat()
    document = connection.execute(
        "SELECT manifest_url, catalog_id FROM documents WHERE id = ?",
        (document_id,),
    ).fetchone()
    document_identity = (
        str(document["catalog_id"] or document["manifest_url"])
        if document
        else str(document_id)
    )
    previous_rows = connection.execute(
        """
        SELECT record_key, decision_uid, page_number, end_page_number, numeral,
               normalized_expediente, normalized_radicado, request_text,
               concept_text
        FROM regulatory_records
        WHERE document_id = ? AND decision_uid != ''
        ORDER BY page_number, id
        """,
        (document_id,),
    ).fetchall()
    old_records = [
        {
            "record_key": str(row["record_key"]),
            "decision_uid": str(row["decision_uid"]),
            "page_number": int(row["page_number"]),
            "end_page_number": int(row["end_page_number"]),
            "numeral": normalize_text(row["numeral"] or ""),
            "expediente": str(row["normalized_expediente"] or ""),
            "radicado": str(row["normalized_radicado"] or ""),
            "text_signature": _record_text_signature(
                row["request_text"], row["concept_text"]
            ),
        }
        for row in previous_rows
    ]
    unused_uids = {item["decision_uid"] for item in old_records}
    connection.execute(
        "DELETE FROM regulatory_records WHERE document_id = ?",
        (document_id,),
    )
    locator_counts: dict[str, int] = {}
    reconciled = 0
    generated = 0
    ambiguous = 0
    assigned_uids: set[str] = set()
    for record in values:
        record_key = _regulatory_record_key(record)
        locator, _ = _decision_locator(record)
        locator_counts[locator] = locator_counts.get(locator, 0) + 1
        decision_uid, was_ambiguous = _reconcile_decision_uid(
            record, record_key, old_records, unused_uids
        )
        if decision_uid:
            identity_strategy = "reconciled_v6"
            unused_uids.discard(decision_uid)
            reconciled += 1
        else:
            if was_ambiguous:
                ambiguous += 1
            occurrence = locator_counts[locator]
            decision_uid, identity_strategy = _decision_uid(
                document_identity, record, occurrence
            )
            while decision_uid in assigned_uids:
                occurrence += 1
                decision_uid, identity_strategy = _decision_uid(
                    document_identity, record, occurrence
                )
            generated += 1
        assigned_uids.add(decision_uid)
        populated = sum(
            bool(value)
            for value in (
                record.producto,
                record.principio_activo,
                record.interesado,
                record.expediente,
                record.radicado,
                record.solicitud,
                record.concepto,
            )
        )
        confidence = min(
            0.95,
            0.45
            + (populated * 0.06)
            + (0.05 if record.resultado_normalizado != "sin_clasificar" else 0.0),
        )
        connection.execute(
            """
            INSERT INTO regulatory_records (
                document_id, record_key, decision_uid, page_number,
                end_page_number, numeral, numeral_title, session_date,
                session_date_raw, request_type_code, identity_strategy, product_name,
                normalized_product_name, active_ingredient,
                normalized_active_ingredient, interested_party,
                normalized_interested_party, expediente,
                normalized_expediente, radicado, normalized_radicado,
                request_text, concept_text, outcome_code, extraction_method,
                confidence, needs_review, extractor_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                document_id,
                record_key,
                decision_uid,
                record.pagina,
                record.pagina_final or record.pagina,
                record.numeral,
                record.titulo_numeral,
                record.fecha_sesion,
                record.fecha_sesion_original,
                record.tipo_solicitud,
                identity_strategy,
                record.producto,
                normalize_text(record.producto or ""),
                record.principio_activo,
                normalize_text(record.principio_activo or ""),
                record.interesado,
                normalize_text(record.interesado or ""),
                record.expediente,
                _normalized_identifier(record.expediente),
                record.radicado,
                _normalized_identifier(record.radicado),
                record.solicitud,
                record.concepto,
                record.resultado_normalizado,
                extraction_method,
                confidence,
                extractor_version,
                now,
            ),
        )
        record_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        # Defensa adicional para registros construidos por importadores o
        # pruebas externas: nunca delegamos a SQLite la resolucion de dos
        # evidencias validas que llegaron con el mismo ordinal local.
        field_evidences = normalize_field_evidence_ordinals(
            getattr(record, "evidencias_campos", ())
        )
        for evidence in field_evidences:
            connection.execute(
                """
                INSERT INTO regulatory_field_evidence (
                    record_id, field_name, ordinal, literal_value,
                    normalized_value, canonical_value, page_number,
                    end_page_number, evidence_text, extraction_method, confidence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    evidence.campo,
                    int(evidence.ordinal),
                    evidence.valor_literal,
                    evidence.valor_normalizado,
                    evidence.valor_canonico,
                    int(evidence.pagina),
                    int(evidence.pagina_final),
                    evidence.fragmento,
                    evidence.metodo,
                    float(evidence.confianza),
                ),
            )
    connection.execute(
        """
        INSERT OR REPLACE INTO document_extractions (
            document_id, document_hash, extractor_version, status,
            record_count, error_message, extracted_at
        ) VALUES (?, ?, ?, 'complete', ?, NULL, ?)
        """,
        (document_id, document_hash, extractor_version, len(values), now),
    )
    return {
        "records": len(values),
        "uids_reconciled": reconciled,
        "uids_generated": generated,
        "uids_ambiguous": ambiguous,
        "uids_orphaned": len(unused_uids),
    }


def sync_regulatory_extractions(
    database_path: Path,
    *,
    extractor_version: str = REGULATORY_EXTRACTOR_VERSION,
) -> dict:
    """Extrae campos pendientes sin descargar nuevamente los PDF.

    Los contadores ``uids_*`` describen la reconciliación de *registros* de la
    extracción previa. En particular, ``uids_orphaned`` no significa por sí
    solo que una revisión humana haya quedado huérfana: el publicador debe
    cruzar esos UID con el log de revisiones antes de decidir si bloquea.
    """
    summary = {
        "documents_total": 0,
        "documents_processed": 0,
        "documents_skipped": 0,
        "documents_failed": 0,
        "records_extracted": 0,
        "uids_reconciled": 0,
        "uids_generated": 0,
        "uids_ambiguous": 0,
        "uids_orphaned": 0,
        "errors": [],
    }
    if not database_path.exists() or not table_exists(
        database_path, "regulatory_records"
    ):
        return summary

    with connect(database_path) as connection:
        documents = connection.execute(
            """
            SELECT d.id, d.title, d.document_hash, e.document_hash AS prior_hash,
                   e.extractor_version AS prior_version, e.status AS prior_status,
                   (SELECT COUNT(*)
                    FROM regulatory_records r
                    WHERE r.document_id = d.id
                      AND (r.decision_uid IS NULL OR TRIM(r.decision_uid) = ''))
                       AS records_without_uid
            FROM documents d
            LEFT JOIN document_extractions e ON e.document_id = d.id
            ORDER BY d.id
            """
        ).fetchall()
        summary["documents_total"] = len(documents)
        for document in documents:
            if (
                document["prior_hash"] == document["document_hash"]
                and document["prior_version"] == extractor_version
                and document["prior_status"] == "complete"
                and int(document["records_without_uid"] or 0) == 0
            ):
                summary["documents_skipped"] += 1
                continue
            connection.execute("SAVEPOINT regulatory_document")
            try:
                rows = connection.execute(
                    """
                    SELECT p.id AS page_id, p.page_number,
                           p.raw_text_compressed, p.raw_text_codec,
                           p.text_source, p.text_quality, p.extraction_error,
                           p.text_extractor_version, c.text
                    FROM pages p
                    LEFT JOIN chunks c ON c.page_id = p.id
                    WHERE p.document_id = ?
                    ORDER BY p.page_number, c.chunk_index
                    """,
                    (document["id"],),
                ).fetchall()
                page_values: dict[int, dict] = {}
                for row in rows:
                    page_number = int(row["page_number"])
                    value = page_values.setdefault(
                        page_number,
                        {
                            "raw": row["raw_text_compressed"],
                            "codec": row["raw_text_codec"],
                            "chunks": [],
                        },
                    )
                    if row["text"] is not None:
                        value["chunks"].append(str(row["text"]))
                pages: list[dict] = []
                used_raw_text = 0
                used_legacy_chunks = 0
                for page_number, value in sorted(page_values.items()):
                    text: str | None = None
                    if value["raw"] is not None:
                        try:
                            text = decompress_page_text(
                                value["raw"], str(value["codec"] or "")
                            )
                        except ValueError:
                            text = None
                    if text is not None:
                        used_raw_text += 1
                    else:
                        text = _merge_overlapping_chunks(value["chunks"])
                        if text:
                            used_legacy_chunks += 1
                    if text:
                        pages.append({"page": page_number, "text": text})
                records = extract_regulatory_records(pages)
                if used_raw_text and not used_legacy_chunks:
                    extraction_method = "deterministic_source_text"
                elif used_raw_text:
                    extraction_method = "deterministic_mixed_source"
                else:
                    extraction_method = "deterministic_chunk_backfill"
                stored = _store_regulatory_records(
                    connection,
                    int(document["id"]),
                    str(document["document_hash"]),
                    records,
                    extraction_method=extraction_method,
                    extractor_version=extractor_version,
                )
                summary["documents_processed"] += 1
                summary["records_extracted"] += stored["records"]
                for key in UID_RECONCILIATION_KEYS:
                    summary[key] += stored[key]
                connection.execute("RELEASE SAVEPOINT regulatory_document")
            except Exception as exc:
                connection.execute("ROLLBACK TO SAVEPOINT regulatory_document")
                connection.execute("RELEASE SAVEPOINT regulatory_document")
                summary["documents_failed"] += 1
                detail = f"{document['title']}: {exc}"
                summary["errors"].append(detail)
                connection.execute(
                    """
                    INSERT OR REPLACE INTO document_extractions (
                        document_id, document_hash, extractor_version, status,
                        record_count, error_message, extracted_at
                    ) VALUES (?, ?, ?, 'error', 0, ?, ?)
                    """,
                    (
                        document["id"],
                        document["document_hash"],
                        extractor_version,
                        str(exc)[:1000],
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
    return summary


def extraction_uid_reconciliation(summary: dict) -> dict[str, int]:
    """Extrae el contrato estable de reconciliación para informes/workflows.

    El resultado debe combinarse con ``regulatory-review-log.csv`` para crear
    métricas de revisiones reconciliadas, ambiguas o huérfanas.
    """

    return {key: int(summary.get(key, 0) or 0) for key in UID_RECONCILIATION_KEYS}


def _database_documents(connection: sqlite3.Connection) -> list[dict]:
    rows = connection.execute(
        """
        SELECT id, catalog_id, manifest_url, title, year, acta_number,
               section, part, document_hash
        FROM documents
        ORDER BY id
        """
    ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "catalog_id": str(row["catalog_id"] or ""),
            "manifest_url": str(row["manifest_url"] or ""),
            "document_hash": str(row["document_hash"] or ""),
            "signature": (
                int(row["year"]) if row["year"] is not None else None,
                normalize_text(row["acta_number"] or ""),
                normalize_text(row["section"] or ""),
                normalize_text(row["part"] or ""),
            ),
            "title": str(row["title"]),
        }
        for row in rows
    ]


def _database_identity_records(
    connection: sqlite3.Connection,
    document_id: int,
) -> list[dict]:
    rows = connection.execute(
        """
        SELECT id, record_key, decision_uid, page_number, end_page_number,
               numeral, normalized_expediente, normalized_radicado,
               normalized_product_name, normalized_active_ingredient,
               normalized_interested_party, request_type_code, outcome_code,
               request_text, concept_text
        FROM regulatory_records
        WHERE document_id = ?
        ORDER BY page_number, id
        """,
        (document_id,),
    ).fetchall()
    records: list[dict] = []
    for ordinal, row in enumerate(rows):
        expediente = str(row["normalized_expediente"] or "")
        radicado = str(row["normalized_radicado"] or "")
        page_number = int(row["page_number"])
        end_page_number = int(row["end_page_number"])
        radicado_prefix_match = re.match(r"^(?:19|20)\d{8}", radicado)
        radicado_prefix = (
            radicado_prefix_match.group(0) if radicado_prefix_match else ""
        )
        numeral = normalize_text(row["numeral"] or "")
        stable_numeral = (
            numeral if re.fullmatch(r"\d+(?:\.\d+)+", numeral) else ""
        )
        text_signature = _record_text_signature(
            row["request_text"], row["concept_text"]
        )
        record = {
            "id": int(row["id"]),
            "record_key": str(row["record_key"]),
            "decision_uid": str(row["decision_uid"] or ""),
            "page_number": page_number,
            "end_page_number": end_page_number,
            "page_span": (page_number, end_page_number),
            "numeral": numeral,
            "stable_numeral": stable_numeral,
            "expediente": expediente,
            "radicado": radicado,
            # Algunos PDF históricos pegan el pie de página al radicado. Un
            # prefijo de diez dígitos (año + consecutivo) permite compararlo
            # sin modificar el valor regulatorio que ve el usuario.
            "radicado_prefix": radicado_prefix,
            "radicado_expediente": (
                (radicado, expediente) if radicado and expediente else None
            ),
            "radicado_page": (
                (radicado, page_number) if radicado else None
            ),
            "radicado_prefix_page": (
                (radicado_prefix, page_number) if radicado_prefix else None
            ),
            "expediente_page": (
                (expediente, page_number) if expediente else None
            ),
            "product": str(row["normalized_product_name"] or ""),
            "active_ingredient": str(
                row["normalized_active_ingredient"] or ""
            ),
            "interested_party": str(row["normalized_interested_party"] or ""),
            "request_type": str(row["request_type_code"] or ""),
            "outcome": str(row["outcome_code"] or ""),
            "ordinal": ordinal,
            "text_signature": text_signature,
            "stable_text_signature": (
                text_signature if len(text_signature) >= 80 else ""
            ),
        }
        records.append(record)
    return records


def _match_unique_groups(
    candidate_items: list[dict],
    baseline_items: list[dict],
    candidate_unused: set[int],
    baseline_unused: set[int],
    field: str,
) -> list[tuple[int, int]]:
    candidate_groups: dict[object, list[int]] = {}
    baseline_groups: dict[object, list[int]] = {}
    for item in candidate_items:
        value = item[field]
        if item["id"] in candidate_unused and value not in (None, "", (None, "", "", "")):
            candidate_groups.setdefault(value, []).append(item["id"])
    for item in baseline_items:
        value = item[field]
        if item["id"] in baseline_unused and value not in (None, "", (None, "", "", "")):
            baseline_groups.setdefault(value, []).append(item["id"])
    return [
        (candidate_ids[0], baseline_groups[value][0])
        for value, candidate_ids in candidate_groups.items()
        if len(candidate_ids) == 1
        and len(baseline_groups.get(value, ())) == 1
    ]


def _record_match_has_evidence(candidate: dict, baseline: dict) -> bool:
    if any(
        candidate[field]
        and candidate[field] == baseline[field]
        for field in (
            "radicado",
            "radicado_prefix",
            "expediente",
            "stable_numeral",
        )
    ):
        return True
    overlaps = not (
        candidate["end_page_number"] < baseline["page_number"]
        or candidate["page_number"] > baseline["end_page_number"]
    )
    if not overlaps or not candidate["text_signature"] or not baseline["text_signature"]:
        return False
    return (
        SequenceMatcher(
            None,
            candidate["text_signature"],
            baseline["text_signature"],
            autojunk=False,
        ).ratio()
        >= 0.85
    )


def _page_ranges_overlap(candidate: dict, baseline: dict) -> bool:
    return not (
        candidate["end_page_number"] < baseline["page_number"]
        or candidate["page_number"] > baseline["end_page_number"]
    )


def _record_link_score(candidate: dict, baseline: dict) -> float | None:
    """Puntúa enlaces conservadores entre dos extracciones del mismo PDF.

    La página o el orden nunca bastan por sí solos. Se exige además un
    identificador coincidente, texto sustancialmente semejante o dos campos
    regulatorios concordantes. Así se aprovecha la geometría estable del PDF
    sin convertir la cercanía accidental en identidad.
    """

    page_overlap = _page_ranges_overlap(candidate, baseline)
    page_distance = abs(candidate["page_number"] - baseline["page_number"])
    same_span = candidate["page_span"] == baseline["page_span"]
    close_pages = page_overlap or page_distance <= 1
    if not close_pages:
        return None
    same_radicado = bool(
        candidate["radicado"]
        and candidate["radicado"] == baseline["radicado"]
    )
    same_radicado_prefix = bool(
        candidate["radicado_prefix"]
        and candidate["radicado_prefix"] == baseline["radicado_prefix"]
    )
    same_expediente = bool(
        candidate["expediente"]
        and candidate["expediente"] == baseline["expediente"]
    )
    same_numeral = bool(
        candidate["stable_numeral"]
        and candidate["stable_numeral"] == baseline["stable_numeral"]
    )
    matching_fields = sum(
        bool(candidate[field] and candidate[field] == baseline[field])
        for field in (
            "product",
            "active_ingredient",
            "interested_party",
        )
    )

    candidate_text = candidate["text_signature"]
    baseline_text = baseline["text_signature"]
    text_similarity = 0.0
    text_containment = False
    substantial_text = min(len(candidate_text), len(baseline_text)) >= 80
    if substantial_text:
        text_similarity = SequenceMatcher(
            None, candidate_text, baseline_text, autojunk=False
        ).ratio()
        shorter, longer = sorted((candidate_text, baseline_text), key=len)
        text_containment = len(shorter) >= 80 and shorter in longer

    eligible = any(
        (
            close_pages
            and (same_radicado or same_radicado_prefix or same_expediente),
            close_pages and (text_similarity >= 0.85 or text_containment),
            same_span and substantial_text and text_similarity >= 0.70,
            close_pages and matching_fields >= 2,
            close_pages and matching_fields >= 1 and text_similarity >= 0.55,
            close_pages and same_numeral and matching_fields >= 1,
        )
    )
    if not eligible:
        return None

    score = 0.0
    score += 320.0 if same_radicado else 0.0
    score += 260.0 if same_radicado_prefix and not same_radicado else 0.0
    score += 250.0 if same_expediente else 0.0
    score += 180.0 if same_numeral else 0.0
    score += 110.0 if candidate["page_number"] == baseline["page_number"] else 0.0
    score += 45.0 if same_span else 0.0
    score += 65.0 if page_overlap else max(0.0, 25.0 - (page_distance * 10.0))
    score += text_similarity * 180.0
    score += 50.0 if text_containment else 0.0
    score += matching_fields * 30.0
    # El orden solo desempata evidencia ya suficiente; no habilita un enlace.
    score += max(0.0, 12.0 - abs(candidate["ordinal"] - baseline["ordinal"]))
    return score


def _mutual_scored_matches(
    candidate_records: list[dict],
    baseline_records: list[dict],
    candidate_unused: set[int],
    baseline_unused: set[int],
) -> list[tuple[int, int]]:
    """Devuelve mejores coincidencias mutuas con margen inequívoco."""

    candidate_by_id = {record["id"]: record for record in candidate_records}
    baseline_by_id = {record["id"]: record for record in baseline_records}
    pairs: list[tuple[float, int, int]] = []
    for candidate_id in candidate_unused:
        candidate = candidate_by_id[candidate_id]
        for baseline_id in baseline_unused:
            baseline = baseline_by_id[baseline_id]
            score = _record_link_score(candidate, baseline)
            if score is not None:
                pairs.append((score, candidate_id, baseline_id))

    by_candidate: dict[int, list[tuple[float, int]]] = {}
    by_baseline: dict[int, list[tuple[float, int]]] = {}
    for score, candidate_id, baseline_id in pairs:
        by_candidate.setdefault(candidate_id, []).append((score, baseline_id))
        by_baseline.setdefault(baseline_id, []).append((score, candidate_id))
    for values in (*by_candidate.values(), *by_baseline.values()):
        values.sort(reverse=True)

    matches: list[tuple[float, int, int]] = []
    for candidate_id, options in by_candidate.items():
        score, baseline_id = options[0]
        reverse = by_baseline.get(baseline_id, [])
        if not reverse or reverse[0][1] != candidate_id:
            continue
        candidate_clear = len(options) == 1 or score - options[1][0] >= 35.0
        baseline_clear = len(reverse) == 1 or score - reverse[1][0] >= 35.0
        if candidate_clear and baseline_clear:
            matches.append((score, candidate_id, baseline_id))
    matches.sort(reverse=True)
    return [(candidate_id, baseline_id) for _, candidate_id, baseline_id in matches]


def _fresh_candidate_uid(
    record: dict,
    document_id: int,
    reserved: set[str],
) -> str:
    salt = 0
    while True:
        payload = (
            f"candidate-v6|{document_id}|{record['id']}|"
            f"{record['record_key']}|{salt}"
        )
        value = "dec_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
        if value not in reserved:
            return value
        salt += 1


def reconcile_database_decision_uids(
    baseline_path: Path,
    candidate_path: Path,
    *,
    protected_decision_uids: Iterable[str] | None = None,
) -> dict:
    """Reconcilia UID de una base reconstruida contra la última base publicada.

    Solo acepta correspondencias uno-a-uno. La actualización de la candidata es
    atómica y nunca modifica ``record_key``; su mapa permite que el gate detecte
    revisiones cuyo contenido fuente cambió aunque el UID permanezca estable.

    Si ``protected_decision_uids`` se proporciona, una coincidencia dudosa solo
    se conserva como ambigua cuando podría afectar uno de esos UID (por ejemplo,
    una revisión humana). Las demás se resuelven explícitamente como identidad
    nueva: es más seguro perder continuidad histórica no revisada que heredar el
    UID de otra decisión. Omitir el argumento mantiene el modo estricto útil para
    auditorías y compatibilidad con llamadas existentes.
    """

    baseline_path = Path(baseline_path)
    candidate_path = Path(candidate_path)
    if baseline_path.resolve() == candidate_path.resolve():
        raise ValueError("La base de referencia y la candidata deben ser distintas")
    for path, label in (
        (baseline_path, "referencia"),
        (candidate_path, "candidata"),
    ):
        if not path.exists() or not table_exists(path, "regulatory_records"):
            raise ValueError(f"La base {label} no contiene fichas regulatorias")

    protected_uids = (
        None
        if protected_decision_uids is None
        else {
            str(value).strip()
            for value in protected_decision_uids
            if str(value).strip()
        }
    )
    baseline_uri = baseline_path.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(baseline_uri, uri=True) as baseline_connection:
        baseline_connection.row_factory = sqlite3.Row
        baseline_documents = _database_documents(baseline_connection)
        baseline_records_by_document = {
            document["id"]: [
                record
                for record in _database_identity_records(
                    baseline_connection, document["id"]
                )
                if record["decision_uid"]
            ]
            for document in baseline_documents
        }

    with connect(candidate_path) as candidate_connection:
        candidate_connection.execute("BEGIN IMMEDIATE")
        candidate_documents = _database_documents(candidate_connection)
        candidate_records_by_document = {
            document["id"]: _database_identity_records(
                candidate_connection, document["id"]
            )
            for document in candidate_documents
        }

        baseline_doc_unused = {document["id"] for document in baseline_documents}
        candidate_doc_unused = {document["id"] for document in candidate_documents}
        document_matches: dict[int, int] = {}
        for field in ("catalog_id", "document_hash", "manifest_url", "signature"):
            for candidate_id, baseline_id in _match_unique_groups(
                candidate_documents,
                baseline_documents,
                candidate_doc_unused,
                baseline_doc_unused,
                field,
            ):
                if (
                    candidate_id not in candidate_doc_unused
                    or baseline_id not in baseline_doc_unused
                ):
                    continue
                document_matches[candidate_id] = baseline_id
                candidate_doc_unused.remove(candidate_id)
                baseline_doc_unused.remove(baseline_id)

        baseline_by_id = {item["id"]: item for item in baseline_documents}
        candidate_by_id = {item["id"]: item for item in candidate_documents}
        ambiguous_document_ids: set[int] = set()
        not_inherited_document_ids: set[int] = set()
        for candidate_id in candidate_doc_unused:
            candidate_document = candidate_by_id[candidate_id]
            possible_baseline_documents = [
                baseline_id
                for baseline_id in baseline_doc_unused
                if any(
                    candidate_document[field]
                    and candidate_document[field] == baseline_by_id[baseline_id][field]
                    for field in (
                        "catalog_id",
                        "document_hash",
                        "manifest_url",
                        "signature",
                    )
                )
            ]
            if not possible_baseline_documents:
                continue
            possible_uids = {
                record["decision_uid"]
                for baseline_id in possible_baseline_documents
                for record in baseline_records_by_document[baseline_id]
                if record["decision_uid"]
            }
            if protected_uids is None or possible_uids & protected_uids:
                ambiguous_document_ids.add(candidate_id)
            else:
                not_inherited_document_ids.add(candidate_id)

        all_candidate_records = [
            record
            for records in candidate_records_by_document.values()
            for record in records
        ]
        all_baseline_records = [
            record
            for records in baseline_records_by_document.values()
            for record in records
        ]
        successful_matches: dict[int, tuple[dict, str]] = {}
        ambiguous_candidate_records: set[int] = set()
        not_inherited_candidate_records: set[int] = set()
        generated_candidate_records: set[int] = set()
        matched_baseline_record_ids: set[int] = set()

        for candidate_document_id, baseline_document_id in document_matches.items():
            candidate_records = candidate_records_by_document[candidate_document_id]
            baseline_records = baseline_records_by_document[baseline_document_id]
            candidate_unused = {record["id"] for record in candidate_records}
            baseline_unused = {record["id"] for record in baseline_records}
            candidate_records_by_id = {record["id"]: record for record in candidate_records}
            baseline_records_by_id = {record["id"]: record for record in baseline_records}

            for field, match_kind in (
                ("record_key", "exact"),
                ("radicado_expediente", "composite"),
                ("radicado_page", "composite"),
                ("radicado_prefix_page", "composite"),
                ("expediente_page", "composite"),
                ("stable_text_signature", "text_exact"),
                ("radicado", "identifier"),
                ("expediente", "identifier"),
                ("stable_numeral", "identifier"),
            ):
                for candidate_id, baseline_id in _match_unique_groups(
                    candidate_records,
                    baseline_records,
                    candidate_unused,
                    baseline_unused,
                    field,
                ):
                    if candidate_id not in candidate_unused or baseline_id not in baseline_unused:
                        continue
                    successful_matches[candidate_id] = (
                        baseline_records_by_id[baseline_id],
                        match_kind,
                    )
                    candidate_unused.remove(candidate_id)
                    baseline_unused.remove(baseline_id)
                    matched_baseline_record_ids.add(baseline_id)

            # La primera ronda de claves únicas deja principalmente
            # identificadores duplicados. Se resuelven con mejores enlaces
            # mutuos: página + contenido/campos, usando el orden solo como
            # desempate. Se repite porque una asignación segura puede volver
            # inequívoca la siguiente dentro del mismo bloque.
            while True:
                scored = _mutual_scored_matches(
                    candidate_records,
                    baseline_records,
                    candidate_unused,
                    baseline_unused,
                )
                if not scored:
                    break
                accepted = 0
                for candidate_id, baseline_id in scored:
                    if (
                        candidate_id not in candidate_unused
                        or baseline_id not in baseline_unused
                    ):
                        continue
                    successful_matches[candidate_id] = (
                        baseline_records_by_id[baseline_id],
                        "scored",
                    )
                    candidate_unused.remove(candidate_id)
                    baseline_unused.remove(baseline_id)
                    matched_baseline_record_ids.add(baseline_id)
                    accepted += 1
                if not accepted:
                    break

            for candidate_id in candidate_unused:
                candidate_record = candidate_records_by_id[candidate_id]
                # Solo los UID de referencia aún disponibles pueden estar en
                # disputa. Comparar contra registros ya consumidos marcaba
                # falsamente como ambigua una decisión nueva que reutilizaba
                # expediente o radicado.
                possible_baseline_ids = {
                    baseline_id
                    for baseline_id in baseline_unused
                    if _record_match_has_evidence(
                        candidate_record,
                        baseline_records_by_id[baseline_id],
                    )
                }
                if possible_baseline_ids:
                    possible_uids = {
                        baseline_records_by_id[baseline_id]["decision_uid"]
                        for baseline_id in possible_baseline_ids
                        if baseline_records_by_id[baseline_id]["decision_uid"]
                    }
                    if protected_uids is None or possible_uids & protected_uids:
                        ambiguous_candidate_records.add(candidate_id)
                    else:
                        not_inherited_candidate_records.add(candidate_id)
                else:
                    generated_candidate_records.add(candidate_id)

        for candidate_document_id in candidate_doc_unused:
            record_ids = {
                record["id"]
                for record in candidate_records_by_document[candidate_document_id]
            }
            if candidate_document_id in ambiguous_document_ids:
                ambiguous_candidate_records.update(record_ids)
            elif candidate_document_id in not_inherited_document_ids:
                not_inherited_candidate_records.update(record_ids)
            else:
                generated_candidate_records.update(record_ids)

        baseline_uids = {
            record["decision_uid"]
            for record in all_baseline_records
            if record["decision_uid"]
        }
        reserved: set[str] = {
            baseline_record["decision_uid"]
            for baseline_record, _ in successful_matches.values()
        }
        final_uids: dict[int, str] = {}
        uid_map: dict[str, str] = {}
        candidate_uid_replacements: dict[str, str] = {}
        record_key_map: dict[str, dict[str, object]] = {}
        candidate_records_by_id = {record["id"]: record for record in all_candidate_records}
        candidate_document_for_record = {
            record["id"]: document_id
            for document_id, records in candidate_records_by_document.items()
            for record in records
        }
        for candidate_id, (baseline_record, match_kind) in successful_matches.items():
            final_uid = baseline_record["decision_uid"]
            if not final_uid:
                generated_candidate_records.add(candidate_id)
                continue
            final_uids[candidate_id] = final_uid
            uid_map[final_uid] = final_uid
            candidate_record = candidate_records_by_id[candidate_id]
            if candidate_record["decision_uid"] != final_uid:
                candidate_uid_replacements[candidate_record["decision_uid"]] = final_uid
            record_key_map[final_uid] = {
                "baseline": baseline_record["record_key"],
                "candidate": candidate_record["record_key"],
                "changed": baseline_record["record_key"]
                != candidate_record["record_key"],
                "match": match_kind,
            }

        for record in all_candidate_records:
            if record["id"] in final_uids:
                continue
            current_uid = record["decision_uid"]
            if not current_uid or current_uid in reserved or current_uid in baseline_uids:
                replacement = _fresh_candidate_uid(
                    record,
                    candidate_document_for_record[record["id"]],
                    reserved | baseline_uids,
                )
                if current_uid:
                    candidate_uid_replacements[current_uid] = replacement
                current_uid = replacement
            final_uids[record["id"]] = current_uid
            reserved.add(current_uid)

        # El índice UNIQUE excluye la cadena vacía. Vaciar dentro de la misma
        # transacción permite aplicar intercambios de UID sin colisiones
        # temporales ni identificadores auxiliares observables.
        candidate_connection.execute(
            "UPDATE regulatory_records SET decision_uid = ''"
        )
        for record in all_candidate_records:
            candidate_id = record["id"]
            if candidate_id in successful_matches:
                strategy = "baseline_" + successful_matches[candidate_id][1]
            elif candidate_id in ambiguous_candidate_records:
                strategy = "reconciliation_ambiguous"
            elif candidate_id in not_inherited_candidate_records:
                strategy = "candidate_not_inherited_v6"
            else:
                strategy = "candidate_generated_v6"
            candidate_connection.execute(
                """
                UPDATE regulatory_records
                SET decision_uid = ?, identity_strategy = ?
                WHERE id = ?
                """,
                (final_uids[candidate_id], strategy, candidate_id),
            )

        matched_baseline_uids = set(uid_map)
        orphaned = sum(
            bool(record["decision_uid"])
            and record["decision_uid"] not in matched_baseline_uids
            for record in all_baseline_records
        )
        exact = sum(kind == "exact" for _, kind in successful_matches.values())
        reconciled = len(successful_matches) - exact
        reconciliation_strategies: dict[str, int] = {}
        for _, kind in successful_matches.values():
            reconciliation_strategies[kind] = (
                reconciliation_strategies.get(kind, 0) + 1
            )
        ambiguous_examples = []
        for candidate_id in sorted(ambiguous_candidate_records)[:25]:
            record = candidate_records_by_id[candidate_id]
            document_id = candidate_document_for_record[candidate_id]
            document = candidate_by_id[document_id]
            ambiguous_examples.append(
                {
                    "decision_uid": final_uids[candidate_id],
                    "record_key": record["record_key"],
                    "document": document["title"],
                    "document_hash": document["document_hash"],
                    "page": record["page_number"],
                    "numeral": record["numeral"],
                    "expediente": record["expediente"],
                    "radicado": record["radicado"],
                }
            )
        not_inherited_examples = []
        for candidate_id in sorted(not_inherited_candidate_records)[:25]:
            record = candidate_records_by_id[candidate_id]
            document_id = candidate_document_for_record[candidate_id]
            document = candidate_by_id[document_id]
            not_inherited_examples.append(
                {
                    "decision_uid": final_uids[candidate_id],
                    "record_key": record["record_key"],
                    "document": document["title"],
                    "document_hash": document["document_hash"],
                    "page": record["page_number"],
                    "numeral": record["numeral"],
                    "expediente": record["expediente"],
                    "radicado": record["radicado"],
                    "resolution": "new_uid_not_inherited",
                }
            )
        report = {
            "exact": exact,
            "reconciled": reconciled,
            "reconciliation_strategies": reconciliation_strategies,
            "generated": len(generated_candidate_records),
            "not_inherited": len(not_inherited_candidate_records),
            "orphaned": int(orphaned),
            "ambiguous": len(ambiguous_candidate_records),
            "uid_map": uid_map,
            "candidate_uid_replacements": candidate_uid_replacements,
            "record_key_map": record_key_map,
            "documents_matched": len(document_matches),
            "documents_ambiguous": len(ambiguous_document_ids),
            "documents_not_inherited": len(not_inherited_document_ids),
            "protected_uids": (
                None if protected_uids is None else len(protected_uids)
            ),
            "ambiguous_examples": ambiguous_examples,
            "not_inherited_examples": not_inherited_examples,
        }
    return report


def optimize_database(database_path: Path) -> None:
    if not database_path.exists():
        return
    with connect(database_path) as connection:
        connection.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('optimize')")
        connection.execute("PRAGMA optimize")
        connection.commit()
        connection.execute("VACUUM")


def database_stats(database_path: Path) -> dict[str, int]:
    if not database_path.exists():
        return {"documents": 0, "pages": 0, "chunks": 0}
    with connect(database_path) as connection:
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("documents", "pages", "chunks")
        }


def dashboard_summary(database_path: Path) -> dict:
    empty = {
        "documents": 0,
        "unique_acts": 0,
        "pages": 0,
        "chunks": 0,
        "regulatory_records": 0,
        "documents_with_records": 0,
        "minimum_year": None,
        "maximum_year": None,
        "last_indexed_at": None,
        "latest_documents": [],
    }
    if not database_path.exists():
        return empty
    with connect(database_path) as connection:
        base = connection.execute(
            """
            SELECT COUNT(*) AS documents,
                   COUNT(DISTINCT COALESCE(CAST(year AS TEXT), '') || '|' ||
                         COALESCE(section, '') || '|' ||
                         COALESCE(acta_number, '')) AS unique_acts,
                   MIN(year) AS minimum_year,
                   MAX(year) AS maximum_year,
                   MAX(indexed_at) AS last_indexed_at
            FROM documents
            """
        ).fetchone()
        pages = int(connection.execute("SELECT COUNT(*) FROM pages").fetchone()[0])
        chunks = int(connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
        has_records = bool(
            connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='regulatory_records'"
            ).fetchone()
        )
        regulatory_records = 0
        documents_with_records = 0
        if has_records:
            regulatory_records = int(
                connection.execute("SELECT COUNT(*) FROM regulatory_records").fetchone()[0]
            )
            documents_with_records = int(
                connection.execute(
                    "SELECT COUNT(DISTINCT document_id) FROM regulatory_records"
                ).fetchone()[0]
            )
        latest_rows = connection.execute(
            """
            SELECT title, url, year, acta_number, section, part, indexed_at
            FROM documents
            ORDER BY indexed_at DESC, year DESC, acta_number DESC
            LIMIT 8
            """
        ).fetchall()
    return {
        "documents": int(base["documents"]),
        "unique_acts": int(base["unique_acts"]),
        "pages": pages,
        "chunks": chunks,
        "regulatory_records": regulatory_records,
        "documents_with_records": documents_with_records,
        "minimum_year": base["minimum_year"],
        "maximum_year": base["maximum_year"],
        "last_indexed_at": base["last_indexed_at"],
        "latest_documents": [dict(row) for row in latest_rows],
    }


def regulatory_records_for_chunks(
    database_path: Path,
    chunk_ids: Iterable[int],
) -> dict[int, list[dict]]:
    ids = list(dict.fromkeys(int(value) for value in chunk_ids))
    result = {chunk_id: [] for chunk_id in ids}
    if not ids or not table_exists(database_path, "regulatory_records"):
        return result
    with connect(database_path) as connection:
        for start in range(0, len(ids), 800):
            batch = ids[start : start + 800]
            placeholders = ",".join("?" for _ in batch)
            rows = connection.execute(
                f"""
                SELECT c.id AS chunk_id, p.page_number AS evidence_page,
                       r.id AS record_id, r.record_key, r.decision_uid,
                       r.page_number, r.end_page_number, r.numeral,
                       r.numeral_title, r.session_date, r.session_date_raw,
                       r.request_type_code, r.identity_strategy, r.product_name,
                       r.active_ingredient,
                       r.interested_party, r.expediente, r.radicado,
                       r.request_text, r.concept_text, r.outcome_code,
                       r.confidence, r.needs_review, r.extraction_method,
                       d.document_hash
                FROM chunks c
                JOIN pages p ON p.id = c.page_id
                JOIN documents d ON d.id = p.document_id
                JOIN regulatory_records r ON r.document_id = p.document_id
                WHERE c.id IN ({placeholders})
                  AND p.page_number BETWEEN r.page_number AND r.end_page_number
                ORDER BY c.id, r.page_number, r.id
                """,
                batch,
            ).fetchall()
            for row in rows:
                result[int(row["chunk_id"])].append(
                    {key: row[key] for key in row.keys() if key != "chunk_id"}
                )
    return result


_REGULATORY_DETAIL_SELECT = """
    SELECT r.id AS record_id, r.record_key, r.decision_uid,
           r.page_number, r.end_page_number, r.numeral, r.numeral_title,
           r.session_date, r.session_date_raw, r.request_type_code,
           r.identity_strategy, r.product_name, r.active_ingredient,
           r.interested_party, r.expediente, r.radicado, r.request_text,
           r.concept_text, r.outcome_code, r.confidence, r.needs_review,
           r.extraction_method, r.extractor_version, r.created_at,
           d.id AS document_id, d.title, d.url, d.manifest_url, d.year,
           d.acta_number, d.section, d.part, d.source_type,
           d.document_hash, d.pdf_page_count
    FROM regulatory_records r
    JOIN documents d ON d.id = r.document_id
"""


def get_regulatory_records_by_uids(
    database_path: Path,
    decision_uids: Iterable[str],
) -> list[dict]:
    """Recupera fichas exactas conservando el orden solicitado."""

    values = [
        str(value).strip()
        for value in dict.fromkeys(decision_uids)
        if str(value).strip()
    ]
    if not values or not database_path.exists():
        return []
    rows_by_uid: dict[str, dict] = {}
    with connect(database_path) as connection:
        for start in range(0, len(values), 800):
            batch = values[start : start + 800]
            placeholders = ",".join("?" for _ in batch)
            rows = connection.execute(
                _REGULATORY_DETAIL_SELECT
                + f" WHERE r.decision_uid IN ({placeholders})",
                batch,
            ).fetchall()
            rows_by_uid.update(
                {str(row["decision_uid"]): dict(row) for row in rows}
            )
    return [rows_by_uid[value] for value in values if value in rows_by_uid]


def list_regulatory_records(
    database_path: Path,
    *,
    query: str = "",
    years: Iterable[int] = (),
    outcomes: Iterable[str] = (),
    request_types: Iterable[str] = (),
    missing_field: str = "",
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """Lista fichas para revisión, comparación o exportación."""

    if not database_path.exists() or limit < 1 or offset < 0:
        return []
    clauses, parameters = _regulatory_filter_clauses(
        query=query,
        years=years,
        outcomes=outcomes,
        request_types=request_types,
        missing_field=missing_field,
    )
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with connect(database_path) as connection:
        rows = connection.execute(
            _REGULATORY_DETAIL_SELECT
            + where
            + " ORDER BY d.year DESC, d.section, d.acta_number DESC, "
            "r.page_number, r.id LIMIT ? OFFSET ?",
            [*parameters, min(int(limit), 1000), int(offset)],
        ).fetchall()
    return [dict(row) for row in rows]


def _regulatory_filter_clauses(
    *,
    query: str = "",
    years: Iterable[int] = (),
    outcomes: Iterable[str] = (),
    request_types: Iterable[str] = (),
    missing_field: str = "",
) -> tuple[list[str], list[object]]:
    clauses: list[str] = []
    parameters: list[object] = []
    clean_query = normalize_text(query)
    if clean_query:
        like = f"%{clean_query}%"
        identifier = f"%{_normalized_identifier(query)}%"
        clauses.append(
            "(r.normalized_product_name LIKE ? OR "
            "r.normalized_active_ingredient LIKE ? OR "
            "r.normalized_interested_party LIKE ? OR "
            "r.normalized_expediente LIKE ? OR r.normalized_radicado LIKE ? OR "
            "LOWER(COALESCE(r.numeral, '')) LIKE ? OR "
            "LOWER(COALESCE(r.request_text, '')) LIKE ?)"
        )
        parameters.extend((like, like, like, identifier, identifier, like, like))
    for values, column in (
        ([int(value) for value in years], "d.year"),
        ([str(value) for value in outcomes if value], "r.outcome_code"),
        ([str(value) for value in request_types if value], "r.request_type_code"),
    ):
        if values:
            placeholders = ",".join("?" for _ in values)
            clauses.append(f"{column} IN ({placeholders})")
            parameters.extend(values)
    missing_columns = {
        "numeral": "r.numeral",
        "session_date": "r.session_date",
        "product": "r.product_name",
        "active_ingredient": "r.active_ingredient",
    }
    if missing_field in missing_columns:
        column = missing_columns[missing_field]
        clauses.append(f"({column} IS NULL OR TRIM({column}) = '')")
    elif missing_field == "request_type":
        clauses.append(
            "(r.request_type_code IS NULL OR TRIM(r.request_type_code) = '' "
            "OR r.request_type_code = 'otra_solicitud')"
        )
    elif missing_field == "outcome":
        clauses.append(
            "(r.outcome_code IS NULL OR TRIM(r.outcome_code) = '' "
            "OR r.outcome_code = 'sin_clasificar')"
        )
    return clauses, parameters


def count_regulatory_records(
    database_path: Path,
    *,
    query: str = "",
    years: Iterable[int] = (),
    outcomes: Iterable[str] = (),
    request_types: Iterable[str] = (),
    missing_field: str = "",
) -> int:
    """Cuenta todas las fichas que coinciden con la cola de revisión."""

    if not database_path.exists():
        return 0
    clauses, parameters = _regulatory_filter_clauses(
        query=query,
        years=years,
        outcomes=outcomes,
        request_types=request_types,
        missing_field=missing_field,
    )
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT COUNT(*) FROM regulatory_records r "
            "JOIN documents d ON d.id = r.document_id" + where,
            parameters,
        ).fetchone()
    return int(row[0] if row else 0)


def indexed_document_catalog(database_path: Path) -> dict[str, dict]:
    if not database_path.exists() or not is_current_schema(database_path):
        return {}
    with connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT manifest_url, title, year, acta_number, section, part, source_type
            FROM documents
            """
        ).fetchall()
    return {
        row["manifest_url"]: {
            "title": row["title"],
            "year": row["year"],
            "acta_number": row["acta_number"],
            "section": row["section"],
            "part": row["part"],
            "source_type": row["source_type"],
        }
        for row in rows
    }


def sync_document_metadata(
    database_path: Path,
    documents: Iterable[DocumentMetadata],
) -> int:
    """Completa metadatos de catálogo sin descargar ni reindexar los PDF.

    Si aparece o cambia ``catalog_id``, invalida únicamente la extracción
    estructurada para que el siguiente ``sync_regulatory_extractions`` regenere
    los identificadores de decisión con esa identidad estable.
    """

    if not database_path.exists() or not is_current_schema(database_path):
        return 0
    updated = 0
    with connect(database_path) as connection:
        for document in documents:
            row = connection.execute(
                "SELECT id, catalog_id, publication_date FROM documents "
                "WHERE manifest_url = ?",
                (document.url,),
            ).fetchone()
            if row is None:
                continue
            old_catalog_id = str(row["catalog_id"] or "")
            old_publication_date = str(row["publication_date"] or "")
            new_catalog_id = str(document.catalog_id or old_catalog_id)
            new_publication_date = str(
                document.publication_date or old_publication_date
            )
            if (
                new_catalog_id == old_catalog_id
                and new_publication_date == old_publication_date
            ):
                continue
            connection.execute(
                "UPDATE documents SET catalog_id = ?, publication_date = ? "
                "WHERE id = ?",
                (
                    new_catalog_id or None,
                    new_publication_date or None,
                    int(row["id"]),
                ),
            )
            if new_catalog_id != old_catalog_id:
                connection.execute(
                    "DELETE FROM document_extractions WHERE document_id = ?",
                    (int(row["id"]),),
                )
            updated += 1
    return updated


def get_filter_options(
    database_path: Path,
    review_events: Iterable[object] = (),
) -> dict[str, list]:
    """Devuelve opciones del índice y de las correcciones humanas vigentes.

    Los códigos normalizados pueden existir únicamente en el registro de
    revisiones. En ese caso se resuelven contra la ficha actual para no ofrecer
    correcciones huérfanas, reabiertas o invalidadas por un cambio de PDF.
    """

    review_values = list(review_events)
    if not database_path.exists():
        return {
            "years": [],
            "sections": [],
            "parts": [],
            "acta_numbers": [],
            "outcomes": [],
            "request_types": [],
        }
    with connect(database_path) as connection:
        mapping = {
            "years": "year",
            "sections": "section",
            "parts": "part",
            "acta_numbers": "acta_number",
        }
        options: dict[str, list] = {}
        for key, column in mapping.items():
            rows = connection.execute(
                f"SELECT DISTINCT {column} FROM documents "
                f"WHERE {column} IS NOT NULL ORDER BY {column}"
            ).fetchall()
            options[key] = [row[0] for row in rows]
        options["years"].sort(reverse=True)
        if table_exists(database_path, "regulatory_records"):
            rows = connection.execute(
                """
                SELECT DISTINCT outcome_code
                FROM regulatory_records
                WHERE outcome_code IS NOT NULL AND outcome_code != ''
                ORDER BY outcome_code
                """
            ).fetchall()
            options["outcomes"] = [row[0] for row in rows]
            rows = connection.execute(
                """
                SELECT DISTINCT request_type_code
                FROM regulatory_records
                WHERE request_type_code IS NOT NULL AND request_type_code != ''
                ORDER BY request_type_code
                """
            ).fetchall()
            options["request_types"] = [row[0] for row in rows]
        else:
            options["outcomes"] = []
            options["request_types"] = []

    if review_values and table_exists(database_path, "regulatory_records"):
        # Importación local para mantener database.py independiente del formato
        # CSV durante la inicialización de los módulos.
        from services.reviews import apply_latest_reviews, latest_reviews

        current_reviews = latest_reviews(review_values)
        relevant_uids = [
            uid
            for uid, event in current_reviews.items()
            if event.status != "reopened"
            and (
                "outcome_code" in event.corrections
                or "request_type_code" in event.corrections
            )
        ]
        if relevant_uids:
            records = get_regulatory_records_by_uids(database_path, relevant_uids)
            effective = apply_latest_reviews(
                records,
                [current_reviews[uid] for uid in relevant_uids],
            )
            options["outcomes"] = sorted(
                {
                    *options["outcomes"],
                    *(
                        str(record.get("outcome_code") or "").strip()
                        for record in effective
                    ),
                }
                - {""},
                key=str.casefold,
            )
            options["request_types"] = sorted(
                {
                    *options["request_types"],
                    *(
                        str(record.get("request_type_code") or "").strip()
                        for record in effective
                    ),
                }
                - {""},
                key=str.casefold,
            )
    return options


def _filter_sql(filters: dict[str, list] | None) -> tuple[str, list]:
    filters = filters or {}
    clauses: list[str] = []
    parameters: list = []
    mapping = {
        "years": "d.year",
        "sections": "d.section",
        "parts": "d.part",
        "acta_numbers": "d.acta_number",
    }
    for key, column in mapping.items():
        values = [value for value in filters.get(key, []) if value not in (None, "")]
        if values:
            placeholders = ",".join("?" for _ in values)
            clauses.append(f"{column} IN ({placeholders})")
            parameters.extend(values)

    record_clauses: list[str] = []
    record_parameters: list = []
    outcomes = [value for value in filters.get("outcomes", []) if value]
    if outcomes:
        placeholders = ",".join("?" for _ in outcomes)
        record_clauses.append(f"rr.outcome_code IN ({placeholders})")
        record_parameters.extend(outcomes)

    request_types = [value for value in filters.get("request_types", []) if value]
    if request_types:
        placeholders = ",".join("?" for _ in request_types)
        record_clauses.append(f"rr.request_type_code IN ({placeholders})")
        record_parameters.extend(request_types)

    structured_mapping = {
        "products": "normalized_product_name",
        "active_ingredients": "normalized_active_ingredient",
        "interested_parties": "normalized_interested_party",
    }
    for key, column in structured_mapping.items():
        values = [normalize_text(str(value)) for value in filters.get(key, []) if value]
        if values:
            record_clauses.append(f"rr.{column} LIKE ?")
            record_parameters.append(f"%{values[0]}%")

    identifiers = [value for value in filters.get("identifiers", []) if value]
    if identifiers:
        normalized = _normalized_identifier(str(identifiers[0]))
        record_clauses.append(
            "(rr.normalized_expediente LIKE ? OR rr.normalized_radicado LIKE ?)"
        )
        record_parameters.extend((f"%{normalized}%", f"%{normalized}%"))
    if record_clauses:
        clauses.append(
            "EXISTS (SELECT 1 FROM regulatory_records rr "
            "WHERE rr.document_id = d.id "
            "AND p.page_number BETWEEN rr.page_number AND rr.end_page_number "
            "AND " + " AND ".join(record_clauses) + ")"
        )
        parameters.extend(record_parameters)
    return (" AND " + " AND ".join(clauses) if clauses else "", parameters)


def _rows_to_results(rows: list[sqlite3.Row], query: str) -> list[SearchResult]:
    normalized_query = normalize_text(query)
    ranked: list[SearchResult] = []
    row_count = max(len(rows), 1)
    for position, row in enumerate(rows):
        base_score = 1.0 - (position / row_count)
        exact_bonus = (
            2.0 if normalized_query in normalize_text(row["text"]) else 0.0
        )
        title_bonus = 0.75 if normalized_query in row["normalized_title"] else 0.0
        score = round(base_score + exact_bonus + title_bonus, 3)
        ranked.append(
            SearchResult(
                chunk_id=int(row["chunk_id"]),
                title=row["title"],
                url=row["url"],
                page=int(row["page_number"]),
                text=row["text"],
                year=row["year"],
                acta_number=row["acta_number"],
                section=row["section"],
                part=row["part"],
                source_type=row["source_type"],
                score=score,
                lexical_score=score,
                match_type="textual",
            )
        )
    ranked.sort(key=lambda item: item.score, reverse=True)
    return ranked


def chunk_ids_for_filters(
    database_path: Path,
    filters: dict[str, list] | None,
) -> list[int] | None:
    if not filters or not any(filters.values()):
        return None
    filter_clause, parameters = _filter_sql(filters)
    with connect(database_path) as connection:
        rows = connection.execute(
            f"""
            SELECT c.id
            FROM chunks c
            JOIN pages p ON p.id = c.page_id
            JOIN documents d ON d.id = p.document_id
            WHERE 1 = 1 {filter_clause}
            ORDER BY c.id
            """,
            parameters,
        ).fetchall()
    return [int(row[0]) for row in rows]


def get_chunks_by_ids(
    database_path: Path,
    chunk_ids: Iterable[int],
    *,
    filters: dict[str, list] | None = None,
    scores: dict[int, float] | None = None,
) -> list[SearchResult]:
    ids = list(dict.fromkeys(int(value) for value in chunk_ids))
    if not ids or not database_path.exists():
        return []
    rows_by_id: dict[int, sqlite3.Row] = {}
    filter_clause, filter_parameters = _filter_sql(filters)
    with connect(database_path) as connection:
        for start in range(0, len(ids), 800):
            batch = ids[start : start + 800]
            placeholders = ",".join("?" for _ in batch)
            rows = connection.execute(
                f"""
                SELECT c.id AS chunk_id, c.text, p.page_number,
                       d.title, d.normalized_title, d.url, d.year,
                       d.acta_number, d.section, d.part, d.source_type
                FROM chunks c
                JOIN pages p ON p.id = c.page_id
                JOIN documents d ON d.id = p.document_id
                WHERE c.id IN ({placeholders}) {filter_clause}
                """,
                [*batch, *filter_parameters],
            ).fetchall()
            rows_by_id.update({int(row["chunk_id"]): row for row in rows})

    results: list[SearchResult] = []
    for position, chunk_id in enumerate(ids):
        row = rows_by_id.get(chunk_id)
        if row is None:
            continue
        score = (
            float(scores[chunk_id])
            if scores and chunk_id in scores
            else 1.0 - (position / max(len(ids), 1))
        )
        results.append(
            SearchResult(
                chunk_id=chunk_id,
                title=row["title"],
                url=row["url"],
                page=int(row["page_number"]),
                text=row["text"],
                year=row["year"],
                acta_number=row["acta_number"],
                section=row["section"],
                part=row["part"],
                source_type=row["source_type"],
                score=round(score, 6),
            )
        )
    return results


def search_chunks(
    database_path: Path,
    query: str,
    top_k: int = 10,
    filters: dict[str, list] | None = None,
    exact_phrase: bool = False,
    max_chunks_per_document: int = 5,
) -> list[SearchResult]:
    query = query.strip()
    if not query or not database_path.exists():
        return []

    terms = tokenize_query(query)
    if not terms:
        terms = [normalize_text(query)]
    if exact_phrase:
        clean_phrase = normalize_text(query).replace('"', "")
        fts_query = f'"{clean_phrase}"'
    else:
        fts_query = " OR ".join(
            f'"{term.replace(chr(34), "")}"' for term in terms
        )
    filter_clause, filter_parameters = _filter_sql(filters)
    if max_chunks_per_document < 1:
        raise ValueError("max_chunks_per_document debe ser mayor que cero")

    sql = f"""
        WITH candidates AS MATERIALIZED (
            SELECT
                c.id AS chunk_id,
                c.text,
                p.page_number,
                d.id AS document_id,
                d.title,
                d.normalized_title,
                d.url,
                d.year,
                d.acta_number,
                d.section,
                d.part,
                d.source_type,
                bm25(chunks_fts, 3.0, 1.0) AS lexical_rank
            FROM chunks_fts
            JOIN chunks c ON c.id = chunks_fts.rowid
            JOIN pages p ON p.id = c.page_id
            JOIN documents d ON d.id = p.document_id
            WHERE chunks_fts MATCH ? {filter_clause}
        ), ranked AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY document_id
                       ORDER BY lexical_rank, chunk_id
                   ) AS document_rank
            FROM candidates
        )
        SELECT *
        FROM ranked
        WHERE document_rank <= ?
        ORDER BY lexical_rank ASC
        LIMIT ?
    """

    with connect(database_path) as connection:
        rows = connection.execute(
            sql,
            [
                fts_query,
                *filter_parameters,
                max_chunks_per_document,
                top_k,
            ],
        ).fetchall()

    results = _rows_to_results(rows, query)
    if exact_phrase:
        normalized_query = normalize_text(query)
        results = [
            result
            for result in results
            if normalized_query in normalize_text(f"{result.title} {result.text}")
        ]
    return results[:top_k]
