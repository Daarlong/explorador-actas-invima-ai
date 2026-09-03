from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import streamlit as st

from config import (
    ALLOWED_DOCUMENT_HOSTS,
    DATABASE_PATH,
    MAX_PDF_BYTES,
    PDF_CACHE_DIR,
    REVIEW_LOG_PATH,
    SEMANTIC_INDEX_PATH,
)
from services.database import (
    database_stats,
    get_filter_options,
    get_regulatory_records_by_uids,
    regulatory_records_for_chunks,
    table_exists,
)
from services.effective_records import (
    associate_effective_records_to_chunks,
    display_value,
    evidence_results_for_records,
    evidence_selection_payload,
    effective_record_matches,
    field_evidence_details,
    field_metadata,
    hydrate_field_evidence,
    matching_review_uids,
    provenance_label,
)
from services.models import SearchResult
from services.pdf_viewer import render_pdf_page
from services.reviews import apply_latest_reviews, latest_reviews, load_review_events
from services.search import SearchResponse, search_corpus
from services.ui_helpers import group_search_results, highlight_query, pdf_page_url


st.set_page_config(page_title="Explorador de actas", page_icon="🔍", layout="wide")
st.title("🔍 Explorador de actas")
st.caption(
    "Búsqueda híbrida, resultados agrupados y verificación en la página exacta"
)


def _file_identity(path: Path) -> tuple[int, int]:
    if not path.exists():
        return (0, 0)
    stat = path.stat()
    return (stat.st_size, stat.st_mtime_ns)


@st.cache_data(show_spinner=False, ttl=3600)
def cached_search(
    query: str,
    mode: str,
    exact_phrase: bool,
    filters_json: str,
    database_identity: tuple[int, int],
    semantic_identity: tuple[int, int],
) -> SearchResponse:
    del database_identity, semantic_identity
    return search_corpus(
        DATABASE_PATH,
        SEMANTIC_INDEX_PATH,
        query,
        mode=mode,
        exact_phrase=exact_phrase,
        top_k=360,
        filters=json.loads(filters_json),
    )


@st.cache_data(show_spinner=False, ttl=3600)
def cached_pdf_page(title: str, url: str, page: int, dpi: int = 125):
    return render_pdf_page(
        title,
        url,
        page,
        PDF_CACHE_DIR,
        ALLOWED_DOCUMENT_HOSTS,
        MAX_PDF_BYTES,
        dpi=dpi,
    )


def clear_selected_evidence() -> None:
    st.session_state["selected_evidence"] = {}
    st.session_state.pop("comparison_selected_sources", None)
    st.session_state.pop("analysis_selected_sources", None)
    for key in list(st.session_state):
        if str(key).startswith(("select_evidence_", "select_reviewed_")):
            del st.session_state[key]


def update_selected_evidence(
    widget_key: str, result: dict, selection_id: str | None = None
) -> None:
    selected: dict[str, dict] = st.session_state.setdefault(
        "selected_evidence",
        {},
    )
    selected_key = selection_id or str(result["chunk_id"])
    if st.session_state.get(widget_key):
        selected[selected_key] = result
    else:
        selected.pop(selected_key, None)
    # Cualquier cambio invalida los snapshots enviados previamente a otras páginas.
    st.session_state.pop("comparison_selected_sources", None)
    st.session_state.pop("analysis_selected_sources", None)


def hydrate_effective_groups(
    records_by_chunk: dict[int, list[dict]],
    review_events: list,
    chunk_context: dict[int, dict],
) -> dict[int, list[dict]]:
    """Hidrata evidencias en un solo acceso SQLite y conserva asociaciones."""

    def record_identity(record: dict) -> tuple[str, object]:
        if record.get("record_id") is not None:
            return ("id", record.get("record_id"))
        return ("uid", record.get("decision_uid"))

    unique: dict[tuple[str, object], dict] = {}
    for records in records_by_chunk.values():
        for record in records:
            unique[record_identity(record)] = record
    page_review_uids = {
        uid
        for uid, event in latest_reviews(review_events).items()
        if any(
            field in (event.corrections or {})
            for field in ("page_number", "end_page_number")
        )
    }
    page_review_records = get_regulatory_records_by_uids(
        DATABASE_PATH, sorted(page_review_uids)
    )
    for record in page_review_records:
        unique[record_identity(record)] = record
    hydrated = hydrate_field_evidence(DATABASE_PATH, unique.values())
    effective = apply_latest_reviews(hydrated, review_events)
    effective_by_key = {record_identity(record): record for record in effective}
    initial_effective = {
        chunk_id: [
            effective_by_key[record_identity(raw)]
            for raw in records
            if record_identity(raw) in effective_by_key
        ]
        for chunk_id, records in records_by_chunk.items()
    }
    supplemental_effective = [
        effective_by_key[record_identity(record)]
        for record in page_review_records
        if record_identity(record) in effective_by_key
    ]
    return associate_effective_records_to_chunks(
        initial_effective,
        chunk_context,
        supplemental_effective,
    )


stats = database_stats(DATABASE_PATH)
if stats["documents"] == 0:
    st.warning(
        "No existe un índice. Ejecuta primero Construir índice desde GitHub Actions."
    )
    st.stop()

has_structured_data = table_exists(DATABASE_PATH, "regulatory_records")
try:
    review_events = load_review_events(REVIEW_LOG_PATH)
except ValueError as exc:
    st.error(
        "El registro de revisiones no es válido. Se detuvo la consulta para "
        f"no mostrar valores automáticos como si fueran vigentes: {exc}"
    )
    st.stop()
options = get_filter_options(DATABASE_PATH, review_events=review_events)
selected_evidence: dict[str, dict] = st.session_state.setdefault(
    "selected_evidence",
    {},
)

mode_labels = {
    "Híbrida (recomendada)": "hybrid",
    "Textual FTS5": "textual",
    "Semántica local": "semantic",
}
order_labels = {
    "Mayor relevancia": "relevance",
    "Más recientes": "newest",
    "Más antiguas": "oldest",
}
outcome_labels = {
    "aprobado": "Aprobado",
    "negado": "Negado",
    "requerido": "Requerimiento",
    "desistido": "Desistido",
    "archivado": "Archivado",
    "favorable": "Favorable",
    "no_favorable": "No favorable",
    "sin_clasificar": "Sin clasificar",
}
review_labels = {
    "automatic": "Automática",
    "reviewed": "Revisada",
    "approved": "Aprobada",
    "reopened": "Reabierta",
    "stale": "Revisión desactualizada",
}
provenance_options = {
    "Explícito": "explicit",
    "Inferido": "inferred",
    "Mixto": "mixed",
    "Verificado": "verified",
    "Automático anterior": "legacy_automatic",
    "No extraído": "not_extracted",
}

with st.sidebar:
    st.header("Buscar y filtrar")
    selected_mode_label = st.radio(
        "Tipo de búsqueda",
        list(mode_labels),
        help=(
            "La búsqueda híbrida combina FTS5 con relaciones aprendidas del "
            "propio corpus, sin enviar información a servicios externos."
        ),
    )
    exact_phrase = st.checkbox(
        "Exigir frase completa",
        help="Utiliza únicamente coincidencias textuales de la frase en ese orden.",
    )
    years = st.multiselect("Año", options["years"])
    acta_numbers = st.multiselect("Número de acta", options["acta_numbers"])
    sections = st.multiselect("Sala o sección", options["sections"])
    parts = st.multiselect("Parte", options["parts"])
    outcomes: list[str] = []
    request_types: list[str] = []
    selected_provenance_labels: list[str] = []
    selected_review_labels: list[str] = []
    missing_active_ingredient = False
    confidence_minimum: float | None = None
    product = active_ingredient = interested_party = identifier = ""
    if has_structured_data:
        with st.expander("Campos regulatorios", expanded=False):
            outcomes = st.multiselect(
                "Resultado extraído",
                options.get("outcomes", []),
                format_func=lambda value: outcome_labels.get(value, value),
            )
            request_types = st.multiselect(
                "Tipo de solicitud",
                options.get("request_types", []),
                format_func=lambda value: str(value).replace("_", " ").capitalize(),
            )
            product = st.text_input("Producto")
            active_ingredient = st.text_input("Principio activo")
            interested_party = st.text_input("Interesado o titular")
            identifier = st.text_input("Expediente o radicado")
            missing_active_ingredient = st.checkbox(
                "Solo fichas sin principio activo vigente"
            )
            selected_provenance_labels = st.multiselect(
                "Procedencia del principio activo",
                list(provenance_options),
            )
            selected_review_labels = st.multiselect(
                "Estado de revisión",
                list(review_labels.values()),
            )
            confidence_choice = st.selectbox(
                "Confianza mínima del principio activo",
                ["Cualquiera", "Media (≥ 60 %)", "Alta (≥ 85 %)"],
            )
            confidence_minimum = {
                "Cualquiera": None,
                "Media (≥ 60 %)": 0.60,
                "Alta (≥ 85 %)": 0.85,
            }[confidence_choice]
            st.caption(
                "Los campos son extraídos automáticamente y deben verificarse "
                "contra el acta."
            )
    selected_order_label = st.selectbox("Orden", list(order_labels))
    page_size = st.select_slider("Actas por página", [5, 10, 15, 20], value=10)

    st.divider()
    st.metric("Evidencias seleccionadas", len(selected_evidence))
    analyze_disabled = not selected_evidence
    if st.button(
        "Analizar seleccionadas",
        type="primary",
        use_container_width=True,
        disabled=analyze_disabled,
    ):
        st.session_state["analysis_selected_sources"] = list(
            selected_evidence.values()
        )
        st.session_state["analysis_use_selected"] = True
        st.switch_page("pages/2_Analista_IA.py")
    if st.button(
        "Comparar seleccionadas",
        use_container_width=True,
        disabled=analyze_disabled,
    ):
        st.session_state["comparison_selected_sources"] = list(
            selected_evidence.values()
        )
        st.switch_page("pages/7_Comparar.py")
    if st.button(
        "Limpiar selección",
        use_container_width=True,
        disabled=analyze_disabled,
    ):
        clear_selected_evidence()
        st.rerun()

query = st.text_input(
    "¿Qué necesitas encontrar?",
    key="explorer_query",
    placeholder=(
        "Ej.: precedentes de semaglutida, expediente 123456 o requerimientos "
        "sobre estabilidad"
    ),
)

if not query.strip():
    st.info(
        "Escribe una consulta. Puedes combinarla con año, sala, producto, "
        "principio activo, interesado, expediente o resultado."
    )
    st.stop()

filters = {
    "years": years,
    "acta_numbers": acta_numbers,
    "sections": sections,
    "parts": parts,
    "outcomes": outcomes,
    "request_types": request_types,
    "products": [product] if product else [],
    "active_ingredients": [active_ingredient] if active_ingredient else [],
    "interested_parties": [interested_party] if interested_party else [],
    "identifiers": [identifier] if identifier else [],
}
effective_filter_values = {
    "products": [product] if product else [],
    "active_ingredients": [active_ingredient] if active_ingredient else [],
    "interested_parties": [interested_party] if interested_party else [],
    "identifiers": [identifier] if identifier else [],
    "outcomes": outcomes,
    "request_types": request_types,
    "missing_field": "active_ingredient" if missing_active_ingredient else "",
    "provenances": [
        provenance_options[label] for label in selected_provenance_labels
    ],
    "confidence_minimum": confidence_minimum,
    "review_statuses": [
        code
        for code, label in review_labels.items()
        if label in selected_review_labels
    ],
}
# Los filtros estructurados se aplican sobre el valor vigente cuando hay
# revisiones o filtros de procedencia. Así una corrección humana es visible sin
# reconstruir FTS. Año/sala/acta siguen resolviéndose eficientemente en SQLite.
post_filter_effective = bool(
    any(
        (
            missing_active_ingredient,
            selected_provenance_labels,
            selected_review_labels,
            confidence_minimum is not None,
        )
    )
    or (
        review_events
        and any(
            (
                product,
                active_ingredient,
                interested_party,
                identifier,
                outcomes,
                request_types,
            )
        )
    )
)
search_filters = dict(filters)
if post_filter_effective:
    for name in (
        "outcomes",
        "request_types",
        "products",
        "active_ingredients",
        "interested_parties",
        "identifiers",
    ):
        search_filters[name] = []
signature = hashlib.sha256(
    json.dumps(
        [
            query,
            selected_mode_label,
            exact_phrase,
            filters,
            effective_filter_values,
            selected_order_label,
        ],
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
).hexdigest()[:12]
if st.session_state.get("explorer_search_signature") != signature:
    st.session_state["explorer_search_signature"] = signature
    st.session_state["explorer_page"] = 1

with st.spinner("Consultando el corpus..."):
    try:
        response = cached_search(
            query.strip(),
            mode_labels[selected_mode_label],
            exact_phrase,
            json.dumps(search_filters, ensure_ascii=False, sort_keys=True),
            _file_identity(DATABASE_PATH),
            _file_identity(SEMANTIC_INDEX_PATH),
        )
        # Conserva también los candidatos que coinciden con los valores
        # automáticos. El barrido sin filtros permite encontrar correcciones;
        # esta segunda consulta evita perder fichas automáticas más profundas
        # en el ranking por el límite de recuperación.
        if post_filter_effective and search_filters != filters:
            automatic_response = cached_search(
                query.strip(),
                mode_labels[selected_mode_label],
                exact_phrase,
                json.dumps(filters, ensure_ascii=False, sort_keys=True),
                _file_identity(DATABASE_PATH),
                _file_identity(SEMANTIC_INDEX_PATH),
            )
            candidates = {result.chunk_id: result for result in response.results}
            for result in automatic_response.results:
                current = candidates.get(result.chunk_id)
                if current is None or result.score > current.score:
                    candidates[result.chunk_id] = result
            response = SearchResponse(
                results=sorted(
                    candidates.values(), key=lambda item: (-item.score, item.chunk_id)
                ),
                requested_mode=response.requested_mode,
                used_mode=response.used_mode,
                semantic_available=response.semantic_available,
                semantic_message=response.semantic_message,
            )
    except Exception as exc:
        st.error(f"No fue posible completar la búsqueda: {exc}")
        st.stop()

all_structured_by_chunk: dict[int, list[dict]] = {}
if post_filter_effective and response.results:
    raw_by_chunk = regulatory_records_for_chunks(
        DATABASE_PATH, [result.chunk_id for result in response.results]
    )
    all_structured_by_chunk = hydrate_effective_groups(
        raw_by_chunk,
        review_events,
        {
            result.chunk_id: {"page": result.page, "url": result.url}
            for result in response.results
        },
    )
    response = SearchResponse(
        results=[
            result
            for result in response.results
            if any(
                effective_record_matches(record, **effective_filter_values)
                for record in all_structured_by_chunk.get(result.chunk_id, [])
            )
        ],
        requested_mode=response.requested_mode,
        used_mode=response.used_mode,
        semantic_available=response.semantic_available,
        semantic_message=response.semantic_message,
    )

if exact_phrase and response.requested_mode != response.used_mode:
    st.info(
        "La opción de frase completa utiliza búsqueda textual para respetar "
        "el orden exacto de las palabras."
    )
elif response.requested_mode != response.used_mode:
    st.warning(
        "El índice semántico todavía no está disponible; esta consulta se "
        "resolvió con búsqueda textual. Ejecuta Construir índice para generar "
        "o actualizar el índice semántico local."
    )

corrected_records: list[dict] = []
corrected_uids = matching_review_uids(review_events, query)
if review_events and any(
    (
        product,
        active_ingredient,
        interested_party,
        identifier,
        outcomes,
        request_types,
        missing_active_ingredient,
        selected_provenance_labels,
        selected_review_labels,
        confidence_minimum is not None,
    )
):
    # Una corrección puede cambiar el resultado de cualquiera de estos filtros
    # aunque el texto de la consulta coincida con solicitud/concepto.
    corrected_uids.update(latest_reviews(review_events))
review_candidate_truncated = len(corrected_uids) > 5_000
review_match_truncated = False
if corrected_uids:
    review_candidate_uids = sorted(corrected_uids)[:5_000]
    for start_uid in range(0, len(review_candidate_uids), 500):
        uid_batch = review_candidate_uids[start_uid : start_uid + 500]
        corrected_raw = get_regulatory_records_by_uids(DATABASE_PATH, uid_batch)
        corrected_effective = apply_latest_reviews(
            hydrate_field_evidence(DATABASE_PATH, corrected_raw), review_events
        )
        corrected_records.extend(
            record
            for record in corrected_effective
            if effective_record_matches(
                record,
                query=query,
                years=years,
                **effective_filter_values,
            )
        )
        if len(corrected_records) > 500:
            corrected_records = corrected_records[:500]
            review_match_truncated = True
            break

# Evita mostrar dos veces una ficha que ya está vinculada a un resultado FTS.
if corrected_records and response.results and not all_structured_by_chunk:
    raw_for_dedup = regulatory_records_for_chunks(
        DATABASE_PATH, [result.chunk_id for result in response.results]
    )
    all_structured_by_chunk = hydrate_effective_groups(
        raw_for_dedup,
        review_events,
        {
            result.chunk_id: {"page": result.page, "url": result.url}
            for result in response.results
        },
    )
regular_uids = {
    str(record.get("decision_uid") or "")
    for records in all_structured_by_chunk.values()
    for record in records
}
corrected_records = [
    record
    for record in corrected_records
    if str(record.get("decision_uid") or "") not in regular_uids
]

groups = group_search_results(response.results, order=order_labels[selected_order_label])
if not groups and not corrected_records:
    st.warning("No se encontraron coincidencias con los filtros seleccionados.")
    st.stop()

total_pages = max(1, math.ceil(len(groups) / page_size))
current_page = min(int(st.session_state.get("explorer_page", 1)), total_pages)
st.session_state["explorer_page"] = current_page
start = (current_page - 1) * page_size
visible_groups = groups[start : start + page_size]
visible_results = [
    result for group in visible_groups for result in group["results"][:3]
]
if all_structured_by_chunk:
    structured_by_chunk = {
        result.chunk_id: all_structured_by_chunk.get(result.chunk_id, [])
        for result in visible_results
    }
else:
    structured_by_chunk = regulatory_records_for_chunks(
        DATABASE_PATH,
        [result.chunk_id for result in visible_results],
    )
    structured_by_chunk = hydrate_effective_groups(
        structured_by_chunk,
        review_events,
        {
            result.chunk_id: {"page": result.page, "url": result.url}
            for result in visible_results
        },
    )

summary_col1, summary_col2, summary_col3 = st.columns([1.2, 1, 1])
summary_col1.metric("PDF recuperados", len(groups))
summary_col2.metric("Fragmentos recuperados", len(response.results))
summary_col3.metric("Página de resultados", f"{current_page} de {total_pages}")
st.caption(
    f"Modo utilizado: **{response.used_mode}** · se muestran hasta tres "
    "fragmentos por documento."
)

if corrected_records:
    with st.expander(
        f"Coincidencias con historial de revisión ({len(corrected_records)})",
        expanded=not groups,
    ):
        st.caption(
            "Estas fichas se recuperaron mediante su historial y se muestran "
            "después de validar cuál revisión sigue vigente."
        )
        if review_candidate_truncated or review_match_truncated:
            st.warning(
                "Se alcanzó el límite de seguridad del historial. Acota la "
                "consulta o los filtros para revisar todas las coincidencias."
            )
        corrected_page_size = 20
        corrected_page_count = max(
            1, math.ceil(len(corrected_records) / corrected_page_size)
        )
        corrected_page = st.number_input(
            "Página de coincidencias revisadas",
            min_value=1,
            max_value=corrected_page_count,
            value=1,
            step=1,
            key=f"corrected_page_{signature}",
        )
        corrected_start = (int(corrected_page) - 1) * corrected_page_size
        corrected_visible = corrected_records[
            corrected_start : corrected_start + corrected_page_size
        ]
        selectable_by_uid, evidence_truncated = evidence_results_for_records(
            DATABASE_PATH, corrected_visible, maximum=corrected_page_size
        )
        if evidence_truncated:
            st.warning("Algunas evidencias no se cargaron por el límite de seguridad.")
        for corrected_index, record in enumerate(
            corrected_visible, start=corrected_start + 1
        ):
            st.markdown(
                f"**{display_value(record.get('product_name'))}** · "
                f"{display_value(record.get('active_ingredient'))} · "
                f"Acta {display_value(record.get('acta_number'))} "
                f"({display_value(record.get('year'))})"
            )
            uid = str(record.get("decision_uid") or "")
            selectable = selectable_by_uid.get(uid)
            controls = st.columns([1.1, 1, 1, 1])
            if selectable is not None:
                selectable_payload = evidence_selection_payload(selectable, uid)
                selected_key = f"review:{uid}:{selectable.chunk_id}"
                selection_key = f"select_reviewed_{signature}_{uid}"
                controls[0].checkbox(
                    "Seleccionar",
                    value=selected_key in selected_evidence,
                    key=selection_key,
                    on_change=update_selected_evidence,
                    args=(selection_key, selectable_payload, selected_key),
                )
                if controls[1].button(
                    "Ver página",
                    key=f"view_reviewed_{signature}_{uid}",
                    use_container_width=True,
                ):
                    st.session_state["viewer_source"] = selectable_payload
                    st.session_state["viewer_page"] = selectable.page
                reviewed_url = pdf_page_url(
                    selectable.url, selectable.page, ALLOWED_DOCUMENT_HOSTS
                )
                if reviewed_url:
                    controls[2].link_button(
                        "Abrir PDF",
                        reviewed_url,
                        key=f"open_reviewed_{signature}_{uid}",
                        use_container_width=True,
                    )
            else:
                controls[0].caption("Sin fragmento seleccionable")
            if controls[3].button(
                "Revisar ficha",
                key=f"review_corrected_{signature}_{uid or corrected_index}",
                use_container_width=True,
            ):
                st.session_state["review_target_uid"] = record.get("decision_uid")
                st.switch_page("pages/8_Revision_Fichas.py")

if groups:
    nav_left, nav_middle, nav_right = st.columns([1, 2, 1])
    if nav_left.button(
        "← Anterior",
        disabled=current_page <= 1,
        use_container_width=True,
    ):
        st.session_state["explorer_page"] = current_page - 1
        st.rerun()
    nav_middle.caption(
        f"Resultados {start + 1}–{min(start + page_size, len(groups))}"
    )
    if nav_right.button(
        "Siguiente →",
        disabled=current_page >= total_pages,
        use_container_width=True,
    ):
        st.session_state["explorer_page"] = current_page + 1
        st.rerun()

results_column, viewer_column = st.columns([1.45, 1], gap="large")
with results_column:
    for group_index, group in enumerate(visible_groups, start=start + 1):
        with st.container(border=True):
            st.markdown(f"### {group_index}. {group['title']}")
            metadata = []
            if group["year"]:
                metadata.append(str(group["year"]))
            if group["section"]:
                metadata.append(group["section"])
            if group["part"]:
                metadata.append(group["part"])
            if group["source_type"] == "historical_mirror":
                metadata.append("Copia histórica")
            metadata.append(f"relevancia {group['score']:.3f}")
            st.caption(" · ".join(metadata))

            for fragment_index, result in enumerate(group["results"][:3], start=1):
                st.markdown(f"**Fragmento {fragment_index} · página {result.page}**")
                st.markdown(highlight_query(result.text, query), unsafe_allow_html=True)
                controls = st.columns([1.2, 1, 1])
                selection_key = f"select_evidence_{signature}_{result.chunk_id}"
                controls[0].checkbox(
                    "Seleccionar",
                    value=str(result.chunk_id) in selected_evidence,
                    key=selection_key,
                    on_change=update_selected_evidence,
                    args=(selection_key, result.as_dict()),
                )
                if controls[1].button(
                    "Ver página",
                    key=f"view_{signature}_{result.chunk_id}",
                    use_container_width=True,
                ):
                    st.session_state["viewer_source"] = result.as_dict()
                    st.session_state["viewer_page"] = result.page
                direct_url = pdf_page_url(
                    result.url,
                    result.page,
                    ALLOWED_DOCUMENT_HOSTS,
                )
                if direct_url:
                    controls[2].link_button(
                        f"Abrir PDF · F{fragment_index}",
                        direct_url,
                        use_container_width=True,
                    )
                else:
                    controls[2].caption("Enlace no disponible")

                structured = structured_by_chunk.get(result.chunk_id, [])
                for record_index, record in enumerate(structured, start=1):
                    ficha_label = "Ficha regulatoria"
                    if len(structured) > 1:
                        ficha_label += f" {record_index} de {len(structured)}"
                    ficha_label += " · " + review_labels.get(
                        str(record.get("review_status") or "automatic"),
                        "Automática",
                    )
                    with st.expander(ficha_label, expanded=False):
                        field_rows = {
                            "Numeral": record.get("numeral"),
                            "Título del numeral": record.get("numeral_title"),
                            "Fecha de sesión": record.get("session_date"),
                            "Tipo de solicitud": record.get("request_type_code"),
                            "Producto": record.get("product_name"),
                            "Principio activo": record.get("active_ingredient"),
                            "Interesado": record.get("interested_party"),
                            "Expediente": record.get("expediente"),
                            "Radicado": record.get("radicado"),
                            "Resultado": outcome_labels.get(
                                record.get("outcome_code"),
                                record.get("outcome_code"),
                            ),
                        }
                        field_names = {
                            "Numeral": "numeral",
                            "Título del numeral": "numeral_title",
                            "Fecha de sesión": "session_date",
                            "Tipo de solicitud": "request_type_code",
                            "Producto": "product_name",
                            "Principio activo": "active_ingredient",
                            "Interesado": "interested_party",
                            "Expediente": "expediente",
                            "Radicado": "radicado",
                            "Resultado": "outcome_code",
                        }
                        table_rows = []
                        for label, value in field_rows.items():
                            meta = field_metadata(record, field_names[label])
                            confidence = meta.get("confidence")
                            table_rows.append(
                                {
                                    "Campo": label,
                                    "Valor vigente": display_value(value),
                                    "Procedencia": provenance_label(
                                        meta.get("provenance")
                                    ),
                                    "Confianza": (
                                        f"{float(confidence):.0%}"
                                        if confidence is not None
                                        else "No disponible"
                                    ),
                                }
                            )
                        st.dataframe(
                            table_rows,
                            hide_index=True,
                            use_container_width=True,
                        )
                        evidence_labels = {
                            "numeral": "Numeral",
                            "titulo_numeral": "Título del numeral",
                            "numeral_title": "Título del numeral",
                            "fecha_sesion": "Fecha de sesión",
                            "session_date": "Fecha de sesión",
                            "fecha_sesion_original": "Fecha de sesión (literal original)",
                            "tipo_solicitud": "Tipo de solicitud",
                            "request_type_code": "Tipo de solicitud",
                            "producto": "Producto",
                            "product_name": "Producto",
                            "principio_activo": "Principio activo",
                            "active_ingredient": "Principio activo",
                            "interesado": "Interesado",
                            "interested_party": "Interesado",
                            "expediente": "Expediente",
                            "radicado": "Radicado",
                            "solicitud": "Solicitud",
                            "request_text": "Solicitud",
                            "concepto": "Concepto",
                            "concept_text": "Concepto",
                            "resultado_normalizado": "Resultado normalizado",
                            "outcome_code": "Resultado normalizado",
                            "rango_paginas": "Rango de páginas",
                            "page_number": "Página inicial",
                            "end_page_number": "Página final",
                        }
                        evidence_rows = []
                        seen_evidence: set[tuple] = set()
                        for detail in field_evidence_details(record):
                            source_field = str(
                                detail.get("source_field") or detail.get("field")
                            )
                            # Una evidencia de rango sustenta dos campos vigentes;
                            # se presenta una sola vez para no duplicar la fila.
                            evidence_key = (
                                source_field,
                                detail.get("ordinal"),
                                detail.get("literal_value"),
                                detail.get("page"),
                                detail.get("end_page"),
                                detail.get("method"),
                            )
                            if evidence_key in seen_evidence:
                                continue
                            seen_evidence.add(evidence_key)
                            start = detail.get("page")
                            end = detail.get("end_page") or start
                            page_range = (
                                start
                                if start == end or end is None
                                else f"{start}–{end}"
                            )
                            evidence_rows.append(
                                {
                                    "Campo": evidence_labels.get(
                                        source_field, source_field
                                    ),
                                    "Valor literal": display_value(
                                        detail.get("literal_value")
                                    ),
                                    "Normalizado": display_value(
                                        detail.get("normalized_value")
                                    ),
                                    "Canónico": display_value(
                                        detail.get("canonical_value")
                                    ),
                                    "Página(s)": display_value(page_range),
                                    "Método": display_value(detail.get("method")),
                                    "Confianza": (
                                        f"{float(detail['confidence']):.0%}"
                                        if detail.get("confidence") is not None
                                        else "No disponible"
                                    ),
                                    "Evidencia": display_value(
                                        detail.get("fragment")
                                    ),
                                }
                            )
                        if evidence_rows:
                            with st.expander("Procedencia de los campos"):
                                st.dataframe(
                                    evidence_rows,
                                    hide_index=True,
                                    use_container_width=True,
                                )
                        st.markdown("**Solicitud**")
                        st.write(display_value(record.get("request_text")))
                        st.markdown("**Concepto**")
                        st.write(display_value(record.get("concept_text")))
                        start_page = record.get("page_number")
                        end_page = record.get("end_page_number") or start_page
                        page_label = (
                            str(start_page)
                            if start_page == end_page
                            else f"{start_page}–{end_page}"
                        )
                        st.caption(
                            f"Estado: {review_labels.get(str(record.get('review_status') or 'automatic'), 'Automática')} "
                            f"· páginas {page_label}."
                        )
                        if record.get("review_stale"):
                            st.warning(
                                "La fuente cambió después de la revisión; esta ficha "
                                "debe verificarse nuevamente."
                            )
                        elif record.get("review_needs_reconfirmation"):
                            st.warning(
                                "La versión del extractor cambió. La corrección "
                                "se conserva por su ID estable, pero conviene "
                                "reconfirmarla contra la fuente."
                            )
                        if record.get("reviewer"):
                            st.caption(
                                f"Revisor declarado: {record.get('reviewer')} · "
                                f"{record.get('reviewed_at', '')}"
                            )
                        if st.button(
                            "Revisar esta ficha",
                            key=(
                                f"review_record_{signature}_{result.chunk_id}_"
                                f"{record.get('decision_uid') or record_index}"
                            ),
                        ):
                            st.session_state["review_target_uid"] = record.get(
                                "decision_uid"
                            )
                            st.switch_page("pages/8_Revision_Fichas.py")
                if fragment_index < min(3, len(group["results"])):
                    st.divider()

with viewer_column:
    st.subheader("Visor de evidencia")
    viewer_source = st.session_state.get("viewer_source")
    if not viewer_source:
        st.info("Selecciona **Ver página** en un resultado para visualizarla aquí.")
    else:
        source = SearchResult.from_dict(viewer_source)
        page_number = int(st.session_state.get("viewer_page", source.page))
        st.markdown(f"**{source.title}**")
        st.caption(f"Página solicitada: {page_number}")
        try:
            with st.spinner("Cargando la página del documento..."):
                rendered = cached_pdf_page(source.title, source.url, page_number)
            st.image(
                rendered.image_bytes,
                caption=f"Página {rendered.page_number} de {rendered.page_count}",
                use_container_width=True,
            )
            previous, following = st.columns(2)
            if previous.button(
                "← Página anterior",
                disabled=page_number <= 1,
                use_container_width=True,
            ):
                st.session_state["viewer_page"] = page_number - 1
                st.rerun()
            if following.button(
                "Página siguiente →",
                disabled=page_number >= rendered.page_count,
                use_container_width=True,
            ):
                st.session_state["viewer_page"] = page_number + 1
                st.rerun()
            external_url = pdf_page_url(
                rendered.resolved_url,
                page_number,
                ALLOWED_DOCUMENT_HOSTS,
            )
            st.link_button(
                "Abrir documento completo",
                external_url,
                use_container_width=True,
            )
        except Exception as exc:
            st.error(f"No fue posible renderizar esta página: {exc}")
            fallback_url = pdf_page_url(
                source.url,
                page_number,
                ALLOWED_DOCUMENT_HOSTS,
            )
            if fallback_url:
                st.link_button("Abrir documento externamente", fallback_url)
        if st.button("Cerrar visor", use_container_width=True):
            st.session_state.pop("viewer_source", None)
            st.session_state.pop("viewer_page", None)
            st.rerun()
