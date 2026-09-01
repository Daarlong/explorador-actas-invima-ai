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
    SEMANTIC_INDEX_PATH,
)
from services.database import (
    database_stats,
    get_filter_options,
    regulatory_records_for_chunks,
    table_exists,
)
from services.models import SearchResult
from services.pdf_viewer import render_pdf_page
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
    for key in list(st.session_state):
        if str(key).startswith("select_evidence_"):
            del st.session_state[key]


def update_selected_evidence(widget_key: str, result: dict) -> None:
    selected: dict[str, dict] = st.session_state.setdefault(
        "selected_evidence",
        {},
    )
    chunk_id = str(result["chunk_id"])
    if st.session_state.get(widget_key):
        selected[chunk_id] = result
    else:
        selected.pop(chunk_id, None)


stats = database_stats(DATABASE_PATH)
if stats["documents"] == 0:
    st.warning(
        "No existe un índice. Ejecuta primero Construir índice desde GitHub Actions."
    )
    st.stop()

options = get_filter_options(DATABASE_PATH)
has_structured_data = table_exists(DATABASE_PATH, "regulatory_records")
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
    product = active_ingredient = interested_party = identifier = ""
    if has_structured_data:
        with st.expander("Campos regulatorios", expanded=False):
            outcomes = st.multiselect(
                "Resultado extraído",
                options.get("outcomes", []),
                format_func=lambda value: outcome_labels.get(value, value),
            )
            product = st.text_input("Producto")
            active_ingredient = st.text_input("Principio activo")
            interested_party = st.text_input("Interesado o titular")
            identifier = st.text_input("Expediente o radicado")
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
    "products": [product] if product else [],
    "active_ingredients": [active_ingredient] if active_ingredient else [],
    "interested_parties": [interested_party] if interested_party else [],
    "identifiers": [identifier] if identifier else [],
}
signature = hashlib.sha256(
    json.dumps(
        [query, selected_mode_label, exact_phrase, filters, selected_order_label],
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
            json.dumps(filters, ensure_ascii=False, sort_keys=True),
            _file_identity(DATABASE_PATH),
            _file_identity(SEMANTIC_INDEX_PATH),
        )
    except Exception as exc:
        st.error(f"No fue posible completar la búsqueda: {exc}")
        st.stop()

if exact_phrase and response.requested_mode != response.used_mode:
    st.info(
        "La opción de frase completa utiliza búsqueda textual para respetar "
        "el orden exacto de las palabras."
    )
elif response.requested_mode != response.used_mode:
    st.warning(
        "El índice semántico todavía no está disponible; esta consulta se "
        "resolvió con búsqueda textual. Ejecuta Construir índice una vez con "
        "la versión 0.5."
    )

groups = group_search_results(response.results, order=order_labels[selected_order_label])
if not groups:
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
structured_by_chunk = regulatory_records_for_chunks(
    DATABASE_PATH,
    [result.chunk_id for result in visible_results],
)

summary_col1, summary_col2, summary_col3 = st.columns([1.2, 1, 1])
summary_col1.metric("PDF recuperados", len(groups))
summary_col2.metric("Fragmentos recuperados", len(response.results))
summary_col3.metric("Página de resultados", f"{current_page} de {total_pages}")
st.caption(
    f"Modo utilizado: **{response.used_mode}** · se muestran hasta tres "
    "fragmentos por documento."
)

nav_left, nav_middle, nav_right = st.columns([1, 2, 1])
if nav_left.button(
    "← Anterior",
    disabled=current_page <= 1,
    use_container_width=True,
):
    st.session_state["explorer_page"] = current_page - 1
    st.rerun()
nav_middle.caption(f"Resultados {start + 1}–{min(start + page_size, len(groups))}")
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
                controls[2].link_button(
                    f"Abrir PDF · F{fragment_index}",
                    direct_url,
                    use_container_width=True,
                )

                structured = structured_by_chunk.get(result.chunk_id, [])
                if structured:
                    record = structured[0]
                    with st.expander("Ficha regulatoria extraída", expanded=False):
                        field_rows = {
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
                        st.dataframe(
                            [
                                {"Campo": label, "Valor": value}
                                for label, value in field_rows.items()
                                if value
                            ],
                            hide_index=True,
                            use_container_width=True,
                        )
                        if record.get("request_text"):
                            st.markdown("**Solicitud**")
                            st.write(record["request_text"])
                        if record.get("concept_text"):
                            st.markdown("**Concepto**")
                            st.write(record["concept_text"])
                        start_page = record.get("page_number")
                        end_page = record.get("end_page_number") or start_page
                        page_label = (
                            str(start_page)
                            if start_page == end_page
                            else f"{start_page}–{end_page}"
                        )
                        st.caption(
                            "Extracción automática pendiente de verificación "
                            f"humana · páginas {page_label}."
                        )
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
