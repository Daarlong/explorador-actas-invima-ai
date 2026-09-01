from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from services.models import DocumentMetadata, SearchResult
from services.text_utils import normalize_text, tokenize_query


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    year INTEGER,
    acta_number TEXT,
    section TEXT,
    part TEXT,
    document_hash TEXT NOT NULL,
    indexed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pages (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL,
    text TEXT NOT NULL,
    UNIQUE(document_id, page_number)
);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    text TEXT NOT NULL,
    normalized_text TEXT NOT NULL,
    UNIQUE(page_id, chunk_index)
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    title,
    text,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE INDEX IF NOT EXISTS idx_documents_year ON documents(year);
CREATE INDEX IF NOT EXISTS idx_documents_acta ON documents(acta_number);
CREATE INDEX IF NOT EXISTS idx_documents_section ON documents(section);
CREATE INDEX IF NOT EXISTS idx_pages_document ON pages(document_id);
CREATE INDEX IF NOT EXISTS idx_chunks_page ON chunks(page_id);
"""


def connect(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database(database_path: Path) -> None:
    with connect(database_path) as connection:
        try:
            connection.executescript(SCHEMA)
        except sqlite3.OperationalError as exc:
            if "fts5" in str(exc).lower():
                raise RuntimeError(
                    "La instalación de SQLite no incluye soporte FTS5"
                ) from exc
            raise


def clear_database(database_path: Path) -> None:
    with connect(database_path) as connection:
        connection.execute("DELETE FROM chunks_fts")
        connection.execute("DELETE FROM documents")


def insert_document(
    database_path: Path,
    metadata: DocumentMetadata,
    document_hash: str,
    pages: Iterable[dict],
) -> int:
    indexed_at = datetime.now(timezone.utc).isoformat()
    with connect(database_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO documents (
                title, normalized_title, url, year, acta_number,
                section, part, document_hash, indexed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                metadata.title,
                normalize_text(metadata.title),
                metadata.url,
                metadata.year,
                metadata.acta_number,
                metadata.section,
                metadata.part,
                document_hash,
                indexed_at,
            ),
        )
        document_id = int(cursor.lastrowid)

        for page in pages:
            page_cursor = connection.execute(
                "INSERT INTO pages (document_id, page_number, text) VALUES (?, ?, ?)",
                (document_id, int(page["page"]), page["text"]),
            )
            page_id = int(page_cursor.lastrowid)
            for chunk_index, chunk in enumerate(page.get("chunks", [])):
                chunk_cursor = connection.execute(
                    """
                    INSERT INTO chunks (page_id, chunk_index, text, normalized_text)
                    VALUES (?, ?, ?, ?)
                    """,
                    (page_id, chunk_index, chunk, normalize_text(chunk)),
                )
                chunk_id = int(chunk_cursor.lastrowid)
                connection.execute(
                    "INSERT INTO chunks_fts (rowid, title, text) VALUES (?, ?, ?)",
                    (chunk_id, metadata.title, chunk),
                )
    return document_id


def database_stats(database_path: Path) -> dict[str, int]:
    if not database_path.exists():
        return {"documents": 0, "pages": 0, "chunks": 0}
    with connect(database_path) as connection:
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("documents", "pages", "chunks")
        }


def get_filter_options(database_path: Path) -> dict[str, list]:
    if not database_path.exists():
        return {"years": [], "sections": [], "parts": [], "acta_numbers": []}
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
    return (" AND " + " AND ".join(clauses) if clauses else "", parameters)


def _rows_to_results(rows: list[sqlite3.Row], query: str) -> list[SearchResult]:
    normalized_query = normalize_text(query)
    ranked: list[SearchResult] = []
    row_count = max(len(rows), 1)
    for position, row in enumerate(rows):
        base_score = 1.0 - (position / row_count)
        exact_bonus = 2.0 if normalized_query in row["normalized_text"] else 0.0
        title_bonus = 0.75 if normalized_query in row["normalized_title"] else 0.0
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
                score=round(base_score + exact_bonus + title_bonus, 3),
            )
        )
    ranked.sort(key=lambda item: item.score, reverse=True)
    return ranked


def search_chunks(
    database_path: Path,
    query: str,
    top_k: int = 10,
    filters: dict[str, list] | None = None,
) -> list[SearchResult]:
    query = query.strip()
    if not query or not database_path.exists():
        return []

    terms = tokenize_query(query)
    if not terms:
        terms = [normalize_text(query)]
    fts_query = " OR ".join(f'"{term.replace(chr(34), "")}"' for term in terms)
    filter_clause, filter_parameters = _filter_sql(filters)
    candidate_limit = max(top_k * 4, 20)

    sql = f"""
        SELECT
            c.id AS chunk_id,
            c.text,
            c.normalized_text,
            p.page_number,
            d.title,
            d.normalized_title,
            d.url,
            d.year,
            d.acta_number,
            d.section,
            d.part,
            bm25(chunks_fts, 3.0, 1.0) AS lexical_rank
        FROM chunks_fts
        JOIN chunks c ON c.id = chunks_fts.rowid
        JOIN pages p ON p.id = c.page_id
        JOIN documents d ON d.id = p.document_id
        WHERE chunks_fts MATCH ? {filter_clause}
        ORDER BY lexical_rank ASC
        LIMIT ?
    """

    with connect(database_path) as connection:
        rows = connection.execute(
            sql,
            [fts_query, *filter_parameters, candidate_limit],
        ).fetchall()

        normalized_query = normalize_text(query)
        exact_sql = f"""
            SELECT
                c.id AS chunk_id,
                c.text,
                c.normalized_text,
                p.page_number,
                d.title,
                d.normalized_title,
                d.url,
                d.year,
                d.acta_number,
                d.section,
                d.part,
                -1000.0 AS lexical_rank
            FROM chunks c
            JOIN pages p ON p.id = c.page_id
            JOIN documents d ON d.id = p.document_id
            WHERE c.normalized_text LIKE ? {filter_clause}
            LIMIT ?
        """
        exact_rows = connection.execute(
            exact_sql,
            [f"%{normalized_query}%", *filter_parameters, candidate_limit],
        ).fetchall()

    deduplicated: dict[int, sqlite3.Row] = {}
    for row in [*exact_rows, *rows]:
        deduplicated.setdefault(int(row["chunk_id"]), row)
    results = _rows_to_results(list(deduplicated.values()), query)
    return results[:top_k]

