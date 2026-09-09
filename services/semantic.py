"""Recuperación semántica local, reproducible y con degradación segura.

Este módulo implementa dos señales complementarias:

* TF-IDF con *feature hashing*, útil para coincidencia léxica aproximada.
* *Random indexing* distribucional: cada término acumula una proyección
  determinística de los términos que aparecen a su alrededor. Dos términos
  usados en contextos parecidos pueden acabar próximos aunque no sean iguales.

La segunda señal es una forma ligera de semántica distribucional, pero no es un
modelo de lenguaje ni un embedding neuronal. Opcionalmente, el mismo archivo
puede guardar embeddings densos multilingües creados por un modelo ONNX local
mediante FastEmbed y un registro versionado. Esta tercera señal se usa
para reordenar candidatos y nunca sustituye el respaldo local: si el paquete o
el modelo neuronal no están disponibles, la consulta continúa con las señales
TF-IDF/distribucionales ya persistidas.

El formato persistente es SQLite con vectores unitarios cuantizados a int8. La
cuantización reduce aproximadamente cuatro veces el tamaño frente a float32 y
es adecuada para reranking, aunque introduce una pequeña pérdida de precisión.
No se usa ``pickle`` y, por tanto, abrir un índice no ejecuta código serializado.
El respaldo determinístico depende solo de la biblioteca estándar; FastEmbed es
la dependencia local de la capa neuronal.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import re
import sqlite3
import struct
import tempfile
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Protocol, Sequence

from services.text_utils import SPANISH_STOPWORDS, normalize_text


SEMANTIC_INDEX_FORMAT_VERSION = 2
SUPPORTED_SEMANTIC_INDEX_FORMATS = frozenset({1, 2})
SEMANTIC_METHOD = "hashed_tfidf+distributional_random_indexing"
SEMANTIC_BUILD_SIGNATURE = (
    "tokenizer-v1-random-indexing-v1-int8-source-fingerprint-v2-content-hash-v1"
)
NEURAL_SEMANTIC_METHOD = f"{SEMANTIC_METHOD}+multilingual_fastembed"
DEFAULT_NEURAL_MODEL_ID = (
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
)
# FastEmbed fija el artefacto ONNX en su registro versionado. Guardamos esa
# versión junto al índice para no mezclar vectores de registros distintos.
DEFAULT_NEURAL_MODEL_REVISION = "fastembed-0.8.0-registry"
NEURAL_VECTOR_ENCODING = "signed_int8_unit_vector"
NEURAL_INPUT_SIGNATURE = "exact-utf8-v1|fastembed-0.8.0-mean-pooling"
_TOKEN_PATTERN = re.compile(r"\b[\w-]+\b", flags=re.UNICODE)
_ITEM_BATCH_SIZE = 800


class SemanticIndexError(RuntimeError):
    """Indica que el índice no existe, está dañado o es incompatible."""


class NeuralSemanticUnavailable(RuntimeError):
    """El complemento neuronal no puede cargarse; el índice local sigue útil."""


class DenseTextEncoder(Protocol):
    """Contrato mínimo para inyectar un codificador local reproducible."""

    model_id: str
    model_revision: str

    def encode_passages(
        self,
        texts: Sequence[str],
        *,
        batch_size: int,
    ) -> list[list[float]]:
        """Devuelve un vector denso normalizado por cada fragmento."""

    def encode_query(self, text: str) -> list[float]:
        """Devuelve el vector normalizado de una consulta."""


@dataclass(frozen=True)
class SemanticDocument:
    """Unidad mínima que se incorporará al índice local."""

    item_id: int
    text: str


@dataclass(frozen=True)
class SemanticBuildSummary:
    documents_indexed: int
    documents_skipped_empty: int
    vocabulary_size: int
    semantic_vocabulary_size: int
    lexical_dimension: int
    semantic_dimension: int
    index_size_bytes: int
    source_fingerprint: str | None = None
    neural_status: str = "disabled"
    neural_documents_indexed: int = 0
    neural_dimension: int | None = None
    neural_model_id: str | None = None
    neural_model_revision: str | None = None
    neural_error: str | None = None
    neural_unique_texts: int = 0
    neural_documents_reused: int = 0
    neural_documents_encoded: int = 0
    neural_documents_remaining: int = 0
    checkpoint_reused: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ResumableSemanticBuildSummary:
    """Avance durable de una construcción neuronal por segmentos."""

    complete: bool
    checkpoint_reused: bool
    documents_total: int
    documents_before: int
    documents_after: int
    documents_encoded: int
    documents_reused: int
    documents_remaining: int
    unique_texts_total: int
    unique_texts_before: int
    unique_texts_after: int
    unique_texts_encoded: int
    neural_dimension: int | None
    source_fingerprint: str
    method: str
    build_signature: str
    index_size_bytes: int

    @property
    def progress_made(self) -> bool:
        return self.documents_after > self.documents_before

    def as_dict(self) -> dict:
        return {**asdict(self), "progress_made": self.progress_made}


@dataclass(frozen=True)
class SemanticHit:
    item_id: int
    score: float
    lexical_score: float
    distributional_score: float
    neural_score: float = 0.0

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class HybridScore:
    item_id: int
    score: float
    lexical_score: float
    semantic_score: float

    def as_dict(self) -> dict:
        return asdict(self)


def _tokenize(text: str) -> list[str]:
    tokens = _TOKEN_PATTERN.findall(normalize_text(text))
    return [
        token
        for token in tokens
        if token not in SPANISH_STOPWORDS and (len(token) > 2 or token.isdigit())
    ]


def _validate_dimensions(
    *,
    lexical_dimension: int,
    semantic_dimension: int,
    context_window: int,
    min_df: int,
    max_vocabulary: int,
    max_context_occurrences: int,
) -> None:
    if lexical_dimension < 32:
        raise ValueError("lexical_dimension debe ser al menos 32")
    if semantic_dimension < 16:
        raise ValueError("semantic_dimension debe ser al menos 16")
    if context_window < 1:
        raise ValueError("context_window debe ser mayor que cero")
    if min_df < 1:
        raise ValueError("min_df debe ser mayor que cero")
    if max_vocabulary < 1:
        raise ValueError("max_vocabulary debe ser mayor que cero")
    if max_context_occurrences < 1:
        raise ValueError("max_context_occurrences debe ser mayor que cero")


def _hash_bytes(value: str, *, person: bytes) -> bytes:
    return hashlib.blake2b(
        value.encode("utf-8"), digest_size=32, person=person
    ).digest()


def _hashed_position(term: str, dimension: int) -> tuple[int, float]:
    digest = _hash_bytes(term, person=b"invima-tfidf")
    position = int.from_bytes(digest[:8], "little") % dimension
    sign = 1.0 if digest[8] & 1 else -1.0
    return position, sign


def _random_projection(
    term: str,
    dimension: int,
    non_zero: int = 4,
) -> tuple[tuple[int, float], ...]:
    """Crea un vector índice disperso y determinístico para ``term``."""
    target = min(non_zero, dimension)
    features: list[tuple[int, float]] = []
    used: set[int] = set()
    counter = 0
    while len(features) < target:
        digest = _hash_bytes(f"{term}\x1f{counter}", person=b"invima-rindex")
        for offset in range(0, len(digest) - 2, 3):
            position = int.from_bytes(digest[offset : offset + 2], "little") % dimension
            if position in used:
                continue
            used.add(position)
            sign = 1.0 if digest[offset + 2] & 1 else -1.0
            features.append((position, sign))
            if len(features) == target:
                break
        counter += 1
    return tuple(features)


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0.0:
        return vector
    return [value / norm for value in vector]


def _validated_dense_vector(vector: Sequence[float]) -> list[float]:
    values = [float(value) for value in vector]
    if not values or any(not math.isfinite(value) for value in values):
        raise NeuralSemanticUnavailable(
            "El modelo neuronal devolvió un vector vacío o no finito"
        )
    normalized = _normalize(values)
    if not any(normalized):
        raise NeuralSemanticUnavailable(
            "El modelo neuronal devolvió un vector sin información"
        )
    return normalized


def semantic_content_hash(text: str) -> bytes:
    """Identifica exactamente el texto que recibe el codificador neuronal."""

    digest = hashlib.sha256()
    digest.update(b"invima-neural-input-v1\0")
    digest.update(text.encode("utf-8"))
    return digest.digest()


def _pack_vector(vector: Sequence[float]) -> bytes:
    if not vector:
        return b""
    quantized = [
        max(-127, min(127, int(round(value * 127.0)))) for value in vector
    ]
    return struct.pack(f"<{len(vector)}b", *quantized)


def _unpack_vector(blob: bytes, expected_dimension: int) -> tuple[float, ...]:
    expected_bytes = expected_dimension
    if len(blob) != expected_bytes:
        raise SemanticIndexError(
            "El índice contiene un vector con una dimensión incompatible"
        )
    return tuple(
        value / 127.0
        for value in struct.unpack(f"<{expected_dimension}b", blob)
    )


def _dot(left: Sequence[float], right: Sequence[float]) -> float:
    return sum(first * second for first, second in zip(left, right))


class FastEmbedDenseEncoder:
    """Adaptador ONNX diferido; no carga el modelo al iniciar Streamlit."""

    def __init__(self, model_id: str, model_revision: str) -> None:
        model_id = model_id.strip()
        model_revision = model_revision.strip()
        if not model_id:
            raise ValueError("El identificador del modelo neuronal es obligatorio")
        if model_revision != DEFAULT_NEURAL_MODEL_REVISION:
            raise ValueError(
                "La revisión neuronal debe coincidir con el registro FastEmbed "
                "fijado por la aplicación"
            )
        try:
            from fastembed import TextEmbedding
        except (ImportError, ModuleNotFoundError) as exc:
            raise NeuralSemanticUnavailable(
                "Falta la dependencia opcional fastembed; "
                "se usará la semántica local."
            ) from exc
        try:
            cache_dir = os.getenv("ACTAS_SEMANTIC_MODEL_CACHE", "").strip()
            self._model = TextEmbedding(
                model_name=model_id,
                cache_dir=cache_dir or None,
                threads=max(1, min(4, os.cpu_count() or 1)),
            )
        except Exception as exc:
            raise NeuralSemanticUnavailable(
                "No fue posible cargar el modelo neuronal fijado; "
                "se usará la semántica local."
            ) from exc
        self.model_id = model_id
        self.model_revision = model_revision

    @staticmethod
    def _normalized(values: Sequence[float]) -> list[float]:
        return _normalize([float(value) for value in values])

    def encode_passages(
        self,
        texts: Sequence[str],
        *,
        batch_size: int,
    ) -> list[list[float]]:
        if not texts:
            return []
        try:
            encoded = self._model.passage_embed(
                texts,
                batch_size=batch_size,
            )
        except Exception as exc:
            raise NeuralSemanticUnavailable(
                "El modelo neuronal no pudo codificar los textos"
            ) from exc
        return [self._normalized(row) for row in encoded]

    def encode_query(self, text: str) -> list[float]:
        try:
            rows = list(self._model.query_embed(text, batch_size=1))
        except Exception as exc:
            raise NeuralSemanticUnavailable(
                "El modelo neuronal no pudo codificar la consulta"
            ) from exc
        if len(rows) != 1:
            raise NeuralSemanticUnavailable(
                "El modelo neuronal devolvió una consulta incompatible"
            )
        return self._normalized(rows[0])


@lru_cache(maxsize=2)
def _fastembed_encoder(
    model_id: str,
    model_revision: str,
) -> FastEmbedDenseEncoder:
    """Conserva una sola copia del modelo por proceso de Streamlit."""

    return FastEmbedDenseEncoder(model_id, model_revision)


def neural_runtime_installed() -> bool:
    """Indica si el complemento está instalado, sin cargar el modelo."""

    return importlib.util.find_spec("fastembed") is not None


def semantic_build_spec(
    *,
    neural_enabled: bool = False,
    neural_model_id: str = DEFAULT_NEURAL_MODEL_ID,
    neural_model_revision: str = DEFAULT_NEURAL_MODEL_REVISION,
) -> tuple[str, str]:
    """Devuelve método y firma esperados para decidir si se puede reutilizar."""

    if not neural_enabled:
        return SEMANTIC_METHOD, SEMANTIC_BUILD_SIGNATURE
    model_id = neural_model_id.strip()
    revision = neural_model_revision.strip()
    if not model_id or revision != DEFAULT_NEURAL_MODEL_REVISION:
        raise ValueError(
            "La construcción neuronal requiere el registro FastEmbed fijado"
        )
    signature = (
        f"{SEMANTIC_BUILD_SIGNATURE}|dense-int8-v1|"
        f"{NEURAL_INPUT_SIGNATURE}|{model_id}@{revision}"
    )
    return NEURAL_SEMANTIC_METHOD, signature


def _idf(document_count: int, document_frequency: int) -> float:
    return math.log((document_count + 1) / (document_frequency + 1)) + 1.0


def _lexical_vector(
    counts: Mapping[str, int],
    idf_by_term: Mapping[str, float],
    dimension: int,
    default_idf: float,
) -> list[float]:
    vector = [0.0] * dimension
    for term, frequency in counts.items():
        position, sign = _hashed_position(term, dimension)
        weight = (1.0 + math.log(frequency)) * idf_by_term.get(term, default_idf)
        vector[position] += sign * weight
    return _normalize(vector)


def _distributional_vector(
    counts: Mapping[str, int],
    idf_by_term: Mapping[str, float],
    context_vectors: Mapping[str, Sequence[float]],
    dimension: int,
) -> list[float]:
    vector = [0.0] * dimension
    for term, frequency in counts.items():
        term_vector = context_vectors.get(term)
        if term_vector is None:
            continue
        weight = (1.0 + math.log(frequency)) * idf_by_term[term]
        for index, value in enumerate(term_vector):
            vector[index] += value * weight
    return _normalize(vector)


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode = DELETE;
        PRAGMA synchronous = NORMAL;

        CREATE TABLE metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE terms (
            term TEXT PRIMARY KEY,
            document_frequency INTEGER NOT NULL,
            inverse_document_frequency REAL NOT NULL,
            distributional_vector BLOB
        );

        CREATE TABLE items (
            item_id INTEGER PRIMARY KEY,
            content_hash BLOB NOT NULL,
            lexical_vector BLOB NOT NULL,
            distributional_vector BLOB NOT NULL
        );

        CREATE INDEX idx_items_content_hash ON items(content_hash);

        CREATE TABLE neural_embeddings (
            content_hash BLOB PRIMARY KEY,
            neural_vector BLOB NOT NULL
        );
        """
    )


def _write_metadata(connection: sqlite3.Connection, values: Mapping[str, object]) -> None:
    connection.executemany(
        "INSERT INTO metadata (key, value) VALUES (?, ?)",
        ((key, json.dumps(value, ensure_ascii=False)) for key, value in values.items()),
    )


def _upsert_metadata(
    connection: sqlite3.Connection,
    values: Mapping[str, object],
) -> None:
    connection.executemany(
        """
        INSERT INTO metadata (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        ((key, json.dumps(value, ensure_ascii=False)) for key, value in values.items()),
    )


def _read_metadata(connection: sqlite3.Connection) -> dict[str, object]:
    try:
        rows = connection.execute("SELECT key, value FROM metadata").fetchall()
    except sqlite3.Error as exc:
        raise SemanticIndexError("El archivo no es un índice semántico válido") from exc
    result: dict[str, object] = {}
    for key, value in rows:
        try:
            result[str(key)] = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise SemanticIndexError("Los metadatos del índice están dañados") from exc
    if result.get("format_version") not in SUPPORTED_SEMANTIC_INDEX_FORMATS:
        raise SemanticIndexError("La versión del índice semántico no es compatible")
    required = {
        "documents",
        "lexical_dimension",
        "semantic_dimension",
        "method",
        "build_signature",
    }
    if not required.issubset(result):
        raise SemanticIndexError("Los metadatos del índice están incompletos")
    return result


def _spooled_documents(path: Path) -> Iterator[tuple[int, list[str], str]]:
    with path.open("r", encoding="utf-8") as spool:
        for line in spool:
            item_id, tokens, text = json.loads(line)
            yield int(item_id), list(tokens), str(text)


def build_semantic_documents_index(
    documents: Iterable[SemanticDocument],
    index_path: Path,
    *,
    lexical_dimension: int = 256,
    semantic_dimension: int = 64,
    context_window: int = 3,
    min_df: int = 2,
    max_vocabulary: int = 15_000,
    max_context_occurrences: int = 128,
    source_fingerprint: str | None = None,
    neural_enabled: bool = False,
    neural_model_id: str = DEFAULT_NEURAL_MODEL_ID,
    neural_model_revision: str = DEFAULT_NEURAL_MODEL_REVISION,
    neural_batch_size: int = 32,
    neural_encoder: DenseTextEncoder | None = None,
) -> SemanticBuildSummary:
    """Construye atómicamente un índice local persistente.

    ``item_id`` debe corresponder al identificador estable del fragmento en la
    base principal. Los textos se escriben temporalmente junto al destino para
    poder recorrer corpus grandes sin retenerlos completos en memoria.
    """
    _validate_dimensions(
        lexical_dimension=lexical_dimension,
        semantic_dimension=semantic_dimension,
        context_window=context_window,
        min_df=min_df,
        max_vocabulary=max_vocabulary,
        max_context_occurrences=max_context_occurrences,
    )
    if neural_batch_size < 1:
        raise ValueError("neural_batch_size debe ser mayor que cero")
    # Valida la especificación antes de crear temporales, incluso cuando se
    # inyecta un codificador de prueba.
    semantic_build_spec(
        neural_enabled=neural_enabled,
        neural_model_id=neural_model_id,
        neural_model_revision=neural_model_revision,
    )
    index_path = Path(index_path)
    index_path.parent.mkdir(parents=True, exist_ok=True)

    spool_handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=f".{index_path.name}.documents-",
        suffix=".jsonl",
        dir=index_path.parent,
        delete=False,
    )
    spool_path = Path(spool_handle.name)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{index_path.name}.building-",
        suffix=".sqlite",
        dir=index_path.parent,
    )
    os.close(file_descriptor)
    temporary_path = Path(temporary_name)
    temporary_path.unlink(missing_ok=True)

    frequencies: Counter[str] = Counter()
    seen_ids: set[int] = set()
    documents_indexed = 0
    documents_skipped_empty = 0

    try:
        with spool_handle:
            for document in documents:
                item_id = int(document.item_id)
                if item_id in seen_ids:
                    raise ValueError(f"item_id duplicado: {item_id}")
                seen_ids.add(item_id)
                tokens = _tokenize(document.text)
                if not tokens:
                    documents_skipped_empty += 1
                    continue
                frequencies.update(set(tokens))
                spool_handle.write(
                    json.dumps(
                        [item_id, tokens, document.text],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                documents_indexed += 1

        selected_terms = [
            term
            for term, frequency in sorted(
                frequencies.items(), key=lambda item: (-item[1], item[0])
            )
            if frequency >= min_df
        ][:max_vocabulary]
        selected = set(selected_terms)
        idf_by_term = {
            term: _idf(documents_indexed, frequency)
            for term, frequency in frequencies.items()
        }

        context_vectors: dict[str, list[float]] = {
            term: [0.0] * semantic_dimension for term in selected_terms
        }
        projection_cache: dict[str, tuple[tuple[int, float], ...]] = {}
        trained_occurrences: Counter[str] = Counter()
        for _, tokens, _ in _spooled_documents(spool_path):
            for target_index, target in enumerate(tokens):
                if target not in selected:
                    continue
                if trained_occurrences[target] >= max_context_occurrences:
                    continue
                trained_occurrences[target] += 1
                target_vector = context_vectors[target]
                start = max(0, target_index - context_window)
                end = min(len(tokens), target_index + context_window + 1)
                for neighbor_index in range(start, end):
                    if neighbor_index == target_index:
                        continue
                    neighbor = tokens[neighbor_index]
                    projection = projection_cache.get(neighbor)
                    if projection is None:
                        projection = _random_projection(neighbor, semantic_dimension)
                        if neighbor in selected or len(projection_cache) < 50_000:
                            projection_cache[neighbor] = projection
                    distance_weight = 1.0 / abs(target_index - neighbor_index)
                    for position, sign in projection:
                        target_vector[position] += sign * distance_weight

        for term in selected_terms:
            context_vectors[term] = _normalize(context_vectors[term])

        requested_encoder = neural_encoder
        neural_status = "disabled"
        neural_error: str | None = None
        neural_documents_indexed = 0
        neural_dimension: int | None = None
        if neural_enabled and requested_encoder is None:
            try:
                requested_encoder = _fastembed_encoder(
                    neural_model_id,
                    neural_model_revision,
                )
            except Exception as exc:
                neural_status = "fallback"
                neural_error = str(exc)
        elif neural_enabled:
            if (
                requested_encoder.model_id != neural_model_id
                or requested_encoder.model_revision != neural_model_revision
            ):
                raise ValueError(
                    "El codificador inyectado no coincide con el modelo fijado"
                )
            neural_status = "pending"

        with sqlite3.connect(temporary_path) as connection:
            _create_schema(connection)
            connection.executemany(
                """
                INSERT INTO terms (
                    term, document_frequency, inverse_document_frequency,
                    distributional_vector
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    (
                        term,
                        frequency,
                        idf_by_term[term],
                        _pack_vector(context_vectors[term])
                        if term in selected
                        else None,
                    )
                    for term, frequency in frequencies.items()
                ),
            )
            default_idf = _idf(documents_indexed, 0)
            for item_id, tokens, text in _spooled_documents(spool_path):
                counts = Counter(tokens)
                lexical = _lexical_vector(
                    counts, idf_by_term, lexical_dimension, default_idf
                )
                distributional = _distributional_vector(
                    counts,
                    idf_by_term,
                    context_vectors,
                    semantic_dimension,
                )
                connection.execute(
                    """
                    INSERT INTO items (
                        item_id, content_hash, lexical_vector,
                        distributional_vector
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        item_id,
                        semantic_content_hash(text),
                        _pack_vector(lexical),
                        _pack_vector(distributional),
                    ),
                )

            if neural_enabled and requested_encoder is not None:
                try:
                    batch_hashes: list[bytes] = []
                    batch_texts: list[str] = []
                    seen_hashes: set[bytes] = set()

                    def flush_neural_batch() -> None:
                        nonlocal neural_dimension, neural_documents_indexed
                        if not batch_hashes:
                            return
                        vectors = requested_encoder.encode_passages(
                            batch_texts,
                            batch_size=neural_batch_size,
                        )
                        if len(vectors) != len(batch_hashes):
                            raise NeuralSemanticUnavailable(
                                "El modelo neuronal no devolvió un vector por texto"
                            )
                        rows: list[tuple[bytes, bytes]] = []
                        for content_hash, vector in zip(batch_hashes, vectors):
                            normalized = _validated_dense_vector(vector)
                            if neural_dimension is None:
                                neural_dimension = len(normalized)
                            elif len(normalized) != neural_dimension:
                                raise NeuralSemanticUnavailable(
                                    "El modelo neuronal cambió de dimensión"
                                )
                            rows.append((content_hash, _pack_vector(normalized)))
                        connection.executemany(
                            """
                            INSERT INTO neural_embeddings (
                                content_hash, neural_vector
                            ) VALUES (?, ?)
                            ON CONFLICT(content_hash) DO UPDATE SET
                                neural_vector = excluded.neural_vector
                            """,
                            rows,
                        )
                        placeholders = ",".join("?" for _ in batch_hashes)
                        neural_documents_indexed += int(
                            connection.execute(
                                "SELECT COUNT(*) FROM items "
                                f"WHERE content_hash IN ({placeholders})",
                                batch_hashes,
                            ).fetchone()[0]
                        )
                        progress_interval = max(neural_batch_size * 50, 5_000)
                        if (
                            neural_documents_indexed == documents_indexed
                            or neural_documents_indexed % progress_interval < len(rows)
                        ):
                            print(
                                "Embeddings neuronales: "
                                f"{neural_documents_indexed}/{documents_indexed}",
                                flush=True,
                            )
                        batch_hashes.clear()
                        batch_texts.clear()

                    for _, _, text in _spooled_documents(spool_path):
                        content_hash = semantic_content_hash(text)
                        if content_hash in seen_hashes:
                            continue
                        seen_hashes.add(content_hash)
                        batch_hashes.append(content_hash)
                        batch_texts.append(text)
                        if len(batch_hashes) >= neural_batch_size:
                            flush_neural_batch()
                    flush_neural_batch()
                    if neural_documents_indexed != documents_indexed:
                        raise NeuralSemanticUnavailable(
                            "La cobertura neuronal no coincide con el corpus"
                        )
                    neural_status = "ready"
                except Exception as exc:
                    # Una descarga, dependencia o inferencia neuronal nunca
                    # invalida las señales locales ya construidas.
                    connection.execute("DELETE FROM neural_embeddings")
                    neural_status = "fallback"
                    neural_error = str(exc)
                    neural_documents_indexed = 0
                    neural_dimension = None

            effective_method, effective_signature = semantic_build_spec(
                neural_enabled=neural_status == "ready",
                neural_model_id=neural_model_id,
                neural_model_revision=neural_model_revision,
            )
            _write_metadata(
                connection,
                {
                    "format_version": SEMANTIC_INDEX_FORMAT_VERSION,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "documents": documents_indexed,
                    "vocabulary_size": len(frequencies),
                    "semantic_vocabulary_size": len(selected_terms),
                    "lexical_dimension": lexical_dimension,
                    "semantic_dimension": semantic_dimension,
                    "context_window": context_window,
                    "min_df": min_df,
                    "max_vocabulary": max_vocabulary,
                    "max_context_occurrences": max_context_occurrences,
                    "vector_encoding": "signed_int8_unit_vector",
                    "method": effective_method,
                    "build_signature": effective_signature,
                    "source_fingerprint": source_fingerprint,
                    "neural_status": neural_status,
                    "neural_documents": neural_documents_indexed,
                    "neural_unique_texts": int(
                        connection.execute(
                            "SELECT COUNT(*) FROM neural_embeddings"
                        ).fetchone()[0]
                    ),
                    "neural_dimension": neural_dimension,
                    "neural_model_id": (
                        neural_model_id if neural_enabled else None
                    ),
                    "neural_model_revision": (
                        neural_model_revision if neural_enabled else None
                    ),
                    "neural_vector_encoding": (
                        NEURAL_VECTOR_ENCODING if neural_status == "ready" else None
                    ),
                    "neural_error": neural_error,
                },
            )
            connection.commit()
            connection.execute("PRAGMA optimize")

        os.replace(temporary_path, index_path)
        return SemanticBuildSummary(
            documents_indexed=documents_indexed,
            documents_skipped_empty=documents_skipped_empty,
            vocabulary_size=len(frequencies),
            semantic_vocabulary_size=len(selected_terms),
            lexical_dimension=lexical_dimension,
            semantic_dimension=semantic_dimension,
            index_size_bytes=index_path.stat().st_size,
            source_fingerprint=source_fingerprint,
            neural_status=neural_status,
            neural_documents_indexed=neural_documents_indexed,
            neural_dimension=neural_dimension,
            neural_model_id=neural_model_id if neural_enabled else None,
            neural_model_revision=(
                neural_model_revision if neural_enabled else None
            ),
            neural_error=neural_error,
            neural_unique_texts=(
                len(seen_hashes)
                if neural_enabled and requested_encoder is not None
                and neural_status == "ready"
                else 0
            ),
            neural_documents_encoded=neural_documents_indexed,
            neural_documents_remaining=(
                max(0, documents_indexed - neural_documents_indexed)
                if neural_enabled
                else 0
            ),
        )
    finally:
        spool_path.unlink(missing_ok=True)
        temporary_path.unlink(missing_ok=True)


def build_semantic_index(
    database_path: Path,
    index_path: Path,
    *,
    lexical_dimension: int = 256,
    semantic_dimension: int = 64,
    context_window: int = 3,
    min_df: int = 2,
    max_vocabulary: int = 15_000,
    max_context_occurrences: int = 128,
    neural_enabled: bool = False,
    neural_model_id: str = DEFAULT_NEURAL_MODEL_ID,
    neural_model_revision: str = DEFAULT_NEURAL_MODEL_REVISION,
    neural_batch_size: int = 32,
    neural_encoder: DenseTextEncoder | None = None,
) -> SemanticBuildSummary:
    """Construye el índice a partir de ``chunks(id, text)`` de ``actas.db``.

    Esta es la entrada destinada al workflow de GitHub Actions. La lectura se
    hace en modo estricto (solo lectura) para no modificar accidentalmente la
    base principal mientras se genera el artefacto semántico.
    """
    database_path = Path(database_path)
    if not database_path.exists():
        raise SemanticIndexError("No existe la base de fragmentos")
    if database_path.resolve() == Path(index_path).resolve():
        raise ValueError("index_path debe ser distinto de database_path")
    source_fingerprint = semantic_source_fingerprint(database_path)
    try:
        with sqlite3.connect(
            f"file:{database_path}?mode=ro", uri=True
        ) as connection:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunks'"
            ).fetchone()
            if table is None:
                raise SemanticIndexError(
                    "La base principal no contiene la tabla chunks"
                )
            documents = (
                SemanticDocument(item_id=int(item_id), text=str(text))
                for item_id, text in connection.execute(
                    "SELECT id, text FROM chunks ORDER BY id"
                )
            )
            return build_semantic_documents_index(
                documents,
                index_path,
                lexical_dimension=lexical_dimension,
                semantic_dimension=semantic_dimension,
                context_window=context_window,
                min_df=min_df,
                max_vocabulary=max_vocabulary,
                max_context_occurrences=max_context_occurrences,
                source_fingerprint=source_fingerprint,
                neural_enabled=neural_enabled,
                neural_model_id=neural_model_id,
                neural_model_revision=neural_model_revision,
                neural_batch_size=neural_batch_size,
                neural_encoder=neural_encoder,
            )
    except SemanticIndexError:
        raise
    except sqlite3.Error as exc:
        raise SemanticIndexError(
            "No fue posible leer los fragmentos de la base principal"
        ) from exc


def _neural_coverage(
    connection: sqlite3.Connection,
    neural_dimension: int | None,
) -> tuple[int, int, int]:
    """Devuelve elementos cubiertos, textos únicos cubiertos y textos totales."""

    unique_total = int(
        connection.execute(
            "SELECT COUNT(DISTINCT content_hash) FROM items"
        ).fetchone()[0]
    )
    if not neural_dimension:
        return 0, 0, unique_total
    unique_covered = int(
        connection.execute(
            """
            SELECT COUNT(DISTINCT neural.content_hash)
            FROM neural_embeddings AS neural
            JOIN items AS item ON item.content_hash = neural.content_hash
            WHERE length(neural.neural_vector) = ?
            """,
            (neural_dimension,),
        ).fetchone()[0]
    )
    documents_covered = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM items AS item
            JOIN neural_embeddings AS neural
              ON neural.content_hash = item.content_hash
            WHERE length(neural.neural_vector) = ?
            """,
            (neural_dimension,),
        ).fetchone()[0]
    )
    return documents_covered, unique_covered, unique_total


def _validate_items_against_source(
    semantic_index_path: Path,
    database_path: Path,
) -> None:
    """Comprueba que cada elemento semántico representa el texto fuente real."""

    try:
        with sqlite3.connect(
            f"file:{semantic_index_path}?mode=ro", uri=True
        ) as semantic, sqlite3.connect(
            f"file:{database_path}?mode=ro", uri=True
        ) as source:
            indexed_rows = iter(
                semantic.execute(
                    "SELECT item_id, content_hash FROM items ORDER BY item_id"
                )
            )
            indexed = next(indexed_rows, None)
            for item_id, text in source.execute(
                "SELECT id, text FROM chunks ORDER BY id"
            ):
                value = str(text)
                if not _tokenize(value):
                    continue
                if indexed is None:
                    raise SemanticIndexError(
                        "El índice semántico omite fragmentos de la base fuente"
                    )
                indexed_id, content_hash = indexed
                if int(indexed_id) != int(item_id) or bytes(
                    content_hash
                ) != semantic_content_hash(value):
                    raise SemanticIndexError(
                        "El índice semántico no coincide con los fragmentos fuente"
                    )
                indexed = next(indexed_rows, None)
            if indexed is not None:
                raise SemanticIndexError(
                    "El índice semántico contiene fragmentos ajenos a la base fuente"
                )
    except SemanticIndexError:
        raise
    except sqlite3.Error as exc:
        raise SemanticIndexError(
            "No fue posible validar el índice contra la base fuente"
        ) from exc


def _resumable_checkpoint_is_compatible(
    checkpoint_path: Path,
    *,
    database_path: Path,
    source_fingerprint: str,
    lexical_dimension: int,
    semantic_dimension: int,
    method: str,
    build_signature: str,
    neural_model_id: str,
    neural_model_revision: str,
) -> bool:
    if not checkpoint_path.exists():
        return False
    try:
        with sqlite3.connect(
            f"file:{checkpoint_path}?mode=ro", uri=True
        ) as connection:
            metadata = _read_metadata(connection)
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            item_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(items)")
            }
            if not {"items", "neural_embeddings"}.issubset(tables):
                return False
            if "content_hash" not in item_columns:
                return False
            if str(connection.execute("PRAGMA quick_check").fetchone()[0]).lower() != "ok":
                return False
            if any(
                (
                    int(metadata.get("format_version", 0)) != 2,
                    metadata.get("source_fingerprint") != source_fingerprint,
                    int(metadata.get("lexical_dimension", -1))
                    != lexical_dimension,
                    int(metadata.get("semantic_dimension", -1))
                    != semantic_dimension,
                    metadata.get("method") != method,
                    metadata.get("build_signature") != build_signature,
                    metadata.get("neural_model_id") != neural_model_id,
                    metadata.get("neural_model_revision")
                    != neural_model_revision,
                    metadata.get("neural_vector_encoding")
                    != NEURAL_VECTOR_ENCODING,
                    metadata.get("neural_status") not in {"building", "ready"},
                )
            ):
                return False
            document_count = int(
                connection.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            )
            if document_count != int(metadata.get("documents", -1)):
                return False
            if int(
                connection.execute(
                    "SELECT COUNT(*) FROM items WHERE length(content_hash) != 32"
                ).fetchone()[0]
            ):
                return False
            neural_dimension = int(metadata.get("neural_dimension") or 0)
            if neural_dimension:
                invalid = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM neural_embeddings "
                        "WHERE length(neural_vector) != ?",
                        (neural_dimension,),
                    ).fetchone()[0]
                )
                if invalid:
                    return False
            elif int(
                connection.execute(
                    "SELECT COUNT(*) FROM neural_embeddings"
                ).fetchone()[0]
            ):
                return False
    except (OSError, TypeError, ValueError, sqlite3.Error, SemanticIndexError):
        return False
    try:
        _validate_items_against_source(checkpoint_path, database_path)
    except (OSError, SemanticIndexError):
        return False
    return True


def _reuse_neural_embeddings(
    target: sqlite3.Connection,
    source_path: Path,
    *,
    method: str,
    build_signature: str,
    neural_model_id: str,
    neural_model_revision: str,
    expected_dimension: int | None,
) -> int | None:
    """Copia vectores compatibles por hash, sin confiar en ``item_id``."""

    source_path = Path(source_path)
    if not source_path.exists():
        return expected_dimension
    try:
        with sqlite3.connect(
            f"file:{source_path}?mode=ro", uri=True
        ) as source:
            metadata = _read_metadata(source)
            tables = {
                str(row[0])
                for row in source.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if int(metadata.get("format_version", 0)) < 2:
                return expected_dimension
            if "neural_embeddings" not in tables:
                return expected_dimension
            if any(
                (
                    metadata.get("method") != method,
                    metadata.get("build_signature") != build_signature,
                    metadata.get("neural_model_id") != neural_model_id,
                    metadata.get("neural_model_revision")
                    != neural_model_revision,
                    metadata.get("neural_vector_encoding")
                    != NEURAL_VECTOR_ENCODING,
                )
            ):
                return expected_dimension
            source_dimension = int(metadata.get("neural_dimension") or 0)
            if source_dimension < 1:
                return expected_dimension
            if expected_dimension not in {None, source_dimension}:
                return expected_dimension

        alias = "reusable_neural"
        target.execute(f"ATTACH DATABASE ? AS {alias}", (str(source_path),))
        changes_before = target.total_changes
        try:
            target.execute(
                f"""
                INSERT OR IGNORE INTO neural_embeddings (
                    content_hash, neural_vector
                )
                SELECT reusable.content_hash, reusable.neural_vector
                FROM {alias}.neural_embeddings AS reusable
                WHERE length(reusable.content_hash) = 32
                  AND length(reusable.neural_vector) = ?
                  AND EXISTS (
                      SELECT 1 FROM main.items AS item
                      WHERE item.content_hash = reusable.content_hash
                  )
                """,
                (source_dimension,),
            )
            target.commit()
        except Exception:
            target.rollback()
            raise
        finally:
            target.execute(f"DETACH DATABASE {alias}")
        if target.total_changes > changes_before or expected_dimension is not None:
            return source_dimension
        return expected_dimension
    except (OSError, TypeError, ValueError, sqlite3.Error, SemanticIndexError):
        # Una caché vieja o dañada nunca invalida la construcción nueva.
        return expected_dimension


def _prepare_resumable_semantic_checkpoint(
    database_path: Path,
    published_index_path: Path,
    checkpoint_path: Path,
    *,
    lexical_dimension: int,
    semantic_dimension: int,
    neural_model_id: str,
    neural_model_revision: str,
) -> tuple[bool, str, str, str]:
    source_fingerprint = semantic_source_fingerprint(database_path)
    method, build_signature = semantic_build_spec(
        neural_enabled=True,
        neural_model_id=neural_model_id,
        neural_model_revision=neural_model_revision,
    )
    if _resumable_checkpoint_is_compatible(
        checkpoint_path,
        database_path=database_path,
        source_fingerprint=source_fingerprint,
        lexical_dimension=lexical_dimension,
        semantic_dimension=semantic_dimension,
        method=method,
        build_signature=build_signature,
        neural_model_id=neural_model_id,
        neural_model_revision=neural_model_revision,
    ):
        return True, source_fingerprint, method, build_signature

    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = checkpoint_path.with_name(
        f".{checkpoint_path.name}.preparing-{os.getpid()}"
    )
    temporary_path.unlink(missing_ok=True)
    reuse_paths = tuple(
        path
        for path in (checkpoint_path, published_index_path)
        if path.exists() and path.resolve() != temporary_path.resolve()
    )
    try:
        build_semantic_index(
            database_path,
            temporary_path,
            lexical_dimension=lexical_dimension,
            semantic_dimension=semantic_dimension,
            neural_enabled=False,
        )
        with sqlite3.connect(temporary_path) as connection:
            neural_dimension: int | None = None
            for reuse_path in reuse_paths:
                neural_dimension = _reuse_neural_embeddings(
                    connection,
                    reuse_path,
                    method=method,
                    build_signature=build_signature,
                    neural_model_id=neural_model_id,
                    neural_model_revision=neural_model_revision,
                    expected_dimension=neural_dimension,
                )
            documents_covered, unique_covered, unique_total = _neural_coverage(
                connection,
                neural_dimension,
            )
            _upsert_metadata(
                connection,
                {
                    "method": method,
                    "build_signature": build_signature,
                    "source_fingerprint": source_fingerprint,
                    "neural_status": (
                        "ready"
                        if documents_covered
                        == int(
                            connection.execute(
                                "SELECT COUNT(*) FROM items"
                            ).fetchone()[0]
                        )
                        else "building"
                    ),
                    "neural_documents": documents_covered,
                    "neural_unique_texts": unique_covered,
                    "neural_unique_texts_total": unique_total,
                    "neural_dimension": neural_dimension,
                    "neural_model_id": neural_model_id,
                    "neural_model_revision": neural_model_revision,
                    "neural_vector_encoding": NEURAL_VECTOR_ENCODING,
                    "neural_input_signature": NEURAL_INPUT_SIGNATURE,
                    "neural_error": None,
                },
            )
            connection.commit()
            if str(connection.execute("PRAGMA quick_check").fetchone()[0]).lower() != "ok":
                raise SemanticIndexError(
                    "La candidata semántica no supera la comprobación SQLite"
                )
        os.replace(temporary_path, checkpoint_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return False, source_fingerprint, method, build_signature


def build_or_resume_semantic_index(
    database_path: Path,
    published_index_path: Path,
    checkpoint_path: Path,
    *,
    lexical_dimension: int = 256,
    semantic_dimension: int = 64,
    neural_model_id: str = DEFAULT_NEURAL_MODEL_ID,
    neural_model_revision: str = DEFAULT_NEURAL_MODEL_REVISION,
    neural_batch_size: int = 64,
    max_unique_texts: int = 0,
    max_seconds: float = 0,
    neural_encoder: DenseTextEncoder | None = None,
) -> ResumableSemanticBuildSummary:
    """Avanza un índice neuronal durable y solo lo publica al completarlo.

    El checkpoint se confirma después de cada lote. Los vectores se identifican
    por el hash del texto exacto, por lo que se reutilizan aunque cambien los
    identificadores SQLite durante una reconstrucción.
    """

    database_path = Path(database_path)
    published_index_path = Path(published_index_path)
    checkpoint_path = Path(checkpoint_path)
    resolved_paths = {
        database_path.resolve(),
        published_index_path.resolve(),
        checkpoint_path.resolve(),
    }
    if len(resolved_paths) != 3:
        raise ValueError(
            "La base fuente, el índice publicado y el checkpoint deben ser distintos"
        )
    if neural_batch_size < 1:
        raise ValueError("neural_batch_size debe ser mayor que cero")
    if max_unique_texts < 0 or max_seconds < 0:
        raise ValueError("Los límites de la construcción no pueden ser negativos")
    if neural_encoder is not None and (
        neural_encoder.model_id != neural_model_id
        or neural_encoder.model_revision != neural_model_revision
    ):
        raise ValueError(
            "El codificador inyectado no coincide con el modelo fijado"
        )

    checkpoint_reused, source_fingerprint, method, build_signature = (
        _prepare_resumable_semantic_checkpoint(
            database_path,
            published_index_path,
            checkpoint_path,
            lexical_dimension=lexical_dimension,
            semantic_dimension=semantic_dimension,
            neural_model_id=neural_model_id,
            neural_model_revision=neural_model_revision,
        )
    )
    started = time.monotonic()
    with sqlite3.connect(checkpoint_path) as checkpoint:
        metadata = _read_metadata(checkpoint)
        neural_dimension = int(metadata.get("neural_dimension") or 0) or None
        documents_before, unique_before, unique_total = _neural_coverage(
            checkpoint,
            neural_dimension,
        )
        documents_total = int(metadata["documents"])
        if documents_total < 1:
            raise SemanticIndexError(
                "La base fuente no contiene fragmentos semánticos indexables"
            )
        documents_reused = documents_before if not checkpoint_reused else 0
        documents_confirmed = documents_before
        unique_confirmed = unique_before

        remaining_unique = max(0, unique_total - unique_before)
        if remaining_unique:
            encoder = neural_encoder or _fastembed_encoder(
                neural_model_id,
                neural_model_revision,
            )
            limit_clause = ""
            parameters: tuple[int, ...] = ()
            if max_unique_texts:
                limit_clause = "LIMIT ?"
                parameters = (max_unique_texts,)
            pending = checkpoint.execute(
                f"""
                SELECT item.content_hash, MIN(item.item_id), COUNT(*)
                FROM items AS item
                LEFT JOIN neural_embeddings AS neural
                  ON neural.content_hash = item.content_hash
                WHERE neural.content_hash IS NULL
                GROUP BY item.content_hash
                ORDER BY MIN(item.item_id)
                {limit_clause}
                """,
                parameters,
            ).fetchall()
            with sqlite3.connect(
                f"file:{database_path}?mode=ro", uri=True
            ) as source:
                for offset in range(0, len(pending), neural_batch_size):
                    if max_seconds and time.monotonic() - started >= max_seconds:
                        break
                    batch = pending[offset : offset + neural_batch_size]
                    item_ids = [int(row[1]) for row in batch]
                    placeholders = ",".join("?" for _ in item_ids)
                    texts = {
                        int(item_id): str(text)
                        for item_id, text in source.execute(
                            "SELECT id, text FROM chunks "
                            f"WHERE id IN ({placeholders})",
                            item_ids,
                        )
                    }
                    ordered_texts = [texts[item_id] for item_id in item_ids]
                    for (content_hash, _, _), text in zip(batch, ordered_texts):
                        if bytes(content_hash) != semantic_content_hash(text):
                            raise SemanticIndexError(
                                "El checkpoint no coincide con el texto fuente"
                            )
                    vectors = encoder.encode_passages(
                        ordered_texts,
                        batch_size=neural_batch_size,
                    )
                    if len(vectors) != len(batch):
                        raise NeuralSemanticUnavailable(
                            "El modelo neuronal no devolvió un vector por texto"
                        )
                    rows: list[tuple[bytes, bytes]] = []
                    for (content_hash, _, _), vector in zip(batch, vectors):
                        normalized = _validated_dense_vector(vector)
                        if neural_dimension is None:
                            neural_dimension = len(normalized)
                        elif len(normalized) != neural_dimension:
                            raise NeuralSemanticUnavailable(
                                "El modelo neuronal cambió de dimensión"
                            )
                        rows.append((bytes(content_hash), _pack_vector(normalized)))
                    try:
                        checkpoint.executemany(
                            """
                            INSERT INTO neural_embeddings (
                                content_hash, neural_vector
                            ) VALUES (?, ?)
                            ON CONFLICT(content_hash) DO UPDATE SET
                                neural_vector = excluded.neural_vector
                            """,
                            rows,
                        )
                        covered_now = sum(int(row[2]) for row in batch)
                        documents_confirmed += covered_now
                        unique_confirmed += len(batch)
                        _upsert_metadata(
                            checkpoint,
                            {
                                "neural_status": "building",
                                "neural_documents": documents_confirmed,
                                "neural_unique_texts": unique_confirmed,
                                "neural_unique_texts_total": unique_total,
                                "neural_dimension": neural_dimension,
                            },
                        )
                        checkpoint.commit()
                    except Exception:
                        checkpoint.rollback()
                        raise
                    current_documents = documents_confirmed
                    if (
                        current_documents == documents_total
                        or current_documents % 5_000 < covered_now
                    ):
                        print(
                            "Embeddings neuronales confirmados: "
                            f"{current_documents}/{documents_total}",
                            flush=True,
                        )

        documents_after, unique_after, unique_total = _neural_coverage(
            checkpoint,
            neural_dimension,
        )
        remaining = max(0, documents_total - documents_after)
        complete = remaining == 0
        if complete:
            _upsert_metadata(
                checkpoint,
                {
                    "neural_status": "ready",
                    "neural_documents": documents_total,
                    "neural_unique_texts": unique_total,
                    "neural_unique_texts_total": unique_total,
                    "neural_dimension": neural_dimension,
                    "neural_error": None,
                },
            )
            checkpoint.commit()
            if str(checkpoint.execute("PRAGMA quick_check").fetchone()[0]).lower() != "ok":
                raise SemanticIndexError(
                    "El índice neuronal completo no supera integridad SQLite"
                )

    if complete:
        # La validación estricta ocurre antes de tocar el índice publicado.
        semantic_index_info(checkpoint_path)
        _validate_items_against_source(checkpoint_path, database_path)
        published_index_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(checkpoint_path, published_index_path)
        result_path = published_index_path
    else:
        result_path = checkpoint_path

    return ResumableSemanticBuildSummary(
        complete=complete,
        checkpoint_reused=checkpoint_reused,
        documents_total=documents_total,
        documents_before=documents_before,
        documents_after=documents_after,
        documents_encoded=max(0, documents_after - documents_before),
        documents_reused=documents_reused,
        documents_remaining=remaining,
        unique_texts_total=unique_total,
        unique_texts_before=unique_before,
        unique_texts_after=unique_after,
        unique_texts_encoded=max(0, unique_after - unique_before),
        neural_dimension=neural_dimension,
        source_fingerprint=source_fingerprint,
        method=method,
        build_signature=build_signature,
        index_size_bytes=result_path.stat().st_size,
    )


@lru_cache(maxsize=8)
def _semantic_source_fingerprint_cached(
    resolved_path: str,
    file_size: int,
    modified_ns: int,
) -> str:
    """Calcula una huella exacta una vez por generación del archivo SQLite."""

    del file_size, modified_ns  # Forman parte de la clave de caché.
    database_path = Path(resolved_path)
    digest = hashlib.sha256()
    try:
        with sqlite3.connect(
            f"file:{database_path}?mode=ro",
            uri=True,
        ) as connection:
            chunk_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(chunks)")
            }
            if {"page_id", "chunk_index"}.issubset(chunk_columns):
                chunk_rows = connection.execute(
                    "SELECT id, page_id, chunk_index, text FROM chunks ORDER BY id"
                )
            else:
                chunk_rows = (
                    (item_id, 0, 0, text)
                    for item_id, text in connection.execute(
                        "SELECT id, text FROM chunks ORDER BY id"
                    )
                )
            for item_id, page_id, chunk_index, text in chunk_rows:
                digest.update(
                    struct.pack(
                        "<QQQ",
                        int(item_id),
                        int(page_id),
                        int(chunk_index),
                    )
                )
                encoded = str(text).encode("utf-8")
                digest.update(struct.pack("<Q", len(encoded)))
                digest.update(encoded)
            has_pages = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pages'"
            ).fetchone()
            if has_pages:
                for page_id, document_id, page_number in connection.execute(
                    "SELECT id, document_id, page_number FROM pages ORDER BY id"
                ):
                    digest.update(
                        struct.pack(
                            "<QQQ",
                            int(page_id),
                            int(document_id),
                            int(page_number),
                        )
                    )
            has_documents = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='documents'"
            ).fetchone()
            if has_documents:
                for document_id, manifest_url, document_hash in connection.execute(
                    "SELECT id, manifest_url, document_hash FROM documents "
                    "ORDER BY id"
                ):
                    digest.update(struct.pack("<Q", int(document_id)))
                    digest.update(
                        f"{manifest_url}\x1f{document_hash}\n".encode("utf-8")
                    )
    except sqlite3.Error as exc:
        raise SemanticIndexError(
            "No fue posible calcular la identidad del corpus"
        ) from exc
    return digest.hexdigest()


def semantic_source_fingerprint(database_path: Path) -> str:
    """Identifica texto, IDs y relaciones exactas de la base documental."""

    database_path = Path(database_path)
    if not database_path.exists():
        raise SemanticIndexError("No existe la base de fragmentos")
    state = database_path.stat()
    return _semantic_source_fingerprint_cached(
        str(database_path.resolve()),
        int(state.st_size),
        int(state.st_mtime_ns),
    )


def semantic_index_info(index_path: Path) -> dict[str, object]:
    """Devuelve metadatos verificando antes la versión del formato."""
    index_path = Path(index_path)
    if not index_path.exists():
        raise SemanticIndexError("No existe el índice semántico")
    try:
        with sqlite3.connect(f"file:{index_path}?mode=ro", uri=True) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if not {"metadata", "terms", "items"}.issubset(tables):
                raise SemanticIndexError(
                    "El índice semántico no contiene todas sus tablas"
                )
            info = _read_metadata(connection)
            try:
                declared_documents = int(info["documents"])
                lexical_dimension = int(info["lexical_dimension"])
                semantic_dimension = int(info["semantic_dimension"])
            except (TypeError, ValueError) as exc:
                raise SemanticIndexError(
                    "Los metadatos dimensionales del índice son inválidos"
                ) from exc
            item_count = int(
                connection.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            )
            if item_count != declared_documents:
                raise SemanticIndexError(
                    "La cobertura declarada del índice semántico no coincide"
                )
            sample = connection.execute(
                "SELECT length(lexical_vector), length(distributional_vector) "
                "FROM items LIMIT 1"
            ).fetchone()
            if sample and (
                sample[0] is None
                or sample[1] is None
                or int(sample[0]) != lexical_dimension
                or int(sample[1]) != semantic_dimension
            ):
                raise SemanticIndexError(
                    "El índice contiene vectores con dimensión incompatible"
                )
            neural_status = str(info.get("neural_status", "disabled"))
            if neural_status == "ready":
                item_columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(items)")
                }
                try:
                    neural_dimension = int(info["neural_dimension"])
                    neural_documents = int(info["neural_documents"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise SemanticIndexError(
                        "Los metadatos neuronales del índice son inválidos"
                    ) from exc
                if int(info.get("format_version", 1)) >= 2:
                    if (
                        "content_hash" not in item_columns
                        or "neural_embeddings" not in tables
                    ):
                        raise SemanticIndexError(
                            "El índice neuronal no contiene su caché por contenido"
                        )
                    invalid_hashes = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM items "
                            "WHERE length(content_hash) != 32"
                        ).fetchone()[0]
                    )
                    missing_neural = int(
                        connection.execute(
                            """
                            SELECT COUNT(*)
                            FROM items AS item
                            LEFT JOIN neural_embeddings AS neural
                              ON neural.content_hash = item.content_hash
                            WHERE neural.neural_vector IS NULL
                               OR length(neural.neural_vector) != ?
                            """,
                            (neural_dimension,),
                        ).fetchone()[0]
                    )
                    neural_count = declared_documents - missing_neural
                    unique_texts = int(
                        connection.execute(
                            "SELECT COUNT(DISTINCT content_hash) FROM items"
                        ).fetchone()[0]
                    )
                    declared_unique = int(
                        info.get("neural_unique_texts", unique_texts)
                    )
                    if invalid_hashes or declared_unique != unique_texts:
                        raise SemanticIndexError(
                            "La identidad de contenido del índice es inválida"
                        )
                else:
                    if "neural_vector" not in item_columns:
                        raise SemanticIndexError(
                            "El índice declara embeddings neuronales pero no los contiene"
                        )
                    neural_count = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM items "
                            "WHERE length(neural_vector) = ?",
                            (neural_dimension,),
                        ).fetchone()[0]
                    )
                if (
                    neural_dimension < 1
                    or neural_documents != declared_documents
                    or neural_count != declared_documents
                ):
                    raise SemanticIndexError(
                        "La cobertura neuronal declarada no coincide con el corpus"
                    )
            return info
    except sqlite3.Error as exc:
        raise SemanticIndexError("No fue posible abrir el índice semántico") from exc


def semantic_index_status(
    index_path: Path,
    source_database_path: Path | None = None,
) -> dict[str, object]:
    """Estado tolerante a fallos y, opcionalmente, coherencia con ``actas.db``."""
    index_path = Path(index_path)
    if not index_path.exists():
        return {
            "available": False,
            "reason": "missing",
            "message": "El índice semántico aún no ha sido construido.",
        }
    try:
        info = semantic_index_info(index_path)
    except SemanticIndexError as exc:
        return {
            "available": False,
            "reason": "invalid",
            "message": str(exc),
        }
    if source_database_path is not None:
        try:
            actual_fingerprint = semantic_source_fingerprint(source_database_path)
        except SemanticIndexError as exc:
            return {
                "available": False,
                "reason": "source_invalid",
                "message": str(exc),
            }
        expected_fingerprint = info.get("source_fingerprint")
        if not expected_fingerprint or expected_fingerprint != actual_fingerprint:
            return {
                "available": False,
                "reason": "stale",
                "message": (
                    "El índice semántico no corresponde a la base documental "
                    "activa; se usará búsqueda textual hasta reconstruirlo."
                ),
                "source_fingerprint": expected_fingerprint,
                "actual_source_fingerprint": actual_fingerprint,
            }
    neural_ready = info.get("neural_status") == "ready"
    runtime_installed = neural_runtime_installed()
    message = "Índice semántico local disponible."
    if neural_ready and runtime_installed:
        message = "Índice semántico neuronal multilingüe disponible."
    elif neural_ready:
        message = (
            "Embeddings neuronales indexados; falta FastEmbed en este entorno, "
            "por lo que se usará el respaldo semántico local."
        )
    return {
        "available": True,
        "reason": "ready",
        "message": message,
        "size_bytes": index_path.stat().st_size,
        "neural_runtime_installed": runtime_installed,
        **info,
    }


def _term_rows(
    connection: sqlite3.Connection,
    tokens: Sequence[str],
) -> dict[str, tuple[float, bytes | None]]:
    unique_tokens = list(dict.fromkeys(tokens))
    if not unique_tokens:
        return {}
    result: dict[str, tuple[float, bytes | None]] = {}
    for start in range(0, len(unique_tokens), _ITEM_BATCH_SIZE):
        batch = unique_tokens[start : start + _ITEM_BATCH_SIZE]
        placeholders = ",".join("?" for _ in batch)
        rows = connection.execute(
            f"""
            SELECT term, inverse_document_frequency, distributional_vector
            FROM terms WHERE term IN ({placeholders})
            """,
            batch,
        ).fetchall()
        result.update(
            {
                str(term): (float(inverse_document_frequency), vector)
                for term, inverse_document_frequency, vector in rows
            }
        )
    return result


def _item_rows(
    connection: sqlite3.Connection,
    candidate_ids: Sequence[int] | None,
    *,
    include_neural: bool = False,
) -> Iterator[tuple[int, bytes, bytes, bytes | None]]:
    has_neural_table = connection.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='table' AND name='neural_embeddings'"
    ).fetchone() is not None
    if include_neural and has_neural_table:
        neural_column = "neural.neural_vector"
        join_clause = (
            "LEFT JOIN neural_embeddings AS neural "
            "ON neural.content_hash = item.content_hash"
        )
    else:
        neural_column = "item.neural_vector" if include_neural else "NULL"
        join_clause = ""
    if candidate_ids is None:
        yield from connection.execute(
            "SELECT item.item_id, item.lexical_vector, "
            "item.distributional_vector, "
            f"{neural_column} FROM items AS item {join_clause}"
        )
        return
    unique_ids = list(dict.fromkeys(int(item_id) for item_id in candidate_ids))
    for start in range(0, len(unique_ids), _ITEM_BATCH_SIZE):
        batch = unique_ids[start : start + _ITEM_BATCH_SIZE]
        placeholders = ",".join("?" for _ in batch)
        yield from connection.execute(
            f"""
            SELECT item.item_id, item.lexical_vector,
                   item.distributional_vector,
                   {neural_column}
            FROM items AS item {join_clause}
            WHERE item.item_id IN ({placeholders})
            """,
            batch,
        )


def query_semantic_index(
    index_path: Path,
    query: str,
    *,
    top_k: int = 20,
    candidate_ids: Sequence[int] | None = None,
    lexical_weight: float = 0.35,
    distributional_weight: float = 0.65,
    neural_weight: float = 0.0,
    neural_encoder: DenseTextEncoder | None = None,
    min_score: float = 0.01,
) -> list[SemanticHit]:
    """Consulta el índice o reordena ``candidate_ids``.

    La ponderación predeterminada favorece la señal distribucional porque FTS5
    puede aportar la señal léxica principal fuera de este módulo. Si la consulta
    no contiene términos presentes en el corpus, no devuelve coincidencias.
    """
    if top_k < 1:
        raise ValueError("top_k debe ser mayor que cero")
    if lexical_weight < 0 or distributional_weight < 0 or neural_weight < 0:
        raise ValueError("Los pesos no pueden ser negativos")
    if lexical_weight + distributional_weight + neural_weight <= 0:
        raise ValueError("Al menos un peso debe ser mayor que cero")
    if min_score < 0:
        raise ValueError("min_score no puede ser negativo")
    if candidate_ids is not None and not candidate_ids:
        return []

    tokens = _tokenize(query)
    if not tokens:
        return []
    index_path = Path(index_path)
    if not index_path.exists():
        return []

    try:
        with sqlite3.connect(f"file:{index_path}?mode=ro", uri=True) as connection:
            metadata = _read_metadata(connection)
            lexical_dimension = int(metadata["lexical_dimension"])
            semantic_dimension = int(metadata["semantic_dimension"])
            document_count = int(metadata["documents"])
            neural_dimension = int(metadata.get("neural_dimension") or 0)
            has_neural_index = (
                metadata.get("neural_status") == "ready"
                and neural_dimension > 0
            )
            terms = _term_rows(connection, tokens)
            if not terms and not (has_neural_index and neural_weight > 0):
                return []
            idf_by_term = {term: values[0] for term, values in terms.items()}
            context_vectors = {
                term: _unpack_vector(values[1], semantic_dimension)
                for term, values in terms.items()
                if values[1] is not None
            }
            counts = Counter(token for token in tokens if token in terms)
            query_lexical = _lexical_vector(
                counts,
                idf_by_term,
                lexical_dimension,
                _idf(document_count, 0),
            )
            query_distributional = _distributional_vector(
                counts,
                idf_by_term,
                context_vectors,
                semantic_dimension,
            )
            query_neural: list[float] = []
            if has_neural_index and neural_weight > 0:
                try:
                    effective_encoder = neural_encoder or _fastembed_encoder(
                        str(metadata.get("neural_model_id") or ""),
                        str(metadata.get("neural_model_revision") or ""),
                    )
                    query_neural = _validated_dense_vector(
                        effective_encoder.encode_query(query)
                    )
                    if len(query_neural) != neural_dimension:
                        raise NeuralSemanticUnavailable(
                            "La dimensión de consulta no coincide con el índice"
                        )
                except Exception:
                    # Fallar al cargar/consultar el modelo no debe romper el
                    # modo semántico local ni el explorador textual.
                    query_neural = []
            has_distributional_signal = any(query_distributional)
            effective_distributional_weight = (
                distributional_weight if has_distributional_signal else 0.0
            )
            effective_neural_weight = neural_weight if query_neural else 0.0
            effective_total = (
                lexical_weight
                + effective_distributional_weight
                + effective_neural_weight
            )
            if effective_total <= 0.0:
                return []
            effective_lexical_weight = lexical_weight / effective_total
            effective_distributional_weight /= effective_total
            effective_neural_weight /= effective_total

            hits: list[SemanticHit] = []
            for item_id, lexical_blob, distributional_blob, neural_blob in _item_rows(
                connection,
                candidate_ids,
                include_neural=bool(query_neural),
            ):
                lexical = max(
                    0.0,
                    min(
                        1.0,
                        _dot(
                            query_lexical,
                            _unpack_vector(lexical_blob, lexical_dimension),
                        ),
                    ),
                )
                distributional = 0.0
                if has_distributional_signal:
                    distributional = max(
                        0.0,
                        min(
                            1.0,
                            _dot(
                                query_distributional,
                                _unpack_vector(
                                    distributional_blob, semantic_dimension
                                ),
                            ),
                        ),
                    )
                neural = 0.0
                if query_neural and neural_blob is not None:
                    neural = max(
                        0.0,
                        min(
                            1.0,
                            _dot(
                                query_neural,
                                _unpack_vector(neural_blob, neural_dimension),
                            ),
                        ),
                    )
                score = (
                    effective_lexical_weight * lexical
                    + effective_distributional_weight * distributional
                    + effective_neural_weight * neural
                )
                if score >= min_score:
                    hits.append(
                        SemanticHit(
                            item_id=int(item_id),
                            score=round(score, 6),
                            lexical_score=round(lexical, 6),
                            distributional_score=round(distributional, 6),
                            neural_score=round(neural, 6),
                        )
                    )
    except sqlite3.Error as exc:
        raise SemanticIndexError("No fue posible consultar el índice semántico") from exc

    hits.sort(key=lambda hit: (-hit.score, hit.item_id))
    return hits[:top_k]


def semantic_search(
    index_path: Path,
    query: str,
    top_k: int = 20,
    allowed_ids: Sequence[int] | None = None,
) -> list[tuple[int, float]]:
    """API compacta para la capa de recuperación de la aplicación.

    Devuelve ``(chunk_id, score)``. ``allowed_ids`` permite reordenar los
    candidatos producidos por FTS5 y evita un barrido completo del índice.
    Para auditoría de cada señal, use :func:`query_semantic_index`.
    """
    candidate_ids = allowed_ids
    # Comparar el vector neuronal de 384 dimensiones contra todo el corpus en
    # Python sería costoso en el modo semántico puro. La señal distribucional
    # liviana genera primero un conjunto amplio de candidatos y MiniLM los
    # reordena después. En modo híbrido, FTS5 ya cumple esa misma función.
    large_pool = candidate_ids is None or len(candidate_ids) > max(top_k * 4, 5_000)
    if large_pool:
        preliminary = query_semantic_index(
            index_path,
            query,
            top_k=max(top_k * 4, 1_200),
            candidate_ids=candidate_ids,
            lexical_weight=0.35,
            distributional_weight=0.65,
            neural_weight=0.0,
            min_score=0.001,
        )
        if preliminary:
            candidate_ids = [hit.item_id for hit in preliminary]

    return [
        (hit.item_id, hit.score)
        for hit in query_semantic_index(
            index_path,
            query,
            top_k=top_k,
            candidate_ids=candidate_ids,
            lexical_weight=0.15,
            distributional_weight=0.25,
            neural_weight=0.60,
        )
    ]


def _normalized_scores(scores: Mapping[int, float]) -> dict[int, float]:
    positive = {int(item_id): max(0.0, float(score)) for item_id, score in scores.items()}
    maximum = max(positive.values(), default=0.0)
    if maximum <= 0.0:
        return {item_id: 0.0 for item_id in positive}
    return {item_id: score / maximum for item_id, score in positive.items()}


def reciprocal_rank_fusion(
    lexical_scores: Mapping[int, float],
    semantic_scores: Mapping[int, float] | Iterable[SemanticHit],
    *,
    lexical_weight: float = 0.60,
    semantic_weight: float = 0.40,
    rank_constant: int = 60,
    top_k: int = 20,
    include_semantic_only: bool = True,
) -> list[HybridScore]:
    """Fusiona rangos sin asumir que BM25 y coseno comparten escala.

    La puntuación de cada lista se deriva de su posición con RRF y se normaliza
    para que la primera posición aporte 1. Esto hace el reranking estable cuando
    cambia la distribución numérica de FTS5 o del codificador semántico.
    """

    if lexical_weight < 0 or semantic_weight < 0:
        raise ValueError("Los pesos no pueden ser negativos")
    if lexical_weight + semantic_weight <= 0:
        raise ValueError("Al menos un peso debe ser mayor que cero")
    if rank_constant < 1:
        raise ValueError("rank_constant debe ser mayor que cero")
    if top_k < 1:
        raise ValueError("top_k debe ser mayor que cero")
    if isinstance(semantic_scores, Mapping):
        semantic_mapping = {
            int(item_id): float(score) for item_id, score in semantic_scores.items()
        }
    else:
        semantic_mapping = {hit.item_id: hit.score for hit in semantic_scores}

    def ranked(values: Mapping[int, float]) -> dict[int, float]:
        ordered = sorted(
            ((int(item_id), float(score)) for item_id, score in values.items()),
            key=lambda item: (-item[1], item[0]),
        )
        return {
            item_id: (rank_constant + 1) / (rank_constant + rank)
            for rank, (item_id, _) in enumerate(ordered, start=1)
        }

    lexical = ranked(lexical_scores)
    semantic = ranked(semantic_mapping)
    item_ids = set(lexical)
    if include_semantic_only:
        item_ids.update(semantic)
    total_weight = lexical_weight + semantic_weight
    lexical_factor = lexical_weight / total_weight
    semantic_factor = semantic_weight / total_weight
    combined = [
        HybridScore(
            item_id=item_id,
            score=round(
                lexical_factor * lexical.get(item_id, 0.0)
                + semantic_factor * semantic.get(item_id, 0.0),
                6,
            ),
            lexical_score=round(lexical.get(item_id, 0.0), 6),
            semantic_score=round(semantic.get(item_id, 0.0), 6),
        )
        for item_id in item_ids
    ]
    combined.sort(key=lambda hit: (-hit.score, hit.item_id))
    return combined[:top_k]


def combine_rankings(
    lexical_scores: Mapping[int, float],
    semantic_scores: Mapping[int, float] | Iterable[SemanticHit],
    *,
    lexical_weight: float = 0.65,
    semantic_weight: float = 0.35,
    top_k: int = 20,
    include_semantic_only: bool = True,
) -> list[HybridScore]:
    """Fusiona puntuaciones externas de FTS5 con la recuperación local.

    Cada fuente se normaliza por su máximo antes de ponderarla, evitando mezclar
    directamente escalas BM25, bonificaciones de interfaz y cosenos. Use
    ``include_semantic_only=False`` para un reranking estricto de candidatos
    léxicos.
    """
    if lexical_weight < 0 or semantic_weight < 0:
        raise ValueError("Los pesos no pueden ser negativos")
    if lexical_weight + semantic_weight <= 0:
        raise ValueError("Al menos un peso debe ser mayor que cero")
    if top_k < 1:
        raise ValueError("top_k debe ser mayor que cero")

    if isinstance(semantic_scores, Mapping):
        semantic_mapping = {
            int(item_id): float(score) for item_id, score in semantic_scores.items()
        }
    else:
        semantic_mapping = {hit.item_id: hit.score for hit in semantic_scores}

    lexical = _normalized_scores(lexical_scores)
    semantic = _normalized_scores(semantic_mapping)
    item_ids = set(lexical)
    if include_semantic_only:
        item_ids.update(semantic)
    total_weight = lexical_weight + semantic_weight
    lexical_factor = lexical_weight / total_weight
    semantic_factor = semantic_weight / total_weight

    combined = [
        HybridScore(
            item_id=item_id,
            score=round(
                lexical_factor * lexical.get(item_id, 0.0)
                + semantic_factor * semantic.get(item_id, 0.0),
                6,
            ),
            lexical_score=round(lexical.get(item_id, 0.0), 6),
            semantic_score=round(semantic.get(item_id, 0.0), 6),
        )
        for item_id in item_ids
    ]
    combined.sort(key=lambda hit: (-hit.score, hit.item_id))
    return combined[:top_k]
