from __future__ import annotations

import gzip
import io
import os
import shutil
import tempfile
from pathlib import Path


_SQLITE_HEADER = b"SQLite format 3\x00"


class _MultipartReader(io.RawIOBase):
    """Presenta varios fragmentos consecutivos como una sola secuencia binaria."""

    def __init__(self, paths: list[Path]) -> None:
        self._paths = iter(paths)
        self._current = None

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        view = memoryview(buffer)
        total = 0
        while total < len(view):
            if self._current is None:
                try:
                    self._current = next(self._paths).open("rb")
                except StopIteration:
                    break
            count = self._current.readinto(view[total:])
            if count:
                total += count
                continue
            self._current.close()
            self._current = None
        return total

    def close(self) -> None:
        if self._current is not None:
            self._current.close()
            self._current = None
        super().close()


def _is_sqlite(path: Path) -> bool:
    if not path.exists() or path.stat().st_size < len(_SQLITE_HEADER):
        return False
    with path.open("rb") as handle:
        return handle.read(len(_SQLITE_HEADER)) == _SQLITE_HEADER


def resolve_database_path(database_path: Path) -> Path:
    """Materializa el índice comprimido del repositorio en un directorio temporal."""
    if database_path.exists() or os.getenv("ACTAS_DISABLE_DATABASE_PACKAGE") == "1":
        return database_path

    parts = sorted(database_path.parent.glob(f"{database_path.name}.gz.part-*"))
    gzip_file = database_path.with_name(f"{database_path.name}.gz")
    sources = parts or ([gzip_file] if gzip_file.exists() else [])
    if not sources:
        return database_path

    runtime_root = Path(
        os.getenv(
            "ACTAS_RUNTIME_DATA_DIR",
            Path(tempfile.gettempdir()) / "explorador-actas-invima",
        )
    )
    runtime_root.mkdir(parents=True, exist_ok=True)
    runtime_database = runtime_root / database_path.name
    newest_source = max(path.stat().st_mtime for path in sources)

    if (
        _is_sqlite(runtime_database)
        and runtime_database.stat().st_mtime >= newest_source
    ):
        return runtime_database

    temporary_database = runtime_database.with_name(
        f"{runtime_database.name}.{os.getpid()}.part"
    )
    temporary_database.unlink(missing_ok=True)

    try:
        with _MultipartReader(sources) as raw_source:
            with gzip.GzipFile(fileobj=raw_source, mode="rb") as source:
                with temporary_database.open("wb") as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
        if not _is_sqlite(temporary_database):
            raise ValueError("El paquete del índice no contiene una base SQLite válida")
        temporary_database.replace(runtime_database)
    finally:
        temporary_database.unlink(missing_ok=True)

    return runtime_database

