from __future__ import annotations

import os
from pathlib import Path

from services.database_package import resolve_database_path


ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("ACTAS_DATA_DIR", ROOT_DIR / "data"))
CACHE_DIR = Path(os.getenv("ACTAS_CACHE_DIR", ROOT_DIR / "local_cache"))
PDF_CACHE_DIR = CACHE_DIR / "documents"

MANIFEST_PATH = Path(
    os.getenv("ACTAS_MANIFEST_PATH", ROOT_DIR / "documents_manifest.csv")
)
CATALOG_SOURCE_URL = os.getenv(
    "ACTAS_CATALOG_SOURCE_URL",
    "https://www.invima.gov.co/productos-vigilados/medicamentos-y-productos-"
    "biologicos/sala-especializada-medicamentos-sintesis",
)
ACTAS_CATALOG_PATH = Path(
    os.getenv("ACTAS_CATALOG_PATH", ROOT_DIR / "actas_catalog.csv")
)
RAW_DATABASE_PATH = Path(os.getenv("ACTAS_DATABASE_PATH", DATA_DIR / "actas.db"))
DATABASE_PATH = resolve_database_path(RAW_DATABASE_PATH)
INTEGRITY_REPORT_PATH = Path(
    os.getenv("ACTAS_INTEGRITY_REPORT_PATH", DATA_DIR / "integrity-report.json")
)
INDEXING_REPORT_PATH = Path(
    os.getenv("ACTAS_INDEXING_REPORT_PATH", DATA_DIR / "indexing-report.json")
)
CATALOG_REPORT_PATH = Path(
    os.getenv("ACTAS_CATALOG_REPORT_PATH", DATA_DIR / "catalog-report.json")
)

CHUNK_SIZE = int(os.getenv("ACTAS_CHUNK_SIZE", "1200"))
CHUNK_OVERLAP = int(os.getenv("ACTAS_CHUNK_OVERLAP", "180"))
DEFAULT_TOP_K = int(os.getenv("ACTAS_TOP_K", "8"))
MAX_CONTEXT_CHARS = int(os.getenv("ACTAS_MAX_CONTEXT_CHARS", "14000"))
MAX_PDF_BYTES = int(os.getenv("ACTAS_MAX_PDF_BYTES", str(120 * 1024 * 1024)))
INDEX_START_YEAR = int(os.getenv("ACTAS_INDEX_START_YEAR", "2013"))
OCR_ENABLED = os.getenv("ACTAS_OCR_ENABLED", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
OCR_LANGUAGES = os.getenv("ACTAS_OCR_LANGUAGES", "spa+eng").strip() or "spa+eng"
OCR_DPI = int(os.getenv("ACTAS_OCR_DPI", "180"))
OCR_TIMEOUT_SECONDS = int(os.getenv("ACTAS_OCR_TIMEOUT_SECONDS", "120"))
OCR_MIN_CHARS = int(os.getenv("ACTAS_OCR_MIN_CHARS", "20"))

ALLOWED_DOCUMENT_HOSTS = tuple(
    host.strip().lower()
    for host in os.getenv(
        "ACTAS_ALLOWED_HOSTS",
        "invima.gov.co,www.invima.gov.co,img.lalr.co",
    ).split(",")
    if host.strip()
)


def ensure_directories() -> None:
    for path in (DATA_DIR, CACHE_DIR, PDF_CACHE_DIR):
        path.mkdir(parents=True, exist_ok=True)
