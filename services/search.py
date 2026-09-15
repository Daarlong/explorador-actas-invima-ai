from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from pathlib import Path

from services.database import (
    chunk_ids_for_filters,
    get_chunks_by_ids,
    search_pages_exact,
    search_regulatory_fields,
    search_chunks,
    search_chunks_document_page,
)
from services.models import SearchResult
from services.semantic import (
    reciprocal_rank_fusion,
    semantic_index_status,
    semantic_search,
)
from services.text_utils import normalize_phrase, normalize_text


SEARCH_MODES = ("hybrid", "textual", "semantic")


@dataclass(frozen=True)
class SearchResponse:
    results: list[SearchResult]
    requested_mode: str
    used_mode: str
    semantic_available: bool
    semantic_message: str
    semantic_backend: str = "none"
    retrieval_backend: str = "fts5"
    fallback_reason: str | None = None
    neural_used: bool = False
    ann_used: bool = False
    candidate_count: int | None = None
    field_scope: str = "all"
    total_documents: int | None = None
    total_fragments: int | None = None
    totals_exact: bool = False
    page: int = 1
    page_size: int | None = None
    total_pages: int | None = None
    has_next: bool = False


SEARCH_FIELD_SCOPES = (
    "all",
    "request",
    "concept",
    "product",
    "active_ingredient",
    "interested_party",
    "expediente",
    "radicado",
    "record",
    "outcome",
)


def _response_totals(results: list[SearchResult]) -> tuple[int, int]:
    return len({(item.title, item.url) for item in results}), len(results)


def _backend_from_state(
    semantic_state: dict[str, object],
) -> tuple[str, str | None]:
    if not semantic_state.get("available"):
        return "none", str(semantic_state.get("message") or "") or None
    if (
        semantic_state.get("neural_status") == "ready"
        and semantic_state.get("neural_runtime_installed") is not False
    ):
        return "neural", None
    if semantic_state.get("neural_status") == "ready":
        return "local_fallback", str(semantic_state.get("message") or "") or None
    return "local", None


def _diversify_results(
    results: list[SearchResult],
    *,
    top_k: int,
    max_per_document: int,
) -> list[SearchResult]:
    counts: dict[tuple[str, str], int] = {}
    selected: list[SearchResult] = []
    for result in results:
        key = (result.title, result.url)
        if counts.get(key, 0) >= max_per_document:
            continue
        selected.append(result)
        counts[key] = counts.get(key, 0) + 1
        if len(selected) == top_k:
            break
    return selected


def _exact_identifier_bonus(query: str, result: SearchResult) -> float:
    identifiers = re.findall(r"\b\d{5,}\b", normalize_text(query))
    if not identifiers:
        return 0.0
    normalized_text = normalize_text(f"{result.title} {result.text}")
    return (
        1.0
        if any(identifier in normalized_text for identifier in identifiers)
        else 0.0
    )


def _literal_phrase_bonus(query: str, result: SearchResult) -> float:
    phrase = normalize_phrase(query)
    if len(phrase.split()) < 2:
        return 0.0
    return (
        2.0
        if phrase in normalize_phrase(result.title)
        or phrase in normalize_phrase(result.text)
        else 0.0
    )


def _merge_lexical_candidates(
    regular: list[SearchResult],
    literal: list[SearchResult],
) -> list[SearchResult]:
    """Une candidatos conservando la mejor puntuación de cada fragmento."""

    by_id: dict[int, SearchResult] = {}
    for result in [*literal, *regular]:
        previous = by_id.get(result.chunk_id)
        if previous is None or result.score > previous.score:
            by_id[result.chunk_id] = result
    return sorted(by_id.values(), key=lambda item: (-item.score, item.chunk_id))


def search_corpus(
    database_path: Path,
    semantic_index_path: Path,
    query: str,
    *,
    mode: str = "hybrid",
    top_k: int = 200,
    filters: dict[str, list] | None = None,
    exact_phrase: bool = False,
    field_scope: str = "all",
    ann_index_path: Path | None = None,
) -> SearchResponse:
    if mode not in SEARCH_MODES:
        raise ValueError(f"Modo de búsqueda desconocido: {mode}")
    if top_k < 1:
        raise ValueError("top_k debe ser mayor que cero")
    if field_scope not in SEARCH_FIELD_SCOPES:
        raise ValueError(f"Campo de búsqueda desconocido: {field_scope}")

    # Un semantic.db válido pero construido para otra actas.db es más
    # peligroso que no tenerlo: los IDs hidratarían fragmentos equivocados.
    semantic_state = semantic_index_status(
        semantic_index_path,
        database_path,
    )
    semantic_available = bool(semantic_state.get("available"))
    semantic_message = str(semantic_state.get("message", ""))
    semantic_backend, fallback_reason = _backend_from_state(semantic_state)
    used_mode = mode
    if exact_phrase:
        used_mode = "textual"
    if mode in {"hybrid", "semantic"} and not semantic_available:
        used_mode = "textual"

    # Las fichas estructuradas tienen un índice textual propio. No se afirma
    # semántica sobre un campo si los embeddings fueron creados por fragmento.
    if field_scope != "all":
        field_results = search_regulatory_fields(
            database_path,
            query,
            scope=field_scope,
            top_k=top_k,
            filters=filters,
            exact_phrase=exact_phrase,
        )
        total_documents, total_fragments = _response_totals(field_results)
        return SearchResponse(
            results=field_results,
            requested_mode=mode,
            used_mode="textual",
            semantic_available=semantic_available,
            semantic_message=semantic_message,
            semantic_backend="none",
            retrieval_backend="structured_fts",
            fallback_reason=None,
            field_scope=field_scope,
            total_documents=total_documents,
            total_fragments=total_fragments,
        )

    candidate_limit = max(top_k * 3, 120)
    max_per_document = 5 if top_k <= 20 else 3
    lexical_results: list[SearchResult] = []
    literal_results: list[SearchResult] = []
    if used_mode in {"textual", "hybrid"}:
        if exact_phrase:
            lexical_results = search_pages_exact(
                database_path,
                query,
                top_k=candidate_limit,
                filters=filters,
                max_pages_per_document=max_per_document,
            )
        else:
            lexical_results = search_chunks(
                database_path,
                query,
                top_k=candidate_limit,
                filters=filters,
                exact_phrase=False,
                max_chunks_per_document=max_per_document,
            )
        # Una consulta de varias palabras no debe perder una cita textual
        # conocida por aplicar el LIMIT del ranking OR antes de calcular el
        # bono literal o hacer el reranking neuronal. Una sonda compatible con
        # FTS detail=column incorpora esos fragmentos al conjunto candidato.
        if not exact_phrase and len(normalize_phrase(query).split()) >= 2:
            literal_results = search_chunks(
                database_path,
                query,
                top_k=candidate_limit,
                filters=filters,
                exact_phrase=True,
                max_chunks_per_document=max_per_document,
            )
            lexical_results = _merge_lexical_candidates(
                lexical_results,
                literal_results,
            )
    if used_mode == "textual":
        total_documents, total_fragments = _response_totals(lexical_results)
        return SearchResponse(
            results=_diversify_results(
                lexical_results,
                top_k=top_k,
                max_per_document=max_per_document,
            ),
            requested_mode=mode,
            used_mode="textual",
            semantic_available=semantic_available,
            semantic_message=semantic_message,
            semantic_backend="none",
            retrieval_backend=("page_phrase" if exact_phrase else "fts5"),
            fallback_reason=(fallback_reason if mode != "textual" else None),
            field_scope=field_scope,
            total_documents=total_documents,
            total_fragments=total_fragments,
        )

    # El carril semántico es independiente del carril textual. Sin filtros se
    # consulta globalmente; con filtros se entrega el universo completo
    # permitido, no solo los IDs ya recuperados por FTS5.
    allowed_ids = chunk_ids_for_filters(database_path, filters)
    semantic_trace: dict[str, object] = {}
    semantic_hits = semantic_search(
        semantic_index_path,
        query,
        top_k=candidate_limit,
        allowed_ids=allowed_ids,
        trace=semantic_trace,
        ann_index_path=ann_index_path,
    )
    if semantic_trace:
        neural_used = bool(semantic_trace.get("neural_used"))
        trace_backend = str(semantic_trace.get("backend") or "")
        if trace_backend == "neural_ann":
            semantic_backend = "neural_ann"
        elif trace_backend == "neural_exact":
            semantic_backend = "neural"
        elif semantic_trace.get("fallback_reason"):
            semantic_backend = "local_fallback"
            fallback_reason = str(semantic_trace["fallback_reason"])
        elif trace_backend:
            semantic_backend = "local"
    else:
        neural_used = semantic_backend == "neural"
    semantic_scores = dict(semantic_hits)

    if used_mode == "semantic":
        semantic_results = get_chunks_by_ids(
            database_path,
            [chunk_id for chunk_id, _ in semantic_hits],
            filters=filters,
            scores=semantic_scores,
        )
        final_results = _diversify_results(
                [
                    replace(
                        result,
                        semantic_score=result.score,
                        lexical_score=None,
                        match_type="semantic",
                    )
                    for result in semantic_results
                ],
                top_k=top_k,
                max_per_document=max_per_document,
            )
        total_documents, total_fragments = _response_totals(final_results)
        return SearchResponse(
            results=final_results,
            requested_mode=mode,
            used_mode="semantic",
            semantic_available=True,
            semantic_message=semantic_message,
            semantic_backend=semantic_backend,
            retrieval_backend=(
                "neural_ann" if semantic_backend == "neural_ann" else "semantic"
            ),
            fallback_reason=fallback_reason,
            neural_used=neural_used,
            ann_used=bool(semantic_trace.get("ann_used")),
            candidate_count=int(
                semantic_trace.get("candidates_scored") or len(semantic_hits)
            ),
            field_scope=field_scope,
            total_documents=total_documents,
            total_fragments=total_fragments,
        )

    lexical_scores = {result.chunk_id: result.score for result in lexical_results}
    fused = reciprocal_rank_fusion(
        lexical_scores,
        semantic_scores,
        lexical_weight=0.60,
        semantic_weight=0.40,
        top_k=candidate_limit,
        include_semantic_only=True,
    )
    fused_scores = {item.item_id: item.score for item in fused}
    lexical_normalized = {item.item_id: item.lexical_score for item in fused}
    semantic_normalized = {item.item_id: item.semantic_score for item in fused}
    fused_ids = [item.item_id for item in fused]
    fused_id_set = set(fused_ids)
    # Una coincidencia literal es evidencia más fuerte que una paráfrasis. Si
    # el truncado de RRF la desplazara, se reintroduce antes de hidratarla; el
    # bonus literal posterior determina su posición final de forma explícita.
    for result in literal_results:
        if result.chunk_id in fused_id_set:
            continue
        fused_ids.append(result.chunk_id)
        fused_id_set.add(result.chunk_id)
        fused_scores[result.chunk_id] = 0.0
        lexical_normalized[result.chunk_id] = 0.0
        semantic_normalized[result.chunk_id] = 0.0
    hydrated = get_chunks_by_ids(
        database_path,
        fused_ids,
        filters=filters,
        scores=fused_scores,
    )
    combined = [
        replace(
            result,
            score=round(
                result.score
                + _exact_identifier_bonus(query, result)
                + _literal_phrase_bonus(query, result),
                6,
            ),
            lexical_score=lexical_normalized.get(result.chunk_id),
            semantic_score=semantic_normalized.get(result.chunk_id),
            match_type="hybrid",
        )
        for result in hydrated
    ]
    combined.sort(key=lambda result: (-result.score, result.chunk_id))
    final_results = _diversify_results(
            combined,
            top_k=top_k,
            max_per_document=max_per_document,
        )
    total_documents, total_fragments = _response_totals(final_results)
    return SearchResponse(
        results=final_results,
        requested_mode=mode,
        used_mode="hybrid",
        semantic_available=True,
        semantic_message=semantic_message,
        semantic_backend=semantic_backend,
        retrieval_backend=(
            "hybrid_ann" if semantic_backend == "neural_ann" else "hybrid"
        ),
        fallback_reason=fallback_reason,
        neural_used=neural_used,
        ann_used=bool(semantic_trace.get("ann_used")),
        candidate_count=int(
            semantic_trace.get("candidates_scored") or len(semantic_hits)
        ),
        field_scope=field_scope,
        total_documents=total_documents,
        total_fragments=total_fragments,
    )


def _group_for_pagination(
    results: list[SearchResult],
    *,
    order: str,
) -> list[list[SearchResult]]:
    grouped: dict[tuple[object, ...], list[SearchResult]] = {}
    first_position: dict[tuple[object, ...], int] = {}
    for position, result in enumerate(results):
        key = (
            result.document_id
            if result.document_id is not None
            else (result.title, result.url)
        )
        compound_key = (key,) if not isinstance(key, tuple) else key
        grouped.setdefault(compound_key, []).append(result)
        first_position.setdefault(compound_key, position)

    def group_key(item: tuple[tuple[object, ...], list[SearchResult]]):
        key, values = item
        best = max(value.score for value in values)
        year = next((value.year for value in values if value.year is not None), None)
        acta = str(next((value.acta_number for value in values if value.acta_number), ""))
        if order == "newest":
            return (-(year if year is not None else -1), acta, first_position[key])
        if order == "oldest":
            return ((year if year is not None else 9999), acta, first_position[key])
        return (-best, first_position[key])

    if order not in {"relevance", "newest", "oldest"}:
        raise ValueError("Orden de resultados desconocido")
    ordered = sorted(grouped.items(), key=group_key)
    return [
        sorted(values, key=lambda value: (-value.score, value.chunk_id))
        for _, values in ordered
    ]


def search_corpus_page(
    database_path: Path,
    semantic_index_path: Path,
    query: str,
    *,
    mode: str = "hybrid",
    page: int = 1,
    page_size: int = 10,
    order: str = "relevance",
    filters: dict[str, list] | None = None,
    exact_phrase: bool = False,
    field_scope: str = "all",
    ann_index_path: Path | None = None,
    fragments_per_document: int = 3,
    candidate_limit: int = 3_000,
) -> SearchResponse:
    """Consulta una página de actas y declara la exactitud de sus totales.

    La modalidad textual ordinaria pagina y cuenta en SQLite sobre todo el
    corpus. Las modalidades neuronal/híbrida pagan solo el conjunto global de
    candidatos recuperado por ANN y lo señalan como aproximado, porque no
    existe un total matemático de "documentos semánticamente parecidos" sin
    fijar un umbral arbitrario.
    """

    if page < 1:
        raise ValueError("page debe ser mayor que cero")
    if page_size < 1 or page_size > 100:
        raise ValueError("page_size debe estar entre 1 y 100")
    if candidate_limit < page_size:
        raise ValueError("candidate_limit no puede ser menor que page_size")
    if order not in {"relevance", "newest", "oldest"}:
        raise ValueError("Orden de resultados desconocido")

    semantic_state = semantic_index_status(semantic_index_path, database_path)
    effective_textual = mode == "textual" or (
        mode in {"hybrid", "semantic"} and not semantic_state.get("available")
    )
    if effective_textual and not exact_phrase and field_scope == "all":
        results, total_documents, total_fragments = search_chunks_document_page(
            database_path,
            query,
            page=page,
            page_size=page_size,
            order=order,
            filters=filters,
            fragments_per_document=fragments_per_document,
        )
        total_pages = max(1, math.ceil(total_documents / page_size))
        return SearchResponse(
            results=results,
            requested_mode=mode,
            used_mode="textual",
            semantic_available=bool(semantic_state.get("available")),
            semantic_message=str(semantic_state.get("message") or ""),
            semantic_backend="none",
            retrieval_backend="fts5",
            fallback_reason=(
                str(semantic_state.get("message") or "") or None
                if mode != "textual"
                else None
            ),
            field_scope=field_scope,
            total_documents=total_documents,
            total_fragments=total_fragments,
            totals_exact=True,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
            has_next=page < total_pages,
        )

    # Recupera un pool global estable y pagina por documento después de
    # fusionar las señales. Su límite se expone al consumidor.
    response = search_corpus(
        database_path,
        semantic_index_path,
        query,
        mode=mode,
        top_k=candidate_limit,
        filters=filters,
        exact_phrase=exact_phrase,
        field_scope=field_scope,
        ann_index_path=ann_index_path,
    )
    groups = _group_for_pagination(response.results, order=order)
    total_documents = len(groups)
    total_fragments = sum(len(group) for group in groups)
    total_pages = max(1, math.ceil(total_documents / page_size))
    start = (page - 1) * page_size
    selected_groups = groups[start : start + page_size]
    visible = [
        result
        for group in selected_groups
        for result in group[:fragments_per_document]
    ]
    # Frase/página y campos se confirman exhaustivamente hasta el límite del
    # pool; ANN/híbrido es aproximado por definición. Nunca se vende el límite
    # como un total global exacto.
    pool_exhausted = len(response.results) < candidate_limit
    exact_totals = (
        response.used_mode == "textual"
        and pool_exhausted
        and (exact_phrase or field_scope != "all")
    )
    return replace(
        response,
        results=visible,
        candidate_count=(response.candidate_count or len(response.results)),
        total_documents=total_documents,
        total_fragments=total_fragments,
        totals_exact=exact_totals,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
        has_next=page < total_pages,
    )
