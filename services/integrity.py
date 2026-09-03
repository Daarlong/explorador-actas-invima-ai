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


# Las columnas se inspeccionan en tiempo de ejecución: una base v0.6 sigue
# pudiendo abrir esta página mientras el workflow prepara el esquema v0.7.
REGULATORY_COMPLETENESS_FIELDS = (
    ("numeral", "Numeral", ("numeral",), "any", False),
    ("product", "Producto", ("product_name",), "any", True),
    (
        "active_ingredient",
        "Principio activo",
        ("active_ingredient",),
        "any",
        True,
    ),
    ("interested_party", "Interesado", ("interested_party",), "any", False),
    ("expediente", "Expediente", ("expediente",), "any", False),
    ("radicado", "Radicado", ("radicado",), "any", False),
    ("identifiers", "Expediente o radicado", ("expediente", "radicado"), "any", True),
    (
        "page_range",
        "Rango de páginas",
        ("page_number", "end_page_number"),
        "page_range",
        False,
    ),
    ("concept", "Concepto", ("concept_text",), "any", True),
    ("outcome", "Resultado clasificado", ("outcome_code",), "classified", True),
)


def _table_columns(connection, table_name: str) -> set[str]:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    if not exists:
        return set()
    return {
        str(row["name"])
        for row in connection.execute(f'PRAGMA table_info("{table_name}")').fetchall()
    }


def _present_expression(columns: tuple[str, ...], mode: str) -> str:
    if mode == "page_range":
        if set(columns) != {"page_number", "end_page_number"}:
            return "(0)"
        return (
            "(page_number IS NOT NULL AND end_page_number IS NOT NULL "
            "AND page_number > 0 AND end_page_number >= page_number)"
        )
    expressions: list[str] = []
    for column in columns:
        value = f'TRIM(COALESCE("{column}", \'\'))'
        if mode == "classified":
            expressions.append(
                f"LOWER({value}) NOT IN ('', 'sin_clasificar', 'unclassified', "
                "'unknown', 'none')"
            )
        else:
            expressions.append(f"{value} != ''")
    return "(" + " OR ".join(expressions) + ")"


def _evidence_origin(method: object) -> str:
    value = str(method or "").strip().lower()
    if value in {
        "explicit_label",
        "composition_label",
        "dosage_statement",
        "numbered_heading",
        "numbered_heading_product",
    }:
        return "explícito estructurado"
    if value == "page_span":
        return "estructura documental"
    if "infer" in value or "alias" in value:
        return "inferido"
    return "otro método"


def _regulatory_quality_snapshot_missing() -> dict:
    return {
        "status": "unavailable",
        "records": 0,
        "fields": [],
        "field_completeness": {},
        "complete_core_records": 0,
        "complete_core_percent": None,
        "record_confidence": {"available": False},
        "field_evidence": {
            "available": False,
            "rows": 0,
            "records": 0,
            "record_coverage_percent": None,
            "by_field": [],
            "by_method": [],
        },
        "extractor_versions": [],
    }


def _regulatory_quality_snapshot(connection) -> dict:
    """Mide presencia y trazabilidad; no pretende medir precisión clínica."""
    record_columns = _table_columns(connection, "regulatory_records")
    if not record_columns:
        return _regulatory_quality_snapshot_missing()

    total = int(connection.execute("SELECT COUNT(*) FROM regulatory_records").fetchone()[0])
    fields: list[dict] = []
    core_expressions: list[str] = []
    expected_core_count = sum(item[4] for item in REGULATORY_COMPLETENESS_FIELDS)
    for key, label, candidates, mode, is_core in REGULATORY_COMPLETENESS_FIELDS:
        available_columns = tuple(
            column for column in candidates if column in record_columns
        )
        if mode == "page_range" and len(available_columns) != len(candidates):
            available_columns = ()
        if not available_columns:
            fields.append(
                {
                    "key": key,
                    "label": label,
                    "available": False,
                    "present": 0,
                    "missing": total,
                    "coverage_percent": None,
                }
            )
            continue
        expression = _present_expression(available_columns, mode)
        present = int(
            connection.execute(
                f"SELECT COUNT(*) FROM regulatory_records WHERE {expression}"
            ).fetchone()[0]
        )
        if is_core:
            core_expressions.append(expression)
        fields.append(
            {
                "key": key,
                "label": label,
                "available": True,
                "present": present,
                "missing": max(total - present, 0),
                "coverage_percent": round((present / total) * 100, 2)
                if total
                else None,
            }
        )

    all_core_available = len(core_expressions) == expected_core_count
    complete_core = 0
    if all_core_available and total:
        complete_core = int(
            connection.execute(
                "SELECT COUNT(*) FROM regulatory_records WHERE "
                + " AND ".join(core_expressions)
            ).fetchone()[0]
        )

    confidence: dict[str, object] = {"available": "confidence" in record_columns}
    if "confidence" in record_columns:
        row = connection.execute(
            """
            SELECT AVG(confidence) AS average_confidence,
                   SUM(CASE WHEN confidence IS NULL OR confidence < 0.70
                       THEN 1 ELSE 0 END) AS low_confidence,
                   SUM(CASE WHEN confidence IS NULL THEN 1 ELSE 0 END)
                       AS without_confidence
            FROM regulatory_records
            """
        ).fetchone()
        confidence.update(
            {
                "average": round(float(row["average_confidence"]), 4)
                if row["average_confidence"] is not None
                else None,
                "low": int(row["low_confidence"] or 0),
                "missing": int(row["without_confidence"] or 0),
            }
        )

    extractor_versions: list[dict] = []
    if "extractor_version" in record_columns:
        extractor_versions = [
            {
                "version": str(row["version"] or "sin versión"),
                "records": int(row["records"]),
            }
            for row in connection.execute(
                """
                SELECT extractor_version AS version, COUNT(*) AS records
                FROM regulatory_records
                GROUP BY extractor_version
                ORDER BY COUNT(*) DESC, extractor_version
                """
            ).fetchall()
        ]

    evidence_columns = _table_columns(connection, "regulatory_field_evidence")
    required_evidence = {
        "record_id",
        "field_name",
        "extraction_method",
        "confidence",
    }
    evidence: dict[str, object] = {
        "available": required_evidence.issubset(evidence_columns),
        "rows": 0,
        "records": 0,
        "record_coverage_percent": None,
        "by_field": [],
        "by_method": [],
    }
    if evidence["available"]:
        aggregate = connection.execute(
            """
            SELECT COUNT(*) AS rows, COUNT(DISTINCT record_id) AS records
            FROM regulatory_field_evidence
            """
        ).fetchone()
        evidence_rows = int(aggregate["rows"] or 0)
        evidence_records = int(aggregate["records"] or 0)
        evidence.update(
            {
                "rows": evidence_rows,
                "records": evidence_records,
                "record_coverage_percent": round(
                    (evidence_records / total) * 100, 2
                )
                if total
                else None,
            }
        )
        evidence["by_field"] = [
            {
                "field": str(row["field_name"] or "sin_campo"),
                "evidence_rows": int(row["evidence_rows"] or 0),
                "records": int(row["records"] or 0),
                "record_coverage_percent": round(
                    (int(row["records"] or 0) / total) * 100, 2
                )
                if total
                else None,
                "average_confidence": round(float(row["average_confidence"]), 4)
                if row["average_confidence"] is not None
                else None,
                "low_confidence": int(row["low_confidence"] or 0),
            }
            for row in connection.execute(
                """
                SELECT field_name, COUNT(*) AS evidence_rows,
                       COUNT(DISTINCT record_id) AS records,
                       AVG(confidence) AS average_confidence,
                       SUM(CASE WHEN confidence IS NULL OR confidence < 0.70
                           THEN 1 ELSE 0 END) AS low_confidence
                FROM regulatory_field_evidence
                GROUP BY field_name
                ORDER BY field_name
                """
            ).fetchall()
        ]
        evidence["by_method"] = [
            {
                "method": str(row["extraction_method"] or "sin_método"),
                "origin": _evidence_origin(row["extraction_method"]),
                "evidence_rows": int(row["evidence_rows"] or 0),
                "average_confidence": round(float(row["average_confidence"]), 4)
                if row["average_confidence"] is not None
                else None,
            }
            for row in connection.execute(
                """
                SELECT extraction_method, COUNT(*) AS evidence_rows,
                       AVG(confidence) AS average_confidence
                FROM regulatory_field_evidence
                GROUP BY extraction_method
                ORDER BY COUNT(*) DESC, extraction_method
                """
            ).fetchall()
        ]

    field_completeness = {
        str(item["key"]): {
            "label": item["label"],
            "present": item["present"],
            "total": total,
            "missing": item["missing"],
            "coverage_percent": item["coverage_percent"],
            "available": item["available"],
        }
        for item in fields
    }
    return {
        "status": "available",
        "records": total,
        "fields": fields,
        "field_completeness": field_completeness,
        "complete_core_records": complete_core,
        "complete_core_percent": round((complete_core / total) * 100, 2)
        if total and all_core_available
        else None,
        "record_confidence": confidence,
        "field_evidence": evidence,
        "extractor_versions": extractor_versions,
    }


def build_regulatory_quality_snapshot(database_path: Path) -> dict:
    """Devuelve métricas compatibles con bases v0.6 y v0.7."""
    if not database_path.exists():
        return _regulatory_quality_snapshot_missing()
    try:
        with connect(database_path) as connection:
            return _regulatory_quality_snapshot(connection)
    except Exception as exc:
        snapshot = _regulatory_quality_snapshot_missing()
        snapshot.update({"status": "error", "error": str(exc)[:500]})
        return snapshot


def _page_numbers(value: str | None) -> list[int]:
    if not value:
        return []
    return [int(item) for item in value.split(",") if item.strip().isdigit()]


def _coverage_groups(
    manifest: list,
    indexed_urls: set[str],
    attribute: str,
) -> list[dict]:
    grouped: dict[object, dict[str, int | float | str]] = {}
    for document in manifest:
        raw_value = getattr(document, attribute, None)
        value = raw_value if raw_value not in (None, "") else "Sin dato"
        row = grouped.setdefault(
            value,
            {
                attribute: value,
                "manifest_documents": 0,
                "indexed_documents": 0,
            },
        )
        row["manifest_documents"] += 1
        if document.url in indexed_urls:
            row["indexed_documents"] += 1

    rows: list[dict] = []
    for row in grouped.values():
        expected = int(row["manifest_documents"])
        indexed = int(row["indexed_documents"])
        rows.append(
            {
                **row,
                "missing_documents": expected - indexed,
                "coverage_percent": round((indexed / expected) * 100, 2)
                if expected
                else 0.0,
            }
        )
    if attribute == "year":
        return sorted(
            rows,
            key=lambda item: (
                isinstance(item[attribute], int),
                item[attribute] if isinstance(item[attribute], int) else -1,
            ),
            reverse=True,
        )
    return sorted(rows, key=lambda item: str(item[attribute]))


def _page_inventory_snapshot(connection, document_rows) -> dict:
    """Audita que cada página física tenga texto o un fallo explícito.

    La consulta no carga los BLOB de texto en memoria; solo comprueba su
    presencia y la metadata necesaria para reconstruir su procedencia.
    """

    rows = connection.execute(
        """
        SELECT p.document_id, p.page_number,
               CASE WHEN p.raw_text_compressed IS NOT NULL
                          AND LENGTH(p.raw_text_compressed) > 0
                          AND p.raw_text_codec = 'zlib-utf8-v1'
                          AND p.text_source IN ('native_pdf', 'ocr')
                          AND p.text_quality IS NOT NULL
                          AND TRIM(COALESCE(p.text_extractor_version, '')) != ''
                    THEN 1 ELSE 0 END AS valid_source,
               CASE WHEN p.raw_text_compressed IS NULL
                          AND p.raw_text_codec IS NULL
                          AND p.text_source IS NULL
                          AND p.text_quality = 0
                          AND TRIM(COALESCE(p.extraction_error, '')) != ''
                          AND TRIM(COALESCE(p.text_extractor_version, '')) != ''
                    THEN 1 ELSE 0 END AS valid_failure,
               CASE WHEN TRIM(COALESCE(p.extraction_error, '')) != ''
                    THEN 1 ELSE 0 END AS has_error
        FROM pages p
        ORDER BY p.document_id, p.page_number
        """
    ).fetchall()
    pages_by_document: dict[int, dict[int, object]] = {}
    for row in rows:
        pages_by_document.setdefault(int(row["document_id"]), {})[
            int(row["page_number"])
        ] = row

    pages_accounted = 0
    pages_with_source_text = 0
    pages_with_failed_extraction = 0
    pages_with_extraction_error = 0
    pages_unaccounted = 0
    mismatches: list[dict] = []
    fatal_mismatches: list[dict] = []
    extraction_errors: list[dict] = []

    for document in document_rows:
        document_id = int(document["id"])
        expected_count = max(0, int(document["pdf_page_count"] or 0))
        expected = set(range(1, expected_count + 1))
        document_pages = pages_by_document.get(document_id, {})
        recorded = set(document_pages)
        missing = sorted(expected - recorded)
        unexpected = sorted(recorded - expected)
        invalid = sorted(
            page_number
            for page_number in expected & recorded
            if not (
                bool(document_pages[page_number]["valid_source"])
                or bool(document_pages[page_number]["valid_failure"])
            )
        )
        error_pages = sorted(
            page_number
            for page_number, row in document_pages.items()
            if bool(row["has_error"])
        )
        if error_pages:
            extraction_errors.append(
                {"title": document["title"], "pages": error_pages}
            )

        for page_number in expected & recorded:
            row = document_pages[page_number]
            if bool(row["valid_source"]):
                pages_with_source_text += 1
                pages_accounted += 1
            elif bool(row["valid_failure"]):
                pages_with_failed_extraction += 1
                pages_accounted += 1
            if bool(row["has_error"]):
                pages_with_extraction_error += 1

        pages_unaccounted += len(missing) + len(unexpected) + len(invalid)
        if missing or unexpected or invalid:
            detail = {
                "title": document["title"],
                "expected_pages": expected_count,
                "recorded_pages": len(recorded),
                "missing_pages": missing,
                "unexpected_pages": unexpected,
                "unaccounted_pages": invalid,
            }
            mismatches.append(detail)
            if bool(document["page_inventory_complete"]):
                fatal_mismatches.append(detail)

    expected_pages = sum(
        max(0, int(document["pdf_page_count"] or 0))
        for document in document_rows
    )
    pending = [
        document["title"]
        for document in document_rows
        if not bool(document["page_inventory_complete"])
    ]
    result = {
        "expected_pdf_pages": expected_pages,
        "pages_recorded": len(rows),
        "pages_accounted": pages_accounted,
        "pages_with_source_text": pages_with_source_text,
        "pages_with_failed_extraction": pages_with_failed_extraction,
        "pages_with_extraction_error": pages_with_extraction_error,
        "pages_unaccounted": pages_unaccounted,
        "inventory_coverage_percent": (
            round((pages_accounted / expected_pages) * 100, 2)
            if expected_pages
            else 0.0
        ),
        "page_inventory_pending": pending,
        "page_inventory_mismatches": mismatches,
        "page_inventory_errors": fatal_mismatches,
        "page_extraction_errors": extraction_errors,
    }
    result["inventory_complete"] = bool(
        not pending
        and not mismatches
        and not pages_unaccounted
        and pages_accounted == expected_pages
    )
    return result


def build_page_inventory_snapshot(database_path: Path) -> dict:
    """Expone la auditoría de páginas para los gates de publicación."""

    empty = {
        "available": False,
        "expected_pdf_pages": 0,
        "pages_recorded": 0,
        "pages_accounted": 0,
        "pages_with_source_text": 0,
        "pages_with_failed_extraction": 0,
        "pages_with_extraction_error": 0,
        "pages_unaccounted": 0,
        "inventory_coverage_percent": 0.0,
        "page_inventory_pending": [],
        "page_inventory_mismatches": [],
        "page_inventory_errors": [],
        "page_extraction_errors": [],
        "inventory_complete": False,
    }
    if not database_path.exists():
        return empty
    try:
        with connect(database_path) as connection:
            required_page_columns = {
                "page_number",
                "raw_text_compressed",
                "raw_text_codec",
                "text_source",
                "text_quality",
                "extraction_error",
                "text_extractor_version",
            }
            required_document_columns = {
                "id",
                "title",
                "pdf_page_count",
                "page_inventory_complete",
            }
            if not required_page_columns.issubset(
                _table_columns(connection, "pages")
            ) or not required_document_columns.issubset(
                _table_columns(connection, "documents")
            ):
                return empty
            documents = connection.execute(
                """
                SELECT id, title, pdf_page_count, page_inventory_complete
                FROM documents ORDER BY id
                """
            ).fetchall()
            return {
                "available": True,
                **_page_inventory_snapshot(connection, documents),
            }
    except Exception as exc:
        return {**empty, "error": str(exc)[:500]}


def build_integrity_report(
    database_path: Path,
    manifest_path: Path,
    allowed_hosts: tuple[str, ...],
) -> dict:
    manifest = load_manifest(manifest_path, allowed_hosts)
    generated_at = datetime.now(timezone.utc).isoformat()
    if not database_path.exists():
        indexed_urls: set[str] = set()
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
            "page_inventory_mismatches": [],
            "page_inventory_errors": [],
            "page_extraction_errors": [],
            "sqlite_integrity": "missing_database",
            "database_bytes": 0,
            "database_mib": 0.0,
            "pages_indexed": 0,
            "pages_recorded": 0,
            "pages_accounted": 0,
            "pages_with_source_text": 0,
            "pages_with_failed_extraction": 0,
            "pages_with_extraction_error": 0,
            "pages_unaccounted": 0,
            "pdf_pages": 0,
            "chunks": 0,
            "fts_rows": 0,
            "text_page_coverage_percent": 0.0,
            "inventory_coverage_percent": 0.0,
            "coverage_by_year": _coverage_groups(
                manifest,
                indexed_urls,
                "year",
            ),
            "coverage_by_section": _coverage_groups(
                manifest,
                indexed_urls,
                "section",
            ),
            "regulatory_records": 0,
            "regulatory_documents": 0,
            "regulatory_extraction_pending": [],
            "regulatory_extraction_errors": [],
            "regulatory_quality": {},
            "regulatory_quality_snapshot": _regulatory_quality_snapshot_missing(),
            "field_completeness": {},
            "foreign_key_errors": 0,
            "fts_rowid_mismatches": 0,
            "duplicate_document_hashes": [],
        }

    with connect(database_path) as connection:
        sqlite_integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_key_errors = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        counts = {
            table: int(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
            for table in ("documents", "pages", "chunks", "chunks_fts")
        }
        document_rows = connection.execute(
            """
            SELECT id, manifest_url, title, year, section, pdf_page_count,
                   indexed_page_count, ocr_candidate_pages,
                   page_inventory_complete
            FROM documents
            ORDER BY year, acta_number, part, title
            """
        ).fetchall()
        page_inventory = _page_inventory_snapshot(connection, document_rows)
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
        fts_rowid_mismatches = int(
            connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM chunks c
                     LEFT JOIN chunks_fts f ON f.rowid = c.id
                     WHERE f.rowid IS NULL)
                  + (SELECT COUNT(*) FROM chunks_fts f
                     LEFT JOIN chunks c ON c.id = f.rowid
                     WHERE c.id IS NULL)
                """
            ).fetchone()[0]
        )
        duplicate_document_hashes = [
            {
                "document_hash": row["document_hash"],
                "documents": int(row["documents"]),
                "titles": str(row["titles"]).split(" || "),
            }
            for row in connection.execute(
                """
                SELECT document_hash, COUNT(*) AS documents,
                       GROUP_CONCAT(title, ' || ') AS titles
                FROM documents
                WHERE document_hash != ''
                GROUP BY document_hash
                HAVING COUNT(*) > 1
                ORDER BY COUNT(*) DESC, document_hash
                """
            ).fetchall()
        ]
        feature_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        regulatory_records = 0
        regulatory_documents = 0
        regulatory_pending: list[str] = []
        regulatory_errors: list[dict] = []
        regulatory_quality: dict[str, int] = {}
        regulatory_quality_snapshot = _regulatory_quality_snapshot(connection)
        if {"regulatory_records", "document_extractions"}.issubset(feature_tables):
            regulatory_records = int(
                connection.execute("SELECT COUNT(*) FROM regulatory_records").fetchone()[0]
            )
            regulatory_documents = int(
                connection.execute(
                    "SELECT COUNT(*) FROM document_extractions "
                    "WHERE status = 'complete'"
                ).fetchone()[0]
            )
            regulatory_pending = [
                row["title"]
                for row in connection.execute(
                    """
                    SELECT d.title
                    FROM documents d
                    LEFT JOIN document_extractions e ON e.document_id = d.id
                    WHERE e.document_id IS NULL
                    ORDER BY d.year, d.acta_number, d.part
                    """
                ).fetchall()
            ]
            regulatory_errors = [
                {"title": row["title"], "error": row["error_message"]}
                for row in connection.execute(
                    """
                    SELECT d.title, e.error_message
                    FROM document_extractions e
                    JOIN documents d ON d.id = e.document_id
                    WHERE e.status = 'error'
                    ORDER BY d.year, d.acta_number, d.part
                    """
                ).fetchall()
            ]
            record_columns = _table_columns(connection, "regulatory_records")
            regulatory_quality = {"total": regulatory_records}
            legacy_metrics = {
                "without_uid": (
                    "decision_uid",
                    "decision_uid IS NULL OR TRIM(decision_uid) = ''",
                ),
                "without_numeral": (
                    "numeral",
                    "numeral IS NULL OR TRIM(numeral) = ''",
                ),
                "without_session_date": (
                    "session_date",
                    "session_date IS NULL OR TRIM(session_date) = ''",
                ),
                "unclassified_request_type": (
                    "request_type_code",
                    "request_type_code IS NULL OR TRIM(request_type_code) = '' "
                    "OR request_type_code = 'otra_solicitud'",
                ),
            }
            for key, (column, condition) in legacy_metrics.items():
                regulatory_quality[key] = (
                    int(
                        connection.execute(
                            "SELECT COUNT(*) FROM regulatory_records WHERE "
                            + condition
                        ).fetchone()[0]
                    )
                    if column in record_columns
                    else 0
                )
            confidence = regulatory_quality_snapshot.get("record_confidence") or {}
            regulatory_quality["low_confidence"] = int(confidence.get("low") or 0)
            for field in regulatory_quality_snapshot.get("fields") or []:
                regulatory_quality[f"without_{field['key']}"] = int(field["missing"])

    manifest_by_url = {document.url: document.title for document in manifest}
    indexed_by_url = {row["manifest_url"]: row["title"] for row in document_rows}
    indexed_urls = set(indexed_by_url)
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
    page_inventory_pending = page_inventory["page_inventory_pending"]
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
            foreign_key_errors,
            fts_rowid_mismatches,
            bool(page_inventory["page_inventory_errors"]),
        )
    )
    status = (
        "error"
        if has_error
        else (
            "warning"
            if (
                ocr_candidates
                or page_inventory_pending
                or page_inventory["page_extraction_errors"]
                or regulatory_pending
                or regulatory_errors
            )
            else "ok"
        )
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
        "page_inventory_mismatches": page_inventory[
            "page_inventory_mismatches"
        ],
        "page_inventory_errors": page_inventory["page_inventory_errors"],
        "page_extraction_errors": page_inventory["page_extraction_errors"],
        "sqlite_integrity": sqlite_integrity,
        "database_bytes": database_bytes,
        "database_mib": round(database_bytes / (1024 * 1024), 1),
        "pages_indexed": indexed_pages,
        "pages_recorded": page_inventory["pages_recorded"],
        "pages_accounted": page_inventory["pages_accounted"],
        "pages_with_source_text": page_inventory["pages_with_source_text"],
        "pages_with_failed_extraction": page_inventory[
            "pages_with_failed_extraction"
        ],
        "pages_with_extraction_error": page_inventory[
            "pages_with_extraction_error"
        ],
        "pages_unaccounted": page_inventory["pages_unaccounted"],
        "pdf_pages": pdf_pages,
        "chunks": counts["chunks"],
        "fts_rows": counts["chunks_fts"],
        "text_page_coverage_percent": (
            round((indexed_pages / pdf_pages) * 100, 2) if pdf_pages else 0.0
        ),
        "inventory_coverage_percent": page_inventory[
            "inventory_coverage_percent"
        ],
        "coverage_by_year": _coverage_groups(manifest, indexed_urls, "year"),
        "coverage_by_section": _coverage_groups(
            manifest,
            indexed_urls,
            "section",
        ),
        "regulatory_records": regulatory_records,
        "regulatory_documents": regulatory_documents,
        "regulatory_extraction_pending": regulatory_pending,
        "regulatory_extraction_errors": regulatory_errors,
        "regulatory_quality": regulatory_quality,
        "regulatory_quality_snapshot": regulatory_quality_snapshot,
        "field_completeness": regulatory_quality_snapshot.get(
            "field_completeness", {}
        ),
        "foreign_key_errors": foreign_key_errors,
        "fts_rowid_mismatches": fts_rowid_mismatches,
        "duplicate_document_hashes": duplicate_document_hashes,
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
