from __future__ import annotations

import os
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("ACTAS_DATA_DIR", ROOT_DIR / "data"))
CACHE_DIR = Path(os.getenv("ACTAS_CACHE_DIR", ROOT_DIR / "local_cache"))
PDF_CACHE_DIR = CACHE_DIR / "documents"

MANIFEST_PATH = Path(
    os.getenv("ACTAS_MANIFEST_PATH", ROOT_DIR / "documents_manifest.csv")
)
DATABASE_PATH = Path(os.getenv("ACTAS_DATABASE_PATH", DATA_DIR / "actas.db"))

CHUNK_SIZE = int(os.getenv("ACTAS_CHUNK_SIZE", "1200"))
CHUNK_OVERLAP = int(os.getenv("ACTAS_CHUNK_OVERLAP", "180"))
DEFAULT_TOP_K = int(os.getenv("ACTAS_TOP_K", "8"))
MAX_CONTEXT_CHARS = int(os.getenv("ACTAS_MAX_CONTEXT_CHARS", "14000"))
MAX_PDF_BYTES = int(os.getenv("ACTAS_MAX_PDF_BYTES", str(60 * 1024 * 1024)))

ALLOWED_DOCUMENT_HOSTS = tuple(
    host.strip().lower()
    for host in os.getenv(
        "ACTAS_ALLOWED_HOSTS", "invima.gov.co,www.invima.gov.co"
    ).split(",")
    if host.strip()
)


def ensure_directories() -> None:
    for path in (DATA_DIR, CACHE_DIR, PDF_CACHE_DIR):
        path.mkdir(parents=True, exist_ok=True)

