from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path

from services.database import (
    chunk_ids_for_filters,
    get_chunks_by_ids,
    search_chunks,
)
from services.models import SearchResult
from services.semantic import (
    reciprocal_rank_fusion,
    semantic_index_status,
    semantic_search,
)
from services.text_utils import normalize_text


SEARCH_MODES = ("hybrid", "textual", "semantic")


@dataclass(frozen=True)
class SearchResponse:
    results: list[SearchResult]
    requested_mode: str
    used_mode: str
    semantic_available: bool
    semantic_message: str


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


def search_corpus(
    database_path: Path,
    semantic_index_path: Path,
    query: str,
    *,
    mode: str = "hybrid",
    top_k: int = 200,
    filters: dict[str, list] | None = None,
    exact_phrase: bool = False,
) -> SearchResponse:
    if mode not in SEARCH_MODES:
        raise ValueError(f"Modo de búsqueda desconocido: {mode}")
    if top_k < 1:
        raise ValueError("top_k debe ser mayor que cero")

    # Un semantic.db válido pero construido para otra actas.db es más
    # peligroso que no tenerlo: los IDs hidratarían fragmentos equivocados.
    semantic_state = semantic_index_status(
        semantic_index_path,
        database_path,
    )
    semantic_available = bool(semantic_state.get("available"))
    semantic_message = str(semantic_state.get("message", ""))
    used_mode = mode
    if exact_phrase:
        used_mode = "textual"
    if mode in {"hybrid", "semantic"} and not semantic_available:
        used_mode = "textual"

    candidate_limit = max(top_k * 3, 120)
    max_per_document = 5 if top_k <= 20 else 3
    lexical_results: list[SearchResult] = []
    if used_mode in {"textual", "hybrid"}:
        lexical_results = search_chunks(
            database_path,
            query,
            top_k=candidate_limit,
            filters=filters,
            exact_phrase=exact_phrase,
            max_chunks_per_document=max_per_document,
        )
    if used_mode == "textual":
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
        )

    if used_mode == "hybrid" and lexical_results:
        # La modalidad recomendada reordena candidatos de FTS5 para evitar un
        # barrido de todos los vectores en cada pulsación. Si FTS5 no recupera
        # nada, sí se permite la recuperación semántica global como respaldo.
        allowed_ids = [result.chunk_id for result in lexical_results]
    else:
        allowed_ids = chunk_ids_for_filters(database_path, filters)
    semantic_hits = semantic_search(
        semantic_index_path,
        query,
        top_k=candidate_limit,
        allowed_ids=allowed_ids,
    )
    semantic_scores = dict(semantic_hits)

    if used_mode == "semantic":
        semantic_results = get_chunks_by_ids(
            database_path,
            [chunk_id for chunk_id, _ in semantic_hits],
            filters=filters,
            scores=semantic_scores,
        )
        return SearchResponse(
            results=_diversify_results(
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
            ),
            requested_mode=mode,
            used_mode="semantic",
            semantic_available=True,
            semantic_message=semantic_message,
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
    hydrated = get_chunks_by_ids(
        database_path,
        [item.item_id for item in fused],
        filters=filters,
        scores=fused_scores,
    )
    combined = [
        replace(
            result,
            score=round(
                result.score + _exact_identifier_bonus(query, result),
                6,
            ),
            lexical_score=lexical_normalized.get(result.chunk_id),
            semantic_score=semantic_normalized.get(result.chunk_id),
            match_type="hybrid",
        )
        for result in hydrated
    ]
    combined.sort(key=lambda result: (-result.score, result.chunk_id))
    return SearchResponse(
        results=_diversify_results(
            combined,
            top_k=top_k,
            max_per_document=max_per_document,
        ),
        requested_mode=mode,
        used_mode="hybrid",
        semantic_available=True,
        semantic_message=semantic_message,
    )
