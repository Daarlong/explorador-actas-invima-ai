from __future__ import annotations

from pathlib import Path

from services.integrity import load_integrity_report


PENDING_KEYS = (
    "missing_documents",
    "documents_without_pages",
    "documents_without_chunks",
)


def pending_index_items(report_path: Path) -> dict[str, object]:
    """Resume lo que puede corregir una actualización incremental del índice."""
    report = load_integrity_report(report_path)
    if not report:
        return {
            "needs_update": True,
            "reason": "missing_integrity_report",
            "pending_count": 1,
        }

    pending_count = sum(len(report.get(key, []) or []) for key in PENDING_KEYS)
    return {
        "needs_update": pending_count > 0,
        "reason": "pending_documents" if pending_count else "complete",
        "pending_count": pending_count,
    }
