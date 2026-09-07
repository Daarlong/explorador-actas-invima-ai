"""Estado técnico del corpus usado por la interfaz.

Este módulo no evalúa la pertinencia de las búsquedas ni el contenido de las
actas. Resume únicamente cobertura, integridad, vigencia de la fuente e índices
disponibles para que el usuario sepa si el corpus puede consultarse.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping


def _load_json_report(path: Path | None) -> dict | None:
    if path is None or not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _parse_report_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_hours(value: object, current_time: datetime) -> float | None:
    generated = _parse_report_time(value)
    if generated is None:
        return None
    return max(0.0, (current_time - generated).total_seconds() / 3600)


def _coverage_percent(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round((numerator / denominator) * 100, 2)


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def _layer_status(percent: float | None, *, verified: bool = True) -> str:
    if not verified or percent is None:
        return "unverified"
    return "ok" if percent >= 100.0 else "incomplete"


def _valid_source_snapshot(snapshot: Mapping | None) -> bool:
    if not snapshot or snapshot.get("status") != "valid":
        return False
    html_hash = str(snapshot.get("html_sha256") or "")
    return bool(
        _parse_report_time(snapshot.get("generated_at"))
        and _safe_int(snapshot.get("discovered_records")) > 0
        and _safe_int(snapshot.get("html_bytes")) > 0
        and re.fullmatch(r"[0-9a-fA-F]{64}", html_hash)
        and str(snapshot.get("parser_version") or "").strip()
    )


def corpus_status_from_reports(
    integrity_report_path: Path,
    indexing_report_path: Path,
    semantic_report_path: Path,
    *,
    catalog_report_path: Path | None = None,
    source_snapshot_path: Path | None = None,
    now: datetime | None = None,
) -> dict:
    """Resume la disponibilidad técnica sin evaluar consultas ni respuestas."""
    integrity = _load_json_report(integrity_report_path)
    indexing = _load_json_report(indexing_report_path)
    semantic = _load_json_report(semantic_report_path)
    catalog = _load_json_report(catalog_report_path)
    source_snapshot = _load_json_report(source_snapshot_path)
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    source_snapshot_valid = _valid_source_snapshot(source_snapshot)
    source_checked = bool(catalog and catalog.get("source_checked"))
    catalog_records = _safe_int((catalog or {}).get("catalog_records"))
    currently_listed = _safe_int((catalog or {}).get("currently_listed"))
    manifest_from_catalog = _safe_int((catalog or {}).get("manifest_documents"))
    source_discovered = _safe_int(
        (source_snapshot or {}).get(
            "discovered_records",
            (catalog or {}).get("discovered_records", 0),
        )
    )
    source_layer_verified = source_checked and source_snapshot_valid
    source_catalog_percent = (
        _coverage_percent(currently_listed, source_discovered)
        if source_layer_verified
        else None
    )
    catalog_manifest_percent = _coverage_percent(
        manifest_from_catalog,
        catalog_records,
    )

    if not integrity:
        return {
            "status": "unavailable",
            "message": "No existe un informe de integridad válido.",
            "integrity_available": False,
            "indexing_available": bool(indexing),
            "semantic_available": bool(semantic),
            "generated_at": None,
            "age_hours": None,
            "freshness": "unknown",
            "manifest_documents": 0,
            "indexed_documents": 0,
            "document_coverage_percent": 0.0,
            "text_page_coverage_percent": 0.0,
            "missing_documents": [],
            "ocr_candidate_documents": 0,
            "ocr_candidate_pages": 0,
            "incomplete_years": [],
            "issues": [
                {
                    "severity": "error",
                    "category": "report",
                    "detail": "Ejecuta el workflow Construir índice.",
                    "count": 1,
                }
            ],
            "source_checked": source_checked,
            "source_snapshot_available": bool(source_snapshot),
            "source_snapshot_valid": source_snapshot_valid,
            "source_age_hours": _age_hours(
                (source_snapshot or {}).get("generated_at"), current_time
            ),
            "source_discovered_records": source_discovered,
            "currently_listed_records": currently_listed,
            "source_catalog_percent": source_catalog_percent,
            "catalog_records": catalog_records,
            "catalog_manifest_documents": manifest_from_catalog,
            "catalog_manifest_percent": catalog_manifest_percent,
            "coverage_layers": [
                {
                    "key": "source_catalog",
                    "label": "Fuente → catálogo",
                    "numerator": currently_listed,
                    "denominator": source_discovered,
                    "coverage_percent": source_catalog_percent,
                    "status": _layer_status(
                        source_catalog_percent,
                        verified=source_layer_verified,
                    ),
                },
                {
                    "key": "catalog_manifest",
                    "label": "Catálogo → manifiesto",
                    "numerator": manifest_from_catalog,
                    "denominator": catalog_records,
                    "coverage_percent": catalog_manifest_percent,
                    "status": _layer_status(catalog_manifest_percent),
                },
                {
                    "key": "manifest_index",
                    "label": "Manifiesto → índice",
                    "numerator": 0,
                    "denominator": 0,
                    "coverage_percent": None,
                    "status": "unverified",
                },
                {
                    "key": "pages",
                    "label": "Páginas consultables",
                    "numerator": 0,
                    "denominator": 0,
                    "coverage_percent": None,
                    "status": "unverified",
                },
            ],
        }

    expected = _safe_int(integrity.get("manifest_documents"))
    indexed = _safe_int(integrity.get("indexed_documents"))
    missing_documents = list(integrity.get("missing_documents") or [])
    ocr_candidates = list(integrity.get("ocr_candidates") or [])
    ocr_pages = sum(
        len(item.get("pages") or [])
        for item in ocr_candidates
        if isinstance(item, Mapping)
    )
    coverage_rows = list(integrity.get("coverage_by_year") or [])
    incomplete_years = [
        {
            "year": item.get("year"),
            "expected": _safe_int(item.get("manifest_documents")),
            "indexed": _safe_int(item.get("indexed_documents")),
            "missing": _safe_int(item.get("missing_documents")),
            "coverage_percent": _safe_float(item.get("coverage_percent")),
        }
        for item in coverage_rows
        if isinstance(item, Mapping)
        and _safe_float(item.get("coverage_percent")) < 100.0
    ]
    generated = _parse_report_time(integrity.get("generated_at"))
    age_hours = _age_hours(integrity.get("generated_at"), current_time)
    if age_hours is None:
        freshness = "unknown"
    elif age_hours <= 24 * 7:
        freshness = "fresh"
    elif age_hours <= 24 * 30:
        freshness = "aging"
    else:
        freshness = "stale"

    issues: list[dict[str, object]] = []

    def add_issue(severity: str, category: str, detail: str, count: int) -> None:
        if count:
            issues.append(
                {
                    "severity": severity,
                    "category": category,
                    "detail": detail,
                    "count": count,
                }
            )

    add_issue("error", "coverage", "Documentos no indexados", len(missing_documents))
    add_issue(
        "error",
        "documents",
        "Documentos sin páginas",
        len(integrity.get("documents_without_pages") or []),
    )
    add_issue(
        "error",
        "documents",
        "Documentos sin fragmentos",
        len(integrity.get("documents_without_chunks") or []),
    )
    add_issue(
        "error",
        "documents",
        "Documentos inesperados en el índice",
        len(integrity.get("unexpected_documents") or []),
    )
    add_issue("warning", "ocr", "Páginas candidatas para OCR", ocr_pages)
    add_issue(
        "warning",
        "inventory",
        "Documentos con inventario de páginas pendiente",
        len(integrity.get("page_inventory_pending") or []),
    )
    add_issue(
        "warning",
        "indexing",
        "Documentos fallidos en la última indexación",
        _safe_int((indexing or {}).get("documents_failed")),
    )
    add_issue(
        "error",
        "extraction",
        "Errores de extracción documental",
        len(integrity.get("regulatory_extraction_errors") or []),
    )
    if catalog_report_path is not None and not source_checked:
        add_issue("error", "source", "La fuente oficial no fue verificada", 1)
    if source_snapshot_path is not None and not source_snapshot_valid:
        add_issue("error", "source", "No existe un snapshot oficial válido", 1)
    catalog_discovered = _safe_int((catalog or {}).get("discovered_records"))
    if source_layer_verified and catalog_discovered != source_discovered:
        add_issue(
            "error",
            "source",
            "El snapshot y el catálogo no coinciden",
            abs(catalog_discovered - source_discovered) or 1,
        )
    if source_layer_verified and currently_listed != source_discovered:
        add_issue(
            "error",
            "coverage",
            "Las publicaciones vigentes no coinciden con la fuente",
            abs(currently_listed - source_discovered) or 1,
        )
    records_without_url = _safe_int((catalog or {}).get("records_without_url"))
    shared_urls = len((catalog or {}).get("shared_url_conflicts") or [])
    add_issue(
        "warning",
        "catalog",
        "Publicaciones sin enlace descargable",
        records_without_url,
    )
    add_issue(
        "warning",
        "catalog",
        "Enlaces compartidos por varias entradas",
        shared_urls,
    )
    if catalog_report_path is not None and catalog_records <= 0:
        add_issue("error", "coverage", "El catálogo no es verificable", 1)
    elif catalog_manifest_percent is not None and catalog_manifest_percent < 100.0:
        add_issue(
            "warning",
            "coverage",
            "Entradas del catálogo que no llegaron al manifiesto",
            max(catalog_records - manifest_from_catalog, 1),
        )
    if expected <= 0:
        add_issue("error", "coverage", "El manifiesto no contiene documentos", 1)

    source_age_hours = _age_hours(
        (source_snapshot or {}).get("generated_at"), current_time
    )
    if source_layer_verified and source_age_hours is not None:
        if source_age_hours > 24 * 30:
            add_issue("error", "source_freshness", "Fuente sin validar en 30 días", 1)
        elif source_age_hours > 24 * 7:
            add_issue("warning", "source_freshness", "Fuente sin validar en 7 días", 1)
    if freshness == "aging":
        add_issue("warning", "freshness", "Informe con más de 7 días", 1)
    elif freshness == "stale":
        add_issue("error", "freshness", "Informe con más de 30 días", 1)
    elif freshness == "unknown":
        add_issue("warning", "freshness", "Informe sin fecha válida", 1)
    if str(integrity.get("sqlite_integrity", "")).lower() != "ok":
        add_issue("error", "database", "Integridad SQLite incorrecta", 1)
    semantic_status = str((semantic or {}).get("status", "missing"))
    if semantic_status in {"error", "missing"}:
        add_issue("warning", "semantic", "Índice semántico no confirmado", 1)

    manifest_index_percent = _coverage_percent(indexed, expected)
    indexed_pages = _safe_int(integrity.get("pages_indexed"))
    pdf_pages = _safe_int(integrity.get("pdf_pages"))
    page_percent = _safe_float(integrity.get("text_page_coverage_percent"))
    if pdf_pages > 0 and page_percent < 100.0 and not ocr_pages:
        add_issue(
            "warning",
            "pages",
            "Páginas no disponibles como texto consultable",
            max(pdf_pages - indexed_pages, 1),
        )

    error_count = sum(item["severity"] == "error" for item in issues)
    warning_count = sum(item["severity"] == "warning" for item in issues)
    status = "error" if error_count else ("warning" if warning_count else "ok")
    coverage_layers = [
        {
            "key": "source_catalog",
            "label": "Fuente → catálogo",
            "numerator": currently_listed,
            "denominator": source_discovered,
            "coverage_percent": source_catalog_percent,
            "status": _layer_status(
                source_catalog_percent,
                verified=source_layer_verified,
            ),
        },
        {
            "key": "catalog_manifest",
            "label": "Catálogo → manifiesto",
            "numerator": manifest_from_catalog,
            "denominator": catalog_records,
            "coverage_percent": catalog_manifest_percent,
            "status": _layer_status(catalog_manifest_percent),
        },
        {
            "key": "manifest_index",
            "label": "Manifiesto → índice",
            "numerator": indexed,
            "denominator": expected,
            "coverage_percent": manifest_index_percent,
            "status": _layer_status(manifest_index_percent),
        },
        {
            "key": "pages",
            "label": "Páginas consultables",
            "numerator": indexed_pages,
            "denominator": pdf_pages,
            "coverage_percent": page_percent if pdf_pages else None,
            "status": _layer_status(page_percent if pdf_pages else None),
        },
    ]
    return {
        "status": status,
        "message": "Estado construido a partir de los informes técnicos.",
        "integrity_available": True,
        "indexing_available": bool(indexing),
        "semantic_available": bool(semantic),
        "generated_at": generated.isoformat() if generated else None,
        "age_hours": round(age_hours, 2) if age_hours is not None else None,
        "freshness": freshness,
        "manifest_documents": expected,
        "indexed_documents": indexed,
        "document_coverage_percent": manifest_index_percent or 0.0,
        "text_page_coverage_percent": page_percent,
        "missing_documents": missing_documents,
        "ocr_candidate_documents": len(ocr_candidates),
        "ocr_candidate_pages": ocr_pages,
        "incomplete_years": incomplete_years,
        "coverage_by_year": coverage_rows,
        "semantic_status": semantic_status,
        "issues": issues,
        "source_checked": source_checked,
        "source_snapshot_available": bool(source_snapshot),
        "source_snapshot_valid": source_snapshot_valid,
        "source_snapshot_generated_at": (source_snapshot or {}).get("generated_at"),
        "source_snapshot_parser_version": (source_snapshot or {}).get("parser_version"),
        "source_snapshot_html_sha256": (source_snapshot or {}).get("html_sha256"),
        "source_age_hours": round(source_age_hours, 2)
        if source_age_hours is not None
        else None,
        "source_discovered_records": source_discovered,
        "currently_listed_records": currently_listed,
        "source_catalog_percent": source_catalog_percent,
        "catalog_records": catalog_records,
        "catalog_manifest_documents": manifest_from_catalog,
        "catalog_manifest_percent": catalog_manifest_percent,
        "records_without_url": records_without_url,
        "shared_url_conflicts": shared_urls,
        "coverage_layers": coverage_layers,
    }
