from __future__ import annotations

import json
from pathlib import Path

from config import (
    INDEXING_REPORT_PATH,
    INTEGRITY_REPORT_PATH,
    RAW_DATABASE_PATH,
    RAW_SEMANTIC_INDEX_PATH,
    SEMANTIC_ENABLED,
    SEMANTIC_REPORT_PATH,
)
from services.automation import pending_index_items
from services.database_package import package_manifest_path


def _load_report(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _package_available(database_path: Path) -> bool:
    if database_path.exists():
        return True
    manifest_path = package_manifest_path(database_path)
    package = _load_report(manifest_path)
    parts = package.get("parts")
    if not isinstance(parts, list) or not parts:
        return False
    for item in parts:
        if not isinstance(item, dict):
            return False
        name = str(item.get("name", ""))
        path = database_path.parent / name
        try:
            expected_size = int(item.get("bytes", -1))
        except (TypeError, ValueError):
            return False
        if (
            Path(name).name != name
            or not name.startswith(f"{database_path.name}.gz.part-")
            or not path.exists()
            or path.stat().st_size != expected_size
        ):
            return False
    return True


def build_pending_status(
    integrity_path: Path,
    indexing_path: Path,
    semantic_report_path: Path,
    database_path: Path,
    semantic_index_path: Path,
    *,
    semantic_enabled: bool,
) -> dict[str, object]:
    base = pending_index_items(integrity_path)
    integrity = _load_report(integrity_path)
    indexing = _load_report(indexing_path)
    semantic = _load_report(semantic_report_path)
    reasons: list[str] = []
    pending_count = int(base.get("pending_count", 0) or 0)
    if base.get("needs_update"):
        reasons.append(str(base.get("reason") or "pending_documents"))

    collection_keys = (
        "regulatory_extraction_pending",
        "regulatory_extraction_errors",
        "source_text_pending",
        "structured_text_pending",
        "field_reprocessing_pending",
        "page_inventory_pending",
    )
    for key in collection_keys:
        values = integrity.get(key) or []
        count = len(values) if isinstance(values, list) else int(bool(values))
        if count:
            reasons.append(key)
            pending_count += count

    failed = int(indexing.get("documents_failed", 0) or 0)
    if failed:
        reasons.append("failed_documents")
        pending_count += failed
    if integrity and integrity.get("schema_version") != integrity.get(
        "expected_schema_version"
    ):
        reasons.append("schema_upgrade")
        pending_count += 1

    if not _package_available(database_path):
        reasons.append("database_package_missing_or_incomplete")
        pending_count += 1
    if semantic_enabled:
        if str(semantic.get("status", "missing")).lower() not in {"built", "reused"}:
            reasons.append("semantic_index_stale")
            pending_count += 1
        if not _package_available(semantic_index_path):
            reasons.append("semantic_package_missing_or_incomplete")
            pending_count += 1

    unique_reasons = list(dict.fromkeys(reasons))
    return {
        "needs_update": bool(unique_reasons),
        "reason": unique_reasons[0] if unique_reasons else "complete",
        "reasons": unique_reasons,
        "pending_count": pending_count,
    }


if __name__ == "__main__":
    status = build_pending_status(
        INTEGRITY_REPORT_PATH,
        INDEXING_REPORT_PATH,
        SEMANTIC_REPORT_PATH,
        RAW_DATABASE_PATH,
        RAW_SEMANTIC_INDEX_PATH,
        semantic_enabled=SEMANTIC_ENABLED,
    )
    print(json.dumps(status, ensure_ascii=False, indent=2))
    raise SystemExit(0 if status["needs_update"] else 1)
