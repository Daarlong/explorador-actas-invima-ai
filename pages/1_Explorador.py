from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import replace
from pathlib import Path

import streamlit as st

from config import (
    ALLOWED_DOCUMENT_HOSTS,
    ANN_ENABLED,
    ANN_INDEX_PATH,
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
)
from services.models import SearchResult
from services.exports import (
    ExportLimitError,
    ExportUnavailableError,
    export_search_results,
)
from services.facets import FacetSummary, get_search_facets
from services.pdf_viewer import (
    render_pdf_page,
    search_pdf_page_text,
    select_viewer_text,
    viewer_session_values,
    viewer_source_payload,
)
from services.reviews import apply_latest_reviews, latest_reviews, load_review_events
from services.search import SearchResponse, search_corpus_page
from services.ui_helpers import (
    apply_app_style,
    badge_html,
    group_search_results,
    highlight_query,
    pdf_page_url,
    search_backend_label,
    search_ranking_explanation,
    search_scope_label,
)


st.set_page_config(
    page_title="Explorador de actas",
    page_icon="🔍",
    layout="wide",
    initial_sidebar_state="expanded",
)
apply_app_style()

st.title("Explorador de actas")
st.caption(
    "Encuentra precedentes regulatorios y comprueba cada hallazgo en su "
    "página de origen."
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
    field_scope: str,
    page: int,
    page_size: int,
    order: str,
    filters_json: str,
    database_identity: tuple[int, int],
    semantic_identity: tuple[int, int],
    ann_identity: tuple[int, int],
) -> SearchResponse:
    del database_identity, semantic_identity, ann_identity
    return search_corpus_page(
        DATABASE_PATH,
        SEMANTIC_INDEX_PATH,
        query,
        mode=mode,
        exact_phrase=exact_phrase,
        field_scope=field_scope,
        page=page,
        page_size=page_size,
        order=order,
        filters=json.loads(filters_json),
        ann_index_path=ANN_INDEX_PATH if ANN_ENABLED else None,
    )


@st.cache_data(show_spinner=False, ttl=3600)
def cached_search_facets(
    query: str,
    exact_phrase: bool,
    field_scope: str,
    filters_json: str,
    candidate_chunk_ids: tuple[int, ...] | None,
    query_alternatives: tuple[str, ...],
    database_identity: tuple[int, int],
) -> FacetSummary:
    """Calcula distribuciones coherentes con el universo de la búsqueda."""

    del database_identity
    return get_search_facets(
        DATABASE_PATH,
        query,
        filters=json.loads(filters_json),
        exact_phrase=exact_phrase,
        field_scope=field_scope,
        candidate_chunk_ids=candidate_chunk_ids,
        query_alternatives=query_alternatives,
        counts_exact=(candidate_chunk_ids is None),
        max_values=20,
    )


@st.cache_data(show_spinner=False, ttl=3600)
def cached_pdf_page(
    title: str,
    url: str,
    page: int,
    dpi: int = 125,
    rotation: int = 0,
):
    return render_pdf_page(
        title,
        url,
        page,
        PDF_CACHE_DIR,
        ALLOWED_DOCUMENT_HOSTS,
        MAX_PDF_BYTES,
        dpi=dpi,
        rotation=rotation,
    )


def open_document_viewer(source: SearchResult | dict, page: int | None = None) -> None:
    """Conserva una fuente verificable para abrirla en el visor compartido."""

    st.session_state.update(viewer_session_values(source, page=page))
    st.session_state.pop("viewer_origin_page", None)


def set_explorer_query(value: str) -> None:
    """Carga un ejemplo en el buscador sin escribir sobre un widget ya creado."""

    st.session_state["explorer_query"] = value


def reset_explorer_filters() -> None:
    """Restablece solo los filtros; conserva la consulta y la selección."""

    defaults = {
        "explorer_exact_phrase": False,
        "explorer_field_scope": "Todo el contenido",
        "explorer_years": [],
        "explorer_acta_numbers": [],
        "explorer_sections": [],
        "explorer_parts": [],
        "explorer_outcomes": [],
        "explorer_request_types": [],
        "explorer_product": "",
        "explorer_active_ingredient": "",
        "explorer_interested_party": "",
        "explorer_identifier": "",
        "explorer_missing_active_ingredient": False,
        "explorer_provenance": [],
        "explorer_confidence": "Cualquiera",
    }
    for key, value in defaults.items():
        st.session_state[key] = value


def apply_suggested_filter(target_key: str, source_key: str) -> None:
    """Aplica una sugerencia facetada al filtro de texto correspondiente."""

    value = str(st.session_state.get(source_key) or "").strip()
    if value:
        st.session_state[target_key] = value


def _state_list(key: str) -> list:
    """Lee listas de widgets sin asumir que Streamlit ya los creó."""

    value = st.session_state.get(key, [])
    return list(value) if isinstance(value, (list, tuple, set)) else []


def _facet_counts(
    summary: FacetSummary | None,
    dimension: str,
) -> dict[object, int]:
    if summary is None:
        return {}
    return {item.value: int(item.count) for item in summary.facets.get(dimension, ())}


def _facet_option_label(
    value: object,
    *,
    counts: dict[object, int],
    exact: bool,
    label: str | None = None,
    show_count: bool = True,
) -> str:
    visible = label or str(value)
    if not show_count:
        return visible
    marker = "" if exact else "≈"
    return f"{visible} ({marker}{counts.get(value, 0)} actas)"


def metadata_tag(value: object) -> str:
    """Presenta metadatos como etiquetas Markdown compactas y seguras."""

    clean = " ".join(str(value).replace("`", "").split())
    return f"`{clean}`"


def extraction_provenance(value: object) -> str:
    """Normaliza la trazabilidad técnica para la interfaz de consulta."""

    labels = {
        "explicit": "Texto explícito",
        "inferred": "Inferido por estructura",
        "mixed": "Evidencia combinada",
        "verified": "Fuente documental",
        "legacy_automatic": "Extracción automática anterior",
        "not_extracted": "No extraído",
    }
    return labels.get(str(value or "not_extracted"), "Extracción automática")


def render_document_viewer(*, key_prefix: str = "viewer") -> None:
    """Renderiza navegación, imagen y texto copiable de la evidencia activa."""

    raw_source = st.session_state.get("viewer_source")
    if not raw_source:
        st.info(
            "Selecciona **Ver en el visor** en una coincidencia para consultar "
            "la página sin salir del Explorador."
        )
        return

    try:
        payload = viewer_source_payload(raw_source)
    except (TypeError, ValueError) as exc:
        st.error(f"La fuente seleccionada no es válida: {exc}")
        return

    source_title = str(payload["title"])
    source_url = str(payload["url"])
    source_page = int(payload["page"])
    indexed_fragment = str(payload.get("text") or "")
    identity = hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:12]
    page_number = int(st.session_state.get("viewer_page", source_page))
    source_heading, close_heading = st.columns([5, 1], vertical_alignment="center")
    source_heading.markdown(f"**{source_title}**")
    source_heading.caption(f"Fuente seleccionada · página {source_page}")
    if close_heading.button(
        "Cerrar",
        key=f"{key_prefix}_close_top_{identity}_{page_number}",
        use_container_width=True,
    ):
        st.session_state.pop("viewer_source", None)
        st.session_state.pop("viewer_page", None)
        st.session_state.pop("viewer_origin_page", None)
        st.rerun()

    detail_col, rotation_col = st.columns(2)
    dpi = detail_col.select_slider(
        "Detalle / zoom",
        options=[90, 110, 125, 150, 180, 220, 240],
        value=125,
        format_func=lambda value: f"{value} DPI",
        key=f"{key_prefix}_dpi_{identity}",
        help=(
            "Una resolución mayor facilita leer letra pequeña, pero tarda más "
            "y utiliza más memoria."
        ),
    )
    rotation = rotation_col.selectbox(
        "Rotación",
        options=[0, 90, 180, 270],
        format_func=lambda value: f"{value}°",
        key=f"{key_prefix}_rotation_{identity}",
    )

    try:
        with st.spinner("Cargando la página del documento..."):
            rendered = cached_pdf_page(
                source_title,
                source_url,
                page_number,
                int(dpi),
                int(rotation),
            )
    except Exception as exc:
        st.error(f"No fue posible renderizar esta página: {exc}")
        fallback_url = pdf_page_url(
            source_url,
            page_number,
            ALLOWED_DOCUMENT_HOSTS,
        )
        if fallback_url:
            st.link_button("Abrir documento externamente", fallback_url)
        if st.button("Cerrar visor", key=f"{key_prefix}_close_error_{identity}"):
            st.session_state.pop("viewer_source", None)
            st.session_state.pop("viewer_page", None)
            st.session_state.pop("viewer_origin_page", None)
            st.rerun()
        return

    jump_col, go_col = st.columns([2, 1])
    requested_page = jump_col.number_input(
        "Ir a página",
        min_value=1,
        max_value=rendered.page_count,
        value=min(max(1, page_number), rendered.page_count),
        step=1,
        key=f"{key_prefix}_jump_{identity}_{page_number}",
    )
    if go_col.button(
        "Ir",
        key=f"{key_prefix}_go_{identity}_{page_number}",
        use_container_width=True,
    ):
        st.session_state["viewer_page"] = int(requested_page)
        st.rerun()

    st.image(
        rendered.image_bytes,
        caption=(
            f"Página {rendered.page_number} de {rendered.page_count} · "
            f"{rendered.dpi} DPI · rotación {rendered.rotation}°"
        ),
        use_container_width=True,
    )
    previous, following = st.columns(2)
    if previous.button(
        "← Página anterior",
        key=f"{key_prefix}_previous_{identity}_{page_number}",
        disabled=page_number <= 1,
        use_container_width=True,
    ):
        st.session_state["viewer_page"] = page_number - 1
        st.rerun()
    if following.button(
        "Página siguiente →",
        key=f"{key_prefix}_next_{identity}_{page_number}",
        disabled=page_number >= rendered.page_count,
        use_container_width=True,
    ):
        st.session_state["viewer_page"] = page_number + 1
        st.rerun()

    with st.expander("Buscar y copiar texto de esta página", expanded=False):
        viewer_text = select_viewer_text(rendered.text, indexed_fragment)
        if viewer_text.text:
            if viewer_text.is_full_page:
                st.caption("Texto completo extraído directamente de esta página del PDF.")
            else:
                st.warning(
                    "El PDF no contiene texto nativo seleccionable. Se muestra "
                    "únicamente el fragmento indexado asociado a este resultado; "
                    "no representa necesariamente toda la página."
                )
            page_query = st.text_input(
                "Buscar dentro de la página",
                key=f"{key_prefix}_text_query_{identity}_{page_number}",
                placeholder="Ej.: semaglutida",
            )
            if page_query.strip():
                matches = search_pdf_page_text(viewer_text.text, page_query)
                if matches.occurrence_count:
                    st.success(
                        f"{matches.occurrence_count} coincidencia(s) en esta página."
                    )
                    for snippet in matches.snippets:
                        st.markdown(
                            highlight_query(snippet, page_query),
                            unsafe_allow_html=True,
                        )
                else:
                    st.info("La frase no aparece en el texto de esta página.")
            st.caption(
                "Usa el icono de copiar del bloque para llevarte el texto mostrado."
            )
            st.code(viewer_text.text, language=None)
            st.download_button(
                (
                    "Descargar texto de la página"
                    if viewer_text.is_full_page
                    else "Descargar fragmento indexado"
                ),
                data=viewer_text.text.encode("utf-8"),
                file_name=f"pagina-{rendered.page_number}.txt",
                mime="text/plain",
                key=f"{key_prefix}_download_text_{identity}_{page_number}",
                use_container_width=True,
            )
        else:
            st.info("No hay texto seleccionable ni fragmento indexado disponible.")

    external_url = pdf_page_url(
        rendered.resolved_url,
        page_number,
        ALLOWED_DOCUMENT_HOSTS,
    )
    if external_url:
        st.link_button(
            "Abrir documento completo",
            external_url,
            use_container_width=True,
        )
    origin_pages = {
        "pages/2_Analista_IA.py": "Volver al Analista IA",
        "pages/7_Comparar.py": "Volver a Comparar",
    }
    origin_page = str(st.session_state.get("viewer_origin_page") or "")
    if origin_page in origin_pages and st.button(
        origin_pages[origin_page],
        key=f"{key_prefix}_return_{identity}_{page_number}",
        use_container_width=True,
    ):
        st.switch_page(origin_page)
    if st.button(
        "Cerrar visor",
        key=f"{key_prefix}_close_{identity}_{page_number}",
        use_container_width=True,
    ):
        st.session_state.pop("viewer_source", None)
        st.session_state.pop("viewer_page", None)
        st.session_state.pop("viewer_origin_page", None)
        st.rerun()


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
has_regulatory_search = table_exists(DATABASE_PATH, "regulatory_records_fts")
try:
    review_events = load_review_events(REVIEW_LOG_PATH)
except ValueError:
    st.error(
        "No fue posible preparar el catálogo estructurado. Se detuvo la "
        "consulta para evitar resultados inconsistentes."
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
    "Semántica neuronal": "semantic",
}
field_scope_labels = {
    "Todo el contenido": "all",
}
if has_regulatory_search:
    field_scope_labels.update(
        {
            "Solicitud": "request",
            "Concepto / decisión": "concept",
            "Producto": "product",
            "Principio activo": "active_ingredient",
            "Interesado": "interested_party",
            "Expediente": "expediente",
            "Radicado": "radicado",
            "Ficha estructurada": "record",
            "Resultado derivado del concepto": "outcome",
        }
    )
if st.session_state.get("explorer_field_scope") not in field_scope_labels:
    st.session_state["explorer_field_scope"] = "Todo el contenido"
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
provenance_options = {
    "Texto explícito": "explicit",
    "Inferido por estructura": "inferred",
    "Evidencia combinada": "mixed",
    "No extraído": "not_extracted",
}

# Las facetas se preparan con el estado confirmado de la ejecución anterior.
# Los callbacks de Streamlit actualizan ese estado antes del rerun, por lo que
# los conteos ya están disponibles cuando se crean los controles laterales.
facet_summary: FacetSummary | None = None
facet_query = str(st.session_state.get("explorer_query") or "").strip()
state_mode_label = str(
    st.session_state.get("explorer_search_mode") or "Híbrida (recomendada)"
)
if state_mode_label not in mode_labels:
    state_mode_label = "Híbrida (recomendada)"
state_field_label = str(
    st.session_state.get("explorer_field_scope") or "Todo el contenido"
)
if state_field_label not in field_scope_labels:
    state_field_label = "Todo el contenido"
state_order_label = str(
    st.session_state.get("explorer_order") or "Mayor relevancia"
)
if state_order_label not in order_labels:
    state_order_label = "Mayor relevancia"
state_page_size = int(st.session_state.get("explorer_page_size") or 10)
if state_page_size not in {5, 10, 15, 20}:
    state_page_size = 10
state_filters = {
    "years": _state_list("explorer_years"),
    "acta_numbers": _state_list("explorer_acta_numbers"),
    "sections": _state_list("explorer_sections"),
    "parts": _state_list("explorer_parts"),
    "outcomes": _state_list("explorer_outcomes"),
    "request_types": _state_list("explorer_request_types"),
    "products": (
        [str(st.session_state.get("explorer_product") or "").strip()]
        if str(st.session_state.get("explorer_product") or "").strip()
        else []
    ),
    "active_ingredients": (
        [str(st.session_state.get("explorer_active_ingredient") or "").strip()]
        if str(st.session_state.get("explorer_active_ingredient") or "").strip()
        else []
    ),
    "interested_parties": (
        [str(st.session_state.get("explorer_interested_party") or "").strip()]
        if str(st.session_state.get("explorer_interested_party") or "").strip()
        else []
    ),
    "identifiers": (
        [str(st.session_state.get("explorer_identifier") or "").strip()]
        if str(st.session_state.get("explorer_identifier") or "").strip()
        else []
    ),
}
facet_suppressed_reason: str | None = None
facet_uses_post_filters = bool(
    st.session_state.get("explorer_missing_active_ingredient", False)
    or _state_list("explorer_provenance")
    or str(st.session_state.get("explorer_confidence") or "Cualquiera")
    != "Cualquiera"
    or (
        review_events
        and any(
            state_filters.get(key)
            for key in (
                "outcomes",
                "request_types",
                "products",
                "active_ingredients",
                "interested_parties",
                "identifiers",
            )
        )
    )
)
if facet_uses_post_filters:
    facet_suppressed_reason = (
        "Los conteos no están disponibles con esta combinación porque algunos "
        "filtros se aplican después de recuperar las evidencias."
    )
elif facet_query:
    try:
        # El pool se obtiene sin filtros: luego `get_search_facets` aplica todos
        # salvo el de la dimensión que cuenta. Así un resultado seleccionado no
        # oculta las alternativas disponibles en esa misma dimensión.
        facet_search_has_filters = any(state_filters.values())
        facet_search_filters = (
            {key: [] for key in state_filters}
            if facet_search_has_filters
            else state_filters
        )
        facet_response = cached_search(
            facet_query,
            mode_labels[state_mode_label],
            bool(st.session_state.get("explorer_exact_phrase", False)),
            field_scope_labels[state_field_label],
            (
                1
                if facet_search_has_filters
                else max(1, int(st.session_state.get("explorer_page", 1)))
            ),
            100 if facet_search_has_filters else state_page_size,
            "relevance" if facet_search_has_filters else order_labels[state_order_label],
            json.dumps(
                facet_search_filters,
                ensure_ascii=False,
                sort_keys=True,
            ),
            _file_identity(DATABASE_PATH),
            _file_identity(SEMANTIC_INDEX_PATH),
            _file_identity(ANN_INDEX_PATH),
        )
        facet_summary = cached_search_facets(
            facet_query,
            bool(st.session_state.get("explorer_exact_phrase", False)),
            field_scope_labels[state_field_label],
            json.dumps(state_filters, ensure_ascii=False, sort_keys=True),
            facet_response.facet_candidate_ids,
            facet_response.query_terms_expanded,
            _file_identity(DATABASE_PATH),
        )
    except (OSError, RuntimeError, ValueError, sqlite3.Error):
        # La búsqueda principal mostrará el error completo si también falla.
        # La ausencia temporal de facetas no debe impedir consultar el corpus.
        facet_summary = None

facet_exact = bool(facet_summary and facet_summary.exact)
year_counts = _facet_counts(facet_summary, "years")
section_counts = _facet_counts(facet_summary, "sections")
outcome_counts = _facet_counts(facet_summary, "outcomes")
request_type_counts = _facet_counts(facet_summary, "request_types")

with st.sidebar:
    st.header("Opciones de búsqueda")
    st.caption("Ajusta la recuperación y acota el conjunto de actas.")

    with st.expander("Cómo buscar", expanded=True):
        selected_mode_label = st.radio(
            "Método",
            list(mode_labels),
            key="explorer_search_mode",
            help=(
                "La búsqueda híbrida combina FTS5 con representaciones "
                "multilingües locales, sin enviar información a servicios externos."
            ),
        )
        selected_field_scope_label = st.selectbox(
            "Buscar en",
            list(field_scope_labels),
            key="explorer_field_scope",
            help=(
                "Limita la consulta a todo el documento o a un campo real de "
                "las fichas extraídas. Los filtros siguen aplicándose."
            ),
        )
        exact_phrase = st.checkbox(
            "Exigir frase completa",
            key="explorer_exact_phrase",
            help="Utiliza únicamente coincidencias textuales en el mismo orden.",
        )

    with st.expander("Acta y publicación", expanded=True):
        if facet_summary is not None:
            if facet_exact:
                st.caption("Conteos exactos de actas para la consulta actual.")
            elif facet_summary.scope in {"bounded_textual", "bounded_phrase"}:
                st.caption(
                    "Conteos aproximados (≈) sobre una muestra determinista: "
                    "la consulta textual es demasiado amplia para contarla "
                    "completa de forma interactiva."
                )
            else:
                st.caption(
                    "Conteos aproximados (≈) dentro del conjunto recuperado "
                    "por la búsqueda neuronal; no son un total exhaustivo."
                )
        elif facet_suppressed_reason:
            st.caption(facet_suppressed_reason)
        years = st.multiselect(
            "Año",
            options["years"],
            format_func=lambda value: _facet_option_label(
                value,
                counts=year_counts,
                exact=facet_exact,
                show_count=facet_summary is not None,
            ),
            key="explorer_years",
            placeholder="Todos los años",
        )
        acta_numbers = st.multiselect(
            "Número de acta",
            options["acta_numbers"],
            key="explorer_acta_numbers",
            placeholder="Todas las actas",
        )
        sections = st.multiselect(
            "Sala o sección",
            options["sections"],
            format_func=lambda value: _facet_option_label(
                value,
                counts=section_counts,
                exact=facet_exact,
                show_count=facet_summary is not None,
            ),
            key="explorer_sections",
            placeholder="Todas las secciones",
        )
        parts = st.multiselect(
            "Parte",
            options["parts"],
            key="explorer_parts",
            placeholder="Todas las partes",
        )
    outcomes: list[str] = []
    request_types: list[str] = []
    selected_provenance_labels: list[str] = []
    missing_active_ingredient = False
    confidence_minimum: float | None = None
    product = active_ingredient = interested_party = identifier = ""
    if has_structured_data:
        with st.expander("Contenido regulatorio", expanded=False):
            outcomes = st.multiselect(
                "Resultado extraído",
                options.get("outcomes", []),
                format_func=lambda value: _facet_option_label(
                    value,
                    counts=outcome_counts,
                    exact=facet_exact,
                    label=outcome_labels.get(value, value),
                    show_count=facet_summary is not None,
                ),
                key="explorer_outcomes",
                placeholder="Todos los resultados",
            )
            request_types = st.multiselect(
                "Tipo de solicitud",
                options.get("request_types", []),
                format_func=lambda value: _facet_option_label(
                    value,
                    counts=request_type_counts,
                    exact=facet_exact,
                    label=str(value).replace("_", " ").capitalize(),
                    show_count=facet_summary is not None,
                ),
                key="explorer_request_types",
                placeholder="Todos los tipos",
            )
            product = st.text_input("Producto", key="explorer_product")
            active_ingredient = st.text_input(
                "Principio activo", key="explorer_active_ingredient"
            )
            interested_party = st.text_input(
                "Interesado o titular", key="explorer_interested_party"
            )
            identifier = st.text_input(
                "Expediente o radicado", key="explorer_identifier"
            )
            missing_active_ingredient = st.checkbox(
                "Solo fichas sin principio activo extraído",
                key="explorer_missing_active_ingredient",
            )
            selected_provenance_labels = st.multiselect(
                "Procedencia del principio activo",
                list(provenance_options),
                key="explorer_provenance",
                placeholder="Cualquier procedencia",
            )
            confidence_choice = st.selectbox(
                "Confianza mínima del principio activo",
                ["Cualquiera", "Media (≥ 60 %)", "Alta (≥ 85 %)"],
                key="explorer_confidence",
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
            high_cardinality_facets = {
                "Principio activo": (
                    "active_ingredients",
                    "explorer_active_ingredient",
                ),
                "Interesado": (
                    "interested_parties",
                    "explorer_interested_party",
                ),
                "Producto": ("products", "explorer_product"),
            }
            if facet_summary is not None and any(
                facet_summary.facets.get(key)
                for key, _ in high_cardinality_facets.values()
            ):
                st.markdown("**Valores frecuentes en esta consulta**")
                suggestion_dimension = st.selectbox(
                    "Mostrar sugerencias de",
                    list(high_cardinality_facets),
                    key="explorer_facet_suggestion_dimension",
                )
                facet_key, target_key = high_cardinality_facets[
                    suggestion_dimension
                ]
                suggestions = list(facet_summary.facets.get(facet_key, ()))[:10]
                if suggestions:
                    suggestion_key = f"explorer_facet_suggestion_{facet_key}"
                    suggestion_counts = {
                        item.value: item.count for item in suggestions
                    }
                    suggestion_labels = {
                        item.value: item.label or str(item.value)
                        for item in suggestions
                    }
                    selected_suggestion = st.selectbox(
                        "Valor sugerido",
                        [item.value for item in suggestions],
                        format_func=lambda value: _facet_option_label(
                            value,
                            counts=suggestion_counts,
                            exact=facet_exact,
                            label=suggestion_labels.get(value),
                        ),
                        key=suggestion_key,
                    )
                    st.button(
                        f"Aplicar a {suggestion_dimension.lower()}",
                        key=f"apply_{suggestion_key}",
                        on_click=apply_suggested_filter,
                        args=(target_key, suggestion_key),
                        disabled=not selected_suggestion,
                        use_container_width=True,
                    )

    with st.expander("Presentación", expanded=False):
        selected_order_label = st.selectbox(
            "Orden",
            list(order_labels),
            key="explorer_order",
        )
        page_size = st.select_slider(
            "PDF/partes por página",
            [5, 10, 15, 20],
            value=10,
            key="explorer_page_size",
        )

    active_filter_count = sum(
        bool(value)
        for value in (
            exact_phrase,
            field_scope_labels[selected_field_scope_label] != "all",
            years,
            acta_numbers,
            sections,
            parts,
            outcomes,
            request_types,
            product.strip(),
            active_ingredient.strip(),
            interested_party.strip(),
            identifier.strip(),
            missing_active_ingredient,
            selected_provenance_labels,
            confidence_minimum is not None,
        )
    )
    filter_status, filter_reset = st.columns([1.15, 1])
    filter_status.caption(
        f"{active_filter_count} filtro(s) activo(s)"
        if active_filter_count
        else "Sin filtros adicionales"
    )
    filter_reset.button(
        "Restablecer",
        key="explorer_reset_filters",
        disabled=active_filter_count == 0,
        on_click=reset_explorer_filters,
        use_container_width=True,
    )

    st.divider()
    with st.container(border=True):
        st.markdown("#### Selección")
        st.caption(
            f"{len(selected_evidence)} evidencia(s) preparada(s) para usar en "
            "otras vistas."
        )
        analyze_disabled = not selected_evidence
        if st.button(
            f"Analizar selección ({len(selected_evidence)})",
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
            f"Comparar selección ({len(selected_evidence)})",
            use_container_width=True,
            disabled=analyze_disabled,
        ):
            st.session_state["comparison_selected_sources"] = list(
                selected_evidence.values()
            )
            st.switch_page("pages/7_Comparar.py")
        if st.button(
            "Vaciar selección",
            use_container_width=True,
            disabled=analyze_disabled,
        ):
            clear_selected_evidence()
            st.rerun()

with st.container(border=True):
    st.markdown("### Buscar en el corpus")
    st.caption(
        "Usa nombres, principios activos, empresas, expedientes, radicados o "
        "preguntas breves sobre el contenido de las actas."
    )
    with st.form("explorer_search_form", border=False):
        search_input, search_action = st.columns(
            [5, 1], vertical_alignment="bottom"
        )
        query = search_input.text_input(
            "Consulta",
            key="explorer_query",
            placeholder=(
                "Ej.: semaglutida, expediente 123456 o requerimientos sobre "
                "estabilidad"
            ),
            label_visibility="collapsed",
        )
        search_action.form_submit_button(
            "Buscar",
            type="primary",
            use_container_width=True,
        )
    search_context = [selected_mode_label, selected_field_scope_label]
    if active_filter_count:
        search_context.append(f"{active_filter_count} filtro(s)")
    context_column, clear_query_column = st.columns(
        [5, 1], vertical_alignment="center"
    )
    context_column.caption(" · ".join(search_context))
    clear_query_column.button(
        "Limpiar consulta",
        key="explorer_clear_query",
        disabled=not query.strip(),
        on_click=set_explorer_query,
        args=("",),
        use_container_width=True,
    )

if not query.strip():
    if st.session_state.get("viewer_source"):
        with st.container(border=True):
            st.subheader("Fuente seleccionada")
            render_document_viewer(key_prefix="viewer_standalone")
    else:
        st.markdown("### ¿Por dónde empezar?")
        st.caption(
            "Prueba una búsqueda frecuente o escribe tu propia consulta en el "
            "campo superior."
        )
        example_columns = st.columns(3)
        examples = (
            ("Principio activo", "semaglutida"),
            ("Tipo de decisión", "requerimientos de estabilidad"),
            ("Antecedente", "cambio de indicación"),
        )
        for column, (label, value) in zip(example_columns, examples):
            with column.container(border=True):
                st.markdown(f"**{label}**")
                st.caption(value.capitalize())
                st.button(
                    "Usar este ejemplo",
                    key=f"example_{hashlib.sha256(value.encode()).hexdigest()[:8]}",
                    on_click=set_explorer_query,
                    args=(value,),
                    use_container_width=True,
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
    "review_statuses": [],
}
# Algunos filtros estructurados se aplican después de la recuperación para
# respetar los metadatos efectivos sin reconstruir FTS. Año/sala/acta siguen
# resolviéndose eficientemente en SQLite.
post_filter_effective = bool(
    any(
        (
            missing_active_ingredient,
            selected_provenance_labels,
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
            field_scope_labels[selected_field_scope_label],
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
current_page = max(1, int(st.session_state.get("explorer_page", 1)))

with st.spinner("Consultando el corpus..."):
    try:
        response = cached_search(
            query.strip(),
            mode_labels[selected_mode_label],
            exact_phrase,
            field_scope_labels[selected_field_scope_label],
            current_page,
            page_size,
            order_labels[selected_order_label],
            json.dumps(search_filters, ensure_ascii=False, sort_keys=True),
            _file_identity(DATABASE_PATH),
            _file_identity(SEMANTIC_INDEX_PATH),
            _file_identity(ANN_INDEX_PATH),
        )
        # Conserva también los candidatos que coinciden con los valores de la
        # extracción. El barrido sin filtros y esta segunda consulta evitan
        # perder fichas más profundas en el ranking por el límite de recuperación.
        if post_filter_effective and search_filters != filters:
            automatic_response = cached_search(
                query.strip(),
                mode_labels[selected_mode_label],
                exact_phrase,
                field_scope_labels[selected_field_scope_label],
                current_page,
                page_size,
                order_labels[selected_order_label],
                json.dumps(filters, ensure_ascii=False, sort_keys=True),
                _file_identity(DATABASE_PATH),
                _file_identity(SEMANTIC_INDEX_PATH),
                _file_identity(ANN_INDEX_PATH),
            )
            candidates = {result.chunk_id: result for result in response.results}
            for result in automatic_response.results:
                current = candidates.get(result.chunk_id)
                if current is None or result.score > current.score:
                    candidates[result.chunk_id] = result
            response = replace(
                response,
                results=sorted(
                    candidates.values(), key=lambda item: (-item.score, item.chunk_id)
                ),
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
    response = replace(
        response,
        results=[
            result
            for result in response.results
            if any(
                effective_record_matches(record, **effective_filter_values)
                for record in all_structured_by_chunk.get(result.chunk_id, [])
            )
        ],
        totals_exact=False,
    )

if response.field_scope != "all" and response.requested_mode != response.used_mode:
    st.info(
        "La búsqueda por campo usa el índice textual de fichas para mantener "
        "aislado el contenido del campo seleccionado."
    )
elif exact_phrase and response.requested_mode != response.used_mode:
    st.info(
        "La opción de frase completa utiliza búsqueda textual para respetar "
        "el orden exacto de las palabras."
    )
elif response.requested_mode != response.used_mode:
    st.warning(
        "El índice semántico todavía no está disponible; esta consulta se "
        "resolvió con búsqueda textual. Ejecuta Construir índice para generar "
        "o actualizar el índice semántico."
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
        confidence_minimum is not None,
    )
):
    # Los metadatos efectivos pueden cambiar el resultado de estos filtros aunque
    # el texto de la consulta coincida con solicitud o concepto.
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

groups = group_search_results(response.results, order="relevance")
if not groups and not corrected_records:
    with st.container(border=True):
        st.warning("No encontramos coincidencias para esta combinación.")
        st.markdown("**Puedes intentar:**")
        st.markdown(
            "- Usar menos palabras o retirar la frase completa.\n"
            "- Buscar por un solo identificador, producto o principio activo.\n"
            "- Restablecer los filtros de la barra lateral.\n"
            "- Cambiar al modo híbrido para ampliar la recuperación."
        )
        if active_filter_count:
            st.button(
                "Restablecer filtros y volver a intentar",
                key=f"empty_reset_{signature}",
                on_click=reset_explorer_filters,
            )
    st.stop()

total_pages = response.total_pages or max(1, math.ceil(len(groups) / page_size))
current_page = min(response.page, total_pages)
st.session_state["explorer_page"] = current_page
start = (current_page - 1) * page_size
visible_groups = groups
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

result_heading, result_selection = st.columns([4, 1], vertical_alignment="bottom")
with result_heading:
    st.subheader("Resultados")
    displayed_documents = response.total_documents
    displayed_fragments = response.total_fragments
    total_label = (
        "coincidencias globales"
        if response.totals_exact
        else "resultados del conjunto recuperado"
    )
    st.caption(
        f"**{displayed_documents if displayed_documents is not None else len(groups)} "
        f"PDF/parte(s)** · {displayed_fragments if displayed_fragments is not None else len(response.results)} "
        f"fragmento(s) · {total_label} · página {current_page} de {total_pages}"
    )
    st.markdown(
        " ".join(
            (
                badge_html(
                    search_backend_label(
                        response.retrieval_backend,
                        mode=response.used_mode,
                    ),
                    tone="success",
                ),
                badge_html(
                    f"Campo: {search_scope_label(response.field_scope)}",
                    tone="info",
                ),
            )
        ),
        unsafe_allow_html=True,
    )
    if response.fallback_reason:
        st.warning(response.fallback_reason)
    if response.query_terms_expanded:
        with st.expander("También se buscaron equivalencias regulatorias"):
            st.caption(
                "Estas expresiones amplían únicamente el carril textual. La "
                "consulta que escribiste y el carril semántico no se modifican."
            )
            st.write(", ".join(response.query_terms_expanded))
            if response.query_expansion_version:
                st.caption(
                    f"Diccionario regulatorio: {response.query_expansion_version}"
                )
    with st.expander("¿Cómo se obtuvieron y ordenaron estos resultados?"):
        st.write(
            search_ranking_explanation(
                mode=response.used_mode,
                backend=response.retrieval_backend,
                exact_phrase=exact_phrase,
            )
        )
        if response.candidate_count is not None and not response.totals_exact:
            st.caption(
                f"Se evaluó un conjunto de {response.candidate_count:,} "
                "candidatos; el total semántico es aproximado."
            )
with result_selection:
    st.markdown(
        badge_html(
            f"{len(selected_evidence)} seleccionada(s)",
            tone="success" if selected_evidence else "neutral",
        ),
        unsafe_allow_html=True,
    )

with st.expander("Exportar resultados", expanded=False):
    export_options = ["Esta página"]
    if response.totals_exact:
        export_options.append("Todas las coincidencias")
    export_choice, export_format = st.columns(2)
    selected_export_scope = export_choice.radio(
        "Alcance",
        export_options,
        horizontal=True,
        key=f"export_scope_{signature}",
    )
    selected_export_format = export_format.radio(
        "Formato",
        ["CSV", "XLSX"],
        horizontal=True,
        key=f"export_format_{signature}",
    )
    if not response.totals_exact:
        st.caption(
            "La búsqueda semántica exporta la página visible porque su conjunto "
            "de candidatos es aproximado."
        )
    if st.button(
        "Preparar exportación",
        key=f"prepare_export_{signature}",
        use_container_width=True,
    ):
        try:
            export_results = list(response.results)
            if selected_export_scope == "Todas las coincidencias":
                export_results = []
                export_total_pages = max(
                    1,
                    math.ceil((response.total_documents or 0) / 100),
                )
                for export_page in range(1, export_total_pages + 1):
                    export_response = cached_search(
                        query.strip(),
                        mode_labels[selected_mode_label],
                        exact_phrase,
                        field_scope_labels[selected_field_scope_label],
                        export_page,
                        100,
                        order_labels[selected_order_label],
                        json.dumps(
                            search_filters,
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        _file_identity(DATABASE_PATH),
                        _file_identity(SEMANTIC_INDEX_PATH),
                        _file_identity(ANN_INDEX_PATH),
                    )
                    export_results.extend(export_response.results)
            artifact = export_search_results(
                export_results,
                file_format=selected_export_format.lower(),
                file_stem=f"consulta_actas_{signature}",
                metadata={
                    "consulta": query,
                    "metodo_solicitado": mode_labels[selected_mode_label],
                    "motor_real": response.retrieval_backend,
                    "campo": response.field_scope,
                    "filtros": json.dumps(
                        filters,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    "frase_completa": exact_phrase,
                    "ruta_respaldo": response.fallback_reason or "",
                    "totales_exactos": response.totals_exact,
                },
            )
            st.session_state["explorer_export_artifact"] = {
                "signature": signature,
                "scope": selected_export_scope,
                "format": selected_export_format,
                "name": artifact.file_name,
                "mime": artifact.mime_type,
                "data": artifact.data,
                "rows": artifact.row_count,
            }
        except (ExportLimitError, ExportUnavailableError, ValueError) as exc:
            st.error(str(exc))
    prepared_export = st.session_state.get("explorer_export_artifact")
    if prepared_export and prepared_export.get("signature") == signature:
        st.download_button(
            f"Descargar {prepared_export['format']} "
            f"({prepared_export['rows']} filas)",
            data=prepared_export["data"],
            file_name=prepared_export["name"],
            mime=prepared_export["mime"],
            key=f"download_export_{signature}_{prepared_export['format']}",
            use_container_width=True,
        )

if corrected_records:
    with st.expander(
        f"Coincidencias adicionales en fichas ({len(corrected_records)})",
        expanded=not groups,
    ):
        st.caption(
            "Estas coincidencias provienen de los campos estructurados y se "
            "presentan con una fuente documental verificable."
        )
        if review_candidate_truncated or review_match_truncated:
            st.warning(
                "Se alcanzó el límite de recuperación. Acota la consulta o los "
                "filtros para consultar todas las coincidencias."
            )
        corrected_page_size = 20
        corrected_page_count = max(
            1, math.ceil(len(corrected_records) / corrected_page_size)
        )
        corrected_page = st.number_input(
            "Página de coincidencias adicionales",
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
        for record in corrected_visible:
            st.markdown(
                f"**{display_value(record.get('product_name'))}** · "
                f"{display_value(record.get('active_ingredient'))} · "
                f"Acta {display_value(record.get('acta_number'))} "
                f"({display_value(record.get('year'))})"
            )
            uid = str(record.get("decision_uid") or "")
            selectable = selectable_by_uid.get(uid)
            controls = st.columns([1.1, 1, 1])
            if selectable is not None:
                selectable_payload = evidence_selection_payload(selectable, uid)
                selected_key = f"review:{uid}:{selectable.chunk_id}"
                selection_key = f"select_reviewed_{signature}_{uid}"
                controls[0].checkbox(
                    "Agregar a selección",
                    value=selected_key in selected_evidence,
                    key=selection_key,
                    on_change=update_selected_evidence,
                    args=(selection_key, selectable_payload, selected_key),
                )
                if controls[1].button(
                    "Ver en el visor",
                    key=f"view_reviewed_{signature}_{uid}",
                    use_container_width=True,
                ):
                    open_document_viewer(selectable_payload, selectable.page)
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

if groups:
    with st.container(border=True):
        nav_left, nav_middle, nav_right = st.columns(
            [1, 2, 1], vertical_alignment="center"
        )
        if nav_left.button(
            "← Anterior",
            key=f"previous_{signature}_{current_page}",
            disabled=current_page <= 1,
            use_container_width=True,
        ):
            st.session_state["explorer_page"] = current_page - 1
            st.rerun()
        nav_middle.markdown(
            f"PDF/partes **{start + 1}–{min(start + len(groups), response.total_documents or start + len(groups))}** "
            f"de **{response.total_documents if response.total_documents is not None else start + len(groups)}**"
        )
        if nav_right.button(
            "Siguiente →",
            key=f"next_{signature}_{current_page}",
            disabled=current_page >= total_pages,
            use_container_width=True,
        ):
            st.session_state["explorer_page"] = current_page + 1
            st.rerun()

results_column, viewer_column = st.columns([1.45, 1], gap="large")
with results_column:
    for group_index, group in enumerate(visible_groups, start=start + 1):
        with st.container(border=True):
            title_column, score_column = st.columns(
                [5, 1], vertical_alignment="top"
            )
            title_column.markdown(f"### {group_index}. {group['title']}")
            score_column.markdown(
                badge_html(f"Posición {group_index}", tone="success"),
                unsafe_allow_html=True,
            )
            metadata = []
            if group["year"]:
                metadata.append(f"Año {group['year']}")
            if group["section"]:
                metadata.append(group["section"])
            if group["part"]:
                metadata.append(group["part"])
            if group["source_type"] == "historical_mirror":
                metadata.append("Copia histórica")
            metadata.append(f"{len(group['results'])} coincidencia(s)")
            selected_in_group = sum(
                str(result.chunk_id) in selected_evidence
                for result in group["results"]
            )
            if selected_in_group:
                metadata.append(f"{selected_in_group} seleccionada(s)")
            st.markdown(
                " ".join(
                    badge_html(
                        value,
                        tone=(
                            "warning"
                            if value == "Copia histórica"
                            else "success"
                            if "seleccionada" in value
                            else "info"
                        ),
                    )
                    for value in metadata
                ),
                unsafe_allow_html=True,
            )

            for fragment_index, result in enumerate(group["results"][:3], start=1):
                st.markdown(
                    f"#### Coincidencia {fragment_index} "
                    f"{metadata_tag(f'Página {result.page}')}"
                )
                visible_text = result.match_excerpt or result.text
                st.markdown(highlight_query(visible_text, query), unsafe_allow_html=True)
                if result.match_type == "semantic" and not set(
                    normalize_word
                    for normalize_word in query.casefold().split()
                ).intersection(visible_text.casefold().split()):
                    st.caption(
                        "Relacionado por significado; puede no contener las "
                        "mismas palabras."
                    )
                with st.expander("Detalle técnico"):
                    st.write(
                        {
                            "tipo": result.match_type,
                            "puntaje_textual": result.lexical_score,
                            "puntaje_semantico": result.semantic_score,
                            "puntaje_fusionado": result.score,
                            "motor": response.retrieval_backend,
                            "campo": result.matched_field or response.field_scope,
                        }
                    )
                controls = st.columns([1.2, 1, 1])
                selection_key = f"select_evidence_{signature}_{result.chunk_id}"
                controls[0].checkbox(
                    "Agregar a selección",
                    value=str(result.chunk_id) in selected_evidence,
                    key=selection_key,
                    on_change=update_selected_evidence,
                    args=(selection_key, result.as_dict()),
                )
                if controls[1].button(
                    "Ver en el visor",
                    key=f"view_{signature}_{result.chunk_id}",
                    use_container_width=True,
                ):
                    open_document_viewer(result)
                direct_url = pdf_page_url(
                    result.url,
                    result.page,
                    ALLOWED_DOCUMENT_HOSTS,
                )
                if direct_url:
                    controls[2].link_button(
                        "Abrir PDF",
                        direct_url,
                        key=f"open_pdf_{signature}_{result.chunk_id}",
                        use_container_width=True,
                    )
                else:
                    controls[2].caption("Enlace no disponible")

                structured = structured_by_chunk.get(result.chunk_id, [])
                for record_index, record in enumerate(structured, start=1):
                    ficha_label = "Datos estructurados"
                    if len(structured) > 1:
                        ficha_label += f" {record_index} de {len(structured)}"
                    with st.expander(ficha_label, expanded=False):
                        st.caption(
                            "Información extraída automáticamente. Comprueba los "
                            "datos relevantes en la fuente enlazada."
                        )
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
                                    "Valor extraído": display_value(value),
                                    "Trazabilidad": extraction_provenance(
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
                            # Una evidencia de rango sustenta dos campos extraídos;
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
                        st.caption(f"Evidencia localizada en páginas {page_label}.")
                if fragment_index < min(3, len(group["results"])):
                    st.divider()

with viewer_column:
    with st.container(border=True):
        st.subheader("Fuente seleccionada")
        st.caption("Consulta la página y navega por el PDF sin perder los resultados.")
        render_document_viewer()

if groups and total_pages > 1:
    st.divider()
    bottom_previous, bottom_status, bottom_next = st.columns(
        [1, 2, 1], vertical_alignment="center"
    )
    if bottom_previous.button(
        "← Página anterior",
        key=f"bottom_previous_{signature}_{current_page}",
        disabled=current_page <= 1,
        use_container_width=True,
    ):
        st.session_state["explorer_page"] = current_page - 1
        st.rerun()
    bottom_status.markdown(
        f"Página **{current_page}** de **{total_pages}** · "
        f"{len(groups)} PDF/parte(s) en esta página"
    )
    if bottom_next.button(
        "Página siguiente →",
        key=f"bottom_next_{signature}_{current_page}",
        disabled=current_page >= total_pages,
        use_container_width=True,
    ):
        st.session_state["explorer_page"] = current_page + 1
        st.rerun()
