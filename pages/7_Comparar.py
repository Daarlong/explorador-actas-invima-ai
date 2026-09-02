from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

import streamlit as st

from config import ALLOWED_DOCUMENT_HOSTS, DATABASE_PATH, REVIEW_LOG_PATH
from services.comparison import (
    MAX_COMPARISON_ITEMS,
    MAX_SELECTED_SOURCES,
    MAX_TIMELINE_QUERY_CHARS,
    comparison_matrix,
    comparison_to_csv,
    load_selected_decisions,
    outcome_label,
    printable_html_report,
    request_type_label,
    review_status_label,
    search_timeline,
    timeline_to_csv,
)
from services.database import database_stats
from services.reviews import parse_review_events
from services.ui_helpers import pdf_page_url


st.set_page_config(page_title="Comparar decisiones", page_icon="⚖️", layout="wide")
st.title("⚖️ Comparar decisiones y construir cronologías")
st.caption("Contrasta fichas lado a lado y conserva el enlace a cada evidencia")

MAX_REVIEW_LOG_BYTES = 5_000_000


def _file_identity(path: Path) -> tuple[int, int]:
    if not path.exists():
        return (0, 0)
    stat = path.stat()
    return (stat.st_size, stat.st_mtime_ns)


@st.cache_data(show_spinner=False, ttl=1800)
def cached_selected_decisions(
    selected_json: str,
    review_payload: str,
    database_identity: tuple[int, int],
):
    del database_identity
    return load_selected_decisions(
        DATABASE_PATH,
        json.loads(selected_json),
        review_events=parse_review_events(review_payload),
    )


@st.cache_data(show_spinner=False, ttl=1800)
def cached_timeline(
    field: str,
    value: str,
    limit: int,
    review_payload: str,
    database_identity: tuple[int, int],
):
    del database_identity
    return search_timeline(
        DATABASE_PATH,
        field,
        value,
        limit=limit,
        review_events=parse_review_events(review_payload),
    )


def render_comparison_report_download(selected_decisions) -> None:
    """Mantiene disponible el reporte aunque una cronologia no tenga resultados."""

    if not selected_decisions:
        return
    report_signature = hashlib.sha256(
        "|".join(item.key for item in selected_decisions).encode("utf-8")
    ).hexdigest()[:8]
    try:
        printable_report = printable_html_report(selected_decisions)
    except ValueError as exc:
        st.error(str(exc))
        return
    st.download_button(
        "Descargar reporte imprimible de la comparación",
        data=printable_report,
        file_name=f"comparacion_invima_{report_signature}.html",
        mime="text/html",
        key=f"comparison_report_{report_signature}",
    )


if database_stats(DATABASE_PATH)["documents"] == 0:
    st.warning("No existe un índice. Ejecuta Construir índice desde GitHub Actions.")
    st.stop()

try:
    if (
        REVIEW_LOG_PATH.exists()
        and REVIEW_LOG_PATH.stat().st_size > MAX_REVIEW_LOG_BYTES
    ):
        raise ValueError("el registro de revisiones supera 5 MB")
    review_payload = (
        REVIEW_LOG_PATH.read_text(encoding="utf-8-sig")
        if REVIEW_LOG_PATH.exists()
        else ""
    )
    parse_review_events(review_payload)
except (OSError, UnicodeDecodeError, ValueError) as exc:
    st.error(
        "No se puede abrir la comparación de forma coherente porque falló el "
        f"registro de revisiones: {exc}"
    )
    st.stop()

comparison_snapshot = st.session_state.get("comparison_selected_sources")
current_selection = st.session_state.get("selected_evidence")
if isinstance(current_selection, dict):
    # La selección viva del Explorador prevalece sobre cualquier snapshot
    # anterior para que al desmarcar o limpiar no reaparezcan evidencias viejas.
    raw_selected = list(current_selection.values())
elif isinstance(comparison_snapshot, list):
    raw_selected = list(comparison_snapshot)
else:
    raw_selected = []
if len(raw_selected) > MAX_SELECTED_SOURCES:
    st.warning(
        f"Se procesan como maximo {MAX_SELECTED_SOURCES} fuentes por seguridad. "
        "Limpia la selección en el Explorador si necesitas otro conjunto."
    )

selection_json = json.dumps(
    raw_selected[:MAX_SELECTED_SOURCES],
    ensure_ascii=False,
    sort_keys=True,
)
decisions = cached_selected_decisions(
    selection_json,
    review_payload,
    _file_identity(DATABASE_PATH),
)
if len(decisions) >= MAX_COMPARISON_ITEMS:
    st.warning(
        f"La comparación está limitada a {MAX_COMPARISON_ITEMS} decisiones. "
        "Reduce la selección si necesitas comprobar que no quedaron fichas fuera."
    )
if any(item.evidence_truncated for item in decisions):
    st.warning(
        "Alguna decisión tiene más evidencias seleccionadas de las que se pueden "
        "mostrar en una sola comparación. Reduce la selección para verlas por grupos."
    )

with st.sidebar:
    st.header("Selección actual")
    st.metric("Evidencias para comparar", len(raw_selected))
    st.metric("Decisiones comparables", len(decisions))
    st.page_link("pages/1_Explorador.py", label="Volver al Explorador", icon="🔍")
    if st.button(
        "Limpiar evidencias",
        use_container_width=True,
        disabled=not raw_selected,
    ):
        st.session_state["selected_evidence"] = {}
        for key in list(st.session_state):
            if str(key).startswith("select_evidence_"):
                del st.session_state[key]
        st.session_state.pop("comparison_selected_sources", None)
        st.rerun()

st.subheader("Comparación")
if not decisions:
    if raw_selected:
        st.warning(
            "La selección ya no coincide con el índice vigente. Límpiala y vuelve "
            "a seleccionar las evidencias en el Explorador."
        )
    else:
        st.info(
            "Selecciona uno o más fragmentos en el Explorador y regresa a esta "
            "página. La cronología inferior funciona aun sin selección."
        )
    selected_decisions = []
else:
    decision_by_key = {decision.key: decision for decision in decisions}
    default_keys = list(decision_by_key)[: min(4, len(decision_by_key))]
    chosen_keys = st.multiselect(
        "Decisiones para contrastar",
        options=list(decision_by_key),
        default=default_keys,
        format_func=lambda value: decision_by_key[value].label,
        max_selections=min(8, MAX_COMPARISON_ITEMS),
        help=(
            "Una misma ficha respaldada por varios fragmentos aparece una sola vez. "
            "Los fragmentos sin ficha también se pueden comparar."
        ),
    )
    selected_decisions = [decision_by_key[key] for key in chosen_keys]

    if selected_decisions:
        st.dataframe(
            comparison_matrix(selected_decisions),
            hide_index=True,
            use_container_width=True,
            height=565,
        )
        export_left, export_right = st.columns(2)
        try:
            comparison_csv = comparison_to_csv(selected_decisions)
        except ValueError as exc:
            export_left.error(str(exc))
        else:
            export_left.download_button(
                "Descargar comparación CSV",
                data=comparison_csv,
                file_name="comparacion_decisiones_invima.csv",
                mime="text/csv",
                use_container_width=True,
            )
        export_right.caption(
            "El reporte imprimible de la parte inferior puede incluir también "
            "la cronología."
        )

        st.markdown("#### Evidencias verificables")
        for index, decision in enumerate(selected_decisions, start=1):
            with st.expander(f"Decision {index}: {decision.label}"):
                metadata = [decision.title]
                if decision.section:
                    metadata.append(decision.section)
                if decision.part:
                    metadata.append(decision.part)
                metadata.append(outcome_label(decision.outcome_code))
                if decision.record_id is None:
                    metadata.append("fragmento sin ficha estructurada")
                elif decision.needs_review:
                    metadata.append("extracción pendiente de revisión")
                elif decision.review_status:
                    metadata.append(
                        f"revisión: {review_status_label(decision.review_status)}"
                    )
                if decision.review_stale:
                    metadata.append("revisión desactualizada")
                st.caption(" · ".join(metadata))
                for evidence_index, evidence in enumerate(
                    decision.evidences,
                    start=1,
                ):
                    st.markdown(f"**Página {evidence.page}**")
                    st.write(evidence.text)
                    evidence_url = pdf_page_url(
                        decision.url,
                        evidence.page,
                        ALLOWED_DOCUMENT_HOSTS,
                    )
                    if evidence_url:
                        st.link_button(
                            f"Abrir evidencia D{index}.{evidence_index} · página "
                            f"{evidence.page} · F{evidence.chunk_id}",
                            evidence_url,
                        )
                source_url = pdf_page_url(
                    decision.url,
                    decision.page_number,
                    ALLOWED_DOCUMENT_HOSTS,
                )
                if source_url:
                    st.link_button(
                        f"Abrir ficha D{index} desde la página "
                        f"{decision.page_number}",
                        source_url,
                    )
    else:
        st.warning("Elige al menos una decisión para mostrar la comparación.")

st.divider()
st.subheader("Cronología de precedentes")
st.write(
    "Busca todas las fichas relacionadas con un producto, principio activo, "
    "expediente o radicado. Se ordenan por fecha de sesión y, si falta, por "
    "año y número de acta."
)

timeline_field_labels = {
    "Producto": "product",
    "Principio activo": "active_ingredient",
    "Expediente": "expediente",
    "Radicado": "radicado",
}
timeline_field_label = st.selectbox("Construir cronología por", timeline_field_labels)
timeline_field = timeline_field_labels[timeline_field_label]

candidate_attribute = {
    "product": "product_name",
    "active_ingredient": "active_ingredient",
    "expediente": "expediente",
    "radicado": "radicado",
}[timeline_field]
candidates = sorted(
    {
        str(getattr(item, candidate_attribute)).strip()
        for item in decisions
        if getattr(item, candidate_attribute)
    },
    key=str.casefold,
)
default_value = candidates[0] if candidates else ""
timeline_value = st.text_input(
    f"{timeline_field_label} a rastrear",
    value=default_value,
    key=f"timeline_input_{timeline_field}",
    placeholder="Escribe un valor extraído de las actas",
    max_chars=MAX_TIMELINE_QUERY_CHARS,
)
timeline_limit = st.select_slider(
    "Máximo de decisiones",
    options=[25, 50, 100, 150, 250],
    value=100,
)

if st.button("Construir cronología", type="primary"):
    st.session_state.pop("comparison_timeline_request", None)
    try:
        timeline_results = cached_timeline(
            timeline_field,
            timeline_value.strip(),
            timeline_limit,
            review_payload,
            _file_identity(DATABASE_PATH),
        )
        st.session_state["comparison_timeline_request"] = {
            "field": timeline_field,
            "field_label": timeline_field_label,
            "value": timeline_value.strip(),
            "limit": timeline_limit,
        }
    except (ValueError, sqlite3.Error) as exc:
        st.error(str(exc))

timeline_request = st.session_state.get("comparison_timeline_request")
timeline_results = []
if timeline_request:
    try:
        timeline_results = cached_timeline(
            timeline_request["field"],
            timeline_request["value"],
            int(timeline_request["limit"]),
            review_payload,
            _file_identity(DATABASE_PATH),
        )
    except (KeyError, TypeError, ValueError, sqlite3.Error) as exc:
        st.session_state.pop("comparison_timeline_request", None)
        st.error(f"No fue posible construir la cronología: {exc}")

if timeline_request and not timeline_results:
    st.warning("No se encontraron fichas estructuradas para ese valor.")
    render_comparison_report_download(selected_decisions)
elif timeline_results:
    st.success(
        f"{len(timeline_results)} decisiones para "
        f"{timeline_request['field_label']}: {timeline_request['value']}"
    )
    timeline_table = [
        {
            "Año": item.year,
            "Acta": item.acta_number,
            "Página": item.page_number,
            "Fecha de sesión": item.session_date,
            "Numeral": item.numeral,
            "Producto": item.product_name,
            "Principio activo": item.active_ingredient,
            "Expediente": item.expediente,
            "Radicado": item.radicado,
            "Resultado": outcome_label(item.outcome_code),
            "Tipo de solicitud": request_type_label(item.request_type),
            "Estado de revisión": review_status_label(item.review_status),
        }
        for item in timeline_results
    ]
    st.dataframe(
        timeline_table,
        hide_index=True,
        use_container_width=True,
        height=min(560, 38 + 35 * len(timeline_table)),
    )
    for index, item in enumerate(timeline_results[:50], start=1):
        with st.expander(f"{index}. {item.label} · {outcome_label(item.outcome_code)}"):
            if item.request_text:
                st.markdown("**Solicitud**")
                st.write(item.request_text)
            if item.concept_text:
                st.markdown("**Concepto**")
                st.write(item.concept_text)
            source_url = pdf_page_url(
                item.url,
                item.page_number,
                ALLOWED_DOCUMENT_HOSTS,
            )
            if source_url:
                st.link_button(f"Abrir evidencia {index}", source_url)
    if len(timeline_results) > 50:
        st.caption("Se muestran 50 detalles; el CSV contiene toda la cronología.")

    timeline_download, report_download = st.columns(2)
    try:
        timeline_csv = timeline_to_csv(timeline_results)
    except ValueError as exc:
        timeline_download.error(str(exc))
    else:
        timeline_download.download_button(
            "Descargar cronología CSV",
            data=timeline_csv,
            file_name="cronologia_invima.csv",
            mime="text/csv",
            use_container_width=True,
        )
    report_title = (
        "Comparación INVIMA y cronología de "
        f"{timeline_request['field_label']}: {timeline_request['value']}"
    )
    try:
        printable_report = printable_html_report(
            selected_decisions,
            timeline=timeline_results,
            title=report_title,
        )
    except ValueError as exc:
        report_download.error(str(exc))
    else:
        report_download.download_button(
            "Descargar reporte imprimible HTML",
            data=printable_report,
            file_name="reporte_comparacion_invima.html",
            mime="text/html",
            use_container_width=True,
        )
    st.caption(
        "Abre el HTML en el navegador y usa Ctrl+P → Guardar como PDF. "
        "Las fuentes quedan enlazadas a la página correspondiente."
    )
elif selected_decisions:
    render_comparison_report_download(selected_decisions)

st.caption(
    "Las fichas y resultados se extraen automáticamente. Verifica cualquier "
    "conclusión contra la página enlazada antes de usarla."
)
