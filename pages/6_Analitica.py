from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import streamlit as st

from config import ALLOWED_DOCUMENT_HOSTS, DATABASE_PATH
from services.analytics import analytics_drilldown, build_corpus_analytics
from services.database import database_stats, get_filter_options
from services.ui_helpers import (
    analytics_explorer_state,
    apply_app_style,
    badge_html,
    pdf_page_url,
)


st.set_page_config(
    page_title="Analítica del corpus",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)
apply_app_style()


DIMENSION_LABELS = {
    "year": "Año",
    "outcome": "Resultado extraído",
    "request_type": "Tipo de solicitud",
    "active_ingredient": "Principio activo",
    "interested_party": "Interesado",
}

OUTCOME_LABELS = {
    "aprobado": "Aprobado",
    "negado": "Negado",
    "requerido": "Requerimiento",
    "desistido": "Desistido",
    "archivado": "Archivado",
    "favorable": "Favorable",
    "no_favorable": "No favorable",
    "sin_clasificar": "Sin clasificar",
}

EXPLORER_DRILLDOWN_DIMENSIONS = frozenset(
    {"outcome", "request_type", "active_ingredient", "interested_party"}
)


def _file_identity(path: Path) -> tuple[int, int]:
    if not path.exists():
        return (0, 0)
    stat = path.stat()
    return (stat.st_size, stat.st_mtime_ns)


@st.cache_data(show_spinner=False, ttl=3600)
def cached_analytics(
    filters_json: str,
    top_limit: int,
    database_identity: tuple[int, int],
) -> dict:
    del database_identity
    return build_corpus_analytics(
        DATABASE_PATH,
        filters=json.loads(filters_json),
        top_limit=top_limit,
        evidence_limit=0,
    )


@st.cache_data(show_spinner=False, ttl=3600)
def cached_drilldown(
    dimension: str,
    value: object,
    filters_json: str,
    limit: int,
    offset: int,
    database_identity: tuple[int, int],
) -> dict:
    del database_identity
    return analytics_drilldown(
        DATABASE_PATH,
        dimension,
        value,
        filters=json.loads(filters_json),
        limit=limit,
        offset=offset,
    )


def _format_count(value: object) -> str:
    if value is None:
        return "No disponible"
    return f"{int(value):,}".replace(",", ".")


def _metric_value(report: dict, name: str) -> object:
    metric = report.get("totals", {}).get(name, {})
    return metric.get("value") if metric.get("available") else None


def _dimension_label(dimension: str, item: dict) -> str:
    value = str(item.get("label") or item.get("value") or "Sin valor")
    if dimension == "outcome":
        return OUTCOME_LABELS.get(str(item.get("value")), value)
    if dimension == "request_type":
        return value.replace("_", " ").capitalize()
    return value


def _dimension_rows(report: dict, dimension: str) -> list[dict]:
    source = report.get("dimensions", {}).get(dimension, {})
    if not source.get("available"):
        return []
    return [
        {
            "Categoría": _dimension_label(dimension, item),
            "Documentos": int(item.get("documents") or 0),
            "Fichas extraídas": (
                int(item["records"]) if item.get("records") is not None else None
            ),
        }
        for item in source.get("items", [])
    ]


def render_dimension(
    report: dict,
    dimension: str,
    title: str,
    *,
    chart: str = "bar",
) -> None:
    details = report.get("dimensions", {}).get(dimension, {})
    st.markdown(f"#### {title}")
    if not details.get("available"):
        st.info("Esta distribución no está disponible en la base publicada.")
        return
    rows = _dimension_rows(report, dimension)
    if not rows:
        st.info("No hay valores para los filtros seleccionados.")
        return
    chart_rows = [
        {
            "Categoría": row["Categoría"],
            "Fichas extraídas": row["Fichas extraídas"],
        }
        for row in rows
        if row["Fichas extraídas"] is not None
    ]
    if dimension == "year":
        records_available = all(
            row["Fichas extraídas"] is not None for row in rows
        )
        chart_rows = []
        for row in reversed(rows):
            point = {
                "Año": row["Categoría"],
                "Documentos": row["Documentos"],
            }
            if records_available:
                point["Fichas extraídas"] = row["Fichas extraídas"]
            chart_rows.append(point)
        if chart == "line":
            st.line_chart(
                chart_rows,
                x="Año",
                y=(
                    ["Documentos", "Fichas extraídas"]
                    if records_available
                    else "Documentos"
                ),
                use_container_width=True,
            )
    elif chart_rows:
        st.bar_chart(
            chart_rows,
            x="Categoría",
            y="Fichas extraídas",
            horizontal=True,
            use_container_width=True,
        )
    st.dataframe(
        rows,
        hide_index=True,
        use_container_width=True,
        column_config={
            "Documentos": st.column_config.NumberColumn(format="%d"),
            "Fichas extraídas": st.column_config.NumberColumn(format="%d"),
        },
    )
    coverage = details.get("coverage", {})
    if coverage.get("percent") is not None:
        st.caption(
            f"Cobertura del campo en las fichas filtradas: "
            f"{float(coverage['percent']):.1f} %."
        )
    if details.get("truncated"):
        st.caption(
            f"Se muestran los primeros {len(rows)} de "
            f"{_format_count(details.get('total_values'))} valores."
        )


def reset_analytics_filters() -> None:
    for key, value in {
        "analytics_years": [],
        "analytics_sections": [],
        "analytics_acta_numbers": [],
        "analytics_outcomes": [],
        "analytics_request_types": [],
        "analytics_active_ingredient": "",
        "analytics_interested_party": "",
    }.items():
        st.session_state[key] = value


st.title("Analítica del corpus")
st.caption(
    "Observa tendencias agregadas de las actas y baja desde cada categoría "
    "hasta la evidencia documental. Los gráficos describen el corpus; no "
    "crean ni sustituyen decisiones oficiales."
)

stats = database_stats(DATABASE_PATH)
if stats["documents"] == 0:
    st.warning(
        "No existe un índice. Ejecuta primero Construir índice desde GitHub Actions."
    )
    st.stop()

options = get_filter_options(DATABASE_PATH)

with st.sidebar:
    st.header("Filtros del tablero")
    st.caption("Todas las métricas y evidencias respetan esta selección.")
    selected_years = st.multiselect(
        "Año",
        options.get("years", []),
        key="analytics_years",
        placeholder="Todos los años",
    )
    selected_sections = st.multiselect(
        "Sala o sección",
        options.get("sections", []),
        key="analytics_sections",
        placeholder="Todas las secciones",
    )
    selected_actas = st.multiselect(
        "Número de acta",
        options.get("acta_numbers", []),
        key="analytics_acta_numbers",
        placeholder="Todas las actas",
    )
    selected_outcomes = st.multiselect(
        "Resultado extraído",
        options.get("outcomes", []),
        format_func=lambda value: OUTCOME_LABELS.get(value, value),
        key="analytics_outcomes",
        placeholder="Todos los resultados",
    )
    selected_request_types = st.multiselect(
        "Tipo de solicitud",
        options.get("request_types", []),
        format_func=lambda value: str(value).replace("_", " ").capitalize(),
        key="analytics_request_types",
        placeholder="Todos los tipos",
    )
    active_ingredient = st.text_input(
        "Principio activo contiene",
        key="analytics_active_ingredient",
    ).strip()
    interested_party = st.text_input(
        "Interesado contiene",
        key="analytics_interested_party",
    ).strip()
    top_limit = st.select_slider(
        "Categorías visibles",
        options=[10, 15, 20, 30, 50],
        value=20,
        key="analytics_top_limit",
    )
    active_filters = sum(
        bool(value)
        for value in (
            selected_years,
            selected_sections,
            selected_actas,
            selected_outcomes,
            selected_request_types,
            active_ingredient,
            interested_party,
        )
    )
    st.button(
        "Restablecer filtros",
        on_click=reset_analytics_filters,
        disabled=active_filters == 0,
        use_container_width=True,
    )

filters = {
    "years": selected_years,
    "sections": selected_sections,
    "acta_numbers": selected_actas,
    "outcomes": selected_outcomes,
    "request_types": selected_request_types,
    "active_ingredients": [active_ingredient] if active_ingredient else [],
    "interested_parties": [interested_party] if interested_party else [],
}
filters_json = json.dumps(filters, ensure_ascii=False, sort_keys=True)

with st.spinner("Calculando métricas del corpus..."):
    report = cached_analytics(
        filters_json,
        int(top_limit),
        _file_identity(DATABASE_PATH),
    )

if report.get("status") == "unavailable":
    st.error("La base publicada no permite construir el tablero en este momento.")
    st.stop()
if report.get("status") == "partial":
    st.warning(
        "Las métricas documentales están disponibles, pero esta versión de la "
        "base no contiene todas las tablas estructuradas."
    )

scope_label = "Corpus completo" if active_filters == 0 else f"{active_filters} filtro(s)"
st.markdown(
    badge_html(scope_label, tone="info"),
    unsafe_allow_html=True,
)

metric_columns = st.columns(4)
metric_columns[0].metric(
    "Actas únicas",
    _format_count(_metric_value(report, "unique_acts")),
)
metric_columns[1].metric(
    "PDF y partes",
    _format_count(_metric_value(report, "documents")),
)
metric_columns[2].metric(
    "Fichas extraídas",
    _format_count(_metric_value(report, "records")),
)
metric_columns[3].metric(
    "Principios activos + interesados distintos",
    _format_count(
        _metric_value(report, "distinct_ingredient_and_party_values")
    ),
)

extraction = report.get("coverage", {}).get("document_extraction", {})
if extraction.get("available"):
    coverage_percent = extraction.get("record_coverage_percent")
    with_records = extraction.get("documents_with_records")
    documents = extraction.get("documents")
    st.caption(
        "Cobertura estructurada: "
        f"{_format_count(with_records)} de {_format_count(documents)} documentos"
        + (
            f" ({float(coverage_percent):.1f} %)."
            if coverage_percent is not None
            else "."
        )
        + " Una ficha es una extracción navegable del acta, no una nueva decisión."
    )
    excluded_coverage_filters = extraction.get("structured_filters_excluded", [])
    if excluded_coverage_filters:
        st.caption(
            "La cobertura documental usa únicamente año, sala y número de acta; "
            "no incorpora los filtros estructurados activos."
        )

overview_tab, regulatory_tab, entities_tab, evidence_tab = st.tabs(
    ["Evolución", "Solicitudes y resultados", "Ingredientes y actores", "Evidencia"]
)

with overview_tab:
    render_dimension(
        report,
        "year",
        "Evolución por año",
        chart="line",
    )
    with st.expander("Cobertura de campos estructurados"):
        field_labels = {
            "outcome": "Resultado extraído",
            "request_type": "Tipo de solicitud",
            "active_ingredient": "Principio activo",
            "interested_party": "Interesado",
        }
        coverage_rows = [
            {
                "Campo": field_labels.get(key, key),
                "Fichas con valor": details.get("present"),
                "Fichas sin valor": details.get("missing"),
                "Cobertura": (
                    f"{float(details['coverage_percent']):.1f} %"
                    if details.get("coverage_percent") is not None
                    else "No disponible"
                ),
            }
            for key, details in report.get("coverage", {}).get("fields", {}).items()
            if details.get("available")
        ]
        if coverage_rows:
            st.dataframe(
                coverage_rows,
                hide_index=True,
                use_container_width=True,
            )
        else:
            st.info("La cobertura de campos no está disponible en esta base.")

with regulatory_tab:
    outcome_col, request_col = st.columns(2, gap="large")
    with outcome_col:
        render_dimension(report, "outcome", "Resultados extraídos")
    with request_col:
        render_dimension(report, "request_type", "Tipos de solicitud")
    st.info(
        "El resultado es una clasificación extraída o derivada del concepto "
        "del acta. Verifica siempre la evidencia antes de usarlo."
    )

with entities_tab:
    ingredient_col, party_col = st.columns(2, gap="large")
    with ingredient_col:
        render_dimension(report, "active_ingredient", "Principios activos")
    with party_col:
        render_dimension(report, "interested_party", "Interesados")

with evidence_tab:
    st.subheader("Abrir la evidencia detrás de una categoría")
    st.caption(
        "Selecciona una barra o categoría equivalente y consulta las fichas "
        "que sustentan su conteo."
    )
    available_dimensions = {
        DIMENSION_LABELS[key]: key
        for key in DIMENSION_LABELS
        if report.get("dimensions", {}).get(key, {}).get("available")
        and report.get("dimensions", {}).get(key, {}).get("items")
    }
    if not available_dimensions:
        st.info("No hay categorías con evidencia para los filtros seleccionados.")
    else:
        selected_dimension_label = st.selectbox(
            "Dimensión",
            list(available_dimensions),
            key="analytics_drilldown_dimension",
        )
        selected_dimension = available_dimensions[selected_dimension_label]
        dimension_items = report["dimensions"][selected_dimension]["items"]
        item_by_value = {str(item.get("value")): item for item in dimension_items}
        selected_value_key = st.selectbox(
            "Categoría",
            list(item_by_value),
            format_func=lambda value: (
                f"{_dimension_label(selected_dimension, item_by_value[value])} · "
                + (
                    f"{_format_count(item_by_value[value].get('records'))} fichas"
                    if item_by_value[value].get("records") is not None
                    else (
                        f"{_format_count(item_by_value[value].get('documents'))} "
                        "documentos"
                    )
                )
            ),
            key=f"analytics_drilldown_value_{selected_dimension}",
        )
        selected_item = item_by_value[selected_value_key]
        detail_page_size = st.select_slider(
            "Evidencias por página",
            options=[10, 25, 50, 100],
            value=25,
            key="analytics_detail_page_size",
        )
        detail_signature = hashlib.sha256(
            json.dumps(
                [selected_dimension, selected_value_key, filters],
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:12]
        page_key = f"analytics_detail_page_{detail_signature}"
        detail_page = max(1, int(st.session_state.get(page_key, 1)))
        detail = cached_drilldown(
            selected_dimension,
            selected_item.get("value"),
            filters_json,
            int(detail_page_size),
            (detail_page - 1) * int(detail_page_size),
            _file_identity(DATABASE_PATH),
        )
        if not detail.get("available"):
            st.info("No fue posible recuperar evidencia para esta categoría.")
        else:
            total_evidence = int(detail.get("total") or 0)
            detail_pages = max(1, math.ceil(total_evidence / detail_page_size))
            if detail_page > detail_pages:
                st.session_state[page_key] = detail_pages
                st.rerun()
            st.caption(
                f"{_format_count(total_evidence)} ficha(s) o documento(s) · "
                f"página {detail_page} de {detail_pages}."
            )
            evidence_rows = []
            for item in detail.get("items", []):
                source = pdf_page_url(
                    str(item.get("url") or ""),
                    int(item.get("page") or 1),
                    ALLOWED_DOCUMENT_HOSTS,
                )
                evidence_rows.append(
                    {
                        "Año": item.get("year"),
                        "Acta": item.get("acta_number"),
                        "Numeral": item.get("numeral"),
                        "Página": item.get("page"),
                        "Documento": item.get("title"),
                        "Evidencia": item.get("evidence_text") or "",
                        "Fuente": source or item.get("url") or "",
                    }
                )
            if evidence_rows:
                st.dataframe(
                    evidence_rows,
                    hide_index=True,
                    use_container_width=True,
                    column_config={
                        "Fuente": st.column_config.LinkColumn(
                            "Fuente oficial",
                            display_text="Abrir página",
                        ),
                        "Año": st.column_config.NumberColumn(format="%d"),
                    },
                )
            else:
                st.info("La categoría no contiene evidencias navegables.")

            previous_col, status_col, next_col = st.columns(
                [1, 2, 1], vertical_alignment="center"
            )
            if previous_col.button(
                "← Anterior",
                disabled=detail_page <= 1,
                key=f"analytics_previous_{detail_signature}_{detail_page}",
                use_container_width=True,
            ):
                st.session_state[page_key] = detail_page - 1
                st.rerun()
            status_col.markdown(
                f"Página **{detail_page}** de **{detail_pages}**"
            )
            if next_col.button(
                "Siguiente →",
                disabled=detail_page >= detail_pages,
                key=f"analytics_next_{detail_signature}_{detail_page}",
                use_container_width=True,
            ):
                st.session_state[page_key] = detail_page + 1
                st.rerun()

        if selected_dimension in EXPLORER_DRILLDOWN_DIMENSIONS:
            if st.button(
                "Continuar esta consulta en el Explorador",
                key=f"analytics_explore_{detail_signature}",
                type="primary",
            ):
                st.session_state.update(
                    analytics_explorer_state(
                        filters,
                        dimension=selected_dimension,
                        query_value=selected_item.get("value"),
                    )
                )
                st.session_state.pop("explorer_search_signature", None)
                st.session_state.pop("explorer_export_artifact", None)
                st.switch_page("pages/1_Explorador.py")
