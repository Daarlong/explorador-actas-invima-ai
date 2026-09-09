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
RAW_SEMANTIC_INDEX_PATH = Path(
    os.getenv("ACTAS_SEMANTIC_INDEX_PATH", DATA_DIR / "semantic.db")
)
SEMANTIC_INDEX_PATH = resolve_database_path(RAW_SEMANTIC_INDEX_PATH)
SEMANTIC_CHECKPOINT_PATH = Path(
    os.getenv(
        "ACTAS_SEMANTIC_CHECKPOINT_PATH",
        RAW_SEMANTIC_INDEX_PATH.with_name("semantic.checkpoint.db"),
    )
)
SEMANTIC_PROGRESS_PATH = Path(
    os.getenv(
        "ACTAS_SEMANTIC_PROGRESS_PATH",
        DATA_DIR / "semantic-progress.json",
    )
)
INTEGRITY_REPORT_PATH = Path(
    os.getenv("ACTAS_INTEGRITY_REPORT_PATH", DATA_DIR / "integrity-report.json")
)
INDEXING_REPORT_PATH = Path(
    os.getenv("ACTAS_INDEXING_REPORT_PATH", DATA_DIR / "indexing-report.json")
)
SEMANTIC_REPORT_PATH = Path(
    os.getenv("ACTAS_SEMANTIC_REPORT_PATH", DATA_DIR / "semantic-report.json")
)
CATALOG_REPORT_PATH = Path(
    os.getenv("ACTAS_CATALOG_REPORT_PATH", DATA_DIR / "catalog-report.json")
)
SOURCE_SNAPSHOT_PATH = Path(
    os.getenv("ACTAS_SOURCE_SNAPSHOT_PATH", DATA_DIR / "source-snapshot.json")
)
REVIEW_LOG_PATH = Path(
    os.getenv("ACTAS_REVIEW_LOG_PATH", DATA_DIR / "regulatory-review-log.csv")
)
REPROCESS_ROOT = Path(
    os.getenv("ACTAS_REPROCESS_ROOT", ROOT_DIR / ".reprocess")
)
REPROCESS_REPORT_PATH = Path(
    os.getenv(
        "ACTAS_REPROCESS_REPORT_PATH",
        REPROCESS_ROOT / "reprocess-report.json",
    )
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
SEMANTIC_ENABLED = os.getenv("ACTAS_SEMANTIC_ENABLED", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
SEMANTIC_LEXICAL_DIMENSION = int(
    os.getenv("ACTAS_SEMANTIC_LEXICAL_DIMENSION", "128")
)
SEMANTIC_DISTRIBUTIONAL_DIMENSION = int(
    os.getenv("ACTAS_SEMANTIC_DISTRIBUTIONAL_DIMENSION", "48")
)
SEMANTIC_BACKEND = os.getenv("ACTAS_SEMANTIC_BACKEND", "neural").strip().lower()
if SEMANTIC_BACKEND not in {"local", "neural"}:
    raise ValueError("ACTAS_SEMANTIC_BACKEND debe ser local o neural")
SEMANTIC_NEURAL_ENABLED = SEMANTIC_BACKEND == "neural"
SEMANTIC_NEURAL_MODEL_ID = os.getenv(
    "ACTAS_SEMANTIC_NEURAL_MODEL_ID",
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
).strip()
SEMANTIC_NEURAL_MODEL_REVISION = os.getenv(
    "ACTAS_SEMANTIC_NEURAL_MODEL_REVISION",
    "fastembed-0.8.0-registry",
).strip()
SEMANTIC_NEURAL_BATCH_SIZE = max(
    1,
    int(os.getenv("ACTAS_SEMANTIC_NEURAL_BATCH_SIZE", "64")),
)
SEMANTIC_MAX_UNIQUE = max(
    0,
    int(os.getenv("ACTAS_SEMANTIC_MAX_UNIQUE", "0")),
)
SEMANTIC_MAX_SECONDS = max(
    0.0,
    float(os.getenv("ACTAS_SEMANTIC_MAX_SECONDS", "0")),
)
REPROCESS_BATCH_SIZE = max(1, int(os.getenv("ACTAS_REPROCESS_BATCH_SIZE", "50")))
REPROCESS_MIN_FREE_MIB = max(
    512,
    int(os.getenv("ACTAS_REPROCESS_MIN_FREE_MIB", "4096")),
)

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
