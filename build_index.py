from __future__ import annotations

from config import DATABASE_PATH, MANIFEST_PATH, PDF_CACHE_DIR, ensure_directories
from services.indexing import rebuild_index


def print_progress(current: int, total: int, title: str) -> None:
    print(f"[{current}/{total}] {title}", flush=True)


if __name__ == "__main__":
    ensure_directories()
    report = rebuild_index(
        MANIFEST_PATH,
        DATABASE_PATH,
        PDF_CACHE_DIR,
        progress_callback=print_progress,
    )
    print(report.as_dict())

