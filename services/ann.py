"""Índice ANN angular, portable y verificable para los embeddings neuronales.

El índice usa *locality-sensitive hashing* (LSH) con hiperplanos aleatorios
determinísticos.  No duplica los vectores neuronales: ``semantic-ann.db`` solo
guarda las firmas y las listas compactas de ``item_id``; el coseno final se
calcula contra los vectores cuantizados que ya existen en ``semantic.db``.

La construcción requiere NumPy para procesar el corpus por lotes. La consulta
también lo usa para el reranking exacto de unos pocos miles de candidatos. No
se ejecuta código serializado ni se carga una extensión nativa de SQLite.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import os
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence


ANN_FORMAT_VERSION = 1
ANN_METHOD = "angular-lsh-rademacher-v1"
DEFAULT_TABLE_COUNT = 12
DEFAULT_BITS_PER_TABLE = 14
DEFAULT_BUILD_BATCH_SIZE = 4_096
DEFAULT_MAX_PROBE_RADIUS = 2
DEFAULT_MAX_CANDIDATES = 50_000
_MAX_SQL_PARAMETERS = 800


class AnnIndexError(RuntimeError):
    """El índice ANN no existe, está dañado o no corresponde al corpus."""


class AnnRuntimeUnavailable(RuntimeError):
    """Falta una dependencia necesaria para construir o consultar el ANN."""


@dataclass(frozen=True)
class AnnBuildSummary:
    items_indexed: int
    table_count: int
    bits_per_table: int
    bucket_count: int
    memberships: int
    neural_dimension: int
    source_fingerprint: str
    semantic_build_signature: str
    index_size_bytes: int

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class AnnHit:
    item_id: int
    score: float
    collisions: int

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class AnnQueryTrace:
    engine: str
    degraded: bool
    message: str
    probe_radius: int
    probes: int
    candidates_retrieved: int
    candidates_after_filter: int
    candidates_scored: int
    candidate_cap_applied: bool
    exact_filter_fallback: bool

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class AnnQueryResult:
    hits: list[AnnHit]
    trace: AnnQueryTrace

    def as_dict(self) -> dict:
        return {
            "hits": [hit.as_dict() for hit in self.hits],
            "trace": self.trace.as_dict(),
        }


def _numpy():
    try:
        import numpy as np
    except (ImportError, ModuleNotFoundError) as exc:
        raise AnnRuntimeUnavailable(
            "NumPy no está disponible para la recuperación neuronal global"
        ) from exc
    return np


def _read_json_metadata(
    connection: sqlite3.Connection,
    *,
    label: str,
) -> dict[str, object]:
    try:
        rows = connection.execute("SELECT key, value FROM metadata").fetchall()
    except sqlite3.Error as exc:
        raise AnnIndexError(f"{label} no contiene metadatos válidos") from exc
    metadata: dict[str, object] = {}
    for key, value in rows:
        try:
            metadata[str(key)] = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise AnnIndexError(f"Los metadatos de {label} están dañados") from exc
    return metadata


def _semantic_metadata(connection: sqlite3.Connection) -> dict[str, object]:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if not {"metadata", "items", "neural_embeddings"}.issubset(tables):
        raise AnnIndexError(
            "semantic.db no contiene los embeddings neuronales reutilizables"
        )
    metadata = _read_json_metadata(connection, label="semantic.db")
    required = {
        "documents",
        "source_fingerprint",
        "build_signature",
        "neural_status",
        "neural_dimension",
        "neural_model_id",
        "neural_model_revision",
        "neural_vector_encoding",
    }
    if not required.issubset(metadata):
        raise AnnIndexError("semantic.db no contiene metadatos neuronales completos")
    if metadata.get("neural_status") != "ready":
        raise AnnIndexError("Los embeddings neuronales todavía no están completos")
    try:
        documents = int(metadata["documents"])
        neural_documents = int(metadata.get("neural_documents", documents))
        dimension = int(metadata["neural_dimension"])
    except (TypeError, ValueError) as exc:
        raise AnnIndexError("Los metadatos neuronales son incompatibles") from exc
    if documents < 1 or dimension < 1 or neural_documents != documents:
        raise AnnIndexError("La cobertura neuronal de semantic.db es inválida")
    for field in (
        "source_fingerprint",
        "build_signature",
        "neural_model_id",
        "neural_model_revision",
        "neural_vector_encoding",
    ):
        if not str(metadata.get(field, "")).strip():
            raise AnnIndexError(f"Falta {field} en semantic.db")
    return metadata


def _ann_identity(metadata: Mapping[str, object]) -> str:
    identity = {
        "source_fingerprint": str(metadata["source_fingerprint"]),
        "semantic_build_signature": str(
            metadata.get("semantic_build_signature", metadata.get("build_signature", ""))
        ),
        "neural_model_id": str(metadata["neural_model_id"]),
        "neural_model_revision": str(metadata["neural_model_revision"]),
        "neural_vector_encoding": str(metadata["neural_vector_encoding"]),
        "neural_dimension": int(metadata["neural_dimension"]),
        "items": int(metadata.get("items", metadata.get("documents", 0))),
    }
    payload = json.dumps(
        identity,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _expected_identity_from_semantic(metadata: Mapping[str, object]) -> str:
    return _ann_identity(
        {
            "source_fingerprint": metadata["source_fingerprint"],
            "semantic_build_signature": metadata["build_signature"],
            "neural_model_id": metadata["neural_model_id"],
            "neural_model_revision": metadata["neural_model_revision"],
            "neural_vector_encoding": metadata["neural_vector_encoding"],
            "neural_dimension": metadata["neural_dimension"],
            "items": metadata["documents"],
        }
    )


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode = DELETE;
        PRAGMA synchronous = NORMAL;

        CREATE TABLE metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE ann_tables (
            table_id INTEGER PRIMARY KEY,
            hyperplanes BLOB NOT NULL
        );

        CREATE TABLE ann_buckets (
            table_id INTEGER NOT NULL,
            signature INTEGER NOT NULL,
            member_count INTEGER NOT NULL,
            item_ids BLOB NOT NULL,
            PRIMARY KEY (table_id, signature)
        ) WITHOUT ROWID;
        """
    )


def _write_metadata(
    connection: sqlite3.Connection,
    values: Mapping[str, object],
) -> None:
    connection.executemany(
        "INSERT INTO metadata (key, value) VALUES (?, ?)",
        (
            (key, json.dumps(value, ensure_ascii=False, sort_keys=True))
            for key, value in values.items()
        ),
    )


def _rademacher_hyperplanes(
    table_count: int,
    bits_per_table: int,
    dimension: int,
):
    """Genera hiperplanos +/-1 estables entre versiones de NumPy."""

    np = _numpy()
    planes = np.empty(
        (table_count, bits_per_table, dimension),
        dtype=np.int8,
    )
    for table_id in range(table_count):
        for bit in range(bits_per_table):
            seed = f"invima-ann-lsh-v1:{table_id}:{bit}".encode("ascii")
            raw = np.frombuffer(
                hashlib.shake_256(seed).digest(dimension),
                dtype=np.uint8,
            )
            planes[table_id, bit] = np.where(raw & 1, 1, -1).astype(np.int8)
    return planes


def _signatures_for_vectors(vectors, planes):
    np = _numpy()
    table_count, bits_per_table, dimension = planes.shape
    if vectors.ndim != 2 or vectors.shape[1] != dimension:
        raise AnnIndexError("La matriz neuronal tiene una dimensión incompatible")
    flat_planes = planes.reshape(table_count * bits_per_table, dimension)
    projections = vectors.astype(np.float32) @ flat_planes.astype(np.float32).T
    signs = projections.reshape(-1, table_count, bits_per_table) >= 0
    weights = (1 << np.arange(bits_per_table, dtype=np.uint32)).reshape(1, 1, -1)
    return (signs * weights).sum(axis=2, dtype=np.uint32)


def _encode_unsigned_varint(value: int, target: bytearray) -> None:
    if value < 0:
        raise ValueError("No se puede codificar un entero negativo")
    while value >= 0x80:
        target.append((value & 0x7F) | 0x80)
        value >>= 7
    target.append(value)


def _encode_delta_ids(values: Iterable[int]) -> bytes:
    payload = bytearray()
    previous = 0
    first = True
    for raw_value in values:
        value = int(raw_value)
        if value < 0 or (not first and value <= previous):
            raise AnnIndexError("Los identificadores del bucket no están ordenados")
        _encode_unsigned_varint(value if first else value - previous, payload)
        previous = value
        first = False
    return bytes(payload)


def _decode_delta_ids(payload: bytes, expected_count: int) -> list[int]:
    values: list[int] = []
    current = 0
    shift = 0
    previous = 0
    for byte in payload:
        current |= (byte & 0x7F) << shift
        if byte & 0x80:
            shift += 7
            if shift > 63:
                raise AnnIndexError("Un bucket ANN contiene un varint inválido")
            continue
        value = current if not values else previous + current
        if value < 0 or (values and value <= previous):
            raise AnnIndexError("Un bucket ANN contiene IDs inválidos")
        values.append(value)
        previous = value
        current = 0
        shift = 0
    if shift or len(values) != expected_count:
        raise AnnIndexError("El tamaño declarado de un bucket ANN no coincide")
    return values


def _validate_build_parameters(
    *,
    table_count: int,
    bits_per_table: int,
    batch_size: int,
) -> None:
    if not 1 <= table_count <= 64:
        raise ValueError("table_count debe estar entre 1 y 64")
    # Las firmas se guardan como INTEGER de SQLite y se enumeran al consultar.
    if not 2 <= bits_per_table <= 24:
        raise ValueError("bits_per_table debe estar entre 2 y 24")
    if batch_size < 1:
        raise ValueError("batch_size debe ser mayor que cero")


def build_ann_index(
    semantic_index_path: Path,
    ann_index_path: Path,
    *,
    table_count: int = DEFAULT_TABLE_COUNT,
    bits_per_table: int = DEFAULT_BITS_PER_TABLE,
    batch_size: int = DEFAULT_BUILD_BATCH_SIZE,
) -> AnnBuildSummary:
    """Construye ``semantic-ann.db`` usando los embeddings ya persistidos.

    La escritura es atómica. Esta función no recibe ni instancia un codificador,
    por lo que no puede volver a calcular embeddings accidentalmente.
    """

    _validate_build_parameters(
        table_count=table_count,
        bits_per_table=bits_per_table,
        batch_size=batch_size,
    )
    np = _numpy()
    semantic_index_path = Path(semantic_index_path)
    ann_index_path = Path(ann_index_path)
    if not semantic_index_path.is_file():
        raise AnnIndexError("No existe semantic.db")
    if semantic_index_path.resolve() == ann_index_path.resolve():
        raise ValueError("semantic.db y semantic-ann.db deben ser archivos distintos")

    try:
        semantic = sqlite3.connect(
            f"file:{semantic_index_path}?mode=ro",
            uri=True,
        )
    except sqlite3.Error as exc:
        raise AnnIndexError("No fue posible abrir semantic.db") from exc

    temporary_path = ann_index_path.with_name(
        f".{ann_index_path.name}.building-{os.getpid()}"
    )
    temporary_path.unlink(missing_ok=True)
    try:
        semantic_metadata = _semantic_metadata(semantic)
        item_count = int(semantic_metadata["documents"])
        dimension = int(semantic_metadata["neural_dimension"])
        actual_items = int(
            semantic.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        )
        covered_items = int(
            semantic.execute(
                """
                SELECT COUNT(*)
                FROM items AS item
                JOIN neural_embeddings AS neural
                  ON neural.content_hash = item.content_hash
                WHERE length(neural.neural_vector) = ?
                """,
                (dimension,),
            ).fetchone()[0]
        )
        if actual_items != item_count or covered_items != item_count:
            raise AnnIndexError(
                "semantic.db no contiene un vector válido por fragmento"
            )

        planes = _rademacher_hyperplanes(
            table_count,
            bits_per_table,
            dimension,
        )
        item_ids = np.empty(item_count, dtype=np.int64)
        signatures = np.empty((item_count, table_count), dtype=np.uint32)
        cursor = semantic.execute(
            """
            SELECT item.item_id, neural.neural_vector
            FROM items AS item
            JOIN neural_embeddings AS neural
              ON neural.content_hash = item.content_hash
            ORDER BY item.item_id
            """
        )
        position = 0
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break
            vectors = np.empty((len(rows), dimension), dtype=np.int8)
            batch_ids = np.empty(len(rows), dtype=np.int64)
            for row_index, (item_id, vector_blob) in enumerate(rows):
                vector = np.frombuffer(vector_blob, dtype=np.int8)
                if vector.size != dimension:
                    raise AnnIndexError(
                        "semantic.db contiene un vector neuronal incompatible"
                    )
                batch_ids[row_index] = int(item_id)
                vectors[row_index] = vector
            stop = position + len(rows)
            item_ids[position:stop] = batch_ids
            signatures[position:stop] = _signatures_for_vectors(vectors, planes)
            position = stop
        if position != item_count:
            raise AnnIndexError("Cambió semantic.db durante la construcción ANN")
        if item_count > 1 and bool(np.any(item_ids[1:] <= item_ids[:-1])):
            raise AnnIndexError("semantic.db contiene item_id duplicados o desordenados")

        ann_index_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(temporary_path) as target:
            _create_schema(target)
            target.executemany(
                "INSERT INTO ann_tables (table_id, hyperplanes) VALUES (?, ?)",
                (
                    (table_id, planes[table_id].tobytes(order="C"))
                    for table_id in range(table_count)
                ),
            )
            bucket_count = 0
            memberships = 0
            for table_id in range(table_count):
                table_signatures = signatures[:, table_id]
                # ``item_ids`` ya está ordenado; el sort estable conserva ese
                # orden dentro de cada firma y permite la codificación delta.
                order = np.argsort(table_signatures, kind="stable")
                ordered_signatures = table_signatures[order]
                ordered_ids = item_ids[order]
                if item_count:
                    boundaries = np.flatnonzero(
                        ordered_signatures[1:] != ordered_signatures[:-1]
                    ) + 1
                    starts = np.concatenate(
                        (np.array([0], dtype=np.int64), boundaries)
                    )
                    stops = np.concatenate(
                        (boundaries, np.array([item_count], dtype=np.int64))
                    )
                    rows_to_insert: list[tuple[int, int, int, bytes]] = []
                    for start, stop in zip(starts.tolist(), stops.tolist()):
                        members = ordered_ids[start:stop]
                        member_count = int(stop - start)
                        rows_to_insert.append(
                            (
                                table_id,
                                int(ordered_signatures[start]),
                                member_count,
                                _encode_delta_ids(members.tolist()),
                            )
                        )
                        if len(rows_to_insert) >= 1_000:
                            target.executemany(
                                "INSERT INTO ann_buckets VALUES (?, ?, ?, ?)",
                                rows_to_insert,
                            )
                            rows_to_insert.clear()
                        bucket_count += 1
                        memberships += member_count
                    if rows_to_insert:
                        target.executemany(
                            "INSERT INTO ann_buckets VALUES (?, ?, ?, ?)",
                            rows_to_insert,
                        )

            ann_metadata = {
                "format_version": ANN_FORMAT_VERSION,
                "method": ANN_METHOD,
                "source_fingerprint": str(
                    semantic_metadata["source_fingerprint"]
                ),
                "semantic_build_signature": str(
                    semantic_metadata["build_signature"]
                ),
                "neural_model_id": str(semantic_metadata["neural_model_id"]),
                "neural_model_revision": str(
                    semantic_metadata["neural_model_revision"]
                ),
                "neural_vector_encoding": str(
                    semantic_metadata["neural_vector_encoding"]
                ),
                "neural_dimension": dimension,
                "items": item_count,
                "table_count": table_count,
                "bits_per_table": bits_per_table,
                "bucket_count": bucket_count,
                "memberships": memberships,
            }
            ann_metadata["semantic_identity"] = _ann_identity(ann_metadata)
            _write_metadata(target, ann_metadata)
            target.commit()
            quick_check = str(target.execute("PRAGMA quick_check").fetchone()[0])
            if quick_check.lower() != "ok":
                raise AnnIndexError("La candidata ANN no supera integridad SQLite")

        # Valida también cobertura y coherencia antes de tocar una versión
        # publicada que pudiera seguir prestando servicio.
        state = ann_index_status(temporary_path, semantic_index_path)
        if not state.get("available"):
            raise AnnIndexError(str(state.get("message", "Índice ANN inválido")))
        os.replace(temporary_path, ann_index_path)
        return AnnBuildSummary(
            items_indexed=item_count,
            table_count=table_count,
            bits_per_table=bits_per_table,
            bucket_count=int(state["bucket_count"]),
            memberships=int(state["memberships"]),
            neural_dimension=dimension,
            source_fingerprint=str(semantic_metadata["source_fingerprint"]),
            semantic_build_signature=str(semantic_metadata["build_signature"]),
            index_size_bytes=ann_index_path.stat().st_size,
        )
    except sqlite3.Error as exc:
        raise AnnIndexError("No fue posible construir el índice ANN") from exc
    finally:
        semantic.close()
        temporary_path.unlink(missing_ok=True)


def ann_index_status(
    ann_index_path: Path,
    semantic_index_path: Path | None = None,
) -> dict[str, object]:
    """Valida estructura, cobertura y correspondencia con ``semantic.db``."""

    ann_index_path = Path(ann_index_path)
    if not ann_index_path.is_file():
        return {
            "available": False,
            "reason": "missing",
            "message": "El índice ANN neuronal aún no ha sido construido.",
        }
    try:
        with sqlite3.connect(
            f"file:{ann_index_path}?mode=ro",
            uri=True,
        ) as connection:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if not {"metadata", "ann_tables", "ann_buckets"}.issubset(tables):
                raise AnnIndexError("El índice ANN no contiene todas sus tablas")
            metadata = _read_json_metadata(connection, label="semantic-ann.db")
            required = {
                "format_version",
                "method",
                "source_fingerprint",
                "semantic_build_signature",
                "semantic_identity",
                "neural_model_id",
                "neural_model_revision",
                "neural_vector_encoding",
                "neural_dimension",
                "items",
                "table_count",
                "bits_per_table",
                "bucket_count",
                "memberships",
            }
            if not required.issubset(metadata):
                raise AnnIndexError("Los metadatos del índice ANN están incompletos")
            if int(metadata["format_version"]) != ANN_FORMAT_VERSION:
                raise AnnIndexError("La versión del índice ANN no es compatible")
            if metadata["method"] != ANN_METHOD:
                raise AnnIndexError("El método del índice ANN no es compatible")
            table_count = int(metadata["table_count"])
            bits_per_table = int(metadata["bits_per_table"])
            dimension = int(metadata["neural_dimension"])
            items = int(metadata["items"])
            declared_buckets = int(metadata["bucket_count"])
            declared_memberships = int(metadata["memberships"])
            _validate_build_parameters(
                table_count=table_count,
                bits_per_table=bits_per_table,
                batch_size=1,
            )
            if dimension < 1 or items < 1:
                raise AnnIndexError("Las dimensiones del índice ANN son inválidas")
            if str(metadata["semantic_identity"]) != _ann_identity(metadata):
                raise AnnIndexError("La identidad declarada del índice ANN es inválida")
            plane_rows = connection.execute(
                "SELECT table_id, length(hyperplanes) FROM ann_tables "
                "ORDER BY table_id"
            ).fetchall()
            if plane_rows != [
                (table_id, bits_per_table * dimension)
                for table_id in range(table_count)
            ]:
                raise AnnIndexError("Los hiperplanos del índice ANN son inválidos")
            bucket_count, memberships = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(member_count), 0) FROM ann_buckets"
            ).fetchone()
            if (
                int(bucket_count) != declared_buckets
                or int(memberships) != declared_memberships
                or declared_memberships != items * table_count
            ):
                raise AnnIndexError("La cobertura declarada del índice ANN no coincide")

        if semantic_index_path is not None:
            semantic_index_path = Path(semantic_index_path)
            if not semantic_index_path.is_file():
                return {
                    "available": False,
                    "reason": "semantic_missing",
                    "message": "No existe semantic.db para validar el índice ANN.",
                }
            with sqlite3.connect(
                f"file:{semantic_index_path}?mode=ro",
                uri=True,
            ) as semantic:
                semantic_metadata = _semantic_metadata(semantic)
            if str(metadata["semantic_identity"]) != _expected_identity_from_semantic(
                semantic_metadata
            ):
                return {
                    "available": False,
                    "reason": "stale",
                    "message": (
                        "El índice ANN no corresponde a los embeddings neuronales "
                        "activos."
                    ),
                    **metadata,
                }
        return {
            "available": True,
            "reason": "ready",
            "message": "Recuperación neuronal global ANN disponible.",
            "size_bytes": ann_index_path.stat().st_size,
            **metadata,
        }
    except (OSError, TypeError, ValueError, sqlite3.Error, AnnIndexError) as exc:
        return {
            "available": False,
            "reason": "invalid",
            "message": str(exc),
        }


def _signatures_with_exact_hamming_radius(
    signature: int,
    bits: int,
    radius: int,
) -> list[int]:
    if radius == 0:
        return [signature]
    return [
        signature
        ^ sum(1 << bit for bit in flipped_bits)
        for flipped_bits in itertools.combinations(range(bits), radius)
    ]


def _query_signature(query_vector, hyperplanes, bits_per_table: int) -> int:
    np = _numpy()
    projections = hyperplanes.astype(np.float32) @ query_vector.astype(np.float32)
    signature = 0
    for bit, positive in enumerate((projections >= 0).tolist()):
        if positive:
            signature |= 1 << bit
    if signature >= 1 << bits_per_table:
        raise AnnIndexError("La firma ANN calculada es inválida")
    return signature


def _load_candidates(
    connection: sqlite3.Connection,
    signatures: Sequence[int],
    *,
    bits_per_table: int,
    max_probe_radius: int,
    allowed: set[int] | None,
    minimum_candidates: int,
) -> tuple[Counter[int], int, int, int, int]:
    collisions: Counter[int] = Counter()
    retrieved_ids: set[int] = set()
    filtered_ids: set[int] = set()
    probes = 0
    reached_radius = 0
    for radius in range(max_probe_radius + 1):
        reached_radius = radius
        for table_id, signature in enumerate(signatures):
            variants = _signatures_with_exact_hamming_radius(
                int(signature),
                bits_per_table,
                radius,
            )
            probes += len(variants)
            for start in range(0, len(variants), _MAX_SQL_PARAMETERS):
                batch = variants[start : start + _MAX_SQL_PARAMETERS]
                placeholders = ",".join("?" for _ in batch)
                rows = connection.execute(
                    f"""
                    SELECT member_count, item_ids
                    FROM ann_buckets
                    WHERE table_id = ?
                      AND signature IN ({placeholders})
                    """,
                    [table_id, *batch],
                ).fetchall()
                for member_count, payload in rows:
                    members = _decode_delta_ids(bytes(payload), int(member_count))
                    retrieved_ids.update(members)
                    for item_id in members:
                        if allowed is None or item_id in allowed:
                            filtered_ids.add(item_id)
                            collisions[item_id] += 1
        if len(filtered_ids) >= minimum_candidates:
            break
    return collisions, len(retrieved_ids), len(filtered_ids), probes, reached_radius


def _normalized_query_vector(vector: Sequence[float], dimension: int):
    np = _numpy()
    values = np.asarray(vector, dtype=np.float32)
    if values.ndim != 1 or values.size != dimension:
        raise ValueError("El vector de consulta tiene una dimensión incompatible")
    if not bool(np.all(np.isfinite(values))):
        raise ValueError("El vector de consulta contiene valores no finitos")
    norm = float(np.linalg.norm(values))
    if not math.isfinite(norm) or norm <= 0.0:
        raise ValueError("El vector de consulta está vacío")
    return values / norm


def _score_candidates(
    semantic: sqlite3.Connection,
    candidate_ids: Sequence[int],
    query_vector,
    *,
    dimension: int,
    top_k: int,
    min_score: float,
    collisions: Mapping[int, int],
) -> list[AnnHit]:
    np = _numpy()
    hits: list[AnnHit] = []
    found = 0
    for start in range(0, len(candidate_ids), _MAX_SQL_PARAMETERS):
        batch = candidate_ids[start : start + _MAX_SQL_PARAMETERS]
        placeholders = ",".join("?" for _ in batch)
        rows = semantic.execute(
            f"""
            SELECT item.item_id, neural.neural_vector
            FROM items AS item
            JOIN neural_embeddings AS neural
              ON neural.content_hash = item.content_hash
            WHERE item.item_id IN ({placeholders})
            """,
            batch,
        ).fetchall()
        found += len(rows)
        if not rows:
            continue
        matrix = np.empty((len(rows), dimension), dtype=np.float32)
        row_ids: list[int] = []
        for row_index, (item_id, blob) in enumerate(rows):
            vector = np.frombuffer(blob, dtype=np.int8)
            if vector.size != dimension:
                raise AnnIndexError("semantic.db contiene un vector neuronal inválido")
            matrix[row_index] = vector.astype(np.float32) / 127.0
            row_ids.append(int(item_id))
        norms = np.linalg.norm(matrix, axis=1)
        valid = norms > 0.0
        scores = np.full(len(rows), -1.0, dtype=np.float32)
        scores[valid] = (matrix[valid] @ query_vector) / norms[valid]
        for item_id, score in zip(row_ids, scores.tolist()):
            bounded = max(-1.0, min(1.0, float(score)))
            if bounded >= min_score:
                hits.append(
                    AnnHit(
                        item_id=item_id,
                        score=round(bounded, 6),
                        collisions=int(collisions.get(item_id, 0)),
                    )
                )
    if found != len(candidate_ids):
        raise AnnIndexError("El ANN referencia vectores que ya no existen")
    hits.sort(key=lambda hit: (-hit.score, -hit.collisions, hit.item_id))
    return hits[:top_k]


def query_ann_index(
    ann_index_path: Path,
    semantic_index_path: Path,
    query_vector: Sequence[float],
    *,
    top_k: int = 20,
    allowed_ids: Sequence[int] | None = None,
    max_probe_radius: int = DEFAULT_MAX_PROBE_RADIUS,
    minimum_candidates: int | None = None,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    exact_filter_limit: int = DEFAULT_MAX_CANDIDATES,
    min_score: float = 0.0,
) -> AnnQueryResult:
    """Recupera candidatos globales y los ordena por coseno neuronal exacto.

    Para filtros estrechos, si LSH no reúne suficientes candidatos, la función
    compara directamente todos los IDs permitidos (hasta ``exact_filter_limit``).
    Así un filtro no sacrifica exhaustividad de manera silenciosa.
    """

    if top_k < 1:
        raise ValueError("top_k debe ser mayor que cero")
    if not 0 <= max_probe_radius <= 3:
        raise ValueError("max_probe_radius debe estar entre 0 y 3")
    if max_candidates < top_k:
        raise ValueError("max_candidates no puede ser menor que top_k")
    if exact_filter_limit < 0:
        raise ValueError("exact_filter_limit no puede ser negativo")
    if not -1.0 <= min_score <= 1.0:
        raise ValueError("min_score debe estar entre -1 y 1")
    target_candidates = (
        max(top_k * 20, 1_200)
        if minimum_candidates is None
        else int(minimum_candidates)
    )
    if target_candidates < top_k:
        raise ValueError("minimum_candidates no puede ser menor que top_k")

    state = ann_index_status(ann_index_path, semantic_index_path)
    if not state.get("available"):
        raise AnnIndexError(str(state.get("message", "Índice ANN no disponible")))
    dimension = int(state["neural_dimension"])
    table_count = int(state["table_count"])
    bits_per_table = int(state["bits_per_table"])
    normalized_query = _normalized_query_vector(query_vector, dimension)
    allowed = None if allowed_ids is None else {int(value) for value in allowed_ids}
    if allowed is not None and not allowed:
        return AnnQueryResult(
            hits=[],
            trace=AnnQueryTrace(
                engine="neural_ann_lsh",
                degraded=False,
                message="El filtro no contiene fragmentos consultables.",
                probe_radius=0,
                probes=0,
                candidates_retrieved=0,
                candidates_after_filter=0,
                candidates_scored=0,
                candidate_cap_applied=False,
                exact_filter_fallback=False,
            ),
        )

    try:
        with sqlite3.connect(
            f"file:{ann_index_path}?mode=ro",
            uri=True,
        ) as ann:
            plane_rows = ann.execute(
                "SELECT table_id, hyperplanes FROM ann_tables ORDER BY table_id"
            ).fetchall()
            if len(plane_rows) != table_count:
                raise AnnIndexError("Faltan hiperplanos en el índice ANN")
            np = _numpy()
            signatures: list[int] = []
            for expected_table, (table_id, blob) in enumerate(plane_rows):
                if int(table_id) != expected_table:
                    raise AnnIndexError("Los hiperplanos ANN están desordenados")
                hyperplanes = np.frombuffer(blob, dtype=np.int8)
                if hyperplanes.size != bits_per_table * dimension:
                    raise AnnIndexError("Un hiperplano ANN tiene tamaño inválido")
                signatures.append(
                    _query_signature(
                        normalized_query,
                        hyperplanes.reshape(bits_per_table, dimension),
                        bits_per_table,
                    )
                )
            (
                collisions,
                candidates_retrieved,
                candidates_after_filter,
                probes,
                reached_radius,
            ) = _load_candidates(
                ann,
                signatures,
                bits_per_table=bits_per_table,
                max_probe_radius=max_probe_radius,
                allowed=allowed,
                minimum_candidates=target_candidates,
            )

        exact_filter_fallback = False
        if (
            allowed is not None
            and len(collisions) < min(target_candidates, len(allowed))
            and len(allowed) <= exact_filter_limit
            and len(allowed) <= max_candidates
        ):
            exact_filter_fallback = True
            for item_id in allowed:
                collisions.setdefault(item_id, 0)

        ordered_candidates = [
            item_id
            for item_id, _ in sorted(
                collisions.items(),
                key=lambda item: (-item[1], item[0]),
            )
        ]
        candidate_cap_applied = len(ordered_candidates) > max_candidates
        if candidate_cap_applied:
            ordered_candidates = ordered_candidates[:max_candidates]
        with sqlite3.connect(
            f"file:{semantic_index_path}?mode=ro",
            uri=True,
        ) as semantic:
            hits = _score_candidates(
                semantic,
                ordered_candidates,
                normalized_query,
                dimension=dimension,
                top_k=top_k,
                min_score=min_score,
                collisions=collisions,
            )
        degraded = candidate_cap_applied
        message = "Recuperación neuronal global mediante ANN."
        if exact_filter_fallback:
            message = (
                "Recuperación neuronal exacta dentro del filtro después de la "
                "preselección ANN."
            )
        elif candidate_cap_applied:
            message = "El conjunto ANN alcanzó el límite seguro de candidatos."
        return AnnQueryResult(
            hits=hits,
            trace=AnnQueryTrace(
                engine=(
                    "neural_exact_filtered"
                    if exact_filter_fallback
                    else "neural_ann_lsh"
                ),
                degraded=degraded,
                message=message,
                probe_radius=reached_radius,
                probes=probes,
                candidates_retrieved=candidates_retrieved,
                candidates_after_filter=candidates_after_filter,
                candidates_scored=len(ordered_candidates),
                candidate_cap_applied=candidate_cap_applied,
                exact_filter_fallback=exact_filter_fallback,
            ),
        )
    except sqlite3.Error as exc:
        raise AnnIndexError("No fue posible consultar el índice ANN") from exc
