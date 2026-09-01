from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import tempfile
from pathlib import Path


_SQLITE_HEADER = b"SQLite format 3\x00"
PACKAGE_FORMAT_VERSION = 1


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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_sqlite(path: Path) -> bool:
    if not path.exists() or path.stat().st_size < len(_SQLITE_HEADER):
        return False
    with path.open("rb") as handle:
        return handle.read(len(_SQLITE_HEADER)) == _SQLITE_HEADER


def package_manifest_path(database_path: Path) -> Path:
    return database_path.with_name(f"{database_path.name}.package.json")


def _load_package(database_path: Path) -> tuple[list[Path], dict | None]:
    manifest_path = package_manifest_path(database_path)
    if manifest_path.exists():
        try:
            package = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("El manifiesto del índice comprimido no es válido") from exc
        if package.get("format_version") != PACKAGE_FORMAT_VERSION:
            raise ValueError("Versión desconocida del paquete del índice")
        parts: list[Path] = []
        for item in package.get("parts", []):
            name = str(item.get("name", ""))
            if Path(name).name != name or not name.startswith(
                f"{database_path.name}.gz.part-"
            ):
                raise ValueError("Nombre de fragmento no permitido")
            path = database_path.parent / name
            if not path.exists():
                raise ValueError(f"Falta el fragmento del índice: {name}")
            if path.stat().st_size != int(item.get("bytes", -1)):
                raise ValueError(f"Tamaño incorrecto para el fragmento: {name}")
            if _sha256(path) != item.get("sha256"):
                raise ValueError(f"Hash incorrecto para el fragmento: {name}")
            parts.append(path)
        if not parts:
            raise ValueError("El paquete del índice no contiene fragmentos")
        return parts, package

    parts = sorted(database_path.parent.glob(f"{database_path.name}.gz.part-*"))
    gzip_file = database_path.with_name(f"{database_path.name}.gz")
    if parts:
        return parts, None
    if gzip_file.exists():
        return [gzip_file], None
    return [], None


def materialize_database_package(
    database_path: Path,
    target_path: Path,
) -> Path:
    sources, package = _load_package(database_path)
    if not sources:
        return target_path

    target_path.parent.mkdir(parents=True, exist_ok=True)
    identity = (
        str(package.get("database_sha256"))
        if package
        else str(max(path.stat().st_mtime_ns for path in sources))
    )
    marker_path = target_path.with_name(f"{target_path.name}.package-id")
    if (
        _is_sqlite(target_path)
        and marker_path.exists()
        and marker_path.read_text(encoding="utf-8").strip() == identity
    ):
        return target_path

    temporary_database = target_path.with_name(
        f"{target_path.name}.{os.getpid()}.part"
    )
    temporary_database.unlink(missing_ok=True)
    digest = hashlib.sha256()
    written = 0

    try:
        with _MultipartReader(sources) as raw_source:
            with gzip.GzipFile(fileobj=raw_source, mode="rb") as source:
                with temporary_database.open("wb") as target:
                    while block := source.read(1024 * 1024):
                        target.write(block)
                        digest.update(block)
                        written += len(block)
        if not _is_sqlite(temporary_database):
            raise ValueError("El paquete no contiene una base SQLite válida")
        if package:
            if written != int(package.get("database_bytes", -1)):
                raise ValueError("El tamaño descomprimido del índice no coincide")
            if digest.hexdigest() != package.get("database_sha256"):
                raise ValueError("El hash descomprimido del índice no coincide")
        temporary_database.replace(target_path)
        marker_path.write_text(identity, encoding="utf-8")
    finally:
        temporary_database.unlink(missing_ok=True)

    return target_path


def resolve_database_path(database_path: Path) -> Path:
    """Materializa el índice del repositorio en un directorio temporal."""
    if database_path.exists() or os.getenv("ACTAS_DISABLE_DATABASE_PACKAGE") == "1":
        return database_path

    runtime_root = Path(
        os.getenv(
            "ACTAS_RUNTIME_DATA_DIR",
            Path(tempfile.gettempdir()) / "explorador-actas-invima",
        )
    )
    materialized = materialize_database_package(
        database_path,
        runtime_root / database_path.name,
    )
    return materialized if materialized.exists() else database_path


def create_database_package(
    database_path: Path,
    *,
    part_size_bytes: int = 90 * 1024 * 1024,
    delete_source: bool = False,
) -> dict:
    if part_size_bytes < 1024:
        raise ValueError("El tamaño de fragmento es demasiado pequeño")
    if not _is_sqlite(database_path):
        raise ValueError("No existe una base SQLite válida para empaquetar")

    database_path.parent.mkdir(parents=True, exist_ok=True)
    gzip_path = database_path.with_name(f"{database_path.name}.gz.building")
    gzip_path.unlink(missing_ok=True)
    for old_part in database_path.parent.glob(f"{database_path.name}.gz.part-*"):
        old_part.unlink()

    try:
        with database_path.open("rb") as source:
            with gzip_path.open("wb") as raw_target:
                with gzip.GzipFile(
                    filename="",
                    mode="wb",
                    compresslevel=9,
                    fileobj=raw_target,
                    mtime=0,
                ) as target:
                    while block := source.read(1024 * 1024):
                        target.write(block)

        parts: list[dict] = []
        with gzip_path.open("rb") as source:
            for index in range(1000):
                payload = source.read(part_size_bytes)
                if not payload:
                    break
                part_name = f"{database_path.name}.gz.part-{index:03d}"
                part_path = database_path.parent / part_name
                part_path.write_bytes(payload)
                parts.append(
                    {
                        "name": part_name,
                        "bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                )
            if source.read(1):
                raise ValueError("El índice requiere más de 1000 fragmentos")

        package = {
            "format_version": PACKAGE_FORMAT_VERSION,
            "database_file": database_path.name,
            "database_bytes": database_path.stat().st_size,
            "database_sha256": _sha256(database_path),
            "compressed_bytes": gzip_path.stat().st_size,
            "part_size_bytes": part_size_bytes,
            "parts": parts,
        }
        manifest_path = package_manifest_path(database_path)
        temporary_manifest = manifest_path.with_suffix(".json.building")
        temporary_manifest.write_text(
            json.dumps(package, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary_manifest.replace(manifest_path)
    finally:
        gzip_path.unlink(missing_ok=True)

    if delete_source:
        database_path.unlink()
    return package
