from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from services.models import DocumentMetadata, SearchResult
from services.regulatory import RegulatoryRecord, extract_regulatory_records
from services.text_utils import normalize_text, tokenize_query


DATABASE_SCHEMA_VERSION = 4

REGULATORY_EXTRACTOR_VERSION = "2"

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
    page_number INTEGER NOT NULL,
    end_page_number INTEGER NOT NULL,
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
    table_exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'documents'"
    ).fetchone()
    if not table_exists:
        return
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(documents)").fetchall()
    }
    if "source_type" not in columns:
        connection.execute(
            "ALTER TABLE documents ADD COLUMN source_type TEXT NOT NULL "
            "DEFAULT 'official'"
        )
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


def migrate_database_schema(database_path: Path) -> bool:
    """Aplica migraciones aditivas v2/v3→v4 sobre una copia verificada."""
    version = database_schema_version(database_path)
    if version == DATABASE_SCHEMA_VERSION:
        return False
    if version > DATABASE_SCHEMA_VERSION:
        raise ValueError(
            f"La base usa el esquema {version}, superior al soportado "
            f"({DATABASE_SCHEMA_VERSION})"
        )
    if version not in {2, 3}:
        return False

    temporary_path = database_path.with_suffix(".schema-v3.db")
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
        else len(indexed_pages) + len(scan_pages)
    )

    with connect(database_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO documents (
                title, normalized_title, url, manifest_url, year, acta_number,
                section, part, source_type, document_hash, pdf_page_count,
                indexed_page_count, ocr_candidate_pages, page_inventory_complete,
                indexed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                document_hash,
                pdf_page_count,
                len(indexed_pages),
                ",".join(str(page) for page in scan_pages),
                int(page_inventory_complete),
                indexed_at,
            ),
        )
        document_id = int(cursor.lastrowid)

        for page in indexed_pages:
            page_cursor = connection.execute(
                "INSERT INTO pages (document_id, page_number) VALUES (?, ?)",
                (document_id, int(page["page"])),
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
    payload = json.dumps(
        record.as_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _store_regulatory_records(
    connection: sqlite3.Connection,
    document_id: int,
    document_hash: str,
    records: Iterable[RegulatoryRecord],
    *,
    extraction_method: str,
    extractor_version: str,
) -> int:
    values = list(records)
    now = datetime.now(timezone.utc).isoformat()
    connection.execute(
        "DELETE FROM regulatory_records WHERE document_id = ?",
        (document_id,),
    )
    for record in values:
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
                document_id, record_key, page_number, end_page_number, product_name,
                normalized_product_name, active_ingredient,
                normalized_active_ingredient, interested_party,
                normalized_interested_party, expediente,
                normalized_expediente, radicado, normalized_radicado,
                request_text, concept_text, outcome_code, extraction_method,
                confidence, needs_review, extractor_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                document_id,
                _regulatory_record_key(record),
                record.pagina,
                record.pagina_final or record.pagina,
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
    connection.execute(
        """
        INSERT OR REPLACE INTO document_extractions (
            document_id, document_hash, extractor_version, status,
            record_count, error_message, extracted_at
        ) VALUES (?, ?, ?, 'complete', ?, NULL, ?)
        """,
        (document_id, document_hash, extractor_version, len(values), now),
    )
    return len(values)


def sync_regulatory_extractions(
    database_path: Path,
    *,
    extractor_version: str = REGULATORY_EXTRACTOR_VERSION,
) -> dict:
    """Extrae campos pendientes sin descargar nuevamente los PDF."""
    summary = {
        "documents_total": 0,
        "documents_processed": 0,
        "documents_skipped": 0,
        "documents_failed": 0,
        "records_extracted": 0,
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
                   e.extractor_version AS prior_version, e.status AS prior_status
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
            ):
                summary["documents_skipped"] += 1
                continue
            try:
                rows = connection.execute(
                    """
                    SELECT p.page_number, c.text
                    FROM pages p
                    JOIN chunks c ON c.page_id = p.id
                    WHERE p.document_id = ?
                    ORDER BY p.page_number, c.chunk_index
                    """,
                    (document["id"],),
                ).fetchall()
                pages: list[dict] = []
                for row in rows:
                    if pages and pages[-1]["page"] == int(row["page_number"]):
                        pages[-1]["text"] += "\n" + row["text"]
                    else:
                        pages.append(
                            {
                                "page": int(row["page_number"]),
                                "text": str(row["text"]),
                            }
                        )
                records = extract_regulatory_records(pages)
                count = _store_regulatory_records(
                    connection,
                    int(document["id"]),
                    str(document["document_hash"]),
                    records,
                    extraction_method="deterministic_chunk_backfill",
                    extractor_version=extractor_version,
                )
                summary["documents_processed"] += 1
                summary["records_extracted"] += count
            except Exception as exc:
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
                       r.page_number, r.end_page_number, r.product_name,
                       r.active_ingredient,
                       r.interested_party, r.expediente, r.radicado,
                       r.request_text, r.concept_text, r.outcome_code,
                       r.confidence, r.needs_review, r.extraction_method
                FROM chunks c
                JOIN pages p ON p.id = c.page_id
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


def get_filter_options(database_path: Path) -> dict[str, list]:
    if not database_path.exists():
        return {
            "years": [],
            "sections": [],
            "parts": [],
            "acta_numbers": [],
            "outcomes": [],
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
        else:
            options["outcomes"] = []
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
