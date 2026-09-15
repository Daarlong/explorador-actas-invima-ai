"""Valida técnicamente el corpus publicado sin calificar sus decisiones.

Las sondas obligatorias se derivan de valores que ya existen en ``actas.db``.
Por ello prueban contratos de almacenamiento, recuperación y trazabilidad, no
afirman que un resultado regulatorio sea correcto. Los casos explícitos que un
equipo añada al JSON se registran como orientativos y nunca cambian el código
de salida.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import urlsplit
from zipfile import BadZipFile, ZipFile

from services.ann import ann_index_status
from services.database import DATABASE_SCHEMA_VERSION, database_schema_version
from services.database_package import package_manifest_path
from services.exports import SEARCH_EXPORT_COLUMNS, export_search_results
from services.manifest import load_manifest
from services.models import SearchResult
from services.search import SEARCH_FIELD_SCOPES, search_corpus_page
from services.semantic import semantic_index_status
from services.text_utils import normalize_phrase, tokenize_query


REPORT_FORMAT_VERSION = 1
DEFAULT_CONFIG_PATH = Path(__file__).with_name("search-validation-cases.json")
_SQLITE_HEADER = b"SQLite format 3\x00"
_FIELD_COLUMNS = {
    "request": "request_text",
    "concept": "concept_text",
    "product": "product_name",
    "active_ingredient": "active_ingredient",
    "interested_party": "interested_party",
    "expediente": "expediente",
    "radicado": "radicado",
    "outcome": "outcome_code",
}


@dataclass(frozen=True)
class ValidationCheck:
    check_id: str
    category: str
    status: str
    severity: str
    message: str
    details: dict[str, Any]


class ValidationRecorder:
    def __init__(self) -> None:
        self.checks: list[ValidationCheck] = []

    def pass_check(
        self,
        check_id: str,
        category: str,
        message: str,
        **details: Any,
    ) -> None:
        self.checks.append(
            ValidationCheck(check_id, category, "passed", "required", message, details)
        )

    def fail(
        self,
        check_id: str,
        category: str,
        message: str,
        **details: Any,
    ) -> None:
        self.checks.append(
            ValidationCheck(check_id, category, "failed", "required", message, details)
        )

    def advisory(
        self,
        check_id: str,
        category: str,
        message: str,
        *,
        status: str = "warning",
        **details: Any,
    ) -> None:
        self.checks.append(
            ValidationCheck(check_id, category, status, "advisory", message, details)
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _open_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_validation_config(path: Path) -> dict[str, Any]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"No fue posible leer la configuración: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"La configuración JSON no es válida: {exc}") from exc
    if not isinstance(config, dict) or config.get("format_version") != 1:
        raise ValueError("La configuración debe usar format_version 1")
    for section in ("schema", "packages", "semantic", "probes"):
        if not isinstance(config.get(section), dict):
            raise ValueError(f"Falta la sección de configuración: {section}")
    if "manifest" in config and not isinstance(config["manifest"], dict):
        raise ValueError("manifest debe ser un objeto de configuración")
    explicit_cases = config.get("explicit_cases", [])
    if not isinstance(explicit_cases, list):
        raise ValueError("explicit_cases debe ser una lista")
    return config


def resolve_config_path(path: Path, allowed_root: Path | None = None) -> Path:
    """Restringe una ruta controlada por workflow al checkout actual."""

    path = Path(path)
    if allowed_root is None:
        return path
    root = Path(allowed_root).resolve()
    if path.is_absolute():
        raise ValueError("La ruta de configuración del workflow debe ser relativa")
    resolved = (root / path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            "La ruta de configuración debe permanecer dentro del checkout"
        ) from exc
    if resolved.suffix.lower() != ".json":
        raise ValueError("La configuración del workflow debe ser un archivo JSON")
    return resolved


def resolve_manifest_path(path: Path, allowed_root: Path | None = None) -> Path:
    """Restringe el manifiesto elegido en Actions al checkout actual."""

    path = Path(path)
    if allowed_root is None:
        return path
    root = Path(allowed_root).resolve()
    if path.is_absolute():
        raise ValueError("La ruta del manifiesto del workflow debe ser relativa")
    resolved = (root / path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("El manifiesto debe permanecer dentro del checkout") from exc
    if resolved.suffix.lower() != ".csv":
        raise ValueError("El manifiesto del workflow debe ser un archivo CSV")
    return resolved


def _validate_package(
    database_path: Path,
    *,
    verify_sha256: bool,
) -> tuple[bool, str, dict[str, Any]]:
    manifest_path = package_manifest_path(database_path)
    if not manifest_path.is_file():
        return False, "No existe el manifiesto del paquete", {
            "manifest": str(manifest_path)
        }
    try:
        package = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"El manifiesto del paquete no es válido: {exc}", {}
    required = {
        "format_version",
        "database_file",
        "database_bytes",
        "database_sha256",
        "compressed_bytes",
        "parts",
    }
    if not required.issubset(package):
        return False, "El manifiesto no declara todos los campos obligatorios", {
            "missing": sorted(required.difference(package))
        }
    if package.get("format_version") != 1:
        return False, "La versión del paquete no es compatible", {
            "format_version": package.get("format_version")
        }
    if package.get("database_file") != database_path.name:
        return False, "El manifiesto pertenece a otra base", {}
    parts = package.get("parts")
    if not isinstance(parts, list) or not parts:
        return False, "El paquete no contiene partes", {}

    compressed_bytes = 0
    for item in parts:
        if not isinstance(item, dict):
            return False, "Una parte del paquete no es válida", {}
        name = str(item.get("name", ""))
        if Path(name).name != name or not name.startswith(
            f"{database_path.name}.gz.part-"
        ):
            return False, "El paquete declara un nombre de parte no permitido", {
                "part": name
            }
        part_path = database_path.parent / name
        if not part_path.is_file():
            return False, "Falta una parte del paquete", {"part": name}
        size = part_path.stat().st_size
        if size != int(item.get("bytes", -1)):
            return False, "El tamaño de una parte no coincide", {"part": name}
        if verify_sha256 and _sha256(part_path) != str(item.get("sha256", "")):
            return False, "La huella SHA-256 de una parte no coincide", {
                "part": name
            }
        compressed_bytes += size

    if compressed_bytes != int(package.get("compressed_bytes", -1)):
        return False, "El tamaño comprimido declarado no coincide", {}
    if not database_path.is_file():
        return False, "La base restaurada no existe", {
            "database": str(database_path)
        }
    if database_path.stat().st_size != int(package.get("database_bytes", -1)):
        return False, "El tamaño de la base restaurada no coincide", {}
    if verify_sha256 and _sha256(database_path) != str(
        package.get("database_sha256", "")
    ):
        return False, "La base restaurada no coincide con su paquete", {}
    with database_path.open("rb") as handle:
        if handle.read(len(_SQLITE_HEADER)) != _SQLITE_HEADER:
            return False, "El paquete restaurado no contiene SQLite", {}
    return True, "Paquete completo y coherente con la base restaurada", {
        "parts": len(parts),
        "database_bytes": database_path.stat().st_size,
        "sha256_verified": verify_sha256,
    }


def _schema_checks(
    recorder: ValidationRecorder,
    database_path: Path,
    config: dict[str, Any],
) -> dict[str, int]:
    if not database_path.is_file():
        recorder.fail("schema.database", "schema", "No existe actas.db")
        return {}
    expected_version = int(config.get("expected_version", DATABASE_SCHEMA_VERSION))
    actual_version = database_schema_version(database_path)
    if actual_version == expected_version:
        recorder.pass_check(
            "schema.version",
            "schema",
            f"Esquema {actual_version} disponible",
            expected=expected_version,
            actual=actual_version,
        )
    else:
        recorder.fail(
            "schema.version",
            "schema",
            "La versión del esquema no coincide",
            expected=expected_version,
            actual=actual_version,
        )

    counts: dict[str, int] = {}
    try:
        with _open_read_only(database_path) as connection:
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                )
            }
            missing = sorted(set(config.get("required_tables", ())).difference(tables))
            if integrity.lower() == "ok":
                recorder.pass_check(
                    "schema.integrity", "schema", "SQLite informó integridad completa"
                )
            else:
                recorder.fail(
                    "schema.integrity",
                    "schema",
                    "SQLite informó un problema de integridad",
                    result=integrity,
                )
            if missing:
                recorder.fail(
                    "schema.tables",
                    "schema",
                    "Faltan tablas obligatorias",
                    missing=missing,
                )
            else:
                recorder.pass_check(
                    "schema.tables",
                    "schema",
                    "Todas las tablas obligatorias están presentes",
                    tables=list(config.get("required_tables", ())),
                )

            allowed_hosts = {
                str(host).strip().lower()
                for host in config.get("allowed_document_hosts", ())
                if str(host).strip()
            }
            invalid_urls: list[dict[str, str]] = []
            for row in connection.execute(
                "SELECT id, url, manifest_url FROM documents ORDER BY id"
            ):
                for field in ("url", "manifest_url"):
                    raw_url = str(row[field] or "").strip()
                    parsed = urlsplit(raw_url)
                    hostname = str(parsed.hostname or "").lower()
                    if (
                        parsed.scheme not in {"http", "https"}
                        or not hostname
                        or parsed.username is not None
                        or parsed.password is not None
                        or (allowed_hosts and hostname not in allowed_hosts)
                    ):
                        invalid_urls.append(
                            {
                                "document_id": str(row["id"]),
                                "field": field,
                                "url": raw_url[:300],
                            }
                        )
                        if len(invalid_urls) >= 20:
                            break
                if len(invalid_urls) >= 20:
                    break
            if invalid_urls:
                recorder.fail(
                    "schema.urls",
                    "schema",
                    "Hay enlaces documentales con formato u origen no permitido",
                    invalid=invalid_urls,
                    allowed_hosts=sorted(allowed_hosts),
                )
            else:
                recorder.pass_check(
                    "schema.urls",
                    "schema",
                    "Los enlaces documentales tienen origen y formato válidos",
                    allowed_hosts=sorted(allowed_hosts),
                )

            coverage_config = config.get("temporal_coverage", {})
            if coverage_config:
                coverage = connection.execute(
                    """
                    SELECT MIN(year) AS minimum_year, MAX(year) AS maximum_year,
                           COUNT(DISTINCT year) AS distinct_years,
                           SUM(CASE WHEN year IS NULL THEN 1 ELSE 0 END) AS without_year
                    FROM documents
                    """
                ).fetchone()
                minimum_year = (
                    int(coverage["minimum_year"])
                    if coverage["minimum_year"] is not None
                    else None
                )
                maximum_year = (
                    int(coverage["maximum_year"])
                    if coverage["maximum_year"] is not None
                    else None
                )
                inclusive_span = (
                    maximum_year - minimum_year + 1
                    if minimum_year is not None and maximum_year is not None
                    else 0
                )
                start_limit = int(
                    coverage_config.get("start_year_at_most", minimum_year or 9999)
                )
                end_limit = int(
                    coverage_config.get("end_year_at_least", maximum_year or 0)
                )
                minimum_span = int(
                    coverage_config.get("minimum_span_years", 1)
                )
                minimum_distinct = int(
                    coverage_config.get("minimum_distinct_years", 1)
                )
                distinct_years = int(coverage["distinct_years"] or 0)
                coverage_valid = bool(
                    minimum_year is not None
                    and maximum_year is not None
                    and minimum_year <= start_limit
                    and maximum_year >= end_limit
                    and inclusive_span >= minimum_span
                    and distinct_years >= minimum_distinct
                )
                coverage_details = {
                    "minimum_year": minimum_year,
                    "maximum_year": maximum_year,
                    "inclusive_span_years": inclusive_span,
                    "distinct_years": distinct_years,
                    "documents_without_year": int(coverage["without_year"] or 0),
                    "expected_start_at_most": start_limit,
                    "expected_end_at_least": end_limit,
                    "expected_minimum_span": minimum_span,
                    "expected_minimum_distinct_years": minimum_distinct,
                }
                if coverage_valid:
                    recorder.pass_check(
                        "schema.temporal_coverage",
                        "schema",
                        "El corpus cubre el intervalo temporal configurado",
                        **coverage_details,
                    )
                else:
                    recorder.fail(
                        "schema.temporal_coverage",
                        "schema",
                        "El corpus no cubre el intervalo temporal configurado",
                        **coverage_details,
                    )

            for table, minimum in config.get("minimum_rows", {}).items():
                if table not in tables or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
                    counts[str(table)] = 0
                    continue
                counts[str(table)] = int(
                    connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                )
                if counts[str(table)] < int(minimum):
                    recorder.fail(
                        f"schema.rows.{table}",
                        "schema",
                        f"{table} no alcanza el mínimo configurado",
                        rows=counts[str(table)],
                        minimum=int(minimum),
                    )
                else:
                    recorder.pass_check(
                        f"schema.rows.{table}",
                        "schema",
                        f"{table} contiene datos",
                        rows=counts[str(table)],
                        minimum=int(minimum),
                    )

            orphan_chunks = int(
                connection.execute(
                    "SELECT COUNT(*) FROM chunks c "
                    "LEFT JOIN pages p ON p.id=c.page_id WHERE p.id IS NULL"
                ).fetchone()[0]
            )
            orphan_pages = int(
                connection.execute(
                    "SELECT COUNT(*) FROM pages p "
                    "LEFT JOIN documents d ON d.id=p.document_id WHERE d.id IS NULL"
                ).fetchone()[0]
            )
            if orphan_chunks or orphan_pages:
                recorder.fail(
                    "schema.relationships",
                    "schema",
                    "Hay páginas o fragmentos sin documento fuente",
                    orphan_pages=orphan_pages,
                    orphan_chunks=orphan_chunks,
                )
            else:
                recorder.pass_check(
                    "schema.relationships",
                    "schema",
                    "Las relaciones documento–página–fragmento son válidas",
                    orphan_pages=0,
                    orphan_chunks=0,
                )

            fts_config = config.get("fts_cardinality", {})
            if fts_config.get("enabled", False):
                chunks = int(
                    connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
                )
                chunk_fts = int(
                    connection.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
                )
                records = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM regulatory_records"
                    ).fetchone()[0]
                )
                record_fts = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM regulatory_records_fts"
                    ).fetchone()[0]
                )
                if chunks == chunk_fts and records == record_fts:
                    recorder.pass_check(
                        "schema.fts_cardinality",
                        "schema",
                        "Los índices FTS cubren exactamente sus entidades fuente",
                        chunks=chunks,
                        chunks_fts=chunk_fts,
                        regulatory_records=records,
                        regulatory_records_fts=record_fts,
                    )
                else:
                    recorder.fail(
                        "schema.fts_cardinality",
                        "schema",
                        "La cardinalidad de uno o más índices FTS no coincide",
                        chunks=chunks,
                        chunks_fts=chunk_fts,
                        regulatory_records=records,
                        regulatory_records_fts=record_fts,
                    )

            relationship_config = config.get("regulatory_relationships", {})
            if relationship_config.get("enabled", False):
                orphan_records = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM regulatory_records rr "
                        "LEFT JOIN documents d ON d.id=rr.document_id "
                        "WHERE d.id IS NULL"
                    ).fetchone()[0]
                )
                orphan_evidence = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM regulatory_field_evidence e "
                        "LEFT JOIN regulatory_records rr ON rr.id=e.record_id "
                        "WHERE rr.id IS NULL"
                    ).fetchone()[0]
                )
                if orphan_records or orphan_evidence:
                    recorder.fail(
                        "schema.regulatory_relationships",
                        "schema",
                        "Hay fichas o evidencias regulatorias sin entidad fuente",
                        orphan_records=orphan_records,
                        orphan_evidence=orphan_evidence,
                    )
                else:
                    recorder.pass_check(
                        "schema.regulatory_relationships",
                        "schema",
                        "Las fichas y sus evidencias conservan relaciones válidas",
                        orphan_records=0,
                        orphan_evidence=0,
                    )
    except sqlite3.Error as exc:
        recorder.fail(
            "schema.read", "schema", f"No fue posible inspeccionar actas.db: {exc}"
        )
    return counts


def _package_checks(
    recorder: ValidationRecorder,
    data_dir: Path,
    config: dict[str, Any],
) -> None:
    verify_sha256 = bool(config.get("verify_sha256", True))
    for name in config.get("required", ()):
        database_path = data_dir / str(name)
        try:
            valid, message, details = _validate_package(
                database_path, verify_sha256=verify_sha256
            )
        except (OSError, TypeError, ValueError) as exc:
            valid, message, details = False, str(exc), {}
        if valid:
            recorder.pass_check(
                f"package.{name}", "packages", message, database=name, **details
            )
        else:
            recorder.fail(
                f"package.{name}", "packages", message, database=name, **details
            )


def _manifest_identity(catalog_id: object, manifest_url: object) -> str:
    catalog = str(catalog_id or "").strip()
    return f"catalog:{catalog}" if catalog else f"url:{str(manifest_url or '').strip()}"


def _manifest_checks(
    recorder: ValidationRecorder,
    database_path: Path,
    manifest_path: Path | None,
    config: dict[str, Any] | None,
    *,
    allowed_hosts: Sequence[str],
) -> None:
    if not config or not config.get("enabled", False):
        return
    if manifest_path is None:
        recorder.fail(
            "manifest.coverage",
            "manifest",
            "La cobertura contra manifiesto está activa pero no se indicó su ruta",
            expected=0,
            indexed=0,
            missing=0,
            unexpected=0,
            coverage_percent=None,
        )
        return
    start_year = int(config.get("start_year", 2013))
    try:
        manifest_documents = [
            document
            for document in load_manifest(
                manifest_path,
                tuple(str(host).strip().lower() for host in allowed_hosts),
            )
            if document.url and document.year is not None and document.year >= start_year
        ]
    except (OSError, TypeError, ValueError) as exc:
        recorder.fail(
            "manifest.coverage",
            "manifest",
            f"No fue posible cargar el manifiesto configurado: {exc}",
            manifest=str(manifest_path),
            start_year=start_year,
        )
        return
    if not manifest_documents:
        recorder.fail(
            "manifest.coverage",
            "manifest",
            "El manifiesto no contiene documentos indexables en el rango configurado",
            manifest=str(manifest_path),
            start_year=start_year,
            expected=0,
            indexed=0,
            missing=0,
            unexpected=0,
            coverage_percent=None,
        )
        return

    expected: dict[str, Any] = {}
    expected_catalogs: set[str] = set()
    expected_urls: set[str] = set()
    for document in manifest_documents:
        identity = _manifest_identity(document.catalog_id, document.url)
        expected.setdefault(identity, document)
        if document.catalog_id:
            expected_catalogs.add(str(document.catalog_id).strip())
        expected_urls.add(str(document.url).strip())

    manifest_years = sorted(
        {
            int(document.year)
            for document in expected.values()
            if document.year is not None
        }
    )
    manifest_minimum_year = manifest_years[0] if manifest_years else None
    manifest_maximum_year = manifest_years[-1] if manifest_years else None
    manifest_distinct_years = len(manifest_years)
    manifest_inclusive_span = (
        manifest_maximum_year - manifest_minimum_year + 1
        if manifest_minimum_year is not None and manifest_maximum_year is not None
        else 0
    )
    temporal_config = config.get("temporal_coverage")
    if temporal_config is None:
        # También acepta las opciones en el nivel raíz para configuraciones
        # sencillas; si no se declaran, el control queda desactivado.
        temporal_keys = (
            "start_year_at_most",
            "end_year_at_least",
            "minimum_distinct_years",
        )
        temporal_config = {
            key: config[key] for key in temporal_keys if key in config
        }
    if temporal_config:
        try:
            expected_start = int(
                temporal_config.get(
                    "start_year_at_most", manifest_minimum_year or 9999
                )
            )
            expected_end = int(
                temporal_config.get(
                    "end_year_at_least", manifest_maximum_year or 0
                )
            )
            expected_distinct = int(
                temporal_config.get("minimum_distinct_years", 1)
            )
        except (AttributeError, TypeError, ValueError) as exc:
            recorder.fail(
                "manifest.temporal_coverage",
                "manifest",
                f"La cobertura temporal del manifiesto no es válida: {exc}",
                minimum_year=manifest_minimum_year,
                maximum_year=manifest_maximum_year,
                distinct_years=manifest_distinct_years,
            )
        else:
            temporal_details = {
                "minimum_year": manifest_minimum_year,
                "maximum_year": manifest_maximum_year,
                "inclusive_span_years": manifest_inclusive_span,
                "distinct_years": manifest_distinct_years,
                "expected_start_at_most": expected_start,
                "expected_end_at_least": expected_end,
                "expected_minimum_distinct_years": expected_distinct,
            }
            temporal_valid = bool(
                manifest_minimum_year is not None
                and manifest_maximum_year is not None
                and manifest_minimum_year <= expected_start
                and manifest_maximum_year >= expected_end
                and manifest_distinct_years >= expected_distinct
            )
            if temporal_valid:
                recorder.pass_check(
                    "manifest.temporal_coverage",
                    "manifest",
                    "El inventario indexable cubre el rango temporal configurado",
                    **temporal_details,
                )
            else:
                recorder.fail(
                    "manifest.temporal_coverage",
                    "manifest",
                    "El inventario indexable no cubre el rango temporal configurado",
                    **temporal_details,
                )

    try:
        with _open_read_only(database_path) as connection:
            indexed_rows = connection.execute(
                """
                SELECT d.id, d.catalog_id, d.manifest_url, d.title, d.year,
                       d.indexed_page_count, d.page_inventory_complete,
                       EXISTS(
                           SELECT 1
                           FROM pages AS p
                           JOIN chunks AS c ON c.page_id = p.id
                           WHERE p.document_id = d.id
                             AND LENGTH(TRIM(c.text)) > 0
                       ) AS has_searchable_content
                FROM documents AS d
                WHERE d.year >= ?
                ORDER BY d.id
                """,
                (start_year,),
            ).fetchall()
    except sqlite3.Error as exc:
        recorder.fail(
            "manifest.coverage",
            "manifest",
            f"No fue posible comparar actas.db con el manifiesto: {exc}",
        )
        return

    consultable_rows = [
        row
        for row in indexed_rows
        if int(row["indexed_page_count"] or 0) > 0
        and int(row["page_inventory_complete"] or 0) == 1
        and int(row["has_searchable_content"] or 0) == 1
    ]
    indexed_catalogs = {
        str(row["catalog_id"]).strip()
        for row in consultable_rows
        if str(row["catalog_id"] or "").strip()
    }
    indexed_urls = {
        str(row["manifest_url"]).strip()
        for row in consultable_rows
        if str(row["manifest_url"] or "").strip()
    }

    missing: list[dict[str, Any]] = []
    unsearchable: list[dict[str, Any]] = []
    matched = 0
    for identity, document in expected.items():
        catalog_id = str(document.catalog_id or "").strip()
        url = str(document.url).strip()
        if (catalog_id and catalog_id in indexed_catalogs) or url in indexed_urls:
            matched += 1
            continue
        missing_entry = {
            "identity": identity,
            "title": document.title,
            "year": document.year,
            "url": url,
            "reason": "not_indexed",
        }
        unusable_row = next(
            (
                row
                for row in indexed_rows
                if (
                    catalog_id
                    and catalog_id == str(row["catalog_id"] or "").strip()
                )
                or url == str(row["manifest_url"] or "").strip()
            ),
            None,
        )
        if unusable_row is not None:
            missing_entry["reason"] = "indexed_but_not_searchable"
            unsearchable.append(
                {
                    **missing_entry,
                    "document_id": int(unusable_row["id"]),
                    "indexed_page_count": int(
                        unusable_row["indexed_page_count"] or 0
                    ),
                    "page_inventory_complete": bool(
                        unusable_row["page_inventory_complete"]
                    ),
                    "has_searchable_content": bool(
                        unusable_row["has_searchable_content"]
                    ),
                }
            )
        missing.append(missing_entry)

    unexpected: list[dict[str, Any]] = []
    for row in consultable_rows:
        catalog_id = str(row["catalog_id"] or "").strip()
        url = str(row["manifest_url"] or "").strip()
        if (catalog_id and catalog_id in expected_catalogs) or url in expected_urls:
            continue
        unexpected.append(
            {
                "identity": _manifest_identity(catalog_id, url),
                "document_id": int(row["id"]),
                "title": str(row["title"]),
                "year": row["year"],
                "url": url,
            }
        )

    expected_count = len(expected)
    coverage_percent = round((matched / expected_count) * 100, 4)
    details = {
        "manifest": str(manifest_path),
        "start_year": start_year,
        "manifest_minimum_year": manifest_minimum_year,
        "manifest_maximum_year": manifest_maximum_year,
        "manifest_distinct_years": manifest_distinct_years,
        "manifest_inclusive_span_years": manifest_inclusive_span,
        "expected": expected_count,
        "indexed": len(consultable_rows),
        "indexed_rows_total": len(indexed_rows),
        "matched": matched,
        "missing": len(missing),
        "unsearchable": len(unsearchable),
        "unexpected": len(unexpected),
        "coverage_percent": coverage_percent,
        "missing_examples": missing[:20],
        "unsearchable_examples": unsearchable[:20],
        "unexpected_examples": unexpected[:20],
        "identity_rule": "catalog_id_then_manifest_url",
    }
    if missing and config.get("require_no_missing", True):
        recorder.fail(
            "manifest.coverage",
            "manifest",
            "La base publicada no contiene de forma consultable todos los documentos indexables del manifiesto",
            **details,
        )
    else:
        recorder.pass_check(
            "manifest.coverage",
            "manifest",
            "La base publicada cubre todos los documentos indexables del manifiesto",
            **details,
        )
    if unexpected:
        recorder.advisory(
            "manifest.unexpected",
            "manifest",
            "La base contiene documentos adicionales al manifiesto vigente",
            **details,
        )


def _semantic_checks(
    recorder: ValidationRecorder,
    database_path: Path,
    semantic_path: Path,
    ann_path: Path,
    config: dict[str, Any],
) -> None:
    semantic_required = bool(config.get("required", True))
    state = semantic_index_status(semantic_path, database_path)
    if state.get("available"):
        recorder.pass_check(
            "semantic.identity",
            "semantic",
            "semantic.db corresponde al corpus publicado",
            documents=int(state.get("documents", 0)),
            neural_status=str(state.get("neural_status", "unknown")),
        )
    elif semantic_required:
        recorder.fail(
            "semantic.identity",
            "semantic",
            str(state.get("message") or "semantic.db no está disponible"),
            reason=str(state.get("reason") or "unknown"),
        )
    else:
        recorder.advisory(
            "semantic.identity",
            "semantic",
            str(state.get("message") or "semantic.db no está disponible"),
            status="skipped",
        )

    if config.get("require_neural_ready") and state.get("available"):
        if state.get("neural_status") == "ready":
            recorder.pass_check(
                "semantic.neural", "semantic", "La cobertura neuronal está completa"
            )
        else:
            recorder.fail(
                "semantic.neural",
                "semantic",
                "semantic.db no declara cobertura neuronal completa",
                neural_status=str(state.get("neural_status", "unknown")),
            )

    ann_required = bool(config.get("ann_required", True))
    ann_state = ann_index_status(ann_path, semantic_path)
    if ann_state.get("available"):
        recorder.pass_check(
            "semantic.ann",
            "semantic",
            "semantic-ann.db corresponde a los embeddings publicados",
            items=int(ann_state.get("items", 0)),
            method=str(ann_state.get("method", "")),
        )
    elif ann_required:
        recorder.fail(
            "semantic.ann",
            "semantic",
            str(ann_state.get("message") or "semantic-ann.db no está disponible"),
            reason=str(ann_state.get("reason") or "unknown"),
        )
    else:
        recorder.advisory(
            "semantic.ann",
            "semantic",
            str(ann_state.get("message") or "semantic-ann.db no está disponible"),
            status="skipped",
        )


def _candidate_rows(database_path: Path, limit: int) -> list[sqlite3.Row]:
    with _open_read_only(database_path) as connection:
        return connection.execute(
            """
            WITH candidates AS (
                SELECT c.id AS chunk_id, c.text, p.page_number,
                       d.id AS document_id, d.title, d.url,
                       ROW_NUMBER() OVER (
                           PARTITION BY d.id ORDER BY c.id
                       ) AS document_rank
                FROM chunks c
                JOIN pages p ON p.id = c.page_id
                JOIN documents d ON d.id = p.document_id
                WHERE length(trim(c.text)) >= 30
            )
            SELECT chunk_id, text, page_number, document_id, title, url
            FROM candidates
            WHERE document_rank <= 2
            ORDER BY document_rank, document_id, chunk_id
            LIMIT ?
            """,
            (max(1, limit),),
        ).fetchall()


def _probe_tokens(rows: Sequence[sqlite3.Row]) -> list[str]:
    documents: dict[str, set[int]] = defaultdict(set)
    frequency: Counter[str] = Counter()
    for row in rows:
        document_id = int(row["document_id"])
        seen: set[str] = set()
        for token in tokenize_query(str(row["text"])):
            if len(token) < 4 or token.isdigit() or token in seen:
                continue
            seen.add(token)
            documents[token].add(document_id)
            frequency[token] += 1
    return sorted(
        documents,
        key=lambda token: (-len(documents[token]), -frequency[token], token),
    )


def _result_identity(result: SearchResult) -> tuple[Any, ...]:
    return (
        result.document_id if result.document_id is not None else result.url,
        result.chunk_id,
        result.page,
    )


def _validate_evidence(
    database_path: Path,
    results: Iterable[SearchResult],
) -> tuple[bool, list[dict[str, Any]]]:
    invalid: list[dict[str, Any]] = []
    with _open_read_only(database_path) as connection:
        for result in results:
            row = connection.execute(
                """
                SELECT c.text, p.page_number, d.id AS document_id,
                       d.title, d.url
                FROM chunks c
                JOIN pages p ON p.id = c.page_id
                JOIN documents d ON d.id = p.document_id
                WHERE c.id = ?
                """,
                (result.chunk_id,),
            ).fetchone()
            reasons: list[str] = []
            if row is None:
                reasons.append("chunk_id inexistente")
            else:
                if int(row["page_number"]) != result.page:
                    reasons.append("página no coincide")
                if str(row["url"]) != result.url:
                    reasons.append("URL no coincide")
                if str(row["title"]) != result.title:
                    reasons.append("título no coincide")
                if result.document_id is not None and int(row["document_id"]) != int(
                    result.document_id
                ):
                    reasons.append("document_id no coincide")
                if not str(result.text).strip():
                    reasons.append("fragmento vacío")
            if result.evidence_scope in {"page", "regulatory_field"} and not str(
                result.match_excerpt or ""
            ).strip():
                reasons.append("falta el extracto que produjo la coincidencia")
            if reasons:
                invalid.append(
                    {
                        "chunk_id": result.chunk_id,
                        "title": result.title,
                        "reasons": reasons,
                    }
                )
    return not invalid, invalid


def _search_textual_probe(
    recorder: ValidationRecorder,
    database_path: Path,
    semantic_path: Path,
    ann_path: Path,
    rows: Sequence[sqlite3.Row],
) -> str | None:
    for query in _probe_tokens(rows):
        response = search_corpus_page(
            database_path,
            semantic_path,
            query,
            mode="textual",
            page=1,
            page_size=5,
            ann_index_path=ann_path,
        )
        if not response.results:
            continue
        valid, invalid = _validate_evidence(database_path, response.results)
        if not valid:
            recorder.fail(
                "probe.textual.evidence",
                "search",
                "La búsqueda textual devolvió evidencia inconsistente",
                query=query,
                invalid=invalid[:10],
            )
            return query
        if response.retrieval_backend != "fts5" or not response.totals_exact:
            recorder.fail(
                "probe.textual.contract",
                "search",
                "La búsqueda textual no informó su contrato global esperado",
                query=query,
                backend=response.retrieval_backend,
                totals_exact=response.totals_exact,
            )
            return query
        recorder.pass_check(
            "probe.textual",
            "search",
            "La búsqueda textual recuperó evidencia trazable",
            query=query,
            results=len(response.results),
            total_documents=response.total_documents,
            backend=response.retrieval_backend,
        )
        return query
    recorder.fail(
        "probe.textual",
        "search",
        "No se pudo derivar una sonda textual recuperable del propio corpus",
    )
    return None


def _search_hybrid_probe(
    recorder: ValidationRecorder,
    database_path: Path,
    semantic_path: Path,
    ann_path: Path,
    rows: Sequence[sqlite3.Row],
    config: dict[str, Any],
    preferred_query: str | None,
) -> None:
    candidates = list(dict.fromkeys([preferred_query, *_probe_tokens(rows)]))
    candidates = [str(item) for item in candidates if item]
    candidate_limit = max(20, int(config.get("candidate_limit", 200)))
    for query in candidates[:20]:
        response = search_corpus_page(
            database_path,
            semantic_path,
            query,
            mode="hybrid",
            page=1,
            page_size=5,
            ann_index_path=ann_path,
            candidate_limit=candidate_limit,
        )
        if not response.results:
            continue
        evidence_valid, invalid = _validate_evidence(
            database_path, response.results
        )
        contract_valid = bool(
            response.used_mode == "hybrid"
            and response.semantic_backend == "neural_ann"
            and response.retrieval_backend == "hybrid_ann"
            and response.neural_used
            and response.ann_used
            and int(response.candidate_count or 0) > 0
            and evidence_valid
        )
        details = {
            "query": query,
            "results": len(response.results),
            "used_mode": response.used_mode,
            "semantic_backend": response.semantic_backend,
            "retrieval_backend": response.retrieval_backend,
            "neural_used": response.neural_used,
            "ann_used": response.ann_used,
            "candidate_count": response.candidate_count,
            "invalid_evidence": invalid[:10],
        }
        if contract_valid:
            recorder.pass_check(
                "probe.hybrid",
                "search",
                "La recuperación híbrida ejecutó realmente el ANN y devolvió evidencia trazable",
                **details,
            )
        else:
            recorder.fail(
                "probe.hybrid",
                "search",
                "La búsqueda solicitada como híbrida no ejecutó el contrato neuronal global",
                **details,
            )
        return
    recorder.fail(
        "probe.hybrid",
        "search",
        "No se pudo ejecutar una sonda híbrida recuperable derivada del corpus",
    )


def _phrase_candidates(rows: Sequence[sqlite3.Row], words: int) -> Iterable[str]:
    width = max(2, min(words, 12))
    emitted: set[str] = set()
    for row in rows:
        tokens = re.findall(r"[^\W_]+", str(row["text"]), flags=re.UNICODE)
        for start in range(0, max(0, len(tokens) - width + 1), width):
            phrase = " ".join(tokens[start : start + width])
            normalized = normalize_phrase(phrase)
            useful_words = [word for word in normalized.split() if len(word) >= 3]
            if (
                len(normalized.split()) == width
                and len(useful_words) >= 2
                and normalized not in emitted
            ):
                emitted.add(normalized)
                yield phrase


def _search_phrase_probe(
    recorder: ValidationRecorder,
    database_path: Path,
    semantic_path: Path,
    ann_path: Path,
    rows: Sequence[sqlite3.Row],
    *,
    words: int,
) -> None:
    for index, query in enumerate(_phrase_candidates(rows, words)):
        if index >= 80:
            break
        response = search_corpus_page(
            database_path,
            semantic_path,
            query,
            mode="textual",
            page=1,
            page_size=5,
            exact_phrase=True,
            ann_index_path=ann_path,
            candidate_limit=200,
        )
        if not response.results:
            continue
        result = response.results[0]
        valid, invalid = _validate_evidence(database_path, response.results)
        confirmed = normalize_phrase(query) in normalize_phrase(
            result.match_excerpt or ""
        )
        if not valid or not confirmed or response.retrieval_backend != "page_phrase":
            recorder.fail(
                "probe.phrase",
                "search",
                "La búsqueda de frase no conservó su evidencia de página",
                query=query,
                backend=response.retrieval_backend,
                phrase_confirmed=confirmed,
                invalid=invalid[:10],
            )
            return
        recorder.pass_check(
            "probe.phrase",
            "search",
            "Una frase derivada del corpus se confirmó en la página fuente",
            query=query,
            page=result.page,
            evidence_scope=result.evidence_scope,
            backend=response.retrieval_backend,
        )
        return
    recorder.fail(
        "probe.phrase",
        "search",
        "No se pudo derivar una frase recuperable del propio corpus",
    )


def _field_query(value: str, scope: str) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    if scope in {"expediente", "radicado", "outcome"}:
        return value[:160]
    words = re.findall(r"[^\W_]+(?:[-/][^\W_]+)*", value, flags=re.UNICODE)
    useful = [word for word in words if len(normalize_phrase(word)) >= 2]
    return " ".join(useful[:4])[:160]


def _field_probe_values(
    database_path: Path,
    scope: str,
    limit: int = 30,
) -> list[str]:
    column = _FIELD_COLUMNS.get(scope)
    if column is None:
        return []
    with _open_read_only(database_path) as connection:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(regulatory_records)")
        }
        if column not in columns:
            return []
        rows = connection.execute(
            f'SELECT DISTINCT "{column}" FROM regulatory_records '
            f'WHERE trim(COALESCE("{column}", \'\')) != \'\' '
            f'ORDER BY id LIMIT ?',
            (max(1, limit),),
        ).fetchall()
    return [str(row[0]) for row in rows]


def _search_field_probes(
    recorder: ValidationRecorder,
    database_path: Path,
    semantic_path: Path,
    ann_path: Path,
    config: dict[str, Any],
) -> None:
    configured_scopes = [str(scope) for scope in config.get("scopes", ())]
    invalid_scopes = sorted(set(configured_scopes).difference(SEARCH_FIELD_SCOPES))
    if invalid_scopes:
        recorder.fail(
            "probe.fields.config",
            "search",
            "La configuración contiene campos desconocidos",
            scopes=invalid_scopes,
        )
        return
    successes = 0
    for scope in configured_scopes:
        found = False
        last_query = ""
        for value in _field_probe_values(database_path, scope):
            query = _field_query(value, scope)
            if not query:
                continue
            last_query = query
            response = search_corpus_page(
                database_path,
                semantic_path,
                query,
                mode="textual",
                page=1,
                page_size=5,
                field_scope=scope,
                ann_index_path=ann_path,
                candidate_limit=200,
            )
            matching = [
                result
                for result in response.results
                if result.matched_field == scope
                and result.evidence_scope == "regulatory_field"
            ]
            if not matching:
                continue
            valid, invalid = _validate_evidence(database_path, matching)
            if not valid:
                recorder.fail(
                    f"probe.field.{scope}",
                    "search",
                    "El campo devolvió evidencia inconsistente",
                    query=query,
                    invalid=invalid[:10],
                )
                found = True
                break
            recorder.pass_check(
                f"probe.field.{scope}",
                "search",
                "El campo recuperó un valor persistido y trazable",
                query=query,
                results=len(matching),
                backend=response.retrieval_backend,
            )
            successes += 1
            found = True
            break
        if not found:
            recorder.advisory(
                f"probe.field.{scope}",
                "search",
                "No se encontró una sonda automática utilizable para este campo",
                query=last_query,
            )
    minimum = int(config.get("minimum_successful_scopes", 1))
    if successes >= minimum:
        recorder.pass_check(
            "probe.fields.minimum",
            "search",
            "Se verificó el mínimo configurado de campos",
            successful_scopes=successes,
            minimum=minimum,
        )
    else:
        recorder.fail(
            "probe.fields.minimum",
            "search",
            "No se verificó el mínimo configurado de campos",
            successful_scopes=successes,
            minimum=minimum,
        )


def _search_pagination_probe(
    recorder: ValidationRecorder,
    database_path: Path,
    semantic_path: Path,
    ann_path: Path,
    rows: Sequence[sqlite3.Row],
    config: dict[str, Any],
    preferred_query: str | None,
) -> None:
    page_size = max(1, min(int(config.get("page_size", 1)), 100))
    minimum_documents = max(2, int(config.get("minimum_documents", 2)))
    candidates = list(dict.fromkeys([preferred_query, *_probe_tokens(rows)]))
    candidates = [str(item) for item in candidates if item]
    for query in candidates[:80]:
        first = search_corpus_page(
            database_path,
            semantic_path,
            query,
            mode="textual",
            page=1,
            page_size=page_size,
            ann_index_path=ann_path,
        )
        if int(first.total_documents or 0) < minimum_documents or not first.has_next:
            continue
        second = search_corpus_page(
            database_path,
            semantic_path,
            query,
            mode="textual",
            page=2,
            page_size=page_size,
            ann_index_path=ann_path,
        )
        repeated = search_corpus_page(
            database_path,
            semantic_path,
            query,
            mode="textual",
            page=1,
            page_size=page_size,
            ann_index_path=ann_path,
        )
        first_docs = {
            item.document_id if item.document_id is not None else item.url
            for item in first.results
        }
        second_docs = {
            item.document_id if item.document_id is not None else item.url
            for item in second.results
        }
        stable = [_result_identity(item) for item in first.results] == [
            _result_identity(item) for item in repeated.results
        ]
        valid_evidence, invalid = _validate_evidence(
            database_path, [*first.results, *second.results]
        )
        valid = all(
            (
                first.totals_exact,
                second.totals_exact,
                first.total_documents == second.total_documents,
                first.total_pages == second.total_pages,
                not first_docs.intersection(second_docs),
                bool(second.results),
                stable,
                valid_evidence,
            )
        )
        if valid:
            recorder.pass_check(
                "probe.pagination",
                "search",
                "La paginación textual es global, estable y sin documentos repetidos",
                query=query,
                total_documents=first.total_documents,
                total_pages=first.total_pages,
                page_size=page_size,
            )
        else:
            recorder.fail(
                "probe.pagination",
                "search",
                "La paginación textual no cumplió su contrato",
                query=query,
                total_documents=first.total_documents,
                total_pages=first.total_pages,
                overlap=len(first_docs.intersection(second_docs)),
                stable=stable,
                invalid_evidence=invalid[:10],
            )
        return
    recorder.fail(
        "probe.pagination",
        "search",
        "No se encontró una consulta del corpus con dos páginas de resultados",
        minimum_documents=minimum_documents,
        page_size=page_size,
    )


def _search_export_probe(
    recorder: ValidationRecorder,
    database_path: Path,
    semantic_path: Path,
    ann_path: Path,
    config: dict[str, Any],
    preferred_query: str | None,
    rows: Sequence[sqlite3.Row],
) -> None:
    formats = [str(item).strip().lower() for item in config.get("formats", ())]
    unknown = sorted(set(formats).difference({"csv", "xlsx"}))
    if unknown or not formats:
        recorder.fail(
            "probe.export.config",
            "export",
            "La configuración de exportación no contiene formatos válidos",
            unknown_formats=unknown,
        )
        return
    query_candidates = list(dict.fromkeys([preferred_query, *_probe_tokens(rows)]))
    query_candidates = [str(item) for item in query_candidates if item]
    maximum_results = max(1, min(int(config.get("maximum_results", 5)), 20))
    results: list[SearchResult] = []
    query = ""
    for candidate in query_candidates[:30]:
        response = search_corpus_page(
            database_path,
            semantic_path,
            candidate,
            mode="textual",
            page=1,
            page_size=maximum_results,
            ann_index_path=ann_path,
        )
        if response.results:
            results = response.results[:maximum_results]
            query = candidate
            break
    if not results:
        recorder.fail(
            "probe.export.source",
            "export",
            "No se recuperaron resultados para probar la exportación",
        )
        return

    expected_headers = [label for _, label in SEARCH_EXPORT_COLUMNS]
    metadata = {
        "consulta": query,
        "metodo_solicitado": "textual",
        "motor_real": "fts5",
    }
    for file_format in formats:
        try:
            artifact = export_search_results(
                results,
                file_format=file_format,
                metadata=metadata,
                max_rows=maximum_results,
                max_bytes=5 * 1024 * 1024,
            )
            if file_format == "csv":
                parsed = list(
                    csv.DictReader(
                        StringIO(artifact.data.decode("utf-8-sig"))
                    )
                )
                valid = bool(
                    artifact.data.startswith(b"\xef\xbb\xbf")
                    and artifact.row_count == len(results)
                    and len(parsed) == len(results)
                    and parsed
                    and all(header in parsed[0] for header in expected_headers)
                    and parsed[0].get("documento") == results[0].title
                    and parsed[0].get("pagina") == str(results[0].page)
                    and parsed[0].get("fuente") == results[0].url
                )
                details = {
                    "bytes": len(artifact.data),
                    "rows": artifact.row_count,
                    "columns": len(parsed[0]) if parsed else 0,
                }
            else:
                with ZipFile(BytesIO(artifact.data)) as workbook:
                    names = set(workbook.namelist())
                    searchable_xml = b"\n".join(
                        workbook.read(name)
                        for name in names
                        if name.startswith("xl/worksheets/")
                        or name == "xl/sharedStrings.xml"
                    ).decode(
                        "utf-8", errors="replace"
                    )
                valid = bool(
                    artifact.data.startswith(b"PK")
                    and artifact.row_count == len(results)
                    and "[Content_Types].xml" in names
                    and "xl/workbook.xml" in names
                    and "xl/worksheets/sheet1.xml" in names
                    and "documento" in searchable_xml
                    and "pagina" in searchable_xml
                    and "fuente" in searchable_xml
                )
                details = {
                    "bytes": len(artifact.data),
                    "rows": artifact.row_count,
                    "archive_entries": len(names),
                }
        except (BadZipFile, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            recorder.fail(
                f"probe.export.{file_format}",
                "export",
                f"No fue posible construir o leer la exportación {file_format.upper()}",
                error=str(exc),
            )
            continue
        if valid:
            recorder.pass_check(
                f"probe.export.{file_format}",
                "export",
                f"La exportación {file_format.upper()} conservó columnas y evidencia básicas",
                query=query,
                **details,
            )
        else:
            recorder.fail(
                f"probe.export.{file_format}",
                "export",
                f"La exportación {file_format.upper()} no cumplió su contrato tabular",
                query=query,
                **details,
            )


def _explicit_cases(
    recorder: ValidationRecorder,
    database_path: Path,
    semantic_path: Path,
    ann_path: Path,
    cases: Sequence[Any],
) -> None:
    for index, raw_case in enumerate(cases, start=1):
        if not isinstance(raw_case, dict):
            recorder.advisory(
                f"explicit.{index}",
                "explicit",
                "El caso explícito no es un objeto válido",
            )
            continue
        name = str(raw_case.get("name") or f"Caso {index}")
        query = str(raw_case.get("query") or "").strip()
        try:
            response = search_corpus_page(
                database_path,
                semantic_path,
                query,
                mode=str(raw_case.get("mode") or "textual"),
                page=1,
                page_size=max(1, min(int(raw_case.get("page_size", 10)), 100)),
                filters=raw_case.get("filters"),
                exact_phrase=bool(raw_case.get("exact_phrase", False)),
                field_scope=str(raw_case.get("field_scope") or "all"),
                ann_index_path=ann_path,
                candidate_limit=max(100, int(raw_case.get("candidate_limit", 500))),
            )
            minimum = max(0, int(raw_case.get("minimum_results", 1)))
            status = "passed" if len(response.results) >= minimum else "warning"
            recorder.advisory(
                f"explicit.{index}",
                "explicit",
                (
                    f"{name}: se observaron {len(response.results)} resultados"
                    if status == "passed"
                    else f"{name}: no alcanzó el mínimo orientativo"
                ),
                status=status,
                query=query,
                results=len(response.results),
                minimum_results=minimum,
                backend=response.retrieval_backend,
            )
        except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError) as exc:
            recorder.advisory(
                f"explicit.{index}",
                "explicit",
                f"{name}: la consulta orientativa no pudo ejecutarse: {exc}",
                query=query,
            )


def validate_published_corpus(
    *,
    database_path: Path,
    semantic_path: Path,
    ann_path: Path,
    config: dict[str, Any],
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    recorder = ValidationRecorder()
    counts = _schema_checks(recorder, database_path, config["schema"])
    _package_checks(recorder, database_path.parent, config["packages"])
    _manifest_checks(
        recorder,
        database_path,
        manifest_path,
        config.get("manifest"),
        allowed_hosts=config["schema"].get("allowed_document_hosts", ()),
    )
    _semantic_checks(
        recorder,
        database_path,
        semantic_path,
        ann_path,
        config["semantic"],
    )

    probe_config = config["probes"]
    rows: list[sqlite3.Row] = []
    try:
        rows = _candidate_rows(
            database_path, int(probe_config.get("candidate_rows", 240))
        )
    except sqlite3.Error as exc:
        recorder.fail(
            "probe.source", "search", f"No fue posible leer sondas del corpus: {exc}"
        )

    textual_query: str | None = None
    try:
        if probe_config.get("textual", {}).get("enabled", True):
            textual_query = _search_textual_probe(
                recorder, database_path, semantic_path, ann_path, rows
            )
        if probe_config.get("hybrid", {}).get("enabled", False):
            _search_hybrid_probe(
                recorder,
                database_path,
                semantic_path,
                ann_path,
                rows,
                probe_config["hybrid"],
                textual_query,
            )
        if probe_config.get("phrase", {}).get("enabled", True):
            _search_phrase_probe(
                recorder,
                database_path,
                semantic_path,
                ann_path,
                rows,
                words=int(probe_config["phrase"].get("words", 4)),
            )
        if probe_config.get("fields", {}).get("enabled", True):
            _search_field_probes(
                recorder,
                database_path,
                semantic_path,
                ann_path,
                probe_config["fields"],
            )
        if probe_config.get("pagination", {}).get("enabled", True):
            _search_pagination_probe(
                recorder,
                database_path,
                semantic_path,
                ann_path,
                rows,
                probe_config["pagination"],
                textual_query,
            )
        if probe_config.get("export", {}).get("enabled", False):
            _search_export_probe(
                recorder,
                database_path,
                semantic_path,
                ann_path,
                probe_config["export"],
                textual_query,
                rows,
            )
    except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError) as exc:
        recorder.fail(
            "probe.execution",
            "search",
            f"Una sonda técnica no pudo completarse: {exc}",
        )

    _explicit_cases(
        recorder,
        database_path,
        semantic_path,
        ann_path,
        config.get("explicit_cases", []),
    )
    required_failures = [
        check
        for check in recorder.checks
        if check.severity == "required" and check.status == "failed"
    ]
    advisories = [check for check in recorder.checks if check.severity == "advisory"]
    passed = sum(check.status == "passed" for check in recorder.checks)
    return {
        "format_version": REPORT_FORMAT_VERSION,
        "generated_at": _utc_now(),
        "status": "passed" if not required_failures else "failed",
        "scope": "technical_search_validation",
        "non_goals": [
            "No califica la corrección regulatoria de las actas",
            "No corrige ni modifica documentos o fichas",
            "No mide relevancia subjetiva de resultados",
        ],
        "summary": {
            "checks": len(recorder.checks),
            "passed": passed,
            "blocking_failures": len(required_failures),
            "advisories": len(advisories),
        },
        "corpus": {
            "database": str(database_path),
            "semantic_index": str(semantic_path),
            "ann_index": str(ann_path),
            "rows": counts,
        },
        "checks": [asdict(check) for check in recorder.checks],
    }


def render_markdown_summary(report: dict[str, Any]) -> str:
    summary = report["summary"]
    status = "aprobada" if report["status"] == "passed" else "con bloqueos técnicos"
    lines = [
        "## Validación técnica de búsquedas",
        "",
        f"- Estado: **{status}**",
        f"- Comprobaciones superadas: **{summary['passed']}**",
        f"- Bloqueos técnicos: **{summary['blocking_failures']}**",
        f"- Observaciones orientativas: **{summary['advisories']}**",
        "- Alcance: estructura, paquetes, búsqueda y trazabilidad; no evalúa decisiones regulatorias.",
        "",
        "| Comprobación | Estado | Tipo | Detalle |",
        "|---|---|---|---|",
    ]
    for check in report["checks"]:
        icon = {"passed": "✅", "failed": "❌", "warning": "⚠️", "skipped": "➖"}.get(
            str(check["status"]), "•"
        )
        message = str(check["message"]).replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| `{check['check_id']}` | {icon} {check['status']} | "
            f"{check['severity']} | {message} |"
        )
    manifest_check = next(
        (
            check
            for check in report["checks"]
            if check["check_id"] == "manifest.coverage"
        ),
        None,
    )
    if manifest_check and manifest_check.get("details"):
        details = manifest_check["details"]
        lines.extend(
            [
                "",
                "### Cobertura contra el manifiesto",
                "",
                "- Rango indexable del manifiesto: "
                f"**{details.get('manifest_minimum_year', '—')}–"
                f"{details.get('manifest_maximum_year', '—')}** "
                f"({details.get('manifest_distinct_years', '—')} años distintos)",
                f"- Esperados: **{details.get('expected', '—')}**",
                f"- Consultables en rango: **{details.get('indexed', '—')}**",
                f"- Filas presentes pero no consultables: "
                f"**{details.get('unsearchable', '—')}**",
                f"- Faltantes: **{details.get('missing', '—')}**",
                f"- Adicionales: **{details.get('unexpected', '—')}**",
                f"- Cobertura: **{details.get('coverage_percent', '—')} %**",
            ]
        )
    lines.append("")
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Valida técnicamente las búsquedas del corpus publicado"
    )
    parser.add_argument("--database", type=Path, default=Path("data/actas.db"))
    parser.add_argument("--semantic", type=Path, default=Path("data/semantic.db"))
    parser.add_argument("--ann", type=Path, default=Path("data/semantic-ann.db"))
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument(
        "--config-root",
        type=Path,
        help="Limita --config a una ruta relativa dentro de este directorio",
    )
    parser.add_argument("--manifest", type=Path)
    parser.add_argument(
        "--manifest-root",
        type=Path,
        help="Limita --manifest a una ruta relativa dentro de este directorio",
    )
    parser.add_argument(
        "--report", type=Path, default=Path("search-validation-report.json")
    )
    parser.add_argument("--summary", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        config_path = resolve_config_path(arguments.config, arguments.config_root)
        config = load_validation_config(config_path)
        manifest_path = (
            resolve_manifest_path(arguments.manifest, arguments.manifest_root)
            if arguments.manifest is not None
            else None
        )
        report = validate_published_corpus(
            database_path=arguments.database,
            semantic_path=arguments.semantic,
            ann_path=arguments.ann,
            config=config,
            manifest_path=manifest_path,
        )
    except (OSError, TypeError, ValueError) as exc:
        report = {
            "format_version": REPORT_FORMAT_VERSION,
            "generated_at": _utc_now(),
            "status": "failed",
            "scope": "technical_search_validation",
            "summary": {
                "checks": 1,
                "passed": 0,
                "blocking_failures": 1,
                "advisories": 0,
            },
            "corpus": {},
            "checks": [
                asdict(
                    ValidationCheck(
                        "runner.configuration",
                        "runner",
                        "failed",
                        "required",
                        str(exc),
                        {},
                    )
                )
            ],
        }
    arguments.report.parent.mkdir(parents=True, exist_ok=True)
    arguments.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown = render_markdown_summary(report)
    print(markdown)
    if arguments.summary:
        arguments.summary.parent.mkdir(parents=True, exist_ok=True)
        with arguments.summary.open("a", encoding="utf-8") as handle:
            handle.write(markdown)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
